"""Verification of recorded evidence and current files."""

from __future__ import annotations

import math
import os
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import yaml

from .assurance import (
    DEPRECATED_VERIFICATION_FIELDS,
    NOT_ESTABLISHED,
    AssuranceLevel,
    ResultStatus,
    VerificationStatus,
    coverage_skeleton,
    recorded_execution_status,
)
from .errors import ConfigError
from .io import comparison_key, fingerprint, read_json, read_source_record, sha256_file, utc_now, write_json
from .manifest import validate_manifest


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


def _check_fingerprint(record: dict[str, Any], check_id: str, category: str) -> dict[str, Any]:
    current = fingerprint(Path(record["path"]))
    passed = comparison_key(record) == comparison_key(current)
    return {
        "id": check_id,
        "category": category,
        "passed": passed,
        "recorded": {key: record.get(key) for key in ("exists", "kind", "size_bytes", "sha256")},
        "current": {key: current.get(key) for key in ("exists", "kind", "size_bytes", "sha256")},
        "path": record["path"],
    }


def _validated_source_path(directory: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"invalid {label} path in source.json; expected a non-empty relative path")
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ConfigError(f"invalid {label} path in source.json; absolute paths are not allowed: {value!r}")
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise ConfigError(f"invalid {label} path in source.json; parent traversal is not allowed: {value!r}")
    try:
        bundle_root = directory.resolve(strict=True)
        candidate = directory / Path(value)
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(bundle_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError(f"invalid {label} path in source.json; path escapes the evidence bundle: {value!r}") from exc
    if candidate.is_symlink():
        raise ConfigError(f"invalid {label} file in evidence bundle; expected a regular file: {candidate}")
    return candidate


def _check_source_file(directory: Path, metadata: Any, check_id: str, label: str) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        raise ConfigError(f"invalid source.json; {label} metadata must be an object")
    candidate = _validated_source_path(directory, metadata.get("path"), label)
    recorded = {key: metadata.get(key) for key in ("size_bytes", "sha256")}
    if not candidate.exists():
        return {
            "id": check_id,
            "category": "source",
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
    return {
        "id": check_id,
        "category": "source",
        "passed": recorded == {"size_bytes": current["size_bytes"], "sha256": current["sha256"]},
        "recorded": recorded,
        "current": current,
        "path": metadata.get("path"),
    }


def verify_bundle(run_dir: str | Path, *, write: bool = True) -> dict[str, Any]:
    directory = Path(run_dir).expanduser().resolve()
    required = [
        "run.json",
        "source.json",
        "environment.json",
        "inputs.json",
        "commands.json",
        "artifacts.json",
        "metrics.json",
        "manifest.resolved.yaml",
    ]
    missing = [name for name in required if not (directory / name).is_file()]
    if missing:
        raise ConfigError(f"invalid evidence bundle {directory}; missing: {', '.join(missing)}")

    run = _require_record_object(read_json(directory / "run.json"), "run.json")
    if run.get("schema_version", 0) != 0:
        raise ConfigError(f"unsupported run.json schema_version: {run.get('schema_version')!r}")
    source = read_source_record(directory / "source.json")
    _require_record_object(read_json(directory / "environment.json"), "environment.json")
    inputs = _require_record_list(read_json(directory / "inputs.json"), "inputs.json")
    commands = _require_record_list(read_json(directory / "commands.json"), "commands.json")
    artifacts = _require_record_list(read_json(directory / "artifacts.json"), "artifacts.json")
    metrics = _require_record_list(read_json(directory / "metrics.json"), "metrics.json")
    resolved_manifest = _read_resolved_manifest(directory / "manifest.resolved.yaml")
    contract_checks: list[dict[str, Any]] = []
    legacy_checks: list[dict[str, Any]] = []

    source_schema = source.get("schema_version", 0)
    if source_schema == 1 and source.get("available") is True:
        source_status_check = _check_source_file(
            directory, source.get("git_status"), "source:git_status", "Git status evidence"
        )
        source_patch_check = _check_source_file(
            directory, source.get("git_patch"), "source:git_patch", "Git patch evidence"
        )
        contract_checks.extend((source_status_check, source_patch_check))
        legacy_checks.extend((source_status_check, source_patch_check))

    expected_ref = source.get("expected_ref")
    if expected_ref:
        legacy_checks.append(
            {
                "id": "source:ref",
                "category": "source",
                "passed": source.get("available") is True and source.get("commit") == expected_ref,
                "expected": expected_ref,
                "actual": source.get("commit"),
            }
        )
    if source.get("available") and not source.get("allow_dirty", True):
        legacy_checks.append(
            {
                "id": "source:clean",
                "category": "source",
                "passed": source.get("dirty") is False,
                "actual": "dirty" if source.get("dirty") else "clean",
            }
        )
    for item in inputs:
        legacy_checks.append(_check_fingerprint(item, f"input:{item['id']}", "input"))

    if run.get("dry_run"):
        preflight_passed = all(check["passed"] for check in legacy_checks)
        status = "planned" if preflight_passed else "preflight_failed"
    else:
        if run.get("evidence_error"):
            legacy_checks.append(
                {
                    "id": "evidence:collection",
                    "category": "evidence",
                    "passed": False,
                    "reason": run["evidence_error"],
                }
            )
        for command in commands:
            legacy_checks.append(
                {
                    "id": f"step:{command.get('step_id')}",
                    "category": "command",
                    "passed": command.get("status") == "completed" and command.get("return_code") == 0,
                    "status": command.get("status"),
                    "return_code": command.get("return_code"),
                }
            )

        for declaration_index, declaration in enumerate(artifacts):
            if not declaration.get("matches"):
                legacy_checks.append(
                    {
                        "id": f"artifact:{declaration['step_id']}:{declaration_index}:missing",
                        "category": "artifact",
                        "passed": False,
                        "reason": "declared artifact matched no paths",
                    }
                )
            for index, record in enumerate(declaration.get("matches", [])):
                legacy_checks.append(
                    _check_fingerprint(
                        record,
                        f"artifact:{declaration['step_id']}:{declaration_index}:{index}",
                        "artifact",
                    )
                )
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
            legacy_checks.append(
                {
                    "id": f"metric:{metric['id']}",
                    "category": "metric",
                    "passed": decision,
                    "actual": metric.get("actual"),
                    "expected": metric.get("expected"),
                    "atol": metric.get("atol"),
                    "rtol": metric.get("rtol"),
                }
            )
        status = (
            "passed" if legacy_checks and all(check["passed"] for check in legacy_checks) else "failed"
        )

    compatibility_passed = status == "passed"
    checks_passed = all(check["passed"] for check in contract_checks)
    result = {
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
            "legacy_passed": compatibility_passed,
        },
        "status": status,
        "passed": compatibility_passed,
        "preflight_passed": preflight_passed if run.get("dry_run") else None,
        "contract_checks": contract_checks,
        "checks": legacy_checks,
    }
    if write:
        write_json(directory / "verification.json", result)
    return result
