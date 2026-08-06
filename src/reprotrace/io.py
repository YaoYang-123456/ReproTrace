"""Small serialization, hashing, and path helpers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

import yaml


SKIPPED_DIRECTORIES = {".git", ".reprotrace", "__pycache__"}


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        from .errors import ConfigError

        raise ConfigError(f"cannot read JSON evidence {path}: {exc}") from exc


def write_yaml(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_files(path: Path) -> Iterable[Path]:
    for root, directories, files in os.walk(path):
        directories[:] = sorted(name for name in directories if name not in SKIPPED_DIRECTORIES)
        for name in sorted(files):
            yield Path(root) / name


def fingerprint(path: Path) -> dict[str, Any]:
    """Fingerprint a file or directory without copying it."""

    path = path.resolve()
    if not path.exists():
        return {"path": str(path), "exists": False}
    if path.is_file():
        return {
            "path": str(path),
            "exists": True,
            "kind": "file",
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    if path.is_dir():
        digest = hashlib.sha256()
        total_size = 0
        count = 0
        for child in _directory_files(path):
            relative = child.relative_to(path).as_posix()
            child_hash = sha256_file(child)
            size = child.stat().st_size
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(child_hash.encode("ascii"))
            digest.update(b"\0")
            total_size += size
            count += 1
        return {
            "path": str(path),
            "exists": True,
            "kind": "directory",
            "file_count": count,
            "size_bytes": total_size,
            "sha256": digest.hexdigest(),
        }
    return {"path": str(path), "exists": True, "kind": "other", "sha256": None}


def comparison_key(record: dict[str, Any]) -> tuple[Any, ...]:
    return record.get("exists"), record.get("kind"), record.get("size_bytes"), record.get("sha256")
