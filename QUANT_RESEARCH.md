# Microprice — Quant Research Role

> **Microprice: A Research and Execution Framework for Short-Horizon Statistical Trading**
> Role owner: **Quant Research (QR)** · Partner role: [Quant Engineering](./QUANT_ENGINEERING.md)
> Duration: 8 weeks · Output: research paper + reproducible repo + dashboard

---

## 1. The Research Question

This project answers a question. It is not a trading bot.

> **Primary question:** Does order-flow imbalance (OFI) contain predictive information about short-horizon (10–60s) mid-price returns **after controlling for spread, volatility, and recent momentum, and after transaction costs?**

### Hypotheses

| ID | Hypothesis | Null (H0) | Primary test |
|----|------------|-----------|--------------|
| **H1** | OFI predicts the sign and size of the next *h*-second mid-price return | Coefficient on OFI = 0 | OLS with Newey-West (HAC) standard errors |
| **H2** | OFI adds information beyond spread, volatility, momentum, and top-of-book imbalance | Incremental R² / IC = 0 | Nested-model F-test, out-of-sample ΔIC |
| **H3** | Predictive power decays with horizon, fastest in liquid names | Decay rate equal across names and horizons | Horizon-by-liquidity IC curves |
| **H4** | The signal is still profitable after realistic costs | Net Sharpe ≤ 0 | Walk-forward backtest, deflated Sharpe ratio |
| **H5** | The signal is regime-dependent and decays over time | Rolling IC is stationary | Structural-break tests (CUSUM, Chow, Bai-Perron) |

**Framing rule:** a clean negative result ("OFI is predictive but not tradable after costs at retail latency") counts as a real finding. Report it honestly. Don't bend the method to get a positive Sharpe.

---

## 2. What You Own

| Area | Responsibility |
|------|----------------|
| **Hypotheses** | Write down each signal's economic rationale *before* testing it |
| **Labels** | Define forward returns: horizons, clock time vs. event time, mid vs. microprice |
| **Features** | Specify each feature mathematically. Engineering implements it; you validate it |
| **Statistics** | Regressions, IC analysis, significance tests, multiple-testing corrections |
| **Models** | Linear baseline, then regularized and nonlinear models, used only if they beat the baseline out of sample |
| **Validation** | Walk-forward design, purging and embargo, bias audits |
| **Evaluation** | Sharpe, drawdown, turnover, hit rate, capacity, break-even cost |
| **Decay** | Measure when and why the signal stops working |
| **Paper** | Write the final report |

**You do NOT own:** data ingestion, order-book reconstruction, backtester internals, fill simulation, infrastructure. You *do* own writing the spec and acceptance tests for each of these.

---

## 3. Feature Specification

Define every feature exactly. Engineering implements from this table.

Notation: at event *n* the best bid/ask prices are *Pᵇₙ, Pᵃₙ* and the sizes are *qᵇₙ, qᵃₙ*. Mid is *mₙ = (Pᵇₙ + Pᵃₙ)/2*.

| Feature | Definition | Rationale |
|---------|------------|-----------|
| **Queue imbalance (QI)** | (qᵇ − qᵃ) / (qᵇ + qᵃ) | Pressure at the touch |
| **Depth imbalance (k levels)** | Σ bid size − Σ ask size over the top *k*, normalized | Pressure deeper in the book |
| **Order-flow imbalance (OFI)** | Σ eₙ over the window, where eₙ = 𝟙{Pᵇₙ ≥ Pᵇₙ₋₁}qᵇₙ − 𝟙{Pᵇₙ ≤ Pᵇₙ₋₁}qᵇₙ₋₁ − 𝟙{Pᵃₙ ≤ Pᵃₙ₋₁}qᵃₙ + 𝟙{Pᵃₙ ≥ Pᵃₙ₋₁}qᵃₙ₋₁ | Net change in liquidity supply and demand (Cont, Kukanov & Stoikov, 2014) |
| **Multi-level OFI** | OFI computed per level, then combined (PCA or weighted) | Deeper flow information |
| **Trade-flow imbalance** | (buy volume − sell volume) / total volume, using aggressor side | Realized aggressive flow |
| **Spread** | (Pᵃ − Pᵇ) / m, in bps and in ticks | Cost and uncertainty control |
| **Microprice** | Pᵇ·qᵃ/(qᵇ+qᵃ) + Pᵃ·qᵇ/(qᵇ+qᵃ) | Better fair-value estimate (Stoikov, 2018) |
| **Realized volatility** | √Σ r² of mid returns over the trailing window | Volatility control |
| **Momentum** | Trailing mid return over 10s, 30s, 60s, 300s | Control for autocorrelation |
| **Volume / activity** | Trade count, volume, message rate over the trailing window | Liquidity-regime control |
| **Time of day** | Minutes since open, open/close flags | Intraday seasonality |

