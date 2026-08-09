from __future__ import annotations

import json
import os
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

import reprotrace.acquisition as acquisition_module
import reprotrace.evidence as evidence_module
import reprotrace.io as io_module
import reprotrace.snapshot_builder as snapshot_builder_module
from reprotrace.evidence import (
    EVIDENCE_INDEX_FILENAME,
    canonical_evidence_index_bytes,
    evidence_root_sha256,
    read_evidence_index,
    write_evidence_index,
)
from reprotrace.io import sha256_bytes
from reprotrace.snapshot import (
    EvidenceObjectState,
    SemanticEvidenceUnavailable,
    SessionState,
    SnapshotState,
    StorageKind,
    VerifiedEvidenceObject,
)
from reprotrace.snapshot_builder import (
    CORE_SEMANTIC_PATHS,
    RUN_RECORD_PATH,
    SchemaOneSnapshotNotApplicable,
    SnapshotBuildError,
    _SnapshotBuildTestHooks,
    open_schema_one_snapshot,
)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fixture_files() -> dict[str, tuple[bytes, list[str]]]:
    return {
        "run.json": (
            _json_bytes({"schema_version": 1, "status": "executed"}),
            ["record"],
        ),
        "source.json": (_json_bytes({"available": True}), ["record", "source"]),
        "environment.json": (
            _json_bytes({"python": "fixture"}),
            ["environment", "record"],
        ),
        "inputs.json": (_json_bytes([]), ["input_record", "record"]),
        "commands.json": (
            _json_bytes([{"id": "train", "status": "completed"}]),
            ["command_record", "record"],
        ),
        "artifacts.json": (_json_bytes([]), ["artifact_record", "record"]),
        "metrics.json": (
            _json_bytes([{"id": "accuracy", "value": 0.75}]),
            ["metric_record", "record"],
        ),
        "manifest.resolved.yaml": (
            b"schema_version: 1\nexperiment:\n  id: snapshot-fixture\nmetrics: []\n",
            ["record", "resolved_manifest"],
        ),
        "metric_sources.json": (
            _json_bytes({"schema_version": 1, "metrics": []}),
            ["metric_source_record", "record"],
        ),
        "commands.jsonl": (b'{"id":"train"}\n', ["command_archive", "record"]),
        "raw/metrics.csv": (
            b"epoch,val_acc\n0,0.75\n",
            ["artifact", "metric_source"],
        ),
        "artifacts/model.bin": (b"\x00\x01model\xff", ["artifact"]),
        "logs/train.stdout.log": (b"training output\n", ["command_log"]),
    }


def _write_bundle(
    parent: Path,
    *,
    name: str = "bundle",
) -> tuple[Path, dict[str, tuple[bytes, list[str]]], list[dict[str, object]]]:
    bundle = parent / name
    bundle.mkdir(parents=True)
    files = _fixture_files()
    declarations: list[dict[str, object]] = []
    for relative_path, (value, roles) in files.items():
        candidate = bundle.joinpath(*relative_path.split("/"))
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(value)
        declarations.append({"path": relative_path, "roles": roles})
    write_evidence_index(bundle, declarations)
    return bundle, files, declarations


def _rewrite_index(bundle: Path, declarations: list[dict[str, object]]) -> None:
    write_evidence_index(bundle, declarations)


def _instrument_bundle_opens(
    monkeypatch: pytest.MonkeyPatch,
    bundle: Path,
) -> Counter[str]:
    original_open = acquisition_module.os.open
    root = bundle.resolve()
    counts: Counter[str] = Counter()

    def tracked_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        try:
            relative = Path(path).resolve(strict=False).relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError, TypeError):
            pass
        else:
            counts[relative] += 1
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(acquisition_module.os, "open", tracked_open)
    return counts


