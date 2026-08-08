"""Bundle-safe evidence paths and canonical evidence index primitives."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .errors import ConfigError
from .io import sha256_bytes, sha256_file, write_bytes_atomic


EVIDENCE_INDEX_FILENAME = "evidence.index.json"
EVIDENCE_INDEX_SCHEMA_VERSION = 1
EVIDENCE_INDEX_EXCLUDED_PATHS = frozenset(
    {EVIDENCE_INDEX_FILENAME, "verification.json", "report.md"}
)


def normalize_bundle_path(value: Any, *, label: str = "evidence") -> str:
    """Return a canonical POSIX-style bundle-relative path."""

    if not isinstance(value, str) or not value:
        raise ConfigError(f"invalid {label} path; expected a non-empty relative path")
    if "\x00" in value:
        raise ConfigError(f"invalid {label} path; NUL bytes are not allowed")

    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or windows_path.root
    ):
        raise ConfigError(f"invalid {label} path; absolute paths are not allowed: {value!r}")
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise ConfigError(f"invalid {label} path; parent traversal is not allowed: {value!r}")
    if "\\" in value:
        raise ConfigError(f"invalid {label} path; use POSIX '/' separators: {value!r}")

    normalized = posix_path.as_posix()
    if normalized in ("", "."):
        raise ConfigError(f"invalid {label} path; expected a file path")
    return normalized


def _resolved_bundle_root(bundle_root: str | Path) -> Path:
    try:
        root = Path(bundle_root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ConfigError(f"cannot resolve evidence bundle root {bundle_root}: {exc}") from exc
    if not root.is_dir():
        raise ConfigError(f"invalid evidence bundle root; expected a directory: {root}")
    return root


def resolve_bundle_file(
    bundle_root: str | Path,
    relative_path: Any,
    *,
    label: str = "evidence",
    allow_missing: bool = False,
) -> Path:
    """Resolve a regular bundle-local file without following an escaping path."""

    normalized = normalize_bundle_path(relative_path, label=label)
    root = _resolved_bundle_root(bundle_root)
    candidate = root.joinpath(*PurePosixPath(normalized).parts)
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError(
            f"invalid {label} path; path escapes the evidence bundle: {relative_path!r}"
        ) from exc

    try:
        file_status = os.stat(candidate, follow_symlinks=False)
    except FileNotFoundError as exc:
        if allow_missing:
            return candidate
        raise ConfigError(f"invalid {label} file; file does not exist: {candidate}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot inspect {label} file {candidate}: {exc}") from exc

    if stat.S_ISLNK(file_status.st_mode) or candidate.is_symlink():
        raise ConfigError(f"invalid {label} file; symlinks are not allowed: {candidate}")
    if not stat.S_ISREG(file_status.st_mode):
        raise ConfigError(f"invalid {label} file; expected a regular file: {candidate}")
    return candidate


def _normalize_roles(value: Any, *, entry_index: int) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"invalid evidence index entry {entry_index}; roles must be a non-empty array")
    if not all(isinstance(role, str) and role and "\x00" not in role for role in value):
        raise ConfigError(
            f"invalid evidence index entry {entry_index}; roles must contain non-empty strings"
        )
    return sorted(set(value))


def _validate_sha256(value: Any, *, entry_index: int) -> str:
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        raise ConfigError(
            f"invalid evidence index entry {entry_index}; sha256 must be lowercase SHA-256 hex"
        )
    try:
        int(value, 16)
    except ValueError as exc:
        raise ConfigError(
            f"invalid evidence index entry {entry_index}; sha256 must be lowercase SHA-256 hex"
        ) from exc
    return value


def _normalized_evidence_index(index: Any) -> dict[str, Any]:
    if not isinstance(index, dict):
        raise ConfigError("invalid evidence.index.json; root must be an object")
    if set(index) != {"schema_version", "entries"}:
        raise ConfigError(
            "invalid evidence.index.json; expected only schema_version and entries fields"
        )
    schema = index.get("schema_version")
    if (
        isinstance(schema, bool)
        or not isinstance(schema, int)
        or schema != EVIDENCE_INDEX_SCHEMA_VERSION
    ):
        raise ConfigError(f"unsupported evidence.index.json schema_version: {schema!r}")
    entries = index.get("entries")
    if not isinstance(entries, list):
        raise ConfigError("invalid evidence.index.json; entries must be an array")

    normalized_entries: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for entry_index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"invalid evidence index entry {entry_index}; expected an object")
        if set(entry) != {"path", "roles", "size_bytes", "sha256"}:
            raise ConfigError(
                f"invalid evidence index entry {entry_index}; unexpected or missing fields"
            )
        path = normalize_bundle_path(entry.get("path"), label="evidence index entry")
        if path in EVIDENCE_INDEX_EXCLUDED_PATHS:
            raise ConfigError(f"evidence index must not include self-derived file: {path}")
        if path in seen_paths:
            raise ConfigError(f"duplicate normalized evidence path: {path}")
        seen_paths.add(path)

        size = entry.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ConfigError(
                f"invalid evidence index entry {entry_index}; size_bytes must be a non-negative integer"
            )
        normalized_entries.append(
            {
                "path": path,
                "roles": _normalize_roles(entry.get("roles"), entry_index=entry_index),
                "size_bytes": size,
                "sha256": _validate_sha256(entry.get("sha256"), entry_index=entry_index),
            }
        )

    normalized_entries.sort(key=lambda entry: entry["path"])
    return {
        "schema_version": EVIDENCE_INDEX_SCHEMA_VERSION,
        "entries": normalized_entries,
    }


def canonical_evidence_index_bytes(index: Any) -> bytes:
    """Serialize a validated index to its canonical UTF-8 byte representation."""

    normalized = _normalized_evidence_index(index)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def evidence_root_sha256(index: Any) -> str:
    """Return the evidence snapshot identifier for canonical index bytes."""

    return sha256_bytes(canonical_evidence_index_bytes(index))


def build_evidence_index(
    bundle_root: str | Path,
    declarations: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Fingerprint declared bundle files and return a canonical index record."""

    root = _resolved_bundle_root(bundle_root)
    entries: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for entry_index, declaration in enumerate(declarations):
        if not isinstance(declaration, Mapping):
            raise ConfigError(f"invalid evidence declaration {entry_index}; expected an object")
        if set(declaration) != {"path", "roles"}:
            raise ConfigError(
                f"invalid evidence declaration {entry_index}; expected only path and roles fields"
            )
        path = normalize_bundle_path(declaration.get("path"), label="evidence declaration")
        if path in EVIDENCE_INDEX_EXCLUDED_PATHS:
            raise ConfigError(f"evidence index must not include self-derived file: {path}")
        if path in seen_paths:
            raise ConfigError(f"duplicate normalized evidence path: {path}")
        seen_paths.add(path)
        roles = _normalize_roles(declaration.get("roles"), entry_index=entry_index)
        candidate = resolve_bundle_file(root, path, label="indexed evidence")
        try:
            file_status = os.stat(candidate, follow_symlinks=False)
        except OSError as exc:
            raise ConfigError(f"cannot inspect indexed evidence file {candidate}: {exc}") from exc
        entries.append(
            {
                "path": path,
                "roles": roles,
                "size_bytes": file_status.st_size,
                "sha256": sha256_file(candidate),
            }
        )
    return _normalized_evidence_index(
        {"schema_version": EVIDENCE_INDEX_SCHEMA_VERSION, "entries": entries}
    )


