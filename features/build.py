"""Book grid → features + labels → data/features/{symbol}/{date}.parquet.

Two separate steps, in this order:
  1. compute_features(): point-in-time only. Sees nothing after t.
  2. compute_labels():   forward-looking, clearly marked, written to `y*` columns.

Week 2 covers the v1 subset of the spec (QI, spread, mid, realized vol, momentum and their
z-scores, plus the clock-time labels). The remaining spec columns are added in Week 3.

Run:  python -m features.build                  # every book file in the universe
      python -m features.build --symbols HBAR-USD --dates 2025-03-01
"""

from __future__ import annotations

import argparse
import json
import logging
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from common.meta import read_metadata, version_metadata, with_metadata
from common.paths import book_path, features_path
from common.universe import load_universe
from features import labels as L
from features import v1
from features.normalize import segment_ids, trailing_zscore

log = logging.getLogger("features.build")

FEATURE_SPEC_VERSION = "1"
MOM_WINDOWS = (10, 30, 60, 300)
RV_WINDOWS = (60, 300)
# Spec §4: z-scored copies (for the v1 subset).
Z_COLUMNS = (
    "qi_l1",
    "spread_bps",
    *(f"mom_{w}s" for w in MOM_WINDOWS),
    *(f"rv_{w}s" for w in RV_WINDOWS),
)
LABEL_COLUMNS = (*(f"y_{h}s" for h in L.HORIZONS), *(f"ydir_{h}s" for h in L.HORIZONS))


def _np(table: pa.Table, name: str) -> np.ndarray:
    return table[name].to_numpy(zero_copy_only=False)


def book_state(book: pa.Table) -> dict[str, np.ndarray]:
    """Top-of-book columns, NaN wherever the book is not valid."""
    bv = _np(book, "book_valid").astype(bool)
    out = {}
    for src, dst in (
        ("bid_px_1", "best_bid"),
        ("ask_px_1", "best_ask"),
        ("bid_sz_1", "bid_size_l1"),
        ("ask_sz_1", "ask_size_l1"),
    ):
        x = _np(book, src).astype(float)
        out[dst] = np.where(bv, x, np.nan)
    out["mid"] = v1.mid(out["best_bid"], out["best_ask"])
    return out


def compute_features(
    state: dict[str, np.ndarray],
    book_valid: np.ndarray,
    interval_valid: np.ndarray,
    tick: float,
) -> dict[str, np.ndarray]:
    """Point-in-time features. Uses rows ≤ t only."""
    f: dict[str, np.ndarray] = {}
    f["qi_l1"] = v1.qi_l1(state["bid_size_l1"], state["ask_size_l1"])
    f["spread_bps"] = v1.spread_bps(state["best_bid"], state["best_ask"])
    f["spread_ticks"] = v1.spread_ticks(state["best_bid"], state["best_ask"], tick)
    log_mid = np.log(state["mid"])
    for w in RV_WINDOWS:
        f[f"rv_{w}s"] = v1.realized_vol(log_mid, interval_valid, w)
    for w in MOM_WINDOWS:
        f[f"mom_{w}s"] = v1.momentum(log_mid, interval_valid, w)
    seg = segment_ids(book_valid)
    for c in Z_COLUMNS:
        f[f"{c}_z"] = trailing_zscore(f[c], segments=seg)
    return f


def compute_labels(
    state: dict[str, np.ndarray],
    book_valid: np.ndarray,
    interval_valid: np.ndarray,
    ts_ns: np.ndarray,
    last_recv_ts_ns: np.ndarray,
) -> dict[str, np.ndarray]:
    """FORWARD-LOOKING labels. Never an input to anything that trades."""
    log_mid = np.log(state["mid"])
    y = {}
    for h in L.HORIZONS:
        y[f"y_{h}s"] = L.forward_log_return(
            log_mid, book_valid, interval_valid, h, ts_ns, last_recv_ts_ns
        )
    for h in L.HORIZONS:
        y[f"ydir_{h}s"] = L.direction(y[f"y_{h}s"])
    return y


def build_table(book: pa.Table, symbol: str) -> pa.Table:
    meta = read_metadata_from_table(book)
    tick = float(meta["tick_size"])
    ts = _np(book, "ts_ns").astype(np.int64)
    bv = _np(book, "book_valid").astype(bool)
    iv = _np(book, "interval_valid").astype(bool)
    state = book_state(book)
    feats = compute_features(state, bv, iv, tick)
    labs = compute_labels(state, bv, iv, ts, _np(book, "last_book_recv_ts_ns").astype(np.int64))
    cols: dict[str, object] = {"ts_ns": ts, "symbol": pa.array([symbol] * len(ts), pa.string())}
    cols.update(state)
    cols["book_valid"] = bv
    cols["interval_valid"] = iv
    cols.update(feats)
    cols.update(labs)
    table = pa.table(cols)
    out_meta = version_metadata(
        source_version=meta.get("source_version", "unknown"),
        feature_spec_version=FEATURE_SPEC_VERSION,
        feature_coverage="v1 subset (Week 2): qi_l1, spread, mid, rv, mom + _z; clock-time labels",
        feature_columns=",".join(feats),
        label_columns=",".join(labs),
        labels_note="y* columns are FORWARD-LOOKING labels; never use them as inputs",
        symbol=symbol,
        date=meta.get("date", ""),
        tick_size=repr(tick),
        book_code_git_sha=meta.get("code_git_sha", ""),
        book_created_at=meta.get("created_at", ""),
        book_source=meta.get("source", ""),
    )
    return with_metadata(table, out_meta)


def read_metadata_from_table(t: pa.Table) -> dict[str, str]:
    raw = t.schema.metadata or {}
    return {k.decode(): v.decode() for k, v in raw.items()}


def build_one(symbol: str, date: str, force: bool = False) -> str:
    out = features_path(symbol, date)
    src = book_path(symbol, date)
    if not src.exists():
        raise FileNotFoundError(f"no book file {src}; run python -m book.build first")
    if out.exists() and not force:
        book_meta = read_metadata(str(src))
        if read_metadata(str(out)).get("book_created_at") == book_meta.get("created_at"):
            return f"skip {symbol} {date} (up to date)"
    table = build_table(pq.read_table(src), symbol)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(out)
    bv = table["book_valid"].to_numpy(zero_copy_only=False)
    nan_y = {c: round(float(np.isnan(table[c].to_numpy()).mean()), 4) for c in ("y_10s", "y_60s")}
    return f"ok {symbol} {date}: valid {bv.mean():.4f}, NaN share {json.dumps(nan_y)}"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--symbols", nargs="+")
    p.add_argument("--dates", nargs="+")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    u = load_universe()
    symbols = args.symbols or list(u.symbols)
    dates = args.dates or list(u.all_dates)
    jobs = [(s, d) for d in dates for s in symbols if book_path(s, d).exists()]
    failures = 0
    with ProcessPoolExecutor(args.workers) as ex:
        futs = {ex.submit(build_one, s, d, args.force): (s, d) for s, d in jobs}
        for f in as_completed(futs):
            try:
                log.info(f.result())
            except Exception as exc:
                failures += 1
                log.error("FAILED %s: %r", futs[f], exc)
    log.info("done: %d jobs, %d failures", len(jobs), failures)
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
