"""Golden tests for book reconstruction. Mismatches must be 0.

Both checks run the production replay (book.replay.replay_to_grid) and compare its book,
through the `before_message` hook, against an independent reference:

* **live** (`coinbase_ws`): the exchange's own full-depth snapshots, captured every 15 min on a
  separate "audit" connection. Each snapshot represents exchange time T (its `event_time`).
  The main-stream book is compared just before the first message with event time > T, on
  **every** price level of both sides.

* **historical** (`coinbase_tardis`): Tardis's `book_snapshot_25` files, which Tardis builds
  from the same feed with its own reconstruction code. Each row is the top 25 levels after
  all messages received up to its `local_timestamp`. We compare our top 25 at the same time.

Run:  python -m book.golden live BTC-USD 2026-10-12
      python -m book.golden tardis HBAR-USD 2025-03-01 [--max-rows 500000]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pyarrow.csv as pacsv
import requests

from book.l2book import L2Book, infer_price_scale, to_ticks
from book.replay import day_start_ns, replay_to_grid
from common.paths import BOOK, VENDOR_CACHE
from ingest.readers import UPDATE, read_tardis_book, read_ws
from ingest.tardis import dataset_url

GOLDEN_DIR = BOOK / "_golden"


@dataclass
class GoldenReport:
    kind: str
    symbol: str
    date: str
    references: int = 0
    compared: int = 0
    skipped_invalid: int = 0
    skipped_unaligned: int = 0
    levels_compared: int = 0
    exact_single_state: int = 0
    unverifiable_levels: int = 0
    exact_at_stamp: int = 0
    mismatched_snapshots: int = 0
    mismatched_levels: int = 0
    examples: list[dict] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.compared > 0 and self.mismatched_levels == 0

    def to_json(self) -> str:
        return json.dumps({**asdict(self), "passed": self.passed}, indent=1)


def _diff_levels(ours: dict[int, float], ref: dict[int, float]) -> list[tuple[int, float, float]]:
    out = []
    for p in ours.keys() | ref.keys():
        a, b = ours.get(p, 0.0), ref.get(p, 0.0)
        if a != b:
            out.append((p, a, b))
    return out


# --- live: exchange audit snapshots ---------------------------------------------------------


ALIGN_WINDOW_NS = 250_000_000


@dataclass
class _AuditCheck:
    """One audit snapshot checked against the main stream around its stamped time T."""

    t: int
    bids: dict[int, float]
    asks: dict[int, float]
    allowed: dict[tuple[bool, int], set[float]]  # values each touched level took in the window
    straddle: set[tuple[bool, int]]  # levels in the batches either side of T: not verifiable
    phase: int = 0  # 0 before the window, 1 inside, 2 done
    outside: list = field(default_factory=list)  # mismatches on levels untouched in the window
    best_state: int | None = None  # fewest total mismatches at any single state in the window
    at_stamp: int | None = None  # total mismatches at the state as-of T


def golden_live(
    symbol: str, date: str, base: Path | None = None, window_ns: int = ALIGN_WINDOW_NS
) -> GoldenReport:
    """Check the main-stream book against every exchange audit snapshot.

    An audit snapshot is stamped with an exchange `event_time` T. Updates that share one
    event time can straddle the snapshot (it can include some updates of a batch and not
    others), and the stamp itself is only good to a few tens of ms. So a single "book at T"
    need not equal the snapshot exactly even when the reconstruction is right. The check is
    per price level, both sides:

      * a level not updated with event time in (T − window, T + window] must equal the
        snapshot exactly;
      * a level updated in that window must equal one of the values it held in the window.

    The Advanced Trade feed also **conflates**: each message carries the final state of the
    levels that changed since the previous message, so intermediate states inside a batch are
    never sent (1-15% of its deletes remove a level we never saw; the Tardis Exchange feed has
    none). A snapshot taken inside a batch can therefore show a state the stream never
    reports. Levels touched by the last message at or before T and the first message after T
    are counted as `unverifiable_levels` and excluded; every other level must pass the rules
    above.

    A reconstruction error (a missed, duplicated or misapplied update, a wrong size, a level
    that should have been deleted) persists across the window and fails the check. As extra
    evidence, the report counts audits where one single stream state matches every level
    exactly (`exact_single_state`) and where the state as-of T itself does (`exact_at_stamp`).
    """
    ev, _, audits = read_ws(symbol, date, base)
    rep = GoldenReport("live_audit", symbol, date, references=len(audits))
    if not audits or len(ev) == 0:
        return rep
    all_px = np.concatenate(
        [ev.price[np.isfinite(ev.price)]]
        + [np.fromiter(a.bids.keys() | a.asks.keys(), dtype=float) for a in audits]
    )
    scale = infer_price_scale(all_px)
    real = np.isfinite(ev.price)
    px = np.zeros(len(ev), dtype=np.int64)
    px[real] = to_ticks(ev.price[real], scale)

    def ticks(d: dict[float, float]) -> dict[int, float]:
        keys = to_ticks(np.fromiter(d.keys(), dtype=float, count=len(d)), scale).tolist()
        return dict(zip(keys, d.values(), strict=True))

    upd = np.flatnonzero(real & (ev.kind == UPDATE))
    upd_evt = ev.event_ts_ns[upd]  # non-decreasing on the live feed
    checks = []
    for a in sorted(audits, key=lambda a: a.event_ts_ns):
        lo, hi = a.event_ts_ns - window_ns, a.event_ts_ns + window_ns
        rows = np.flatnonzero(real & (ev.event_ts_ns > lo) & (ev.event_ts_ns <= hi))
        allowed: dict[tuple[bool, int], set[float]] = {}
        for b, p, z in zip(
            ev.is_bid[rows].tolist(), px[rows].tolist(), ev.size[rows].tolist(), strict=True
        ):
            allowed.setdefault((b, p), set()).add(z)
        k = int(np.searchsorted(upd_evt, a.event_ts_ns, side="right"))
        msgs = {int(ev.msg_id[upd[j]]) for j in (k - 1, k) if 0 <= j < len(upd)}
        srows = upd[np.isin(ev.msg_id[upd], list(msgs))]
        straddle = set(zip(ev.is_bid[srows].tolist(), px[srows].tolist(), strict=True))
        checks.append(_AuditCheck(a.event_ts_ns, ticks(a.bids), ticks(a.asks), allowed, straddle))

    def diff(book: L2Book, c: _AuditCheck) -> list[tuple]:
        ob, oa = book.levels()
        return [(True, *d) for d in _diff_levels(ob, c.bids)] + [
            (False, *d) for d in _diff_levels(oa, c.asks)
        ]

    def touched_mismatches(book: L2Book, c: _AuditCheck) -> int:
        n = 0
        for is_bid, p in c.allowed:
            ours = (book.bids if is_bid else book.asks).get(p, 0.0)
            n += ours != (c.bids if is_bid else c.asks).get(p, 0.0)
        return n

    def single_state(book: L2Book, c: _AuditCheck) -> None:
        m = len(c.outside) + touched_mismatches(book, c)
        c.best_state = m if c.best_state is None else min(c.best_state, m)

    def finalize(c: _AuditCheck) -> None:
        c.phase = 2
        if c.best_state is None:
            rep.skipped_invalid += 1
            return
        bad = [d for d in c.outside if (d[0], d[1]) not in c.straddle]
        for (is_bid, p), vals in c.allowed.items():
            ref = (c.bids if is_bid else c.asks).get(p, 0.0)
            if ref not in vals and (is_bid, p) not in c.straddle:
                bad.append((is_bid, p, sorted(vals), ref))
        rep.compared += 1
        rep.levels_compared += len(c.bids) + len(c.asks) - len(c.straddle)
        rep.unverifiable_levels += len(c.straddle)
        rep.exact_single_state += int(c.best_state == 0)
        rep.exact_at_stamp += int(c.at_stamp == 0)
        if bad:
            rep.mismatched_snapshots += 1
            rep.mismatched_levels += len(bad)
            if len(rep.examples) < 10:
                ex = [("bid" if b else "ask", p, ours, ref) for b, p, ours, ref in bad[:5]]
                rep.examples.append({"audit_event_ts_ns": c.t, "diffs": ex})

    k = 0
    prev_evt = 0

    def hook(recv_ts: int, first_evt: int, book: L2Book, valid: bool) -> None:
        # Called before each message, so `book` is the state as-of prev_evt. Live messages
        # carry one event time each (ingest.readers splits them); markers carry 0.
        nonlocal k, prev_evt
        if first_evt <= 0:
            return
        while k < len(checks):
            c = checks[k]
            lo, hi = c.t - window_ns, c.t + window_ns
            if c.phase == 0:
                if first_evt <= lo:
                    break
                c.phase = 1
                if valid:  # book = state as-of the window start
                    for is_bid, p in c.allowed:  # value each touched level held at the start
                        c.allowed[(is_bid, p)].add((book.bids if is_bid else book.asks).get(p, 0.0))
                    c.outside = [d for d in diff(book, c) if (d[0], d[1]) not in c.allowed]
                    single_state(book, c)
            elif valid and prev_evt > lo:
                single_state(book, c)
            if c.at_stamp is None and first_evt > c.t and valid and c.phase == 1:
                c.at_stamp = len(c.outside) + touched_mismatches(book, c)
            if first_evt > hi:
                finalize(c)
                k += 1
                continue
            break
        prev_evt = first_evt

    replay_to_grid(ev, day_start_ns(date), scale=scale, before_message=hook)
    for c in checks[k:]:
        if c.phase == 1 and c.best_state is not None:
            finalize(c)
        else:
            rep.skipped_unaligned += 1
    return rep


# --- historical: Tardis book_snapshot_25 ----------------------------------------------------


def _snapshot25_rows(
    symbol: str, date: str, max_rows: int | None, local: Path | None = None
) -> dict[str, np.ndarray]:
    url = dataset_url("book_snapshot_25", symbol, date)
    cache = local or (
        VENDOR_CACHE / "tardis" / "coinbase" / "book_snapshot_25" / symbol / f"{date}.csv.gz"
    )
    if max_rows is None or local is not None:
        if not cache.exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                tmp = cache.with_suffix(".part")
                with tmp.open("wb") as fh:
                    for chunk in r.iter_content(1 << 20):
                        fh.write(chunk)
                tmp.replace(cache)
        t = pacsv.read_csv(cache)
        return {c: t[c].to_numpy(zero_copy_only=False) for c in t.column_names}
    # Partial: stream and stop after max_rows (for big symbols).
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        reader = csv.reader(io.TextIOWrapper(gzip.GzipFile(fileobj=r.raw)))
        header = next(reader)
        rows = []
        for i, row in enumerate(reader):
            if i >= max_rows:
                break
            rows.append(row)
    cols: list[tuple[str, ...]] = list(zip(*rows, strict=True)) if rows else [() for _ in header]
    out: dict[str, np.ndarray] = {}
    for name, col in zip(header, cols, strict=True):
        if name in ("exchange", "symbol"):
            continue
        out[name] = np.array([float(x) if x else np.nan for x in col])
    return out


def golden_tardis(
    symbol: str,
    date: str,
    max_rows: int | None = None,
    base: Path | None = None,
    snapshot_csv: Path | None = None,
) -> GoldenReport:
    ev = read_tardis_book(symbol, date, base)
    snap = _snapshot25_rows(symbol, date, max_rows, snapshot_csv)
    T = (np.asarray(snap["local_timestamp"]).astype(np.int64)) * 1000
    # Several rows can share a receive time; the state after all of them is the last one.
    last = np.ones(len(T), dtype=bool)
    last[:-1] = T[1:] != T[:-1]
    idx = np.flatnonzero(last)
    rep = GoldenReport("tardis_snapshot25", symbol, date, references=len(idx))
    if len(idx) == 0:
        return rep
    scale = infer_price_scale(ev.price)
    depth = 25

    def side_arrays(side: str) -> tuple[np.ndarray, np.ndarray]:
        px = np.stack(
            [np.asarray(snap[f"{side}[{i}].price"], dtype=float) for i in range(depth)], 1
        )
        sz = np.stack(
            [np.asarray(snap[f"{side}[{i}].amount"], dtype=float) for i in range(depth)], 1
        )
        return px, sz

    bpx, bsz = side_arrays("bids")
    apx, asz = side_arrays("asks")
    pos = 0

    def compare(book: L2Book, valid: bool, r: int) -> None:
        if not valid:
            rep.skipped_invalid += 1
            return
        ours_b, ours_a = book.top(depth)
        rep.compared += 1
        diffs = []
        for name, ours, px, sz in (
            ("bid", ours_b, bpx[r], bsz[r]),
            ("ask", ours_a, apx[r], asz[r]),
        ):
            ok = np.isfinite(px)
            ref = list(zip(to_ticks(px[ok], scale).tolist(), sz[ok].tolist(), strict=True))
            rep.levels_compared += max(len(ref), len(ours))
            for i in range(max(len(ref), len(ours))):
                a = ours[i] if i < len(ours) else None
                b = ref[i] if i < len(ref) else None
                if a is None or b is None or a[0] != b[0] or a[1] != b[1]:
                    diffs.append((name, i, a, b))
        if diffs:
            rep.mismatched_snapshots += 1
            rep.mismatched_levels += len(diffs)
            if len(rep.examples) < 10:
                rep.examples.append({"local_ts_ns": int(T[r]), "diffs": diffs[:5]})

    def hook(recv_ts: int, first_evt: int, book: L2Book, valid: bool) -> None:
        nonlocal pos
        while pos < len(idx) and T[idx[pos]] < recv_ts:
            compare(book, valid, int(idx[pos]))
            pos += 1

    replay_to_grid(ev, day_start_ns(date), scale=scale, before_message=hook)
    rep.skipped_unaligned += len(idx) - pos  # at or after the last message
    return rep


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("kind", choices=["live", "tardis"])
    p.add_argument("symbol")
    p.add_argument("date")
    p.add_argument("--max-rows", type=int)
    args = p.parse_args()
    if args.kind == "live":
        rep = golden_live(args.symbol, args.date)
    else:
        rep = golden_tardis(args.symbol, args.date, args.max_rows)
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    out = GOLDEN_DIR / f"{args.kind}_{args.symbol}_{args.date}.json"
    out.write_text(rep.to_json())
    print(rep.to_json())
    raise SystemExit(0 if rep.passed else 1)


if __name__ == "__main__":
    main()
