"""The frozen research universe (configs/universe.yaml) and the holdout guard."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

import yaml

from common.paths import CONFIGS


class HoldoutError(RuntimeError):
    """Raised when research code touches a holdout date without explicitly unsealing it."""


@dataclass(frozen=True)
class Universe:
    version: int
    venue: str
    source: str
    symbols: tuple[str, ...]
    in_sample: tuple[str, ...]
    holdout: tuple[str, ...]

    @property
    def all_dates(self) -> tuple[str, ...]:
        return tuple(sorted(self.in_sample + self.holdout))

    def research_dates(self, *, unseal_holdout: bool = False) -> tuple[str, ...]:
        """Dates research may use. The holdout is excluded unless explicitly unsealed."""
        return self.all_dates if unseal_holdout else self.in_sample

    def check(self, date: str, *, unseal_holdout: bool = False) -> None:
        if date in self.holdout and not unseal_holdout:
            raise HoldoutError(
                f"{date} is a holdout date. It is evaluated exactly once, in Week 7 "
                "(research/universe.md). Pass unseal_holdout=True only for that run."
            )


@cache
def load_universe() -> Universe:
    raw = yaml.safe_load((CONFIGS / "universe.yaml").read_text())
    return Universe(
        version=int(raw["version"]),
        venue=raw["venue"],
        source=raw["source"],
        symbols=tuple(raw["symbols"]),
        in_sample=tuple(raw["dates"]["in_sample"]),
        holdout=tuple(raw["dates"]["holdout"]),
    )