**Normalization:** z-score each feature with a **trailing** mean and std only, e.g. an expanding window or a rolling window of N minutes. Never normalize with full-day statistics.

**Labels:** yₜ,ₕ = log(m_{t+h} / m_t) for h ∈ {10, 30, 60}s. Also compute an event-time version (next *k* mid changes) and a direction label sign(yₜ,ₕ) that drops zero moves.

---

## 4. Methodology

### 4.1 Data splits

```
|---- Train ----|-- Purge --|-- Val --|-- Embargo --|-- Test (untouched until Week 7) --|
```

- **Walk-forward:** rolling or expanding train window, then the next block out of sample. Step forward and repeat.
- **Purging:** labels overlap because horizon *h* > sampling interval. Drop any training sample whose label window overlaps the test fold.
- **Embargo:** also drop a buffer of at least *h* after each test fold.
- **Final holdout:** reserve the last ~20% of dates. Touch it **once**, in Phase 7. Record in the paper that you did this.

### 4.2 Model ladder

Only move up a rung if the rung above beats the one below **out of sample**, net of complexity.

1. **Univariate:** IC (Spearman) of each feature against each label, by horizon
2. **OLS / HAC:** yₜ,ₕ = α + β·OFI + γ·controls + ε, with Newey-West lags ≥ h / sampling interval
3. **Regularized linear:** Ridge / Lasso / Elastic Net across all features
4. **Logistic:** direction classification with a calibrated probability output
5. **Gradient boosting:** LightGBM / XGBoost, only if it beats step 3
6. **Deep models (PyTorch):** only if a clear nonlinearity is shown. Optional.

### 4.3 Statistical tests

| Question | Test |
|----------|------|
| Is the coefficient nonzero, given overlapping labels? | Newey-West HAC t-stats |
| Does OFI add information beyond the controls? | Nested F-test, out-of-sample ΔR², ΔIC |
| Is model A better than model B? | Diebold-Mariano test |
| Did I get lucky after trying many signals? | Deflated Sharpe Ratio (Bailey & López de Prado), White's Reality Check or Hansen's SPA, Benjamini-Hochberg for feature screens |
| Does the result hold without distributional assumptions? | Stationary block bootstrap CIs on IC and Sharpe |
| Is the signal stable over time? | Rolling IC, CUSUM, Chow test, Bai-Perron multiple breaks |

**Keep a trial log.** Every configuration you test goes into `research/trials.csv`, including failures. The deflated Sharpe ratio needs the true number of trials.

---

## 5. Evaluation Metrics

### Predictive
- Information coefficient (Pearson and Spearman), IC t-stat, IC information ratio
- Hit rate, overall and conditional on signal strength (by decile)
- Out-of-sample R²
- Calibration curves for classifiers

### Strategy (from the engineering backtester)
- **Gross vs. net** Sharpe, both annualized with the method stated
- Max drawdown, drawdown duration, Calmar ratio
- Turnover, average holding period, trades per day
- Average P&L per trade in bps, compared against average cost per trade
- **Break-even cost:** the cost in bps per trade at which net P&L = 0. This may be the single most informative number.
- Capacity: net P&L as a function of trade size

### Decay
- IC vs. horizon curve (signal half-life)
- Rolling 20-day IC, with detected break dates
- Performance split by volatility regime, spread regime, and time of day

---

## 6. Bias Checklist

Review this before every result goes into the paper.

- [ ] **Look-ahead:** every feature at time *t* uses only data with receive timestamp ≤ *t*
- [ ] **Normalization leakage:** no full-sample means, stds, or quantiles
- [ ] **Label overlap:** purging and embargo applied, HAC errors used
- [ ] **Survivorship / selection:** symbols and dates chosen *before* looking at results
- [ ] **Fill optimism:** no fills at mid, and no passive fills without queue modeling
- [ ] **Latency:** the signal can't act on the same event that produced it
- [ ] **Data snooping:** trial count logged, deflated Sharpe reported
- [ ] **Holdout discipline:** final test set evaluated once
- [ ] **Microstructure noise:** bid-ask bounce handled (use mid or microprice, not last trade)

