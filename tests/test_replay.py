"""Replay semantics on hand-built event streams: as-of sampling, window boundaries,
validity (snapshot, gap, crossed book), trade bucketing and invariant failures."""

from __future__ import annotations

import numpy as np
import pytest

from book.l2book import InvariantError
from book.replay import (
    GRID_NS,
    messages_per_second,
    replay_to_grid,
    trades_per_second,
    validity_on_grid,
)
from ingest.readers import INVALID, SNAPSHOT, UPDATE, BookEvents, Trades

S = GRID_NS
D0 = 1_790_812_800 * S  # 2026-10-01T00:00:00Z


def events(rows: list[tuple], tail: bool = True) -> BookEvents:
    """rows: (recv_ts_ns, msg_id, kind, is_bid, price, size).

    `tail` appends a no-op message (delete a level that doesn't exist) well after the grid,
    so the data covers the whole toy grid (the book is invalid past the last message)."""
    if tail:
        rows = [
            *rows,
            (
                max(r[0] for r in rows) + 100 * S,
                max(r[1] for r in rows) + 1,
                UPDATE,
                True,
                1.0,
                0.0,
            ),
        ]
    a = list(zip(*rows, strict=True))
    return BookEvents(
        recv_ts_ns=np.array(a[0], dtype=np.int64),
        event_ts_ns=np.array(a[0], dtype=np.int64),
        msg_id=np.array(a[1], dtype=np.int64),
        kind=np.array(a[2], dtype=np.int8),
        is_bid=np.array(a[3], dtype=bool),
        price=np.array(a[4], dtype=float),
        size=np.array(a[5], dtype=float),
    )


def snapshot_at(ts: int, msg: int = 0) -> list[tuple]:
    return [
        (ts, msg, SNAPSHOT, True, 100.00, 1.0),
        (ts, msg, SNAPSHOT, True, 99.99, 2.0),
        (ts, msg, SNAPSHOT, False, 100.01, 3.0),
        (ts, msg, SNAPSHOT, False, 100.02, 4.0),
    ]


