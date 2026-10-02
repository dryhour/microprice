# Feature and Label Spec

**Owner:** QR (Derek) · **Implements:** QE (Josh) · **Status:** signed off v1, Fri Oct 2, 2026
**Supersedes:** the feature table in [QUANT_RESEARCH.md §3](../QUANT_RESEARCH.md). Where they differ, this file wins.
**Change rule:** changes are made by agreement and bump the version. The version goes in the Parquet metadata as `feature_spec_version`.

---

## 1. Conventions

### Time

- All timestamps are **`recv_ts_ns`**: local receive time, int64 nanoseconds UTC. `exchange_ts` is stored but never used to decide what is "known" at time t.
- **Point-in-time rule.** A feature value at time t uses only events with `recv_ts_ns ≤ t`. No exceptions.
- **Sampling grid.** Features and labels are emitted on a **1 s grid**: t = every whole second UTC. The value at t is the state after the last event with `recv_ts_ns ≤ t` ("as-of" join). An event-time output (one row per book update) may be added later, but the 1 s grid is the primary dataset.
- **Windows** are half-open: (t − w, t]. An event exactly at t − w is excluded, and an event exactly at t is included.

### Book state

Notation at event n: best bid/ask prices Pᵇₙ, Pᵃₙ and sizes qᵇₙ, qᵃₙ. Level m prices and sizes are Pᵇₙ⁽ᵐ⁾, qᵇₙ⁽ᵐ⁾, with m = 1 the best level. Mid is mₙ = (Pᵇₙ + Pᵃₙ) / 2. Tick size is δ.

- **`book_valid`** (bool) is false from a detected sequence gap until the book is resynced from a snapshot, and while the book is crossed or locked (Pᵇ ≥ Pᵃ).
- Any feature whose window overlaps an invalid period is **NaN**. Rows with NaN features are kept in the file and dropped by research, so gaps stay visible.

### Sizes and returns

- Sizes are in the instrument's base units (e.g. BTC, or shares for equities), not in notional.
- Returns are log returns. A bps version is log return × 10⁴.

### Venue-dependent choices

The data source is chosen by QE on Fri Oct 2 (crypto L2 is recommended). The spec covers both cases. Only **time of day** (§3.10) and **warm-up** (§4) differ.

---

## 2. Labels

Labels are forward-looking. QE computes them in a **separate step** from features. They are written to clearly named `y_*` columns and are never available to the strategy or the backtester.

### Decisions

| Question | Decision | Reason |
|----------|----------|--------|
| Mid or microprice? | **Mid** | The microprice depends on queue imbalance at t. A microprice-based label would correlate mechanically with QI and inflate its apparent predictive power. Mid is also the standard reference in the literature. Microprice is used as a **feature** instead. |
| Clock time or event time? | **Clock time is primary**; event time is secondary | Clock-time horizons map directly to holding periods and latency in the backtest. Event time is a robustness check that adjusts for activity. |
| Horizons | **h ∈ {10, 30, 60} s** | Matches the research question |
| Zero moves | Kept in the return labels; **dropped** from direction labels | Mid often doesn't change over 10 s in large-tick instruments. The share of zeros is reported per instrument and horizon. |

### Definitions

| Column | Definition | Notes |
|--------|------------|-------|
| `y_10s`, `y_30s`, `y_60s` | log(m_{t+h} / m_t), with m_{t+h} the as-of mid at t + h | Primary labels |
| `ydir_10s`, `ydir_30s`, `ydir_60s` | sign(y_h) ∈ {−1, +1}; NaN when y_h = 0 | For classifiers and hit rate |
| `yev_1`, `yev_5`, `yev_10` | log(m_{τ_k} / m_t), where τ_k is the time of the k-th mid change after t | Event-time labels |
| `y_10s_lag100ms` | log(m_{t+0.1s+10s} / m_{t+0.1s}) | Same 10 s horizon, starting 100 ms later. Checks whether the predictable move happens before a realistic order could arrive (input to H4). |

**Validity.** A label is NaN if `book_valid` is false anywhere in [t, t + h], or if there is no event in the 5 s before t + h (stale mid). The share of NaN labels is reported.

---

## 3. Features

Each feature below has a column name, an exact definition, its parameters, and a reason for including it. QE writes one function per feature with a unit test against a hand-computed example from QR.

### 3.1 Queue imbalance (QI)

