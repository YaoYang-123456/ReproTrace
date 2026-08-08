"""Human-readable presentation of canonical verification results."""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .io import read_json, read_source_record


def _value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _contract_display(value: Any) -> str:
    return _value(value).replace("_", " ").upper()


def _cell(value: Any) -> str:
    return _value(value).replace("|", "\\|").replace("\n", " ")


def _metric_id(check: Mapping[str, Any], suffix: str) -> str | None:
    check_id = check.get("id")
    if not isinstance(check_id, str) or not check_id.startswith("metric:"):
        return None
    ending = f":{suffix}"
    if not check_id.endswith(ending):
        return None
    value = check_id[len("metric:") : -len(ending)]
    return value or None


def _metric_table(
    metrics: Sequence[Mapping[str, Any]],
    verification: Mapping[str, Any],
    *,
    legacy_bundle: bool,
) -> list[str]:
    contract_checks = verification.get("contract_checks", [])
    checks = verification.get("checks", [])
    if not isinstance(contract_checks, list):
        contract_checks = []
    if not isinstance(checks, list):
        checks = []

    derivations: dict[str, Mapping[str, Any]] = {}
    expectations: dict[str, Mapping[str, Any]] = {}
    ordered_ids: list[str] = []
    recorded_by_id: dict[str, Mapping[str, Any]] = {}
    for metric in metrics:
        metric_id = metric.get("id")
        if isinstance(metric_id, str) and metric_id:
            recorded_by_id.setdefault(metric_id, metric)
            if metric_id not in ordered_ids:
                ordered_ids.append(metric_id)
    for check in contract_checks:
        if not isinstance(check, Mapping):
            continue
        metric_id = _metric_id(check, "derived-match")
        if metric_id is not None:
            derivations[metric_id] = check
            if metric_id not in ordered_ids:
                ordered_ids.append(metric_id)
    for check in checks:
        if not isinstance(check, Mapping):
            continue
        metric_id = _metric_id(check, "expectation")
        if metric_id is not None:
            expectations[metric_id] = check
            if metric_id not in ordered_ids:
                ordered_ids.append(metric_id)

    lines = [
        "| Metric | Recorded actual | Recomputed actual | Expected from manifest | atol | rtol | Result |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    if not ordered_ids:
        lines.append("| — | — | — | — | — | — | not_evaluated |")
        return lines

    for metric_id in ordered_ids:
        recorded = recorded_by_id.get(metric_id, {})
        derivation = derivations.get(metric_id)
        expectation = expectations.get(metric_id)
        if legacy_bundle:
            result = "not_evaluated"
        elif derivation is None or derivation.get("passed") is not True:
            result = "indeterminate"
        elif expectation is not None and expectation.get("passed") is True:
            result = "matched"
        else:
            result = "not_matched"
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(metric_id),
                    _cell(recorded.get("actual")),
                    _cell(None if derivation is None else derivation.get("recomputed_actual")),
                    _cell(
                        recorded.get("expected")
                        if expectation is None
                        else expectation.get("expected")
                    ),
                    _cell(recorded.get("atol") if expectation is None else expectation.get("atol")),
                    _cell(recorded.get("rtol") if expectation is None else expectation.get("rtol")),
                    result,
                )
            )
            + " |"
        )
    return lines


def _coverage_total(record: Mapping[str, Any]) -> int:
    total = record.get("total")
    if isinstance(total, int) and not isinstance(total, bool):
        return total
    return sum(
        value
        for key in ("bundle_local", "external_metadata_only")
        if isinstance((value := record.get(key)), int) and not isinstance(value, bool)
    )


def _append_check_table(
    lines: list[str],
    title: str,
    checks: Sequence[Mapping[str, Any]],
) -> None:
    lines.extend(["", f"## {title}", ""])
    if not checks:
        lines.append("No applicable checks were recorded.")
        return
    passed = sum(check.get("passed") is True for check in checks)
    lines.extend(
        [
            f"{passed}/{len(checks)} checks passed.",
            "",
            "| Check | Kind | Category | Result | Detail |",
            "|---|---|---|---|---|",
        ]
    )
    for check in checks:
        detail = check.get("reason") or ""
        lines.append(
            f"| `{_cell(check.get('id'))}` | `{_cell(check.get('kind'))}` | "
            f"`{_cell(check.get('category'))}` | "
            f"{'PASS' if check.get('passed') is True else 'FAIL'} | {_cell(detail)} |"
        )


