# Hand-computed examples: v1 features

**Owner:** QR (Derek) · **For:** QE unit tests (`tests/test_features_v1.py`) · **Spec:** [feature_label_spec.md](../feature_label_spec.md) v1
**Written:** Oct 11, 2026 (due Fri Oct 9)

These are the reference values the feature code must reproduce. The numbers come from [compute_v1.py](./compute_v1.py), a calculator that implements the spec formulas with exact fractions and only the standard library. It imports no project code, so it checks the implementation independently. The machine-readable copy is [v1_features.json](./v1_features.json).

Windows are shrunk so the example fits on a page. The functions under test take the window as a parameter; production values are in brackets.

| Parameter | Toy | Production |
|---|---|---|
| tick δ | 0.01 | from book metadata |
| momentum window w | 2 s | 10, 30, 60, 300 s |
| realized-vol window w | 3 s | 60, 300 s |
| label horizon h | 2 s | 10, 30, 60 s |
| z-score window (t excluded) | 3 points | 1800 points (30 min) |
| z-score minimum observations | 2 | 600 (10 min) |
| z clip | ±5 | ±5 |

## Input book (1 s grid)

t = 5 falls inside a sequence gap, so the book is invalid at 5 and the intervals (4, 5] and (5, 6] are not fully valid. t = 0 is the first grid point, so (−1, 0] is not valid either.

| t | Pᵇ | Pᵃ | qᵇ | qᵃ | book_valid | interval_valid (t−1, t] |
|---:|---:|---:|---:|---:|:---:|:---:|
| 0 | 100.00 | 100.02 | 3 | 1 | ✓ | ✗ |
| 1 | 100.00 | 100.02 | 1 | 1 | ✓ | ✓ |
| 2 | 100.01 | 100.02 | 2 | 6 | ✓ | ✓ |
| 3 | 100.02 | 100.04 | 5 | 5 | ✓ | ✓ |
| 4 | 100.02 | 100.03 | 4 | 1 | ✓ | ✓ |
| 5 | — | — | — | — | ✗ | ✗ |
| 6 | 100.04 | 100.06 | 1 | 3 | ✓ | ✗ |
| 7 | 100.05 | 100.06 | 2 | 2 | ✓ | ✓ |

## Point-in-time features (valid iff `book_valid` at t)

| t | mid = (Pᵇ+Pᵃ)/2 | qi_l1 = (qᵇ−qᵃ)/(qᵇ+qᵃ) | spread_ticks = (Pᵃ−Pᵇ)/δ | spread_bps = (Pᵃ−Pᵇ)/mid·10⁴ |
|---:|---:|---:|---:|---:|
| 0 | 100.01 | (3−1)/4 = 0.5 | 2 | 0.02/100.01·10⁴ = 1.9998000200 |
| 1 | 100.01 | 0 | 2 | 1.9998000200 |
| 2 | 100.015 | (2−6)/8 = −0.5 | 1 | 0.01/100.015·10⁴ = 0.9998500225 |
| 3 | 100.03 | 0 | 2 | 0.02/100.03·10⁴ = 1.9994001799 |
| 4 | 100.025 | (4−1)/5 = 0.6 | 1 | 0.01/100.025·10⁴ = 0.9997500625 |
| 5 | NaN | NaN | NaN | NaN |
| 6 | 100.05 | (1−3)/4 = −0.5 | 2 | 0.02/100.05·10⁴ = 1.9990004998 |
| 7 | 100.055 | 0 | 1 | 0.01/100.055·10⁴ = 0.9994503023 |

## Window features (valid iff every interval in (t−w, t] is valid)

**Momentum** `mom_2` = log(mₜ / mₜ₋₂):

| t | window (t−2, t] | value |
|---:|---|---:|
| 0, 1 | includes (−1, 0] | NaN |
| 2 | (0, 2] ✓ | log(100.015/100.01) = 4.99938e−5 |
| 3 | (1, 3] ✓ | log(100.03/100.01) = 1.99960e−4 |
| 4 | (2, 4] ✓ | log(100.025/100.015) = 9.99800e−5 |
| 5, 6, 7 | include (4, 5] or (5, 6] | NaN |

**Realized volatility** `rv_3` = √(r₁² + r₂² + r₃²) over the 1 s log mid returns in (t−3, t]:

| t | returns | value |
|---:|---|---:|
| 3 | log(100.01/100.01)=0, log(100.015/100.01), log(100.03/100.015) | 1.580799e−4 |
| 4 | log(100.015/100.01), log(100.03/100.015), log(100.025/100.03) | 1.657947e−4 |
| others | window touches an invalid interval | NaN |

## Labels (valid iff the book is valid on all of [t, t+h])

`y_2` = log(mₜ₊₂ / mₜ); `ydir_2` = sign(y_2), NaN when y_2 = 0.

| t | y_2 | ydir_2 |
|---:|---:|---:|
| 0 | log(100.015/100.01) = 4.99938e−5 | +1 |
| 1 | log(100.03/100.01) = 1.99960e−4 | +1 |
| 2 | log(100.025/100.015) = 9.99800e−5 | +1 |
| 3, 4 | (t, t+2] contains (4, 5] → NaN | NaN |
| 5 | book invalid at t → NaN | NaN |
| 6, 7 | t+2 beyond the data → NaN | NaN |

The stale-mid rule (NaN if there was no book message in the 5 s before t+h) is tested separately in `tests/test_features_v1.py` with a dedicated case.

## Trailing z-score (§4)

zₜ = (xₜ − μ) / σ, where μ and σ are the mean and **sample** standard deviation (ddof = 1) of the previous 3 non-NaN values (t excluded). It is NaN with fewer than 2 observations, NaN if σ = 0, and clipped to ±5.

**`qi_l1_z`**

| t | history | μ | σ | z |
|---:|---|---:|---:|---:|
| 0, 1 | fewer than 2 values | | | NaN |
| 2 | 0.5, 0 | 0.25 | √0.125 = 0.353553 | (−0.5 − 0.25)/0.353553 = −2.121320 |
| 3 | 0.5, 0, −0.5 | 0 | 0.5 | 0 |
| 4 | 0, −0.5, 0 | −1/6 | √(1/12) = 0.288675 | (0.6 + 1/6)/0.288675 = 2.655811 |
| 5 | x₅ is NaN | | | NaN |
| 6 | 0, 0.6 (x₅ skipped) | 0.3 | 0.424264 | (−0.5 − 0.3)/0.424264 = −1.885618 |
| 7 | 0.6, −0.5 | 0.05 | 0.777817 | (0 − 0.05)/0.777817 = −0.064282 |

**`spread_bps_z`**: at t = 2 the history (t = 0, 1) is constant, so σ = 0 and z is NaN. Remaining values: t3 = 0.5766576913, t4 = −1.1548736482, t6 = 0.7065413502, t7 = −0.7075310241 (see JSON).

## Tolerance

Floating-point implementations should match to **1e−9 relative** (1e−12 absolute for values near 0).
