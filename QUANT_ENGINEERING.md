# Microprice — Quant Engineering Role

> **Microprice: A Research and Execution Framework for Short-Horizon Statistical Trading**
> Role owner: **Quant Engineering (QE)** · Partner role: [Quant Research](./QUANT_RESEARCH.md)
> Duration: 8 weeks · Output: data platform + backtester + execution sim + API/dashboard

---

## 1. Mission

Build infrastructure that lets research answer this question **correctly, reproducibly, and quickly**:

> *Does order-flow imbalance predict 10–60s returns after controlling for spread, volatility, and momentum, and after transaction costs?*

The research conclusion is only as trustworthy as the system under it. Your job is to make three kinds of error impossible or detectable:

1. **Data errors:** a wrong book, missing messages, bad timestamps
2. **Look-ahead errors:** the future leaking into features or fills
3. **Optimistic execution:** fills that couldn't happen in reality

A lightning-fast backtester that fills at mid is worthless. A correct one that runs in 10 minutes is fine. **Correctness first, then measure, then optimize the real bottleneck.**

---

## 2. What You Own

| Area | Responsibility |
|------|----------------|
| **Ingestion** | Historical loaders and a live WebSocket collector; gap detection, recovery |
| **Order book** | L2 (optionally L3) book reconstruction from snapshots and deltas |
| **Storage** | Partitioned, versioned, columnar tick storage; fast point-in-time queries |
| **Feature pipeline** | Implement research's feature spec; the same code runs in backtest and live |
| **Backtester** | Event-driven, deterministic, no look-ahead by construction |
| **Execution sim** | Latency, spread crossing, queue position, partial fills |
| **Cost model** | Fees, half-spread, slippage, market impact; pluggable scenarios |
| **Performance** | Profile, find the real bottleneck, optimize it, and document why |
| **API & dashboard** | Launch runs, browse results, compare experiments |
| **Reproducibility** | Docker, CI, data versioning, config-driven runs |

**You do NOT own:** choosing signals, statistical conclusions, the paper's claims. You *do* own the engineering sections of the paper (data, execution model, limitations).

---

## 3. Architecture

```
                    ┌──────────────────────────┐
  Exchange WS / ──▶ │  Ingestion (async)        │  seq-check, gap-fill, raw archive
  Historical files  └────────────┬─────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │  Raw event store          │  Parquet, append-only, partitioned
                    │  (symbol/date/*.parquet)  │  by symbol/date
                    └────────────┬─────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │  Book reconstruction      │  deterministic replay → L2 snapshots
                    └────────────┬─────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │  Feature pipeline         │  streaming, point-in-time, same code
                    │  (research spec §3)       │  for batch and live
                    └──────┬─────────────┬─────┘
                           ▼             ▼
                 features/*.parquet   live feature stream (Redis, optional)
                           │
          signals from QR  ▼
                    ┌──────────────────────────┐
                    │  Event-driven backtester  │  clock, event queue, strategy,
                    │  + execution simulator    │  order mgmt, fills, costs, portfolio
                    └────────────┬─────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │  Results store + FastAPI  │  runs, metrics, trades, configs
                    │  + dashboard              │
                    └──────────────────────────┘
```

---

## 4. Component Specs

### 4.1 Market data

**Source options:** pick one in Week 1 and document the choice.

| Source | Pros | Cons |
|--------|------|------|
| Crypto exchange L2 WebSocket (e.g. Coinbase, Binance) | Free, real-time, 24/7, full depth | Different microstructure from equities; fee structure differs |
| LOBSTER sample files (NASDAQ) | Real equity L3 data, research standard | Samples are limited; full access is paid/academic |
| Databento / Polygon / similar | Clean historical equity L2/L3 | Paid |

**Recommendation:** crypto L2 for the live collector and volume of data. Add a LOBSTER sample for an equity comparison if time allows.

**Ingestion requirements:**
- Async WebSocket client (`asyncio` + `websockets`/`aiohttp`) with reconnect and backoff
- **Sequence-number checks.** On a gap, mark the book invalid, refetch the snapshot, and resync.
- Record **both** `exchange_ts` and `recv_ts_ns`. Use `time.time_ns()` for wall-clock time and a monotonic clock for latency measurement.
- Write raw messages to an append-only archive *before* any processing. Raw data is immutable; everything else can be regenerated.
- Health metrics: messages/sec, gaps, resyncs, lag between `exchange_ts` and `recv_ts`

### 4.2 Storage

- **Format:** Parquet (zstd), partitioned `raw/{venue}/{symbol}/{date}/`, `book/…`, `features/…`
- **Query engine:** DuckDB or polars lazy scans, with no server needed for research
- **Timestamps:** int64 ns UTC everywhere, sorted within partition; never float seconds
- **Versioning:** each derived dataset records `code_git_sha`, `source_version`, `created_at` in the Parquet metadata
- **Optional:** PostgreSQL/TimescaleDB for run metadata and results; Redis for the live feature stream; Kafka only if you're deliberately practicing it