def generate_report(
    run_dir: str | Path,
    *,
    _verification: Mapping[str, Any] | None = None,
) -> Path:
    """Generate report.md from a fresh verifier result and bundle-local records.

    Public calls refresh verification. The private result injection is used only
    by runner/CLI paths that have just called ``verify_bundle``.
    """

    directory = Path(run_dir).expanduser().resolve()
    if _verification is None:
        from .verifier import verify_bundle

        verification: Mapping[str, Any] = verify_bundle(directory)
    else:
        verification = _verification
    if not isinstance(verification, Mapping):
        raise ConfigError("cannot report invalid verification result; expected an object")

    names = ["run", "source", "environment", "inputs", "commands", "artifacts", "metrics"]
    evidence: dict[str, Any] = {}
    missing = []
    for name in names:
        path = directory / f"{name}.json"
        if not path.is_file():
            missing.append(path.name)
        else:
            evidence[name] = read_source_record(path) if name == "source" else read_json(path)
    if missing:
        raise ConfigError(f"cannot report invalid evidence bundle; missing: {', '.join(missing)}")

    run = evidence["run"]
    source = evidence["source"]
    environment = evidence["environment"]
    if not isinstance(run, dict) or not isinstance(environment, dict):
        raise ConfigError("cannot report invalid run/environment evidence")
    if not all(isinstance(evidence[name], list) for name in ("inputs", "commands", "artifacts", "metrics")):
        raise ConfigError("cannot report invalid list evidence records")
    legacy_bundle = run.get("schema_version", 0) == 0
    claim = run.get("claim", {})
    if not isinstance(claim, Mapping):
        claim = {}
    not_established = verification.get("not_established", {})
    if not isinstance(not_established, Mapping):
        not_established = {}
    evidence_root = verification.get("evidence_root_sha256")
    checks_passed = verification.get("checks_passed") is True

    lines = [
        f"# ReproTrace report: {run.get('project_name', run.get('run_id'))}",
        "",
        f"**Bundle type:** {'Legacy bundle (run schema 0)' if legacy_bundle else 'Run schema 1'}  ",
        f"**Verification:** `{_contract_display(verification.get('verification_status'))}`  ",
        f"**Checks:** `{'PASS' if checks_passed else 'FAIL'}`  ",
        f"**Assurance:** `{verification.get('assurance_level')}`  ",
        f"**Recorded execution:** `{verification.get('execution_record_status')}`  ",
        f"**Declared result:** `{verification.get('result_status')}`  ",
        "**Execution authenticity:** "
        f"`{_contract_display(not_established.get('execution_authenticity', 'not_established'))}`  ",
        "**Independent replay:** "
        f"`{_contract_display(not_established.get('independent_replay', 'not_performed'))}`  ",
        "**Scientific reproduction:** "
        f"`{_contract_display(not_established.get('scientific_reproduction', 'not_established'))}`  ",
        f"**Evidence root SHA-256:** `{evidence_root if evidence_root else 'NOT VERIFIED'}`  ",
        f"**Run ID:** `{run.get('run_id')}`  ",
        f"**Dry run:** {_value(run.get('dry_run'))}  ",
        f"**Started:** {_value(run.get('started_at'))}  ",
        f"**Finished:** {_value(run.get('finished_at'))}",
    ]
    if run.get("dry_run") is True:
        lines.extend(
            [
                "",
                "**Planning context:** This is a dry-run planning bundle; no experiment execution "
                "or evaluated result is claimed.",
            ]
        )

    lines.extend(
        [
            "",
            "## Claim",
            "",
            f"- ID: `{claim.get('id', 'unspecified')}`",
            f"- Source: {_value(claim.get('source'))}",
            f"- Locator: {_value(claim.get('locator'))}",
            f"- Protocol: {_value(claim.get('protocol'))}",
            "",
            "## Source and environment",
            "",
            f"- Source available: {_value(source.get('available'))}",
            f"- Source unavailable reason: {_value(source.get('reason'))}",
            f"- Commit: `{_value(source.get('commit'))}`",
            f"- Branch: `{_value(source.get('branch'))}`",
            f"- Remote: {_value(source.get('remote'))}",
            f"- Dirty: {_value(source.get('dirty'))}",
            f"- Python: `{environment.get('python')}`",
            f"- Platform: `{environment.get('platform')}`",
        ]
    )
    git_patch = source.get("git_patch")
    git_status = source.get("git_status")
    if source.get("schema_version") == 1 and source.get("available"):
        if not isinstance(git_patch, dict) or not isinstance(git_status, dict):
            raise ConfigError(
                "cannot report invalid source.json; Git patch/status metadata must be objects"
            )
        git_metadata = source["git"]
        source_coverage = source["coverage"]
        summary = source["summary"]
        lines.extend(
            [
                f"- Git: `{git_metadata.get('version')}`",
                f"- Source replay coverage: `{source_coverage.get('replay', 'partial')}` "
                "(tracked changes only)",
                f"- Source patch: `{git_patch.get('path')}`; format `{git_patch.get('format')}`; "
                f"{git_patch.get('size_bytes')} bytes; SHA-256 `{git_patch.get('sha256')}`",
                f"- Source status: `{git_status.get('path')}`; format `{git_status.get('format')}`; "
                f"{git_status.get('size_bytes')} bytes; SHA-256 `{git_status.get('sha256')}`",
            ]
        )
        untracked_count = summary.get("untracked_file_count", 0)
        if untracked_count:
            lines.append(
                f"- **WARNING:** {untracked_count} untracked path(s) were recorded by name/status only; "
                "their contents are absent, so this bundle cannot fully replay the source worktree."
            )

    lines.extend(
        [
            "",
            "## Recorded command outcomes",
            "",
            "These are producer-recorded outcomes; they do not establish that a process actually ran.",
            "",
            "| Step | Recorded status | Recorded exit code | Elapsed (s) | Command |",
            "|---|---|---:|---:|---|",
        ]
    )
    for command in evidence["commands"]:
        argv_values = command.get("argv", [])
        if not isinstance(argv_values, list):
            argv_values = []
        argv = " ".join(shlex.quote(_value(item)) for item in argv_values)
        lines.append(
            f"| {_cell(command.get('step_id'))} | {_cell(command.get('status'))} | "
            f"{_cell(command.get('return_code'))} | {_cell(command.get('elapsed_seconds'))} | "
            f"`{_cell(argv)}` |"
        )

    lines.extend(["", "## Metrics", ""])
    lines.extend(
        _metric_table(
            evidence["metrics"],
            verification,
            legacy_bundle=legacy_bundle,
        )
    )

    coverage = verification.get("coverage", {})
    if not isinstance(coverage, Mapping):
        coverage = {}
    input_coverage = coverage.get("inputs", {})
    artifact_coverage = coverage.get("artifacts", {})
    metric_coverage = coverage.get("metric_sources", {})
    source_coverage = coverage.get("source", {})
    input_coverage = input_coverage if isinstance(input_coverage, Mapping) else {}
    artifact_coverage = artifact_coverage if isinstance(artifact_coverage, Mapping) else {}
    metric_coverage = metric_coverage if isinstance(metric_coverage, Mapping) else {}
    source_coverage = source_coverage if isinstance(source_coverage, Mapping) else {}
    lines.extend(
        [
            "",
            "## Evidence coverage",
            "",
            "### Inputs",
            "",
            f"- Total: {_coverage_total(input_coverage)}",
            f"- Bundle-local: {_value(input_coverage.get('bundle_local', 0))}",
            "- `external_metadata_only`: "
            f"{_value(input_coverage.get('external_metadata_only', 0))}",
            "",
            "### Artifacts",
            "",
            f"- Total: {_coverage_total(artifact_coverage)}",
            f"- Bundle-local: {_value(artifact_coverage.get('bundle_local', 0))}",
            "- `external_metadata_only`: "
            f"{_value(artifact_coverage.get('external_metadata_only', 0))}",
            "",
            "### Metric sources",
            "",
            f"- Declared metrics total: {_value(metric_coverage.get('total', 0))}",
            f"- Recorded derived metrics: {_value(metric_coverage.get('recorded', 0))}",
            f"- Captured metric sets: {_value(metric_coverage.get('captured', 0))}",
            f"- Source files captured: {_value(metric_coverage.get('source_files_captured', 0))}",
            "",
            "### Source",
            "",
            f"- Replay coverage: `{_value(source_coverage.get('replay', 'unknown'))}`",
            "",
            "`external_metadata_only` records describe origin metadata; they are not captured evidence bytes.",
        ]
    )

    contract_checks = verification.get("contract_checks", [])
    all_checks = verification.get("checks", [])
    contract_checks = (
        [check for check in contract_checks if isinstance(check, Mapping)]
        if isinstance(contract_checks, list)
        else []
    )
    compatibility_checks = (
        [
            check
            for check in all_checks
            if isinstance(check, Mapping) and check.get("canonical") is not True
        ]
        if isinstance(all_checks, list)
        else []
    )
    _append_check_table(lines, "Canonical verification checks", contract_checks)
    _append_check_table(
        lines,
        "Compatibility and recorded-result checks",
        compatibility_checks,
    )

    compatibility = verification.get("compatibility", {})
    if not isinstance(compatibility, Mapping):
        compatibility = {}
    lines.extend(
        [
            "",
            "## Compatibility",
            "",
            "The following fields preserve older caller behavior and are not canonical assurance conclusions.",
            "",
            f"- Legacy compatibility status: `{compatibility.get('legacy_status')}`",
            f"- Legacy compatibility passed: {_value(compatibility.get('legacy_passed'))}",
            "",
            "## Limitations",
            "",
            "This report does not establish:",
            "",
            "- Execution authenticity: **NOT ESTABLISHED**",
            "- Independent replay: **NOT PERFORMED**",
            "- Scientific reproduction: **NOT ESTABLISHED**",
            "- External inputs or artifacts may be metadata-only rather than captured evidence.",
        ]
    )
    if legacy_bundle:
        lines.extend(
            [
                "- `legacy_bundle_without_evidence_index`",
                "- `external_origin_paths_not_checked`",
                "- `metric_derivations_not_recomputed`",
            ]
        )
    lines.append("")

    path = directory / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
