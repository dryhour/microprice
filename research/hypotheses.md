# Research Question and Hypotheses

**Owner:** QR (Derek) · **Status:** final v1, frozen Fri Oct 2, 2026
**Change rule:** after freezing, a hypothesis may be added but not edited. Any added hypothesis is marked as post hoc in the paper and counted in `trials.csv`.

---

## Research question

> Does order-flow imbalance (OFI) contain predictive information about short-horizon (10, 30, 60 s) mid-price log returns, **beyond** spread, realized volatility, recent momentum and top-of-book queue imbalance, and does any such predictability survive **realistic transaction costs** at retail latency?

There are two separate questions inside this, and the paper keeps them apart:

1. **Predictability (H1–H3, H5):** is there statistical information in OFI about future mid moves?
2. **Tradability (H4):** is that information large enough, and does it arrive early enough, to pay for crossing the spread and fees?

A "yes" to (1) and a "no" to (2) is the most likely outcome and a perfectly good result.

### Why this is not a trivial question

Cont, Kukanov & Stoikov (2014) show that OFI explains a large share of the **contemporaneous** mid-price change over the same interval. That relation is mechanical to a large degree: order flow that depletes the best ask queue *is* the price move. Our question is whether OFI over (t − w, t] predicts the mid change over (t, t + h]. That is a much weaker relation, and it only exists if:

- order flow is **persistent**: large orders are split into many child orders, so flow keeps coming in the same direction (Lillo & Farmer, 2004, on long memory in order signs), or
- prices **adjust slowly** to flow, e.g. because a depleted queue makes the next price move more likely before it happens.

Both mechanisms are well documented, and both are also exactly what fast liquidity providers compete away. That tension is why H3–H5 matter.

---

## Common settings

These apply to every hypothesis unless the test says otherwise.

| Item | Setting |
|------|---------|
| Label | y_h = log(m_{t+h} / m_t), mid price, h ∈ {10, 30, 60} s (see [feature_label_spec.md](./feature_label_spec.md)) |
| Main OFI variable | `ofi_10s_z`: 10 s trailing OFI, depth-normalized, trailing z-score |
| Controls | `spread_bps`, `rv_60s`, `mom_10s`, `mom_60s`, `qi_l1` |
| Sampling | 1 s grid; HAC lags L = h / 1 s (10, 30, 60) |
| Significance | α = 0.05, two-sided. Feature screens across many features or windows use Benjamini-Hochberg at FDR 10% |
| Sample | In-sample walk-forward folds only. The final holdout is used once, in Week 7 |
| Primary metric | Out-of-sample Spearman IC; t-stats from HAC regressions |

---

## H1: OFI predicts future returns

**Statement.** OFI over the trailing window predicts the sign and size of the next h-second mid return, with a **positive** coefficient.

**Economic rationale.** Positive OFI means net buying pressure: bids are added or asks are consumed. If that pressure is persistent (split orders, herding) or the price adjusts with a lag, the mid keeps moving in the same direction after t.

| | |
|---|---|
| **H0** | β_OFI = 0 in y_h = α + β·OFI + ε |
| **Test** | OLS with Newey-West HAC standard errors; Spearman IC with block-bootstrap CI |
| **Supported if** | β_OFI > 0 with HAC p < 0.05 at h = 10 s, and the OOS IC is positive in a majority of walk-forward folds |
| **Rejected if** | β_OFI is not significant, or the sign is negative. A negative sign is reported as evidence of mean reversion, not dropped |

---

## H2: OFI adds information beyond the controls

**Statement.** OFI still predicts y_h after controlling for spread, volatility, momentum and queue imbalance.

**Economic rationale.** Each control removes a specific alternative explanation:

- **Momentum.** OFI is strongly correlated with the *past* return (that is the contemporaneous impact). If returns are autocorrelated, OFI could look predictive only because it proxies for the last return.
- **Queue imbalance (QI).** QI is the *stock* of resting liquidity at the touch, and it is known to predict the next mid move. OFI is the *flow*. H2 asks whether flow tells us anything the current book state does not.
- **Spread and volatility.** Both scale how large OFI and returns are. Without them, OFI could just be picking up high-activity periods where every variable is larger.

| | |
|---|---|
| **H0** | Incremental R² (and incremental IC) from adding OFI to the control model = 0 |
| **Test** | Nested-model F-test (HAC-robust Wald) in sample; OOS ΔR² and ΔIC across walk-forward folds; Diebold-Mariano on squared forecast errors |
| **Supported if** | The HAC Wald test rejects at 0.05, **and** OOS ΔIC > 0 with a bootstrap CI that excludes 0 |
| **Rejected if** | Either condition fails. In particular, if OFI is significant alone (H1) but not with QI in the model, the conclusion is "OFI is subsumed by book imbalance" |

