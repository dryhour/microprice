"""v1 features against QR's hand calculations (research/hand_calcs/v1_features.md), plus
labels, z-score warm-up, a truncation (no look-ahead) check and an end-to-end build."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest

from book.replay import build_book_grid
from common.meta import read_metadata
from common.universe import HoldoutError
from features import build as fb
from features import labels as L
from features import v1
from features.normalize import segment_ids, trailing_zscore
from ingest.readers import read_tardis_book, read_tardis_trades
from tests.conftest import FIXTURES

HAND = json.loads(
    (Path(__file__).parents[1] / "research" / "hand_calcs" / "v1_features.json").read_text()
)


def arr(xs) -> np.ndarray:
    return np.array([np.nan if x is None else x for x in xs], dtype=float)


def assert_matches(got: np.ndarray, expected: list) -> None:
    exp = arr(expected)
    assert np.array_equal(np.isnan(got), np.isnan(exp)), (got, exp)
    ok = ~np.isnan(exp)
    np.testing.assert_allclose(got[ok], exp[ok], rtol=1e-9, atol=1e-12)


@pytest.fixture(scope="module")
def toy():
    i, p = HAND["input"], HAND["params"]
    bid, ask = arr(i["best_bid"]), arr(i["best_ask"])
    qb, qa = arr(i["bid_size_l1"]), arr(i["ask_size_l1"])
    return {
        "bid": bid,
        "ask": ask,
        "qb": qb,
        "qa": qa,
        "bv": np.array(i["book_valid"]),
        "iv": np.array(i["interval_valid"]),
        "p": p,
        "lm": np.log(v1.mid(bid, ask)),
    }


def test_mid_qi_spread_match_hand_calc(toy):
    e = HAND["expected"]
    assert_matches(v1.mid(toy["bid"], toy["ask"]), e["mid"])
    assert_matches(v1.qi_l1(toy["qb"], toy["qa"]), e["qi_l1"])
    assert_matches(v1.spread_bps(toy["bid"], toy["ask"]), e["spread_bps"])
    assert_matches(v1.spread_ticks(toy["bid"], toy["ask"], toy["p"]["tick"]), e["spread_ticks"])


def test_momentum_and_realized_vol_match_hand_calc(toy):
    p, e = toy["p"], HAND["expected"]
    assert_matches(v1.momentum(toy["lm"], toy["iv"], p["mom_w"]), e[f"mom_{p['mom_w']}"])
    assert_matches(v1.realized_vol(toy["lm"], toy["iv"], p["rv_w"]), e[f"rv_{p['rv_w']}"])


def test_labels_match_hand_calc(toy):
    p, e = toy["p"], HAND["expected"]
    y = L.forward_log_return(toy["lm"], toy["bv"], toy["iv"], p["label_h"])
    assert_matches(y, e[f"y_{p['label_h']}"])
    assert_matches(L.direction(y), e[f"ydir_{p['label_h']}"])


def test_zscores_match_hand_calc(toy):
    p, e = toy["p"], HAND["expected"]
    for col, x in (
        ("qi_l1", v1.qi_l1(toy["qb"], toy["qa"])),
        ("spread_bps", v1.spread_bps(toy["bid"], toy["ask"])),
    ):
        z = trailing_zscore(x, window=p["z_window"], min_obs=p["z_min_obs"], clip=p["z_clip"])
        assert_matches(z, e[f"{col}_z"])


def test_zscore_clips_and_constant_window_is_nan():
    x = np.array([1.0, 1.0, 1.0, 1.0, 2.0, 1000.0])
    z = trailing_zscore(x, window=4, min_obs=2)
    assert np.isnan(z[:5]).all()  # history constant → σ = 0 → NaN (t=4 history is all 1s)
    assert z[5] == 5.0  # clipped


def test_zscore_warmup_restarts_after_long_invalid_period():
    n = 40
    bv = np.ones(n, dtype=bool)
    bv[10:21] = False  # 11 invalid rows > reset_gap=10
    seg = segment_ids(bv, reset_gap=10)
    assert seg[:21].max() == 0 and (seg[21:] == 1).all()
    x = np.where(bv, np.arange(n, dtype=float) % 3, np.nan)
    z = trailing_zscore(x, window=8, min_obs=4, segments=seg)
    # After the gap, 4 fresh observations are needed before the first z.
    first_after = 21 + np.flatnonzero(~np.isnan(z[21:]))[0]
    assert first_after == 25
    # A short gap (≤ reset_gap) does not restart the segment.
    assert (segment_ids(np.r_[np.ones(5), np.zeros(10), np.ones(5)].astype(bool), 10) == 0).all()


def test_stale_mid_makes_label_nan():
    n, h = 20, 3
    ts = np.arange(n, dtype=np.int64) * 10**9
    last = ts.copy()
    last[12] = ts[12] - 6 * 10**9  # nothing received in the 5 s before t = 12
    lm = np.linspace(0, 1e-3, n)
    ok = np.ones(n, dtype=bool)
    y = L.forward_log_return(lm, ok, ok, h, ts, last)
    assert np.isnan(y[12 - h]) and not np.isnan(y[11 - h]) and not np.isnan(y[13 - h])
    assert np.isnan(y[-h:]).all()


def _random_book(n: int, seed: int) -> tuple[dict, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    bid = 100 + np.cumsum(rng.choice([-0.01, 0, 0.01], n))
    ask = bid + rng.choice([0.01, 0.02, 0.03], n)
    state = {
        "best_bid": bid,
        "best_ask": ask,
        "bid_size_l1": rng.uniform(0.1, 5, n),
        "ask_size_l1": rng.uniform(0.1, 5, n),
    }
    state["mid"] = v1.mid(bid, ask)
    bv = rng.random(n) > 0.01
    iv = bv & np.r_[False, bv[:-1]]
    for k in state:
        state[k] = np.where(bv, state[k], np.nan)
    return state, bv, iv


def test_features_at_t_do_not_depend_on_data_after_t():
    """Leakage check for the v1 features: truncating the future changes nothing at t."""
    n, cut = 4000, 2500
    state, bv, iv = _random_book(n, 7)
    full = fb.compute_features(state, bv, iv, 0.01)
    short = fb.compute_features({k: v[:cut] for k, v in state.items()}, bv[:cut], iv[:cut], 0.01)
    for k in full:
        np.testing.assert_array_equal(full[k][:cut], short[k], err_msg=k)
    # And perturbing the future (instead of removing it) changes nothing either.
    state2 = {k: v.copy() for k, v in state.items()}
    for k in ("best_bid", "best_ask", "mid"):
        state2[k][cut:] += 0.37
    pert = fb.compute_features(state2, bv, iv, 0.01)
    for k in full:
        np.testing.assert_array_equal(full[k][:cut], pert[k][:cut], err_msg=k)


def test_end_to_end_build_on_fixture(tmp_path):
    base = FIXTURES / "raw"
    ev = read_tardis_book("HBAR-USD", "2025-03-01", base)
    tr = read_tardis_trades("HBAR-USD", "2025-03-01", base)
    book, st = build_book_grid(ev, tr, "2025-03-01", "HBAR-USD", "coinbase_tardis", "test")
    assert st.tick_size == pytest.approx(1e-5)
    t = fb.build_table(book, "HBAR-USD")
    names = set(t.column_names)
    for c in ("ts_ns", "symbol", "mid", "best_bid", "best_ask", "bid_size_l1", "ask_size_l1"):
        assert c in names
    for c in ("qi_l1", "spread_bps", "spread_ticks", "rv_60s", "rv_300s", "mom_10s", "mom_300s"):
        assert c in names
    for c in fb.Z_COLUMNS:
        assert f"{c}_z" in names
    assert "spread_ticks_z" not in names  # spec §4 does not z-score spread_ticks
    for c in fb.LABEL_COLUMNS:
        assert c in names
    assert t.num_rows == 86_400
    path = tmp_path / "f.parquet"
    pq.write_table(t, path)
    meta = read_metadata(str(path))
    assert meta["feature_spec_version"] == "1"
    assert {"code_git_sha", "source_version", "created_at"} <= meta.keys()
    assert "FORWARD-LOOKING" in meta["labels_note"]
    # The fixture holds 3 minutes: mid exists only then; z-scores need 10 minutes → all NaN.
    mid = t["mid"].to_numpy(zero_copy_only=False)
    assert (~np.isnan(mid)).sum() == pytest.approx(180, abs=2)
    assert np.isnan(t["qi_l1_z"].to_numpy(zero_copy_only=False)).all()


def test_query_guards():
    from features import query

    with pytest.raises(HoldoutError):
        query.feature_files(dates=["2026-10-01"])
    with pytest.raises(ValueError):
        query.asof("BTC-USD", "2025-03-01", [0], ["y_10s"])


def test_asof_returns_latest_row_at_or_before_query(tmp_data, monkeypatch):
    import pyarrow as pa

    import common.paths as paths
    from features import query

    d0 = 1_730_419_200 * 10**9  # 2024-11-01, in-sample
    t = pa.table(
        {
            "ts_ns": [d0, d0 + 10**9, d0 + 2 * 10**9],
            "symbol": ["BTC-USD"] * 3,
            "book_valid": [True, True, True],
            "mid": [1.0, 2.0, 3.0],
            "y_10s": [0.1, 0.2, 0.3],
        }
    )
    p = paths.features_path("BTC-USD", "2024-11-01")
    p.parent.mkdir(parents=True)
    pq.write_table(t, p)
    out = query.asof("BTC-USD", "2024-11-01", [d0 + 10**9 - 1, d0 + 10**9, d0 + 5 * 10**9], ["mid"])
    assert out["mid"].to_list() == [1.0, 2.0, 3.0]
    assert "y_10s" not in out.columns
    lf = query.scan_features(symbols=["BTC-USD"], dates=["2024-11-01"])
    assert "y_10s" not in lf.collect_schema().names()
    assert (
        "y_10s"
        in query.scan_features(symbols=["BTC-USD"], dates=["2024-11-01"], with_labels=True)
        .collect_schema()
        .names()
    )
