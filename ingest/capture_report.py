"""Summarize the live capture: coverage, gaps, resyncs, book validity and golden results.

This is the evidence for the Week 1 gate ("24 h capture completed with gaps detected and
resynced") and for the live golden test.

Run:  python -m ingest.capture_report                 # all captured symbol-days
      python -m ingest.capture_report --no-golden     # skip the (slower) golden replay
"""

from __future__ import annotations

import argparse
import json
from collections import Counter

import numpy as np

from book.golden import golden_live
from book.replay import day_start_ns, replay_to_grid
from common.paths import LOGS, RAW
from ingest.coinbase_ws import VENUE
from ingest.readers import read_ws, read_ws_messages


def summarize(symbol: str, date: str, golden: bool) -> dict:
    rows = read_ws_messages(symbol, date)
    main = [r for r in rows if r["stream"] == "main"]
    controls = Counter(
        json.loads(r["payload"]).get("event") for r in main if r["channel"] == "_control"
    )
    channels = Counter(r["channel"] for r in main)
    conns = {r["conn_id"] for r in main}
    t0, t1 = rows[0]["recv_ts_ns"], rows[-1]["recv_ts_ns"]
    ev, trades, audits = read_ws(symbol, date)
    res = replay_to_grid(ev, day_start_ns(date))
    st = res.stats
    in_span = (res.columns["ts_ns"] >= t0) & (res.columns["ts_ns"] <= t1)
    out = {
        "symbol": symbol,
        "date": date,
        "first_recv_utc": str(np.datetime64(t0, "ns").astype("datetime64[ms]")),
        "last_recv_utc": str(np.datetime64(t1, "ns").astype("datetime64[ms]")),
        "hours": round((t1 - t0) / 3.6e12, 2),
        "messages": len(main),
        "l2_messages": channels.get("l2_data", 0),
        "trade_messages": channels.get("market_trades", 0),
        "trades": len(trades),
        "connections": len(conns),
        "control_events": dict(controls),
        "snapshots": st.snapshots,
        "invalid_markers": st.invalid_markers,
        "skipped_updates": st.skipped_updates,
        "crossed_messages": st.crossed_messages,
        "book_valid_share_in_span": round(float(res.columns["book_valid"][in_span].mean()), 6)
        if in_span.any()
        else None,
        "audits": len(audits),
        "events_per_sec": round(st.events_per_sec),
    }
    if golden:
        g = golden_live(symbol, date)
        out["golden"] = {
            "compared": g.compared,
            "levels": g.levels_compared,
            "mismatched_levels": g.mismatched_levels,
            "exact_at_stamp": g.exact_at_stamp,
            "skipped": g.skipped_invalid + g.skipped_unaligned,
            "passed": g.passed,
        }
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--no-golden", action="store_true")
    args = p.parse_args()
    base = RAW / VENUE
    results = []
    for sym_dir in sorted(base.glob("*")):
        for date_dir in sorted(sym_dir.glob("20*")):
            r = summarize(sym_dir.name, date_dir.name, not args.no_golden)
            results.append(r)
            print(json.dumps(r))
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / "capture_report.json").write_text(json.dumps(results, indent=1))
    gaps = sum(r["control_events"].get("gap", 0) for r in results)
    resyncs = sum(r["snapshots"] for r in results) - len(results)
    failed = [r for r in results if r.get("golden", {}).get("mismatched_levels")]
    print(
        f"\n{len(results)} symbol-days; collector-detected gaps: {gaps}; "
        f"resync snapshots: {resyncs}; golden failures: {len(failed)}"
    )


if __name__ == "__main__":
    main()
