"""Build data/book/{symbol}/{date}.parquet from the raw archive.

Run:  python -m book.build                       # whole universe, all dates (holdout included:
                                                 # building data is not evaluating it)
      python -m book.build --symbols HBAR-USD --dates 2025-03-01
      python -m book.build --source coinbase_ws --dates 2026-10-12
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

from book.replay import build_book_grid, write_book_grid
from common.paths import book_path, raw_dir
from common.universe import load_universe
from ingest import coinbase_ws, tardis
from ingest.readers import read_tardis_book, read_tardis_trades, read_ws

log = logging.getLogger("book.build")


def detect_source(symbol: str, date: str) -> str:
    if (raw_dir(tardis.VENUE, symbol, date) / "incremental_book_L2.parquet").exists():
        return tardis.VENUE
    if raw_dir(coinbase_ws.VENUE, symbol, date).exists():
        return coinbase_ws.VENUE
    raise FileNotFoundError(f"no raw data for {symbol} {date}")


def build_one(symbol: str, date: str, source: str | None = None, force: bool = False) -> str:
    if book_path(symbol, date).exists() and not force:
        return f"skip {symbol} {date} (exists)"
    source = source or detect_source(symbol, date)
    t0 = time.time()
    if source == tardis.VENUE:
        if not (raw_dir(source, symbol, date) / "trades.parquet").exists():
            raise FileNotFoundError(f"trades for {symbol} {date} not downloaded yet")
        ev, tr = read_tardis_book(symbol, date), read_tardis_trades(symbol, date)
        version = tardis.SOURCE_VERSION
    else:
        ev, tr, _ = read_ws(symbol, date)
        version = coinbase_ws.SOURCE_VERSION
    table, st = build_book_grid(ev, tr, date, symbol, source, version)
    write_book_grid(table, symbol, date)
    summary = {
        "rows": st.rows,
        "msgs": st.messages,
        "snapshots": st.snapshots,
        "resyncs": st.resyncs,
        "crossed_msgs": st.crossed_messages,
        "invalid_s": round(st.invalid_seconds, 1),
        "ev_per_s": round(st.events_per_sec),
        "tick": st.tick_size,
    }
    return f"ok {symbol} {date} [{source}] {time.time() - t0:.0f}s {json.dumps(summary)}"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--symbols", nargs="+")
    p.add_argument("--dates", nargs="+")
    p.add_argument("--source", choices=[tardis.VENUE, coinbase_ws.VENUE])
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--force", action="store_true", help="rebuild existing outputs")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    u = load_universe()
    symbols = args.symbols or list(u.symbols)
    dates = args.dates or list(u.all_dates)
    jobs = [(s, d) for d in dates for s in symbols]
    failures = 0
    with ProcessPoolExecutor(args.workers) as ex:
        futs = {ex.submit(build_one, s, d, args.source, args.force): (s, d) for s, d in jobs}
        for f in as_completed(futs):
            try:
                log.info(f.result())
            except Exception as exc:
                failures += 1
                log.error("FAILED %s: %r", futs[f], exc)
    log.info("done: %d jobs, %d failures", len(jobs), failures)
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
