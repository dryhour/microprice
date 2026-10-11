"""Data-quality review of the reconstructed book (QR Week 2). In-sample dates only.

Checks per symbol-day: gaps/resyncs and invalid time, crossed or locked books, stale feed
(no book message for > 5 s), spread outliers, 1 s mid-return outliers, top-of-book size
outliers, depth (levels present), and tick-size stability. No labels, no feature-label
relationships.

    python -m research.studies.data_quality
Writes research/output/data_quality_by_day.csv and research/output/data_quality_summary.md
(the narrative review is research/data_quality.md).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

from common.meta import read_metadata
from common.paths import book_path
from common.universe import load_universe

OUT = Path(__file__).resolve().parents[1] / "output"
STALE_NS = 5_000_000_000


def day_stats(symbol: str, date: str) -> dict:
    p = book_path(symbol, date)
    meta = read_metadata(str(p))
    rs = json.loads(meta["replay_stats"])
    df = pl.read_parquet(p)
    ts = df["ts_ns"].to_numpy()
    bv = df["book_valid"].to_numpy()
    bid, ask = df["bid_px_1"].to_numpy(), df["ask_px_1"].to_numpy()
    qb, qa = df["bid_sz_1"].to_numpy(), df["ask_sz_1"].to_numpy()
    last = df["last_book_recv_ts_ns"].to_numpy()
    tick = float(meta["tick_size"])
    v = bv
    mid = (bid + ask) / 2
    spread_bps = (ask - bid) / mid * 1e4
    spread_ticks = (ask - bid) / tick
    stale = v & (last <= ts - STALE_NS)
    # 1 s log mid returns where both ends are valid
    lm = np.log(np.where(v, mid, np.nan))
    r = np.diff(lm)
    r = r[np.isfinite(r)]
    mad = np.median(np.abs(r - np.median(r))) if r.size else np.nan
    robust_sd = 1.4826 * mad
    nz = r[r != 0]
    sd_nz = 1.4826 * np.median(np.abs(nz - np.median(nz))) if nz.size else np.nan
    big = np.abs(r) > 20 * sd_nz if np.isfinite(sd_nz) and sd_nz > 0 else np.zeros_like(r, bool)
    size_l1 = np.concatenate([qb[v], qa[v]])
    med_size = np.median(size_l1) if size_l1.size else np.nan
    # Time since the previous book message, at grid points inside valid stretches only
    # (gaps across a vendor resync are counted as invalid time, not as a quiet feed).
    both = v[1:] & v[:-1]
    gaps = ((ts[1:] - last[1:]) / 1e9)[both] if both.any() else np.array([])
    lv_b, lv_a = df["n_bid_levels"].to_numpy(), df["n_ask_levels"].to_numpy()
    return {
        "symbol": symbol,
        "date": date,
        "tick": tick,
        "book_rows": rs["rows"],
        "book_msgs": rs["messages"],
        "trades": int(df["n_trades"].sum()),
        "snapshots": rs["snapshots"],
        "vendor_resyncs": rs["resyncs"],
        "skipped_updates": rs["skipped_updates"],
        "phantom_delete_share": rs.get("phantom_deletes", 0) / max(rs.get("deletes", 0), 1),
        "crossed_msgs": rs["crossed_messages"],
        "crossed_episodes": rs["crossed_episodes"],
        "invalid_s": round(rs["invalid_seconds"], 2),
        "max_invalid_period_s": round(
            max((b - a for a, b in rs["invalid_periods"]), default=0) / 1e9, 2
        ),
        "book_valid_share": float(v.mean()),
        "stale_5s_seconds": int(stale.sum()),
        "max_quiet_s": float(gaps.max()) if gaps.size else np.nan,
        "spread_bps_p50": float(np.nanmedian(spread_bps[v])),
        "spread_bps_p99": float(np.nanpercentile(spread_bps[v], 99)),
        "spread_bps_max": float(np.nanmax(spread_bps[v])),
        "spread_gt_50bps_s": int(np.sum(spread_bps[v] > 50)),
        "spread_ticks_p50": float(np.nanmedian(spread_ticks[v])),
        "spread_1tick_share": float(np.mean(np.isclose(spread_ticks[v], 1.0))),
        "nonpositive_spread_s": int(np.sum((ask - bid)[v] <= 0)),
        "ret_1s_zero_share": float(np.mean(r == 0)) if r.size else np.nan,
        "ret_1s_robust_sd_bps": float(robust_sd * 1e4),
        "ret_1s_nonzero_robust_sd_bps": float(sd_nz * 1e4),
        "ret_1s_max_abs_bps": float(np.max(np.abs(r)) * 1e4) if r.size else np.nan,
        "ret_1s_gt20sd_count": int(big.sum()),
        "l1_size_median": float(med_size),
        "l1_size_max_over_median": float(np.max(size_l1) / med_size) if size_l1.size else np.nan,
        "levels_bid_min": int(lv_b[v].min()) if v.any() else 0,
        "levels_ask_min": int(lv_a[v].min()) if v.any() else 0,
        "levels_lt10_share": float(np.mean((lv_b[v] < 10) | (lv_a[v] < 10))) if v.any() else np.nan,
    }


def main() -> None:
    u = load_universe()
    rows = [day_stats(s, d) for s in u.symbols for d in u.in_sample if book_path(s, d).exists()]
    df = pl.DataFrame(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    df.write_csv(OUT / "data_quality_by_day.csv")
    agg = df.group_by("symbol", maintain_order=True).agg(
        pl.len().alias("days"),
        pl.col("tick").n_unique().alias("tick_values"),
        pl.col("book_msgs").median().alias("msgs_per_day_med"),
        pl.col("trades").median().alias("trades_per_day_med"),
        pl.col("vendor_resyncs").sum(),
        pl.col("phantom_delete_share").max(),
        pl.col("crossed_msgs").sum(),
        pl.col("invalid_s").sum(),
        pl.col("max_invalid_period_s").max(),
        pl.col("book_valid_share").min().alias("book_valid_share_min"),
        pl.col("stale_5s_seconds").sum(),
        pl.col("max_quiet_s").max(),
        pl.col("spread_bps_p50").median(),
        pl.col("spread_ticks_p50").median(),
        pl.col("spread_1tick_share").median(),
        pl.col("spread_bps_max").max(),
        pl.col("spread_gt_50bps_s").sum(),
        pl.col("nonpositive_spread_s").sum(),
        pl.col("ret_1s_zero_share").median(),
        pl.col("ret_1s_max_abs_bps").max(),
        pl.col("ret_1s_gt20sd_count").sum(),
        pl.col("l1_size_max_over_median").max(),
        pl.col("levels_lt10_share").max(),
    )
    with pl.Config(
        tbl_formatting="MARKDOWN", tbl_cols=-1, tbl_rows=-1, float_precision=4, tbl_width_chars=400
    ):
        (OUT / "data_quality_summary.md").write_text(
            "# Data-quality summary (generated)\n\nIn-sample dates only. Per-day detail: "
            "`data_quality_by_day.csv`.\n\n" + str(agg) + "\n"
        )
        print(agg)


if __name__ == "__main__":
    main()
