# Symbol and Date Universe

**Owner:** QR (Derek) · **Status:** frozen v1, Sun Oct 11, 2026 (due Tue Oct 6; 5 days late)
**Machine-readable copy:** [configs/universe.yaml](../configs/universe.yaml). Research code loads it through `common.universe`, which refuses holdout dates unless explicitly unsealed.
**Change rule:** frozen. A change is a new version with a dated entry in the log at the bottom, and it counts as a trial in `trials.csv`.

**Frozen before any result.** When this was written, no feature, label, return, IC or P&L had been computed on any of these dates. Data had been downloaded and the book replayed only to check the data format, replay speed and the golden test (engineering checks, no research outputs).

---

## Venue and data source

| | |
|---|---|
| Venue | **Coinbase** spot, USD quote. US-accessible, deep USD books, no fees to read market data. Binance.com is geo-blocked from here (HTTP 451). |
| Research data | Coinbase Exchange L2 (`level2`) and trades (`match`) as recorded by **Tardis.dev**, which publishes the first UTC day of every month for free. Full depth, every level change, with both exchange and receive timestamps. |
| Live data | Our own collector on the Coinbase Advanced Trade WebSocket (same matching engine, different gateway). Used for engineering (golden test against exchange snapshots, latency, health), **not** part of the research sample. See "Live forward sample" below. |

Details of both feeds are in [docs/data.md](../docs/data.md).

## Symbols

**Rule (decided before ranking):**

1. Start from every online Coinbase `*-USD` spot pair.
2. Exclude stablecoins and pegged assets (USDT, PYUSD, EURC, PAXG, …) and wrapped or staked derivatives (cbBTC, cbETH, WBTC, …). Their prices don't move independently.
3. Exclude pairs that Tardis didn't have before **2024-10-01**, so every symbol has a full month of history before the first sample date and exists on every sample date.
4. Rank the rest by 30-day USD volume (`volume_30day × last`, Coinbase `/products/{id}/stats`, fetched 2026-10-11 22:10 UTC).
5. Take eligible ranks **1, 2, 4, 8, 16, 32**.
6. Fallback: if a chosen symbol has no Tardis file on a sample date, replace it with the next eligible rank (r + 1) for **all** dates, and log the change below.

**Why geometric ranks.** H3 needs a spread of liquidity. Taking the top 6 would give six very liquid books. Doubling the rank each time gives a sample that runs from the most liquid USD pair to one trading about $2.5M a day, and the choice is mechanical, so we couldn't pick names we liked.

| Eligible rank | Symbol | 30d USD volume | Tick (quote increment) | Last price | Tick in bps |
|---:|---|---:|---:|---:|---:|
| 1 | **BTC-USD** | $14.63B | 0.01 | 83,554 | 0.001 |
| 2 | **ETH-USD** | $7.48B | 0.01 | 2,531 | 0.04 |
| 4 | **ZEC-USD** | $3.93B | 0.01 | 1,250 | 0.08 |
| 8 | **QNT-USD** | $1.00B | 0.01 | 237.9 | 0.42 |
| 16 | **HBAR-USD** | $0.43B | 0.00001 | 0.0932 | 1.07 |
| 32 | **APT-USD** | $0.075B | 0.0001 | 0.8984 | 1.11 |

The full ranking, including every exclusion and its reason, is saved in [universe_selection.csv](./universe_selection.csv). That file is the record. Re-running the API today would give different volumes.

Tick sizes can change over the sample. The book replay infers the tick for each symbol-day from the data (the GCD of observed price-level spacings) and stores it in the book file metadata as `tick_size`. `spread_ticks` uses that value.

## Dates

| Set | Dates | Count |
|---|---|---:|
| **In-sample** (walk-forward folds) | First UTC day of each month, **2024-11-01 → 2026-05-01** | 19 |
| **Final holdout** | **2026-06-01, 2026-07-01, 2026-08-01, 2026-09-01, 2026-10-01** | 5 |
| Total | | 24 |

- The holdout is the **last 20.8%** of dates (5 of 24), chronologically. It is used **once**, in Week 7, and the paper says so. Until then, `common.universe.Universe.check()` raises `HoldoutError` for these dates in research code. Building book and feature files for them is allowed, because that is not evaluation. Nobody looks at research outputs computed on them.
- Each date is a full UTC day (00:00:00–23:59:59), 86,400 one-second grid points per symbol.
- Dates are one month apart, so every day is replayed on its own. The trailing windows and z-score warm-up (10 min) restart each day. Effectively that drops the first 10 minutes of every day from feature-dependent analysis, about 0.7%.
- **Why monthly samples instead of consecutive days.** These are the free days. A two-year span also helps H5 (decay over time, structural breaks). The cost is that "rolling 20-day IC" in QUANT_RESEARCH.md §5 becomes a rolling IC over consecutive *sample* days, and break dates can only be placed to the nearest month. The paper states both.
- New Year's Day (2025-01-01, 2026-01-01) is in the sample. It is a thin-liquidity day in traditional markets but not obviously so in crypto. Rule fixed now: it stays in, and the EDA reports it.

## Liquidity buckets (H3)

Fixed by rule before any IC is computed:

1. On the **in-sample** dates only, compute each symbol's median trades per minute and median spread in ticks (from the book files, `book_valid` rows only).
2. Rank by median trades per minute, descending. Break ties by median spread in ticks, ascending.
3. Three buckets of two: ranks 1–2 = `high`, 3–4 = `mid`, 5–6 = `low`.

The result is written to `configs/liquidity_buckets.yaml` by `research/studies/liquidity_buckets.py`, and that step runs before `univariate_ic.py`. Expectation from volume ranks: high = BTC, ETH; mid = ZEC, QNT; low = HBAR, APT. The rule decides, not this expectation.

## Live forward sample

The collector was trialled for 1 h on 2026-10-11 (22:20–23:20 UTC) on the same six symbols. That data is from a different gateway (Advanced Trade, not Exchange) and a different receive location (this laptop), so it isn't pooled with the research sample. If enough clean days are captured before Week 7, they may be reported as an extra out-of-sample check, sealed like the holdout. That would be a **post hoc** addition and labeled as one.

## Known limitations of this universe

- **Selection on current volume.** Ranks use October 2026 volume. A symbol's liquidity in late 2024 can be very different (ZEC's 2026 volume in particular is far above its 2024 level). This is selection on liquidity, not on signal performance, so it doesn't bias the H1–H3 tests. It does mean the "rank 4" name was not rank 4 throughout the sample. H3 uses in-sample *measured* liquidity, not these ranks.
- **Survivorship.** Pairs delisted before October 2026 can't be chosen. For an OFI study on liquid pairs this is minor, and the paper notes it.
- **One venue.** All results are for Coinbase only. No cross-venue effects.

## Change log

| Date | Change | Reason |
|---|---|---|
| 2026-10-11 | v1 frozen | — |
