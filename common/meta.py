"""Version metadata stamped into every derived Parquet file.

Every derived dataset records `code_git_sha`, `source_version` and `created_at`, so any
result can be traced back to (git SHA, config, data version).
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from functools import cache

import pyarrow as pa
import pyarrow.parquet as pq

from common.paths import REPO_ROOT


@cache
def git_sha() -> str:
    """HEAD SHA, with a `-dirty` suffix when tracked files have uncommitted changes."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{sha}-dirty" if dirty else sha


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def version_metadata(source_version: str, **extra: str) -> dict[str, str]:
    meta = {
        "code_git_sha": git_sha(),
        "source_version": source_version,
        "created_at": utc_now_iso(),
    }
    meta.update(extra)
    return meta


def with_metadata(table: pa.Table, meta: dict[str, str]) -> pa.Table:
    existing = dict(table.schema.metadata or {})
    existing.update({k.encode(): v.encode() for k, v in meta.items()})
    return table.replace_schema_metadata(existing)


def read_metadata(path: str) -> dict[str, str]:
    raw = pq.read_schema(path).metadata or {}
    return {
        k.decode(): v.decode()
        for k, v in raw.items()
        if not k.startswith(b"ARROW:") and k != b"pandas"
    }
