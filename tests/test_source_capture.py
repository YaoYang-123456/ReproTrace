from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import reprotrace.capture as capture_module
import reprotrace.runner as runner_module
from reprotrace.capture import GitResult, SourceCapture, capture_source
from reprotrace.cli import main as cli_main
from reprotrace.diffing import compare_bundles
from reprotrace.errors import ConfigError
from reprotrace.io import read_json, sha256_bytes, write_json
from reprotrace.manifest import LoadedManifest, load_manifest
from reprotrace.reporting import generate_report
from reprotrace.runner import run_manifest
from reprotrace.verifier import verify_bundle


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=False,
        env=environment,
    )


def initialize_repository(root: Path, files: dict[str, bytes] | None = None) -> str:
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "core.autocrlf", "false")
    for name, content in (files or {"tracked.txt": b"initial\n"}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "fixture")
    return git(root, "rev-parse", "HEAD").stdout.strip().decode("ascii")


def make_manifest(tmp_path: Path, project: Path, *, allow_dirty: bool = True) -> Path:
    adapter = tmp_path / "adapter"
    adapter.mkdir(exist_ok=True)
    manifest = adapter / "reprotrace.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 0,
                "project": {"name": "source-test", "root": ".", "allow_dirty": allow_dirty},
                "run": {
                    "output_root": ".evidence",
                    "steps": [{"id": "noop", "argv": [sys.executable, "-V"]}],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return manifest


def loaded_manifest(tmp_path: Path, project: Path) -> LoadedManifest:
    return load_manifest(make_manifest(tmp_path, project), project_root=project)


def make_bundle(tmp_path: Path, *, dirty: bool = False) -> tuple[Path, Path, Path]:
    project = tmp_path / "repository"
    initialize_repository(project)
    if dirty:
        (project / "tracked.txt").write_text("changed 状态\n", encoding="utf-8")
    manifest = make_manifest(tmp_path, project)
    run_dir, _ = run_manifest(manifest, dry_run=True, project_root=project)
    return project, manifest, run_dir


def direct_patch(project: Path, record: dict[str, object]) -> bytes:
    metadata = record["git_patch"]
    assert isinstance(metadata, dict)
    argv = metadata["argv"]
    assert isinstance(argv, list)
    process = git(project, *argv[1:])
    return process.stdout


def test_clean_repository_records_empty_raw_source_files(tmp_path: Path) -> None:
    project = tmp_path / "repository"
    initialize_repository(project)

    captured = capture_source(loaded_manifest(tmp_path, project))

    empty_hash = sha256_bytes(b"")
    assert captured.record["dirty"] is False
    assert captured.record["diff_sha256"] is None
    assert captured.record["git_patch"]["sha256"] == empty_hash
    assert captured.record["git_status"]["sha256"] == empty_hash
    assert captured.files == {"source.status": b"", "source.patch": b""}


@pytest.mark.parametrize(
    "content",
    ["changed 状态\n".encode("utf-8"), b"changed GBK: \x81\x40\n"],
    ids=["utf8", "gbk"],
)
def test_non_ascii_text_patch_is_captured_without_decoding(tmp_path: Path, content: bytes) -> None:
    project = tmp_path / "repository"
    initialize_repository(project)
    (project / "tracked.txt").write_bytes(content)

    captured = capture_source(loaded_manifest(tmp_path, project))
    expected = direct_patch(project, captured.record)

    assert captured.files["source.patch"] == expected
    assert captured.record["diff_sha256"] == sha256_bytes(expected)
    assert captured.record["git_patch"]["sha256"] == sha256_bytes(expected)


def test_non_ascii_filename_is_preserved_in_raw_status(tmp_path: Path) -> None:
    project = tmp_path / "repository"
    initialize_repository(project, {"状态.txt": b"before\n"})
    (project / "状态.txt").write_text("after 状态\n", encoding="utf-8")

    captured = capture_source(loaded_manifest(tmp_path, project))

    assert "状态.txt".encode("utf-8") in captured.files["source.status"]
    assert b"\0" in captured.files["source.status"]


def test_binary_patch_can_be_checked_and_applied(tmp_path: Path) -> None:
    project = tmp_path / "repository"
    initialize_repository(project, {"weights.bin": b"\x00before\xff"})
    expected_content = b"\x00after\x80\xff\x01"
    (project / "weights.bin").write_bytes(expected_content)

    captured = capture_source(loaded_manifest(tmp_path, project))
    patch = captured.files["source.patch"]
    assert b"GIT binary patch" in patch

    clean_checkout = tmp_path / "clean-checkout"
    subprocess.run(["git", "clone", "-q", str(project), str(clean_checkout)], check=True)
    patch_path = tmp_path / "captured.patch"
    patch_path.write_bytes(patch)
    git(clean_checkout, "apply", "--check", str(patch_path))
    git(clean_checkout, "apply", str(patch_path))
    assert (clean_checkout / "weights.bin").read_bytes() == expected_content


def test_patch_contains_staged_and_unstaged_changes(tmp_path: Path) -> None:
    project = tmp_path / "repository"
    initialize_repository(project, {"staged.txt": b"before\n", "unstaged.txt": b"before\n"})
    (project / "staged.txt").write_bytes(b"staged\n")
    git(project, "add", "staged.txt")
    (project / "unstaged.txt").write_bytes(b"unstaged\n")

    captured = capture_source(loaded_manifest(tmp_path, project))
    patch = captured.files["source.patch"]

    assert b"staged.txt" in patch
    assert b"unstaged.txt" in patch


def test_untracked_only_state_has_partial_coverage_and_no_diff_hash(tmp_path: Path) -> None:
    project = tmp_path / "repository"
    initialize_repository(project)
    (project / "untracked.txt").write_bytes(b"not copied\n")

    captured = capture_source(loaded_manifest(tmp_path, project))

    assert captured.record["dirty"] is True
    assert captured.record["diff_sha256"] is None
    assert captured.files["source.patch"] == b""
    assert captured.record["git_patch"]["sha256"] == sha256_bytes(b"")
    assert captured.record["git_status"]["sha256"] == sha256_bytes(captured.files["source.status"])
    assert captured.record["summary"]["untracked_file_count"] == 1
    assert captured.record["coverage"]["replay"] == "partial"


def test_untracked_count_covers_git_worktree_outside_project_subdirectory(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    initialize_repository(repository, {"project/tracked.txt": b"tracked\n"})
    (repository / "outside-untracked.txt").write_bytes(b"not copied\n")
    project = repository / "project"

    captured = capture_source(loaded_manifest(tmp_path, project))

    assert captured.record["root"] == str(project.resolve())
    assert captured.record["worktree_root"] == str(repository.resolve())
    assert captured.record["summary"]["untracked_file_count"] == 1
    assert b"outside-untracked.txt" in captured.files["source.status"]


def test_git_worktree_dot_git_file_is_supported_from_subdirectory(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    initialize_repository(repository)
    worktree = tmp_path / "linked-worktree"
    git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    assert (worktree / ".git").is_file()
    project = worktree / "nested"
    project.mkdir()

    captured = capture_source(loaded_manifest(tmp_path, project))

    assert captured.record["available"] is True
    assert captured.worktree_root == worktree.resolve()
    assert captured.record["worktree_root"] == str(worktree.resolve())


def test_non_git_and_unborn_repositories_remain_unavailable(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    plain_capture = capture_source(loaded_manifest(tmp_path, plain))
    assert plain_capture.record["available"] is False
    assert plain_capture.record["reason"] == "not_git_repository"
    assert plain_capture.files == {}

    unborn = tmp_path / "unborn"
    unborn.mkdir()
    git(unborn, "init", "-q", "-b", "main")
    unborn_capture = capture_source(loaded_manifest(tmp_path, unborn))
    assert unborn_capture.record["available"] is False
    assert unborn_capture.record["reason"] == "unborn_head"
    assert unborn_capture.files == {}


def test_missing_git_remains_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    project.mkdir()

    def missing_git(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(capture_module.subprocess, "run", missing_git)
    captured = capture_source(loaded_manifest(tmp_path, project))

    assert captured.record["available"] is False
    assert captured.record["reason"] == "git_unavailable"


def test_git_probe_error_with_ancestor_marker_is_not_reported_as_non_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    initialize_repository(repository, {"nested/tracked.txt": b"tracked\n"})
    project = repository / "nested"
    original = capture_module._git_bytes

    def fail_repository_probe(root: Path, *args: str) -> GitResult:
        if args == ("rev-parse", "--is-inside-work-tree"):
            return GitResult(128, b"", b"unsafe repository \x81")
        return original(root, *args)

    monkeypatch.setattr(capture_module, "_git_bytes", fail_repository_probe)

    with pytest.raises(ConfigError, match=r"repository state.*non-authoritative.*\\x81"):
        capture_source(loaded_manifest(tmp_path, project))


def test_critical_git_capture_failure_is_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "repository"
    initialize_repository(project)
    original = capture_module._git_bytes

    def fail_status(root: Path, *args: str) -> GitResult:
        if args and args[0] == "status":
            return GitResult(1, b"", b"failure \x81")
        return original(root, *args)

    monkeypatch.setattr(capture_module, "_git_bytes", fail_status)

    with pytest.raises(ConfigError, match=r"non-authoritative.*\\x81"):
        capture_source(loaded_manifest(tmp_path, project))


def test_output_isolation_does_not_repeat_worktree_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "repository"
    initialize_repository(project)
    manifest = make_manifest(tmp_path, project)
    original = capture_module._git_bytes
    discoveries = 0

    def fail_second_discovery(root: Path, *args: str) -> GitResult:
        nonlocal discoveries
        if args == ("rev-parse", "--show-toplevel"):
            discoveries += 1
            if discoveries > 1:
                return GitResult(128, b"", b"second discovery must not run")
        return original(root, *args)

    monkeypatch.setattr(capture_module, "_git_bytes", fail_second_discovery)

    run_dir, verification = run_manifest(manifest, dry_run=True, project_root=project)

    assert discoveries == 1
    assert run_dir.is_dir()
    assert verification["preflight_passed"] is True


def test_output_isolation_check_ignore_failure_creates_no_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_data = {
        "schema_version": 0,
        "project": {"name": "isolation-test", "root": ".", "allow_dirty": True},
        "run": {
            "output_root": ".evidence",
            "steps": [{"id": "noop", "argv": [sys.executable, "-V"]}],
        },
    }
    project = tmp_path / "repository"
    manifest_bytes = yaml.safe_dump(manifest_data, sort_keys=False).encode("utf-8")
    initialize_repository(project, {"reprotrace.yaml": manifest_bytes})
    manifest = project / "reprotrace.yaml"
    original = capture_module._git_bytes

    def fail_check_ignore(root: Path, *args: str) -> GitResult:
        if args and args[0] == "check-ignore":
            return GitResult(2, b"", b"ignore failure \x81")
        return original(root, *args)

    monkeypatch.setattr(capture_module, "_git_bytes", fail_check_ignore)

    with pytest.raises(ConfigError, match=r"output ignore state.*non-authoritative.*\\x81"):
        run_manifest(manifest, dry_run=True, project_root=project)
    assert not (project / ".evidence").exists()


def test_output_isolation_requires_captured_worktree_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "repository"
    initialize_repository(project)
    manifest = make_manifest(tmp_path, project)
    source = SourceCapture(record={"available": True}, files={}, worktree_root=None)
    monkeypatch.setattr(runner_module, "capture_source", lambda loaded: source)

    with pytest.raises(ConfigError, match="captured Git worktree root is missing"):
        run_manifest(manifest, dry_run=True, project_root=project)
    assert not (manifest.parent / ".evidence").exists()


def test_diff_serialization_config_is_neutralized(tmp_path: Path) -> None:
    project = tmp_path / "repository"
    initialize_repository(project, {"a.txt": b"one\n\nthree\nfour\nfive\n", "z.txt": b"before\n"})
    (project / "a.txt").write_bytes(b"ONE\n\nthree\nfour\nFIVE\n")
    (project / "z.txt").write_bytes(b"after\n")
    manifest = loaded_manifest(tmp_path, project)

    baseline = capture_source(manifest).files["source.patch"]
    order_file = project / ".git" / "reprotrace-test-order"
    order_file.write_text("z.txt\na.txt\n", encoding="utf-8")
    git(project, "config", "diff.orderFile", str(order_file))
    git(project, "config", "diff.interHunkContext", "99")
    git(project, "config", "diff.suppressBlankEmpty", "true")
    configured_direct = git(project, "diff", "--binary", "--full-index", "HEAD", "--").stdout

    configured_capture = capture_source(manifest).files["source.patch"]

    assert configured_direct != baseline
    assert configured_capture == baseline


def test_bundle_writes_and_verifies_raw_source_evidence(tmp_path: Path) -> None:
    _, _, run_dir = make_bundle(tmp_path, dirty=True)
    source = read_json(run_dir / "source.json")

    assert (run_dir / source["git_patch"]["path"]).read_bytes()
    assert source["git_patch"]["sha256"] == sha256_bytes((run_dir / "source.patch").read_bytes())
    result = verify_bundle(run_dir, write=False)
    assert next(check for check in result["checks"] if check["id"] == "source:git_patch")["passed"] is True
    assert next(check for check in result["checks"] if check["id"] == "source:git_status")["passed"] is True


@pytest.mark.parametrize("filename", ["source.patch", "source.status"])
def test_tampered_source_evidence_fails_verification(tmp_path: Path, filename: str) -> None:
    _, _, run_dir = make_bundle(tmp_path, dirty=True)
    (run_dir / filename).write_bytes(b"tampered")

    result = verify_bundle(run_dir, write=False)

    check_id = "source:git_patch" if filename.endswith("patch") else "source:git_status"
    assert next(check for check in result["checks"] if check["id"] == check_id)["passed"] is False


def test_missing_source_evidence_fails_verification(tmp_path: Path) -> None:
    _, _, run_dir = make_bundle(tmp_path, dirty=True)
    (run_dir / "source.patch").unlink()

    result = verify_bundle(run_dir, write=False)

    check = next(check for check in result["checks"] if check["id"] == "source:git_patch")
    assert check["passed"] is False
    assert check["reason"] == "recorded Git patch evidence file is missing"


@pytest.mark.parametrize("unsafe", ["../outside.patch", "/absolute.patch", "C:\\outside.patch", "C:outside.patch", "\\\\server\\share\\patch"])
def test_source_evidence_rejects_unsafe_paths(tmp_path: Path, unsafe: str) -> None:
    _, _, run_dir = make_bundle(tmp_path)
    source = read_json(run_dir / "source.json")
    source["git_patch"]["path"] = unsafe
    write_json(run_dir / "source.json", source)

    with pytest.raises(ConfigError, match="invalid Git patch evidence path"):
        verify_bundle(run_dir, write=False)


def test_source_evidence_rejects_symlink_escape(tmp_path: Path) -> None:
    _, _, run_dir = make_bundle(tmp_path)
    outside = tmp_path / "outside.patch"
    outside.write_bytes(b"outside")
    link = run_dir / "linked.patch"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    source = read_json(run_dir / "source.json")
    source["git_patch"]["path"] = link.name
    write_json(run_dir / "source.json", source)

    with pytest.raises(ConfigError, match="path escapes the evidence bundle"):
        verify_bundle(run_dir, write=False)


def test_source_evidence_rejects_parent_link_escape(tmp_path: Path) -> None:
    _, _, run_dir = make_bundle(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.patch").write_bytes(b"outside")
    link = run_dir / "linked-directory"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation unavailable: {exc}")
    source = read_json(run_dir / "source.json")
    source["git_patch"]["path"] = "linked-directory/source.patch"
    write_json(run_dir / "source.json", source)

    with pytest.raises(ConfigError, match="path escapes the evidence bundle"):
        verify_bundle(run_dir, write=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_source_evidence_rejects_junction_escape(tmp_path: Path) -> None:
    _, _, run_dir = make_bundle(tmp_path)
    outside = tmp_path / "junction-target"
    outside.mkdir()
    (outside / "source.patch").write_bytes(b"outside")
    junction = run_dir / "junction"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        check=False,
        capture_output=True,
        text=False,
    )
    if created.returncode != 0:
        pytest.skip("junction creation unavailable")
    try:
        source = read_json(run_dir / "source.json")
        source["git_patch"]["path"] = "junction/source.patch"
        write_json(run_dir / "source.json", source)
        with pytest.raises(ConfigError, match="path escapes the evidence bundle"):
            verify_bundle(run_dir, write=False)
    finally:
        os.rmdir(junction)


def test_source_evidence_requires_regular_files(tmp_path: Path) -> None:
    _, _, run_dir = make_bundle(tmp_path)
    source = read_json(run_dir / "source.json")
    source["git_patch"]["path"] = "artifacts"
    write_json(run_dir / "source.json", source)

    with pytest.raises(ConfigError, match="expected a regular file"):
        verify_bundle(run_dir, write=False)


def test_source_json_is_not_written_after_binary_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "repository"
    initialize_repository(project)
    (project / "tracked.txt").write_text("changed\n", encoding="utf-8")
    manifest = make_manifest(tmp_path, project)
    original = runner_module.write_bytes_atomic

    def fail_patch(path: Path, value: bytes) -> None:
        if path.name == "source.patch":
            raise OSError("simulated write failure")
        original(path, value)

    monkeypatch.setattr(runner_module, "write_bytes_atomic", fail_patch)

    with pytest.raises(OSError, match="simulated write failure"):
        run_manifest(manifest, dry_run=True, project_root=project)

    run_dirs = list((manifest.parent / ".evidence").iterdir())
    assert len(run_dirs) == 1
    assert not (run_dirs[0] / "source.json").exists()


def test_legacy_source_bundle_remains_readable(tmp_path: Path) -> None:
    _, _, run_dir = make_bundle(tmp_path)
    source = read_json(run_dir / "source.json")
    legacy = {
        key: source.get(key)
        for key in (
            "available",
            "root",
            "commit",
            "branch",
            "remote",
            "dirty",
            "diff_sha256",
            "expected_repo",
            "expected_ref",
            "allow_dirty",
        )
    }
    write_json(run_dir / "source.json", legacy)
    (run_dir / "source.patch").unlink()
    (run_dir / "source.status").unlink()

    result = verify_bundle(run_dir, write=False)

    assert result["preflight_passed"] is True
    assert generate_report(run_dir).is_file()
    assert compare_bundles(run_dir, run_dir)["identical"] is True


@pytest.mark.parametrize("consumer", ["verify", "diff", "report"])
@pytest.mark.parametrize(
    "damage",
    ["array-root", "unknown-schema", "summary-null", "coverage-null", "git-null", "git-status-null", "git-patch-null"],
)
def test_source_record_validation_is_consistent_across_consumers(
    tmp_path: Path, consumer: str, damage: str
) -> None:
    _, _, run_dir = make_bundle(tmp_path)
    source_path = run_dir / "source.json"
    source = read_json(source_path)
    if damage == "array-root":
        damaged: object = []
    elif damage == "unknown-schema":
        source["schema_version"] = 2
        damaged = source
    else:
        field = damage.removesuffix("-null").replace("-", "_")
        source[field] = None
        damaged = source
    write_json(source_path, damaged)

    with pytest.raises(ConfigError, match="source.json"):
        if consumer == "verify":
            verify_bundle(run_dir, write=False)
        elif consumer == "diff":
            compare_bundles(run_dir, run_dir)
        else:
            generate_report(run_dir)


@pytest.mark.parametrize("consumer", ["verify", "diff", "report"])
def test_malformed_source_record_cli_error_has_no_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], consumer: str
) -> None:
    _, _, run_dir = make_bundle(tmp_path)
    write_json(run_dir / "source.json", [])
    argv = [consumer, str(run_dir), str(run_dir)] if consumer == "diff" else [consumer, str(run_dir)]

    exit_code = cli_main(argv)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "reprotrace: invalid source.json" in captured.err
    assert "Traceback" not in captured.err


def test_report_warns_that_untracked_contents_are_not_captured(tmp_path: Path) -> None:
    project = tmp_path / "repository"
    initialize_repository(project)
    (project / "untracked.txt").write_bytes(b"not copied\n")
    manifest = make_manifest(tmp_path, project)

    run_dir, _ = run_manifest(manifest, dry_run=True, project_root=project)
    report = (run_dir / "report.md").read_text(encoding="utf-8")

    assert "WARNING" in report
    assert "cannot fully replay the source worktree" in report


def test_bundle_diff_compares_patch_and_status_hashes(tmp_path: Path) -> None:
    project = tmp_path / "repository"
    initialize_repository(project)
    manifest = make_manifest(tmp_path, project)
    (project / "tracked.txt").write_bytes(b"first\n")
    left, _ = run_manifest(manifest, dry_run=True, project_root=project)
    (project / "tracked.txt").write_bytes(b"second\n")
    (project / "untracked.txt").write_bytes(b"status changed\n")
    right, _ = run_manifest(manifest, dry_run=True, project_root=project)

    fields = {item["field"] for item in compare_bundles(left, right)["differences"]}

    assert "source.diff_sha256" in fields
    assert "source.status_sha256" in fields
