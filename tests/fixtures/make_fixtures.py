"""Regenerate the small raw-data fixtures in tests/fixtures/raw/ (also QR's raw data sample).

    python tests/fixtures/make_fixtures.py

* coinbase_ws/HBAR-USD/2026-10-11: the live collector's first ~11 minutes for HBAR-USD,
  through the first audit snapshot plus 60 s, in the exact raw format (one row per message).
* coinbase_tardis/HBAR-USD/2025-03-01: the first 3 minutes of the Tardis L2 and trades files,
  plus Tardis's own book_snapshot_25 rows for the same 3 minutes (reference for the golden test).
"""

from __future__ import annotations

import csv
import gzip
import io
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import requests

from common.paths import raw_dir
from ingest.readers import read_ws_messages
from ingest.schema import WS_MESSAGE_SCHEMA
from ingest.tardis import dataset_url

OUT = Path(__file__).parent / "raw"
WS = ("HBAR-USD", "2026-10-11")
TARDIS = ("HBAR-USD", "2025-03-01")
TARDIS_SECONDS = 180


def make_ws() -> None:
    rows = read_ws_messages(*WS)
    audit = next(i for i, r in enumerate(rows) if r["stream"] == "audit")
    cutoff = rows[audit]["recv_ts_ns"] + 60 * 10**9
    keep = [r for r in rows if r["recv_ts_ns"] <= cutoff]
    d = OUT / "coinbase_ws" / WS[0] / WS[1]
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.parquet"):
        f.unlink()
    meta = pq.read_schema(next(raw_dir("coinbase_ws", *WS).glob("*.parquet"))).metadata
    t = pa.Table.from_pylist(keep, schema=WS_MESSAGE_SCHEMA).replace_schema_metadata(meta)
    pq.write_table(t, d / f"part-{keep[0]['recv_ts_ns']:019d}-fixture0.parquet", compression="zstd")
    print(f"coinbase_ws: {len(keep)} messages")


def make_tardis() -> None:
    src = raw_dir("coinbase_tardis", *TARDIS)
    d = OUT / "coinbase_tardis" / TARDIS[0] / TARDIS[1]
    d.mkdir(parents=True, exist_ok=True)
    book = pq.read_table(src / "incremental_book_L2.parquet")
    start = int(book["recv_ts_ns"][0].as_py())
    cutoff = (start // 10**9 // 86_400) * 86_400 * 10**9 + TARDIS_SECONDS * 10**9
    for name in ("incremental_book_L2", "trades"):
        t = pq.read_table(src / f"{name}.parquet")
        t = t.filter(pc.less_equal(t["recv_ts_ns"], cutoff))
        pq.write_table(t, d / f"{name}.parquet", compression="zstd")
        print(f"coinbase_tardis {name}: {t.num_rows} rows")
    # Reference snapshots for the same window.
    url = dataset_url("book_snapshot_25", *TARDIS)
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        reader = csv.reader(io.TextIOWrapper(gzip.GzipFile(fileobj=r.raw)))
        header = next(reader)
        ts_col = header.index("local_timestamp")
        rows = []
        for row in reader:
            if int(row[ts_col]) * 1000 > cutoff:
                break
            rows.append(row)
    with gzip.open(d / "book_snapshot_25.csv.gz", "wt", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    print(f"coinbase_tardis book_snapshot_25: {len(rows)} rows")


if __name__ == "__main__":
    make_ws()
    make_tardis()