`qi_l1` = (qᵇ − qᵃ) / (qᵇ + qᵃ), at the best level, evaluated at t.

- Range [−1, 1].
- **Reason:** pressure at the touch. The strongest known short-horizon predictor of the next mid move, so it is the key control for H2.

### 3.2 Depth imbalance

`depth_imb_{k}` = (Σₘ₌₁ᵏ qᵇ⁽ᵐ⁾ − Σₘ₌₁ᵏ qᵃ⁽ᵐ⁾) / (Σₘ₌₁ᵏ qᵇ⁽ᵐ⁾ + Σₘ₌₁ᵏ qᵃ⁽ᵐ⁾), for **k ∈ {3, 5, 10}**.

- Levels are the k best **populated** price levels per side, not k ticks.
- If a side has fewer than k levels, sum the ones present and set `depth_levels_short` = true.
- **Reason:** pressure deeper in the book.

### 3.3 Order-flow imbalance (OFI)

Per-event contribution (Cont, Kukanov & Stoikov, 2014):

eₙ = 𝟙{Pᵇₙ ≥ Pᵇₙ₋₁}·qᵇₙ − 𝟙{Pᵇₙ ≤ Pᵇₙ₋₁}·qᵇₙ₋₁ − 𝟙{Pᵃₙ ≤ Pᵃₙ₋₁}·qᵃₙ + 𝟙{Pᵃₙ ≥ Pᵃₙ₋₁}·qᵃₙ₋₁

- Computed on **every best-level change**, in sequence order, including changes caused by trades.
- The first event after a resync has no valid n − 1 and contributes 0.

Raw window sum: `ofi_raw_{w}` = Σ eₙ over events in (t − w, t], for **w ∈ {1, 5, 10, 30, 60} s**.

Depth-normalized: `ofi_{w}` = `ofi_raw_{w}` / D̄ₜ, where D̄ₜ = trailing 30 min mean of (qᵇ + qᵃ)/2 at the best level, sampled on the 1 s grid.

- **Reason:** net change in supply and demand at the touch. Normalizing by depth makes values comparable across instruments and over time, as in Cont et al.
- **Main variable for H1 and H2:** `ofi_10s_z` (the z-scored `ofi_10s`, see §4). All other windows are secondary.

### 3.4 Multi-level OFI

Per-level contribution eₙ⁽ᵐ⁾: the same formula as §3.3, using level-m prices and sizes, for **m = 1…10** (Xu, Gould & Howison, 2019).

`mlofi_{w}_l{m}` = Σ eₙ⁽ᵐ⁾ over (t − w, t], divided by the trailing 30 min mean depth at level m. Windows **w ∈ {10, 60} s**.

**Combining levels.** QE stores all 10 per-level columns. QR combines them in research:

- `mlofi_{w}_pc1`: first principal component of the 10 levels. **The PCA is fit on the training fold only** and applied to validation and test. Fitting it on the full sample would leak.
- `mlofi_{w}_eq`: equal-weighted mean of the 10 levels. This needs no fitting and is a leak-free baseline.

**Reason:** flow deeper in the book carries information that level 1 misses.

### 3.5 Trade-flow imbalance

`tfi_{w}` = (buy volume − sell volume) / (buy volume + sell volume), over trades in (t − w, t], for **w ∈ {10, 60} s**.

- Side is the **aggressor side** reported by the venue. If the venue doesn't report it, use the Lee-Ready rule against the as-of mid, and document that in `docs/data.md`.
- If there were no trades in the window, the value is 0 and `tfi_{w}_empty` = true.
- **Reason:** realized aggressive flow, as opposed to the resting-book changes in OFI.

### 3.6 Spread

- `spread_bps` = (Pᵃ − Pᵇ) / m × 10⁴
- `spread_ticks` = (Pᵃ − Pᵇ) / δ

**Reason:** a cost and uncertainty control. `spread_ticks` = 1 most of the time in large-tick instruments, which is itself informative.

### 3.7 Microprice

`wmid` = Pᵇ · qᵃ / (qᵇ + qᵃ) + Pᵃ · qᵇ / (qᵇ + qᵃ)

`wmid_dev_bps` = (wmid − m) / m × 10⁴. **This is the column used as a feature**, because the price level itself is not stationary.

