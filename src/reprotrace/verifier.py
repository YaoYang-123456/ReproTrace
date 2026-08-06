"""Verification of recorded evidence and current files."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .io import comparison_key, fingerprint, read_json, utc_now, write_json


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

    run = read_json(directory / "run.json")
    source = read_json(directory / "source.json")
    inputs = read_json(directory / "inputs.json")
    commands = read_json(directory / "commands.json")
    artifacts = read_json(directory / "artifacts.json")
    metrics = read_json(directory / "metrics.json")
    checks: list[dict[str, Any]] = []

    expected_ref = source.get("expected_ref")
    if expected_ref:
        checks.append(
            {
                "id": "source:ref",
                "category": "source",
                "passed": source.get("available") is True and source.get("commit") == expected_ref,
                "expected": expected_ref,
                "actual": source.get("commit"),
            }
        )
    if source.get("available") and not source.get("allow_dirty", True):
        checks.append(
            {
                "id": "source:clean",
                "category": "source",
                "passed": source.get("dirty") is False,
                "actual": "dirty" if source.get("dirty") else "clean",
            }
        )
    for item in inputs:
        checks.append(_check_fingerprint(item, f"input:{item['id']}", "input"))

    if run.get("dry_run"):
        preflight_passed = all(check["passed"] for check in checks)
        checks.append({"id": "execution", "category": "run", "passed": False, "reason": "dry run; no experiment was executed"})
        status = "planned" if preflight_passed else "preflight_failed"
    else:
        if run.get("evidence_error"):
            checks.append(
                {
                    "id": "evidence:collection",
                    "category": "evidence",
                    "passed": False,
                    "reason": run["evidence_error"],
                }
            )
        for command in commands:
            checks.append(
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
                checks.append(
                    {
                        "id": f"artifact:{declaration['step_id']}:{declaration_index}:missing",
                        "category": "artifact",
                        "passed": False,
                        "reason": "declared artifact matched no paths",
                    }
                )
            for index, record in enumerate(declaration.get("matches", [])):
                checks.append(
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
            checks.append(
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
        status = "passed" if checks and all(check["passed"] for check in checks) else "failed"

    result = {
        "schema_version": 0,
        "run_id": run.get("run_id"),
        "verified_at": utc_now(),
        "status": status,
        "passed": status == "passed",
        "preflight_passed": preflight_passed if run.get("dry_run") else None,
        "checks": checks,
    }
    if write:
        write_json(directory / "verification.json", result)
    return result
