"""Append-only Parquet archive for raw data.

Rules enforced here:

* A part file is written once and never modified or overwritten. Each flush writes a new
  file, `part-{first_recv_ts_ns}-{nonce}.parquet`, via a temp file and an atomic rename that
  refuses to replace an existing path.
* Rows are partitioned by the UTC date of `recv_ts_ns`, so one flush may write several files
  when a buffer spans midnight.
* Within a part file rows keep their arrival order, which is also `recv_ts_ns` order.
"""

from __future__ import annotations

import os
import secrets
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from common.meta import version_metadata
from common.paths import raw_dir
from ingest.schema import RAW_SCHEMA_VERSION

NS_PER_DAY = 86_400 * 1_000_000_000


def utc_date(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns // 1_000_000_000, tz=UTC).strftime("%Y-%m-%d")


def write_part(directory: Path, table: pa.Table, metadata: dict[str, str]) -> Path:
    """Write one immutable part file. Never overwrites."""
    directory.mkdir(parents=True, exist_ok=True)
    first_ts = int(table.column("recv_ts_ns")[0].as_py()) if table.num_rows else 0
    final = directory / f"part-{first_ts:019d}-{secrets.token_hex(4)}.parquet"
    tmp = directory / f".{final.name}.tmp"
    meta = dict(table.schema.metadata or {})
    meta.update({k.encode(): v.encode() for k, v in metadata.items()})
    pq.write_table(table.replace_schema_metadata(meta), tmp, compression="zstd")
    # os.link fails if `final` exists, unlike os.replace, so a name collision can never
    # clobber archived data.
    os.link(tmp, final)
    tmp.unlink()
    return final


@dataclass
class RawArchiveWriter:
    """Buffers rows for one (venue, symbol) and flushes them as new immutable part files."""

    venue: str
    symbol: str
    schema: pa.Schema
    source_version: str
    _rows: dict[str, list] = field(default_factory=lambda: defaultdict(list))
    _n: int = 0
    files_written: int = 0
    rows_written: int = 0

    def append(self, row: dict) -> None:
        for name in self.schema.names:
            self._rows[name].append(row.get(name))
        self._n += 1

    def __len__(self) -> int:
        return self._n

    def take_buffer(self) -> pa.Table | None:
        """Detach the current buffer as a table (cheap; call from the event loop)."""
        if self._n == 0:
            return None
        table = pa.table({n: self._rows[n] for n in self.schema.names}, schema=self.schema)
        self._rows = defaultdict(list)
        self._n = 0
        return table

    def write_table(self, table: pa.Table) -> list[Path]:
        """Write a detached buffer, split by UTC date (safe to call from a worker thread)."""
        out: list[Path] = []
        meta = version_metadata(
            source_version=self.source_version,
            raw_schema_version=RAW_SCHEMA_VERSION,
            venue=self.venue,
            symbol=self.symbol,
        )
        ts = table.column("recv_ts_ns").to_numpy()
        days = ts // NS_PER_DAY
        ends = [*(np.flatnonzero(np.diff(days)) + 1).tolist(), len(days)]
        start = 0
        for end in ends:
            chunk = table.slice(start, end - start)
            date = utc_date(int(ts[start]))
            out.append(write_part(raw_dir(self.venue, self.symbol, date), chunk, meta))
            start = end
        self.files_written += len(out)
        self.rows_written += table.num_rows
        return out

    def flush(self) -> list[Path]:
        table = self.take_buffer()
        return self.write_table(table) if table is not None else []
