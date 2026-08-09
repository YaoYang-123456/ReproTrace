"""Shared deterministic protocol helpers for schema-1 evidence."""

from __future__ import annotations

import fnmatch
import ntpath
import posixpath
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any

from .errors import ConfigError
from .evidence import normalize_bundle_path


COMMAND_STATUSES = frozenset(
    {"planned", "completed", "failed", "timeout", "launch_error"}
)
FAILED_COMMAND_STATUSES = frozenset({"failed", "timeout", "launch_error"})
PROTOCOL_METADATA_KEY = "_reprotrace"
PROTOCOL_SCHEMA_VERSION = 1


def command_log_evidence_path(step_id: str, stream: str) -> str:
    if stream not in {"stdout", "stderr"}:
        raise ValueError(f"unsupported command stream: {stream}")
    return normalize_bundle_path(
        f"logs/{step_id}.{stream}.log",
        label=f"command {stream} evidence",
    )


def join_protocol_path(root: str, child: str) -> str:
    """Join producer path strings without consulting the verifier filesystem."""

    has_windows_drive = (
        len(root) >= 2 and root[0].isalpha() and root[1] == ":"
    )
    uses_windows_separators = root.startswith("\\")
    module = ntpath if has_windows_drive or uses_windows_separators else posixpath
    return module.normpath(module.join(root, child))


def bundle_artifact_pattern(declared_path: Any) -> str | None:
    """Return a canonical bundle-relative pattern for a run-dir declaration."""

    if not isinstance(declared_path, str):
        return None
    suffix: str | None = None
    for prefix in ("{run_dir}/", "{run_dir}\\"):
        if declared_path.startswith(prefix):
            suffix = declared_path[len(prefix) :].replace("\\", "/")
            break
    if suffix is None:
        return None
    return normalize_bundle_path(suffix, label="bundle artifact declaration")


def bundle_artifact_path_matches(pattern: str, evidence_path: Any) -> bool:
    """Match canonical POSIX bundle paths using segment-aware glob semantics.

    ``*``, ``?``, and bracket expressions match within one path segment. A
    complete ``**`` segment matches zero or more segments.
    """

    normalized_pattern = normalize_bundle_path(
        pattern, label="bundle artifact declaration"
    )
    normalized_path = normalize_bundle_path(
        evidence_path, label="bundle artifact evidence"
    )
    pattern_parts = PurePosixPath(normalized_pattern).parts
    path_parts = PurePosixPath(normalized_path).parts

    @lru_cache(maxsize=None)
    def matches(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        segment = pattern_parts[pattern_index]
        if segment == "**":
            return matches(pattern_index + 1, path_index) or (
                path_index < len(path_parts)
                and matches(pattern_index, path_index + 1)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], segment)
            and matches(pattern_index + 1, path_index + 1)
        )

    return matches(0, 0)


def require_protocol_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("invalid manifest.resolved.yaml; _reprotrace must be an object")
    if set(value) != {
        "schema_version",
        "command_authority",
        "command_archive",
        "commands",
        "runtime_context",
    }:
        raise ConfigError(
            "invalid manifest.resolved.yaml; unexpected or missing _reprotrace fields"
        )
    schema = value.get("schema_version")
    if (
        isinstance(schema, bool)
        or not isinstance(schema, int)
        or schema != PROTOCOL_SCHEMA_VERSION
    ):
        raise ConfigError(
            f"unsupported manifest command protocol schema_version: {schema!r}"
        )
    if value.get("command_authority") != "commands.json":
        raise ConfigError(
            "invalid manifest.resolved.yaml; command authority must be commands.json"
        )
    if value.get("command_archive") != "commands.jsonl":
        raise ConfigError(
            "invalid manifest.resolved.yaml; command archive must be commands.jsonl"
        )
    commands = value.get("commands")
    if not isinstance(commands, list) or not all(
        isinstance(command, dict) for command in commands
    ):
        raise ConfigError(
            "invalid manifest.resolved.yaml; _reprotrace.commands must be an array of objects"
        )
    runtime_context = value.get("runtime_context")
    if not isinstance(runtime_context, dict) or set(runtime_context) != {
        "python",
        "project_root",
        "run_dir",
        "seed",
    }:
        raise ConfigError(
            "invalid manifest.resolved.yaml; _reprotrace.runtime_context is invalid"
        )
    return value
