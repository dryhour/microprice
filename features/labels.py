"""LABELS: forward-looking. Computed separately from features (feature_label_spec.md §2).

Nothing in here may be used by a strategy, a signal or the backtester. Label columns are
named `y*` so they are easy to exclude, and `features.query` drops them unless asked.

y_h(t) = log(m_{t+h} / m_t), the as-of mid at t + h on the 1 s grid. NaN when:
    * the book is invalid anywhere in [t, t + h], or
    * the mid at t + h is stale: no book message received in the 5 s before t + h.
ydir_h = sign(y_h) ∈ {−1, +1}, NaN when y_h = 0 or NaN.
"""

from __future__ import annotations

import numpy as np

from features.v1 import window_valid

HORIZONS = (10, 30, 60)
STALE_NS = 5_000_000_000


def forward_log_return(
    log_mid: np.ndarray,
    book_valid: np.ndarray,
    interval_valid: np.ndarray,
    h: int,
    ts_ns: np.ndarray | None = None,
    last_recv_ts_ns: np.ndarray | None = None,
    stale_ns: int = STALE_NS,
) -> np.ndarray:
    n = len(log_mid)
    out = np.full(n, np.nan)
    if n <= h:
        return out
    target_ok = window_valid(interval_valid, h)[h:]  # valid on (t, t + h], indexed by t
    ok = np.asarray(book_valid, dtype=bool)[: n - h] & target_ok
    if ts_ns is not None and last_recv_ts_ns is not None:
        ok &= last_recv_ts_ns[h:] > ts_ns[h:] - stale_ns
    idx = np.flatnonzero(ok)
    out[idx] = log_mid[idx + h] - log_mid[idx]
    return out


def direction(y: np.ndarray) -> np.ndarray:
    d = np.sign(y)
    d[(d == 0) | ~np.isfinite(y)] = np.nan
    return d
