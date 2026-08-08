"""Small serialization, hashing, and path helpers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

import yaml


SKIPPED_DIRECTORIES = {".git", ".reprotrace", "__pycache__"}


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        encoded = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        from .errors import ConfigError

        raise ConfigError(f"cannot serialize strict JSON evidence {path}: {exc}") from exc
    path.write_text(encoded, encoding="utf-8")


def write_bytes_atomic(path: Path, value: bytes) -> None:
    """Atomically replace a file with exact bytes using a sibling temporary file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_json_atomic(path: Path, value: Any) -> None:
    try:
        encoded = (
            json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        from .errors import ConfigError

        raise ConfigError(f"cannot serialize strict JSON evidence {path}: {exc}") from exc
    write_bytes_atomic(path, encoded)


def read_json(path: Path, *, strict: bool = False) -> Any:
    def reject_non_finite(token: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {token}")

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            **({"parse_constant": reject_non_finite} if strict else {}),
        )
    except (OSError, ValueError) as exc:
        from .errors import ConfigError

        raise ConfigError(f"cannot read JSON evidence {path}: {exc}") from exc


def read_source_record(path: Path, *, strict_json: bool = False) -> dict[str, Any]:
    """Read and minimally validate a legacy or schema-1 source record."""

    from .errors import ConfigError

    source = read_json(path, strict=strict_json)
    if not isinstance(source, dict):
        raise ConfigError(f"invalid source.json {path}; root must be an object")

    schema = source.get("schema_version", 0)
    if isinstance(schema, bool) or not isinstance(schema, int) or schema not in (0, 1):
        raise ConfigError(f"unsupported source.json schema_version: {schema!r}")

    available = source.get("available")
    if not isinstance(available, bool):
        raise ConfigError(f"invalid source.json {path}; available must be boolean")
    if "dirty" in source and not isinstance(source["dirty"], bool):
        raise ConfigError(f"invalid source.json {path}; dirty must be boolean")
    if "diff_sha256" in source and source["diff_sha256"] is not None and not isinstance(source["diff_sha256"], str):
        raise ConfigError(f"invalid source.json {path}; diff_sha256 must be a string or null")

    if schema == 0:
        return source

    object_fields = ("summary", "coverage", "git", "git_status", "git_patch")
    for field in object_fields:
        if not isinstance(source.get(field), dict):
            raise ConfigError(f"invalid source.json {path}; {field} must be an object")

    if available:
        for field in ("commit", "worktree_root"):
            if not isinstance(source.get(field), str) or not source[field]:
                raise ConfigError(f"invalid source.json {path}; {field} must be a non-empty string")
        if not isinstance(source.get("dirty"), bool):
            raise ConfigError(f"invalid source.json {path}; dirty must be boolean")

        summary = source["summary"]
        if not isinstance(summary.get("tracked_changes"), bool):
            raise ConfigError(f"invalid source.json {path}; summary.tracked_changes must be boolean")
        untracked_count = summary.get("untracked_file_count")
        if isinstance(untracked_count, bool) or not isinstance(untracked_count, int) or untracked_count < 0:
            raise ConfigError(
                f"invalid source.json {path}; summary.untracked_file_count must be a non-negative integer"
            )

        coverage = source["coverage"]
        if not isinstance(coverage.get("replay"), str) or not coverage["replay"]:
            raise ConfigError(f"invalid source.json {path}; coverage.replay must be a non-empty string")

        git_metadata = source["git"]
        if not isinstance(git_metadata.get("version"), str) or not git_metadata["version"]:
            raise ConfigError(f"invalid source.json {path}; git.version must be a non-empty string")

        for field in ("git_status", "git_patch"):
            metadata = source[field]
            if not isinstance(metadata.get("path"), str) or not metadata["path"]:
                raise ConfigError(f"invalid source.json {path}; {field}.path must be a non-empty string")
            size = metadata.get("size_bytes")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ConfigError(f"invalid source.json {path}; {field}.size_bytes must be a non-negative integer")
            sha256 = metadata.get("sha256")
            if not isinstance(sha256, str) or len(sha256) != 64:
                raise ConfigError(f"invalid source.json {path}; {field}.sha256 must be a SHA-256 hex string")
            try:
                int(sha256, 16)
            except ValueError as exc:
                raise ConfigError(f"invalid source.json {path}; {field}.sha256 must be a SHA-256 hex string") from exc
    else:
        reason = source.get("reason")
        if not isinstance(reason, str) or not reason:
            raise ConfigError(f"invalid source.json {path}; unavailable source requires a non-empty reason")

    return source


def write_yaml(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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
