from __future__ import annotations

import errno
import os
import subprocess
from pathlib import Path

import pytest
import reprotrace.acquisition as acquisition_module

from reprotrace.acquisition import (
    EvidenceAcquisitionError,
    _AcquisitionTestHooks,
    acquire_bundle_evidence,
    capture_bundle_file_once,
    capture_bundle_root_identity,
)
from reprotrace.evidence import canonical_evidence_index_bytes
from reprotrace.io import sha256_bytes
from reprotrace.snapshot import (
    BundleRootIdentity,
    EvidenceObjectState,
    FileIdentity,
    SemanticEvidenceUnavailable,
    SessionState,
    SnapshotStateError,
    StorageKind,
    VerificationSession,
    VerifiedBundleSnapshot,
    VerifiedEvidenceObject,
)


def make_owned_evidence(
    tmp_path: Path,
    *,
    relative_path: str = "evidence/data.bin",
    live_bytes: bytes = b"captured evidence",
    expected_bytes: bytes | None = None,
    storage_kind: StorageKind = StorageKind.MEMORY,
    spool_max_size: int = 1024,
    root_identity_override: BundleRootIdentity | None = None,
) -> tuple[Path, Path, VerifiedBundleSnapshot, VerifiedEvidenceObject, VerificationSession]:
    root = tmp_path / "bundle"
    candidate = root.joinpath(*relative_path.split("/"))
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(live_bytes)
    expected = live_bytes if expected_bytes is None else expected_bytes
    roles = ["metric_source"]
    index = {
        "schema_version": 1,
        "entries": [
            {
                "path": relative_path,
                "roles": roles,
                "size_bytes": len(expected),
                "sha256": sha256_bytes(expected),
            }
        ],
    }
    encoded = canonical_evidence_index_bytes(index)
    snapshot = VerifiedBundleSnapshot(
        bundle_root=str(root),
        root_identity=root_identity_override or capture_bundle_root_identity(root),
        index_bytes=encoded,
        parsed_index=index,
    )
    evidence = VerifiedEvidenceObject(
        relative_path=relative_path,
        roles=roles,
        expected_size=len(expected),
        expected_sha256=sha256_bytes(expected),
        storage_kind=storage_kind,
        spool_max_size=spool_max_size,
    )
    session = VerificationSession(snapshot)
    snapshot.add_evidence(evidence)
    return root, candidate, snapshot, evidence, session


def test_normal_memory_acquisition_uses_retained_bytes(tmp_path: Path) -> None:
    value = b"structured\x00evidence"
    root, _, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        live_bytes=value,
    )

    def assert_non_inheritable(descriptor: int, path: Path) -> None:
        assert os.get_inheritable(descriptor) is False

    with session:
        result = acquire_bundle_evidence(
            bundle_root=root,
            snapshot=snapshot,
            evidence=evidence,
            chunk_size=3,
            _hooks=_AcquisitionTestHooks(
                after_open_before_postcheck=assert_non_inheritable
            ),
        )
        assert result is None
        assert evidence.state is EvidenceObjectState.ACQUIRED
        assert evidence.acquisition_identity is not None
        assert evidence.acquisition_identity.available is True
        evidence.seal()
        with evidence.open_reader() as reader:
            assert reader.read() == value


def test_bootstrap_capture_returns_exact_immutable_handle_bound_bytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bootstrap"
    root.mkdir()
    candidate = root / "run.json"
    value = b'{"schema_version":1}\x00\xff'
    candidate.write_bytes(value)
    root_identity = capture_bundle_root_identity(root)

    captured = capture_bundle_file_once(
        bundle_root=root,
        expected_root_identity=root_identity,
        relative_path="run.json",
        chunk_size=3,
    )

    assert captured.relative_path == "run.json"
    assert captured.exact_bytes == value
    assert isinstance(captured.exact_bytes, bytes)
    assert captured.observed_fingerprint.size_bytes == len(value)
    assert captured.observed_fingerprint.sha256 == sha256_bytes(value)
    assert captured.file_identity.available is True
    assert captured.root_identity == root_identity


def test_acquisition_rejects_claimed_but_inactive_session(tmp_path: Path) -> None:
    root, _, snapshot, evidence, session = make_owned_evidence(tmp_path)

    with pytest.raises(SnapshotStateError, match="active verification session"):
        acquire_bundle_evidence(
            bundle_root=root,
            snapshot=snapshot,
            evidence=evidence,
        )

    assert session.state is SessionState.NEW
    assert evidence.state is EvidenceObjectState.NEW
    session.close()