### 4.3 Order-book reconstruction

- Price-level map per side, e.g. a sorted dict or arrays indexed by tick offset
- Apply snapshot, then deltas in sequence order
- **Invariants checked on every update** (can be disabled in fast mode): best bid < best ask, no negative sizes, sequence strictly increasing
- **Golden tests:** replay a recorded day and compare against snapshots the exchange sent periodically. Mismatches must be 0.

### 4.4 Feature pipeline

- Implements research's feature spec **exactly**, one function per feature with a unit test against a hand-computed example
- **Streaming/incremental** design (update on each event), so the same code path serves backtest and live. Batch mode = replay events through the streaming engine.
- **Point-in-time guarantee:** the feature at `t` is computed only from events with `recv_ts_ns ≤ t`. Enforce this with an assertion and a dedicated leakage test (shuffle future events → features must not change).
- Trailing-only normalization (rolling or expanding stats)
- Labels (`y_10s`, …) are computed in a **separate** step and clearly marked as forward-looking. They are never available to the strategy.

### 4.5 Event-driven backtester

Core loop: one priority queue ordered by timestamp.

```
MarketEvent → FeatureUpdate → Strategy.on_event → OrderEvent
           → (latency delay) → ExecutionSim → FillEvent → Portfolio → metrics
```

- **Deterministic:** same inputs + config + seed ⇒ byte-identical results. CI tests this.
- **Latency modeled explicitly:** decision latency plus order-to-exchange latency. An order placed at `t` interacts with the book at `t + latency`.
- Strategy interface: `on_event(state) -> list[Order]`. Research can also supply a precomputed signal file.
- Supports market orders, limit orders, cancels; position limits; flat-by-close option
- Outputs: `trades.parquet`, `pnl.parquet`, `metrics.json`, the frozen `config.yaml`, and the git SHA

### 4.6 Execution simulator

| Level | Model | Use |
|-------|-------|-----|
| 0 | Fill at mid, no cost | **Upper bound only.** Label it clearly, never report it as a result. |
| 1 | Market orders cross the spread at the touch | Baseline realistic |
| 2 | Walk the book for size beyond the top level | Size-dependent slippage |
| 3 | Limit orders with **queue-position** modeling (join the back of the queue; fill when volume ahead is consumed) | Passive strategies |
| 4 | + adverse selection (measure the post-fill mid move) | Honest passive P&L |

### 4.7 Transaction cost model

Pluggable, and every run declares a named scenario:

```
cost = fees(maker/taker, tier) + spread_cost + slippage(size, depth) + impact(size, vol, ADV)
```

- Scenarios: `optimistic`, `realistic`, `pessimistic`, plus a **cost sweep** (0 → N bps) so research can find the break-even cost
- Impact: start with the square-root model *k·σ·√(Q/V)* and document its parameters

### 4.8 Performance

Process:
1. **Measure first:** `cProfile` / `py-spy` / `scalene`; record events/sec for book replay, features, and backtest
2. Find the one or two real hotspots (likely book updates and rolling-window features)
3. Optimize in order: vectorize / polars → NumPy arrays → Numba → **C++ via pybind11 only if still the bottleneck**
4. Write a short `docs/performance.md`: before/after throughput, what was slow, why, and what fixed it

