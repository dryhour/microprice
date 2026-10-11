"""Historical loader: Coinbase Exchange L2 + trades from Tardis.dev free daily samples.

Tardis publishes the first UTC day of every month for free (no API key):
    https://datasets.tardis.dev/v1/coinbase/{data_type}/{YYYY}/{MM}/{DD}/{SYMBOL}.csv.gz

For each (symbol, date) this downloads the vendor CSVs, checks them, and writes
    data/raw/coinbase_tardis/{symbol}/{date}/incremental_book_L2.parquet
    data/raw/coinbase_tardis/{symbol}/{date}/trades.parquet
with columns renamed to our raw schema (ingest/schema.py) and µs converted to int64 ns.
Values are not otherwise changed. Provenance (URL, sha256 and row count of the vendor file)
goes into the Parquet metadata. Raw files are immutable: an existing file is never rewritten.

Run:  python -m ingest.tardis                    # whole frozen universe
      python -m ingest.tardis --symbols HBAR-USD --dates 2026-10-01
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
import requests

from common.meta import version_metadata
from common.paths import VENDOR_CACHE, raw_dir
from common.universe import load_universe
from ingest.schema import RAW_SCHEMA_VERSION, TARDIS_BOOK_SCHEMA, TARDIS_TRADES_SCHEMA

VENUE = "coinbase_tardis"
EXCHANGE = "coinbase"
BASE_URL = "https://datasets.tardis.dev/v1"
DATA_TYPES = ("incremental_book_L2", "trades")
SOURCE_VERSION = "tardis.dev/datasets-v1/coinbase"

log = logging.getLogger("tardis")


def dataset_url(data_type: str, symbol: str, date: str) -> str:
    y, m, d = date.split("-")
    return f"{BASE_URL}/{EXCHANGE}/{data_type}/{y}/{m}/{d}/{symbol}.csv.gz"


def raw_path(data_type: str, symbol: str, date: str) -> Path:
    return raw_dir(VENUE, symbol, date) / f"{data_type}.parquet"


class QuotaExceeded(RuntimeError):
    def __init__(self, retry_after_s: float) -> None:
        super().__init__(f"Tardis daily transfer limit reached; retry in {retry_after_s:.0f}s")
        self.retry_after_s = retry_after_s


def download(url: str, dest: Path, retries: int = 8) -> str:
    """Stream `url` to `dest`. Returns the sha256 of the bytes written.

    Raises QuotaExceeded when Tardis reports its daily data-transfer limit (HTTP 429 with a
    retry-after of hours); short 429s and 5xx are retried with backoff.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(retries):
        try:
            h = hashlib.sha256()
            with requests.get(url, stream=True, timeout=60) as r:
                if r.status_code == 429 or r.status_code >= 500:
                    ra = r.headers.get("Retry-After", "")
                    wait = float(ra) if ra.isdigit() else min(300, 15 * 2**attempt)
                    if wait > 600:
                        raise QuotaExceeded(wait)
                    log.warning(
                        "HTTP %s, retry %d in %.0fs: %s", r.status_code, attempt + 1, wait, url
                    )
                    time.sleep(wait * random.uniform(1.0, 1.2))
                    continue
                r.raise_for_status()
                with tmp.open("wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
                        h.update(chunk)
            os.replace(tmp, dest)
            return h.hexdigest()
        except (requests.RequestException, OSError) as exc:
            wait = min(300, 5 * 2**attempt)
            log.warning("download failed (%s), retry in %ss: %s", exc, wait, url)
            time.sleep(wait)
    raise RuntimeError(f"giving up on {url}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _convert_book(table: pa.Table) -> pa.Table:
    return pa.table(
        {
            "exchange_ts_ns": pc.multiply(table["timestamp"], 1000),
            "recv_ts_ns": pc.multiply(table["local_timestamp"], 1000),
            "is_snapshot": table["is_snapshot"],
            "side": table["side"],
            "price": table["price"],
            "size": table["amount"],
        },
        schema=TARDIS_BOOK_SCHEMA,
    )


def _convert_trades(table: pa.Table) -> pa.Table:
    return pa.table(
        {
            "exchange_ts_ns": pc.multiply(table["timestamp"], 1000),
            "recv_ts_ns": pc.multiply(table["local_timestamp"], 1000),
            "trade_id": pc.cast(table["id"], pa.string()),
            "side": table["side"],
            "price": table["price"],
            "size": table["amount"],
        },
        schema=TARDIS_TRADES_SCHEMA,
    )


_CSV_TYPES = {
    "incremental_book_L2": {
        "exchange": pa.string(),
        "symbol": pa.string(),
        "timestamp": pa.int64(),
        "local_timestamp": pa.int64(),
        "is_snapshot": pa.bool_(),
        "side": pa.string(),
        "price": pa.float64(),
        "amount": pa.float64(),
    },
    "trades": {
        "exchange": pa.string(),
        "symbol": pa.string(),
        "timestamp": pa.int64(),
        "local_timestamp": pa.int64(),
        "id": pa.string(),
        "side": pa.string(),
        "price": pa.float64(),
        "amount": pa.float64(),
    },
}


def convert(data_type: str, src: Path, dest: Path, symbol: str, meta: dict[str, str]) -> int:
    """Stream-convert a vendor CSV.gz to one Parquet file. Returns the row count."""
    convert_fn = _convert_book if data_type == "incremental_book_L2" else _convert_trades
    schema = TARDIS_BOOK_SCHEMA if data_type == "incremental_book_L2" else TARDIS_TRADES_SCHEMA
    reader = pacsv.open_csv(
        src,
        read_options=pacsv.ReadOptions(block_size=64 << 20),
        convert_options=pacsv.ConvertOptions(column_types=_CSV_TYPES[data_type]),
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp")
    rows = 0
    file_meta = {k.encode(): v.encode() for k, v in meta.items()}
    with pq.ParquetWriter(tmp, schema.with_metadata(file_meta), compression="zstd") as writer:
        for batch in reader:
            t = pa.Table.from_batches([batch])
            syms = pc.unique(t["symbol"]).to_pylist()
            if syms and syms != [symbol]:
                raise ValueError(f"{src}: unexpected symbols {syms}")
            writer.write_table(convert_fn(t).replace_schema_metadata(file_meta))
            rows += t.num_rows
    os.link(tmp, dest)  # never overwrite an existing raw file
    tmp.unlink()
    return rows


def load_one(symbol: str, date: str, data_type: str, keep_vendor: bool = False) -> str:
    dest = raw_path(data_type, symbol, date)
    if dest.exists():
        return f"skip {data_type} {symbol} {date} (exists)"
    url = dataset_url(data_type, symbol, date)
    src = VENDOR_CACHE / "tardis" / EXCHANGE / data_type / symbol / f"{date}.csv.gz"
    t0 = time.time()
    sha = sha256_file(src) if src.exists() else download(url, src)
    size = src.stat().st_size
    if size < 100:
        raise RuntimeError(f"{url}: empty download ({size} bytes); not a free sample day?")
    meta = version_metadata(
        source_version=SOURCE_VERSION,
        raw_schema_version=RAW_SCHEMA_VERSION,
        venue=VENUE,
        symbol=symbol,
        date=date,
        data_type=data_type,
        source_url=url,
        source_sha256=sha,
        source_bytes=str(size),
    )
    rows = convert(data_type, src, dest, symbol, meta)
    if not keep_vendor:
        src.unlink()
    secs = time.time() - t0
    return f"ok {data_type} {symbol} {date}: {rows:,} rows, {size / 1e6:.0f} MB, {secs:.0f}s"


def job_order(jobs: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """In-sample before holdout, then the smaller (less liquid) symbols first, so a daily
    transfer quota is spent on what the next analysis step needs."""
    u = load_universe()
    rank = {s: i for i, s in enumerate(u.symbols)}  # universe order = volume rank
    return sorted(
        jobs,
        key=lambda j: (j[1] in u.holdout, -rank.get(j[0], 0), j[1], j[2] != "trades"),
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--symbols", nargs="+")
    p.add_argument("--dates", nargs="+")
    p.add_argument("--types", nargs="+", default=list(DATA_TYPES))
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--keep-vendor", action="store_true", help="keep the downloaded CSV.gz")
    p.add_argument(
        "--wait-for-quota",
        action="store_true",
        help="when the daily transfer limit is hit, sleep until it resets and continue",
    )
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    u = load_universe()
    symbols, dates = args.symbols or list(u.symbols), args.dates or list(u.all_dates)
    jobs = job_order([(s, d, t) for s in symbols for d in dates for t in args.types])
    failures = 0
    while jobs:
        retry: list[tuple[str, str, str]] = []
        quota_wait = 0.0
        with ThreadPoolExecutor(args.workers) as ex:
            futs = {ex.submit(load_one, s, d, t, args.keep_vendor): (s, d, t) for s, d, t in jobs}
            for f in as_completed(futs):
                if f.cancelled():
                    retry.append(futs[f])
                    continue
                try:
                    log.info(f.result())
                except QuotaExceeded as exc:
                    retry.append(futs[f])
                    if not quota_wait:
                        # Don't keep hitting the server: everything queued would fail too.
                        for other in futs:
                            other.cancel()
                    quota_wait = max(quota_wait, exc.retry_after_s)
                except Exception as exc:
                    failures += 1
                    log.error("FAILED %s: %s", futs[f], exc)
        jobs = job_order(retry)
        if jobs:
            if not args.wait_for_quota:
                log.error("transfer quota reached; %d jobs left (rerun later)", len(jobs))
                failures += len(jobs)
                break
            resume = time.time() + quota_wait + 120
            log.warning(
                "transfer quota reached; %d jobs left; sleeping %.1f h until %s",
                len(jobs),
                (quota_wait + 120) / 3600,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(resume)),
            )
            time.sleep(quota_wait + 120)
    log.info("done: %d failures", failures)
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