def test_normal_spool_acquisition_streams_exact_bytes(tmp_path: Path) -> None:
    value = b"raw metric bytes" * 10
    root, _, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        live_bytes=value,
        storage_kind=StorageKind.SPOOL,
        spool_max_size=8,
    )

    with session:
        acquire_bundle_evidence(
            bundle_root=root,
            snapshot=snapshot,
            evidence=evidence,
            chunk_size=7,
        )
        assert evidence.state is EvidenceObjectState.ACQUIRED
        assert evidence.spool_rolled_to_disk is True
        evidence.seal()
        with evidence.open_reader() as reader:
            assert reader.read() == value


def test_integrity_only_acquisition_validates_without_reader(tmp_path: Path) -> None:
    value = b"integrity only"
    root, _, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        live_bytes=value,
        storage_kind=StorageKind.INTEGRITY_ONLY,
    )

    with session:
        acquire_bundle_evidence(
            bundle_root=root,
            snapshot=snapshot,
            evidence=evidence,
            chunk_size=2,
        )
        assert evidence.observed_fingerprint == evidence.expected_fingerprint
        evidence.seal()
        with pytest.raises(SemanticEvidenceUnavailable, match="integrity-only"):
            evidence.open_reader()


def test_acquisition_uses_one_open_and_only_bounded_descriptor_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = b"bounded reads require multiple chunks"
    root, _, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        live_bytes=value,
    )
    requested_sizes: list[int] = []
    opened_paths: list[Path] = []
    original_open = os.open

    def single_open(path: str | os.PathLike[str], flags: int) -> int:
        opened_paths.append(Path(path))
        return original_open(path, flags)

    def bounded_reader(descriptor: int, size: int) -> bytes:
        requested_sizes.append(size)
        return os.read(descriptor, size)

    monkeypatch.setattr(acquisition_module.os, "open", single_open)
    hooks = _AcquisitionTestHooks(read_chunk=bounded_reader)
    with session:
        acquire_bundle_evidence(
            bundle_root=root,
            snapshot=snapshot,
            evidence=evidence,
            chunk_size=4,
            _hooks=hooks,
        )

    assert opened_paths == [root / "evidence" / "data.bin"]
    assert len(requested_sizes) > 2
    assert set(requested_sizes) == {4}


def test_fingerprint_mismatch_fails_owned_object(tmp_path: Path) -> None:
    root, _, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        live_bytes=b"actual bytes",
        expected_bytes=b"different expected bytes",
        storage_kind=StorageKind.SPOOL,
    )

    with pytest.raises(EvidenceAcquisitionError, match="fingerprint_mismatch"):
        with session:
            acquire_bundle_evidence(
                bundle_root=root,
                snapshot=snapshot,
                evidence=evidence,
                chunk_size=3,
            )

    assert evidence.state is EvidenceObjectState.FAILED
    assert evidence.snapshot_owned is True
    assert evidence.closed is True
    assert session.state is SessionState.CLOSED


def test_final_symlink_swap_before_open_fails_without_reading_outside(
    tmp_path: Path,
) -> None:
    original = b"intended A"
    outside_bytes = b"outside sentinel B"
    root, candidate, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        live_bytes=original,
    )
    outside = tmp_path / "outside.bin"
    outside.write_bytes(outside_bytes)
    read_called = False

    def swap_to_symlink(path: Path) -> None:
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"file symlink creation unavailable: {exc}")

    def reader(descriptor: int, size: int) -> bytes:
        nonlocal read_called
        read_called = True
        return os.read(descriptor, size)

    hooks = _AcquisitionTestHooks(
        after_precheck=swap_to_symlink,
        read_chunk=reader,
    )
    with pytest.raises(EvidenceAcquisitionError):
        with session:
            acquire_bundle_evidence(
                bundle_root=root,
                snapshot=snapshot,
                evidence=evidence,
                _hooks=hooks,
            )

    assert read_called is False
    assert evidence.state is EvidenceObjectState.FAILED
    assert evidence.observed_fingerprint is None
    assert outside.read_bytes() == outside_bytes
    assert candidate.is_symlink()


