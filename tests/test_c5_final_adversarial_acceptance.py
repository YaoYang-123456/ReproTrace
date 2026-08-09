from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

import reprotrace.acquisition as acquisition_module
import reprotrace.derived_outputs as derived_outputs_module
import reprotrace.evidence as evidence_module
import reprotrace.metrics as metrics_module
import reprotrace.operations as operations_module
import reprotrace.verifier as verifier_module
from reprotrace.acquisition import EvidenceAcquisitionError
from reprotrace.errors import ConfigError
from reprotrace.evidence import read_evidence_index, write_evidence_index
from reprotrace.io import read_json, sha256_bytes, sha256_file, write_json
from reprotrace.operations import _VerifyReportTestHooks, verify_and_report_bundle
from reprotrace.protocol import bundle_artifact_path_matches
from reprotrace.reporting import generate_report
from reprotrace.runner import run_manifest
from reprotrace.snapshot import SessionState, SnapshotStateError
from reprotrace.snapshot_builder import (
    SnapshotBuildError,
    _SnapshotBuildTestHooks,
    open_schema_one_snapshot,
)
from reprotrace.verifier import (
    _VerifyBundleTestHooks,
    _verify_bundle_with_hooks,
    verify_bundle,
)
from tests.test_assurance_verifier import (
    check_by_id as _check,
    make_manifest as _make_manifest,
    rebuild_index_from_records as _rebuild_index_from_records,
    refresh_index as _refresh_index,
)
from tests.test_snapshot_verifier_integration import (
    stable_verification as _stable_verification,
)


def _make_bundle(
    tmp_path: Path,
    *,
    metrics: bool = True,
    expected: float = 3.0,
    dry_run: bool = False,
    extra_artifact: bool = False,
) -> tuple[Path, dict[str, Any]]:
    return run_manifest(
        _make_manifest(
            tmp_path / "project",
            metrics=metrics,
            expected=expected,
            extra_artifact=extra_artifact,
        ),
        dry_run=dry_run,
    )


def _replace_bundle_root(run_dir: Path, parked: Path) -> None:
    run_dir.replace(parked)
    run_dir.mkdir()
    (run_dir / "replacement-root-b.txt").write_text("state B\n", encoding="utf-8")


def _attempt_guarded_root_replacement(run_dir: Path, parked: Path) -> bool:
    try:
        _replace_bundle_root(run_dir, parked)
    except PermissionError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) != 32:
            raise
        return False
    return True


def _rewrite_large_regex_metric(run_dir: Path) -> bytes:
    # Exceeds the production 8 MiB spool threshold without millions of CSV rows.
    payload = b"x" * (8 * 1024 * 1024 + 257) + b"\nscore=3.0\n"
    raw = run_dir / "artifacts" / "metrics.csv"
    raw.write_bytes(payload)
    size_bytes = len(payload)
    digest = sha256_bytes(payload)

    metric_sources = read_json(run_dir / "metric_sources.json")
    metric_sources["metrics"][0]["sources"][0].update(
        size_bytes=size_bytes,
        sha256=digest,
    )
    write_json(run_dir / "metric_sources.json", metric_sources)

    artifacts = read_json(run_dir / "artifacts.json")
    artifacts[0]["matches"][0].update(size_bytes=size_bytes, sha256=digest)
    write_json(run_dir / "artifacts.json", artifacts)

    resolved_path = run_dir / "manifest.resolved.yaml"
    resolved = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    metric = resolved["metrics"][0]
    metric.update(
        extractor="log_regex",
        pattern=r"score=([0-9.]+)",
        group=1,
    )
    metric.pop("column", None)
    resolved_path.write_text(yaml.safe_dump(resolved, sort_keys=False), encoding="utf-8")

    metrics = read_json(run_dir / "metrics.json")
    metrics[0]["extractor"] = "log_regex"
    write_json(run_dir / "metrics.json", metrics)
    _refresh_index(run_dir)
    return payload


