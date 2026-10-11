"""Point-in-time query path over the feature and book files (polars lazy scans and DuckDB).

No server, nothing to load up front: scans read only the columns and rows a query needs.

    from features.query import scan_features, asof
    lf = scan_features(symbols=["BTC-USD"], columns=["qi_l1", "y_10s"], with_labels=True)
    df = lf.filter(pl.col("book_valid")).collect()

    # What did we know at these times? (latest row with ts_ns ≤ query time; labels excluded)
    asof("BTC-USD", "2025-03-01", [t1_ns, t2_ns], columns=["mid", "qi_l1_z"])

Guards:
  * holdout dates are refused unless `unseal_holdout=True` (common.universe);
  * label columns (`y*`) are excluded unless `with_labels=True`, and `asof` never returns them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import duckdb
import polars as pl

from common.paths import book_path, features_path
from common.universe import load_universe

KEYS = ("ts_ns", "symbol")


def _is_label(name: str) -> bool:
    return name.startswith("y_") or name.startswith("ydir_") or name.startswith("yev_")


def feature_files(
    symbols: Iterable[str] | None = None,
    dates: Iterable[str] | None = None,
    unseal_holdout: bool = False,
) -> list[str]:
    u = load_universe()
    symbols = list(symbols or u.symbols)
    dates = list(dates or u.research_dates(unseal_holdout=unseal_holdout))
    for d in dates:
        u.check(d, unseal_holdout=unseal_holdout)
    return [str(p) for d in dates for s in symbols if (p := features_path(s, d)).exists()]


def scan_features(
    symbols: Iterable[str] | None = None,
    dates: Iterable[str] | None = None,
    columns: Sequence[str] | None = None,
    with_labels: bool = False,
    unseal_holdout: bool = False,
) -> pl.LazyFrame:
    files = feature_files(symbols, dates, unseal_holdout)
    if not files:
        raise FileNotFoundError("no feature files match; run python -m features.build")
    lf = pl.scan_parquet(files)
    names = lf.collect_schema().names()
    if columns is None:
        keep = [c for c in names if with_labels or not _is_label(c)]
    else:
        bad = [c for c in columns if _is_label(c) and not with_labels]
        if bad:
            raise ValueError(f"label columns {bad} requested without with_labels=True")
        keep = [*KEYS, "book_valid", *[c for c in columns if c not in (*KEYS, "book_valid")]]
    return lf.select(keep)


def scan_book(
    symbols: Iterable[str], dates: Iterable[str], unseal_holdout: bool = False
) -> pl.LazyFrame:
    u = load_universe()
    files = []
    for s in symbols:
        for d in dates:
            u.check(d, unseal_holdout=unseal_holdout)
            if (p := book_path(s, d)).exists():
                files.append(str(p))
    return pl.scan_parquet(files, include_file_paths="file")


def asof(
    symbol: str,
    date: str,
    query_ts_ns: Sequence[int],
    columns: Sequence[str],
    unseal_holdout: bool = False,
) -> pl.DataFrame:
    """Latest feature row with ts_ns ≤ each query time. Never returns label columns."""
    if any(_is_label(c) for c in columns):
        raise ValueError("asof() is for point-in-time state; labels are forward-looking")
    lf = scan_features([symbol], [date], columns, unseal_holdout=unseal_holdout)
    q = pl.DataFrame({"query_ts_ns": list(query_ts_ns)}).with_columns(
        pl.col("query_ts_ns").cast(pl.Int64)
    )
    right = lf.sort("ts_ns").collect()
    return q.sort("query_ts_ns").join_asof(
        right, left_on="query_ts_ns", right_on="ts_ns", strategy="backward"
    )


def duckdb_connect(unseal_holdout: bool = False) -> duckdb.DuckDBPyConnection:
    """DuckDB connection with a `features` view over every allowed feature file.

    Example (as-of join in SQL):
        con = duckdb_connect()
        con.sql('''
            SELECT q.ts, f.mid, f.qi_l1_z
            FROM queries q ASOF JOIN features f
              ON q.symbol = f.symbol AND q.ts >= f.ts_ns
        ''')
    """
    files = feature_files(unseal_holdout=unseal_holdout)
    con = duckdb.connect()
    if files:
        file_list = ", ".join(f"'{f}'" for f in files)
        con.sql(f"CREATE VIEW features AS SELECT * FROM read_parquet([{file_list}])")
    return con