def test_final_regular_file_replacement_before_open_fails_identity(
    tmp_path: Path,
) -> None:
    root, candidate, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        live_bytes=b"intended A",
    )
    parked = candidate.with_name("parked-A.bin")
    read_called = False

    def replace_with_b(path: Path) -> None:
        path.replace(parked)
        path.write_bytes(b"replacement B")

    def reader(descriptor: int, size: int) -> bytes:
        nonlocal read_called
        read_called = True
        return os.read(descriptor, size)

    hooks = _AcquisitionTestHooks(
        after_precheck=replace_with_b,
        read_chunk=reader,
    )
    with pytest.raises(
        EvidenceAcquisitionError, match="candidate_identity_unstable"
    ):
        with session:
            acquire_bundle_evidence(
                bundle_root=root,
                snapshot=snapshot,
                evidence=evidence,
                _hooks=hooks,
            )

    assert read_called is False
    assert evidence.observed_fingerprint is None


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows os.open sharing prevents renaming an opened regular file",
)
def test_post_open_regular_path_replacement_fails_before_stream(tmp_path: Path) -> None:
    root, candidate, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        live_bytes=b"opened A",
    )
    parked = candidate.with_name("opened-A.bin")
    read_called = False

    def replace_after_open(descriptor: int, path: Path) -> None:
        path.replace(parked)
        path.write_bytes(b"new path B")

    def reader(descriptor: int, size: int) -> bytes:
        nonlocal read_called
        read_called = True
        return os.read(descriptor, size)

    hooks = _AcquisitionTestHooks(
        after_open_before_postcheck=replace_after_open,
        read_chunk=reader,
    )
    with pytest.raises(
        EvidenceAcquisitionError, match="candidate_identity_unstable"
    ):
        with session:
            acquire_bundle_evidence(
                bundle_root=root,
                snapshot=snapshot,
                evidence=evidence,
                _hooks=hooks,
            )

    assert read_called is False
    assert evidence.observed_fingerprint is None


@pytest.mark.skipif(os.name != "nt", reason="Windows post-open identity fixture")
def test_windows_post_open_identity_mismatch_fails_before_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        live_bytes=b"opened A",
    )
    original_inspect = acquisition_module._inspect_candidate
    inspections = 0
    read_called = False

    def inspect_with_post_mismatch(
        inspected_root: Path,
        relative_path: str,
    ) -> tuple[Path, FileIdentity]:
        nonlocal inspections
        path, identity = original_inspect(inspected_root, relative_path)
        inspections += 1
        if inspections == 2:
            assert identity.device is not None
            assert identity.file_id is not None
            identity = FileIdentity(
                identity.mechanism,
                identity.device,
                identity.file_id + 1,
                identity.file_type,
            )
        return path, identity

    def reader(descriptor: int, size: int) -> bytes:
        nonlocal read_called
        read_called = True
        return os.read(descriptor, size)

    monkeypatch.setattr(
        acquisition_module,
        "_inspect_candidate",
        inspect_with_post_mismatch,
    )
    with pytest.raises(
        EvidenceAcquisitionError, match="candidate_identity_unstable"
    ):
        with session:
            acquire_bundle_evidence(
                bundle_root=root,
                snapshot=snapshot,
                evidence=evidence,
                _hooks=_AcquisitionTestHooks(read_chunk=reader),
            )

    assert inspections == 2
    assert read_called is False
    assert evidence.observed_fingerprint is None


def test_outside_symlink_after_open_fails_before_stream(tmp_path: Path) -> None:
    root, candidate, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        live_bytes=b"opened A",
    )
    parked = candidate.with_name("opened-A.bin")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside B")
    try:
        probe = tmp_path / "symlink-probe"
        probe.symlink_to(outside)
        probe.unlink()
    except OSError as exc:
        pytest.skip(f"file symlink creation unavailable: {exc}")
    read_called = False

    def replace_after_open(descriptor: int, path: Path) -> None:
        path.replace(parked)
        path.symlink_to(outside)

    def reader(descriptor: int, size: int) -> bytes:
        nonlocal read_called
        read_called = True
        return os.read(descriptor, size)

    hooks = _AcquisitionTestHooks(
        after_open_before_postcheck=replace_after_open,
        read_chunk=reader,
    )
    with pytest.raises(EvidenceAcquisitionError, match="candidate_path_invalid"):
        with session:
            acquire_bundle_evidence(
                bundle_root=root,
                snapshot=snapshot,
                evidence=evidence,
                _hooks=hooks,
            )

    assert read_called is False
    assert evidence.observed_fingerprint is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent symlink case")
