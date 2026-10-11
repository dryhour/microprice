# Data

**Owner:** QE (Josh) · **Status:** v1, Oct 11, 2026 · Covers QE Week 1–2 deliverables: source choice, raw message schema, raw archive, book reconstruction, golden tests.

## 1. Source choice

| Option | Verdict |
|---|---|
| **Coinbase spot (chosen)** | US-accessible, the deepest USD crypto books, no auth needed for public L2. Two ways in: our live WebSocket collector, and free historical days via Tardis.dev. |
| Binance.com | Geo-blocked from our network (HTTP 451). Binance.US works but is far less liquid. |
| Kraken / OKX / Bybit | Kraken is viable (per-update CRC32 checksums) but thinner. OKX and Bybit restrict US users (Bybit returned 403). |
| LOBSTER (NASDAQ) | Possible equity comparison later. Samples are short and full access is paid/academic. |

We use two Coinbase feeds. Both come from the same matching engine:

| | Live | Historical (research sample) |
|---|---|---|
| Feed | Coinbase **Advanced Trade** WebSocket `wss://advanced-trade-ws.coinbase.com`: `level2`, `market_trades`, `heartbeats` | Coinbase **Exchange** `level2` + `match`, recorded by **Tardis.dev** |
| Access | Unauthenticated | Free for the first UTC day of each month (`datasets.tardis.dev/v1/coinbase/{type}/{Y}/{M}/{D}/{SYMBOL}.csv.gz`) |
| Receive clock | This machine (`time.time_ns()`) | Tardis's recorder (`local_timestamp`, µs) |
| Venue key in `data/raw/` | `coinbase_ws` | `coinbase_tardis` |
| Used for | Engineering: 24 h capture gate, golden test against exchange snapshots, latency and health stats | All research results (universe in [research/universe.md](../research/universe.md)) |

Coinbase Exchange's `level2` and `full` WebSocket channels now require authentication. The unauthenticated `level2_batch` channel has no sequence numbers, so we can't detect gaps on it. That is why the live collector uses Advanced Trade, which has a per-connection `sequence_num`.

## 2. Raw message schema

Raw data is stored **exactly as received, before any processing**. Each derived file carries `code_git_sha`, `source_version`, `created_at` and `raw_schema_version` in its Parquet metadata. Schemas are defined in [ingest/schema.py](../ingest/schema.py).

### 2.1 Live collector: one row per WebSocket message

`data/raw/coinbase_ws/{symbol}/{YYYY-MM-DD}/part-{first_recv_ts_ns}-{nonce}.parquet`

| Column | Type | Meaning |
|---|---|---|
| `recv_ts_ns` | int64 | **Local receive time**, ns UTC (`time.time_ns()` right after `recv()`). The point-in-time clock for everything downstream. |
| `recv_mono_ns` | int64 | `time.monotonic_ns()` at receipt. For latency measurement on this machine; not comparable across machines. |
| `symbol` | string | Product id, e.g. `BTC-USD` |
| `stream` | string | `main` (long-lived connection the book is built from) or `audit` (short-lived connection that only captures a snapshot for the golden test) |
| `conn_id` | string | `{symbol}:{stream}:{connect_time_ns}:{counter}`. Unique per connection. |
| `conn_seq` | int64 | Coinbase `sequence_num`: counts every message on the connection, all channels, from 0. Null for control rows. |
| `channel` | string | `l2_data`, `market_trades`, `heartbeats`, `subscriptions`, or `_control` for rows the collector writes itself |
| `exchange_ts_ns` | int64 | Message-level `timestamp` (Coinbase send time), parsed to ns. Per-update exchange times (`event_time`) stay inside `payload`. |
| `payload` | string | The message text, verbatim |

**Control rows** (`channel = "_control"`, JSON payload with an `event` key): `connected`, `disconnected` (with reason), `gap` (with `expected` and `got`), `stale`, `rollover` (UTC midnight), `exchange_error`, `shutdown`.

