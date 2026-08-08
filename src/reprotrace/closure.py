"""Authoritative dependency closure for schema-1 evidence bundles."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .errors import ConfigError
from .evidence import normalize_bundle_path


CORE_EVIDENCE_ROLES: dict[str, tuple[str, ...]] = {
    "run.json": ("record",),
    "manifest.resolved.yaml": ("record", "resolved_manifest"),
    "source.json": ("record",),
    "environment.json": ("record",),
    "inputs.json": ("record",),
    "commands.json": ("command_record", "record"),
    "commands.jsonl": ("command_record", "record"),
    "artifacts.json": ("record",),
    "metric_sources.json": ("record",),
    "metrics.json": ("record",),
}


def evidence_dependency_declarations(
    *,
    run: Mapping[str, Any],
    source: Mapping[str, Any],
    inputs: Sequence[Mapping[str, Any]],
    commands: Sequence[Mapping[str, Any]],
    artifacts: Sequence[Mapping[str, Any]],
    metric_sources: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return the deterministic, referenced verifier dependency closure.

    Duplicate references are represented by one path with the union of roles.
    Unreferenced files in the bundle are intentionally excluded.
    """

    roles_by_path: dict[str, set[str]] = {}

    def add(path: Any, role: str, *, label: str) -> None:
        normalized = normalize_bundle_path(path, label=label)
        roles_by_path.setdefault(normalized, set()).add(role)

    for path, roles in CORE_EVIDENCE_ROLES.items():
        for role in roles:
            add(path, role, label="core evidence")

    if source.get("schema_version", 0) == 1 and source.get("available") is True:
        for key, label in (
            ("git_status", "Git status evidence"),
            ("git_patch", "Git patch evidence"),
        ):
            metadata = source.get(key)
            if not isinstance(metadata, Mapping):
                raise ConfigError(f"invalid source.json; {key} metadata must be an object")
            add(metadata.get("path"), "source_evidence", label=label)

    for index, item in enumerate(inputs):
        if item.get("path_scope") == "bundle":
            add(
                item.get("evidence_path"),
                "input",
                label=f"input {index} evidence",
            )

    if run.get("dry_run") is not True:
        for index, command in enumerate(commands):
            add(
                command.get("stdout_evidence_path"),
                "command_log",
                label=f"command {index} stdout evidence",
            )
            add(
                command.get("stderr_evidence_path"),
                "command_log",
                label=f"command {index} stderr evidence",
            )

    for declaration_index, declaration in enumerate(artifacts):
        matches = declaration.get("matches", [])
        if not isinstance(matches, list):
            raise ConfigError(
                f"invalid artifacts.json declaration {declaration_index}; matches must be an array"
            )
        for match_index, match in enumerate(matches):
            if not isinstance(match, Mapping):
                raise ConfigError(
                    f"invalid artifacts.json declaration {declaration_index} match {match_index}"
                )
            if match.get("path_scope") == "bundle":
                add(
                    match.get("evidence_path"),
                    "artifact",
                    label=f"artifact {declaration_index} match {match_index} evidence",
                )

    if run.get("dry_run") is not True:
        metric_records = metric_sources.get("metrics", [])
        if not isinstance(metric_records, list):
            raise ConfigError("invalid metric_sources.json; metrics must be an array")
        for metric_index, metric in enumerate(metric_records):
            if not isinstance(metric, Mapping):
                raise ConfigError(f"invalid metric_sources.json metric {metric_index}")
            sources = metric.get("sources", [])
            if not isinstance(sources, list):
                raise ConfigError(
                    f"invalid metric_sources.json metric {metric_index}; sources must be an array"
                )
            for source_index, metric_source in enumerate(sources):
                if not isinstance(metric_source, Mapping):
                    raise ConfigError(
                        f"invalid metric_sources.json metric {metric_index} source {source_index}"
                    )
                add(
                    metric_source.get("evidence_path"),
                    "metric_source",
                    label=f"metric {metric_index} source {source_index} evidence",
                )

    return [
        {"path": path, "roles": sorted(roles)}
        for path, roles in sorted(roles_by_path.items())
    ]