def test_parent_symlink_swap_fails_before_stream(tmp_path: Path) -> None:
    root, candidate, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        relative_path="subdir/data.bin",
        live_bytes=b"intended A",
    )
    subdir = candidate.parent
    parked = root / "parked-subdir"
    outside = tmp_path / "outside-directory"
    outside.mkdir()
    (outside / "data.bin").write_bytes(b"outside B")
    read_called = False

    def replace_parent(path: Path) -> None:
        subdir.replace(parked)
        subdir.symlink_to(outside, target_is_directory=True)

    def reader(descriptor: int, size: int) -> bytes:
        nonlocal read_called
        read_called = True
        return os.read(descriptor, size)

    hooks = _AcquisitionTestHooks(after_precheck=replace_parent, read_chunk=reader)
    try:
        with pytest.raises(EvidenceAcquisitionError):
            with session:
                acquire_bundle_evidence(
                    bundle_root=root,
                    snapshot=snapshot,
                    evidence=evidence,
                    _hooks=hooks,
                )
        assert read_called is False
        assert evidence.observed_fingerprint is None
    finally:
        if subdir.is_symlink():
            subdir.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction case")
def test_parent_junction_swap_fails_before_stream(tmp_path: Path) -> None:
    root, candidate, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        relative_path="subdir/data.bin",
        live_bytes=b"intended A",
    )
    subdir = candidate.parent
    parked = root / "parked-subdir"
    outside = tmp_path / "outside-directory"
    outside.mkdir()
    (outside / "data.bin").write_bytes(b"outside B")
    probe = tmp_path / "junction-probe"
    probe_result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(probe), str(outside)],
        check=False,
        capture_output=True,
        text=False,
    )
    if probe_result.returncode != 0:
        pytest.skip(
            "junction creation unavailable: "
            + probe_result.stderr.decode("utf-8", errors="backslashreplace")
        )
    os.rmdir(probe)
    read_called = False

    def replace_parent(path: Path) -> None:
        subdir.replace(parked)
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(subdir), str(outside)],
            check=False,
            capture_output=True,
            text=False,
        )
        assert created.returncode == 0, created.stderr.decode(
            "utf-8", errors="backslashreplace"
        )

    def reader(descriptor: int, size: int) -> bytes:
        nonlocal read_called
        read_called = True
        return os.read(descriptor, size)

    hooks = _AcquisitionTestHooks(after_precheck=replace_parent, read_chunk=reader)
    try:
        with pytest.raises(EvidenceAcquisitionError):
            with session:
                acquire_bundle_evidence(
                    bundle_root=root,
                    snapshot=snapshot,
                    evidence=evidence,
                    _hooks=hooks,
                )
        assert read_called is False
        assert evidence.observed_fingerprint is None
    finally:
        if subdir.exists():
            os.rmdir(subdir)


def test_bundle_root_identity_replacement_fails_closed(tmp_path: Path) -> None:
    root, _, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        live_bytes=b"original root A",
    )
    moved = tmp_path / "moved-original-root"
    root.replace(moved)
    replacement = root / "evidence"
    replacement.mkdir(parents=True)
    (replacement / "data.bin").write_bytes(b"replacement root B")

    with pytest.raises(EvidenceAcquisitionError, match="bundle_root_unstable"):
        with session:
            acquire_bundle_evidence(
                bundle_root=root,
                snapshot=snapshot,
                evidence=evidence,
            )

    assert evidence.state is EvidenceObjectState.FAILED
    assert evidence.observed_fingerprint is None


