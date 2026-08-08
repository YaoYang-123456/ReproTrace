from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from reprotrace.assurance import (
    ASSURANCE_HIERARCHY,
    DEPRECATED_VERIFICATION_FIELDS,
    NOT_ESTABLISHED,
    AssuranceLevel,
    ExecutionRecordStatus,
    ResultStatus,
    VerificationStatus,
    highest_assurance_level,
    recorded_execution_status,
)
from reprotrace.runner import run_manifest
from reprotrace.errors import ConfigError
from reprotrace.io import write_json
from reprotrace.verifier import verify_bundle


def make_contract_manifest(tmp_path: Path, *, exit_code: int = 0) -> Path:
    manifest = tmp_path / "reprotrace.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 0,
                "project": {"name": "assurance-contract", "root": "."},
                "run": {
                    "output_root": ".evidence",
                    "steps": [
                        {
                            "id": "recorded-step",
                            "argv": [sys.executable, "-c", f"raise SystemExit({exit_code})"],
                        }
                    ],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return manifest


def test_canonical_contract_values_are_stable() -> None:
    assert tuple(item.value for item in ASSURANCE_HIERARCHY) == (
        "recorded",
        "bundle_integrity_checked",
        "metric_derivations_recomputed",
    )
    assert {item.value for item in VerificationStatus} == {"complete", "incomplete", "invalid"}
    assert {item.value for item in ExecutionRecordStatus} == {
        "not_run",
        "recorded_success",
        "recorded_failure",
        "unknown",
    }
    assert {item.value for item in ResultStatus} == {
        "matched",
        "not_matched",
        "indeterminate",
        "not_evaluated",
    }
    assert DEPRECATED_VERIFICATION_FIELDS == ("status", "passed", "preflight_passed")
    assert NOT_ESTABLISHED == {
        "execution_authenticity": "not_established",
        "independent_replay": "not_performed",
        "scientific_reproduction": "not_established",
    }


def test_zero_metric_contract_stops_at_bundle_integrity() -> None:
    assert (
        highest_assurance_level(
            bundle_integrity_checked=True,
            metric_derivations_recomputed=False,
            declared_metric_count=0,
        )
        is AssuranceLevel.BUNDLE_INTEGRITY_CHECKED
    )
    assert (
        highest_assurance_level(
            bundle_integrity_checked=True,
            metric_derivations_recomputed=True,
            declared_metric_count=0,
        )
        is AssuranceLevel.BUNDLE_INTEGRITY_CHECKED
    )
    assert (
        highest_assurance_level(
            bundle_integrity_checked=True,
            metric_derivations_recomputed=True,
            declared_metric_count=1,
        )
        is AssuranceLevel.METRIC_DERIVATIONS_RECOMPUTED
    )


def test_metric_derivation_assurance_requires_integrity() -> None:
    with pytest.raises(ValueError, match="requires bundle integrity"):
        highest_assurance_level(
            bundle_integrity_checked=False,
            metric_derivations_recomputed=True,
            declared_metric_count=1,
        )


def test_recorded_execution_status_never_claims_authenticity() -> None:
    assert recorded_execution_status({"dry_run": True}, []) is ExecutionRecordStatus.NOT_RUN
    assert recorded_execution_status({"dry_run": False}, []) is ExecutionRecordStatus.UNKNOWN
    assert (
        recorded_execution_status(
            {"dry_run": False},
            [{"status": "completed", "return_code": 0}],
        )
        is ExecutionRecordStatus.RECORDED_SUCCESS
    )
    assert (
        recorded_execution_status(
            {"dry_run": False},
            [{"status": "failed", "return_code": 3}],
        )
        is ExecutionRecordStatus.RECORDED_FAILURE
    )


def test_schema_one_verification_skeleton_preserves_legacy_fields(tmp_path: Path) -> None:
    run_dir, verification = run_manifest(make_contract_manifest(tmp_path))

    assert verification["schema_version"] == 1
    assert verification["verification_status"] == "complete"
    assert verification["assurance_level"] == "recorded"
    assert verification["execution_record_status"] == "recorded_success"
    assert verification["result_status"] == "not_evaluated"
    assert verification["checks_passed"] is True
    assert verification["coverage"]["metric_sources"] == {"captured": 0, "total": 0}
    assert verification["not_established"] == NOT_ESTABLISHED
    assert verification["compatibility"] == {
        "deprecated_fields": ["status", "passed", "preflight_passed"],
        "legacy_status": "passed",
        "legacy_passed": True,
    }
    assert verification["status"] == "passed"
    assert verification["passed"] is True
    assert verification["preflight_passed"] is None
    assert (run_dir / "verification.json").is_file()


def test_dry_run_is_not_an_execution_failure(tmp_path: Path) -> None:
    _, verification = run_manifest(make_contract_manifest(tmp_path), dry_run=True)

    assert verification["verification_status"] == "complete"
    assert verification["assurance_level"] == "recorded"
    assert verification["execution_record_status"] == "not_run"
    assert verification["result_status"] == "not_evaluated"
    assert verification["checks_passed"] is True
    assert verification["status"] == "planned"
    assert verification["passed"] is False
    assert verification["preflight_passed"] is True
    assert all(check["id"] != "execution" for check in verification["checks"])


def test_recorded_command_failure_is_orthogonal_to_assurance(tmp_path: Path) -> None:
    _, verification = run_manifest(make_contract_manifest(tmp_path, exit_code=3))

    assert verification["verification_status"] == "incomplete"
    assert verification["assurance_level"] == "recorded"
    assert verification["execution_record_status"] == "recorded_failure"
    assert verification["result_status"] == "not_evaluated"
    assert verification["checks_passed"] is False
    assert verification["status"] == "failed"
    assert verification["passed"] is False


@pytest.mark.parametrize(
    ("filename", "invalid_value"),
    [
        ("run.json", []),
        ("environment.json", []),
        ("inputs.json", {}),
        ("commands.json", {}),
        ("artifacts.json", {}),
        ("metrics.json", {}),
    ],
)
def test_recorded_assurance_requires_valid_core_record_shapes(
    tmp_path: Path, filename: str, invalid_value: object
) -> None:
    run_dir, _ = run_manifest(make_contract_manifest(tmp_path))
    write_json(run_dir / filename, invalid_value)

    with pytest.raises(ConfigError, match=filename):
        verify_bundle(run_dir, write=False)


def test_recorded_assurance_requires_valid_resolved_manifest(tmp_path: Path) -> None:
    run_dir, _ = run_manifest(make_contract_manifest(tmp_path))
    (run_dir / "manifest.resolved.yaml").write_text("- not\n- an\n- object\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="manifest.resolved.yaml"):
        verify_bundle(run_dir, write=False)