---

## H3: Predictability decays with horizon, faster in liquid instruments

**Statement.** IC(h) falls as h goes from 10 s to 60 s, and it falls faster for more liquid instruments.

**Economic rationale.** Information in order flow gets incorporated into prices over time, so it should be most visible at short horizons. In more liquid instruments, more participants (including fast market makers) watch the same flow and trade on it sooner, so the information is absorbed faster.

**Liquidity measure.** Each instrument is ranked by median trade count per minute and median spread in ticks over the in-sample period. Liquidity buckets are fixed before results are computed.

| | |
|---|---|
| **H0** | The IC decay rate is equal across horizons and liquidity buckets |
| **Test** | IC by horizon × liquidity bucket; fit IC(h) = IC₀·exp(−h/τ) per instrument and compare the half-lives τ·ln 2 across buckets (bootstrap CI on the difference) |
| **Supported if** | IC(10 s) > IC(60 s) for every bucket, and τ for the most liquid bucket is shorter than for the least liquid, with a CI on the difference that excludes 0 |
| **Rejected if** | Decay is flat or non-monotonic, or the half-lives are not distinguishable. With only a few instruments, this test has low power, and the paper will say so |

---

## H4: The signal is profitable after realistic costs

**Statement.** A strategy trading on OFI makes positive net P&L after fees, spread and slippage under the `realistic` cost scenario.

**Economic rationale.** Predictability is necessary but not sufficient. A taker strategy pays roughly half the spread plus the taker fee on every entry and exit. The expected move it captures must be larger than that, **and** must still be there after the latency between seeing the signal and the order reaching the venue. Our prior is that at retail latency and retail fee tiers this hypothesis is **rejected** for taker strategies. Passive (maker) strategies earn the spread but face adverse selection, which execution Level 4 measures.

| | |
|---|---|
| **H0** | Net Sharpe ≤ 0 |
| **Test** | Walk-forward backtest; deflated Sharpe ratio using the true trial count from `trials.csv`; cost sweep from 0 to N bps |
| **Supported if** | Deflated Sharpe significant at 0.05 under `realistic` costs, on the walk-forward folds **and** on the holdout |
| **Key output either way** | **Break-even cost** in bps per trade, compared with actual fee and spread levels for the venue |

---

## H5: The signal depends on regime and decays over time

**Statement.** OFI's predictive power is not stable. It differs across volatility, spread and time-of-day regimes, and it shows structural breaks over the sample.

**Economic rationale.** OFI is more informative when liquidity is thin, because the same flow moves the price further and queues refill more slowly. It should be strongest in high-volatility, wide-spread periods. Over longer periods, a public and widely known signal attracts competition, and venue changes (fee schedules, tick sizes, matching engine changes) can shift the relation abruptly.

| | |
|---|---|
| **H0** | The rolling IC is stationary, and IC is equal across regimes |
| **Test** | Rolling 20-day IC; CUSUM and Chow tests at known event dates (if any); Bai-Perron for unknown multiple breaks; IC by volatility tercile, spread tercile and time-of-day bucket, with bootstrap CIs on the differences |
| **Supported if** | At least one regime difference has a CI that excludes 0, or a structural break is detected at 0.05 |
| **Reporting** | Break dates and regime splits are reported even if not significant, together with the power caveat for a short sample |

---

## Things we commit to before seeing results

- [x] Hypotheses, nulls, tests and decision rules above are frozen as of Fri Oct 2, 2026
- [x] The main OFI variable (`ofi_10s_z`) and the control set are fixed. Other OFI windows and multi-level OFI are reported as secondary results and counted as trials
- [x] A negative sign in H1 or a rejection of H4 gets reported, not reframed
- [ ] Symbol and date universe, and liquidity buckets, chosen and frozen (due Tue Oct 6)
- [ ] Holdout dates recorded (due Tue Oct 6)

## References

- Cont, R., Kukanov, A. & Stoikov, S. (2014). The price impact of order book events. *Journal of Financial Econometrics*, 12(1).
- Stoikov, S. (2018). The micro-price: a high-frequency estimator of future prices. *Quantitative Finance*, 18(12).
- Cartea, Á., Jaimungal, S. & Penalva, J. (2015). *Algorithmic and High-Frequency Trading*. Cambridge University Press.
- Lillo, F. & Farmer, J. D. (2004). The long memory of the efficient market. *Studies in Nonlinear Dynamics & Econometrics*, 8(3).
- Xu, K., Gould, M. D. & Howison, S. D. (2019). Multi-level order-flow imbalance in a limit order book. *Market Microstructure and Liquidity*, 4.
- Bailey, D. H. & López de Prado, M. (2014). The deflated Sharpe ratio. *Journal of Portfolio Management*, 40(5).
