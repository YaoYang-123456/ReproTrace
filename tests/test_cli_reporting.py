from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from reprotrace.cli import main as cli_main
from reprotrace.evidence import read_evidence_index, write_evidence_index
from reprotrace.io import read_json, write_json
from reprotrace.runner import run_manifest


def make_manifest(
    project: Path,
    *,
    expected: float = 3.0,
    exit_code: int = 0,
    metrics: bool = True,
) -> Path:
    project.mkdir(parents=True)
    (project / "input.txt").write_text("input\n", encoding="utf-8")
    command = (
        f"raise SystemExit({exit_code})"
        if exit_code
        else (
            "import os; from pathlib import Path; "
            "p=Path(os.environ['REPROTRACE_RUN_DIR'])/'artifacts'/'metrics.csv'; "
            "p.write_text('score\\n3.0\\n', encoding='utf-8')"
        )
    )
    step: dict[str, object] = {
        "id": "produce",
        "argv": [sys.executable, "-c", command],
    }
    if metrics:
        step["artifacts"] = ["{run_dir}/artifacts/metrics.csv"]
    manifest: dict[str, object] = {
        "schema_version": 0,
        "project": {"name": "stage-five", "root": "."},
        "inputs": [
            {"id": "input", "kind": "dataset", "path": "input.txt", "required": True}
        ],
        "run": {"output_root": ".evidence", "steps": [step]},
    }
    if metrics:
        manifest["metrics"] = [
            {
                "id": "score",
                "extractor": "csv",
                "path": "{run_dir}/artifacts/metrics.csv",
                "column": "score",
                "select": "last",
                "expected": expected,
                "atol": 0.0,
                "rtol": 0.0,
            }
        ]
    path = project / "reprotrace.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


def report_text(run_dir: Path) -> str:
    return (run_dir / "report.md").read_text(encoding="utf-8")


def refresh_index(run_dir: Path) -> None:
    index = read_evidence_index(run_dir)
    write_evidence_index(
        run_dir,
        [{"path": entry["path"], "roles": entry["roles"]} for entry in index["entries"]],
    )


def test_c1_run_matched_uses_canonical_output_and_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli_main(["run", str(make_manifest(tmp_path / "project"))])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "verification: COMPLETE" in output
    assert "checks: PASS" in output
    assert "assurance: metric_derivations_recomputed" in output
    assert "recorded execution: recorded_success" in output
    assert "declared result: matched" in output
    assert "execution authenticity: NOT ESTABLISHED" in output
    assert "independent replay: NOT PERFORMED" in output
    assert "scientific reproduction: NOT ESTABLISHED" in output
    assert "evidence root: NOT VERIFIED" not in output
    assert "bundle:" in output


