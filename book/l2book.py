"""L2 order book: one price-level map per side.

Prices are stored as integers in units of 1/`price_scale` (e.g. scale 100 → cents), so level
keys are exact and never suffer float comparison issues. Sizes stay float64; they are only
copied, never accumulated, so they round-trip exactly.

Invariants (checked by the replay after every complete message, see book/replay.py):
    * no negative sizes                         → InvariantError (data is corrupt)
    * best bid < best ask                        → book flagged invalid while crossed/locked
    * receive time / sequence never goes backward → InvariantError
"""

from __future__ import annotations

import numpy as np
from sortedcontainers import SortedDict


class InvariantError(RuntimeError):
    pass


def infer_price_scale(prices: np.ndarray, max_decimals: int = 10) -> int:
    """Smallest power of ten that makes every price an integer (within float noise)."""
    p = np.unique(prices[np.isfinite(prices)])
    for d in range(max_decimals + 1):
        scaled = p * 10**d
        if np.all(np.abs(scaled - np.round(scaled)) < 1e-6):
            return 10**d
    raise ValueError("prices have more than max_decimals decimals")


def to_ticks(prices: np.ndarray, scale: int) -> np.ndarray:
    return np.round(prices * scale).astype(np.int64)


class L2Book:
    __slots__ = ("asks", "bids", "scale")

    def __init__(self, scale: int) -> None:
        self.scale = scale
        self.bids: SortedDict = SortedDict()  # price_int -> size
        self.asks: SortedDict = SortedDict()

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()

    def set_level(self, is_bid: bool, price: int, size: float) -> None:
        side = self.bids if is_bid else self.asks
        if size > 0.0:
            side[price] = size
        elif size == 0.0:
            side.pop(price, None)
        else:
            raise InvariantError(f"negative size {size} at {'bid' if is_bid else 'ask'} {price}")

    def best_bid(self) -> tuple[int, float] | None:
        return self.bids.peekitem(-1) if self.bids else None

    def best_ask(self) -> tuple[int, float] | None:
        return self.asks.peekitem(0) if self.asks else None

    def is_crossed_or_locked(self) -> bool:
        if not self.bids or not self.asks:
            return False
        return bool(self.bids.peekitem(-1)[0] >= self.asks.peekitem(0)[0])

    def is_two_sided(self) -> bool:
        return bool(self.bids) and bool(self.asks)

    def top(self, k: int) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
        """Best k levels per side: bids descending, asks ascending."""
        nb, na = min(k, len(self.bids)), min(k, len(self.asks))
        bids = [self.bids.peekitem(-1 - i) for i in range(nb)]
        asks = [self.asks.peekitem(i) for i in range(na)]
        return bids, asks

    def levels(self) -> tuple[dict[int, float], dict[int, float]]:
        return dict(self.bids), dict(self.asks)

    def __repr__(self) -> str:
        bb, ba = self.best_bid(), self.best_ask()
        return f"L2Book(bid={bb}, ask={ba}, levels={len(self.bids)}/{len(self.asks)})"
