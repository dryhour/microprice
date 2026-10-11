# Week 3 feature list (final)

**Owner:** QR (Derek) · **For:** QE (Josh), Week 3 "full feature set" · **Written:** Oct 11, 2026 (due Fri Oct 9)
**Base spec:** [feature_label_spec.md](./feature_label_spec.md) v1. Every definition there stands. This file (a) fixes the build list and parameters for Week 3 and (b) settles the ambiguities found while implementing Week 2. The clarifications go into the spec as **v1.1** once QE confirms them at the Tuesday sync. None of them changes a definition QR has evaluated, because no OFI-family result exists yet.

Already built in Week 2 (`features/v1.py`, `features/labels.py`): `mid`, `qi_l1`, `spread_bps`, `spread_ticks`, `rv_60s`, `rv_300s`, `mom_{10,30,60,300}s`, their `_z` copies, and the clock-time labels `y_{10,30,60}s`, `ydir_{10,30,60}s`. They are checked against [hand_calcs/v1_features.md](./hand_calcs/v1_features.md).

## 1. Build list

| # | Columns | Spec § | Parameters | z-scored | Priority |
|---|---|---|---|---|---|
| 1 | `ofi_raw_{w}s`, `ofi_{w}s` | 3.3 | w ∈ {1, 5, 10, 30, 60} | `ofi_{w}s_z` | **P0**: `ofi_10s_z` is the H1/H2 main variable |
| 2 | `depth_l1_mean_30m` (D̄ₜ, stored for audit) | 3.3 | 1800 s trailing | no | P0 (needed by 1) |
| 3 | `mlofi_{w}s_l{m}` | 3.4 | w ∈ {10, 60}, m = 1…10 | `_z` each | P1 (pre-registered secondary) |
| 4 | `depth_imb_{k}`, `depth_levels_short` | 3.2 | k ∈ {3, 5, 10} | `depth_imb_{k}_z` | P1 |
| 5 | `tfi_{w}s`, `tfi_{w}s_empty` | 3.5 | w ∈ {10, 60} | `tfi_{w}s_z` | P1 |
| 6 | `wmid`, `wmid_dev_bps` | 3.7 | — | no (spec §4 doesn't list it) | P2 |
| 7 | `trades_{w}s`, `volume_{w}s`, `msgs_{w}s` | 3.10 | w ∈ {10, 60} | `_z` of log1p | P1 (H2/H5 controls) |
| 8 | `tod_sin`, `tod_cos`, `is_weekend`, `us_session` | 3.11 (crypto) | — | no | P1 |
| 9 | `yev_1`, `yev_5`, `yev_10` | 2 | k ∈ {1, 5, 10} mid changes | — (label) | P1 |
| 10 | `y_10s_lag100ms` | 2 | 100 ms delay | — (label) | P1 (input to H4) |

Bump `feature_coverage` in the file metadata when a group lands, so QR can tell which files have what.

## 2. Clarifications (proposed spec v1.1)

**C1. What an OFI "event" is.** eₙ is computed per **book message**, after the whole message has been applied: compare the best bid/ask (price, size) after message n with those after message n−1, and only when either changed. Not per level-change row. Within a message the book can pass through states that never existed at the exchange (our replay sees transient crossings inside messages, never between them). A message is what `ingest.readers` groups under one `msg_id`: one receive microsecond for Tardis, one exchange `event_time` for the live feed.

**C2. Resets.** The first message after any snapshot (start of day, vendor resync, live gap) contributes eₙ = 0, as the spec says, and so does any message while `book_valid` is false. Window sums whose window touches an invalid interval are NaN (the `interval_valid` rule from Week 2).

**C3. Exact window sums from per-second buckets.** Windows are half-open (t − w, t] and t is on the whole-second grid. So every OFI, MLOFI, TFI and activity window is an exact sum of per-second buckets (t−w, t−w+1], …, (t−1, t]. The book replay already emits per-second counts of book messages and trades (`n_book_msgs`, `n_trades`, `buy_volume`, `sell_volume`). Emitting `ofi_e_1s` and `mlofi_e_1s_l{m}` the same way makes every window a rolling sum, and the hand-calc tests check the bucket edges. That's a suggestion for the batch path. The streaming engine (QE Week 3) must reproduce the same numbers.

**C4. D̄ₜ for depth normalization.** D̄ₜ = mean of (qᵇ + qᵃ)/2 at level 1 over the valid grid rows in **(t − 1800 s, t]**, with t itself included because the flow it scales is also measured up to t, so including t adds no look-ahead. NaN until 600 valid rows exist (same warm-up as the z-scores, reset after invalid stretches longer than 5 min). For MLOFI level m, the same with level-m sizes.

**C5. Level m means the m-th best populated level**, as for depth imbalance. If a side has fewer than m levels, that level's eₙ⁽ᵐ⁾ is 0 for the message and `mlofi_levels_short` is true for the row.

**C6. Trade side.** Coinbase reports the aggressor, so no Lee-Ready. Tardis `side` is the aggressor (239,480 of 239,480 trades at the touch consistent). The live feed reports the maker side, and `ingest.readers` flips it (docs/data.md §2.1).

**C7. Message counts are feed-specific.** `msgs_{w}` counts book messages as the reader groups them, so Tardis and live counts are not comparable. Only the Tardis sample is used in research, so this is a documentation point.

**C8. `us_session` with daylight saving.** `us_session` = 1 when the time in America/New_York is in [09:30, 16:00) on a weekday (13:30–20:00 UTC in summer, 14:30–21:00 UTC in winter). Exchange holidays are ignored (documented limitation). `is_weekend` uses the UTC date.

**C9. Tick size.** δ for `spread_ticks` is the per-symbol-day `tick_size` in the book file metadata (the GCD of observed level spacings). It has been constant on every in-sample day checked so far.

**C10. Event-time labels need event-level mids.** `yev_k` and `y_10s_lag100ms` can't be computed from the 1 s grid. The book stage needs to emit the mid-change event stream (receive time and mid after each message where the mid changed). The same stream gives the receive time of each mid change, for the stale-mid label rule.

## 3. What Week 2 told us that matters here

- **BTC-USD sits at a 1-tick spread 83.5% of the time** (1 cent ≈ 0.001 bps). BTC on Coinbase therefore behaves like a large-tick book in tick terms even though the tick is tiny in bps, so queue imbalance and level-1 OFI should matter for it. The full six-symbol picture comes from the EDA notebook.
- **47% of 1 s BTC mid returns are exactly zero.** Expect a large share of zero labels at 10 s for the less active names (reported per symbol and horizon in the EDA).
- **Vendor resyncs exist** (BTC-USD 2025-12-01 had 10, ~10 s invalid in total). The C2 resets and NaN propagation are not edge cases.

## 4. Acceptance (QR supplies hand-computed examples by Wed Oct 14)

As in spec §6, plus:
- OFI on a toy stream where the bid price ticks up, the ask price ticks down, a pure size change happens on each side, and one message contains several level changes (C1).
- A window boundary exactly at t − w for OFI and TFI (excluded) and at t (included).
- A resync inside a window gives NaN; the first message after it contributes 0.
- MLOFI level 3 when the ask side has only 2 levels (C5).
- `us_session` on a date on each side of the DST change (C8).
