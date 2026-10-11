"""QR's hand calculation of the v1 features on a toy book (feature_label_spec.md §3, §4).

This is the calculator behind v1_features.md. It is written straight from the spec's formulas
with exact rational arithmetic (fractions.Fraction) and the standard library only. It does NOT
import any project code, so it is an independent check of features/v1.py.

    python research/hand_calcs/compute_v1.py   # rewrites v1_features.json
"""

from __future__ import annotations

import json
import math
from fractions import Fraction as F
from pathlib import Path

# --- toy input: 1 s grid, t = 0..7 -----------------------------------------------------------
TICK = F("0.01")
BID = [F(x) for x in ["100.00", "100.00", "100.01", "100.02", "100.02", "0", "100.04", "100.05"]]
ASK = [F(x) for x in ["100.02", "100.02", "100.02", "100.04", "100.03", "0", "100.06", "100.06"]]
QB = [F(x) for x in [3, 1, 2, 5, 4, 0, 1, 2]]
QA = [F(x) for x in [1, 1, 6, 5, 1, 0, 3, 2]]
# t=5 is inside a sequence gap: the book is invalid at 5, so the intervals (4,5] and (5,6]
# are not fully valid. (0 is the first grid point, so (−1,0] is not valid either.)
BOOK_VALID = [True, True, True, True, True, False, True, True]
INTERVAL_VALID = [False, True, True, True, True, False, False, True]
N = len(BID)

# Toy-sized parameters (production values in brackets).
MOM_W = 2  # [10, 30, 60, 300]
RV_W = 3  # [60, 300]
LABEL_H = 2  # [10, 30, 60]
Z_WINDOW = 3  # [1800 = 30 min], excluding t itself
Z_MIN = 2  # [600 = 10 min of valid data]
Z_CLIP = 5


def window_ok(t: int, w: int) -> bool:
    """Every interval (s−1, s] for s in (t−w, t] is valid."""
    return t - w + 1 >= 0 and all(INTERVAL_VALID[s] for s in range(t - w + 1, t + 1))


def mid(t: int) -> F:
    return (BID[t] + ASK[t]) / 2


def opt(x):
    return None if x is None else float(x)


rows = []
for t in range(N):
    r: dict[str, float | None] = {"t": t}
    if BOOK_VALID[t]:
        m = mid(t)
        r["mid"] = float(m)
        r["qi_l1"] = float((QB[t] - QA[t]) / (QB[t] + QA[t]))
        r["spread_bps"] = float((ASK[t] - BID[t]) / m * 10_000)
        r["spread_ticks"] = float((ASK[t] - BID[t]) / TICK)
    else:
        r.update(mid=None, qi_l1=None, spread_bps=None, spread_ticks=None)
    # Momentum: log(m_t / m_{t−w}) over a fully valid window.
    r[f"mom_{MOM_W}"] = math.log(mid(t) / mid(t - MOM_W)) if window_ok(t, MOM_W) else None
    # Realized vol: sqrt(sum of squared 1 s log mid returns over (t−w, t]).
    if window_ok(t, RV_W):
        rets = [math.log(mid(s) / mid(s - 1)) for s in range(t - RV_W + 1, t + 1)]
        r[f"rv_{RV_W}"] = math.sqrt(sum(x * x for x in rets))
    else:
        r[f"rv_{RV_W}"] = None
    # Label: log(m_{t+h} / m_t), valid iff the book is valid on all of [t, t+h].
    ok = t + LABEL_H < N and BOOK_VALID[t] and window_ok(t + LABEL_H, LABEL_H)
    r[f"y_{LABEL_H}"] = math.log(mid(t + LABEL_H) / mid(t)) if ok else None
    y = r[f"y_{LABEL_H}"]
    r[f"ydir_{LABEL_H}"] = None if (y is None or y == 0) else math.copysign(1.0, y)
    rows.append(r)


def zscore(col: str) -> list[float | None]:
    """z_t = (x_t − mean) / std over the previous Z_WINDOW points (t excluded), NaNs skipped,
    sample std (ddof = 1), at least Z_MIN observations, clipped to ±Z_CLIP."""
    out: list[float | None] = []
    for t in range(N):
        x = rows[t][col]
        hist = [rows[s][col] for s in range(max(0, t - Z_WINDOW), t) if rows[s][col] is not None]
        if x is None or len(hist) < Z_MIN:
            out.append(None)
            continue
        mu = math.fsum(hist) / len(hist)
        sd = math.sqrt(math.fsum((h - mu) ** 2 for h in hist) / (len(hist) - 1))
        out.append(None if sd == 0 else max(-Z_CLIP, min(Z_CLIP, (x - mu) / sd)))
    return out


for col in ("qi_l1", "spread_bps"):
    for t, z in enumerate(zscore(col)):
        rows[t][f"{col}_z"] = z

doc = {
    "description": "Hand-computed v1 features on a toy 1 s grid. See v1_features.md.",
    "feature_spec_version": "1",
    "params": {
        "tick": float(TICK),
        "mom_w": MOM_W,
        "rv_w": RV_W,
        "label_h": LABEL_H,
        "z_window": Z_WINDOW,
        "z_min_obs": Z_MIN,
        "z_clip": Z_CLIP,
    },
    "input": {
        "best_bid": [float(x) if BOOK_VALID[i] else None for i, x in enumerate(BID)],
        "best_ask": [float(x) if BOOK_VALID[i] else None for i, x in enumerate(ASK)],
        "bid_size_l1": [float(x) if BOOK_VALID[i] else None for i, x in enumerate(QB)],
        "ask_size_l1": [float(x) if BOOK_VALID[i] else None for i, x in enumerate(QA)],
        "book_valid": BOOK_VALID,
        "interval_valid": INTERVAL_VALID,
    },
    "expected": {k: [r[k] for r in rows] for k in rows[0] if k != "t"},
}

if __name__ == "__main__":
    out = Path(__file__).with_name("v1_features.json")
    out.write_text(json.dumps(doc, indent=1) + "\n")
    for k, v in doc["expected"].items():
        print(f"{k:14s}", ["—" if x is None else round(x, 10) for x in v])