**Naming note.** The formula in QUANT_RESEARCH.md §3 is the size-**weighted mid**, not Stoikov's (2018) micro-price. Stoikov's micro-price is the limit of expected future mids, estimated from a Markov model of imbalance and spread. We call this column `wmid` so the paper doesn't misattribute it. Stoikov's estimator is optional for v2 and, if added, will be fit on training folds only.

**Reason:** a better fair-value estimate than mid. `wmid_dev_bps` is a monotone function of `qi_l1` and the spread, so it mostly serves as a check on QI rather than as an independent feature.

### 3.8 Realized volatility

`rv_{w}` = √( Σ r_s² ), where r_s are 1 s log mid returns on the grid in (t − w, t], for **w ∈ {60, 300} s**.

**Reason:** volatility control. It is computed from the grid mid, not from trade prices, to avoid bid-ask bounce.

### 3.9 Momentum

`mom_{w}` = log(m_t / m_{t−w}), for **w ∈ {10, 30, 60, 300} s**.

**Reason:** control for return autocorrelation, so that OFI's predictive power isn't just a proxy for the last return (H2).

### 3.10 Activity

Over (t − w, t], for **w ∈ {10, 60} s**:

- `trades_{w}`: trade count
- `volume_{w}`: traded volume in base units
- `msgs_{w}`: book update message count

All three are stored raw. Research uses log1p of each.

**Reason:** a liquidity-regime control. OFI and returns are both larger when activity is high.

### 3.11 Time of day

| Venue | Columns |
|-------|---------|
| **Crypto (24/7)** | `tod_sin`, `tod_cos` = sin/cos(2π · seconds since 00:00 UTC / 86 400); `is_weekend`; `us_session` = 1 during 13:30–20:00 UTC (US equity cash session, adjusted for daylight saving) |
| **Equities** | `min_since_open`; `is_open_30` (first 30 min); `is_close_30` (last 30 min) |

**Reason:** intraday seasonality in volume, spread and volatility.

---

## 4. Normalization

- QE stores the **raw** value of every feature, and a z-scored copy with the suffix `_z` for: `ofi_*`, `mlofi_*_l*`, `tfi_*`, `depth_imb_*`, `qi_l1`, `mom_*`, `spread_bps`, `rv_*`, and the log1p activity columns.
- **z-score:** z_t = (x_t − μ_t) / σ_t, where μ_t and σ_t are the mean and standard deviation of x over the trailing **30 min** of the 1 s grid, **excluding** t itself. Clip to [−5, 5].
- **Warm-up:** z-scores are NaN until 10 min of valid data is available. Crypto: once at the start of the dataset and after each invalid period longer than 5 min. Equities: also at each session open, with no carry-over from the previous day.
- **Never** use full-day or full-sample statistics. That includes quantiles and winsorization thresholds.
- Normalization that must be *fit* (PCA, regression weights, Stoikov's micro-price) is done by QR inside each walk-forward fold, never by QE in the feature files.

---

## 5. Output

One file per symbol per day: `data/features/{symbol}/{date}.parquet`.

| Column group | Columns |
|--------------|---------|
| Keys | `ts_ns` (grid time, int64 ns UTC), `symbol` |
| Book state | `mid`, `best_bid`, `best_ask`, `bid_size_l1`, `ask_size_l1`, `book_valid` |
| Features | all columns in §3, raw and `_z` |
| Labels | all `y*` columns in §2 |

**Parquet metadata:** `feature_spec_version` = `1`, `code_git_sha`, `source_version`, `created_at`.

---

## 6. Acceptance tests (QE)

QR supplies a hand-computed example for each item by Wed Oct 14 (Week 3), and the v1 features (QI, spread, mid, vol, momentum) by Fri Oct 9.

- [ ] Each feature matches the hand calculation on a toy book of 10–20 events, including a price-level change on each side
- [ ] OFI sign check: a pure bid-size increase at an unchanged price gives eₙ > 0; a pure ask-size increase gives eₙ < 0
- [ ] Window boundaries: an event at exactly t − w is excluded and an event at exactly t is included
- [ ] Leakage test: changing any event after t changes no feature at t
- [ ] Any feature whose window overlaps an invalid period is NaN
- [ ] z-scores at t don't change when data after t is removed

---

## Sign-off

| | Name | Date |
|---|------|------|
| Spec (QR) | Derek | Fri Oct 2, 2026 |
| Implementation review (QE) | Josh | _pending_ |
