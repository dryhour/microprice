"""Trailing-only z-scores (feature_label_spec.md §4).

z_t = (x_t − μ_t) / σ_t, where μ_t, σ_t are the mean and sample std (ddof = 1) of the
non-NaN values of x over the previous `window` grid points, **excluding t**. NaN with fewer
than `min_obs` observations, NaN when σ_t = 0, clipped to ±`clip`.

Warm-up restarts after any invalid period longer than `reset_gap` seconds: statistics never
reach across such a gap (crypto rule in §4; each sample day also starts fresh because days
are replayed independently).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

Z_WINDOW = 1800  # 30 min of 1 s grid points
Z_MIN_OBS = 600  # 10 min of valid data
Z_CLIP = 5.0
RESET_GAP = 300  # invalid stretch (s) after which warm-up restarts


def segment_ids(book_valid: np.ndarray, reset_gap: int = RESET_GAP) -> np.ndarray:
    """Segment id per row; a new segment starts after > `reset_gap` consecutive invalid rows."""
    bv = np.asarray(book_valid, dtype=bool)
    seg = np.zeros(len(bv), dtype=np.int64)
    run = 0
    cur = 0
    for i, v in enumerate(bv.tolist()):
        if v:
            if run > reset_gap:
                cur += 1
            run = 0
        else:
            run += 1
        seg[i] = cur
    return seg


def trailing_zscore(
    x: np.ndarray,
    window: int = Z_WINDOW,
    min_obs: int = Z_MIN_OBS,
    clip: float = Z_CLIP,
    segments: np.ndarray | None = None,
) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if segments is None:
        segments = np.zeros(len(x), dtype=np.int64)
    bounds = np.flatnonzero(np.diff(segments)) + 1
    for a, b in zip(np.r_[0, bounds], np.r_[bounds, len(x)], strict=True):
        s = pd.Series(x[a:b])
        roll = s.rolling(window, min_periods=min_obs)
        # rolling(...) at t covers [t−window+1, t]; shift(1) moves it to [t−window, t−1].
        mu = roll.mean().shift(1).to_numpy()
        sd = roll.std(ddof=1).shift(1).to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            z = (x[a:b] - mu) / sd
        z[~(sd > 0)] = np.nan
        out[a:b] = np.clip(z, -clip, clip)
    return out
