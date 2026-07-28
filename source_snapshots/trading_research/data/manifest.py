"""Content-addressed manifests for research datasets."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from data.schema import DatasetContract, parse_utc, resolve_project_path


MANIFEST_VERSION = 1


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_path_for(data_path: str | Path) -> Path:
    path = Path(data_path)
    return path.with_name(f"{path.name}.manifest.json")


def build_manifest(
    data_path: str | Path,
    frame: pd.DataFrame,
    contract: DatasetContract,
    *,
    retrieved_at: str | datetime | pd.Timestamp | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = resolve_project_path(data_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    retrieved = parse_utc(retrieved_at or datetime.now(timezone.utc), field_name="retrieved_at")
    start = frame["timestamp"].iloc[0]
    end_exclusive = frame["timestamp"].iloc[-1] + pd.Timedelta(contract.interval)
    try:
        dataset_path = str(path.relative_to(resolve_project_path("."))).replace("\\", "/")
    except ValueError:
        dataset_path = str(path)
    return {
        "manifest_version": MANIFEST_VERSION,
        "dataset_path": dataset_path,
        "symbol": contract.symbol,
        "timeframe": contract.timeframe,
        "source": contract.source,
        "venue": contract.venue,
        "timezone": "UTC",
        "data_kind": contract.data_kind,
        "start": start.isoformat(),
        "end_exclusive": end_exclusive.isoformat(),
        "row_count": int(len(frame)),
        "content_sha256": sha256_file(path),
        "retrieved_at": retrieved.isoformat(),
        "contract": {
            "required_columns": ["timestamp", "open", "high", "low", "close"],
            "interval_seconds": int(contract.interval.total_seconds()),
            "gap_policy": "reject non-closure gaps; permit configured broker closures",
            "known_closure_dates": list(contract.known_closure_dates),
        },
        "metadata": extra_metadata or {},
    }


def write_manifest(manifest: dict[str, Any], data_path: str | Path) -> Path:
    target = manifest_path_for(resolve_project_path(data_path))
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def load_manifest(data_path: str | Path) -> dict[str, Any] | None:
    target = manifest_path_for(resolve_project_path(data_path))
    if not target.is_file():
        return None
    return json.loads(target.read_text(encoding="utf-8"))


def verify_manifest(data_path: str | Path, manifest: dict[str, Any]) -> None:
    actual_hash = sha256_file(resolve_project_path(data_path))
    if manifest.get("content_sha256") != actual_hash:
        raise ValueError("Dataset content hash does not match its manifest")
