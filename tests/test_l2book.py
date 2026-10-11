from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from book.l2book import InvariantError, L2Book, infer_price_scale, to_ticks


def test_set_delete_best_and_top():
    b = L2Book(100)
    b.set_level(True, 10000, 1.0)
    b.set_level(True, 9999, 2.0)
    b.set_level(False, 10001, 3.0)
    b.set_level(False, 10003, 4.0)
    assert b.best_bid() == (10000, 1.0)
    assert b.best_ask() == (10001, 3.0)
    assert b.top(5) == ([(10000, 1.0), (9999, 2.0)], [(10001, 3.0), (10003, 4.0)])
    b.set_level(True, 10000, 0.0)  # delete
    assert b.best_bid() == (9999, 2.0)
    b.set_level(True, 12345, 0.0)  # deleting a missing level is a no-op
    assert not b.is_crossed_or_locked()
    b.set_level(True, 10001, 1.0)  # locked
    assert b.is_crossed_or_locked()


def test_negative_size_raises():
    with pytest.raises(InvariantError):
        L2Book(100).set_level(False, 1, -0.5)


@pytest.mark.parametrize(
    ("prices", "scale"),
    [
        ([83556.14, 83556.15, 1.0], 100),
        ([0.08641, 0.0864], 100000),
        ([5.7073, 5.7], 10000),
        ([100.0, 101.0], 1),
    ],
)
def test_infer_price_scale(prices, scale):
    assert infer_price_scale(np.array(prices)) == scale


def test_to_ticks_is_exact_for_decimal_prices():
    p = np.array([83556.14, 0.29, 1e-5 * 8641])
    assert to_ticks(p, 100000).tolist() == [8355614000, 29000, 8641]


ops = st.lists(
    st.tuples(st.booleans(), st.integers(0, 40), st.sampled_from([0.0, 0.5, 1.0, 2.25, 7.0])),
    max_size=300,
)


@settings(max_examples=300, deadline=None)
@given(ops)
def test_book_matches_reference_model(seq):
    """Property: after any sequence of level updates the book equals a plain-dict model,
    sides are sorted, sizes are positive and best levels are the extremes."""
    b = L2Book(1)
    model: dict[bool, dict[int, float]] = {True: {}, False: {}}
    for is_bid, px, sz in seq:
        b.set_level(is_bid, px, sz)
        if sz == 0:
            model[is_bid].pop(px, None)
        else:
            model[is_bid][px] = sz
    bids, asks = b.levels()
    assert bids == model[True] and asks == model[False]
    assert all(v > 0 for v in [*bids.values(), *asks.values()])
    tb, ta = b.top(1000)
    assert [p for p, _ in tb] == sorted(model[True], reverse=True)
    assert [p for p, _ in ta] == sorted(model[False])
    if model[True]:
        assert b.best_bid() == (max(model[True]), model[True][max(model[True])])
    if model[False]:
        assert b.best_ask() == (min(model[False]), model[False][min(model[False])])