def test_normal_schema_one_snapshot_build(tmp_path: Path) -> None:
    bundle, _, _ = _write_bundle(tmp_path)
    expected_paths = {entry["path"] for entry in read_evidence_index(bundle)["entries"]}

    session = open_schema_one_snapshot(bundle)
    try:
        snapshot = session.snapshot
        assert session.state is SessionState.OPEN
        assert snapshot.state is SnapshotState.SEALED
        assert snapshot.acquisition_complete is True
        assert snapshot.established_evidence_root is not None
        assert set(snapshot.objects) == expected_paths
        for path in CORE_SEMANTIC_PATHS:
            assert snapshot.parsed_record(path) is not None
    finally:
        session.close()


def test_established_root_matches_captured_canonical_index(tmp_path: Path) -> None:
    bundle, _, _ = _write_bundle(tmp_path)
    index = read_evidence_index(bundle)
    exact_index_bytes = (bundle / EVIDENCE_INDEX_FILENAME).read_bytes()

    session = open_schema_one_snapshot(bundle)
    try:
        snapshot = session.snapshot
        expected = sha256_bytes(exact_index_bytes)
        assert snapshot.index_bytes == exact_index_bytes
        assert snapshot.require_established_evidence_root() == expected
        assert expected == evidence_root_sha256(index)
    finally:
        session.close()


def test_run_json_live_path_is_opened_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _, _ = _write_bundle(tmp_path)
    counts = _instrument_bundle_opens(monkeypatch, bundle)

    with open_schema_one_snapshot(bundle):
        pass

    assert counts[RUN_RECORD_PATH] == 1


def test_evidence_index_live_path_is_opened_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _, _ = _write_bundle(tmp_path)
    counts = _instrument_bundle_opens(monkeypatch, bundle)

    with open_schema_one_snapshot(bundle):
        pass

    assert counts[EVIDENCE_INDEX_FILENAME] == 1


def test_every_indexed_path_is_opened_once_in_canonical_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _, _ = _write_bundle(tmp_path)
    index = read_evidence_index(bundle)
    counts = _instrument_bundle_opens(monkeypatch, bundle)
    acquired_order: list[str] = []

    hooks = _SnapshotBuildTestHooks(
        before_indexed_entry_acquire=lambda path, evidence: acquired_order.append(path)
    )
    with open_schema_one_snapshot(bundle, _hooks=hooks):
        pass

    indexed_paths = [entry["path"] for entry in index["entries"]]
    assert acquired_order == [path for path in indexed_paths if path != RUN_RECORD_PATH]
    assert all(counts[path] == 1 for path in indexed_paths)
    assert counts[EVIDENCE_INDEX_FILENAME] == 1
    assert sum(counts.values()) == len(indexed_paths) + 1


def test_captured_run_is_bound_without_live_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, files, _ = _write_bundle(tmp_path)
    captured_run = json.loads(files[RUN_RECORD_PATH][0].decode("utf-8"))
    replacement = _json_bytes(
        {"schema_version": 1, "status": "replacement-never-cached"}
    )
    counts = _instrument_bundle_opens(monkeypatch, bundle)
    hooks = _SnapshotBuildTestHooks(
        after_run_bootstrap_capture=lambda captured: (bundle / RUN_RECORD_PATH).write_bytes(
            replacement
        )
    )

    session = open_schema_one_snapshot(bundle, _hooks=hooks)
    try:
        assert session.snapshot.parsed_record(RUN_RECORD_PATH) == captured_run
        assert counts[RUN_RECORD_PATH] == 1
    finally:
        session.close()


def test_captured_run_mismatch_with_index_fails_without_reread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _, _ = _write_bundle(tmp_path)
    (bundle / RUN_RECORD_PATH).write_bytes(
        _json_bytes({"schema_version": 1, "status": "changed-before-build"})
    )
    counts = _instrument_bundle_opens(monkeypatch, bundle)

    with pytest.raises(SnapshotBuildError) as raised:
        open_schema_one_snapshot(bundle)

    assert raised.value.category == "run_fingerprint_mismatch"
    assert counts[RUN_RECORD_PATH] == 1