---

## 7. 8-Week Plan

| Week | Phase | QR deliverable | Needs from QE |
|------|-------|----------------|---------------|
| 1 | Data pipeline | Research question, hypotheses, feature spec (§3), label spec, symbol and date universe | Raw data sample, schema |
| 2 | Baseline signals | EDA notebook: spread and volatility profiles, intraday seasonality, univariate ICs | Reconstructed book + v1 features |
| 3 | Deep hypothesis | OFI study: HAC regressions, IC by horizon and liquidity, nested tests (H1–H3) | Full feature set, multi-symbol |
| 4 | Backtester | Signal-to-position rules (thresholds, holding period); acceptance tests for the backtester | Event-driven backtester v1 |
| 5 | Costs + slippage | Gross vs. net analysis, break-even cost, cost sensitivity grid (H4) | Execution sim + cost model |
| 6 | Walk-forward | Full walk-forward results, model ladder comparison, Diebold-Mariano tests | Parallel walk-forward runner |
| 7 | Robustness | Holdout evaluation, deflated Sharpe, bootstrap CIs, regime splits, decay analysis (H5) | Parameter-sweep API, fast reruns |
| 8 | Paper | Final paper, figures, README research section | Dashboard, reproducible `make paper` |

---

## 8. Paper Outline

`paper/microprice.pdf`, 8–15 pages:

1. **Abstract:** question, data, main finding with numbers
2. **Introduction:** why OFI, and prior work (Cont et al. 2014; Stoikov 2018; Cartea, Jaimungal & Penalva)
3. **Data:** source, symbols, dates, book reconstruction, cleaning, summary stats
4. **Features & labels:** exact definitions from §3
5. **Methodology:** splits, purging, model ladder, tests
6. **Results: predictability:** IC tables, HAC regressions, horizon decay
7. **Results: tradability:** backtest, costs, break-even, capacity
8. **Robustness:** holdout, regimes, bootstrap, deflated Sharpe, breaks
9. **When the signal stops working:** decay analysis and interpretation
10. **Limitations:** latency, data resolution, fill assumptions
11. **Conclusion**
12. **Appendix:** trial log summary, full tables, reproducibility instructions

---

## 9. Stack

| Tool | Use |
|------|-----|
| Python 3.11+ | Everything |
| polars / pandas | Feature analysis (polars for scale) |
| NumPy, SciPy | Numerics, tests |
| statsmodels | OLS, HAC, structural-break tests |
| scikit-learn | Regularized and linear models, pipelines, `TimeSeriesSplit` as a base |
| LightGBM | Nonlinear benchmark |
| PyTorch | Only if justified |
| matplotlib / plotly | Paper figures |
| Jupyter to scripts | Notebooks for exploration; every paper figure is produced by a **script** |

---

## 10. Interface Contract with Engineering

Both roles share this contract, and it is repeated in `QUANT_ENGINEERING.md`. Change it only by agreement, and bump the version.

| Artifact | Producer → Consumer | Format |
|----------|---------------------|--------|
| `features/{symbol}/{date}.parquet` | QE → QR | `ts_ns`, `symbol`, one column per feature in §3, labels `y_10s`, `y_30s`, `y_60s` |
| `signals/{run_id}.parquet` | QR → QE | `ts_ns`, `symbol`, `signal` (float, target position in [-1, 1]), `model_id` |
| `configs/strategy.yaml` | QR → QE | thresholds, holding period, max position, cost scenario |
| `results/{run_id}/` | QE → QR | `trades.parquet`, `pnl.parquet`, `metrics.json`, `config.yaml`, git SHA |

**Rules:**
- `ts_ns` is the **local receive time** in int64 nanoseconds UTC. The exchange time is stored separately.
- Every result is reproducible from `(git SHA, config, data version)`.

---

## 11. Definition of Done

- [ ] H1–H5 each have a stated result, test statistic, and p-value or CI
- [ ] Net-of-cost results with break-even cost reported
- [ ] Holdout evaluated exactly once; deflated Sharpe reported
- [ ] Decay analysis identifies when and under what conditions the signal fails
- [ ] Every paper figure regenerates with one command
- [ ] Bias checklist (§6) fully checked

### Resume framing

> *Designed and executed a study of order-flow imbalance as a predictor of 10–60s returns across N symbols and M million book events. Used purged walk-forward validation, HAC inference, and deflated Sharpe ratios. Found [result], with a break-even cost of X bps and signal half-life of Y seconds.*
