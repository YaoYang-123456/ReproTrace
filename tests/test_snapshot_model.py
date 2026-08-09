from __future__ import annotations

import io
from pathlib import Path

import pytest

from reprotrace.evidence import canonical_evidence_index_bytes
from reprotrace.io import sha256_bytes
from reprotrace.snapshot import (
    EvidenceObjectState,
    SemanticEvidenceUnavailable,
    SessionState,
    SnapshotState,
    SnapshotStateError,
    StorageKind,
    VerificationSession,
    VerifiedBundleSnapshot,
    VerifiedEvidenceObject,
)


def make_index(
    evidence: dict[str, tuple[bytes, list[str]]],
) -> tuple[dict[str, object], bytes]:
    index: dict[str, object] = {
        "schema_version": 1,
        "entries": [
            {
                "path": path,
                "roles": roles,
                "size_bytes": len(value),
                "sha256": sha256_bytes(value),
            }
            for path, (value, roles) in evidence.items()
        ],
    }
    encoded = canonical_evidence_index_bytes(index)
    return index, encoded


def make_snapshot(
    evidence: dict[str, tuple[bytes, list[str]]],
) -> VerifiedBundleSnapshot:
    index, encoded = make_index(evidence)
    return VerifiedBundleSnapshot(
        bundle_root="C:/display-only/bundle",
        index_bytes=encoded,
        parsed_index=index,
    )


def make_object(
    path: str,
    value: bytes,
    roles: list[str],
    storage_kind: StorageKind,
    *,
    spool_max_size: int = 1024,
) -> VerifiedEvidenceObject:
    return VerifiedEvidenceObject(
        relative_path=path,
        roles=roles,
        expected_size=len(value),
        expected_sha256=sha256_bytes(value),
        storage_kind=storage_kind,
        spool_max_size=spool_max_size,
    )


def acquire_and_seal(evidence: VerifiedEvidenceObject, value: bytes) -> None:
    evidence.acquire_bytes(value)
    evidence.seal()


def test_memory_object_seal_retains_exact_immutable_bytes() -> None:
    value = b"exact structured evidence\x00\xff"
    evidence = make_object("run.json", value, ["core_record"], StorageKind.MEMORY)

    acquire_and_seal(evidence, value)

    assert evidence.state is EvidenceObjectState.SEALED
    with evidence.open_reader() as reader:
        assert reader.read() == value
        with pytest.raises(io.UnsupportedOperation, match="read-only"):
            reader.write(b"replacement")
    with pytest.raises(AttributeError):
        evidence.expected_fingerprint = evidence.expected_fingerprint  # type: ignore[misc]


def test_memory_reader_is_fresh_and_begins_at_zero() -> None:
    value = b"abcdef"
    evidence = make_object("run.json", value, ["core_record"], StorageKind.MEMORY)
    acquire_and_seal(evidence, value)

    first = evidence.open_reader()
    assert first.read(3) == b"abc"
    second = evidence.open_reader()
    assert second.tell() == 0
    assert second.read() == value
    assert first.read() == b"def"
    first.close()
    second.close()


def test_integrity_only_object_rejects_semantic_reader() -> None:
    value = b"artifact bytes"
    evidence = make_object(
        "artifacts/model.bin",
        value,
        ["artifact"],
        StorageKind.INTEGRITY_ONLY,
    )
    acquire_and_seal(evidence, value)

    with pytest.raises(SemanticEvidenceUnavailable, match="integrity-only"):
        evidence.open_reader()


def test_spool_retention_has_independent_fresh_readers() -> None:
    value = b"raw metric evidence"
    evidence = make_object(
        "raw/metrics.csv",
        value,
        ["metric_source"],
        StorageKind.SPOOL,
    )
    evidence.begin_acquisition()
    evidence.append_acquired_bytes(value[:5])
    evidence.append_acquired_bytes(value[5:])
    evidence.finish_acquisition()
    evidence.seal()

    first = evidence.open_reader()
    second = evidence.open_reader()
    assert first.read(4) == value[:4]
    assert second.read() == value
    assert first.read() == value[4:]
    first.seek(0)
    assert first.read() == value
    first.close()
    second.close()
    evidence.close()


