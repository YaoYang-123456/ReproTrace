from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

import reprotrace.derived_outputs as derived_outputs_module
import reprotrace.evidence as evidence_module
import reprotrace.metrics as metrics_module
import reprotrace.operations as operations_module
import reprotrace.runner as runner_module
import reprotrace.verifier as verifier_module
from reprotrace.acquisition import EvidenceAcquisitionError
from reprotrace.cli import main as cli_main
from reprotrace.errors import ConfigError
from reprotrace.evidence import read_evidence_index, write_evidence_index
from reprotrace.io import read_json, sha256_bytes, write_json
from reprotrace.operations import _VerifyReportTestHooks, verify_and_report_bundle
from reprotrace.reporting import generate_report
from reprotrace.runner import run_manifest
from reprotrace.snapshot import SessionState, SnapshotStateError
from reprotrace.verifier import (
    _VerifyBundleTestHooks,
    _verify_bundle_with_hooks,
    verify_bundle,
)


def make_manifest(
    project: Path,
    *,
    metrics: bool = True,
    expected: float = 3.0,
    exit_code: int = 0,
) -> Path:
    project.mkdir(parents=True, exist_ok=True)
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
    step: dict[str, Any] = {"id": "produce", "argv": [sys.executable, "-c", command]}
    if metrics:
        step["artifacts"] = ["{run_dir}/artifacts/metrics.csv"]
    manifest: dict[str, Any] = {
        "schema_version": 0,
        "project": {"name": "snapshot-verifier", "root": "."},
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


def make_bundle(
    tmp_path: Path,
    *,
    metrics: bool = True,
    expected: float = 3.0,
    dry_run: bool = False,
) -> tuple[Path, dict[str, Any]]:
    return run_manifest(
        make_manifest(tmp_path / "project", metrics=metrics, expected=expected),
        dry_run=dry_run,
    )


def refresh_index(run_dir: Path) -> str:
    index = read_evidence_index(run_dir)
    _, root = write_evidence_index(
        run_dir,
        [{"path": entry["path"], "roles": entry["roles"]} for entry in index["entries"]],
    )
    return root


def check_by_id(verification: dict[str, Any], check_id: str) -> dict[str, Any]:
    return next(check for check in verification["checks"] if check["id"] == check_id)


def stable_verification(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result.pop("verified_at", None)
    return result


def test_production_verify_schema_one_uses_established_snapshot(tmp_path: Path) -> None:
    run_dir, expected = make_bundle(tmp_path)

    actual = verify_bundle(run_dir, write=False)

    assert stable_verification(actual) == stable_verification(expected)
    assert actual["verification_status"] == "complete"
    assert actual["assurance_level"] == "metric_derivations_recomputed"
    assert actual["result_status"] == "matched"


def test_production_verify_has_no_post_snapshot_live_content_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, _ = make_bundle(tmp_path)

    def forbid(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("post-snapshot live evidence read")

    def block_live_reads(session: Any) -> None:
        monkeypatch.setattr(verifier_module, "read_json", forbid)
        monkeypatch.setattr(verifier_module, "read_source_record", forbid)
        monkeypatch.setattr(verifier_module, "resolve_bundle_file", forbid)
        monkeypatch.setattr(verifier_module, "sha256_file", forbid)
        monkeypatch.setattr(evidence_module, "read_evidence_index", forbid)
        monkeypatch.setattr(evidence_module, "validate_evidence_index", forbid)
        monkeypatch.setattr(Path, "read_text", forbid)
        monkeypatch.setattr(Path, "read_bytes", forbid)

    result = _verify_bundle_with_hooks(
        run_dir,
        write=False,
        _hooks=_VerifyBundleTestHooks(after_snapshot_open=block_live_reads),
    )

    assert result["verification_status"] == "complete"


@pytest.mark.parametrize("record_name", ["commands.json", "metrics.json"])
def test_live_core_mutation_after_snapshot_does_not_change_verification(
    tmp_path: Path, record_name: str
) -> None:
    run_dir, expected = make_bundle(tmp_path)

    def mutate(session: Any) -> None:
        write_json(run_dir / record_name, [])

    actual = _verify_bundle_with_hooks(
        run_dir,
        write=False,
        _hooks=_VerifyBundleTestHooks(after_snapshot_open=mutate),
    )

    assert stable_verification(actual) == stable_verification(expected)


@pytest.mark.parametrize("action", ["delete", "replace"])
def test_live_metric_source_change_after_snapshot_uses_retained_source(
    tmp_path: Path, action: str
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    source = run_dir / "artifacts" / "metrics.csv"

    def mutate(session: Any) -> None:
        if action == "delete":
            source.unlink()
        else:
            source.write_text("score\n99.0\n", encoding="utf-8")

    result = _verify_bundle_with_hooks(
        run_dir,
        write=False,
        _hooks=_VerifyBundleTestHooks(after_snapshot_open=mutate),
    )

    assert check_by_id(result, "metric:score:derived-match")["recomputed_actual"] == 3.0
    assert result["result_status"] == "matched"


def test_live_index_replacement_after_snapshot_keeps_captured_root(tmp_path: Path) -> None:
    run_dir, expected = make_bundle(tmp_path)
    captured_root = expected["evidence_root_sha256"]

    def mutate(session: Any) -> None:
        (run_dir / "evidence.index.json").write_bytes(b"{}")

    result = _verify_bundle_with_hooks(
        run_dir,
        write=False,
        _hooks=_VerifyBundleTestHooks(after_snapshot_open=mutate),
    )

    assert result["evidence_root_sha256"] == captured_root
    assert result["verification_status"] == "complete"


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=False,
    )


def make_dirty_source_bundle(tmp_path: Path) -> Path:
    project = tmp_path / "git-project"
    project.mkdir()
    git(project, "init", "-q", "-b", "main")
    git(project, "config", "user.name", "Test")
    git(project, "config", "user.email", "test@example.com")
    (project / ".gitignore").write_text(".evidence/\n", encoding="utf-8")
    (project / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "base")
    (project / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run_dir, _ = run_manifest(make_manifest(project, metrics=False))
    return run_dir


def test_source_patch_and_status_checks_use_snapshot_objects(tmp_path: Path) -> None:
    run_dir = make_dirty_source_bundle(tmp_path)

    def mutate(session: Any) -> None:
        (run_dir / "source.patch").unlink()
        (run_dir / "source.status").write_bytes(b"changed-live-status")

    result = _verify_bundle_with_hooks(
        run_dir,
        write=False,
        _hooks=_VerifyBundleTestHooks(after_snapshot_open=mutate),
    )

    assert check_by_id(result, "source:git_patch")["passed"] is True
    assert check_by_id(result, "source:git_status")["passed"] is True


def test_bundle_file_current_values_are_snapshot_observations(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)

    def mutate(session: Any) -> None:
        (run_dir / "artifacts" / "metrics.csv").write_text(
            "score\n77.0\n", encoding="utf-8"
        )

    result = _verify_bundle_with_hooks(
        run_dir,
        write=False,
        _hooks=_VerifyBundleTestHooks(after_snapshot_open=mutate),
    )
    check = check_by_id(result, "bundle:file:artifacts/metrics.csv")

    assert check["passed"] is True
    assert check["current"] == check["recorded"] | {"exists": True}


def test_index_and_closure_checks_project_snapshot_state(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    result = verify_bundle(run_dir, write=False)

    assert check_by_id(result, "bundle:index")["passed"] is True
    assert check_by_id(result, "bundle:closure")["passed"] is True
    for check_id in (
        "input:declaration-closure",
        "artifact:declaration-closure",
        "artifact:bundle-scope-closure",
        "command:protocol-closure",
        "closure:command-log:produce:stdout",
        "closure:command-log:produce:stderr",
        "closure:artifact:produce:0:0",
    ):
        assert check_by_id(result, check_id)["passed"] is True


def test_production_metric_route_never_uses_legacy_path_extractor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, _ = make_bundle(tmp_path)

    def forbid(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("legacy path metric extractor used")

    monkeypatch.setattr(metrics_module, "extract_metric_from_evidence", forbid)
    result = verify_bundle(run_dir, write=False)

    assert result["assurance_level"] == "metric_derivations_recomputed"


@pytest.mark.parametrize("dry_run", [False, True])
def test_zero_metric_and_dry_run_do_not_force_snapshot_metric_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    run_dir, _ = make_bundle(tmp_path, metrics=False, dry_run=dry_run)

    def forbid(session: Any) -> Any:
        raise AssertionError("metric derivation is not applicable")

    monkeypatch.setattr(verifier_module, "extract_metrics_from_snapshot", forbid)
    result = verify_bundle(run_dir, write=False)

    assert result["assurance_level"] == "bundle_integrity_checked"
    assert result["result_status"] == "not_evaluated"
    assert result["execution_record_status"] == ("not_run" if dry_run else "recorded_success")


def test_recorded_metric_mismatch_preserves_existing_result_semantics(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    metrics = read_json(run_dir / "metrics.json")
    metrics[0].update(actual=9.0, absolute_error=6.0, passed=False)
    write_json(run_dir / "metrics.json", metrics)
    refresh_index(run_dir)

    result = verify_bundle(run_dir, write=False)

    assert result["assurance_level"] == "bundle_integrity_checked"
    assert result["verification_status"] == "incomplete"
    assert result["result_status"] == "indeterminate"


def test_expected_result_not_matched_remains_complete(tmp_path: Path) -> None:
    _, result = make_bundle(tmp_path, expected=4.0)

    assert result["verification_status"] == "complete"
    assert result["assurance_level"] == "metric_derivations_recomputed"
    assert result["result_status"] == "not_matched"


def test_report_uses_same_snapshot_after_core_and_metric_mutation(tmp_path: Path) -> None:
    run_dir, expected = make_bundle(tmp_path)

    def mutate(session: Any, verification: Any) -> None:
        write_json(run_dir / "commands.json", [])
        forged = read_json(run_dir / "metrics.json")
        forged[0].update(actual=99.0, absolute_error=96.0, passed=False)
        write_json(run_dir / "metrics.json", forged)
        (run_dir / "artifacts" / "metrics.csv").write_text(
            "score\n99.0\n", encoding="utf-8"
        )

    operation = verify_and_report_bundle(
        run_dir,
        _hooks=_VerifyReportTestHooks(after_verification_before_report=mutate),
    )
    report = operation.report_path.read_text(encoding="utf-8")

    assert stable_verification(operation.verification) == stable_verification(expected)
    assert "| produce | completed | 0 |" in report
    assert "| score | 3.0 | 3.0 | 3.0 |" in report
    assert "99.0" not in report


def test_report_has_no_post_snapshot_live_evidence_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, _ = make_bundle(tmp_path)

    def forbid(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("post-snapshot live report read")

    def block(session: Any) -> None:
        monkeypatch.setattr(verifier_module, "read_json", forbid)
        monkeypatch.setattr(verifier_module, "read_source_record", forbid)
        monkeypatch.setattr(verifier_module, "resolve_bundle_file", forbid)
        monkeypatch.setattr(verifier_module, "sha256_file", forbid)
        monkeypatch.setattr(evidence_module, "read_evidence_index", forbid)
        monkeypatch.setattr(evidence_module, "validate_evidence_index", forbid)
        monkeypatch.setattr(Path, "read_text", forbid)
        monkeypatch.setattr(Path, "read_bytes", forbid)

    operation = verify_and_report_bundle(
        run_dir,
        _hooks=_VerifyReportTestHooks(after_snapshot_open=block),
    )

    assert operation.verification["verification_status"] == "complete"
    assert operation.report_path.is_file()


def test_standalone_generate_report_opens_one_fresh_schema_one_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    original = operations_module._open_schema_one_snapshot_for_production
    calls = 0

    def counted(directory: Path) -> Any:
        nonlocal calls
        calls += 1
        return original(directory)

    monkeypatch.setattr(operations_module, "_open_schema_one_snapshot_for_production", counted)

    assert generate_report(run_dir).is_file()
    assert calls == 1


@pytest.mark.parametrize("command", ["verify", "report"])
def test_cli_verify_and_report_use_one_combined_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    original = operations_module._open_schema_one_snapshot_for_production
    calls = 0

    def counted(directory: Path) -> Any:
        nonlocal calls
        calls += 1
        return original(directory)

    monkeypatch.setattr(operations_module, "_open_schema_one_snapshot_for_production", counted)

    assert cli_main([command, str(run_dir)]) == 0
    capsys.readouterr()
    assert calls == 1


@pytest.mark.parametrize("dry_run", [False, True])
def test_runner_finalizes_executed_and_dry_runs_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    original = runner_module.verify_and_report_bundle
    calls = 0

    def counted(run_dir: Path) -> Any:
        nonlocal calls
        calls += 1
        return original(run_dir)

    monkeypatch.setattr(runner_module, "verify_and_report_bundle", counted)

    run_dir, result = run_manifest(
        make_manifest(tmp_path / "project", metrics=not dry_run),
        dry_run=dry_run,
    )

    assert calls == 1
    assert (run_dir / "verification.json").is_file()
    assert (run_dir / "report.md").is_file()
    assert result["execution_record_status"] == ("not_run" if dry_run else "recorded_success")


def test_standalone_verify_bundle_write_false_closes_without_output(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    (run_dir / "verification.json").unlink()
    captured: list[Any] = []

    result = _verify_bundle_with_hooks(
        run_dir,
        write=False,
        _hooks=_VerifyBundleTestHooks(after_snapshot_open=captured.append),
    )

    assert result["verification_status"] == "complete"
    assert not (run_dir / "verification.json").exists()
    assert captured[0].state is SessionState.CLOSED


def test_standalone_verify_bundle_write_true_uses_atomic_safe_output(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    (run_dir / "verification.json").unlink()

    result = verify_bundle(run_dir, write=True)

    assert read_json(run_dir / "verification.json") == result


def _replace_bundle_root(run_dir: Path, parked: Path) -> None:
    run_dir.replace(parked)
    run_dir.mkdir()


def test_root_replacement_before_verification_write_fails_closed(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    parked = run_dir.with_name(run_dir.name + "-parked-verification")

    def replace(session: Any, verification: Any) -> None:
        _replace_bundle_root(run_dir, parked)

    with pytest.raises(ConfigError, match="root identity differs"):
        _verify_bundle_with_hooks(
            run_dir,
            write=True,
            _hooks=_VerifyBundleTestHooks(before_verification_write=replace),
        )

    assert not (run_dir / "verification.json").exists()


def test_root_replacement_before_report_write_fails_closed(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    parked = run_dir.with_name(run_dir.name + "-parked-report")

    def replace(session: Any, verification: Any, report: str) -> None:
        _replace_bundle_root(run_dir, parked)

    with pytest.raises(ConfigError, match="root identity differs"):
        verify_and_report_bundle(
            run_dir,
            _hooks=_VerifyReportTestHooks(before_report_write=replace),
        )

    assert not (run_dir / "report.md").exists()
    assert (parked / "verification.json").is_file()


def test_root_identity_unavailable_before_derived_write_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, _ = make_bundle(tmp_path)

    def unavailable(path: Any) -> Any:
        raise EvidenceAcquisitionError("identity_unavailable", "simulated")

    monkeypatch.setattr(derived_outputs_module, "capture_bundle_root_identity", unavailable)

    with pytest.raises(ConfigError, match="identity is unavailable"):
        verify_bundle(run_dir)


def test_derived_outputs_remain_unindexed_and_root_stable(tmp_path: Path) -> None:
    run_dir, initial = make_bundle(tmp_path)
    index_before = (run_dir / "evidence.index.json").read_bytes()
    root_before = initial["evidence_root_sha256"]

    first = verify_and_report_bundle(run_dir)
    second = verify_and_report_bundle(run_dir)
    index_after = (run_dir / "evidence.index.json").read_bytes()
    paths = {entry["path"] for entry in read_evidence_index(run_dir)["entries"]}

    assert index_after == index_before
    assert first.verification["evidence_root_sha256"] == root_before
    assert second.verification["evidence_root_sha256"] == root_before
    assert {"verification.json", "report.md", "evidence.index.json"}.isdisjoint(paths)


@pytest.mark.parametrize("failure", ["verification", "report"])
def test_operation_failure_closes_snapshot_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    captured: list[Any] = []

    if failure == "verification":
        def fail_verification(session: Any) -> Any:
            raise ConfigError("simulated verification failure")

        monkeypatch.setattr(operations_module, "verify_snapshot_session", fail_verification)
    else:
        def fail_report(evidence: Any, verification: Any) -> Any:
            raise ConfigError("simulated report failure")

        monkeypatch.setattr(operations_module, "render_report", fail_report)

    with pytest.raises(ConfigError, match=f"simulated {failure} failure"):
        verify_and_report_bundle(
            run_dir,
            _hooks=_VerifyReportTestHooks(after_snapshot_open=captured.append),
        )

    assert captured[0].state is SessionState.CLOSED


def test_schema_one_snapshot_failure_never_falls_back_to_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    (run_dir / "metrics.json").write_bytes(b"[]")

    def legacy_forbidden(directory: Path) -> Any:
        raise AssertionError("legacy fallback used")

    monkeypatch.setattr(verifier_module, "_verify_legacy_directory", legacy_forbidden)

    with pytest.raises(ConfigError, match="cannot establish schema-1 evidence snapshot"):
        verify_bundle(run_dir, write=False)


def test_schema_zero_dispatch_verify_report_and_cli_exit_remain_legacy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, _ = make_bundle(tmp_path, metrics=False)
    run = read_json(run_dir / "run.json")
    run["schema_version"] = 0
    write_json(run_dir / "run.json", run)
    (run_dir / "evidence.index.json").unlink()

    result = verify_bundle(run_dir, write=False)
    report_path = generate_report(run_dir)
    exit_code = cli_main(["verify", str(run_dir)])
    capsys.readouterr()

    assert result["assurance_level"] == "recorded"
    assert result["result_status"] == "not_evaluated"
    assert report_path.is_file()
    assert exit_code == 0


def test_schema_one_production_opens_run_json_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    import reprotrace.acquisition as acquisition_module

    original_open = acquisition_module.os.open
    run_opens = 0

    def counted(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal run_opens
        if Path(path).name == "run.json":
            run_opens += 1
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(acquisition_module.os, "open", counted)

    verify_bundle(run_dir, write=False)

    assert run_opens == 1


def test_same_session_root_is_used_by_verification_and_report(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    captured_roots: list[str] = []

    def capture(session: Any, verification: Any) -> None:
        captured_roots.append(session.snapshot.require_established_evidence_root())
        captured_roots.append(verification["evidence_root_sha256"])

    operation = verify_and_report_bundle(
        run_dir,
        _hooks=_VerifyReportTestHooks(after_verification_before_report=capture),
    )
    report = operation.report_path.read_text(encoding="utf-8")

    assert captured_roots[0] == captured_roots[1]
    assert captured_roots[0] in report


def test_post_session_metric_reader_is_unavailable(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    captured: list[Any] = []

    def capture(session: Any) -> None:
        captured.append(session.snapshot.objects["artifacts/metrics.csv"])

    verify_and_report_bundle(
        run_dir,
        _hooks=_VerifyReportTestHooks(after_snapshot_open=capture),
    )

    with pytest.raises(SnapshotStateError, match="closed"):
        captured[0].open_reader()


def test_original_h1_shape_is_bound_to_captured_state_a(tmp_path: Path) -> None:
    run_dir, baseline = make_bundle(tmp_path)
    root_a = baseline["evidence_root_sha256"]

    def replace_with_b(session: Any, verification: Any) -> None:
        (run_dir / "artifacts" / "metrics.csv").write_text(
            "score\n88.0\n", encoding="utf-8"
        )
        metrics_b = read_json(run_dir / "metrics.json")
        metrics_b[0].update(actual=88.0, absolute_error=85.0, passed=False)
        write_json(run_dir / "metrics.json", metrics_b)
        (run_dir / "evidence.index.json").write_bytes(b'{"state":"B"}')

    operation = verify_and_report_bundle(
        run_dir,
        _hooks=_VerifyReportTestHooks(after_verification_before_report=replace_with_b),
    )
    report = operation.report_path.read_text(encoding="utf-8")

    assert operation.verification["evidence_root_sha256"] == root_a
    assert check_by_id(
        operation.verification, "metric:score:derived-match"
    )["recomputed_actual"] == 3.0
    assert "| score | 3.0 | 3.0 | 3.0 |" in report
    assert "88.0" not in report


def test_non_ascii_schema_one_verify_and_report_is_utf8_explicit(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    run = read_json(run_dir / "run.json")
    run["project_name"] = "证据快照"
    write_json(run_dir / "run.json", run)
    refresh_index(run_dir)

    operation = verify_and_report_bundle(run_dir)
    report = operation.report_path.read_text(encoding="utf-8")

    assert operation.verification["verification_status"] == "complete"
    assert "证据快照" in report
