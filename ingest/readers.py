"""Read a raw partition into normalized, venue-independent event arrays.

The book replay (book/replay.py) only sees `BookEvents` and `Trades`, so adding a venue means
adding a reader here, not touching the book.

BookEvents rows are level changes in arrival order. Rows that share `msg_id` came in one
exchange message and are applied together (invariants are checked between messages, not
inside one). `kind`:
    0  update    set the level to `size` (0 deletes it)
    1  snapshot  all rows of a snapshot message; the book is cleared before the first one
    2  invalid   marker: the book is unreliable from `recv_ts_ns` until the next snapshot
                 (sequence gap, disconnect, stale feed, planned resync)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from common.paths import raw_dir

UPDATE, SNAPSHOT, INVALID = 0, 1, 2


@dataclass
class BookEvents:
    recv_ts_ns: np.ndarray  # int64
    event_ts_ns: np.ndarray  # int64, exchange time of the level change (0 if unknown)
    msg_id: np.ndarray  # int64, non-decreasing
    kind: np.ndarray  # int8
    is_bid: np.ndarray  # bool
    price: np.ndarray  # float64
    size: np.ndarray  # float64

    def __len__(self) -> int:
        return len(self.recv_ts_ns)


@dataclass
class Trades:
    recv_ts_ns: np.ndarray  # int64
    event_ts_ns: np.ndarray  # int64
    price: np.ndarray  # float64
    size: np.ndarray  # float64
    is_buy: np.ndarray  # bool: aggressor (taker) was the buyer
    trade_id: np.ndarray  # object (str)

    def __len__(self) -> int:
        return len(self.recv_ts_ns)


@dataclass
class AuditSnapshot:
    """An exchange-sent full-depth snapshot captured on a separate connection."""

    recv_ts_ns: int
    event_ts_ns: int  # exchange time the snapshot represents
    bids: dict[float, float]
    asks: dict[float, float]


# --- Tardis (historical Coinbase Exchange feed) ---------------------------------------------


def _tardis_dir(symbol: str, date: str, base: Path | None) -> Path:
    return (
        base / "coinbase_tardis" / symbol / date
        if base
        else raw_dir("coinbase_tardis", symbol, date)
    )


def read_tardis_book(symbol: str, date: str, base: Path | None = None) -> BookEvents:
    path = _tardis_dir(symbol, date, base) / "incremental_book_L2.parquet"
    t = pq.read_table(path)
    recv = t["recv_ts_ns"].to_numpy()
    snap = t["is_snapshot"].to_numpy(zero_copy_only=False)
    n = len(recv)
    # Message boundaries: a new receive timestamp, or a switch between snapshot and update
    # rows. A run of snapshot rows is one snapshot message even if it spans timestamps.
    new_msg = np.ones(n, dtype=bool)
    if n > 1:
        ts_change = recv[1:] != recv[:-1]
        snap_change = snap[1:] != snap[:-1]
        both_snap = snap[1:] & snap[:-1]
        new_msg[1:] = (ts_change & ~both_snap) | snap_change
    msg_id = np.cumsum(new_msg) - 1
    return BookEvents(
        recv_ts_ns=recv,
        event_ts_ns=t["exchange_ts_ns"].to_numpy(),
        msg_id=msg_id.astype(np.int64),
        kind=np.where(snap, SNAPSHOT, UPDATE).astype(np.int8),
        is_bid=(t["side"].to_numpy(zero_copy_only=False) == "bid"),
        price=t["price"].to_numpy(),
        size=t["size"].to_numpy(),
    )


def read_tardis_trades(symbol: str, date: str, base: Path | None = None) -> Trades:
    path = _tardis_dir(symbol, date, base) / "trades.parquet"
    t = pq.read_table(path)
    return Trades(
        recv_ts_ns=t["recv_ts_ns"].to_numpy(),
        event_ts_ns=t["exchange_ts_ns"].to_numpy(),
        price=t["price"].to_numpy(),
        size=t["size"].to_numpy(),
        is_buy=(t["side"].to_numpy(zero_copy_only=False) == "buy"),
        trade_id=t["trade_id"].to_numpy(zero_copy_only=False),
    )


# --- live collector (Coinbase Advanced Trade WebSocket) -------------------------------------

# Control events after which the main connection's book can't be trusted until a new snapshot.
_INVALIDATING = {"gap", "disconnected", "stale", "rollover", "shutdown", "exchange_error"}

# market_trades `side` on the Advanced Trade feed is the MAKER side (checked against the book,
# see docs/data.md §Trade side): "BUY" means a resting bid was hit, i.e. a seller aggressed.
WS_TRADE_SIDE_IS_MAKER = True


def _ts(s: str) -> int:
    return int(np.datetime64(s.rstrip("Z"), "ns").astype(np.int64))


def read_ws_messages(symbol: str, date: str, base: Path | None = None) -> list[dict]:
    """Raw rows of one live partition, in arrival order."""
    d = (base / "coinbase_ws" / symbol / date) if base else raw_dir("coinbase_ws", symbol, date)
    files = sorted(d.glob("part-*.parquet"))
    if not files:
        raise FileNotFoundError(d)
    t = ds.dataset([str(f) for f in files], format="parquet").to_table()
    rows = t.to_pylist()
    # Part files are named by their first recv_ts_ns, so sorting files keeps arrival order;
    # a stable sort on recv_ts_ns guards against two buffers overlapping in time.
    rows.sort(key=lambda r: r["recv_ts_ns"])
    return rows


def read_ws(
    symbol: str, date: str, base: Path | None = None
) -> tuple[BookEvents, Trades, list[AuditSnapshot]]:
    rows = read_ws_messages(symbol, date, base)
    cols: dict[str, list] = {k: [] for k in ("recv", "evt", "msg", "kind", "bid", "px", "sz")}
    tr: dict[str, list] = {k: [] for k in ("recv", "evt", "px", "sz", "buy", "id")}
    audits: list[AuditSnapshot] = []
    last_seq: dict[str, int] = {}
    current_conn: str | None = None
    msg = 0

    def invalid(recv: int) -> None:
        nonlocal msg
        msg += 1
        for k, v in zip(cols, (recv, 0, msg, INVALID, False, np.nan, np.nan), strict=True):
            cols[k].append(v)

    for r in rows:
        recv = r["recv_ts_ns"]
        if r["stream"] == "audit":
            if r["channel"] == "l2_data":
                audits.extend(_parse_audit(r["payload"], recv))
            continue
        # Main stream from here on.
        if r["conn_id"] != current_conn:
            if current_conn is not None:
                invalid(recv)
            current_conn = r["conn_id"]
        if r["channel"] == "_control":
            if json.loads(r["payload"]).get("event") in _INVALIDATING:
                invalid(recv)
            continue
        seq = r["conn_seq"]
        if seq is not None:
            prev = last_seq.get(r["conn_id"])
            if (prev is None and seq != 0) or (prev is not None and seq != prev + 1):
                invalid(recv)  # independent re-check of the collector's gap detection
            last_seq[r["conn_id"]] = seq
        if r["channel"] == "l2_data":
            for ev in json.loads(r["payload"]).get("events", []):
                kind = SNAPSHOT if ev.get("type") == "snapshot" else UPDATE
                msg += 1
                prev_evt = None
                for u in ev.get("updates", []):
                    evt = _ts(u["event_time"])
                    # An update batch can span several exchange event times. Each distinct
                    # time is its own message, so invariants and golden checks see the book
                    # exactly as it was between matching-engine events.
                    if kind == UPDATE and prev_evt is not None and evt != prev_evt:
                        msg += 1
                    prev_evt = evt
                    cols["recv"].append(recv)
                    cols["evt"].append(evt)
                    cols["msg"].append(msg)
                    cols["kind"].append(kind)
                    cols["bid"].append(u["side"] == "bid")
                    cols["px"].append(float(u["price_level"]))
                    cols["sz"].append(float(u["new_quantity"]))
        elif r["channel"] == "market_trades":
            for ev in json.loads(r["payload"]).get("events", []):
                if ev.get("type") == "snapshot":
                    continue  # recent-trades backfill on subscribe, already seen or pre-session
                for t in ev.get("trades", []):
                    side_buy = t["side"].upper() == "BUY"
                    tr["recv"].append(recv)
                    tr["evt"].append(_ts(t["time"]))
                    tr["px"].append(float(t["price"]))
                    tr["sz"].append(float(t["size"]))
                    tr["buy"].append(not side_buy if WS_TRADE_SIDE_IS_MAKER else side_buy)
                    tr["id"].append(str(t["trade_id"]))
    book = BookEvents(
        recv_ts_ns=np.asarray(cols["recv"], dtype=np.int64),
        event_ts_ns=np.asarray(cols["evt"], dtype=np.int64),
        msg_id=np.asarray(cols["msg"], dtype=np.int64),
        kind=np.asarray(cols["kind"], dtype=np.int8),
        is_bid=np.asarray(cols["bid"], dtype=bool),
        price=np.asarray(cols["px"], dtype=np.float64),
        size=np.asarray(cols["sz"], dtype=np.float64),
    )
    trades = _dedupe_trades(
        Trades(
            recv_ts_ns=np.asarray(tr["recv"], dtype=np.int64),
            event_ts_ns=np.asarray(tr["evt"], dtype=np.int64),
            price=np.asarray(tr["px"], dtype=np.float64),
            size=np.asarray(tr["sz"], dtype=np.float64),
            is_buy=np.asarray(tr["buy"], dtype=bool),
            trade_id=np.asarray(tr["id"], dtype=object),
        )
    )
    return book, trades, audits


def _dedupe_trades(t: Trades) -> Trades:
    """Keep the first arrival of each trade_id (reconnects can replay recent trades)."""
    if len(t) == 0:
        return t
    _, first = np.unique(t.trade_id.astype(str), return_index=True)
    keep = np.sort(first)
    return Trades(*(getattr(t, f)[keep] for f in t.__dataclass_fields__))


def _parse_audit(payload: str, recv: int) -> list[AuditSnapshot]:
    out = []
    for ev in json.loads(payload).get("events", []):
        if ev.get("type") != "snapshot":
            continue
        bids: dict[float, float] = {}
        asks: dict[float, float] = {}
        event_ts = 0
        for u in ev.get("updates", []):
            (bids if u["side"] == "bid" else asks)[float(u["price_level"])] = float(
                u["new_quantity"]
            )
            event_ts = max(event_ts, _ts(u["event_time"]))
        out.append(AuditSnapshot(recv, event_ts, bids, asks))
    return out
