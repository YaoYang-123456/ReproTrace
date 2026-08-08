"""Verification of legacy records and schema-1 bundle-local evidence."""

from __future__ import annotations

import math
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from .assurance import (
    DEPRECATED_VERIFICATION_FIELDS,
    NOT_ESTABLISHED,
    AssuranceLevel,
    ResultStatus,
    VerificationStatus,
    coverage_skeleton,
    highest_assurance_level,
    recorded_execution_status,
)
from .closure import evidence_dependency_declarations
from .evidence import read_evidence_index, resolve_bundle_file, validate_evidence_index
from .errors import ConfigError
from .io import read_json, read_source_record, sha256_file, utc_now, write_json_atomic
from .manifest import validate_manifest
from .metrics import extract_metric_from_evidence, validate_metric_sources_record


def _require_record_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"invalid {name}; root must be an object")
    return value


def _require_record_list(value: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ConfigError(f"invalid {name}; root must be an array of objects")
    return value


def _read_resolved_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read resolved manifest evidence {path}: {exc}") from exc
    manifest = _require_record_object(manifest, "manifest.resolved.yaml")
    validate_manifest(manifest)
    return manifest


def _source_file_check(
    directory: Path,
    metadata: Any,
    check_id: str,
    label: str,
    *,
    index_entries: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        raise ConfigError(f"invalid source.json; {label} metadata must be an object")
    candidate = resolve_bundle_file(
        directory,
        metadata.get("path"),
        label=label,
        allow_missing=True,
    )
    recorded = {key: metadata.get(key) for key in ("size_bytes", "sha256")}
    if not candidate.exists():
        return {
            "id": check_id,
            "kind": "integrity",
            "category": "source",
            "canonical": True,
            "required": True,
            "passed": False,
            "recorded": recorded,
            "current": {"exists": False, "size_bytes": None, "sha256": None},
            "path": metadata.get("path"),
            "reason": f"recorded {label} file is missing",
        }
    try:
        file_status = os.stat(candidate, follow_symlinks=False)
    except OSError as exc:
        raise ConfigError(f"cannot inspect recorded {label} file {candidate}: {exc}") from exc
    if not stat.S_ISREG(file_status.st_mode):
        raise ConfigError(f"invalid {label} file in evidence bundle; expected a regular file: {candidate}")
    current = {
        "exists": True,
        "size_bytes": file_status.st_size,
        "sha256": sha256_file(candidate),
    }
    passed = recorded == {
        "size_bytes": current["size_bytes"],
        "sha256": current["sha256"],
    }
    if index_entries is not None:
        entry = index_entries.get(str(metadata.get("path")))
        passed = passed and entry is not None and "source_evidence" in entry.get("roles", [])
        if entry is not None:
            passed = passed and recorded == {
                "size_bytes": entry.get("size_bytes"),
                "sha256": entry.get("sha256"),
            }
    return {
        "id": check_id,
        "kind": "integrity",
        "category": "source",
        "canonical": True,
        "required": True,
        "passed": passed,
        "recorded": recorded,
        "current": current,
        "path": metadata.get("path"),
    }


def _source_policy_checks(source: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    expected_ref = source.get("expected_ref")
    if expected_ref:
        checks.append(
            {
                "id": "source:ref",
                "kind": "compatibility",
                "category": "source",
                "canonical": False,
                "required": True,
                "passed": source.get("available") is True
                and source.get("commit") == expected_ref,
                "expected": expected_ref,
                "actual": source.get("commit"),
            }
        )
    if source.get("available") and not source.get("allow_dirty", True):
        checks.append(
            {
                "id": "source:clean",
                "kind": "compatibility",
                "category": "source",
                "canonical": False,
                "required": True,
                "passed": source.get("dirty") is False,
                "actual": "dirty" if source.get("dirty") else "clean",
            }
        )
    return checks


def _recorded_outcome_checks(commands: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"command:{command.get('step_id')}:recorded-outcome",
            "kind": "recorded_outcome",
            "category": "command",
            "canonical": False,
            "required": True,
            "passed": command.get("status") == "completed"
            and command.get("return_code") == 0,
            "status": command.get("status"),
            "return_code": command.get("return_code"),
        }
        for command in commands
    ]


def _legacy_metric_checks(metrics: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for metric in metrics:
        try:
            decision = math.isclose(
                float(metric["actual"]),
                float(metric["expected"]),
                abs_tol=float(metric.get("atol", 0.0)),
                rel_tol=float(metric.get("rtol", 0.0)),
            )
        except (KeyError, TypeError, ValueError):
            decision = False
        checks.append(
            {
                "id": f"metric:{metric.get('id')}:expectation",
                "kind": "expectation",
                "category": "metric",
                "canonical": False,
                "required": True,
                "passed": decision,
                "actual": metric.get("actual"),
                "expected": metric.get("expected"),
                "atol": metric.get("atol"),
                "rtol": metric.get("rtol"),
            }
        )
    return checks


def _compatibility_status(
    run: Mapping[str, Any],
    checks: Sequence[Mapping[str, Any]],
) -> tuple[str, bool | None]:
    all_passed = all(check.get("passed") is True for check in checks)
    if run.get("dry_run") is True:
        return ("planned" if all_passed else "preflight_failed"), all_passed
    return ("passed" if checks and all_passed else "failed"), None


def _legacy_verification(
    directory: Path,
    run: dict[str, Any],
    source: dict[str, Any],
    inputs: list[dict[str, Any]],
    commands: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    resolved_manifest: dict[str, Any],
) -> dict[str, Any]:
    contract_checks: list[dict[str, Any]] = []
    if source.get("schema_version", 0) == 1 and source.get("available") is True:
        contract_checks.extend(
            (
                _source_file_check(
                    directory,
                    source.get("git_status"),
                    "source:git_status",
                    "Git status evidence",
                ),
                _source_file_check(
                    directory,
                    source.get("git_patch"),
                    "source:git_patch",
                    "Git patch evidence",
                ),
            )
        )

    legacy_checks = list(contract_checks)
    legacy_checks.extend(_source_policy_checks(source))
    if run.get("dry_run") is not True:
        if run.get("evidence_error"):
            legacy_checks.append(
                {
                    "id": "evidence:collection",
                    "kind": "compatibility",
                    "category": "evidence",
                    "canonical": False,
                    "required": True,
                    "passed": False,
                    "reason": run["evidence_error"],
                }
            )
        legacy_checks.extend(_recorded_outcome_checks(commands))
        for declaration_index, declaration in enumerate(artifacts):
            matches = declaration.get("matches", [])
            legacy_checks.append(
                {
                    "id": f"artifact:{declaration.get('step_id')}:{declaration_index}:recorded",
                    "kind": "compatibility",
                    "category": "artifact",
                    "canonical": False,
                    "required": True,
                    "passed": isinstance(matches, list) and bool(matches),
                    "reason": None if matches else "declared artifact matched no paths",
                }
            )
        legacy_checks.extend(_legacy_metric_checks(metrics))

    status, preflight_passed = _compatibility_status(run, legacy_checks)
    checks_passed = all(check["passed"] for check in contract_checks)
    return {
        "schema_version": 1,
        "run_id": run.get("run_id"),
        "verified_at": utc_now(),
        "verification_status": str(
            VerificationStatus.COMPLETE if checks_passed else VerificationStatus.INCOMPLETE
        ),
        "assurance_level": str(AssuranceLevel.RECORDED),
        "execution_record_status": str(recorded_execution_status(run, commands)),
        "result_status": str(ResultStatus.NOT_EVALUATED),
        "checks_passed": checks_passed,
        "evidence_root_sha256": None,
        "coverage": coverage_skeleton(
            source=source,
            inputs=inputs,
            artifacts=artifacts,
            declared_metric_count=len(resolved_manifest.get("metrics", [])),
            recorded_metric_count=len(metrics),
        ),
        "not_established": dict(NOT_ESTABLISHED),
        "compatibility": {
            "deprecated_fields": list(DEPRECATED_VERIFICATION_FIELDS),
            "legacy_status": status,
            "legacy_passed": status == "passed",
        },
        "status": status,
        "passed": status == "passed",
        "preflight_passed": preflight_passed,
        "contract_checks": contract_checks,
        "checks": legacy_checks,
    }


def _validate_scoped_fingerprint(record: Any, context: str) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ConfigError(f"invalid {context}; expected an object")
    scope = record.get("path_scope")
    if scope not in {"bundle", "external"}:
        raise ConfigError(f"invalid {context}; path_scope must be bundle or external")
    evidence_path = record.get("evidence_path")
    if scope == "bundle":
        if not isinstance(evidence_path, str) or not evidence_path:
            raise ConfigError(f"invalid {context}; bundle evidence_path is required")
        if record.get("exists") is not True or record.get("kind") != "file":
            raise ConfigError(f"invalid {context}; bundle evidence must be a recorded file")
        size = record.get("size_bytes")
        digest = record.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ConfigError(f"invalid {context}; size_bytes must be a non-negative integer")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ConfigError(f"invalid {context}; sha256 must be SHA-256 hex")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ConfigError(f"invalid {context}; sha256 must be SHA-256 hex") from exc
    elif evidence_path is not None:
        raise ConfigError(f"invalid {context}; external evidence_path must be null")
    return record


def _validate_schema_one_records(
    run: Mapping[str, Any],
    inputs: Sequence[dict[str, Any]],
    commands: Sequence[dict[str, Any]],
    artifacts: Sequence[dict[str, Any]],
) -> None:
    if not isinstance(run.get("dry_run"), bool):
        raise ConfigError("invalid run.json; dry_run must be boolean")
    for index, item in enumerate(inputs):
        _validate_scoped_fingerprint(item, f"inputs.json item {index}")
    seen_steps: set[str] = set()
    for index, command in enumerate(commands):
        step_id = command.get("step_id")
        if not isinstance(step_id, str) or not step_id or step_id in seen_steps:
            raise ConfigError(f"invalid commands.json item {index}; step_id must be unique")
        seen_steps.add(step_id)
        for stream in ("stdout", "stderr"):
            value = command.get(f"{stream}_evidence_path")
            if not isinstance(value, str) or not value:
                raise ConfigError(
                    f"invalid commands.json item {index}; {stream}_evidence_path is required"
                )
    for declaration_index, declaration in enumerate(artifacts):
        matches = declaration.get("matches")
        if not isinstance(matches, list):
            raise ConfigError(
                f"invalid artifacts.json declaration {declaration_index}; matches must be an array"
            )
        for match_index, match in enumerate(matches):
            _validate_scoped_fingerprint(
                match,
                f"artifacts.json declaration {declaration_index} match {match_index}",
            )


def _indexed_reference_check(
    *,
    check_id: str,
    category: str,
    path: Any,
    role: str,
    index_entries: Mapping[str, Mapping[str, Any]],
    validated_paths: Mapping[str, bool],
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    entry = index_entries.get(path) if isinstance(path, str) else None
    passed = (
        entry is not None
        and role in entry.get("roles", [])
        and validated_paths.get(path) is True
    )
    if metadata is not None and entry is not None:
        passed = passed and metadata.get("size_bytes") == entry.get("size_bytes")
        passed = passed and metadata.get("sha256") == entry.get("sha256")
    return {
        "id": check_id,
        "kind": "integrity",
        "category": category,
        "canonical": True,
        "required": True,
        "passed": passed,
        "path": path,
        "required_role": role,
        "recorded": (
            None
            if metadata is None
            else {key: metadata.get(key) for key in ("size_bytes", "sha256")}
        ),
        "indexed": (
            None
            if entry is None
            else {
                key: entry.get(key) for key in ("roles", "size_bytes", "sha256")
            }
        ),
    }


def _strict_number_equal(recorded: Any, recomputed: float) -> bool:
    return (
        not isinstance(recorded, bool)
        and isinstance(recorded, (int, float))
        and float(recorded) == recomputed
    )


def _strict_integer_equal(recorded: Any, recomputed: int) -> bool:
    return not isinstance(recorded, bool) and isinstance(recorded, int) and recorded == recomputed


def _schema_one_coverage(
    source: Mapping[str, Any],
    inputs: Sequence[Mapping[str, Any]],
    artifacts: Sequence[Mapping[str, Any]],
    manifest_metrics: Sequence[Mapping[str, Any]],
    metrics: Sequence[Mapping[str, Any]],
    metric_sources: Mapping[str, Any],
    captured_metric_ids: set[str],
) -> dict[str, Any]:
    artifact_matches = [
        match
        for declaration in artifacts
        for match in declaration.get("matches", [])
    ]
    source_records = metric_sources.get("metrics", [])
    source_files_captured = sum(
        len(metric.get("sources", []))
        for metric in source_records
        if metric.get("id") in captured_metric_ids
    )
    source_coverage = source.get("coverage")
    replay = (
        source_coverage.get("replay", "unknown")
        if isinstance(source_coverage, Mapping)
        else "unknown"
    )
    return {
        "inputs": {
            "bundle_local": sum(item.get("path_scope") == "bundle" for item in inputs),
            "external_metadata_only": sum(
                item.get("path_scope") == "external" for item in inputs
            ),
            "total": len(inputs),
        },
        "artifacts": {
            "bundle_local": sum(
                item.get("path_scope") == "bundle" for item in artifact_matches
            ),
            "external_metadata_only": sum(
                item.get("path_scope") == "external" for item in artifact_matches
            ),
            "total": len(artifact_matches),
        },
        "metric_sources": {
            "total": len(manifest_metrics),
            "recorded": len(metrics),
            "captured": len(captured_metric_ids),
            "source_files_captured": source_files_captured,
        },
        "source": {"replay": replay},
    }


def _schema_one_verification(
    directory: Path,
    run: dict[str, Any],
    source: dict[str, Any],
    inputs: list[dict[str, Any]],
    commands: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    resolved_manifest: dict[str, Any],
    metric_sources_value: Any,
) -> dict[str, Any]:
    _validate_schema_one_records(run, inputs, commands, artifacts)
    metric_sources = validate_metric_sources_record(metric_sources_value)
    index = read_evidence_index(directory)
    index_validation = validate_evidence_index(directory, index)
    index_entries = {entry["path"]: entry for entry in index["entries"]}
    validated_paths = {
        check["path"]: check["passed"] for check in index_validation["checks"]
    }

    expected_declarations = evidence_dependency_declarations(
        run=run,
        source=source,
        inputs=inputs,
        commands=commands,
        artifacts=artifacts,
        metric_sources=metric_sources,
    )
    expected_map = {
        declaration["path"]: declaration["roles"] for declaration in expected_declarations
    }
    actual_map = {entry["path"]: entry["roles"] for entry in index["entries"]}

    integrity_checks: list[dict[str, Any]] = [
        {
            "id": "bundle:index",
            "kind": "integrity",
            "category": "bundle",
            "canonical": True,
            "required": True,
            "passed": index_validation["valid"],
        },
        {
            "id": "bundle:closure",
            "kind": "integrity",
            "category": "bundle",
            "canonical": True,
            "required": True,
            "passed": actual_map == expected_map,
            "missing": sorted(set(expected_map) - set(actual_map)),
            "unexpected": sorted(set(actual_map) - set(expected_map)),
            "role_mismatches": sorted(
                path
                for path in set(expected_map) & set(actual_map)
                if expected_map[path] != actual_map[path]
            ),
        },
    ]
    integrity_checks.extend(
        {
            "id": f"bundle:file:{check['path']}",
            "kind": "integrity",
            "category": "bundle",
            "canonical": True,
            "required": True,
            "passed": check["passed"],
            "path": check["path"],
            "recorded": check["recorded"],
            "current": check["current"],
            **({"reason": check["reason"]} if "reason" in check else {}),
        }
        for check in index_validation["checks"]
    )

    if run.get("evidence_error"):
        integrity_checks.append(
            {
                "id": "evidence:collection",
                "kind": "structure",
                "category": "evidence",
                "canonical": True,
                "required": True,
                "passed": False,
                "reason": run["evidence_error"],
            }
        )

    if source.get("schema_version", 0) == 1 and source.get("available") is True:
        integrity_checks.extend(
            (
                _source_file_check(
                    directory,
                    source.get("git_status"),
                    "source:git_status",
                    "Git status evidence",
                    index_entries=index_entries,
                ),
                _source_file_check(
                    directory,
                    source.get("git_patch"),
                    "source:git_patch",
                    "Git patch evidence",
                    index_entries=index_entries,
                ),
            )
        )

    for index_number, item in enumerate(inputs):
        if item.get("path_scope") == "bundle":
            integrity_checks.append(
                _indexed_reference_check(
                    check_id=f"closure:input:{item.get('id', index_number)}",
                    category="input",
                    path=item.get("evidence_path"),
                    role="input",
                    index_entries=index_entries,
                    validated_paths=validated_paths,
                    metadata=item,
                )
            )

    if run.get("dry_run") is not True:
        for command in commands:
            for stream in ("stdout", "stderr"):
                integrity_checks.append(
                    _indexed_reference_check(
                        check_id=(
                            f"closure:command-log:{command.get('step_id')}:{stream}"
                        ),
                        category="command_log",
                        path=command.get(f"{stream}_evidence_path"),
                        role="command_log",
                        index_entries=index_entries,
                        validated_paths=validated_paths,
                    )
                )

    manifest_step_ids = [
        step["id"] for step in resolved_manifest.get("run", {}).get("steps", [])
    ]
    recorded_step_ids = [command.get("step_id") for command in commands]
    command_prefix_is_valid = (
        bool(recorded_step_ids)
        and recorded_step_ids == manifest_step_ids[: len(recorded_step_ids)]
        and (
            run.get("dry_run") is True
            or not all(
                command.get("status") == "completed" and command.get("return_code") == 0
                for command in commands
            )
            or recorded_step_ids == manifest_step_ids
        )
    )
    if run.get("dry_run") is True:
        command_prefix_is_valid = recorded_step_ids == manifest_step_ids
    integrity_checks.append(
        {
            "id": "command:record-closure",
            "kind": "structure",
            "category": "command",
            "canonical": True,
            "required": True,
            "passed": command_prefix_is_valid,
            "manifest_step_ids": manifest_step_ids,
            "recorded_step_ids": recorded_step_ids,
        }
    )

    for declaration_index, declaration in enumerate(artifacts):
        matches = declaration.get("matches", [])
        integrity_checks.append(
            {
                "id": f"closure:artifact:{declaration.get('step_id')}:{declaration_index}",
                "kind": "integrity",
                "category": "artifact",
                "canonical": True,
                "required": True,
                "passed": bool(matches),
                **({"reason": "declared artifact matched no paths"} if not matches else {}),
            }
        )
        for match_index, match in enumerate(matches):
            if match.get("path_scope") == "bundle":
                integrity_checks.append(
                    _indexed_reference_check(
                        check_id=(
                            f"closure:artifact:{declaration.get('step_id')}:"
                            f"{declaration_index}:{match_index}"
                        ),
                        category="artifact",
                        path=match.get("evidence_path"),
                        role="artifact",
                        index_entries=index_entries,
                        validated_paths=validated_paths,
                        metadata=match,
                    )
                )

    execution_status = recorded_execution_status(run, commands)
    manifest_metrics = resolved_manifest.get("metrics", [])
    manifest_ids = [specification["id"] for specification in manifest_metrics]
    source_ids = [metric["id"] for metric in metric_sources["metrics"]]

    metric_source_checks_by_id: dict[str, list[dict[str, Any]]] = {}
    if run.get("dry_run") is not True:
        for metric in metric_sources["metrics"]:
            checks: list[dict[str, Any]] = []
            for metric_source in metric["sources"]:
                check = _indexed_reference_check(
                    check_id=(
                        f"closure:metric-source:{metric['id']}:{metric_source['ordinal']}"
                    ),
                    category="metric_source",
                    path=metric_source["evidence_path"],
                    role="metric_source",
                    index_entries=index_entries,
                    validated_paths=validated_paths,
                    metadata=metric_source,
                )
                checks.append(check)
                integrity_checks.append(check)
            metric_source_checks_by_id[metric["id"]] = checks

    if run.get("dry_run") is True:
        integrity_checks.append(
            {
                "id": "metric:planning-source-closure",
                "kind": "integrity",
                "category": "metric_source",
                "canonical": True,
                "required": True,
                "passed": source_ids == [],
                "recorded_metric_source_ids": source_ids,
            }
        )
    elif manifest_metrics and str(execution_status) == "recorded_success":
        declared_paths = {
            specification["id"]: specification["path"] for specification in manifest_metrics
        }
        source_paths_match = all(
            metric.get("declared_path") == declared_paths.get(metric["id"])
            for metric in metric_sources["metrics"]
        )
        integrity_checks.append(
            {
                "id": "metric:source-closure",
                "kind": "integrity",
                "category": "metric_source",
                "canonical": True,
                "required": True,
                "passed": source_ids == manifest_ids and source_paths_match,
                "manifest_ids": manifest_ids,
                "recorded_metric_source_ids": source_ids,
                "declared_paths_match": source_paths_match,
            }
        )

    recorded_ids = [metric.get("id") for metric in metrics]
    derivation_checks: list[dict[str, Any]] = []
    expectation_checks: list[dict[str, Any]] = []
    captured_metric_ids = {
        metric_id
        for metric_id, checks in metric_source_checks_by_id.items()
        if checks and all(check["passed"] for check in checks)
    }

    derivation_applicable = (
        run.get("dry_run") is not True
        and bool(manifest_metrics)
        and str(execution_status) == "recorded_success"
    )
    if derivation_applicable:
        id_check = {
            "id": "metric:id-closure",
            "kind": "derivation",
            "category": "metric",
            "canonical": True,
            "required": True,
            "passed": source_ids == manifest_ids and recorded_ids == manifest_ids,
            "manifest_ids": manifest_ids,
            "metric_source_ids": source_ids,
            "recorded_metric_ids": recorded_ids,
        }
        derivation_checks.append(id_check)
        sources_by_id = {metric["id"]: metric for metric in metric_sources["metrics"]}
        recorded_by_id: dict[str, dict[str, Any]] = {}
        duplicate_recorded_ids: set[str] = set()
        for metric in metrics:
            metric_id = metric.get("id")
            if isinstance(metric_id, str) and metric_id in recorded_by_id:
                duplicate_recorded_ids.add(metric_id)
            elif isinstance(metric_id, str):
                recorded_by_id[metric_id] = metric

        for specification in manifest_metrics:
            metric_id = specification["id"]
            source_record = sources_by_id.get(metric_id)
            recorded = recorded_by_id.get(metric_id)
            recomputed: dict[str, Any] | None = None
            extraction_error: str | None = None
            evidence_paths: list[Path] = []
            evidence_path_values: list[str] = []
            if source_record is None or recorded is None or metric_id in duplicate_recorded_ids:
                extraction_error = "metric source or derived record is missing or duplicated"
            else:
                evidence_path_values = [
                    source_item["evidence_path"] for source_item in source_record["sources"]
                ]
                try:
                    evidence_paths = [
                        resolve_bundle_file(
                            directory,
                            evidence_path,
                            label=f"metric {metric_id} source evidence",
                        )
                        for evidence_path in evidence_path_values
                    ]
                    recomputed = extract_metric_from_evidence(specification, evidence_paths)
                except ConfigError as exc:
                    extraction_error = str(exc)

            source_integrity = (
                metric_id in captured_metric_ids
                and source_record is not None
                and source_record.get("declared_path") == specification.get("path")
            )
            passed = recomputed is not None and recorded is not None and source_integrity
            expected = float(specification["expected"])
            atol = float(specification.get("atol", 0.0))
            rtol = float(specification.get("rtol", 0.0))
            expectation = False
            if recomputed is not None and recorded is not None:
                actual = float(recomputed["actual"])
                expectation = math.isclose(actual, expected, abs_tol=atol, rel_tol=rtol)
                passed = passed and recorded.get("extractor") == specification["extractor"]
                passed = passed and recorded.get("select") == recomputed["select"]
                passed = passed and recorded.get("source_evidence_paths") == evidence_path_values
                passed = passed and _strict_integer_equal(
                    recorded.get("sample_count"), recomputed["sample_count"]
                )
                passed = passed and _strict_number_equal(recorded.get("actual"), actual)
                passed = passed and _strict_number_equal(recorded.get("expected"), expected)
                passed = passed and _strict_number_equal(recorded.get("atol"), atol)
                passed = passed and _strict_number_equal(recorded.get("rtol"), rtol)
                passed = passed and recorded.get("passed") is expectation
                passed = passed and _strict_number_equal(
                    recorded.get("absolute_error"), abs(actual - expected)
                )

            derivation_checks.append(
                {
                    "id": f"metric:{metric_id}:derived-match",
                    "kind": "derivation",
                    "category": "metric",
                    "canonical": True,
                    "required": True,
                    "passed": passed,
                    "comparison_rule": "strict_python_numeric_equality",
                    "recomputed_actual": (
                        None if recomputed is None else float(recomputed["actual"])
                    ),
                    "recorded_actual": None if recorded is None else recorded.get("actual"),
                    "recomputed_sample_count": (
                        None if recomputed is None else recomputed["sample_count"]
                    ),
                    **({"reason": extraction_error} if extraction_error else {}),
                }
            )
            expectation_checks.append(
                {
                    "id": f"metric:{metric_id}:expectation",
                    "kind": "expectation",
                    "category": "metric",
                    "canonical": False,
                    "required": True,
                    "passed": expectation,
                    "actual": None if recomputed is None else float(recomputed["actual"]),
                    "expected": expected,
                    "atol": atol,
                    "rtol": rtol,
                    "authority": "manifest.resolved.yaml+recomputed_evidence",
                }
            )

    contract_checks = integrity_checks + derivation_checks
    checks_passed = all(check["passed"] for check in contract_checks)
    integrity_passed = all(check["passed"] for check in integrity_checks)
    derivations_recomputed = (
        derivation_applicable
        and bool(derivation_checks)
        and all(check["passed"] for check in derivation_checks)
    )
    assurance = highest_assurance_level(
        bundle_integrity_checked=integrity_passed,
        metric_derivations_recomputed=derivations_recomputed and integrity_passed,
        declared_metric_count=len(manifest_metrics),
    )

    if run.get("dry_run") is True or not manifest_metrics:
        result_status = ResultStatus.NOT_EVALUATED
    elif not derivations_recomputed:
        result_status = ResultStatus.INDETERMINATE
    elif all(check["passed"] for check in expectation_checks):
        result_status = ResultStatus.MATCHED
    else:
        result_status = ResultStatus.NOT_MATCHED

    legacy_checks = list(contract_checks)
    legacy_checks.extend(_source_policy_checks(source))
    if run.get("dry_run") is not True:
        legacy_checks.extend(_recorded_outcome_checks(commands))
        legacy_checks.extend(expectation_checks)
    status, preflight_passed = _compatibility_status(run, legacy_checks)
    coverage = _schema_one_coverage(
        source,
        inputs,
        artifacts,
        manifest_metrics,
        metrics,
        metric_sources,
        captured_metric_ids,
    )
    return {
        "schema_version": 1,
        "run_id": run.get("run_id"),
        "verified_at": utc_now(),
        "verification_status": str(
            VerificationStatus.COMPLETE if checks_passed else VerificationStatus.INCOMPLETE
        ),
        "assurance_level": str(assurance),
        "execution_record_status": str(execution_status),
        "result_status": str(result_status),
        "checks_passed": checks_passed,
        "evidence_root_sha256": (
            index_validation["evidence_root_sha256"] if integrity_passed else None
        ),
        "coverage": coverage,
        "not_established": dict(NOT_ESTABLISHED),
        "compatibility": {
            "deprecated_fields": list(DEPRECATED_VERIFICATION_FIELDS),
            "legacy_status": status,
            "legacy_passed": status == "passed",
        },
        "status": status,
        "passed": status == "passed",
        "preflight_passed": preflight_passed,
        "contract_checks": contract_checks,
        "checks": legacy_checks,
    }


def verify_bundle(run_dir: str | Path, *, write: bool = True) -> dict[str, Any]:
    directory = Path(run_dir).expanduser().resolve()
    common_required = [
        "run.json",
        "source.json",
        "environment.json",
        "inputs.json",
        "commands.json",
        "artifacts.json",
        "metrics.json",
        "manifest.resolved.yaml",
    ]
    common_paths = {
        name: resolve_bundle_file(
            directory,
            name,
            label=f"{name} evidence",
            allow_missing=True,
        )
        for name in common_required
    }
    missing = [name for name, path in common_paths.items() if not path.exists()]
    if missing:
        raise ConfigError(f"invalid evidence bundle {directory}; missing: {', '.join(missing)}")

    run = _require_record_object(read_json(common_paths["run.json"]), "run.json")
    run_schema = run.get("schema_version", 0)
    if isinstance(run_schema, bool) or not isinstance(run_schema, int) or run_schema not in (0, 1):
        raise ConfigError(f"unsupported run.json schema_version: {run_schema!r}")
    if run_schema == 1:
        schema_one_required = ["commands.jsonl", "metric_sources.json", "evidence.index.json"]
        schema_one_paths = {
            name: resolve_bundle_file(
                directory,
                name,
                label=f"{name} evidence",
                allow_missing=True,
            )
            for name in schema_one_required
        }
        missing = [name for name, path in schema_one_paths.items() if not path.exists()]
        if missing:
            raise ConfigError(f"invalid evidence bundle {directory}; missing: {', '.join(missing)}")

    source = read_source_record(common_paths["source.json"])
    _require_record_object(read_json(common_paths["environment.json"]), "environment.json")
    inputs = _require_record_list(read_json(common_paths["inputs.json"]), "inputs.json")
    commands = _require_record_list(read_json(common_paths["commands.json"]), "commands.json")
    artifacts = _require_record_list(read_json(common_paths["artifacts.json"]), "artifacts.json")
    metrics = _require_record_list(read_json(common_paths["metrics.json"]), "metrics.json")
    resolved_manifest = _read_resolved_manifest(common_paths["manifest.resolved.yaml"])

    if run_schema == 0:
        result = _legacy_verification(
            directory,
            run,
            source,
            inputs,
            commands,
            artifacts,
            metrics,
            resolved_manifest,
        )
    else:
        metric_sources = read_json(schema_one_paths["metric_sources.json"])
        result = _schema_one_verification(
            directory,
            run,
            source,
            inputs,
            commands,
            artifacts,
            metrics,
            resolved_manifest,
            metric_sources,
        )
    if write:
        write_json_atomic(directory / "verification.json", result)
    return result