def test_h1_production_snapshot_a_survives_live_multi_record_state_b(
    tmp_path: Path,
) -> None:
    """H1 A1-A4: all semantic conclusions remain bound to captured state A."""

    run_dir, baseline = _make_bundle(tmp_path)
    root_a = baseline["evidence_root_sha256"]

    def install_state_b(session: Any) -> None:
        (run_dir / "artifacts" / "metrics.csv").write_text(
            "score\n88.0\n", encoding="utf-8"
        )
        forged_metrics = read_json(run_dir / "metrics.json")
        forged_metrics[0].update(actual=88.0, absolute_error=85.0, passed=False)
        write_json(run_dir / "metrics.json", forged_metrics)
        write_json(run_dir / "commands.json", [])
        write_json(run_dir / "source.json", {"available": False, "reason": "state_b"})
        write_json(run_dir / "artifacts.json", [])
        (run_dir / "evidence.index.json").write_bytes(b'{"state":"B"}\n')

    operation = verify_and_report_bundle(
        run_dir,
        _hooks=_VerifyReportTestHooks(after_snapshot_open=install_state_b),
    )
    report = operation.report_path.read_text(encoding="utf-8")

    assert operation.verification["verification_status"] == "complete"
    assert operation.verification["evidence_root_sha256"] == root_a
    assert _check(
        operation.verification, "metric:score:derived-match"
    )["recomputed_actual"] == 3.0
    assert operation.verification["result_status"] == "matched"
    assert "88.0" not in report
    assert root_a in report


def test_h1_source_patch_status_and_live_tree_loss_use_captured_objects(
    tmp_path: Path,
) -> None:
    """H1 A5/A12: sealed source/core evidence remains usable after live loss."""

    project = tmp_path / "git-project"
    project.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.name", "Test"),
        ("config", "user.email", "test@example.com"),
    ):
        subprocess.run(
            ["git", "-C", str(project), *args],
            check=True,
            capture_output=True,
            text=False,
        )
    (project / ".gitignore").write_text(".evidence/\n", encoding="utf-8")
    (project / "input.txt").write_text("producer input\n", encoding="utf-8")
    (project / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(project), "add", "."],
        check=True,
        capture_output=True,
        text=False,
    )
    subprocess.run(
        ["git", "-C", str(project), "commit", "-q", "-m", "base"],
        check=True,
        capture_output=True,
        text=False,
    )
    (project / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run_dir, _ = run_manifest(_make_manifest(project, metrics=False))
    indexed_paths = [entry["path"] for entry in read_evidence_index(run_dir)["entries"]]

    def remove_live_evidence(session: Any) -> None:
        for relative_path in indexed_paths:
            candidate = run_dir.joinpath(*relative_path.split("/"))
            candidate.unlink(missing_ok=True)
        (run_dir / "evidence.index.json").unlink(missing_ok=True)
        (run_dir / "verification.json").unlink(missing_ok=True)
        (run_dir / "report.md").unlink(missing_ok=True)

    operation = verify_and_report_bundle(
        run_dir,
        _hooks=_VerifyReportTestHooks(after_snapshot_open=remove_live_evidence),
    )

    assert _check(operation.verification, "source:git_patch")["passed"] is True
    assert _check(operation.verification, "source:git_status")["passed"] is True
    assert operation.verification["verification_status"] == "complete"
    assert operation.report_path.is_file()


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "ordinary symlink privilege is not guaranteed on Windows; equivalent "
        "structured post-open identity rejection and real junction coverage run elsewhere"
    ),
)
def test_h1_post_snapshot_final_symlink_to_outside_cannot_change_semantics(
    tmp_path: Path,
) -> None:
    """H1 A6: a post-snapshot POSIX symlink is irrelevant to retained bytes."""

    run_dir, baseline = _make_bundle(tmp_path)
    outside = tmp_path / "outside.csv"
    outside.write_text("score\n99.0\n", encoding="utf-8")
    live = run_dir / "artifacts" / "metrics.csv"

    def redirect(session: Any) -> None:
        live.unlink()
        live.symlink_to(outside)

    result = _verify_bundle_with_hooks(
        run_dir,
        write=False,
        _hooks=_VerifyBundleTestHooks(after_snapshot_open=redirect),
    )

    assert result["evidence_root_sha256"] == baseline["evidence_root_sha256"]
    assert _check(result, "metric:score:derived-match")["recomputed_actual"] == 3.0


def test_h1_verify_to_report_mutation_keeps_one_internal_snapshot(
    tmp_path: Path,
) -> None:
    """H1 A7: mutations between verification and report cannot split authority."""

    run_dir, baseline = _make_bundle(tmp_path)

    def mutate_between_phases(session: Any, verification: Any) -> None:
        write_json(run_dir / "commands.json", [])
        write_json(run_dir / "metrics.json", [])
        (run_dir / "artifacts" / "metrics.csv").unlink()

    operation = verify_and_report_bundle(
        run_dir,
        _hooks=_VerifyReportTestHooks(
            after_verification_before_report=mutate_between_phases
        ),
    )
    report = operation.report_path.read_text(encoding="utf-8")

    assert _stable_verification(operation.verification) == _stable_verification(baseline)
    assert "| produce | completed | 0 |" in report
    assert "| score | 3.0 | 3.0 | 3.0 |" in report
    assert operation.verification["evidence_root_sha256"] in report