**Payload formats** (Coinbase Advanced Trade):

- `l2_data`: `events[].type` is `snapshot` (full depth, every level, all with the same `event_time`) or `update`. Each `updates[]` item has `side` (`bid`/`offer`), `price_level`, `new_quantity` (absolute size; `0` deletes the level), and `event_time`.
- `market_trades`: `events[].trades[]` has `trade_id`, `price`, `size`, `time`, `side`. **`side` is the maker side** (checked against the book: for BTC-USD, 627 of 708 trades at the touch fit "maker side", and the 81 that don't are trade/book message timing races). The reader flips it to aggressor side. The `snapshot` event sent on subscribe (recent trade history) is ignored.

### 2.2 Historical (Tardis): one row per level change / trade

`data/raw/coinbase_tardis/{symbol}/{YYYY-MM-DD}/incremental_book_L2.parquet` and `trades.parquet`

Converted from the vendor CSV with columns renamed and µs → ns. Values are unchanged. Row counts match the vendor files exactly (e.g. BTC-USD 2026-10-01: 43,433,817 book rows, 489,297 trades). The vendor URL, sha256 and byte size are in the metadata.

| Column | Type | Meaning |
|---|---|---|
| `exchange_ts_ns` | int64 | Exchange timestamp of the message |
| `recv_ts_ns` | int64 | Tardis `local_timestamp`: receive time at Tardis's recorder |
| `is_snapshot` | bool | Row is part of a full snapshot. A snapshot that follows updates means Tardis reconnected: discard the book and rebuild |
| `side` | string | `bid` / `ask` |
| `price`, `size` | float64 | Level price; new absolute size (0 deletes) |

Trades: `exchange_ts_ns`, `recv_ts_ns`, `trade_id`, `side`, `price`, `size`. **`side` is the aggressor side.** Checked on HBAR-USD 2025-03-01 against our reconstructed book: 239,480 of 239,480 trades at the touch are consistent (buys at or above the ask, sells at or below the bid), with 40 inside the spread.

What a Tardis day looks like (BTC-USD 2026-10-01): 43.4M level changes in 34.7M messages (1.25 rows per message), ~20k levels per side, one vendor reconnect at 23:39:52 (≈2.4 s without a valid book), the first snapshot at 00:00:00.39 with 87 updates before it that must be skipped. Receive minus exchange time is ~39.6 ms median (Tardis's recorder location plus clock offset).

### 2.3 Sizes

| | Per symbol-day |
|---|---|
| Tardis book, BTC-USD | ~450 MB Parquet (zstd), ~485 MB vendor CSV.gz |
| Tardis book, HBAR-USD | 55–215 MB depending on the day |
| Live capture, all 6 symbols | ~0.55 GB/day (23 MB in the first hour) |

## 3. Storage layout and rules

```
data/raw/{venue}/{symbol}/{date}/...      immutable, append-only (git-ignored)
data/book/{symbol}/{date}.parquet         1 s grid book snapshots      (book/replay.py)
data/features/{symbol}/{date}.parquet     features + labels            (features/build.py)
data/vendor/                              download cache, deleted after conversion
data/logs/                                collector, loader and build logs, collector_health.jsonl
```

- **Immutability.** Raw part files are written through a temp file and `os.link`, which refuses to replace an existing path. A collector flush always writes new files, and the historical loader skips symbol-days that already exist.
- **Partitioning.** By UTC date of `recv_ts_ns`. A buffer that spans midnight is split.
- **Timestamps.** int64 ns UTC everywhere, never float seconds. Rows within a file are in arrival order.
- **Versioning.** `raw_schema_version` = 1, `book_schema_version` = 1, `feature_spec_version` = 1, plus `code_git_sha` (with `-dirty` if the tree had uncommitted changes), `source_version` and `created_at` in every file.

## 4. Live collector integrity

[ingest/coinbase_ws.py](../ingest/coinbase_ws.py). Run with `python -m ingest.coinbase_ws` (settings in [configs/collector.yaml](../configs/collector.yaml)).

| Mechanism | Behavior |
|---|---|
| Sequence check | `sequence_num` must be last + 1 (first = 0). On a gap the collector writes a `gap` row and reconnects, which delivers a fresh full snapshot. The reader re-checks sequence continuity on its own, so a gap is caught even without the control row. |
| Staleness | Heartbeats come every second. No message for 10 s → `stale` row and reconnect. |
| Reconnect | Exponential backoff with full jitter (1 s base, 60 s cap), reset after 60 s of healthy connection. Planned resyncs reconnect immediately. |
| Midnight rollover | Forced resync at 00:00 UTC, so each date partition starts with its own snapshot and replays on its own (~0.3–1 s invalid per day). |
| Audit snapshots | Every 15 min per symbol, staggered: a second connection captures a full snapshot (`stream = audit`) for the golden test. |
| Health | Every 60 s to `data/logs/collector_health.jsonl`: messages, bytes, gaps, reconnects, stale events, audits, receive-lag p50/p99. |
| Shutdown | SIGINT/SIGTERM flushes all buffers. Data loss on a hard crash is at most one flush interval (5 min). |

Tested end to end against a fake exchange that injects a sequence gap (`tests/test_collector.py`).

**Clocks.** `recv_ts_ns` comes from this laptop's NTP-disciplined clock. Receive-minus-exchange lag on the live feed is sometimes *negative* (−5 to −37 ms medians for some symbols), so the laptop clock is behind Coinbase's by tens of ms. Cross-clock lags are therefore not latency measurements. Use `recv_mono_ns` differences for latency on this machine. Point-in-time correctness doesn't depend on this, because every "known at t" decision uses `recv_ts_ns` from one clock.

## 5. Book reconstruction

[book/l2book.py](../book/l2book.py), [book/replay.py](../book/replay.py). Run with `python -m book.build`.

- One sorted price → size map per side. Prices are stored as integer ticks (`price × 10^d`, with d inferred per symbol-day), so level keys are exact. The tick size for `spread_ticks` is the GCD of observed level spacings, stored as `tick_size` in the book metadata.
- Readers ([ingest/readers.py](../ingest/readers.py)) turn either source into one normalized event stream: level changes grouped into messages, plus `INVALID` markers. The book code doesn't know which venue it is reading.
- **Invariants**, checked after every complete message: no negative sizes and receive time never decreasing (both raise), best bid < best ask (otherwise the book is flagged invalid while crossed or locked).
- **Validity** (`book_valid`): false before the first snapshot; from just after the last good message to the end of the next snapshot when there's a gap, reconnect or vendor resync (updates in between are skipped); while crossed, locked or one-sided; after the last message in the input.
- **Output**: one row per UTC second, as-of state (all messages with `recv_ts_ns ≤ t`): best 10 levels per side, level counts, `book_valid`, `interval_valid` (valid on all of (t−1 s, t]), last book receive time, and per-second counts of book messages, trades, and buy/sell volume.
- **Throughput** (single core, pure Python + `sortedcontainers`, M-series laptop): BTC-USD full day 43.4M events in 69 s (**626k events/s**); HBAR-USD 700k events/s. The QE spec target is ≥1M/s/core. Week 6 (profiling and optimization) is where to close the gap; it's fast enough for the research pipeline now.

## 6. Golden tests (mismatches must be 0)

[book/golden.py](../book/golden.py), [tests/test_golden.py](../tests/test_golden.py). Both run the production replay and compare through a hook, on every message boundary.

**Historical: vs Tardis `book_snapshot_25`.** Tardis computes top-25 snapshots from the same feed with its own reconstruction code. At every snapshot's `local_timestamp` we compare our top 25 per side (prices and sizes, exactly).
- BTC-USD 2024-11-01, first 300k snapshots: **299,559 compared, 14,977,950 levels, 0 mismatches.**
- HBAR-USD 2025-03-01 fixture (3 min): passes in CI.

**Live: vs exchange snapshots.** Every 15 minutes an audit connection captures a full-depth snapshot stamped with exchange time T. Three things make "the book at T" fuzzy on this feed, and we observed all three:

1. The stamp is accurate to tens of ms. A level added at T−23 ms and removed at T+14 ms was already gone from the snapshot.
2. Updates that share one `event_time` can straddle the snapshot (one BTC batch at T+9.5 ms was half-included).
3. **The Advanced Trade `level2` feed conflates.** Each message carries the final state of every level that changed since the previous message, so intermediate states are never sent. Evidence: 1–15% of its deletes remove a level we never saw ("phantom deletes"; BTC 15%, HBAR 1.4%), while the Tardis Exchange feed has **zero** phantom deletes in 18.9M deletes checked. A snapshot taken inside a batch can show a state the stream never reports.

So the check is **per price level, on every level of both sides**:
- a level not updated within ±250 ms of T must equal the snapshot exactly;
- a level updated in that window must equal one of the values it held in the window;
- levels touched by the last message at or before T and the first message after T (the batches either side of the snapshot) are **unverifiable** and reported as a count rather than compared.

A missed, duplicated or misapplied update persists beyond those two batches and fails. A negative control (one corrupted level) fails as expected.
- First 1.5 h of capture, all 6 symbols: **18 audits, 233,764 levels compared, 0 mismatches**; 212 levels (0.09%) unverifiable. In 15 of 18 audits the stream state exactly at T matches every level.

Results for the full 24 h capture go in §8 once it has run.

**Consequence for research.** The live feed is conflated, so OFI computed on it would miss intermediate queue changes. That is one more reason the research sample is the Tardis (Coinbase Exchange `level2`) feed, which is a complete event stream. The replay counts phantom deletes per symbol-day (`phantom_deletes` in the book metadata) so this stays monitored.

## 7. Raw data sample (for QR)

Small, real samples live in [tests/fixtures/raw/](../tests/fixtures/raw/) (regenerate with `tests/fixtures/make_fixtures.py`):

- `coinbase_ws/HBAR-USD/2026-10-11/`: the first ~11 minutes of the live capture, 3,540 messages including one audit snapshot, in the exact raw format.
- `coinbase_tardis/HBAR-USD/2025-03-01/`: the first 3 minutes of the Tardis book (36,573 rows) and trades (354), plus Tardis's `book_snapshot_25.csv.gz` for the same window.

```python
from pathlib import Path
from ingest.readers import read_ws, read_tardis_book

ev, trades, audits = read_ws("HBAR-USD", "2026-10-11", base=Path("tests/fixtures/raw"))
```

## 8. Capture log

| Run | Window (UTC) | Symbols | Gaps | Reconnects | Golden | Notes |
|---|---|---|---|---|---|---|
| Trial run | 2026-10-11 22:20 → 23:20 (1 h) | universe v1 (6) | 0 | 0 | 18 audits, 0 mismatches | stopped by hand; 232k messages archived |
| 24 h gate run | _not started_ | universe v1 (6) | | | | run with `caffeinate -i`; check with `python -m ingest.capture_report` |

## 9. Known limitations

- Live and historical receive times come from different machines and locations, so they aren't comparable. Research uses only the historical feed.
- Tardis receive times reflect Tardis's recorder, not us. Latency assumptions for H4 must come from our own `recv_mono_ns` measurements and a stated model, not from the Tardis lag.
- Tardis messages are grouped by identical receive microsecond. Several exchange messages received in the same µs are treated as one, which is harmless for invariant checks.
- One venue and one gateway per sample. No cross-venue effects.