def test_read_io_failure_invalidates_partial_memory_capture(tmp_path: Path) -> None:
    value = b"first chunk then read failure"
    root, _, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        live_bytes=value,
    )
    calls = 0

    def failing_reader(descriptor: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            return os.read(descriptor, size)
        raise OSError(errno.EIO, "simulated descriptor read failure")

    hooks = _AcquisitionTestHooks(read_chunk=failing_reader)
    with pytest.raises(EvidenceAcquisitionError, match="read_failed"):
        with session:
            acquire_bundle_evidence(
                bundle_root=root,
                snapshot=snapshot,
                evidence=evidence,
                chunk_size=4,
                _hooks=hooks,
            )

    assert calls == 2
    assert evidence.state is EvidenceObjectState.FAILED
    with pytest.raises(SnapshotStateError):
        evidence.open_reader()


def test_spool_write_failure_has_no_memory_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        live_bytes=b"spool write failure",
        storage_kind=StorageKind.SPOOL,
    )
    original_append = VerifiedEvidenceObject.append_acquired_bytes

    def fail_spool_write(self: VerifiedEvidenceObject, chunk: bytes) -> None:
        if self is evidence:
            raise OSError(errno.ENOSPC, "simulated spool ENOSPC")
        original_append(self, chunk)

    monkeypatch.setattr(
        VerifiedEvidenceObject,
        "append_acquired_bytes",
        fail_spool_write,
    )

    with pytest.raises(EvidenceAcquisitionError, match="retention_failed"):
        with session:
            acquire_bundle_evidence(
                bundle_root=root,
                snapshot=snapshot,
                evidence=evidence,
                chunk_size=3,
            )

    assert evidence.storage_kind is StorageKind.SPOOL
    assert evidence.state is EvidenceObjectState.FAILED
    assert evidence.closed is True


def test_failed_spool_acquisition_remains_session_owned_for_cleanup(
    tmp_path: Path,
) -> None:
    root, _, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        live_bytes=b"actual",
        expected_bytes=b"expected",
        storage_kind=StorageKind.SPOOL,
    )
    assert snapshot.objects[evidence.relative_path] is evidence
    assert evidence.snapshot_owned is True

    with pytest.raises(EvidenceAcquisitionError):
        with session:
            acquire_bundle_evidence(
                bundle_root=root,
                snapshot=snapshot,
                evidence=evidence,
            )

    assert session.state is SessionState.CLOSED
    assert evidence.state is EvidenceObjectState.FAILED
    assert evidence.closed is True


def test_file_identity_is_structured_and_rejects_bool_aliases() -> None:
    first = FileIdentity("test", 1, 2, "regular")
    same = FileIdentity("test", 1, 2, "regular")
    different_device = FileIdentity("test", 3, 2, "regular")
    different_file = FileIdentity("test", 1, 4, "regular")

    assert first == same
    assert first != different_device
    assert first != different_file
    assert first.available is True
    with pytest.raises(ValueError, match="device"):
        FileIdentity("test", False, 2, "regular")
    with pytest.raises(ValueError, match="id"):
        FileIdentity("test", 1, False, "regular")
    unavailable = FileIdentity.unavailable(
        file_type="regular",
        reason="platform did not provide stable identity",
    )
    assert unavailable.available is False
    assert unavailable.device is None
    assert unavailable.file_id is None


def test_unavailable_root_identity_fails_closed_before_read(tmp_path: Path) -> None:
    unavailable_root = BundleRootIdentity(
        FileIdentity.unavailable(
            file_type="directory",
            reason="simulated unavailable root identity",
        )
    )
    root, _, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        live_bytes=b"never read",
        root_identity_override=unavailable_root,
    )
    read_called = False

    def reader(descriptor: int, size: int) -> bytes:
        nonlocal read_called
        read_called = True
        return os.read(descriptor, size)

    with pytest.raises(EvidenceAcquisitionError, match="identity_unavailable"):
        with session:
            acquire_bundle_evidence(
                bundle_root=root,
                snapshot=snapshot,
                evidence=evidence,
                _hooks=_AcquisitionTestHooks(read_chunk=reader),
            )

    assert read_called is False
    assert evidence.state is EvidenceObjectState.FAILED


def test_successful_acquisition_is_independent_of_later_live_path_mutation(
    tmp_path: Path,
) -> None:
    captured = b"captured A"
    root, candidate, snapshot, evidence, session = make_owned_evidence(
        tmp_path,
        live_bytes=captured,
        storage_kind=StorageKind.SPOOL,
    )

    with session:
        acquire_bundle_evidence(
            bundle_root=root,
            snapshot=snapshot,
            evidence=evidence,
            chunk_size=2,
        )
        candidate.write_bytes(b"later live mutation B")
        evidence.seal()
        with evidence.open_reader() as reader:
            assert reader.read() == captured
