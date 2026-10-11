"""Deterministic replay: normalized book events → L2 book → 1 s grid snapshots.

Output: data/book/{symbol}/{date}.parquet, one row per whole UTC second t of the day:

    ts_ns                       grid time t (int64 ns UTC)
    bid_px_{m}, bid_sz_{m}      best 10 populated levels per side, as-of t (NaN if missing)
    ask_px_{m}, ask_sz_{m}
    n_bid_levels, n_ask_levels  populated levels per side
    book_valid                  book trustworthy at t (feature_label_spec §1)
    interval_valid              book_valid throughout (t − 1 s, t]
    last_book_recv_ts_ns        receive time of the last book message ≤ t
    n_book_msgs                 book messages received in (t − 1 s, t]
    n_trades, buy_volume, sell_volume   trades received in (t − 1 s, t], by aggressor side

"As-of t" means the state after every message with recv_ts_ns ≤ t. A feature over a window
(t − w, t] is valid iff `interval_valid` holds on all w seconds in it.

Validity rules:
    * invalid until the first snapshot has been applied;
    * a snapshot arriving after updates (vendor reconnect) or an INVALID marker (live gap,
      disconnect, stale feed, rollover) makes the book invalid from just after the last good
      message until the next snapshot is fully applied; updates in between are skipped;
    * invalid while crossed or locked (best bid ≥ best ask) or one-sided;
    * invalid after the last message in the input (nothing is known beyond the data).
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from book.l2book import InvariantError, L2Book, infer_price_scale, to_ticks
from common.meta import version_metadata, with_metadata
from common.paths import book_path
from ingest.readers import INVALID, SNAPSHOT, UPDATE, BookEvents, Trades

GRID_NS = 1_000_000_000
N_GRID = 86_400
DEPTH = 10
BOOK_SCHEMA_VERSION = "1"
CHUNK = 1 << 21


@dataclass
class ReplayStats:
    rows: int = 0
    messages: int = 0
    snapshots: int = 0
    resyncs: int = 0  # snapshots that replaced a live book (vendor/our reconnects)
    invalid_markers: int = 0
    skipped_updates: int = 0  # updates received while waiting for a snapshot
    deletes: int = 0
    phantom_deletes: int = 0  # deletes of a level not in the book: a lossy/conflated feed
    crossed_messages: int = 0
    crossed_episodes: int = 0
    max_bid_levels: int = 0
    max_ask_levels: int = 0
    price_scale: int = 0
    tick_size: float = 0.0
    replay_seconds: float = 0.0
    events_per_sec: float = 0.0
    invalid_seconds: float = 0.0
    invalid_periods: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class GridResult:
    columns: dict[str, np.ndarray]
    transitions_ts: np.ndarray
    transitions_valid: np.ndarray
    stats: ReplayStats


def day_start_ns(date: str) -> int:
    return int(np.datetime64(date, "ns").astype(np.int64))


def tick_size_from(price_ints: np.ndarray, scale: int) -> float:
    u = np.unique(price_ints)
    if len(u) < 2:
        return 1.0 / scale
    return math.gcd(*np.unique(np.diff(u)).tolist()) / scale


def replay_to_grid(
    ev: BookEvents,
    day_start: int,
    n_grid: int = N_GRID,
    depth: int = DEPTH,
    scale: int | None = None,
    before_message: Callable[[int, int, L2Book, bool], None] | None = None,
) -> GridResult:
    """Replay `ev` and sample the book on the grid day_start + g·1 s, g = 0..n_grid−1.

    `before_message(recv_ts, first_event_ts, book, valid)`, if given, is called when a new
    message starts, before it is applied: `book` is then the state after every earlier
    message. The golden tests use it to compare against reference snapshots.
    """
    t0 = time.perf_counter()
    stats = ReplayStats(rows=len(ev))
    real = ev.kind != INVALID
    if scale is None:
        scale = infer_price_scale(ev.price[real]) if real.any() else 1
    stats.price_scale = scale
    px = np.zeros(len(ev), dtype=np.int64)
    px[real] = to_ticks(ev.price[real], scale)
    stats.tick_size = tick_size_from(px[real], scale) if real.any() else 0.0

    book = L2Book(scale)
    bids, asks = book.bids, book.asks

    bid_px = np.full((n_grid, depth), np.nan)
    bid_sz = np.full((n_grid, depth), np.nan)
    ask_px = np.full((n_grid, depth), np.nan)
    ask_sz = np.full((n_grid, depth), np.nan)
    n_bid = np.zeros(n_grid, dtype=np.int32)
    n_ask = np.zeros(n_grid, dtype=np.int32)
    last_msg = np.zeros(n_grid, dtype=np.int64)

    tr_ts: list[int] = []
    tr_v: list[bool] = []
    state = {"valid": False, "awaiting": True, "crossed": False, "last_msg_ts": 0}

    def record(ts: int, v: bool) -> None:
        if v != state["valid"]:
            tr_ts.append(ts)
            tr_v.append(v)
            state["valid"] = v

    g = 0
    next_grid = day_start
    fscale = float(scale)  # ticks / scale is correctly rounded: equals float(decimal price)

    def emit_until(ts: int) -> None:
        """Emit every grid point strictly before `ts` (all messages ≤ it are applied)."""
        nonlocal g, next_grid
        while g < n_grid and next_grid < ts:
            nb, na = len(bids), len(asks)
            n_bid[g], n_ask[g] = nb, na
            last_msg[g] = state["last_msg_ts"]
            for i in range(min(depth, nb)):
                p, s = bids.peekitem(-1 - i)
                bid_px[g, i] = p / fscale
                bid_sz[g, i] = s
            for i in range(min(depth, na)):
                p, s = asks.peekitem(i)
                ask_px[g, i] = p / fscale
                ask_sz[g, i] = s
            g += 1
            next_grid += GRID_NS

    def end_message(ts: int, kind: int) -> None:
        if kind == INVALID:
            return
        stats.messages += 1
        state["last_msg_ts"] = ts
        if kind == SNAPSHOT:
            state["awaiting"] = False
        if state["awaiting"]:
            return
        crossed = book.is_crossed_or_locked()
        if crossed:
            stats.crossed_messages += 1
            if not state["crossed"]:
                stats.crossed_episodes += 1
        state["crossed"] = crossed
        record(ts, (not crossed) and book.is_two_sided())
        stats.max_bid_levels = max(stats.max_bid_levels, len(bids))
        stats.max_ask_levels = max(stats.max_ask_levels, len(asks))

    def invalidate() -> None:
        # Messages may have been missed any time after the last good one.
        record(state["last_msg_ts"] + 1, False)
        state["awaiting"] = True

    cur_msg = -1
    cur_kind = UPDATE
    cur_ts = -1
    n = len(ev)
    for c0 in range(0, n, CHUNK):
        c1 = min(n, c0 + CHUNK)
        L_msg = ev.msg_id[c0:c1].tolist()
        L_ts = ev.recv_ts_ns[c0:c1].tolist()
        L_evt = ev.event_ts_ns[c0:c1].tolist()
        L_kind = ev.kind[c0:c1].tolist()
        L_bid = ev.is_bid[c0:c1].tolist()
        L_px = px[c0:c1].tolist()
        L_sz = ev.size[c0:c1].tolist()
        for i in range(c1 - c0):
            m = L_msg[i]
            if m != cur_msg:
                if cur_msg >= 0:
                    end_message(cur_ts, cur_kind)
                ts = L_ts[i]
                if ts < cur_ts:
                    raise InvariantError(f"receive time went backwards: {ts} < {cur_ts}")
                if m < cur_msg:
                    raise InvariantError(f"message id went backwards: {m} < {cur_msg}")
                emit_until(ts)
                if before_message is not None:
                    before_message(
                        ts, L_evt[i], book, bool(state["valid"] and not state["awaiting"])
                    )
                cur_msg, cur_ts, cur_kind = m, ts, L_kind[i]
                if cur_kind == SNAPSHOT:
                    stats.snapshots += 1
                    if not state["awaiting"]:
                        stats.resyncs += 1
                        invalidate()
                    book.clear()
                elif cur_kind == INVALID:
                    stats.invalid_markers += 1
                    if not state["awaiting"]:
                        invalidate()
            k = L_kind[i]
            if k == INVALID:
                continue
            if k == UPDATE and state["awaiting"]:
                stats.skipped_updates += 1
                continue
            s = L_sz[i]
            side = bids if L_bid[i] else asks
            if s > 0.0:
                side[L_px[i]] = s
            elif s == 0.0:
                if side.pop(L_px[i], None) is None and k == UPDATE:
                    stats.phantom_deletes += 1
                if k == UPDATE:
                    stats.deletes += 1
            else:
                raise InvariantError(f"negative size {s} at message {m}")
    if cur_msg >= 0:
        end_message(cur_ts, cur_kind)
        # Nothing is known after the last message: the book is not valid past the data.
        record(state["last_msg_ts"] + 1, False)
    emit_until(day_start + n_grid * GRID_NS + 1)

    stats.replay_seconds = time.perf_counter() - t0
    stats.events_per_sec = len(ev) / stats.replay_seconds if stats.replay_seconds else 0.0
    columns: dict[str, np.ndarray] = {
        "ts_ns": day_start + np.arange(n_grid, dtype=np.int64) * GRID_NS
    }
    for m in range(depth):
        columns[f"bid_px_{m + 1}"] = bid_px[:, m]
        columns[f"bid_sz_{m + 1}"] = bid_sz[:, m]
    for m in range(depth):
        columns[f"ask_px_{m + 1}"] = ask_px[:, m]
        columns[f"ask_sz_{m + 1}"] = ask_sz[:, m]
    columns["n_bid_levels"] = n_bid
    columns["n_ask_levels"] = n_ask
    columns["last_book_recv_ts_ns"] = last_msg
    T = np.asarray(tr_ts, dtype=np.int64)
    V = np.asarray(tr_v, dtype=bool)
    bv, iv = validity_on_grid(T, V, columns["ts_ns"])
    columns["book_valid"] = bv
    columns["interval_valid"] = iv
    stats.invalid_periods = invalid_periods(T, V, day_start, day_start + n_grid * GRID_NS)
    stats.invalid_seconds = sum(b - a for a, b in stats.invalid_periods) / 1e9
    return GridResult(columns, T, V, stats)


def validity_on_grid(
    T: np.ndarray, V: np.ndarray, grid: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """book_valid(t) and interval_valid(t) = valid on all of (t − 1 s, t]."""

    def valid_at(x: np.ndarray) -> np.ndarray:
        idx = np.searchsorted(T, x, side="right") - 1
        out = np.zeros(len(x), dtype=bool)
        ok = idx >= 0
        out[ok] = V[idx[ok]]
        return out

    def n_invalidations_upto(x: np.ndarray) -> np.ndarray:
        cf = np.concatenate([[0], np.cumsum(~V)])
        return cf[np.searchsorted(T, x, side="right")]

    bv = valid_at(grid)
    prev = grid - GRID_NS
    iv = valid_at(prev) & (n_invalidations_upto(grid) == n_invalidations_upto(prev))
    return bv, iv


def invalid_periods(T: np.ndarray, V: np.ndarray, start: int, end: int) -> list[tuple[int, int]]:
    """[a, b) intervals inside [start, end) where the book is not valid."""
    out: list[tuple[int, int]] = []
    cur_valid, since = False, start
    for ts, v in zip(T.tolist(), V.tolist(), strict=True):
        if v == cur_valid:
            continue
        if not v:
            since = max(ts, start)
        elif ts > start:
            out.append((since, min(ts, end)))
        cur_valid = v
    if not cur_valid:
        out.append((since, end))
    return [(a, b) for a, b in out if b > a]


def trades_per_second(tr: Trades, day_start: int, n_grid: int = N_GRID) -> dict[str, np.ndarray]:
    """Trades received in (t − 1 s, t] for each grid t."""
    k = -((day_start - tr.recv_ts_ns) // GRID_NS)  # ceil((recv - day_start) / 1 s)
    ok = (k >= 0) & (k < n_grid)
    k, buy, size = k[ok], tr.is_buy[ok], tr.size[ok]
    return {
        "n_trades": np.bincount(k, minlength=n_grid).astype(np.int32),
        "buy_volume": np.bincount(k[buy], weights=size[buy], minlength=n_grid),
        "sell_volume": np.bincount(k[~buy], weights=size[~buy], minlength=n_grid),
    }


def messages_per_second(ev: BookEvents, day_start: int, n_grid: int = N_GRID) -> np.ndarray:
    real = ev.kind != INVALID
    first = np.ones(len(ev), dtype=bool)
    first[1:] = ev.msg_id[1:] != ev.msg_id[:-1]
    ts = ev.recv_ts_ns[first & real]
    k = -((day_start - ts) // GRID_NS)
    k = k[(k >= 0) & (k < n_grid)]
    return np.bincount(k, minlength=n_grid).astype(np.int32)


def build_book_grid(
    ev: BookEvents,
    tr: Trades,
    date: str,
    symbol: str,
    source: str,
    source_version: str,
) -> tuple[pa.Table, ReplayStats]:
    ds0 = day_start_ns(date)
    res = replay_to_grid(ev, ds0)
    cols = res.columns
    cols["n_book_msgs"] = messages_per_second(ev, ds0)
    cols.update(trades_per_second(tr, ds0))
    table = pa.table(cols)
    st = res.stats
    meta = version_metadata(
        source_version=source_version,
        book_schema_version=BOOK_SCHEMA_VERSION,
        source=source,
        symbol=symbol,
        date=date,
        price_scale=str(st.price_scale),
        tick_size=repr(st.tick_size),
        n_trades=str(len(tr)),
        replay_stats=json.dumps(asdict(st)),
    )
    return with_metadata(table, meta), st


def write_book_grid(table: pa.Table, symbol: str, date: str) -> str:
    path = book_path(symbol, date)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(path)  # derived data: regenerating overwrites
    return str(path)
