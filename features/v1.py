"""v1 features (feature_label_spec.md §3.1, 3.6, 3.8, 3.9): QI, spread, mid, realized vol,
momentum. One function per feature, all on the 1 s grid, all point-in-time:

the value at grid index t uses only grid rows ≤ t, i.e. book state as-of t (events with
recv_ts_ns ≤ t). A window feature over (t − w, t] is NaN unless the book was valid on every
interval (s − 1, s], s ∈ (t − w, t] (`interval_valid`).

Windows are in grid steps (= seconds).
"""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

BPS = 1e4


def mid(best_bid: np.ndarray, best_ask: np.ndarray) -> np.ndarray:
    return (best_bid + best_ask) / 2.0


def qi_l1(bid_size: np.ndarray, ask_size: np.ndarray) -> np.ndarray:
    """(qᵇ − qᵃ) / (qᵇ + qᵃ) at the best level, in [−1, 1]."""
    return (bid_size - ask_size) / (bid_size + ask_size)


def spread_bps(best_bid: np.ndarray, best_ask: np.ndarray) -> np.ndarray:
    return (best_ask - best_bid) / mid(best_bid, best_ask) * BPS


def spread_ticks(best_bid: np.ndarray, best_ask: np.ndarray, tick: float) -> np.ndarray:
    return (best_ask - best_bid) / tick


def window_valid(interval_valid: np.ndarray, w: int) -> np.ndarray:
    """True at t iff interval_valid holds for every s in (t − w, t] (and t − w ≥ −1)."""
    v = np.asarray(interval_valid, dtype=np.int64)
    c = np.concatenate([[0], np.cumsum(v)])
    out = np.zeros(len(v), dtype=bool)
    if w <= len(v):
        out[w - 1 :] = (c[w:] - c[:-w]) == w
    return out


def momentum(log_mid: np.ndarray, interval_valid: np.ndarray, w: int) -> np.ndarray:
    """mom_w = log(m_t / m_{t−w})."""
    out = np.full(len(log_mid), np.nan)
    ok = window_valid(interval_valid, w)
    ok[:w] = False  # needs m_{t−w}
    idx = np.flatnonzero(ok)
    out[idx] = log_mid[idx] - log_mid[idx - w]
    return out


def realized_vol(log_mid: np.ndarray, interval_valid: np.ndarray, w: int) -> np.ndarray:
    """rv_w = sqrt(Σ r_s²) over the 1 s log mid returns r_s = log(m_s / m_{s−1}), s ∈ (t−w, t]."""
    n = len(log_mid)
    r2 = np.zeros(n)
    r2[1:] = np.diff(log_mid) ** 2
    r2[~np.isfinite(r2)] = 0.0  # those windows are masked out below anyway
    out = np.full(n, np.nan)
    ok = window_valid(interval_valid, w)
    ok[:w] = False  # the first return needs m_{t−w}
    if n >= w:
        sums = sliding_window_view(r2, w).sum(axis=1)  # sums[k] covers r2[k : k + w]
        idx = np.flatnonzero(ok)
        out[idx] = np.sqrt(sums[idx - w + 1])
    return out
