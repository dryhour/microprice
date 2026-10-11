"""Univariate ICs of the v1 features vs y_10s, y_30s, y_60s (QR Week 2; model ladder rung 1).

In-sample dates only (the holdout guard in features.query refuses anything else).

Method
  * For each (symbol, date, feature, label): Spearman and Pearson correlation over the 1 s
    grid rows where the book is valid and both values are finite. Labels overlap within a
    day, so no inference is done at the row level.
  * Per symbol: mean of the 19 daily ICs, t = mean / (sd / √days). The sample days are a
    month apart, so daily ICs are close to independent.
  * Pooled: average across symbols per date first (symbols on the same day are correlated),
    then t across the 19 dates.
  * Benjamini-Hochberg q-values over the pooled feature × label screen (FDR 10%, per
    research/hypotheses.md).
Every (feature, label, scope) evaluated is appended to research/trials.csv.

    python -m research.studies.univariate_ic
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl
from scipy import stats

from common.paths import CONFIGS
from common.universe import load_universe
from features.build import FEATURE_SPEC_VERSION
from features.query import scan_features
from research.studies.trials_log import append_trials

OUT = Path(__file__).resolve().parents[1] / "output"
LABELS = ("y_10s", "y_30s", "y_60s")
FEATURES = (
    "qi_l1",
    "spread_bps",
    "spread_ticks",
    "rv_60s",
    "rv_300s",
    "mom_10s",
    "mom_30s",
    "mom_60s",
    "mom_300s",
    "qi_l1_z",
    "spread_bps_z",
    "rv_60s_z",
    "rv_300s_z",
    "mom_10s_z",
    "mom_30s_z",
    "mom_60s_z",
    "mom_300s_z",
)


def bh_qvalues(p: np.ndarray) -> np.ndarray:
    n = len(p)
    order = np.argsort(p)
    ranked = p[order] * n / np.arange(1, n + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.minimum(q, 1.0)
    return out


def daily_ics() -> pl.DataFrame:
    u = load_universe()
    lf = scan_features(columns=[*FEATURES, *LABELS], with_labels=True)
    lf = lf.select("ts_ns", "symbol", "book_valid", *FEATURES, *LABELS)
    df = lf.filter(pl.col("book_valid")).collect()
    df = df.with_columns(
        pl.from_epoch("ts_ns", time_unit="ns").dt.strftime("%Y-%m-%d").alias("date")
    )
    assert set(df["date"].unique()) <= set(u.in_sample), "holdout rows leaked into the IC study"
    rows = []
    for (sym, date), g in df.group_by("symbol", "date"):
        for y in LABELS:
            for f in FEATURES:
                sub = g.select(f, y).drop_nans().drop_nulls()
                n = sub.height
                if n < 1000:
                    continue
                a, b = sub[f].to_numpy(), sub[y].to_numpy()
                if np.std(a) == 0 or np.std(b) == 0:
                    continue
                rows.append(
                    {
                        "symbol": sym,
                        "date": date,
                        "feature": f,
                        "label": y,
                        "n": n,
                        "ic_spearman": float(stats.spearmanr(a, b).statistic),
                        "ic_pearson": float(np.corrcoef(a, b)[0, 1]),
                    }
                )
    return pl.DataFrame(rows)


def summarize(daily: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    def tstat(col: str) -> list[pl.Expr]:
        return [
            pl.col(col).mean().alias(f"{col}_mean"),
            pl.col(col).std().alias(f"{col}_sd"),
            pl.len().alias("days"),
            (pl.col(col) > 0).mean().alias(f"{col}_pos_share"),
        ]

    by_sym = (
        daily.group_by("symbol", "feature", "label")
        .agg(
            *tstat("ic_spearman"),
            pl.col("ic_pearson").mean().alias("ic_pearson_mean"),
            pl.col("n").sum(),
        )
        .with_columns(
            (pl.col("ic_spearman_mean") / (pl.col("ic_spearman_sd") / pl.col("days").sqrt())).alias(
                "t"
            )
        )
    )
    per_date = daily.group_by("date", "feature", "label").agg(
        pl.col("ic_spearman").mean(), pl.col("ic_pearson").mean(), pl.col("n").sum()
    )
    pooled = (
        per_date.group_by("feature", "label")
        .agg(
            *tstat("ic_spearman"),
            pl.col("ic_pearson").mean().alias("ic_pearson_mean"),
            pl.col("n").sum(),
        )
        .with_columns(
            (pl.col("ic_spearman_mean") / (pl.col("ic_spearman_sd") / pl.col("days").sqrt())).alias(
                "t"
            )
        )
    )
    p = 2 * stats.t.sf(np.abs(pooled["t"].to_numpy()), df=pooled["days"].to_numpy() - 1)
    pooled = pooled.with_columns(pl.Series("p", p), pl.Series("q_bh", bh_qvalues(p)))
    p_sym = 2 * stats.t.sf(np.abs(by_sym["t"].to_numpy()), df=by_sym["days"].to_numpy() - 1)
    by_sym = by_sym.with_columns(pl.Series("p", p_sym))
    return by_sym.sort("symbol", "label", "feature"), pooled.sort("label", "feature")


def log_trials(by_sym: pl.DataFrame, pooled: pl.DataFrame) -> list[str]:
    u = load_universe()
    common = {
        "owner": "QR",
        "stage": "ic",
        "hypothesis": "none",
        "model": "spearman",
        "dates": "in_sample",
        "sample": "in_sample",
        "metric": "ic_spearman_mean_daily",
        "status": "ok",
    }
    params = {
        "grid": "1s",
        "filter": "book_valid & finite",
        "aggregation": "mean of daily ICs; t across days",
        "feature_spec_version": FEATURE_SPEC_VERSION,
        "universe_version": u.version,
        "script": "research/studies/univariate_ic.py",
    }
    rows = []
    for r in pooled.iter_rows(named=True):
        rows.append(
            {
                **common,
                "feature": r["feature"],
                "label": r["label"],
                "params": {**params, "pooling": "mean across symbols per date, t across dates"},
                "symbols": "ALL",
                "n_obs": r["n"],
                "value": f"{r['ic_spearman_mean']:.6f}",
                "t_stat": f"{r['t']:.3f}",
                "p_value": f"{r['p']:.3g}",
                "notes": f"BH q={r['q_bh']:.3g}; pearson={r['ic_pearson_mean']:.6f}; Week 2 screen",
            }
        )
    for r in by_sym.iter_rows(named=True):
        rows.append(
            {
                **common,
                "feature": r["feature"],
                "label": r["label"],
                "params": params,
                "symbols": r["symbol"],
                "n_obs": r["n"],
                "value": f"{r['ic_spearman_mean']:.6f}",
                "t_stat": f"{r['t']:.3f}",
                "p_value": f"{r['p']:.3g}",
                "notes": f"pearson={r['ic_pearson_mean']:.6f}; Week 2 screen",
            }
        )
    return append_trials(rows)


def main() -> None:
    if not (CONFIGS / "liquidity_buckets.yaml").exists():
        sys.exit("run research/studies/liquidity_buckets.py first (buckets are frozen before ICs)")
    daily = daily_ics()
    by_sym, pooled = summarize(daily)
    OUT.mkdir(parents=True, exist_ok=True)
    daily.write_csv(OUT / "ic_v1_daily.csv")
    by_sym.write_csv(OUT / "ic_v1_by_symbol.csv")
    pooled.write_csv(OUT / "ic_v1_pooled.csv")
    ids = log_trials(by_sym, pooled)
    print(json.dumps({"trials_logged": len(ids), "first": ids[0], "last": ids[-1]}))
    with pl.Config(tbl_rows=-1, tbl_cols=-1, float_precision=4, tbl_width_chars=250):
        print(
            pooled.select(
                "feature", "label", "ic_spearman_mean", "t", "q_bh", "ic_spearman_pos_share"
            )
        )


if __name__ == "__main__":
    main()