def test_asof_sampling_includes_event_at_t_excludes_after():
    ev = events(
        [
            *snapshot_at(D0 + S // 2),
            (D0 + 2 * S, 1, UPDATE, True, 100.00, 5.0),  # exactly at t=2s → included at t=2
            (D0 + 3 * S + 1, 2, UPDATE, True, 100.00, 7.0),  # 1 ns after t=3 → only from t=4
        ]
    )
    r = replay_to_grid(ev, D0, n_grid=6, depth=2)
    c = r.columns
    assert np.isnan(c["bid_px_1"][0])  # nothing before the snapshot
    assert c["bid_sz_1"].tolist()[1:] == [1.0, 5.0, 5.0, 7.0, 7.0]
    assert c["bid_px_2"][1] == 99.99 and c["ask_px_1"][1] == 100.01 and c["ask_sz_2"][1] == 4.0
    assert c["book_valid"].tolist() == [False, True, True, True, True, True]
    # (0,1] contains the invalid pre-snapshot period, so the first full valid interval is (1,2].
    assert c["interval_valid"].tolist() == [False, False, True, True, True, True]
    assert c["last_book_recv_ts_ns"].tolist()[1:4] == [D0 + S // 2, D0 + 2 * S, D0 + 2 * S]


def test_updates_before_first_snapshot_are_ignored():
    ev = events([(D0 + 1, 0, UPDATE, True, 50.0, 9.0), *snapshot_at(D0 + 2, 1)])
    r = replay_to_grid(ev, D0, n_grid=2, depth=2)
    assert r.stats.skipped_updates == 1
    assert r.columns["bid_px_1"][1] == 100.0


def test_gap_marker_invalidates_until_next_snapshot():
    ev = events(
        [
            *snapshot_at(D0 + S // 2, 0),
            (D0 + 2 * S + 100, 1, UPDATE, True, 100.00, 1.5),  # last good message
            (D0 + 3 * S + 500, 2, INVALID, False, np.nan, np.nan),  # gap detected
            (D0 + 3 * S + 600, 3, UPDATE, True, 100.00, 99.0),  # skipped: awaiting snapshot
            *snapshot_at(D0 + 5 * S + 10, 4),
        ]
    )
    r = replay_to_grid(ev, D0, n_grid=8, depth=1)
    c = r.columns
    # Invalid from just after the last good message (2s+100ns) until the snapshot (5s+10ns).
    assert c["book_valid"].tolist() == [False, True, True, False, False, False, True, True]
    assert c["interval_valid"].tolist() == [False, False, True, False, False, False, False, True]
    assert r.stats.skipped_updates == 1
    assert r.stats.invalid_periods[1] == (D0 + 2 * S + 101, D0 + 5 * S + 10)
    assert c["bid_sz_1"][6] == 1.0  # the snapshot replaced the book


def test_snapshot_after_updates_is_a_resync():
    ev = events([*snapshot_at(D0 + 10, 0), (D0 + S, 1, UPDATE, True, 100.0, 2.0)])
    ev2 = events(
        [
            *snapshot_at(D0 + 10, 0),
            (D0 + S, 1, UPDATE, True, 100.0, 2.0),
            *snapshot_at(D0 + 3 * S, 2),
        ]
    )
    assert replay_to_grid(ev, D0, n_grid=4).stats.resyncs == 0
    r = replay_to_grid(ev2, D0, n_grid=5, depth=1)
    assert r.stats.resyncs == 1 and r.stats.snapshots == 2
    assert r.columns["book_valid"].tolist() == [False, True, False, True, True]


def test_crossed_book_is_invalid_while_crossed():
    ev = events(
        [
            *snapshot_at(D0 + 10, 0),
            (D0 + S + 5, 1, UPDATE, True, 100.01, 1.0),  # bid locks the ask
            (D0 + 2 * S + 5, 2, UPDATE, False, 100.01, 0.0),  # ask level removed → uncrossed
        ]
    )
    r = replay_to_grid(ev, D0, n_grid=4, depth=1)
    assert r.columns["book_valid"].tolist() == [False, True, False, True]
    assert r.stats.crossed_messages == 1 and r.stats.crossed_episodes == 1


def test_invariants_checked_between_messages_not_inside():
    # Inside one message the book may cross transiently; only the end state matters.
    ev = events(
        [
            *snapshot_at(D0 + 10, 0),
            (D0 + S, 1, UPDATE, True, 100.02, 1.0),
            (D0 + S, 1, UPDATE, False, 100.01, 0.0),
            (D0 + S, 1, UPDATE, False, 100.02, 0.0),
            (D0 + S, 1, UPDATE, False, 100.05, 1.0),
        ]
    )
    r = replay_to_grid(ev, D0, n_grid=3, depth=1)
    assert r.stats.crossed_messages == 0
    assert r.columns["book_valid"].tolist() == [False, True, True]


def test_book_is_invalid_after_the_last_message():
    ev = events([*snapshot_at(D0 + 10, 0), (D0 + S + 5, 1, UPDATE, True, 100.0, 2.0)], tail=False)
    r = replay_to_grid(ev, D0, n_grid=4, depth=1)
    assert r.columns["book_valid"].tolist() == [False, True, False, False]
    assert r.stats.invalid_periods[-1] == (D0 + S + 6, D0 + 4 * S)


def test_phantom_deletes_are_counted():
    ev = events(
        [
            *snapshot_at(D0 + 10, 0),
            (D0 + 20, 1, UPDATE, True, 100.00, 0.0),  # real delete
            (D0 + 30, 2, UPDATE, True, 98.00, 0.0),  # level never existed
        ]
    )
    st = replay_to_grid(ev, D0, n_grid=2).stats
    assert st.deletes == 3 and st.phantom_deletes == 2  # + the tail no-op from events()


def test_negative_size_and_time_going_backwards_raise():
    with pytest.raises(InvariantError):
        replay_to_grid(events([*snapshot_at(10), (20, 1, UPDATE, True, 100.0, -1.0)]), 0, 3)
    with pytest.raises(InvariantError):
        replay_to_grid(events([*snapshot_at(10), (5, 1, UPDATE, True, 100.0, 1.0)]), 0, 3)


def test_trades_and_messages_bucketed_into_half_open_seconds():
    tr = Trades(
        recv_ts_ns=np.array([D0, D0 + 1, D0 + S, D0 + S + 1, D0 + 3 * S], dtype=np.int64),
        event_ts_ns=np.zeros(5, dtype=np.int64),
        price=np.ones(5),
        size=np.array([1.0, 2.0, 4.0, 8.0, 16.0]),
        is_buy=np.array([True, True, False, True, False]),
        trade_id=np.array(list("abcde"), dtype=object),
    )
    out = trades_per_second(tr, D0, n_grid=4)
    # t=0: (−1,0] ∋ D0 ; t=1: (0,1] ∋ D0+1, D0+S ; t=2: (1,2] ∋ D0+S+1 ; t=3: (2,3] ∋ D0+3S
    assert out["n_trades"].tolist() == [1, 2, 1, 1]
    assert out["buy_volume"].tolist() == [1.0, 2.0, 8.0, 0.0]
    assert out["sell_volume"].tolist() == [0.0, 4.0, 0.0, 16.0]
    ev = events([*snapshot_at(D0 + 1, 0), (D0 + S, 1, UPDATE, True, 100.0, 2.0)])
    assert messages_per_second(ev, D0, n_grid=3).tolist() == [0, 2, 0]


def test_validity_on_grid_handles_flicker_inside_interval():
    T = np.array([0, 5 * S + 10, 5 * S + 20], dtype=np.int64)
    V = np.array([True, False, True])
    bv, iv = validity_on_grid(T, V, np.arange(1, 8, dtype=np.int64) * S)
    assert bv.all()  # valid at every grid point...
    assert iv.tolist() == [True, True, True, True, True, False, True]  # ...but not on (5,6]