def test_spool_rollover_preserves_reader_semantics() -> None:
    value = b"0123456789"
    evidence = make_object(
        "raw/large.log",
        value,
        ["metric_source"],
        StorageKind.SPOOL,
        spool_max_size=3,
    )

    acquire_and_seal(evidence, value)

    assert evidence.spool_rolled_to_disk is True
    with evidence.open_reader() as reader:
        assert reader.read() == value
    evidence.close()


def test_sealed_object_rejects_payload_and_state_mutation() -> None:
    value = b"sealed"
    evidence = make_object("run.json", value, ["core_record"], StorageKind.MEMORY)
    acquire_and_seal(evidence, value)

    with pytest.raises(SnapshotStateError, match="cannot begin"):
        evidence.begin_acquisition()
    with pytest.raises(SnapshotStateError, match="only be appended"):
        evidence.append_acquired_bytes(b"different")
    with pytest.raises(SnapshotStateError, match="sealed evidence cannot be marked failed"):
        evidence.mark_failed("replacement attempt")
    with pytest.raises(SnapshotStateError, match="only be sealed"):
        evidence.seal()


def test_snapshot_rejects_duplicate_canonical_evidence_path() -> None:
    value = b"record"
    snapshot = make_snapshot({"records/run.json": (value, ["core_record"])})
    first = make_object(
        "records/run.json", value, ["core_record"], StorageKind.MEMORY
    )
    second = make_object(
        "records/./run.json", value, ["core_record"], StorageKind.MEMORY
    )
    snapshot.add_evidence(first)

    with pytest.raises(SnapshotStateError, match="duplicate acquired evidence path"):
        snapshot.add_evidence(second)


def test_evidence_object_has_one_snapshot_owner() -> None:
    value = b"record"
    first_snapshot = make_snapshot({"run.json": (value, ["core_record"])})
    second_snapshot = make_snapshot({"run.json": (value, ["core_record"])})
    evidence = make_object("run.json", value, ["core_record"], StorageKind.MEMORY)
    first_snapshot.add_evidence(evidence)

    with pytest.raises(SnapshotStateError, match="already belongs to a snapshot"):
        second_snapshot.add_evidence(evidence)


def test_incomplete_snapshot_keeps_candidate_root_unpublishable() -> None:
    first_value = b"first"
    second_value = b"second"
    snapshot = make_snapshot(
        {
            "first.json": (first_value, ["core_record"]),
            "second.json": (second_value, ["core_record"]),
        }
    )
    first = make_object(
        "first.json", first_value, ["core_record"], StorageKind.MEMORY
    )
    acquire_and_seal(first, first_value)
    snapshot.add_evidence(first)

    assert snapshot.candidate_evidence_root
    assert snapshot.established_evidence_root is None
    with pytest.raises(SnapshotStateError, match="missing evidence: second.json"):
        snapshot.complete_acquisition()
    with pytest.raises(SnapshotStateError, match="not established"):
        snapshot.require_established_evidence_root()


def test_complete_sealed_snapshot_establishes_candidate_root() -> None:
    value = b"record"
    snapshot = make_snapshot({"run.json": (value, ["core_record"])})
    evidence = make_object("run.json", value, ["core_record"], StorageKind.MEMORY)
    acquire_and_seal(evidence, value)
    session = VerificationSession(snapshot)

    with session:
        snapshot.add_evidence(evidence)
        snapshot.complete_acquisition()
        assert snapshot.state is SnapshotState.ACQUIRED
        assert snapshot.established_evidence_root is None
        snapshot.seal()
        assert snapshot.state is SnapshotState.SEALED
        assert snapshot.require_established_evidence_root() == sha256_bytes(
            snapshot.index_bytes
        )

    assert session.state is SessionState.CLOSED


def test_failed_object_blocks_snapshot_completion_and_root() -> None:
    expected = b"expected"
    evidence = make_object(
        "run.json", expected, ["core_record"], StorageKind.MEMORY
    )
    with pytest.raises(SnapshotStateError, match="fingerprint mismatch"):
        evidence.acquire_bytes(b"different")
    assert evidence.state is EvidenceObjectState.FAILED

    snapshot = make_snapshot({"run.json": (expected, ["core_record"])})
    snapshot.add_evidence(evidence)
    with pytest.raises(SnapshotStateError, match="failed or unvalidated"):
        snapshot.complete_acquisition()
    assert snapshot.established_evidence_root is None


