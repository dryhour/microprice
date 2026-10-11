# Literature Review Notes

**Owner:** QR (Derek) · **Written:** Oct 11, 2026 (due Tue Oct 6)
**Purpose:** what each core reference says, what we take from it, and where our study differs. These notes feed the paper's introduction (§2) and the methodology choices already frozen in [hypotheses.md](./hypotheses.md) and [feature_label_spec.md](./feature_label_spec.md).

> These notes are from reading, not re-derivation. Quoted numbers are approximate. Check them against the papers before they go into the final paper, and cite the paper's table or equation number.

---

## 1. Cont, Kukanov & Stoikov (2014): The price impact of order book events

*Journal of Financial Econometrics* 12(1), 47–88.

**Question.** What explains short-interval mid-price changes: trades, volume, or something else in the book?

**Setup.** US equities (a sample of S&P 500 stocks, NYSE TAQ best-quote data, one month in 2010). They work with the **best bid and ask only** (level 1).

**The OFI variable.** For each best-quote update *n*:

eₙ = 𝟙{Pᵇₙ ≥ Pᵇₙ₋₁}qᵇₙ − 𝟙{Pᵇₙ ≤ Pᵇₙ₋₁}qᵇₙ₋₁ − 𝟙{Pᵃₙ ≤ Pᵃₙ₋₁}qᵃₙ + 𝟙{Pᵃₙ ≥ Pᵃₙ₋₁}qᵃₙ₋₁

and OFI over an interval is Σ eₙ. Intuition: bid-side size added or ask-side size removed is buying pressure. A bid price that ticks up counts its whole new queue; a bid price that ticks down counts the whole old queue as removed. Limit orders, cancels and market orders are all treated the same: only their effect on the queues at the touch matters.

**Main findings.**
- Over short intervals (≈10 s), mid-price changes are **linear** in OFI, ΔP ≈ β·OFI + ε, with high explanatory power (average R² of roughly two-thirds).
- The slope β is **inversely proportional to market depth** at the touch, β ≈ c / D. Thin books move more for the same flow.
- **Trade imbalance** (signed volume) explains much less than OFI, and adds little once OFI is in the model. Cancels and limit-order arrivals carry most of the information.
- The well-known concave (square-root-like) relation between price change and *volume* can come from aggregating this linear OFI relation, because depth and volume co-move.
- A simple model with equal depth D at every level gives ΔP ≈ OFI / (2D) (in ticks), which motivates the depth normalization.

**What we take.**
- OFI's exact definition (spec §3.3) and computing it on every best-level change, including trades.
- **Normalizing by depth** (`ofi_w = ofi_raw_w / D̄`), so β is comparable across symbols and over time.
- Trade-flow imbalance (`tfi_w`) as a separate, likely weaker, control.
- Intraday variation in β (thin books near the open) supports using spread, volatility and time-of-day controls.

**Where we differ.** Cont et al. study the **contemporaneous** relation: OFI over [t−Δ, t] against ΔP over the same interval. Much of that is mechanical, because depleting the ask queue *is* the mid move. We study OFI over (t−w, t] against the return over **(t, t+h]**, which is a much weaker relation and only exists through order-flow persistence or slow price adjustment (see Lillo & Farmer below). We should **expect much smaller R²** (well under 1%) and say up front why that is not a contradiction.

**Check in our data.** Reproduce their contemporaneous regression (ΔP over the window against OFI over the same window) as a sanity check of our OFI implementation. If R² isn't large, our OFI is probably wrong. This is a Week 3 item for the signal study.

---

## 2. Stoikov (2018): The micro-price

*Quantitative Finance* 18(12), 1959–1966.

**Question.** What is the best estimate of the "fair" price at high frequency, given the top of book?

**Problem with the mid and the weighted mid.**
- The **mid** ignores queue sizes, although the imbalance I = qᵇ/(qᵇ+qᵃ) clearly predicts the next mid move.
- The **weighted mid**, I·Pᵃ + (1−I)·Pᵇ, uses the imbalance, but it is noisy and jumps whenever a queue depletes. It is not a martingale (its expected future value is not its current value), so it is a poor fair-value estimate.

**Definition.** The micro-price is the limit of expected future mids, conditioned on the current state:

P_micro = lim_{i→∞} E[ M_{τᵢ} | Iₜ, Sₜ ]

where τᵢ are the times of successive mid changes and S is the spread. It is written as M + g(I, S), and the adjustment g is computed from a **discrete Markov chain** over (imbalance bucket, spread, mid change), estimated from data. The adjustment series converges, and the result is a martingale by construction.

**Findings.** The micro-price predicts short-term prices better than the mid and the weighted mid, and the adjustment g depends on tick size (larger relative ticks make imbalance more informative).

**What we take.**
- The **naming correction** in spec §3.7: the formula Pᵇ·qᵃ/(qᵇ+qᵃ) + Pᵃ·qᵇ/(qᵇ+qᵃ) is the *weighted mid*, so our column is `wmid`, not "microprice". Citing Stoikov for it would be wrong.
- `wmid_dev_bps` is a monotone function of QI and the spread, so it can't add much beyond `qi_l1` (a check, not a new signal).
- **Labels use the mid, not the micro-price** (spec §2). A micro-price label would be built from QI at t, so QI would look predictive mechanically.
- An optional v2 feature: Stoikov's micro-price adjustment g(I, S), estimated **inside each training fold** (it is a fitted model, so fitting it on the full sample would leak).

