"""Canonical on-disk locations. Everything under data/ is regenerated from data/raw/."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.environ.get("MICROPRICE_DATA", REPO_ROOT / "data"))

RAW = DATA_ROOT / "raw"
BOOK = DATA_ROOT / "book"
FEATURES = DATA_ROOT / "features"
SIGNALS = DATA_ROOT / "signals"
VENDOR_CACHE = DATA_ROOT / "vendor"
LOGS = DATA_ROOT / "logs"

CONFIGS = REPO_ROOT / "configs"


def raw_dir(venue: str, symbol: str, date: str) -> Path:
    """data/raw/{venue}/{symbol}/{date}/ (immutable, append-only)."""
    return RAW / venue / symbol / date


def book_path(symbol: str, date: str) -> Path:
    return BOOK / symbol / f"{date}.parquet"


def features_path(symbol: str, date: str) -> Path:
    return FEATURES / symbol / f"{date}.parquet"