def test_c2_expectation_miss_is_complete_but_run_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli_main(
        ["run", str(make_manifest(tmp_path / "project", expected=4.0))]
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "verification: COMPLETE" in output
    assert "checks: PASS" in output
    assert "assurance: metric_derivations_recomputed" in output
    assert "declared result: not_matched" in output
    assert "verification: INCOMPLETE" not in output


def test_c3_integrity_failure_verify_exit_one_and_root_not_verified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    (run_dir / "artifacts" / "metrics.csv").write_text(
        "score\n9.0\n", encoding="utf-8"
    )

    exit_code = cli_main(["verify", str(run_dir)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "verification: INCOMPLETE" in output
    assert "checks: FAIL" in output
    assert "evidence root: NOT VERIFIED" in output


def test_c3_derivation_failure_exits_one_without_losing_integrity_assurance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    metrics = read_json(run_dir / "metrics.json")
    metrics[0]["actual"] = 9.0
    metrics[0]["absolute_error"] = 6.0
    metrics[0]["passed"] = False
    write_json(run_dir / "metrics.json", metrics)
    refresh_index(run_dir)

    exit_code = cli_main(["verify", str(run_dir)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "verification: INCOMPLETE" in output
    assert "checks: FAIL" in output
    assert "assurance: bundle_integrity_checked" in output
    assert "declared result: indeterminate" in output
    assert "evidence root: NOT VERIFIED" not in output


def test_c4_invalid_bundle_exit_two_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid = tmp_path / "invalid"
    invalid.mkdir()

    exit_code = cli_main(["verify", str(invalid)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "reprotrace:" in captured.err
    assert "Traceback" not in captured.err


def test_c5_dry_run_preflight_exit_zero_and_planning_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli_main(
        ["run", str(make_manifest(tmp_path / "project")), "--dry-run"]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "verification: COMPLETE" in output
    assert "assurance: bundle_integrity_checked" in output
    assert "recorded execution: not_run" in output
    assert "declared result: not_evaluated" in output


def test_c6_recorded_command_failure_exits_one_without_verification_failure_claim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli_main(
        ["run", str(make_manifest(tmp_path / "project", exit_code=7))]
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "verification: COMPLETE" in output
    assert "checks: PASS" in output
    assert "assurance: bundle_integrity_checked" in output
    assert "recorded execution: recorded_failure" in output
    assert "declared result: indeterminate" in output


def test_c7_legacy_bundle_remains_readable_with_conservative_canonical_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project", metrics=False))
    run = read_json(run_dir / "run.json")
    run["schema_version"] = 0
    write_json(run_dir / "run.json", run)

    exit_code = cli_main(["verify", str(run_dir)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "verification: COMPLETE" in output
    assert "assurance: recorded" in output
    assert "declared result: not_evaluated" in output
    assert "evidence root: NOT VERIFIED" in output


def test_verify_json_preserves_full_canonical_and_compatibility_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))

    exit_code = cli_main(["verify", str(run_dir), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["verification_status"] == "complete"
    assert payload["checks_passed"] is True
    assert payload["assurance_level"] == "metric_derivations_recomputed"
    assert payload["execution_record_status"] == "recorded_success"
    assert payload["result_status"] == "matched"
    assert payload["evidence_root_sha256"]
    assert payload["not_established"]["scientific_reproduction"] == "not_established"
    assert {"status", "passed", "preflight_passed"} <= payload.keys()


def test_r1_schema_one_report_presents_canonical_dimensions_and_metric_derivation(
    tmp_path: Path,
) -> None:
    run_dir, verification = run_manifest(make_manifest(tmp_path / "project"))
    report = report_text(run_dir)

    assert "**Verification:** `COMPLETE`" in report
    assert "**Checks:** `PASS`" in report
    assert "**Assurance:** `metric_derivations_recomputed`" in report
    assert "**Recorded execution:** `recorded_success`" in report
    assert "**Declared result:** `matched`" in report
    assert "**Execution authenticity:** `NOT ESTABLISHED`" in report
    assert "**Independent replay:** `NOT PERFORMED`" in report
    assert "**Scientific reproduction:** `NOT ESTABLISHED`" in report
    assert verification["evidence_root_sha256"] in report
    assert "| score | 3.0 | 3.0 | 3.0 | 0.0 | 0.0 | matched |" in report
    assert "Decision:" not in report


def test_r2_expectation_miss_report_separates_verification_from_result(
    tmp_path: Path,
) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project", expected=4.0))
    report = report_text(run_dir)

    assert "**Verification:** `COMPLETE`" in report
    assert "**Checks:** `PASS`" in report
    assert "**Declared result:** `not_matched`" in report
    assert "| score | 3.0 | 3.0 | 4.0 | 0.0 | 0.0 | not_matched |" in report
    assert "| `metric:score:expectation` | `expectation` | `metric` | FAIL |" in report
    assert "Verification failed" not in report


def test_r3_legacy_report_is_conservative_and_labels_compatibility(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project", metrics=False))
    run = read_json(run_dir / "run.json")
    run["schema_version"] = 0
    write_json(run_dir / "run.json", run)

    exit_code = cli_main(["report", str(run_dir)])
    report = report_text(run_dir)
    capsys.readouterr()

    assert exit_code == 0
    assert "Legacy bundle (run schema 0)" in report
    assert "**Assurance:** `recorded`" in report
    assert "**Declared result:** `not_evaluated`" in report
    assert "Legacy compatibility status: `passed`" in report
    assert "`legacy_bundle_without_evidence_index`" in report
    assert "`external_origin_paths_not_checked`" in report
    assert "`metric_derivations_not_recomputed`" in report
    assert "Verification passed" not in report


def test_r4_dry_run_report_is_explicitly_planning_only(tmp_path: Path) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"), dry_run=True)
    report = report_text(run_dir)

    assert "**Dry run:** yes" in report
    assert "dry-run planning bundle" in report
    assert "**Recorded execution:** `not_run`" in report
    assert "**Declared result:** `not_evaluated`" in report
    assert "**Assurance:** `bundle_integrity_checked`" in report
    assert "Captured metric sets: 0" in report
    assert "Source files captured: 0" in report


def test_r5_coverage_distinguishes_metadata_from_captured_evidence(tmp_path: Path) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    report = report_text(run_dir)

    assert "### Inputs" in report
    assert "- Total: 1" in report
    assert "- Bundle-local: 0" in report
    assert "- `external_metadata_only`: 1" in report
    assert "### Artifacts" in report
    assert "- Bundle-local: 1" in report
    assert "- `external_metadata_only`: 0" in report
    assert "- Declared metrics total: 1" in report
    assert "- Recorded derived metrics: 1" in report
    assert "- Captured metric sets: 1" in report
    assert "- Source files captured: 1" in report
    assert "they are not captured evidence bytes" in report


def test_recorded_failure_report_keeps_canonical_verification_complete(tmp_path: Path) -> None:
    run_dir, _ = run_manifest(
        make_manifest(tmp_path / "project", exit_code=7)
    )
    report = report_text(run_dir)

    assert "**Verification:** `COMPLETE`" in report
    assert "**Checks:** `PASS`" in report
    assert "**Assurance:** `bundle_integrity_checked`" in report
    assert "**Recorded execution:** `recorded_failure`" in report
    assert "**Declared result:** `indeterminate`" in report
    assert "Verification failed" not in report


def test_r6_report_command_refreshes_stale_verification_after_tamper(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    assert "**Declared result:** `matched`" in report_text(run_dir)
    (run_dir / "artifacts" / "metrics.csv").write_text(
        "score\n9.0\n", encoding="utf-8"
    )

    exit_code = cli_main(["report", str(run_dir)])

    output = capsys.readouterr().out
    report = report_text(run_dir)
    verification = read_json(run_dir / "verification.json")
    assert exit_code == 1
    assert "verification: INCOMPLETE" in output
    assert verification["verification_status"] == "incomplete"
    assert verification["evidence_root_sha256"] is None
    assert "**Verification:** `INCOMPLETE`" in report
    assert "**Checks:** `FAIL`" in report
    assert "**Declared result:** `indeterminate`" in report
    assert "**Evidence root SHA-256:** `NOT VERIFIED`" in report
    assert "**Declared result:** `matched`" not in report
