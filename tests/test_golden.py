"""Golden-snapshot tests: book reconstruction must match independent references exactly.

The fixture tests run everywhere (CI included). The `data` tests run the same checks on full
days when data/ is present and are what the Week 2 gate is judged on.
"""

from __future__ import annotations

import pytest

from book import golden
from ingest import readers
from tests.conftest import FIXTURES, has_raw

RAW = FIXTURES / "raw"


def test_live_fixture_matches_exchange_audit_snapshot():
    rep = golden.golden_live("HBAR-USD", "2026-10-11", base=RAW)
    assert rep.references == 1 and rep.compared == 1
    assert rep.levels_compared > 1000
    assert rep.mismatched_levels == 0, rep.examples
    assert rep.passed


def test_tardis_fixture_matches_vendor_top25_snapshots():
    rep = golden.golden_tardis(
        "HBAR-USD",
        "2025-03-01",
        base=RAW,
        snapshot_csv=RAW
        / "coinbase_tardis"
        / "HBAR-USD"
        / "2025-03-01"
        / "book_snapshot_25.csv.gz",
    )
    assert rep.compared > 10_000
    assert rep.mismatched_levels == 0, rep.examples
    assert rep.passed


def test_golden_detects_a_corrupted_book(monkeypatch):
    """Negative control: one wrong size that survives to the audit must fail the test."""
    real = readers.read_ws

    def corrupted(*a, **k):
        ev, tr, audits = real(*a, **k)
        touched = set(ev.price[ev.kind == readers.UPDATE].tolist())
        snap = (ev.kind == readers.SNAPSHOT).nonzero()[0]
        i = next(j for j in snap if ev.price[j] not in touched)  # never updated afterwards
        ev.size[i] += 1.0
        return ev, tr, audits

    monkeypatch.setattr(golden, "read_ws", corrupted)
    rep = golden.golden_live("HBAR-USD", "2026-10-11", base=RAW)
    assert rep.compared == 1 and not rep.passed


@pytest.mark.data
@pytest.mark.parametrize(
    "symbol", ["BTC-USD", "ETH-USD", "ZEC-USD", "QNT-USD", "HBAR-USD", "APT-USD"]
)
def test_live_full_day(symbol):
    import common.paths as paths

    days = sorted(p.name for p in (paths.RAW / "coinbase_ws" / symbol).glob("20*"))
    if not days:
        pytest.skip("no live capture")
    for d in days:
        rep = golden.golden_live(symbol, d)
        if rep.references == 0:
            continue
        assert rep.mismatched_levels == 0, (d, rep.examples)


@pytest.mark.data
@pytest.mark.network
def test_tardis_full_day_hbar():
    import requests

    if not has_raw("coinbase_tardis", "HBAR-USD", "2025-03-01"):
        pytest.skip("no Tardis data")
    try:
        rep = golden.golden_tardis("HBAR-USD", "2025-03-01", max_rows=200_000)
    except requests.HTTPError as exc:  # e.g. the free tier's daily transfer quota (429)
        pytest.skip(f"Tardis reference snapshots unavailable: {exc}")
    assert rep.compared > 100_000 and rep.mismatched_levels == 0, rep.examples
