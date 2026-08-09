from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import reprotrace.derived_outputs as derived_outputs_module
import reprotrace.operations as operations_module
import reprotrace.verifier as verifier_module
from reprotrace.cli import main as cli_main
from reprotrace.derived_outputs import (
    _DerivedOutputLifecycleTestHooks,
    DerivedOutputLifecycleGuard,
)
from reprotrace.errors import ConfigError
from reprotrace.io import read_json, write_json
from reprotrace.operations import _VerifyReportTestHooks, verify_and_report_bundle
from reprotrace.snapshot import BundleRootIdentity, FileIdentity
from reprotrace.verifier import (
    _VerifyBundleTestHooks,
    _verify_bundle_with_hooks,
    verify_bundle,
)
from tests.test_snapshot_verifier_integration import make_bundle, refresh_index


CANONICAL_OUTPUTS = ("verification.json", "report.md")
REPLACEMENT_VERIFICATION = b"replacement verification\n"
REPLACEMENT_REPORT = b"replacement report\n"


def _output_state(run_dir: Path) -> dict[str, tuple[bytes, int, int, int]]:
    state: dict[str, tuple[bytes, int, int, int]] = {}
    for name in CANONICAL_OUTPUTS:
        path = run_dir / name
        content = path.read_bytes()
        status = path.stat()
        state[name] = (
            content,
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
        )
    return state


def _corrupt_indexed_metric_source(run_dir: Path) -> None:
    (run_dir / "artifacts" / "metrics.csv").write_text(
        "score\n9.0\n",
        encoding="utf-8",
    )


def _replace_bundle_root(run_dir: Path, parked: Path) -> None:
    run_dir.replace(parked)
    run_dir.mkdir()
    (run_dir / "verification.json").write_bytes(REPLACEMENT_VERIFICATION)
    (run_dir / "report.md").write_bytes(REPLACEMENT_REPORT)
    (run_dir / "replacement-root-b.txt").write_text("state B\n", encoding="utf-8")


def _attempt_bundle_root_replacement(run_dir: Path, parked: Path) -> bool:
    """Install B, or prove the retained Windows root handle blocked the rename."""

    try:
        _replace_bundle_root(run_dir, parked)
    except PermissionError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) != 32:
            raise
        return False
    return True


def _assert_replacement_root_untouched(run_dir: Path) -> None:
    assert (run_dir / "verification.json").read_bytes() == REPLACEMENT_VERIFICATION
    assert (run_dir / "report.md").read_bytes() == REPLACEMENT_REPORT
    assert (run_dir / "replacement-root-b.txt").read_text(encoding="utf-8") == "state B\n"


def _assert_no_derived_temporary_files(directory: Path) -> None:
    assert not list(directory.glob(".verification.json.*.tmp"))
    assert not list(directory.glob(".report.md.*.tmp"))


