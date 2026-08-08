"""Canonical C5 verification assurance vocabulary and contract helpers."""

from __future__ import annotations

from enum import Enum
from typing import Any, Sequence


class ContractValue(str, Enum):
    """A JSON-compatible string enum with stable human-readable values."""

    def __str__(self) -> str:
        return self.value


class VerificationStatus(ContractValue):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"


class AssuranceLevel(ContractValue):
    RECORDED = "recorded"
    BUNDLE_INTEGRITY_CHECKED = "bundle_integrity_checked"
    METRIC_DERIVATIONS_RECOMPUTED = "metric_derivations_recomputed"


class ExecutionRecordStatus(ContractValue):
    NOT_RUN = "not_run"
    RECORDED_SUCCESS = "recorded_success"
    RECORDED_FAILURE = "recorded_failure"
    UNKNOWN = "unknown"


class ResultStatus(ContractValue):
    MATCHED = "matched"
    NOT_MATCHED = "not_matched"
    INDETERMINATE = "indeterminate"
    NOT_EVALUATED = "not_evaluated"


ASSURANCE_HIERARCHY = (
    AssuranceLevel.RECORDED,
    AssuranceLevel.BUNDLE_INTEGRITY_CHECKED,
    AssuranceLevel.METRIC_DERIVATIONS_RECOMPUTED,
)

DEPRECATED_VERIFICATION_FIELDS = ("status", "passed", "preflight_passed")

NOT_ESTABLISHED = {
    "execution_authenticity": "not_established",
    "independent_replay": "not_performed",
    "scientific_reproduction": "not_established",
}


def highest_assurance_level(
    *,
    bundle_integrity_checked: bool,
    metric_derivations_recomputed: bool,
    declared_metric_count: int,
) -> AssuranceLevel:
    """Return the highest assurance allowed by verified capabilities."""

    if (
        isinstance(declared_metric_count, bool)
        or not isinstance(declared_metric_count, int)
        or declared_metric_count < 0
    ):
        raise ValueError("declared_metric_count must be a non-negative integer")
    if metric_derivations_recomputed and not bundle_integrity_checked:
        raise ValueError("metric derivation assurance requires bundle integrity assurance")
    if bundle_integrity_checked and metric_derivations_recomputed and declared_metric_count > 0:
        return AssuranceLevel.METRIC_DERIVATIONS_RECOMPUTED
    if bundle_integrity_checked:
        return AssuranceLevel.BUNDLE_INTEGRITY_CHECKED
    return AssuranceLevel.RECORDED


def recorded_execution_status(run: dict[str, Any], commands: Sequence[dict[str, Any]]) -> ExecutionRecordStatus:
    """Interpret producer command records without claiming execution authenticity."""

    if run.get("dry_run") is True:
        return ExecutionRecordStatus.NOT_RUN
    if not commands:
        return ExecutionRecordStatus.UNKNOWN
    successful = all(
        command.get("status") == "completed" and command.get("return_code") == 0 for command in commands
    )
    return ExecutionRecordStatus.RECORDED_SUCCESS if successful else ExecutionRecordStatus.RECORDED_FAILURE


def coverage_skeleton(
    *,
    source: dict[str, Any],
    inputs: Sequence[dict[str, Any]],
    artifacts: Sequence[dict[str, Any]],
    metrics: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Describe Stage-1 coverage without implying bundle-local closure."""

    artifact_records = sum(
        len(declaration.get("matches", []))
        for declaration in artifacts
        if isinstance(declaration, dict) and isinstance(declaration.get("matches", []), list)
    )
    source_coverage = source.get("coverage")
    replay = source_coverage.get("replay", "unknown") if isinstance(source_coverage, dict) else "unknown"
    return {
        "inputs": {
            "bundle_local": 0,
            "external_metadata_only": len(inputs),
        },
        "artifacts": {
            "bundle_local": 0,
            "external_metadata_only": artifact_records,
        },
        "metric_sources": {
            "captured": 0,
            "total": len(metrics),
        },
        "source": {
            "replay": replay,
        },
    }
