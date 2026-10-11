"""Raw archive schemas. See docs/data.md for the field-by-field description.

Raw data is written exactly as received, before any processing, and is never modified.
Two raw formats exist because the two sources deliver different things:

* `coinbase_ws`: our own live collector. One row per WebSocket message, payload kept verbatim.
* `coinbase_tardis`: historical Coinbase Exchange feed from Tardis.dev. The vendor already
  splits messages into one row per level change, so we keep their rows, only renaming
  columns and converting microseconds to int64 nanoseconds.
"""

from __future__ import annotations

import pyarrow as pa

RAW_SCHEMA_VERSION = "1"

# --- live collector (venue key "coinbase_ws") ----------------------------------------------
# `stream` is "main" for the long-lived connection the book is built from, and "audit" for the
# short-lived connections opened only to capture an exchange snapshot for the golden test.
# `channel` is the Coinbase channel name, or "_control" for rows the collector writes itself
# (connect, disconnect, sequence gap, shutdown). Control payloads are JSON objects with an
# "event" key.
WS_MESSAGE_SCHEMA = pa.schema(
    [
        pa.field("recv_ts_ns", pa.int64(), nullable=False),
        pa.field("recv_mono_ns", pa.int64(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("stream", pa.string(), nullable=False),
        pa.field("conn_id", pa.string(), nullable=False),
        pa.field("conn_seq", pa.int64()),
        pa.field("channel", pa.string(), nullable=False),
        pa.field("exchange_ts_ns", pa.int64()),
        pa.field("payload", pa.string(), nullable=False),
    ]
)

# --- historical vendor rows (venue key "coinbase_tardis") ----------------------------------
# One row per level change; `is_snapshot` rows replace the book (see book/replay.py).
TARDIS_BOOK_SCHEMA = pa.schema(
    [
        pa.field("exchange_ts_ns", pa.int64(), nullable=False),
        pa.field("recv_ts_ns", pa.int64(), nullable=False),
        pa.field("is_snapshot", pa.bool_(), nullable=False),
        pa.field("side", pa.string(), nullable=False),  # "bid" | "ask"
        pa.field("price", pa.float64(), nullable=False),
        pa.field("size", pa.float64(), nullable=False),  # 0 deletes the level
    ]
)

# Tardis normalizes `side` to the aggressor (taker) side: "buy" means the buyer took liquidity.
TARDIS_TRADES_SCHEMA = pa.schema(
    [
        pa.field("exchange_ts_ns", pa.int64(), nullable=False),
        pa.field("recv_ts_ns", pa.int64(), nullable=False),
        pa.field("trade_id", pa.string(), nullable=False),
        pa.field("side", pa.string(), nullable=False),  # aggressor side: "buy" | "sell"
        pa.field("price", pa.float64(), nullable=False),
        pa.field("size", pa.float64(), nullable=False),
    ]
)