@pytest.mark.parametrize("command", ["verify", "report"])
def test_cli_failed_refresh_removes_historical_canonical_pair(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    _corrupt_indexed_metric_source(run_dir)

    assert cli_main([command, str(run_dir)]) == 2

    captured = capsys.readouterr()
    assert "cannot establish schema-1 evidence snapshot" in captured.err
    assert not (run_dir / "verification.json").exists()
    assert not (run_dir / "report.md").exists()


def test_verification_only_refresh_removes_historical_report(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    assert (run_dir / "report.md").is_file()

    result = verify_bundle(run_dir, write=True)

    assert read_json(run_dir / "verification.json") == result
    assert not (run_dir / "report.md").exists()


def test_write_false_success_preserves_canonical_bytes_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    before = _output_state(run_dir)

    def authority_forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("write=False acquired mutation authority")

    monkeypatch.setattr(
        derived_outputs_module,
        "_acquire_mutation_authority",
        authority_forbidden,
    )

    result = verify_bundle(run_dir, write=False)

    assert result["verification_status"] == "complete"
    assert _output_state(run_dir) == before


def test_write_false_failure_preserves_canonical_bytes_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    before = _output_state(run_dir)
    _corrupt_indexed_metric_source(run_dir)

    def authority_forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("write=False acquired mutation authority")

    monkeypatch.setattr(
        derived_outputs_module,
        "_acquire_mutation_authority",
        authority_forbidden,
    )

    with pytest.raises(ConfigError, match="cannot establish schema-1 evidence snapshot"):
        verify_bundle(run_dir, write=False)

    assert _output_state(run_dir) == before


def test_root_replaced_after_guard_capture_is_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    parked = run_dir.with_name(run_dir.name + "-parked-before-invalidation")
    replacement_results: list[bool] = []

    def replace(guard: Any) -> None:
        replaced = _attempt_bundle_root_replacement(run_dir, parked)
        replacement_results.append(replaced)
        if not replaced:
            raise ConfigError("Windows root replacement blocked by lifecycle authority")

    def verification_forbidden(directory: Path) -> Any:
        raise AssertionError("verification continued after lifecycle root replacement")

    monkeypatch.setattr(
        verifier_module,
        "_open_schema_one_snapshot_for_production",
        verification_forbidden,
    )
    hooks = _VerifyBundleTestHooks(
        lifecycle=_DerivedOutputLifecycleTestHooks(after_guard_capture=replace)
    )

    expected_error = (
        "Windows root replacement blocked" if os.name == "nt" else "operation-start identity"
    )
    with pytest.raises(ConfigError, match=expected_error):
        _verify_bundle_with_hooks(run_dir, write=True, _hooks=hooks)

    assert replacement_results == [os.name != "nt"]
    if os.name == "nt":
        assert not parked.exists()
        assert (run_dir / "verification.json").is_file()
        assert (run_dir / "report.md").is_file()
    else:
        _assert_replacement_root_untouched(run_dir)
        assert (parked / "verification.json").is_file()
        assert (parked / "report.md").is_file()


def test_root_replaced_between_invalidation_steps_leaves_orphan_only_in_a(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    parked = run_dir.with_name(run_dir.name + "-parked-between-invalidation")
    replacement_results: list[bool] = []

    def replace(guard: Any, relative_path: str) -> None:
        if relative_path == "verification.json":
            replaced = _attempt_bundle_root_replacement(run_dir, parked)
            replacement_results.append(replaced)
            if not replaced:
                raise ConfigError("Windows root replacement blocked by lifecycle authority")

    def verification_forbidden(directory: Path) -> Any:
        raise AssertionError("verification continued after lifecycle root replacement")

    monkeypatch.setattr(
        verifier_module,
        "_open_schema_one_snapshot_for_production",
        verification_forbidden,
    )
    hooks = _VerifyBundleTestHooks(
        lifecycle=_DerivedOutputLifecycleTestHooks(after_output_invalidated=replace)
    )

    expected_error = (
        "Windows root replacement blocked" if os.name == "nt" else "operation-start identity"
    )
    with pytest.raises(ConfigError, match=expected_error):
        _verify_bundle_with_hooks(run_dir, write=True, _hooks=hooks)

    assert replacement_results == [os.name != "nt"]
    if os.name == "nt":
        assert not parked.exists()
        assert not (run_dir / "verification.json").exists()
        assert (run_dir / "report.md").is_file()
    else:
        _assert_replacement_root_untouched(run_dir)
        assert not (parked / "verification.json").exists()
        assert (parked / "report.md").is_file()


def test_authority_acquisition_rejects_root_replaced_after_identity_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    parked = run_dir.with_name(run_dir.name + "-parked-during-acquisition")

    def replace(directory: Path, identity: BundleRootIdentity) -> None:
        assert directory == run_dir
        assert identity.file_identity.available
        _replace_bundle_root(run_dir, parked)

    def verification_forbidden(directory: Path) -> Any:
        raise AssertionError("verification continued after authority acquisition race")

    monkeypatch.setattr(
        verifier_module,
        "_open_schema_one_snapshot_for_production",
        verification_forbidden,
    )
    hooks = _VerifyBundleTestHooks(
        lifecycle=_DerivedOutputLifecycleTestHooks(after_identity_capture=replace)
    )

    with pytest.raises(ConfigError, match="opened root identity differs"):
        _verify_bundle_with_hooks(run_dir, write=True, _hooks=hooks)

    _assert_replacement_root_untouched(run_dir)
    assert (parked / "verification.json").is_file()
    assert (parked / "report.md").is_file()
    moved_b = run_dir.with_name(run_dir.name + "-replacement-after-acquisition-failure")
    run_dir.replace(moved_b)
    moved_b.replace(run_dir)


def test_real_invalidation_unlink_never_mutates_replacement_root(
    tmp_path: Path,
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    parked = run_dir.with_name(run_dir.name + "-parked-before-unlink")
    replacement_results: list[bool] = []

    def replace_before_unlink(
        guard: DerivedOutputLifecycleGuard,
        relative_path: str,
    ) -> None:
        if relative_path == "verification.json":
            replacement_results.append(
                _attempt_bundle_root_replacement(run_dir, parked)
            )

    hooks = _VerifyBundleTestHooks(
        lifecycle=_DerivedOutputLifecycleTestHooks(
            before_canonical_unlink=replace_before_unlink
        )
    )

    if os.name == "nt":
        result = _verify_bundle_with_hooks(run_dir, write=True, _hooks=hooks)
        assert replacement_results == [False]
        assert read_json(run_dir / "verification.json") == result
        assert not (run_dir / "report.md").exists()
        assert not parked.exists()
    else:
        with pytest.raises(ConfigError, match="operation-start identity"):
            _verify_bundle_with_hooks(run_dir, write=True, _hooks=hooks)
        assert replacement_results == [True]
        _assert_replacement_root_untouched(run_dir)
        assert not (parked / "verification.json").exists()
        assert (parked / "report.md").is_file()


def test_temporary_creation_never_targets_replacement_root(
    tmp_path: Path,
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    parked = run_dir.with_name(run_dir.name + "-parked-before-temp-create")
    replacement_results: list[bool] = []

    def replace_before_temp_create(
        guard: DerivedOutputLifecycleGuard,
        relative_path: str,
    ) -> None:
        if relative_path == "verification.json":
            replacement_results.append(
                _attempt_bundle_root_replacement(run_dir, parked)
            )

    hooks = _VerifyBundleTestHooks(
        lifecycle=_DerivedOutputLifecycleTestHooks(
            before_temp_create=replace_before_temp_create
        )
    )

    if os.name == "nt":
        result = _verify_bundle_with_hooks(run_dir, write=True, _hooks=hooks)
        assert replacement_results == [False]
        assert read_json(run_dir / "verification.json") == result
        assert not (run_dir / "report.md").exists()
        assert not parked.exists()
        _assert_no_derived_temporary_files(run_dir)
    else:
        with pytest.raises(ConfigError, match="operation-start identity"):
            _verify_bundle_with_hooks(run_dir, write=True, _hooks=hooks)
        assert replacement_results == [True]
        _assert_replacement_root_untouched(run_dir)
        assert not (parked / "verification.json").exists()
        assert not (parked / "report.md").exists()
        _assert_no_derived_temporary_files(parked)


def test_verification_publication_never_mutates_replacement_root(
    tmp_path: Path,
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    parked = run_dir.with_name(run_dir.name + "-parked-before-verification-replace")
    replacement_results: list[bool] = []

    def replace_before_publication(
        guard: DerivedOutputLifecycleGuard,
        temporary_name: str,
        relative_path: str,
    ) -> None:
        assert temporary_name.startswith(".verification.json.")
        if relative_path == "verification.json":
            replacement_results.append(
                _attempt_bundle_root_replacement(run_dir, parked)
            )

    hooks = _VerifyBundleTestHooks(
        lifecycle=_DerivedOutputLifecycleTestHooks(
            before_atomic_replace=replace_before_publication
        )
    )

    if os.name == "nt":
        result = _verify_bundle_with_hooks(run_dir, write=True, _hooks=hooks)
        assert replacement_results == [False]
        assert read_json(run_dir / "verification.json") == result
        assert not (run_dir / "report.md").exists()
        assert not parked.exists()
        _assert_no_derived_temporary_files(run_dir)
    else:
        with pytest.raises(ConfigError, match="operation-start identity"):
            _verify_bundle_with_hooks(run_dir, write=True, _hooks=hooks)
        assert replacement_results == [True]
        _assert_replacement_root_untouched(run_dir)
        assert read_json(parked / "verification.json")["verification_status"] == "complete"
        assert not (parked / "report.md").exists()
        _assert_no_derived_temporary_files(parked)


def test_unavailable_root_identity_fails_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    before = _output_state(run_dir)

    def unavailable(path: Any) -> BundleRootIdentity:
        return BundleRootIdentity(
            FileIdentity.unavailable(file_type="directory", reason="simulated")
        )

    monkeypatch.setattr(
        derived_outputs_module,
        "capture_bundle_root_identity",
        unavailable,
    )

    with pytest.raises(ConfigError, match="root identity is unavailable"):
        verify_bundle(run_dir, write=True)

    assert _output_state(run_dir) == before


def test_post_invalidation_failure_leaves_canonical_outputs_absent(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    invalidated: list[str] = []

    def record(guard: Any, relative_path: str) -> None:
        invalidated.append(relative_path)

    def fail(guard: Any) -> None:
        raise ConfigError("simulated post-invalidation failure")

    hooks = _VerifyBundleTestHooks(
        lifecycle=_DerivedOutputLifecycleTestHooks(
            after_output_invalidated=record,
            after_invalidation=fail,
        )
    )

    with pytest.raises(ConfigError, match="post-invalidation failure"):
        _verify_bundle_with_hooks(run_dir, write=True, _hooks=hooks)

    assert invalidated == ["verification.json", "report.md"]
    assert not (run_dir / "verification.json").exists()
    assert not (run_dir / "report.md").exists()


def test_mutation_authority_is_released_after_success(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    guards: list[DerivedOutputLifecycleGuard] = []
    descriptors: list[int] = []

    def record(guard: DerivedOutputLifecycleGuard) -> None:
        guards.append(guard)
        descriptors.append(guard.fileno())

    hooks = _VerifyBundleTestHooks(
        lifecycle=_DerivedOutputLifecycleTestHooks(after_guard_capture=record)
    )

    _verify_bundle_with_hooks(run_dir, write=True, _hooks=hooks)

    assert len(guards) == 1
    assert guards[0].closed
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    moved = run_dir.with_name(run_dir.name + "-after-authority-close")
    run_dir.replace(moved)
    moved.replace(run_dir)
    _assert_no_derived_temporary_files(run_dir)


def test_mutation_authority_is_released_after_exception(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    guards: list[DerivedOutputLifecycleGuard] = []
    descriptors: list[int] = []

    def fail(guard: DerivedOutputLifecycleGuard) -> None:
        guards.append(guard)
        descriptors.append(guard.fileno())
        raise ConfigError("simulated lifecycle failure")

    hooks = _VerifyBundleTestHooks(
        lifecycle=_DerivedOutputLifecycleTestHooks(after_invalidation=fail)
    )

    with pytest.raises(ConfigError, match="simulated lifecycle failure"):
        _verify_bundle_with_hooks(run_dir, write=True, _hooks=hooks)

    assert len(guards) == 1
    assert guards[0].closed
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    moved = run_dir.with_name(run_dir.name + "-after-exception-close")
    run_dir.replace(moved)
    moved.replace(run_dir)
    assert not (run_dir / "verification.json").exists()
    assert not (run_dir / "report.md").exists()
    _assert_no_derived_temporary_files(run_dir)


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows lifecycle authority prevents the root replacement needed by this probe",
)
def test_session_root_must_match_lifecycle_root(
    tmp_path: Path,
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    parked = run_dir.with_name(run_dir.name + "-parked-session-binding")

    def replace_with_valid_b(guard: Any) -> None:
        run_dir.replace(parked)
        shutil.copytree(parked, run_dir)

    hooks = _VerifyBundleTestHooks(
        lifecycle=_DerivedOutputLifecycleTestHooks(
            after_invalidation=replace_with_valid_b
        )
    )

    with pytest.raises(ConfigError, match="session bundle root identity differs"):
        _verify_bundle_with_hooks(run_dir, write=True, _hooks=hooks)

    assert not (run_dir / "verification.json").exists()
    assert not (run_dir / "report.md").exists()


def test_semantic_incomplete_result_is_published(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    commands = read_json(run_dir / "commands.json")
    commands[0]["argv"] = ["tampered-command"]
    write_json(run_dir / "commands.json", commands)
    refresh_index(run_dir)

    operation = verify_and_report_bundle(run_dir)

    assert operation.verification["verification_status"] == "incomplete"
    assert operation.verification["checks_passed"] is False
    assert read_json(run_dir / "verification.json") == operation.verification
    assert operation.report_path.is_file()


def test_metric_derivation_failure_result_is_published(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    metrics = read_json(run_dir / "metrics.json")
    metrics[0].update(actual=9.0, absolute_error=6.0, passed=False)
    write_json(run_dir / "metrics.json", metrics)
    refresh_index(run_dir)

    operation = verify_and_report_bundle(run_dir)

    assert operation.verification["verification_status"] == "incomplete"
    assert operation.verification["result_status"] == "indeterminate"
    assert read_json(run_dir / "verification.json") == operation.verification
    assert operation.report_path.is_file()


def test_combined_publication_never_mutates_replacement_root(
    tmp_path: Path,
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    parked = run_dir.with_name(run_dir.name + "-parked-before-report-replace")
    replacement_results: list[bool] = []

    def replace_before_report(
        guard: DerivedOutputLifecycleGuard,
        temporary_name: str,
        relative_path: str,
    ) -> None:
        if relative_path == "report.md":
            assert temporary_name.startswith(".report.md.")
            replacement_results.append(
                _attempt_bundle_root_replacement(run_dir, parked)
            )

    hooks = _VerifyReportTestHooks(
        lifecycle=_DerivedOutputLifecycleTestHooks(
            before_atomic_replace=replace_before_report
        )
    )

    if os.name == "nt":
        operation = verify_and_report_bundle(run_dir, _hooks=hooks)
        assert replacement_results == [False]
        assert read_json(run_dir / "verification.json") == operation.verification
        assert operation.report_path == run_dir / "report.md"
        assert operation.report_path.is_file()
        assert not parked.exists()
        _assert_no_derived_temporary_files(run_dir)
    else:
        with pytest.raises(ConfigError, match="operation-start identity"):
            verify_and_report_bundle(run_dir, _hooks=hooks)
        assert replacement_results == [True]
        _assert_replacement_root_untouched(run_dir)
        assert read_json(parked / "verification.json")["verification_status"] == "complete"
        assert (parked / "report.md").read_text(encoding="utf-8").startswith(
            "# ReproTrace Evidence Report"
        )
        _assert_no_derived_temporary_files(parked)


def test_report_render_failure_leaves_both_outputs_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = make_bundle(tmp_path)

    def fail(evidence: Any, verification: Any) -> str:
        raise ConfigError("simulated report render failure")

    monkeypatch.setattr(operations_module, "render_report", fail)

    with pytest.raises(ConfigError, match="report render failure"):
        verify_and_report_bundle(run_dir)

    assert not (run_dir / "verification.json").exists()
    assert not (run_dir / "report.md").exists()


def test_verification_write_failure_leaves_both_outputs_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = make_bundle(tmp_path)

    def fail(*args: Any, **kwargs: Any) -> Path:
        raise ConfigError("simulated verification write failure")

    monkeypatch.setattr(operations_module, "write_session_derived_json", fail)

    with pytest.raises(ConfigError, match="verification write failure"):
        verify_and_report_bundle(run_dir)

    assert not (run_dir / "verification.json").exists()
    assert not (run_dir / "report.md").exists()


def test_report_write_failure_keeps_fresh_verification_and_no_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    published: list[dict[str, Any]] = []
    original_replace = DerivedOutputLifecycleGuard._replace_temporary

    def record(session: Any, verification: Any, report: str) -> None:
        published.append(dict(verification))

    def fail_report_replace(
        guard: DerivedOutputLifecycleGuard,
        temporary_name: str,
        relative_path: str,
    ) -> None:
        if relative_path == "report.md":
            raise OSError("simulated report atomic replace failure")
        original_replace(guard, temporary_name, relative_path)

    monkeypatch.setattr(
        DerivedOutputLifecycleGuard,
        "_replace_temporary",
        fail_report_replace,
    )

    with pytest.raises(ConfigError, match="report atomic replace failure"):
        verify_and_report_bundle(
            run_dir,
            _hooks=_VerifyReportTestHooks(before_report_write=record),
        )

    assert read_json(run_dir / "verification.json") == published[0]
    assert not (run_dir / "report.md").exists()
    _assert_no_derived_temporary_files(run_dir)


def _convert_to_schema_zero(run_dir: Path) -> None:
    run = read_json(run_dir / "run.json")
    run["schema_version"] = 0
    write_json(run_dir / "run.json", run)
    (run_dir / "evidence.index.json").unlink()


def test_schema_zero_refresh_uses_lifecycle_without_assurance_upgrade(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path, metrics=False)
    _convert_to_schema_zero(run_dir)

    operation = verify_and_report_bundle(run_dir)

    assert operation.legacy_bundle is True
    assert operation.verification["assurance_level"] == "recorded"
    assert operation.verification["result_status"] == "not_evaluated"
    assert operation.verification["evidence_root_sha256"] is None
    assert read_json(run_dir / "verification.json") == operation.verification
    assert operation.report_path.is_file()


def test_schema_zero_publication_never_mutates_replacement_root(
    tmp_path: Path,
) -> None:
    run_dir, _ = make_bundle(tmp_path, metrics=False)
    _convert_to_schema_zero(run_dir)
    parked = run_dir.with_name(run_dir.name + "-parked-schema-zero-report")
    replacement_results: list[bool] = []

    def replace_before_report(
        guard: DerivedOutputLifecycleGuard,
        temporary_name: str,
        relative_path: str,
    ) -> None:
        if relative_path == "report.md":
            assert temporary_name.startswith(".report.md.")
            replacement_results.append(
                _attempt_bundle_root_replacement(run_dir, parked)
            )

    hooks = _VerifyReportTestHooks(
        lifecycle=_DerivedOutputLifecycleTestHooks(
            before_atomic_replace=replace_before_report
        )
    )

    if os.name == "nt":
        operation = verify_and_report_bundle(run_dir, _hooks=hooks)
        assert replacement_results == [False]
        assert operation.legacy_bundle is True
        assert operation.verification["assurance_level"] == "recorded"
        assert operation.verification["result_status"] == "not_evaluated"
        assert operation.verification["evidence_root_sha256"] is None
        assert read_json(run_dir / "verification.json") == operation.verification
        assert operation.report_path.is_file()
        assert not parked.exists()
        _assert_no_derived_temporary_files(run_dir)
    else:
        with pytest.raises(ConfigError, match="operation-start identity"):
            verify_and_report_bundle(run_dir, _hooks=hooks)
        assert replacement_results == [True]
        _assert_replacement_root_untouched(run_dir)
        verification = read_json(parked / "verification.json")
        assert verification["assurance_level"] == "recorded"
        assert verification["result_status"] == "not_evaluated"
        assert verification["evidence_root_sha256"] is None
        assert (parked / "report.md").is_file()
        _assert_no_derived_temporary_files(parked)


def test_schema_zero_failed_refresh_removes_historical_outputs(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path, metrics=False)
    _convert_to_schema_zero(run_dir)
    (run_dir / "metrics.json").unlink()

    with pytest.raises(ConfigError, match="missing: metrics.json"):
        verify_and_report_bundle(run_dir)

    assert not (run_dir / "verification.json").exists()
    assert not (run_dir / "report.md").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX final symlink semantics")
def test_posix_final_symlink_is_unlinked_without_touching_target(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    outside = tmp_path / "outside-verification.json"
    outside.write_bytes(b"outside target\n")
    (run_dir / "verification.json").unlink()
    (run_dir / "verification.json").symlink_to(outside)

    result = verify_bundle(run_dir, write=True)

    assert outside.read_bytes() == b"outside target\n"
    assert not (run_dir / "verification.json").is_symlink()
    assert read_json(run_dir / "verification.json") == result
    assert not (run_dir / "report.md").exists()


def test_hard_link_entry_is_unlinked_without_touching_other_entry(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    outside = tmp_path / "hard-link-verification.json"
    outside.write_bytes(b"hard-linked historical output\n")
    (run_dir / "verification.json").unlink()
    os.link(outside, run_dir / "verification.json")

    result = verify_bundle(run_dir, write=True)

    assert outside.read_bytes() == b"hard-linked historical output\n"
    assert not os.path.samefile(outside, run_dir / "verification.json")
    assert read_json(run_dir / "verification.json") == result


def test_directory_canonical_output_fails_before_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    old_report = (run_dir / "report.md").read_bytes()
    (run_dir / "verification.json").unlink()
    (run_dir / "verification.json").mkdir()

    def verification_forbidden(directory: Path) -> Any:
        raise AssertionError("verification continued after invalid canonical output")

    monkeypatch.setattr(
        verifier_module,
        "_open_schema_one_snapshot_for_production",
        verification_forbidden,
    )

    with pytest.raises(ConfigError, match="regular file or final symlink"):
        verify_bundle(run_dir, write=True)

    assert (run_dir / "verification.json").is_dir()
    assert (run_dir / "report.md").read_bytes() == old_report


@pytest.mark.skipif(os.name == "nt", reason="POSIX special-file semantics")
def test_posix_fifo_canonical_output_is_rejected(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    (run_dir / "verification.json").unlink()
    os.mkfifo(run_dir / "verification.json")

    with pytest.raises(ConfigError, match="regular file or final symlink"):
        verify_bundle(run_dir, write=True)

    assert (run_dir / "verification.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_windows_junction_canonical_output_is_rejected(tmp_path: Path) -> None:
    run_dir, _ = make_bundle(tmp_path)
    outside = tmp_path / "junction-target"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside\n", encoding="utf-8")
    junction = run_dir / "verification.json"
    junction.unlink()
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"junction creation unavailable: {result.stderr or result.stdout}")
    try:
        with pytest.raises(ConfigError, match="reparse object|regular file"):
            verify_bundle(run_dir, write=True)
        assert sentinel.read_text(encoding="utf-8") == "outside\n"
        assert junction.exists()
    finally:
        os.rmdir(junction)


@pytest.mark.skipif(os.name != "nt", reason="Windows opened-file sharing semantics")
def test_windows_opened_canonical_file_delete_failure_stops_before_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = make_bundle(tmp_path)
    verification_path = run_dir / "verification.json"

    def verification_forbidden(directory: Path) -> Any:
        raise AssertionError("verification continued after invalidation failure")

    monkeypatch.setattr(
        verifier_module,
        "_open_schema_one_snapshot_for_production",
        verification_forbidden,
    )
    descriptor = os.open(
        verification_path,
        os.O_RDONLY | getattr(os, "O_BINARY", 0),
    )
    try:
        with pytest.raises(ConfigError, match="cannot invalidate canonical derived output"):
            verify_bundle(run_dir, write=True)
    finally:
        os.close(descriptor)

    assert verification_path.is_file()
    assert (run_dir / "report.md").is_file()


def test_refresh_does_not_change_index_bytes_or_evidence_root(tmp_path: Path) -> None:
    run_dir, initial = make_bundle(tmp_path)
    index_before = (run_dir / "evidence.index.json").read_bytes()

    verification_only = verify_bundle(run_dir, write=True)
    combined = verify_and_report_bundle(run_dir)

    assert (run_dir / "evidence.index.json").read_bytes() == index_before
    assert verification_only["evidence_root_sha256"] == initial["evidence_root_sha256"]
    assert combined.verification["evidence_root_sha256"] == initial["evidence_root_sha256"]


def test_runner_finalization_treats_initial_invalidation_as_noop(tmp_path: Path) -> None:
    run_dir, verification = make_bundle(tmp_path)

    assert verification["verification_status"] == "complete"
    assert (run_dir / "verification.json").is_file()
    assert (run_dir / "report.md").is_file()