**Relevance to crypto.** Most of our symbols are small-tick (BTC's tick is ~0.001 bps), where the spread is often many ticks wide and imbalance at the touch says less. HBAR and APT have ticks above 1 bp, closer to the large-tick case where Stoikov finds the adjustment matters most. This is one reason H3 looks at liquidity and tick size together.

---

## 3. Cartea, Jaimungal & Penalva (2015): Algorithmic and High-Frequency Trading

Cambridge University Press. (Chapter numbers below are from the 2015 edition.)

A textbook rather than one result. The parts that matter for us:

**Microstructure and empirical facts (Part I, Ch. 1–4).** How limit order books work; the cost of a market order (half the spread, plus walking the book for size); adverse selection; intraday patterns in spreads, volume and volatility. Our EDA (spread and volatility profiles, intraday seasonality) follows the empirical chapters.

**Execution and market making (Ch. 6–10).** Trading off market orders (pay the spread, certain fill) against limit orders (earn the spread, uncertain fill, adverse selection). Market-making models with inventory risk. These are the reference for our execution Levels 1–4 and for the H4 cost argument: a taker strategy pays roughly the half-spread plus fees per side, so the predicted move must exceed the round-trip cost.

**Order imbalance (Ch. 12).** Models the volume imbalance at the touch as a Markov chain, shows that it predicts the direction of the next mid change, and puts that prediction into trading strategies (for example, when to post or take). This is the closest textbook treatment of our QI control and the stock-vs-flow distinction in H2 (QI is the stock of resting liquidity; OFI is the flow).

**What we take.**
- The cost-accounting framework for H4 and the break-even-cost framing.
- The explicit warning that passive fills are adversely selected, which is why execution Level 3/4 and the "no passive fills without queue modeling" rule are in the bias checklist.
- Imbalance as the key control: if OFI only works through its correlation with QI, H2 fails.

---

## 4. Bailey & López de Prado (2014): The deflated Sharpe ratio

*Journal of Portfolio Management* 40(5), 94–107.

**Problem.** If you try N strategy variants and report the best, its Sharpe ratio is biased upward. Even if every variant has a true Sharpe of zero, the maximum of N noisy estimates grows with N. Non-normal returns (skew, fat tails) make a short track record's Sharpe even less reliable.

**Probabilistic Sharpe ratio (PSR).** The probability that the true Sharpe exceeds a benchmark SR*, given an estimate SR̂ from T returns with skewness γ₃ and kurtosis γ₄:

PSR(SR*) = Φ( (SR̂ − SR*)·√(T − 1) / √(1 − γ₃·SR̂ + ((γ₄ − 1)/4)·SR̂²) )

(SR̂ and SR* are per-period, not annualized; γ₄ = 3 for a normal distribution.)

**Deflated Sharpe ratio (DSR).** PSR with the benchmark set to the Sharpe you'd expect from the best of N independent trials when all true Sharpes are zero:

SR₀ = √V[{SR̂ₙ}] · ( (1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) ),  DSR = PSR(SR₀)

with γ ≈ 0.5772 (Euler–Mascheroni), V[{SR̂ₙ}] the variance of the Sharpe estimates across trials, and N the number of **independent** trials. A DSR above 0.95 is the usual bar.

**What we need for it.**
- **Every trial logged** in `trials.csv`, including failures, with its Sharpe (or another per-trial statistic) so that V[{SR̂ₙ}] can be estimated. That is why the log exists. [trials.md](./trials.md) defines the columns.
- An **effective N**. Our trials are correlated (the same OFI at five windows is not five independent tries). The paper suggests clustering correlated trials and counting clusters. We will report DSR under both the raw count and the clustered count.
- T, skewness and kurtosis of the **selected** strategy's returns, measured at the frequency the Sharpe is computed at (we will use daily P&L per sample day, so T is small, which the PSR penalizes correctly).

**Related tools in our plan.** White's Reality Check (2000) and Hansen's SPA test (2005) answer the same question for the best model among many, using a bootstrap. Benjamini–Hochberg (1995) controls the false discovery rate when screening many features (we use FDR 10%, see hypotheses.md).

---

## Other references already cited in the hypotheses

- **Lillo & Farmer (2004), "The long memory of the efficient market."** The signs of market orders are strongly autocorrelated with long memory, mostly because large orders are split into many small ones. This is the main mechanism that would let *past* OFI predict *future* returns (H1), and it explains why the effect should be stronger for flow (OFI, TFI) than for book state.
- **Xu, Gould & Howison (2019), "Multi-level order-flow imbalance in a limit order book."** Extends OFI to deeper levels and finds that adding levels 2–10 improves the explanation of contemporaneous mid changes, with the combination fit by regression. Basis for `mlofi_w_l{m}` and for fitting the combination (PCA or regression) inside each training fold only.

## Open questions this review raises for Week 3

1. Reproduce the contemporaneous OFI → ΔP relation first (implementation check), then the predictive one (H1).
2. Does depth-normalizing by the trailing 30-min mean depth (spec §3.3) behave well for HBAR and APT, where touch depth is small and jumpy? Check the distribution of D̄ in the EDA.
3. For small-tick names (BTC, ETH), the touch holds very little size. OFI at level 1 may be mostly noise, and multi-level OFI may matter more. This is a reason to keep `mlofi` as a pre-registered secondary result.
