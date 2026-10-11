"""Collector logic, plus an end-to-end run against a fake Coinbase server that injects a
sequence gap. The collector must archive everything, record the gap, reconnect, get a new
snapshot, and the replay must mark the book invalid in between."""

from __future__ import annotations

import asyncio
import json
import random

import numpy as np
import pytest
import websockets

from book.replay import replay_to_grid
from ingest import coinbase_ws as cw
from ingest.readers import INVALID, SNAPSHOT, read_ws, read_ws_messages


def test_sequence_tracker_detects_gaps():
    t = cw.SequenceTracker()
    assert t.check(0) is None
    assert t.check(1) is None
    assert t.check(None) is None  # messages without a sequence are ignored
    assert t.check(3) == (2, 3)
    assert t.check(4) is None
    assert t.check(4) == (5, 4)  # duplicate / backwards is a gap too


def test_sequence_tracker_first_message_must_be_zero():
    assert cw.SequenceTracker().check(5) == (0, 5)


def test_backoff_grows_caps_and_resets():
    b = cw.Backoff(base_s=1.0, cap_s=8.0, rng=random.Random(0))
    delays = [b.next_delay() for _ in range(10)]
    ceilings = [min(8.0, 2**i) for i in range(10)]
    assert all(0 <= d <= c for d, c in zip(delays, ceilings, strict=True))
    b.reset()
    assert b.attempt == 0 and b.next_delay() <= 1.0


@pytest.mark.parametrize(
    ("s", "expected"),
    [
        ("2026-10-11T22:08:50.355151126Z", 1791756530355151126),
        ("2026-10-11T22:08:50Z", 1791756530000000000),
        ("2026-10-11T22:08:50.5Z", 1791756530500000000),
        (None, None),
        ("garbage", None),
    ],
)
def test_parse_ts_ns(s, expected):
    assert cw.parse_ts_ns(s) == expected


# --- end-to-end against a fake exchange ------------------------------------------------------


def _l2(seq: int, kind: str, updates: list[tuple[str, str, str, str]]) -> str:
    return json.dumps(
        {
            "channel": "l2_data",
            "timestamp": "2026-10-11T12:00:00.000000000Z",
            "sequence_num": seq,
            "events": [
                {
                    "type": kind,
                    "product_id": "TEST-USD",
                    "updates": [
                        {"side": s, "event_time": et, "price_level": p, "new_quantity": q}
                        for s, p, q, et in updates
                    ],
                }
            ],
        }
    )


def _trades(seq: int, side: str) -> str:
    return json.dumps(
        {
            "channel": "market_trades",
            "timestamp": "2026-10-11T12:00:00.100000000Z",
            "sequence_num": seq,
            "events": [
                {
                    "type": "update",
                    "trades": [
                        {
                            "product_id": "TEST-USD",
                            "trade_id": f"t{seq}",
                            "price": "100.01",
                            "size": "0.5",
                            "time": "2026-10-11T12:00:00.090000Z",
                            "side": side,
                        }
                    ],
                }
            ],
        }
    )


ET = "2026-10-11T12:00:00.0{:02d}000Z"
SNAP = [("bid", "100.00", "1.0", ET.format(0)), ("offer", "100.01", "2.0", ET.format(0))]


async def _fake_server(conns: list[int]):
    async def handler(ws):
        conns.append(1)
        n = len(conns)
        for _ in range(3):  # level2, market_trades, heartbeats
            await ws.recv()
        if n == 1:
            # seq 0..2 fine, then jump to 5: a gap.
            await ws.send(_l2(0, "snapshot", SNAP))
            await ws.send(_l2(1, "update", [("bid", "100.00", "1.5", ET.format(1))]))
            await ws.send(_trades(2, "SELL"))  # maker SELL → taker buy
            await ws.send(_l2(5, "update", [("bid", "99.99", "3.0", ET.format(2))]))
        else:
            await ws.send(_l2(0, "snapshot", [*SNAP, ("bid", "99.99", "3.0", ET.format(3))]))
            await ws.send(_l2(1, "update", [("offer", "100.02", "1.0", ET.format(4))]))
        await asyncio.sleep(5)

    return await websockets.serve(handler, "127.0.0.1", 0)


def test_collector_gap_resync_end_to_end(tmp_data):
    async def run():
        conns: list[int] = []
        server = await _fake_server(conns)
        port = server.sockets[0].getsockname()[1]
        cfg = cw.CollectorConfig(
            symbols=["TEST-USD"],
            url=f"ws://127.0.0.1:{port}",
            flush_interval_s=60,
            audit_interval_s=3600,
            stale_timeout_s=30,
            health_interval_s=60,
        )
        col = cw.Collector(cfg)
        await col.run(duration_s=1.5)
        server.close()
        await server.wait_closed()
        return col, conns

    col, conns = asyncio.run(run())
    assert len(conns) == 2, "collector should reconnect after the gap"
    assert col.health["TEST-USD"].gaps == 1
    date = next((tmp_data / "raw" / "coinbase_ws" / "TEST-USD").iterdir()).name
    rows = read_ws_messages("TEST-USD", date)
    controls = [json.loads(r["payload"]) for r in rows if r["channel"] == "_control"]
    events = [c["event"] for c in controls]
    assert events.count("connected") == 2
    assert {"event": "gap", "expected": 3, "got": 5} in controls
    # The message that revealed the gap is archived too (raw first, then inspect).
    assert [r["conn_seq"] for r in rows if r["channel"] == "l2_data"] == [0, 1, 5, 0, 1]

    ev, trades, _ = read_ws("TEST-USD", date)
    assert (ev.kind == INVALID).any()
    assert (ev.kind == SNAPSHOT).sum() == 5  # 2 levels + 3 levels
    assert trades.is_buy.tolist() == [True]  # maker SELL → aggressor buy
    day0 = int(np.datetime64(date, "ns").astype(np.int64))
    res = replay_to_grid(ev, day0)
    assert res.stats.snapshots == 2
    assert res.stats.invalid_markers >= 2  # reader's own seq check + the collector's gap row
    assert res.stats.skipped_updates == 1  # the update that arrived after the gap
    # Valid after the first snapshot, invalid from the gap until the second snapshot.
    assert res.transitions_valid.tolist() == [True, False, True, False]
