"""The Week 2 research scripts on synthetic data (never on real data before their turn: the
liquidity buckets must be frozen before any IC is computed on the real sample)."""

from __future__ import annotations

import csv

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

import common.paths as paths
from common.meta import with_metadata
from common.universe import load_universe

DATES = load_universe().in_sample[:3]
N = 86_400


def _day_ns(date: str) -> int:
    return int(np.datetime64(date, "ns").astype(np.int64))


def _write_book(symbol: str, date: str, trades_per_s: float, rng) -> None:
    ts = _day_ns(date) + np.arange(N, dtype=np.int64) * 10**9
    bid = np.full(N, 100.0)
    t = pa.table(
        {
            "ts_ns": ts,
            "n_trades": rng.poisson(trades_per_s, N).astype(np.int32),
            "bid_px_1": bid,
            "ask_px_1": bid + 0.01 * rng.integers(1, 3, N),
            "book_valid": np.ones(N, dtype=bool),
        }
    )
    p = paths.book_path(symbol, date)
    p.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(with_metadata(t, {"tick_size": "0.01"}), p)


def test_liquidity_buckets_rank_by_trades(tmp_data, tmp_path, monkeypatch):
    from research.studies import liquidity_buckets as lb

    u = load_universe()
    monkeypatch.setattr(lb, "CONFIGS", tmp_path)
    rng = np.random.default_rng(0)
    rates = dict(zip(u.symbols, [0.1, 5.0, 0.5, 2.0, 1.0, 0.05], strict=True))
    for s in u.symbols:
        for d in u.in_sample:
            _write_book(s, d, rates[s], rng)
    lb.main()
    doc = yaml.safe_load((tmp_path / "liquidity_buckets.yaml").read_text())
    by_rate = sorted(u.symbols, key=lambda s: -rates[s])
    assert doc["buckets"] == {"high": by_rate[:2], "mid": by_rate[2:4], "low": by_rate[4:]}


def test_liquidity_buckets_refuse_partial_data(tmp_data, tmp_path, monkeypatch):
    from research.studies import liquidity_buckets as lb

    monkeypatch.setattr(lb, "CONFIGS", tmp_path)
    _write_book("BTC-USD", DATES[0], 1.0, np.random.default_rng(0))
    with pytest.raises(SystemExit):
        lb.main()


def _write_features(symbol: str, date: str, beta: float, rng) -> None:
    ts = _day_ns(date) + np.arange(N, dtype=np.int64) * 10**9
    cols = {"ts_ns": ts, "symbol": pa.array([symbol] * N), "book_valid": np.ones(N, dtype=bool)}
    from research.studies.univariate_ic import FEATURES, LABELS

    for f in FEATURES:
        cols[f] = rng.standard_normal(N)
    for y in LABELS:
        cols[y] = beta * cols["qi_l1"] + rng.standard_normal(N)
    p = paths.features_path(symbol, date)
    p.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(cols), p)


def test_univariate_ic_finds_planted_signal_and_logs_trials(tmp_data, tmp_path, monkeypatch):
    from research.studies import trials_log
    from research.studies import univariate_ic as ic

    trials = tmp_path / "trials.csv"
    trials.write_text(
        (paths.REPO_ROOT / "research" / "trials.csv").read_text().splitlines()[0] + "\n"
    )
    monkeypatch.setattr(trials_log, "TRIALS", trials)
    rng = np.random.default_rng(1)
    for s in ("BTC-USD", "HBAR-USD"):
        for d in DATES:
            _write_features(s, d, 0.2, rng)
    daily = ic.daily_ics()
    by_sym, pooled = ic.summarize(daily)
    q = pooled.filter((pooled["feature"] == "qi_l1") & (pooled["label"] == "y_10s"))
    assert q["ic_spearman_mean"][0] == pytest.approx(0.19, abs=0.02)
    noise = pooled.filter(pooled["feature"] == "mom_10s")
    assert noise["ic_spearman_mean"].abs().max() < 0.02
    ids = ic.log_trials(by_sym, pooled)
    rows = list(csv.DictReader(trials.open()))
    assert len(rows) == len(ids) == pooled.height + by_sym.height
    assert rows[0]["trial_id"] == "T0001" and rows[-1]["trial_id"] == f"T{len(ids):04d}"
    assert {r["sample"] for r in rows} == {"in_sample"}
    assert all(r["git_sha"] and r["run_at_utc"] for r in rows)
    # Append-only: a second call continues the numbering.
    more = ic.log_trials(by_sym.head(1), pooled.head(0))
    assert more == [f"T{len(ids) + 1:04d}"]


def test_bh_qvalues():
    from research.studies.univariate_ic import bh_qvalues

    p = np.array([0.01, 0.04, 0.03, 0.5])
    q = bh_qvalues(p)
    np.testing.assert_allclose(q, [0.04, 0.0533333, 0.0533333, 0.5], rtol=1e-5)
