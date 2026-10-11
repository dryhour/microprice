"""Live Coinbase L2 + trades collector (Coinbase Advanced Trade WebSocket, no auth needed).

One long-lived "main" connection per symbol subscribes to `level2`, `market_trades` and
`heartbeats`. Every message is appended to the raw archive *before* it is parsed for
anything else.

Integrity:

* **Sequence gaps.** `sequence_num` counts every message on a connection, across channels,
  starting at 0. If a message arrives with anything other than last + 1, the collector writes a
  `gap` control row and resyncs by reconnecting, which delivers a fresh full-depth snapshot.
  The book builder treats everything from the gap to the next snapshot as invalid.
* **Staleness.** Heartbeats arrive every second. No message for `stale_timeout_s` → resync.
* **Reconnect.** Exponential backoff with full jitter on connection errors; immediate
  reconnect for planned resyncs (gap, midnight rollover).
* **Midnight rollover.** The collector resyncs at each UTC midnight so that every
  `data/raw/coinbase_ws/{symbol}/{date}/` partition starts with its own snapshot and can be
  replayed on its own.
* **Audit snapshots.** Every `audit_interval_s` a short-lived second connection captures a
  fresh snapshot (stream = "audit"). The golden test replays the main stream to the snapshot's
  exchange `event_time` and requires an exact match on every price level.

Run:  python -m ingest.coinbase_ws --symbols BTC-USD ETH-USD --duration 600
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import websockets
import yaml
from websockets.exceptions import InvalidHandshake, WebSocketException

from common.paths import CONFIGS, LOGS
from ingest.archive import RawArchiveWriter, utc_date
from ingest.schema import WS_MESSAGE_SCHEMA

VENUE = "coinbase_ws"
SOURCE_VERSION = "coinbase-advanced-trade-ws/v1"
WS_URL = "wss://advanced-trade-ws.coinbase.com"
MAIN_CHANNELS = ("level2", "market_trades", "heartbeats")

log = logging.getLogger("collector")


def parse_ts_ns(ts: str | None) -> int | None:
    """RFC 3339 UTC timestamp with 0-9 fractional digits → int64 ns. None-safe."""
    if not ts:
        return None
    try:
        return int(np.datetime64(ts.rstrip("Z"), "ns").astype(np.int64))
    except ValueError:
        return None


class SequenceTracker:
    """Per-connection `sequence_num` check: the first message has 0, then +1 per message."""

    def __init__(self) -> None:
        self.last: int | None = None

    def check(self, seq: int | None) -> tuple[int, int] | None:
        """Return (expected, got) on a gap, else None. Messages without a seq are ignored."""
        if seq is None:
            return None
        expected = 0 if self.last is None else self.last + 1
        self.last = seq
        if seq != expected:
            return expected, seq
        return None


@dataclass
class Backoff:
    """Exponential backoff with full jitter: delay ~ U(0, min(cap, base * 2**attempt))."""

    base_s: float = 1.0
    cap_s: float = 60.0
    rng: random.Random = field(default_factory=random.Random)
    attempt: int = 0

    def next_delay(self) -> float:
        ceiling = min(self.cap_s, self.base_s * (2**self.attempt))
        self.attempt += 1
        return self.rng.uniform(0, ceiling)

    def reset(self) -> None:
        self.attempt = 0


@dataclass
class Health:
    symbol: str
    msgs: int = 0
    bytes: int = 0
    l2_msgs: int = 0
    trade_msgs: int = 0
    gaps: int = 0
    reconnects: int = 0
    stale: int = 0
    rollovers: int = 0
    audits_ok: int = 0
    audits_failed: int = 0
    lag_ms: deque[float] = field(default_factory=lambda: deque(maxlen=20_000))

    def snapshot_and_reset_lag(self, interval_s: float) -> dict[str, Any]:
        lags = np.fromiter(self.lag_ms, dtype=float)
        self.lag_ms.clear()
        out: dict[str, Any] = {
            k: getattr(self, k)
            for k in (
                "symbol",
                "msgs",
                "bytes",
                "l2_msgs",
                "trade_msgs",
                "gaps",
                "reconnects",
                "stale",
                "rollovers",
                "audits_ok",
                "audits_failed",
            )
        }
        if lags.size:
            out["lag_ms_p50"] = round(float(np.percentile(lags, 50)), 2)
            out["lag_ms_p99"] = round(float(np.percentile(lags, 99)), 2)
        out["interval_s"] = interval_s
        return out


@dataclass
class CollectorConfig:
    symbols: list[str]
    url: str = WS_URL
    flush_interval_s: float = 300.0
    audit_interval_s: float = 900.0
    stale_timeout_s: float = 10.0
    health_interval_s: float = 60.0
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 60.0
    healthy_after_s: float = 60.0
    max_message_bytes: int = 256 * 2**20

    @classmethod
    def load(cls, path: Path) -> CollectorConfig:
        raw = yaml.safe_load(path.read_text())
        return cls(**raw)


class Collector:
    def __init__(self, cfg: CollectorConfig) -> None:
        self.cfg = cfg
        self.writers = {
            s: RawArchiveWriter(VENUE, s, WS_MESSAGE_SCHEMA, SOURCE_VERSION) for s in cfg.symbols
        }
        self.health = {s: Health(s) for s in cfg.symbols}
        self.stopping = asyncio.Event()
        self._conn_counter = 0
        LOGS.mkdir(parents=True, exist_ok=True)
        self.health_log = LOGS / "collector_health.jsonl"

    async def _sleep_unless_stopped(self, seconds: float) -> bool:
        """Sleep up to `seconds`; return True early if the collector is stopping."""
        try:
            await asyncio.wait_for(self.stopping.wait(), timeout=max(seconds, 0.001))
        except TimeoutError:
            return False
        return True

    # --- archive helpers --------------------------------------------------------------
    def _conn_id(self, symbol: str, stream: str) -> str:
        self._conn_counter += 1
        return f"{symbol}:{stream}:{time.time_ns()}:{self._conn_counter}"

    def _append(
        self,
        symbol: str,
        stream: str,
        conn_id: str,
        payload: str,
        recv_ns: int,
        mono_ns: int,
        conn_seq: int | None,
        channel: str,
        exchange_ts_ns: int | None,
    ) -> None:
        self.writers[symbol].append(
            {
                "recv_ts_ns": recv_ns,
                "recv_mono_ns": mono_ns,
                "symbol": symbol,
                "stream": stream,
                "conn_id": conn_id,
                "conn_seq": conn_seq,
                "channel": channel,
                "exchange_ts_ns": exchange_ts_ns,
                "payload": payload,
            }
        )

    def _control(self, symbol: str, stream: str, conn_id: str, event: str, **info: Any) -> None:
        payload = json.dumps({"event": event, **info}, separators=(",", ":"))
        self._append(
            symbol,
            stream,
            conn_id,
            payload,
            time.time_ns(),
            time.monotonic_ns(),
            None,
            "_control",
            None,
        )
        level = logging.WARNING if event in {"gap", "stale", "disconnected"} else logging.INFO
        log.log(level, "%s %s %s %s", symbol, stream, event, info or "")

    # --- main connection ---------------------------------------------------------------
    async def run_main(self, symbol: str) -> None:
        cfg = self.cfg
        backoff = Backoff(cfg.backoff_base_s, cfg.backoff_cap_s)
        health = self.health[symbol]
        first = True
        while not self.stopping.is_set():
            conn_id = self._conn_id(symbol, "main")
            planned = False  # planned resyncs reconnect immediately
            if not first:
                health.reconnects += 1
            first = False
            try:
                async with websockets.connect(
                    cfg.url,
                    max_size=cfg.max_message_bytes,
                    open_timeout=15,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    self._control(symbol, "main", conn_id, "connected", url=cfg.url)
                    for ch in MAIN_CHANNELS:
                        await ws.send(
                            json.dumps(
                                {"type": "subscribe", "product_ids": [symbol], "channel": ch}
                            )
                        )
                    planned = await self._read_main(ws, symbol, conn_id, backoff)
            except (OSError, TimeoutError, WebSocketException, InvalidHandshake) as exc:
                self._control(
                    symbol, "main", conn_id, "disconnected", reason=f"{type(exc).__name__}: {exc}"
                )
            if self.stopping.is_set():
                break
            delay = 0.0 if planned else backoff.next_delay()
            if delay:
                log.info("%s reconnecting in %.1fs", symbol, delay)
            await self._sleep_unless_stopped(delay)

    async def _read_main(self, ws: Any, symbol: str, conn_id: str, backoff: Backoff) -> bool:
        """Read until a resync is needed. Returns True for a planned resync (gap, rollover)."""
        cfg = self.cfg
        health = self.health[symbol]
        tracker = SequenceTracker()
        connected_mono = time.monotonic()
        day = utc_date(time.time_ns())
        stop_task = asyncio.ensure_future(self.stopping.wait())
        try:
            while True:
                recv_task = asyncio.ensure_future(ws.recv())
                done, _ = await asyncio.wait(
                    {recv_task, stop_task},
                    timeout=cfg.stale_timeout_s,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done:
                    recv_task.cancel()
                    self._control(symbol, "main", conn_id, "shutdown")
                    return True
                if not done:
                    recv_task.cancel()
                    health.stale += 1
                    self._control(symbol, "main", conn_id, "stale", timeout_s=cfg.stale_timeout_s)
                    return True
                raw = recv_task.result()  # raises ConnectionClosed on disconnect
                recv_ns = time.time_ns()
                mono_ns = time.monotonic_ns()
                text = raw if isinstance(raw, str) else raw.decode()
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    msg = {}
                seq = msg.get("sequence_num")
                channel = msg.get("channel", "_unknown")
                exch_ns = parse_ts_ns(msg.get("timestamp"))
                # Archive first, then inspect.
                self._append(symbol, "main", conn_id, text, recv_ns, mono_ns, seq, channel, exch_ns)
                health.msgs += 1
                health.bytes += len(text)
                if channel == "l2_data":
                    health.l2_msgs += 1
                elif channel == "market_trades":
                    health.trade_msgs += 1
                if exch_ns is not None:
                    health.lag_ms.append((recv_ns - exch_ns) / 1e6)
                if channel == "error" or msg.get("type") == "error":
                    self._control(symbol, "main", conn_id, "exchange_error", message=text[:500])
                    return False
                gap = tracker.check(seq)
                if gap is not None:
                    health.gaps += 1
                    self._control(symbol, "main", conn_id, "gap", expected=gap[0], got=gap[1])
                    return True
                if time.monotonic() - connected_mono > cfg.healthy_after_s:
                    backoff.reset()
                today = utc_date(recv_ns)
                if today != day:
                    health.rollovers += 1
                    self._control(symbol, "main", conn_id, "rollover", new_date=today)
                    return True
        finally:
            stop_task.cancel()

    # --- audit snapshots ---------------------------------------------------------------
    async def audit_once(self, symbol: str, timeout_s: float = 30.0) -> bool:
        conn_id = self._conn_id(symbol, "audit")
        health = self.health[symbol]
        try:
            async with websockets.connect(
                self.cfg.url, max_size=self.cfg.max_message_bytes, open_timeout=15
            ) as ws:
                await ws.send(
                    json.dumps({"type": "subscribe", "product_ids": [symbol], "channel": "level2"})
                )
                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline:
                    raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
                    recv_ns, mono_ns = time.time_ns(), time.monotonic_ns()
                    text = raw if isinstance(raw, str) else raw.decode()
                    msg = json.loads(text)
                    channel = msg.get("channel", "_unknown")
                    is_snap = channel == "l2_data" and any(
                        ev.get("type") == "snapshot" for ev in msg.get("events", [])
                    )
                    if is_snap:
                        self._append(
                            symbol,
                            "audit",
                            conn_id,
                            text,
                            recv_ns,
                            mono_ns,
                            msg.get("sequence_num"),
                            channel,
                            parse_ts_ns(msg.get("timestamp")),
                        )
                        health.audits_ok += 1
                        return True
        except (OSError, TimeoutError, WebSocketException, InvalidHandshake, ValueError) as exc:
            log.warning("%s audit failed: %s", symbol, exc)
        health.audits_failed += 1
        return False

    async def run_audits(self, symbol: str, offset_s: float) -> None:
        if await self._sleep_unless_stopped(offset_s):
            return
        while not self.stopping.is_set():
            await self.audit_once(symbol)
            await self._sleep_unless_stopped(self.cfg.audit_interval_s)

    # --- flushing and health -------------------------------------------------------------
    async def flush_all(self) -> None:
        for w in self.writers.values():
            table = w.take_buffer()
            if table is not None:
                await asyncio.to_thread(w.write_table, table)

    async def run_flusher(self) -> None:
        while not self.stopping.is_set():
            await self._sleep_unless_stopped(self.cfg.flush_interval_s)
            await self.flush_all()

    async def run_health(self) -> None:
        while not self.stopping.is_set():
            await self._sleep_unless_stopped(self.cfg.health_interval_s)
            now = time.time_ns()
            with self.health_log.open("a") as fh:
                for s, h in self.health.items():
                    rec = {"ts_ns": now, **h.snapshot_and_reset_lag(self.cfg.health_interval_s)}
                    rec["rows_written"] = self.writers[s].rows_written
                    rec["files_written"] = self.writers[s].files_written
                    fh.write(json.dumps(rec) + "\n")
                    log.info("health %s", rec)

    async def run(self, duration_s: float | None = None) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stopping.set)
        n = len(self.cfg.symbols)
        tasks = [asyncio.create_task(self.run_main(s)) for s in self.cfg.symbols]
        tasks += [
            asyncio.create_task(self.run_audits(s, 30 + i * self.cfg.audit_interval_s / max(n, 1)))
            for i, s in enumerate(self.cfg.symbols)
        ]
        tasks += [asyncio.create_task(self.run_flusher()), asyncio.create_task(self.run_health())]
        if duration_s is not None:
            await self._sleep_unless_stopped(duration_s)
            self.stopping.set()
        else:
            await self.stopping.wait()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.flush_all()
        log.info("stopped; rows written %s", {s: w.rows_written for s, w in self.writers.items()})


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config", type=Path, default=CONFIGS / "collector.yaml")
    p.add_argument("--symbols", nargs="+", help="override the symbols in the config")
    p.add_argument("--duration", type=float, help="stop after N seconds (default: run forever)")
    p.add_argument("--flush-interval", type=float)
    p.add_argument("--audit-interval", type=float)
    args = p.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = CollectorConfig.load(args.config)
    if args.symbols:
        cfg.symbols = args.symbols
    if args.flush_interval:
        cfg.flush_interval_s = args.flush_interval
    if args.audit_interval:
        cfg.audit_interval_s = args.audit_interval
    asyncio.run(Collector(cfg).run(args.duration))


if __name__ == "__main__":
    main()
