"""Append rows to research/trials.csv (see research/trials.md). Append-only, IDs never reused."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path

from common.meta import git_sha, utc_now_iso

TRIALS = Path(__file__).resolve().parents[1] / "trials.csv"


def columns() -> list[str]:
    with TRIALS.open() as fh:
        return next(csv.reader(fh))


def _next_id() -> int:
    with TRIALS.open() as fh:
        rows = list(csv.DictReader(fh))
    return 1 + max((int(r["trial_id"][1:]) for r in rows), default=0)


def append_trials(rows: Iterable[dict]) -> list[str]:
    """Append rows; fills trial_id, run_at_utc and git_sha. Returns the new trial ids."""
    cols = columns()
    n = _next_id()
    now, sha = utc_now_iso(), git_sha()
    ids = []
    with TRIALS.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="raise")
        for r in rows:
            r = dict(r)
            if isinstance(r.get("params"), dict):
                r["params"] = json.dumps(r["params"], sort_keys=True)
            tid = f"T{n:04d}"
            n += 1
            w.writerow(
                {**{c: "" for c in cols}, **r, "trial_id": tid, "run_at_utc": now, "git_sha": sha}
            )
            ids.append(tid)
    return ids