Also: parallelize walk-forward folds and parameter sweeps across processes (they're independent).

Targets (set real ones after Week 1 measurements):

| Stage | Target |
|-------|--------|
| Book replay | ≥ 1M events/sec/core |
| Full backtest, 1 symbol-day | < 30 s |
| Walk-forward sweep (all folds) | Overnight → under 1 hour |

### 4.9 API & dashboard

- **FastAPI:**
  - `POST /runs` (launch backtest from config)
  - `GET /runs/{id}` (status + metrics)
  - `GET /runs/{id}/trades`
  - `GET /compare?ids=…`
  - `GET /features/{symbol}/{date}`
  - `GET /health` (live collector stats)
- **Dashboard** (Streamlit or React):
  - Equity curve, gross vs. net
  - Drawdown
  - Rolling IC / Sharpe
  - Cost-sweep chart
  - Trade scatter on the price chart
  - Run comparison table
  - Live data health panel

---

## 5. 8-Week Plan

| Week | Phase | QE deliverable | Acceptance test |
|------|-------|----------------|-----------------|
| 1 | Data pipeline | Source chosen, collector running, raw Parquet archive, schema doc | 24h capture with gaps detected and resynced |
| 2 | Baseline signals | Book reconstruction + v1 features (QI, spread, mid, vol, momentum) | Golden-snapshot test passes; features match hand calcs |
| 3 | Deep hypothesis | Full feature set incl. OFI and multi-level OFI; labels; multi-symbol batch | Leakage test passes; throughput benchmark recorded |
| 4 | Backtester | Event-driven engine, latency, Level 1 execution | Determinism test; hand-built toy scenario gives known P&L |
| 5 | Costs + slippage | Execution Levels 2–3, cost model, cost-sweep mode | Queue-fill unit tests; cost sweep produces break-even curve |
| 6 | Walk-forward | Parallel fold runner, purge/embargo support, results store | Folds reproduce identically; N× speedup documented |
| 7 | Robustness | Parameter-sweep API, profiling + optimization, `performance.md` | Before/after benchmarks |
| 8 | Paper + dashboard | FastAPI + dashboard, Docker Compose, CI, `make paper` | Fresh clone → `docker compose up` → reproduces paper figures |

---

## 6. Testing Strategy

- **Unit:** each feature, book operations, cost functions, fill rules
- **Golden:** book reconstruction vs. exchange snapshots; recorded backtest outputs pinned by hash
- **Property-based (Hypothesis):** book invariants hold under random delta sequences
- **Leakage:** perturbing future events must not change any feature or decision at `t`
- **Determinism:** two runs give identical outputs
- **Sanity strategies:**
  - Random signal gives ~0 gross P&L and negative net
  - Always-long matches buy-and-hold minus costs
  - A "cheating" strategy using future labels is profitable, which proves the harness would catch leaks if they got in

---

## 7. Stack

| Layer | Tools |
|-------|-------|
| Language | Python 3.11+; C++17 + pybind11 **only** for proven hotspots |
| Data | Parquet, pyarrow, polars, DuckDB |
| Async / streaming | asyncio, websockets/aiohttp; Redis (optional); Kafka (optional, deliberate) |
| Numerics | NumPy, Numba |
| Storage (metadata) | PostgreSQL or SQLite |
| API / UI | FastAPI, Pydantic, Streamlit or React |
| Ops | Docker, Docker Compose, GitHub Actions, `uv`/`poetry`, pre-commit, ruff, mypy |
| Testing | pytest, Hypothesis, pytest-benchmark |

---

## 8. Interface Contract with Research

Both roles share this contract, and it is repeated in `QUANT_RESEARCH.md`. Change it only by agreement, and bump the version.

| Artifact | Producer → Consumer | Format |
|----------|---------------------|--------|
| `features/{symbol}/{date}.parquet` | QE → QR | `ts_ns`, `symbol`, one column per feature in research §3, labels `y_10s`, `y_30s`, `y_60s` |
| `signals/{run_id}.parquet` | QR → QE | `ts_ns`, `symbol`, `signal` (float, target position in [-1, 1]), `model_id` |
| `configs/strategy.yaml` | QR → QE | thresholds, holding period, max position, cost scenario |
| `results/{run_id}/` | QE → QR | `trades.parquet`, `pnl.parquet`, `metrics.json`, `config.yaml`, git SHA |

**Rules:**
- `ts_ns` is the **local receive time** in int64 nanoseconds UTC. The exchange time is stored separately.
- Every result is reproducible from `(git SHA, config, data version)`.

---

## 9. Repository Layout (shared)

```
microprice/
├── ingest/          # QE: collectors, loaders
├── book/            # QE: order-book reconstruction
├── features/        # QE implements, QR specs
├── backtest/        # QE: engine, execution, costs, portfolio
├── api/             # QE: FastAPI
├── dashboard/       # QE
├── research/        # QR: notebooks, studies, trials.csv
├── paper/           # QR (+ QE engineering sections)
├── configs/
├── tests/
├── docs/            # architecture.md, performance.md, data.md
├── docker-compose.yml
└── Makefile         # make data | features | backtest | walkforward | paper
```

---

## 10. Definition of Done

- [ ] Fresh clone + `docker compose up` + `make paper` reproduces every result
- [ ] Book reconstruction golden test: 0 mismatches
- [ ] Leakage, determinism, and sanity-strategy tests pass in CI
- [ ] Execution Levels 1–3 and the cost sweep are implemented and documented
- [ ] `performance.md` shows measured bottleneck, fix, and before/after numbers
- [ ] Dashboard shows gross vs. net, drawdown, cost sweep, and run comparison

### Resume framing

> *Built the data and execution infrastructure for a short-horizon trading research platform. Wrote an async L2 ingestion pipeline with gap recovery, deterministic order-book reconstruction at X M events/sec, and an event-driven backtester with latency, queue-position fills, and pluggable cost models. Profiled and optimized the feature pipeline for a Y× speedup, and delivered a FastAPI/dashboard layer for reproducible experiments.*