def test_h1_post_snapshot_semantic_phase_performs_no_live_evidence_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H1 A8: production verify/report cannot reopen evidence after sealing."""

    run_dir, _ = _make_bundle(tmp_path)

    def forbid(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("post-snapshot live evidence access")

    def block_after_snapshot(session: Any) -> None:
        monkeypatch.setattr(verifier_module, "read_json", forbid)
        monkeypatch.setattr(verifier_module, "read_source_record", forbid)
        monkeypatch.setattr(verifier_module, "resolve_bundle_file", forbid)
        monkeypatch.setattr(verifier_module, "sha256_file", forbid)
        monkeypatch.setattr(metrics_module, "resolve_bundle_file", forbid)
        monkeypatch.setattr(metrics_module, "sha256_file", forbid)
        monkeypatch.setattr(evidence_module, "read_evidence_index", forbid)
        monkeypatch.setattr(evidence_module, "validate_evidence_index", forbid)
        monkeypatch.setattr(Path, "read_text", forbid)
        monkeypatch.setattr(Path, "read_bytes", forbid)
        monkeypatch.setattr(Path, "open", forbid)

    operation = verify_and_report_bundle(
        run_dir,
        _hooks=_VerifyReportTestHooks(after_snapshot_open=block_after_snapshot),
    )
    monkeypatch.undo()

    assert operation.verification["verification_status"] == "complete"
    assert operation.report_path.is_file()


def test_h1_schema_one_bootstrap_and_each_indexed_object_are_acquired_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H1 A9-A11: run, index, and every indexed object have one acquisition."""

    run_dir, _ = _make_bundle(tmp_path)
    entries = read_evidence_index(run_dir)["entries"]
    watched = {
        os.path.normcase(os.path.abspath(os.fspath(run_dir / entry["path"]))): entry["path"]
        for entry in entries
    }
    watched[os.path.normcase(os.path.abspath(os.fspath(run_dir / "evidence.index.json")))] = (
        "evidence.index.json"
    )
    counts = {path: 0 for path in watched.values()}
    original_open = acquisition_module.os.open

    def counted(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        normalized = os.path.normcase(os.path.abspath(os.fspath(path)))
        relative = watched.get(normalized)
        if relative is not None:
            counts[relative] += 1
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(acquisition_module.os, "open", counted)
    verify_and_report_bundle(run_dir)

    assert counts["run.json"] == 1
    assert counts["evidence.index.json"] == 1
    assert all(counts[entry["path"]] == 1 for entry in entries)
    assert "evidence.index.json" not in {entry["path"] for entry in entries}


@pytest.mark.parametrize("phase", ["verification", "report"])
def test_root_replacement_never_writes_derived_output_into_state_b(
    tmp_path: Path,
    phase: str,
) -> None:
    """B1/B2: derived writes target A or Windows prevents installing B."""

    run_dir, _ = _make_bundle(tmp_path)
    parked = run_dir.with_name(f"{run_dir.name}-parked-{phase}")
    replacements: list[bool] = []

    if phase == "verification":
        def replace_before_verification(session: Any, verification: Any) -> None:
            replacements.append(_attempt_guarded_root_replacement(run_dir, parked))

        if os.name == "nt":
            result = _verify_bundle_with_hooks(
                run_dir,
                write=True,
                _hooks=_VerifyBundleTestHooks(
                    before_verification_write=replace_before_verification
                ),
            )
            assert replacements == [False]
            assert read_json(run_dir / "verification.json") == result
            assert not (run_dir / "report.md").exists()
            assert not parked.exists()
        else:
            with pytest.raises(ConfigError, match="root identity differs"):
                _verify_bundle_with_hooks(
                    run_dir,
                    write=True,
                    _hooks=_VerifyBundleTestHooks(
                        before_verification_write=replace_before_verification
                    ),
                )
            assert replacements == [True]
            assert not (run_dir / "verification.json").exists()
            assert not (run_dir / "report.md").exists()
    else:
        def replace_before_report(session: Any, verification: Any, report: str) -> None:
            replacements.append(_attempt_guarded_root_replacement(run_dir, parked))

        if os.name == "nt":
            operation = verify_and_report_bundle(
                run_dir,
                _hooks=_VerifyReportTestHooks(before_report_write=replace_before_report),
            )
            assert replacements == [False]
            assert read_json(run_dir / "verification.json") == operation.verification
            assert operation.report_path.is_file()
            assert not parked.exists()
        else:
            with pytest.raises(ConfigError, match="root identity differs"):
                verify_and_report_bundle(
                    run_dir,
                    _hooks=_VerifyReportTestHooks(
                        before_report_write=replace_before_report
                    ),
                )
            assert replacements == [True]
            assert (parked / "verification.json").is_file()
            assert not (run_dir / "verification.json").exists()
            assert not (run_dir / "report.md").exists()


def test_root_identity_unavailable_fails_closed_before_derived_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B3: an unavailable structured root identity cannot authorize a write."""

    run_dir, _ = _make_bundle(tmp_path)

    def unavailable(path: Any) -> Any:
        raise EvidenceAcquisitionError("identity_unavailable", "acceptance fixture")

    monkeypatch.setattr(derived_outputs_module, "capture_bundle_root_identity", unavailable)
    with pytest.raises(ConfigError, match="identity is unavailable"):
        verify_bundle(run_dir)


def test_derived_outputs_remain_unindexed_and_do_not_change_root(
    tmp_path: Path,
) -> None:
    """B4/B5: unchanged roots allow atomic outputs without index self-reference."""

    run_dir, initial = _make_bundle(tmp_path)
    index_before = (run_dir / "evidence.index.json").read_bytes()
    first = verify_and_report_bundle(run_dir)
    second = verify_and_report_bundle(run_dir)
    index_after = (run_dir / "evidence.index.json").read_bytes()
    paths = {entry["path"] for entry in read_evidence_index(run_dir)["entries"]}

    assert index_after == index_before
    assert first.verification["evidence_root_sha256"] == initial["evidence_root_sha256"]
    assert second.verification["evidence_root_sha256"] == initial["evidence_root_sha256"]
    assert {"evidence.index.json", "verification.json", "report.md"}.isdisjoint(paths)


def test_large_spooled_metric_has_fresh_readers_and_survives_live_deletion(
    tmp_path: Path,
) -> None:
    """C1-C3: rolled spools retain bytes, provide fresh readers, then invalidate."""

    run_dir, _ = _make_bundle(tmp_path)
    payload = _rewrite_large_regex_metric(run_dir)
    captured: list[Any] = []
    readers: list[Any] = []

    def inspect_spool(session: Any) -> None:
        evidence = session.snapshot.objects["artifacts/metrics.csv"]
        assert evidence.spool_rolled_to_disk is True
        first = evidence.open_reader()
        second = evidence.open_reader()
        assert first.tell() == second.tell() == 0
        assert first.read() == payload
        assert second.read() == payload
        first.close()
        with evidence.open_reader() as fresh:
            assert fresh.read(1) == payload[:1]
        (run_dir / "artifacts" / "metrics.csv").unlink()
        captured.append(evidence)
        readers.append(second)

    result = _verify_bundle_with_hooks(
        run_dir,
        write=False,
        _hooks=_VerifyBundleTestHooks(after_snapshot_open=inspect_spool),
    )

    assert _check(result, "metric:score:derived-match")["recomputed_actual"] == 3.0
    assert result["result_status"] == "matched"
    assert captured[0].closed is True
    with pytest.raises(SnapshotStateError, match="closed"):
        readers[0].read(1)


def test_spooled_metric_cleanup_runs_after_post_acquisition_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C4: a deterministic semantic failure still closes retained temp resources."""

    run_dir, _ = _make_bundle(tmp_path)
    _rewrite_large_regex_metric(run_dir)
    captured: list[Any] = []

    def capture(session: Any) -> None:
        captured.append(session.snapshot.objects["artifacts/metrics.csv"])

    def fail(session: Any) -> Any:
        raise ConfigError("acceptance post-acquisition failure")

    monkeypatch.setattr(operations_module, "verify_snapshot_session", fail)
    with pytest.raises(ConfigError, match="post-acquisition failure"):
        verify_and_report_bundle(
            run_dir,
            _hooks=_VerifyReportTestHooks(after_snapshot_open=capture),
        )

    assert captured[0].spool_rolled_to_disk is True
    assert captured[0].closed is True
    with pytest.raises(SnapshotStateError, match="closed"):
        captured[0].open_reader()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_argv", ["forged", "--requested"]),
        ("argv", ["forged", "--resolved"]),
        ("cwd", "C:/forged/cwd"),
        ("environment_overrides", {"FORGED": "1"}),
        ("timeout_seconds", 17),
        ("step_id", "forged-step"),
        ("stdout_evidence_path", "logs/forged.stdout.log"),
        ("stderr_evidence_path", "logs/forged.stderr.log"),
    ],
)
def test_h2_reindexed_command_protocol_mutation_fails_semantic_closure(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    """H2 D1-D8: byte-self-consistent command mutations cannot retain assurance."""

    run_dir, _ = _make_bundle(tmp_path)
    commands = read_json(run_dir / "commands.json")
    commands[0][field] = value
    write_json(run_dir / "commands.json", commands)
    _refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert _check(verification, "bundle:index")["passed"] is True
    assert _check(verification, "command:protocol-closure")["passed"] is False
    assert verification["assurance_level"] == "recorded"
    assert verification["evidence_root_sha256"] is None


def test_h2_unrelated_indexed_evidence_cannot_replace_command_logs(
    tmp_path: Path,
) -> None:
    """H2 D8/D9: rebinding logs to indexed metric/artifact bytes fails protocol."""

    run_dir, _ = _make_bundle(tmp_path, extra_artifact=True)
    commands = read_json(run_dir / "commands.json")
    commands[0]["stdout_evidence_path"] = "artifacts/metrics.csv"
    commands[0]["stderr_evidence_path"] = "artifacts/notes.txt"
    write_json(run_dir / "commands.json", commands)
    (run_dir / "logs" / "produce.stdout.log").unlink()
    (run_dir / "logs" / "produce.stderr.log").unlink()
    _rebuild_index_from_records(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert _check(verification, "bundle:index")["passed"] is True
    assert _check(verification, "bundle:closure")["passed"] is True
    assert _check(verification, "command:protocol-closure")["passed"] is False
    assert verification["assurance_level"] == "recorded"


@pytest.mark.parametrize(
    ("run_status", "command_status", "return_code", "raises"),
    [
        ("executed", "completed", False, True),
        ("execution_failed", "completed", 0, False),
        ("execution_failed", "failed", 0, False),
        ("executed", "timeout", 0, False),
        ("executed", "launch_error", 0, False),
    ],
)
def test_h2_bool_and_invalid_command_state_machine_fail_closed(
    tmp_path: Path,
    run_status: str,
    command_status: str,
    return_code: int | bool,
    raises: bool,
) -> None:
    """H2 D10-D12: bool aliases and impossible command states never pass."""

    run_dir, _ = _make_bundle(tmp_path)
    run = read_json(run_dir / "run.json")
    run["status"] = run_status
    write_json(run_dir / "run.json", run)
    commands = read_json(run_dir / "commands.json")
    commands[0].update(status=command_status, return_code=return_code)
    write_json(run_dir / "commands.json", commands)
    _refresh_index(run_dir)

    if raises:
        with pytest.raises(ConfigError, match="return_code"):
            verify_bundle(run_dir, write=False)
    else:
        verification = verify_bundle(run_dir, write=False)
        assert _check(verification, "command:protocol-closure")["passed"] is False
        assert verification["assurance_level"] == "recorded"


def test_h2_commands_jsonl_is_never_semantic_authority(tmp_path: Path) -> None:
    """H2 D13: a consistently reindexed archive cannot alter command semantics."""

    run_dir, baseline = _make_bundle(tmp_path)
    (run_dir / "commands.jsonl").write_text(
        '{"forged":"archive-only state B"}\n', encoding="utf-8"
    )
    _refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert verification["verification_status"] == "complete"
    assert verification["assurance_level"] == "metric_derivations_recomputed"
    assert verification["result_status"] == baseline["result_status"]


def test_h3_reindexed_wildcard_artifact_rebinding_fails_scope_closure(
    tmp_path: Path,
) -> None:
    """H3 E1: an unrelated indexed file cannot satisfy wildcard membership."""

    run_dir, _ = _make_bundle(tmp_path, extra_artifact=True)
    wildcard = "{run_dir}/artifacts/*.txt"
    resolved_path = run_dir / "manifest.resolved.yaml"
    resolved = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    resolved["run"]["steps"][0]["artifacts"] = [wildcard]
    resolved_path.write_text(yaml.safe_dump(resolved, sort_keys=False), encoding="utf-8")
    rebound = run_dir / "logs" / "produce.stdout.log"
    write_json(
        run_dir / "artifacts.json",
        [
            {
                "step_id": "produce",
                "declared_path": wildcard,
                "resolved_pattern": str(rebound),
                "matches": [
                    {
                        "path": str(rebound),
                        "exists": True,
                        "kind": "file",
                        "size_bytes": rebound.stat().st_size,
                        "sha256": sha256_file(rebound),
                        "path_scope": "bundle",
                        "evidence_path": "logs/produce.stdout.log",
                    }
                ],
            }
        ],
    )
    _rebuild_index_from_records(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert _check(verification, "bundle:index")["passed"] is True
    assert _check(verification, "bundle:closure")["passed"] is True
    assert _check(verification, "artifact:bundle-scope-closure")["passed"] is False
    assert verification["assurance_level"] == "recorded"


def test_h3_posix_segment_wildcard_semantics_are_explicit() -> None:
    """H3 E2: ordinary wildcards never cross separators; full ** segments do."""

    assert bundle_artifact_path_matches("artifacts/*.txt", "artifacts/result.txt")
    assert not bundle_artifact_path_matches(
        "artifacts/*.txt", "artifacts/nested/result.txt"
    )
    assert bundle_artifact_path_matches("artifacts/r?sult.txt", "artifacts/result.txt")
    assert bundle_artifact_path_matches("artifacts/[ab].txt", "artifacts/a.txt")
    assert not bundle_artifact_path_matches("artifacts/[ab].txt", "artifacts/c.txt")
    assert bundle_artifact_path_matches(
        "artifacts/**/*.txt", "artifacts/deep/nested/result.txt"
    )
    assert bundle_artifact_path_matches("artifacts/**/*.txt", "artifacts/result.txt")


@pytest.mark.parametrize("mutation", ["duplicate", "extra", "missing", "zero_match"])
def test_h3_artifact_declaration_membership_closure(
    tmp_path: Path,
    mutation: str,
) -> None:
    """H3 E3-E6: duplicates/extra/missing fail; legal zero-match remains legal."""

    run_dir, _ = _make_bundle(tmp_path, extra_artifact=True)
    artifacts = read_json(run_dir / "artifacts.json")
    if mutation == "duplicate":
        artifacts.append(copy.deepcopy(artifacts[0]))
    elif mutation == "extra":
        artifacts.append(
            {
                "step_id": "produce",
                "declared_path": "{run_dir}/artifacts/extra.txt",
                "resolved_pattern": str(run_dir / "artifacts" / "extra.txt"),
                "matches": [],
            }
        )
    elif mutation == "missing":
        artifacts = artifacts[:-1]
    else:
        wildcard = "{run_dir}/artifacts/*.missing"
        resolved_path = run_dir / "manifest.resolved.yaml"
        resolved = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
        resolved["run"]["steps"][0]["artifacts"].append(wildcard)
        resolved_path.write_text(yaml.safe_dump(resolved, sort_keys=False), encoding="utf-8")
        artifacts.append(
            {
                "step_id": "produce",
                "declared_path": wildcard,
                "resolved_pattern": str(run_dir / "artifacts" / "*.missing"),
                "matches": [],
            }
        )
    write_json(run_dir / "artifacts.json", artifacts)
    _rebuild_index_from_records(run_dir)

    verification = verify_bundle(run_dir, write=False)
    closure = _check(verification, "artifact:declaration-closure")
    scope = _check(verification, "artifact:bundle-scope-closure")

    if mutation == "zero_match":
        assert closure["passed"] is True
        assert scope["passed"] is True
    else:
        assert closure["passed"] is False or scope["passed"] is False
        assert verification["assurance_level"] == "recorded"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected", True),
        ("atol", True),
        ("rtol", True),
        ("expected", float("nan")),
        ("expected", float("inf")),
        ("expected", float("-inf")),
        ("atol", -1.0),
        ("rtol", -1.0),
    ],
)
def test_numeric_manifest_domains_fail_closed(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    """F1-F7: bool, non-finite, and negative numeric domains are invalid."""

    path = _make_manifest(tmp_path / "project")
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    manifest["metrics"][0][field] = value
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigError):
        run_manifest(path)


@pytest.mark.parametrize("extractor", ["csv", "log_regex"])
def test_non_finite_extracted_metric_fails_closed(
    tmp_path: Path,
    extractor: str,
) -> None:
    """F8/F9: non-finite CSV and regex values cannot become derived results."""

    run_dir, _ = _make_bundle(tmp_path)
    raw = run_dir / "artifacts" / "metrics.csv"
    if extractor == "csv":
        payload = b"score\nNaN\n"
    else:
        payload = b"score=NaN\n"
        resolved_path = run_dir / "manifest.resolved.yaml"
        resolved = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
        specification = resolved["metrics"][0]
        specification.update(
            extractor="log_regex",
            pattern=r"score=(\S+)",
            group=1,
        )
        specification.pop("column", None)
        resolved_path.write_text(
            yaml.safe_dump(resolved, sort_keys=False), encoding="utf-8"
        )
        metrics = read_json(run_dir / "metrics.json")
        metrics[0]["extractor"] = "log_regex"
        write_json(run_dir / "metrics.json", metrics)
    raw.write_bytes(payload)
    size_bytes = len(payload)
    digest = sha256_bytes(payload)
    sources = read_json(run_dir / "metric_sources.json")
    sources["metrics"][0]["sources"][0].update(size_bytes=size_bytes, sha256=digest)
    write_json(run_dir / "metric_sources.json", sources)
    artifacts = read_json(run_dir / "artifacts.json")
    artifacts[0]["matches"][0].update(size_bytes=size_bytes, sha256=digest)
    write_json(run_dir / "artifacts.json", artifacts)
    _refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert _check(verification, "bundle:index")["passed"] is True
    derived = _check(verification, "metric:score:derived-match")
    assert derived["passed"] is False
    assert "finite" in derived["reason"]
    assert verification["assurance_level"] == "bundle_integrity_checked"
    assert verification["result_status"] == "indeterminate"


@pytest.mark.parametrize(
    "attack",
    ["noncanonical_index", "missing_run", "later_fingerprint", "root_mid_acquisition"],
)
def test_canonical_index_and_snapshot_root_fail_before_establishment(
    tmp_path: Path,
    attack: str,
) -> None:
    """G1-G5/G8: root appears only after exact canonical full acquisition."""

    run_dir, baseline = _make_bundle(tmp_path)
    exact_index = (run_dir / "evidence.index.json").read_bytes()
    assert baseline["evidence_root_sha256"] == sha256_bytes(exact_index)

    if attack == "noncanonical_index":
        parsed = read_json(run_dir / "evidence.index.json")
        (run_dir / "evidence.index.json").write_text(
            json.dumps(parsed, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="cannot establish schema-1 evidence snapshot"):
            verify_bundle(run_dir, write=False)
    elif attack == "missing_run":
        entries = read_evidence_index(run_dir)["entries"]
        write_evidence_index(
            run_dir,
            [
                {"path": entry["path"], "roles": entry["roles"]}
                for entry in entries
                if entry["path"] != "run.json"
            ],
        )
        with pytest.raises(ConfigError, match="cannot establish schema-1 evidence snapshot"):
            verify_bundle(run_dir, write=False)
    elif attack == "later_fingerprint":
        (run_dir / "environment.json").write_text("{}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="cannot establish schema-1 evidence snapshot"):
            verify_bundle(run_dir, write=False)
    else:
        parked = run_dir.with_name(f"{run_dir.name}-parked-mid-build")
        replacements = 0

        def replace_once(path: str, evidence: Any) -> None:
            nonlocal replacements
            if replacements == 0 and path != "run.json":
                replacements += 1
                _replace_bundle_root(run_dir, parked)

        with pytest.raises(SnapshotBuildError):
            open_schema_one_snapshot(
                run_dir,
                _hooks=_SnapshotBuildTestHooks(before_indexed_entry_acquire=replace_once),
            )
        assert replacements == 1


def test_relocation_and_missing_origin_metadata_do_not_change_schema_one_io(
    tmp_path: Path,
) -> None:
    """H1-H3: schema-1 meaning comes from relocated captured evidence only."""

    original, baseline = _make_bundle(tmp_path / "producer")
    missing = tmp_path / "origin-no-longer-exists" / "metric.csv"
    inputs = read_json(original / "inputs.json")
    inputs[0]["path"] = str(missing)
    write_json(original / "inputs.json", inputs)
    commands = read_json(original / "commands.json")
    commands[0]["stdout_path"] = str(missing)
    commands[0]["stderr_path"] = str(missing)
    write_json(original / "commands.json", commands)
    artifacts = read_json(original / "artifacts.json")
    artifacts[0]["resolved_pattern"] = str(missing)
    artifacts[0]["matches"][0]["path"] = str(missing)
    write_json(original / "artifacts.json", artifacts)
    sources = read_json(original / "metric_sources.json")
    sources["metrics"][0]["sources"][0]["origin_path"] = str(missing)
    write_json(original / "metric_sources.json", sources)
    metrics = read_json(original / "metrics.json")
    metrics[0]["source_paths"] = [str(missing)]
    write_json(original / "metrics.json", metrics)
    root = _refresh_index(original)

    relocated = tmp_path / "consumer" / "bundle"
    relocated.parent.mkdir()
    shutil.copytree(original, relocated)
    shutil.rmtree(tmp_path / "producer")
    verification = verify_bundle(relocated, write=False)

    assert not original.exists()
    assert root != baseline["evidence_root_sha256"]
    assert verification["evidence_root_sha256"] == root
    assert verification["verification_status"] == "complete"
    assert verification["assurance_level"] == "metric_derivations_recomputed"
    assert verification["result_status"] == "matched"


@pytest.mark.parametrize("case", ["dry_run", "zero_metrics", "legacy", "malformed_schema_one"])
def test_dry_run_zero_metric_legacy_and_no_fallback_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """I1-I6: planning, empty metrics, legacy, and malformed schema stay distinct."""

    if case == "dry_run":
        run_dir, _ = _make_bundle(tmp_path, dry_run=True)
        result = verify_bundle(run_dir, write=False)
        assert result["verification_status"] == "complete"
        assert result["assurance_level"] == "bundle_integrity_checked"
        assert result["execution_record_status"] == "not_run"
        assert result["result_status"] == "not_evaluated"
    elif case == "zero_metrics":
        run_dir, _ = _make_bundle(tmp_path, metrics=False)
        result = verify_bundle(run_dir, write=False)
        assert result["verification_status"] == "complete"
        assert result["assurance_level"] == "bundle_integrity_checked"
        assert result["result_status"] == "not_evaluated"
        assert result["coverage"]["metric_sources"]["total"] == 0
    elif case == "legacy":
        run_dir, _ = _make_bundle(tmp_path, metrics=False)
        run = read_json(run_dir / "run.json")
        run["schema_version"] = 0
        write_json(run_dir / "run.json", run)
        (run_dir / "evidence.index.json").unlink()
        result = verify_bundle(run_dir, write=False)
        report = generate_report(run_dir).read_text(encoding="utf-8")
        assert result["assurance_level"] == "recorded"
        assert result["result_status"] == "not_evaluated"
        assert result["evidence_root_sha256"] is None
        assert "recorded" in report
    else:
        run_dir, _ = _make_bundle(tmp_path)
        (run_dir / "metrics.json").write_bytes(b"[]")

        def forbid_legacy(directory: Path) -> Any:
            raise AssertionError("malformed schema-1 fell back to legacy")

        monkeypatch.setattr(verifier_module, "_verify_legacy_directory", forbid_legacy)
        with pytest.raises(ConfigError, match="cannot establish schema-1 evidence snapshot"):
            verify_bundle(run_dir, write=False)


def test_production_stable_result_and_check_order_parity(tmp_path: Path) -> None:
    """J: standalone production verification preserves canonical/compatibility parity."""

    run_dir, runner_result = _make_bundle(tmp_path)
    standalone = verify_bundle(run_dir, write=False)

    assert _stable_verification(standalone) == _stable_verification(runner_result)
    assert [item["id"] for item in standalone["checks"]] == [
        item["id"] for item in runner_result["checks"]
    ]
    assert [item["id"] for item in standalone["contract_checks"]] == [
        item["id"] for item in runner_result["contract_checks"]
    ]
    assert [item["id"] for item in standalone["checks"]] == [
        item["id"] for item in runner_result["checks"]
    ]
    for field in (
        "schema_version",
        "run_id",
        "verification_status",
        "assurance_level",
        "execution_record_status",
        "result_status",
        "checks_passed",
        "evidence_root_sha256",
        "coverage",
        "not_established",
        "compatibility",
        "status",
        "passed",
        "preflight_passed",
    ):
        assert standalone[field] == runner_result[field]
