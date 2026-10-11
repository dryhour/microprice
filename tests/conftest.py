from __future__ import annotations

from pathlib import Path

import pytest

from common.paths import DATA_ROOT

FIXTURES = Path(__file__).parent / "fixtures"


def has_raw(venue: str, symbol: str, date: str) -> bool:
    return (DATA_ROOT / "raw" / venue / symbol / date).exists()


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    """Point every data/ path at a temp dir."""
    import common.paths as paths

    root = tmp_path / "data"
    for name, sub in [
        ("DATA_ROOT", ""),
        ("RAW", "raw"),
        ("BOOK", "book"),
        ("FEATURES", "features"),
        ("VENDOR_CACHE", "vendor"),
        ("LOGS", "logs"),
    ]:
        monkeypatch.setattr(paths, name, root / sub if sub else root)
    import ingest.coinbase_ws as cw

    monkeypatch.setattr(cw, "LOGS", root / "logs")
    return root