def write_evidence_index(
    bundle_root: str | Path,
    declarations: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Build and atomically write the exact canonical index bytes."""

    root = _resolved_bundle_root(bundle_root)
    index = build_evidence_index(root, declarations)
    encoded = canonical_evidence_index_bytes(index)
    write_bytes_atomic(root / EVIDENCE_INDEX_FILENAME, encoded)
    return index, sha256_bytes(encoded)


def read_evidence_index(bundle_root: str | Path) -> dict[str, Any]:
    """Read an index and reject non-canonical or invalid serialization."""

    path = resolve_bundle_file(
        bundle_root,
        EVIDENCE_INDEX_FILENAME,
        label="evidence index",
    )
    try:
        encoded = path.read_bytes()
        parsed = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read evidence index {path}: {exc}") from exc
    normalized = _normalized_evidence_index(parsed)
    if encoded != canonical_evidence_index_bytes(normalized):
        raise ConfigError("invalid evidence.index.json; serialization is not canonical UTF-8 JSON")
    return normalized


def validate_evidence_index(
    bundle_root: str | Path,
    index: Any | None = None,
) -> dict[str, Any]:
    """Validate every indexed file without claiming producer authenticity."""

    normalized = read_evidence_index(bundle_root) if index is None else _normalized_evidence_index(index)
    checks: list[dict[str, Any]] = []
    for entry in normalized["entries"]:
        candidate = resolve_bundle_file(
            bundle_root,
            entry["path"],
            label="indexed evidence",
            allow_missing=True,
        )
        try:
            file_status = os.stat(candidate, follow_symlinks=False)
        except FileNotFoundError:
            checks.append(
                {
                    "path": entry["path"],
                    "roles": entry["roles"],
                    "passed": False,
                    "recorded": {
                        "size_bytes": entry["size_bytes"],
                        "sha256": entry["sha256"],
                    },
                    "current": {"exists": False, "size_bytes": None, "sha256": None},
                    "reason": "indexed evidence file is missing",
                }
            )
            continue
        except OSError as exc:
            raise ConfigError(f"cannot inspect indexed evidence file {candidate}: {exc}") from exc

        current = {
            "exists": True,
            "size_bytes": file_status.st_size,
            "sha256": sha256_file(candidate),
        }
        recorded = {"size_bytes": entry["size_bytes"], "sha256": entry["sha256"]}
        checks.append(
            {
                "path": entry["path"],
                "roles": entry["roles"],
                "passed": recorded
                == {"size_bytes": current["size_bytes"], "sha256": current["sha256"]},
                "recorded": recorded,
                "current": current,
            }
        )

    return {
        "schema_version": EVIDENCE_INDEX_SCHEMA_VERSION,
        "valid": all(check["passed"] for check in checks),
        "evidence_root_sha256": evidence_root_sha256(normalized),
        "checks": checks,
    }
