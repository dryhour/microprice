from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ingest.archive import RawArchiveWriter, write_part
from ingest.schema import WS_MESSAGE_SCHEMA

DAY = 86_400 * 10**9
T0 = 1_790_812_800 * 10**9  # 2026-10-01T00:00:00Z


def _row(ts: int, seq: int) -> dict:
    return {
        "recv_ts_ns": ts,
        "recv_mono_ns": ts,
        "symbol": "BTC-USD",
        "stream": "main",
        "conn_id": "c1",
        "conn_seq": seq,
        "channel": "l2_data",
        "exchange_ts_ns": ts - 5,
        "payload": "{}",
    }


def test_write_part_never_overwrites(tmp_path):
    t = pa.table({"recv_ts_ns": [1, 2]})
    p = write_part(tmp_path, t, {"a": "b"})
    assert pq.read_table(p).num_rows == 2
    # Force a name collision: os.link must refuse to replace the existing file.
    import os

    with pytest.raises(FileExistsError):
        os.link(p, p)
    assert not list(tmp_path.glob(".*.tmp"))


def test_writer_splits_by_utc_date_and_keeps_order(tmp_data):
    w = RawArchiveWriter("coinbase_ws", "BTC-USD", WS_MESSAGE_SCHEMA, "test")
    ts = [T0 + DAY - 2, T0 + DAY - 1, T0 + DAY, T0 + DAY + 1]
    for i, x in enumerate(ts):
        w.append(_row(x, i))
    paths = w.flush()
    assert len(paths) == 2
    assert {p.parent.name for p in paths} == {"2026-10-01", "2026-10-02"}
    got = sorted(
        (pq.read_table(p).column("recv_ts_ns").to_pylist() for p in paths), key=lambda x: x[0]
    )
    assert got == [ts[:2], ts[2:]]
    meta = pq.read_schema(paths[0]).metadata
    assert b"code_git_sha" in meta and b"source_version" in meta and b"created_at" in meta
    # A second flush appends new files; old ones are untouched.
    w.append(_row(T0 + DAY + 5, 9))
    more = w.flush()
    assert len(more) == 1 and more[0] not in paths
    assert w.rows_written == 5 and w.files_written == 3
    assert w.flush() == []