def test_captured_index_remains_authoritative_without_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _, _ = _write_bundle(tmp_path)
    captured_index = (bundle / EVIDENCE_INDEX_FILENAME).read_bytes()
    counts = _instrument_bundle_opens(monkeypatch, bundle)
    hooks = _SnapshotBuildTestHooks(
        after_index_bootstrap_capture=lambda captured: (
            bundle / EVIDENCE_INDEX_FILENAME
        ).write_bytes(b'{"replacement":true}')
    )

    session = open_schema_one_snapshot(bundle, _hooks=hooks)
    try:
        assert session.snapshot.index_bytes == captured_index
        assert session.snapshot.require_established_evidence_root() == sha256_bytes(
            captured_index
        )
        assert counts[EVIDENCE_INDEX_FILENAME] == 1
    finally:
        session.close()


def test_noncanonical_index_bytes_fail_before_root_establishment(tmp_path: Path) -> None:
    bundle, _, _ = _write_bundle(tmp_path)
    index = read_evidence_index(bundle)
    (bundle / EVIDENCE_INDEX_FILENAME).write_text(
        json.dumps(index, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(SnapshotBuildError) as raised:
        open_schema_one_snapshot(bundle)

    assert raised.value.category == "index_noncanonical"


def test_nonfinite_index_json_fails_strict_parse(tmp_path: Path) -> None:
    bundle, _, _ = _write_bundle(tmp_path)
    (bundle / EVIDENCE_INDEX_FILENAME).write_bytes(
        b'{"entries":[],"schema_version":NaN}'
    )

    with pytest.raises(SnapshotBuildError) as raised:
        open_schema_one_snapshot(bundle)

    assert raised.value.category == "index_parse_failed"


def test_missing_run_index_entry_fails_without_run_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _, declarations = _write_bundle(tmp_path)
    _rewrite_index(
        bundle,
        [entry for entry in declarations if entry["path"] != RUN_RECORD_PATH],
    )
    counts = _instrument_bundle_opens(monkeypatch, bundle)

    with pytest.raises(SnapshotBuildError) as raised:
        open_schema_one_snapshot(bundle)

    assert raised.value.category == "run_not_indexed"
    assert counts[RUN_RECORD_PATH] == 1


def test_changed_later_indexed_evidence_aborts_and_cleans_session(tmp_path: Path) -> None:
    bundle, _, _ = _write_bundle(tmp_path)
    observed: list[VerifiedEvidenceObject] = []

    def mutate(path: str, evidence: VerifiedEvidenceObject) -> None:
        if path == "raw/metrics.csv":
            observed.append(evidence)
            (bundle / "raw" / "metrics.csv").write_bytes(b"changed")

    with pytest.raises(SnapshotBuildError) as raised:
        open_schema_one_snapshot(
            bundle,
            _hooks=_SnapshotBuildTestHooks(before_indexed_entry_acquire=mutate),
        )

    assert raised.value.category == "indexed_acquisition_failed"
    assert len(observed) == 1
    assert observed[0].state is EvidenceObjectState.FAILED
    assert observed[0].closed is True


def test_root_replacement_mid_build_fails_without_restart(tmp_path: Path) -> None:
    bundle, _, _ = _write_bundle(tmp_path)
    original = tmp_path / "captured-root"

    def replace_root(captured: Any) -> None:
        bundle.rename(original)
        bundle.mkdir()

    with pytest.raises(SnapshotBuildError) as raised:
        open_schema_one_snapshot(
            bundle,
            _hooks=_SnapshotBuildTestHooks(
                after_index_bootstrap_capture=replace_root
            ),
        )

    assert raised.value.category == "indexed_acquisition_failed"
    assert "bundle_root_unstable" in str(raised.value)


def test_builder_captures_bundle_root_identity_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _, _ = _write_bundle(tmp_path)
    original_capture = snapshot_builder_module.capture_bundle_root_identity
    calls = 0

    def count_capture(root: Any) -> Any:
        nonlocal calls
        calls += 1
        return original_capture(root)

    monkeypatch.setattr(
        snapshot_builder_module,
        "capture_bundle_root_identity",
        count_capture,
    )

    with open_schema_one_snapshot(bundle):
        pass

    assert calls == 1


def test_storage_classifier_applies_role_precedence(tmp_path: Path) -> None:
    bundle, _, _ = _write_bundle(tmp_path)

    session = open_schema_one_snapshot(bundle)
    try:
        objects = session.snapshot.objects
        assert all(
            objects[path].storage_kind is StorageKind.MEMORY
            for path in CORE_SEMANTIC_PATHS
        )
        assert objects["raw/metrics.csv"].storage_kind is StorageKind.SPOOL
        assert objects["artifacts/model.bin"].storage_kind is StorageKind.INTEGRITY_ONLY
        assert objects["commands.jsonl"].storage_kind is StorageKind.INTEGRITY_ONLY
        assert objects["logs/train.stdout.log"].storage_kind is StorageKind.INTEGRITY_ONLY
    finally:
        session.close()


def test_metric_source_retention_survives_live_deletion(tmp_path: Path) -> None:
    bundle, files, _ = _write_bundle(tmp_path)
    expected = files["raw/metrics.csv"][0]

    session = open_schema_one_snapshot(bundle)
    try:
        (bundle / "raw" / "metrics.csv").unlink()
        with session.snapshot.objects["raw/metrics.csv"].open_reader() as reader:
            assert reader.read() == expected
    finally:
        session.close()


def test_core_parsed_cache_survives_live_mutation(tmp_path: Path) -> None:
    bundle, files, _ = _write_bundle(tmp_path)
    expected = json.loads(files["commands.json"][0].decode("utf-8"))

    session = open_schema_one_snapshot(bundle)
    try:
        (bundle / "commands.json").write_bytes(_json_bytes([]))
        assert session.snapshot.parsed_record("commands.json") == expected
    finally:
        session.close()


def test_integrity_only_entry_has_no_semantic_reader(tmp_path: Path) -> None:
    bundle, _, _ = _write_bundle(tmp_path)

    session = open_schema_one_snapshot(bundle)
    try:
        evidence = session.snapshot.objects["artifacts/model.bin"]
        assert evidence.state is EvidenceObjectState.SEALED
        with pytest.raises(SemanticEvidenceUnavailable, match="integrity-only"):
            evidence.open_reader()
    finally:
        session.close()


def test_malformed_captured_json_core_prevents_complete_snapshot(tmp_path: Path) -> None:
    bundle, _, declarations = _write_bundle(tmp_path)
    (bundle / "commands.json").write_bytes(b"[not valid JSON")
    _rewrite_index(bundle, declarations)
    acquired: list[VerifiedEvidenceObject] = []

    with pytest.raises(SnapshotBuildError) as raised:
        open_schema_one_snapshot(
            bundle,
            _hooks=_SnapshotBuildTestHooks(
                before_indexed_entry_acquire=lambda path, evidence: acquired.append(
                    evidence
                )
            ),
        )

    assert raised.value.category == "core_parse_failed"
    assert all(evidence.closed for evidence in acquired)


def test_malformed_captured_yaml_manifest_prevents_complete_snapshot(tmp_path: Path) -> None:
    bundle, _, declarations = _write_bundle(tmp_path)
    (bundle / "manifest.resolved.yaml").write_bytes(b"metrics: [\n")
    _rewrite_index(bundle, declarations)

    with pytest.raises(SnapshotBuildError) as raised:
        open_schema_one_snapshot(bundle)

    assert raised.value.category == "core_parse_failed"


@pytest.mark.parametrize("token", [b"NaN", b"Infinity", b"-Infinity"])
def test_nonfinite_captured_json_core_fails_closed(
    tmp_path: Path,
    token: bytes,
) -> None:
    bundle, _, declarations = _write_bundle(tmp_path)
    (bundle / "metrics.json").write_bytes(b"[" + token + b"]")
    _rewrite_index(bundle, declarations)

    with pytest.raises(SnapshotBuildError) as raised:
        open_schema_one_snapshot(bundle)

    assert raised.value.category == "core_parse_failed"


def test_snapshot_root_is_relocation_and_origin_independent(tmp_path: Path) -> None:
    origin, _, _ = _write_bundle(tmp_path, name="origin")
    relocated = tmp_path / "relocated"
    shutil.copytree(origin, relocated)

    with open_schema_one_snapshot(origin) as first:
        first_root = first.snapshot.require_established_evidence_root()
    shutil.rmtree(origin)
    with open_schema_one_snapshot(relocated) as second:
        assert second.snapshot.require_established_evidence_root() == first_root


def test_schema_zero_does_not_open_evidence_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "legacy"
    bundle.mkdir()
    (bundle / RUN_RECORD_PATH).write_bytes(
        _json_bytes({"schema_version": 0, "status": "executed"})
    )
    counts = _instrument_bundle_opens(monkeypatch, bundle)

    with pytest.raises(SchemaOneSnapshotNotApplicable) as raised:
        open_schema_one_snapshot(bundle)

    assert raised.value.category == "schema_one_snapshot_not_applicable"
    assert counts[RUN_RECORD_PATH] == 1
    assert counts[EVIDENCE_INDEX_FILENAME] == 0


def test_mid_build_spool_failure_cleans_failed_owned_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _, _ = _write_bundle(tmp_path)
    original_append = VerifiedEvidenceObject.append_acquired_bytes
    metric_objects: list[VerifiedEvidenceObject] = []

    def fail_metric_spool(self: VerifiedEvidenceObject, chunk: bytes) -> None:
        if self.relative_path == "raw/metrics.csv":
            raise OSError("injected spool failure")
        original_append(self, chunk)

    def remember(path: str, evidence: VerifiedEvidenceObject) -> None:
        if path == "raw/metrics.csv":
            metric_objects.append(evidence)

    monkeypatch.setattr(
        VerifiedEvidenceObject,
        "append_acquired_bytes",
        fail_metric_spool,
    )
    with pytest.raises(SnapshotBuildError) as raised:
        open_schema_one_snapshot(
            bundle,
            _hooks=_SnapshotBuildTestHooks(before_indexed_entry_acquire=remember),
        )

    assert raised.value.category == "indexed_acquisition_failed"
    assert len(metric_objects) == 1
    assert metric_objects[0].snapshot_owned is True
    assert metric_objects[0].state is EvidenceObjectState.FAILED
    assert metric_objects[0].closed is True


def test_complete_snapshot_remains_usable_after_live_bundle_is_unavailable(
    tmp_path: Path,
) -> None:
    bundle, files, _ = _write_bundle(tmp_path)
    unavailable = tmp_path / "bundle-unavailable"

    session = open_schema_one_snapshot(bundle)
    try:
        expected_commands = json.loads(files["commands.json"][0].decode("utf-8"))
        expected_metric_source = files["raw/metrics.csv"][0]
        bundle.rename(unavailable)

        assert session.snapshot.parsed_record("commands.json") == expected_commands
        with session.snapshot.objects["commands.json"].open_reader() as core_reader:
            assert core_reader.read() == files["commands.json"][0]
        with session.snapshot.objects["raw/metrics.csv"].open_reader() as raw_reader:
            assert raw_reader.read() == expected_metric_source
    finally:
        session.close()


def test_snapshot_builder_never_uses_path_based_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _, _ = _write_bundle(tmp_path)

    def forbid_path_hash(path: Any) -> str:
        raise AssertionError(f"path-based hashing was called for {path}")

    monkeypatch.setattr(io_module, "sha256_file", forbid_path_hash)
    monkeypatch.setattr(evidence_module, "sha256_file", forbid_path_hash)

    with open_schema_one_snapshot(bundle) as session:
        assert session.snapshot.established_evidence_root is not None