def test_session_cleanup_closes_spool_and_is_idempotent() -> None:
    value = b"spooled evidence"
    snapshot = make_snapshot({"raw/data.bin": (value, ["metric_source"])})
    evidence = make_object(
        "raw/data.bin", value, ["metric_source"], StorageKind.SPOOL
    )
    acquire_and_seal(evidence, value)
    snapshot.add_evidence(evidence)
    snapshot.complete_acquisition()
    snapshot.seal()
    session = VerificationSession(snapshot)

    session.close()
    session.close()

    assert session.state is SessionState.CLOSED
    assert snapshot.session_closed is True
    assert evidence.closed is True
    assert session.cleanup_diagnostics == ()


def test_spool_reader_fails_clearly_after_session_close() -> None:
    value = b"spooled evidence"
    snapshot = make_snapshot({"raw/data.bin": (value, ["metric_source"])})
    evidence = make_object(
        "raw/data.bin", value, ["metric_source"], StorageKind.SPOOL
    )
    acquire_and_seal(evidence, value)
    snapshot.add_evidence(evidence)
    snapshot.complete_acquisition()
    snapshot.seal()
    session = VerificationSession(snapshot)
    reader = evidence.open_reader()
    assert reader.read(2) == value[:2]

    session.close()

    with pytest.raises(SnapshotStateError, match="closed"):
        reader.read()
    with pytest.raises(SnapshotStateError, match="closed"):
        evidence.open_reader()


def test_cleanup_failure_is_diagnostic_and_does_not_change_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = b"spooled evidence"
    snapshot = make_snapshot({"raw/data.bin": (value, ["metric_source"])})
    evidence = make_object(
        "raw/data.bin", value, ["metric_source"], StorageKind.SPOOL
    )
    acquire_and_seal(evidence, value)
    snapshot.add_evidence(evidence)
    snapshot.complete_acquisition()
    snapshot.seal()
    established = snapshot.require_established_evidence_root()
    session = VerificationSession(snapshot)
    original_close = VerifiedEvidenceObject.close

    def close_then_report_failure(self: VerifiedEvidenceObject) -> None:
        original_close(self)
        if self is evidence:
            raise OSError("simulated cleanup diagnostic")

    monkeypatch.setattr(VerifiedEvidenceObject, "close", close_then_report_failure)

    session.close()

    assert session.state is SessionState.CLOSED
    assert snapshot.require_established_evidence_root() == established
    assert len(session.cleanup_diagnostics) == 1
    diagnostic = session.cleanup_diagnostics[0]
    assert diagnostic.resource == "raw/data.bin"
    assert diagnostic.exception_type == "OSError"
    assert diagnostic.message == "simulated cleanup diagnostic"


def test_parsed_record_cache_is_bound_to_sealed_semantic_evidence() -> None:
    value = b'{"schema_version":1}'
    snapshot = make_snapshot({"run.json": (value, ["core_record"])})
    evidence = make_object("run.json", value, ["core_record"], StorageKind.MEMORY)
    acquire_and_seal(evidence, value)
    snapshot.add_evidence(evidence)
    parsed = {"schema_version": 1, "nested": {"value": "captured"}}

    snapshot.cache_parsed_record("run.json", parsed)
    parsed["nested"]["value"] = "caller mutation"
    cached = snapshot.parsed_record("run.json")
    cached["nested"]["value"] = "reader mutation"

    assert snapshot.parsed_record("run.json")["nested"]["value"] == "captured"


def test_snapshot_model_does_not_consult_display_bundle_path(tmp_path: Path) -> None:
    value = b"record"
    index, encoded = make_index({"run.json": (value, ["core_record"])})
    absent_path = tmp_path / "does-not-exist"

    snapshot = VerifiedBundleSnapshot(
        bundle_root=str(absent_path),
        index_bytes=encoded,
        parsed_index=index,
    )

    assert snapshot.root_metadata.display_path == str(absent_path)
    assert not absent_path.exists()
