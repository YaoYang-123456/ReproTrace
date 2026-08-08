"""Schema-1 bootstrap and index-wide verified snapshot construction."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from .acquisition import (
    DEFAULT_ACQUISITION_CHUNK_SIZE,
    CapturedBundleFile,
    EvidenceAcquisitionError,
    acquire_bundle_evidence,
    capture_bundle_file_once,
    capture_bundle_root_identity,
)
from .errors import ConfigError
from .evidence import EVIDENCE_INDEX_FILENAME
from .snapshot import (
    EvidenceObjectState,
    SnapshotStateError,
    StorageKind,
    VerificationSession,
    VerifiedBundleSnapshot,
    VerifiedEvidenceObject,
)


RUN_RECORD_PATH = "run.json"
RESOLVED_MANIFEST_PATH = "manifest.resolved.yaml"
CORE_JSON_OBJECT_PATHS = frozenset(
    {
        RUN_RECORD_PATH,
        "source.json",
        "environment.json",
        "metric_sources.json",
    }
)
CORE_JSON_ARRAY_PATHS = frozenset(
    {
        "inputs.json",
        "commands.json",
        "artifacts.json",
        "metrics.json",
    }
)
CORE_SEMANTIC_PATHS = frozenset(
    {*CORE_JSON_OBJECT_PATHS, *CORE_JSON_ARRAY_PATHS, RESOLVED_MANIFEST_PATH}
)


class SnapshotBuildError(SnapshotStateError):
    """A fail-closed schema-1 snapshot-construction error."""

    def __init__(self, category: str, detail: str) -> None:
        self.category = category
        self.detail = detail
        super().__init__(f"{category}: {detail}")


class SchemaOneSnapshotNotApplicable(SnapshotBuildError):
    def __init__(self, schema_version: int) -> None:
        super().__init__(
            "schema_one_snapshot_not_applicable",
            f"run.json schema_version is {schema_version}, not 1",
        )


@dataclass(slots=True)
class _SnapshotBuildTestHooks:
    after_run_bootstrap_capture: Callable[[CapturedBundleFile], None] | None = None
    after_index_bootstrap_capture: Callable[[CapturedBundleFile], None] | None = None
    before_indexed_entry_acquire: (
        Callable[[str, VerifiedEvidenceObject], None] | None
    ) = None


def classify_snapshot_storage(path: str, roles: list[str]) -> StorageKind:
    """Return the explicit Stage 6.2c retention class for one index entry."""

    if path in CORE_SEMANTIC_PATHS:
        return StorageKind.MEMORY
    if "metric_source" in roles:
        return StorageKind.SPOOL
    return StorageKind.INTEGRITY_ONLY


def _reject_non_finite_json(token: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {token}")


def _parse_json_bytes(
    exact_bytes: bytes,
    *,
    label: str,
    expected_container: type[dict] | type[list],
    error_category: str = "core_parse_failed",
) -> dict[str, Any] | list[Any]:
    try:
        parsed = json.loads(
            exact_bytes.decode("utf-8"),
            parse_constant=_reject_non_finite_json,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise SnapshotBuildError(
            error_category,
            f"cannot parse strict UTF-8 JSON {label}: {exc}",
        ) from exc
    if not isinstance(parsed, expected_container):
        expected = "object" if expected_container is dict else "array"
        raise SnapshotBuildError(
            error_category,
            f"{label} root must be a JSON {expected}",
        )
    return parsed


def _parse_manifest_bytes(exact_bytes: bytes) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(exact_bytes.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SnapshotBuildError(
            "core_parse_failed",
            f"cannot parse UTF-8 YAML {RESOLVED_MANIFEST_PATH}: {exc}",
        ) from exc
    if not isinstance(parsed, dict):
        raise SnapshotBuildError(
            "core_parse_failed",
            f"{RESOLVED_MANIFEST_PATH} root must be a YAML mapping",
        )
    return parsed


def _parse_core_bytes(path: str, exact_bytes: bytes) -> Any:
    if path in CORE_JSON_OBJECT_PATHS:
        return _parse_json_bytes(
            exact_bytes,
            label=path,
            expected_container=dict,
        )
    if path in CORE_JSON_ARRAY_PATHS:
        return _parse_json_bytes(
            exact_bytes,
            label=path,
            expected_container=list,
        )
    if path == RESOLVED_MANIFEST_PATH:
        return _parse_manifest_bytes(exact_bytes)
    raise SnapshotBuildError("core_parse_failed", f"unsupported core record: {path}")


def _read_retained_bytes(evidence: VerifiedEvidenceObject) -> bytes:
    with evidence.open_reader() as reader:
        value = reader.read()
    if not isinstance(value, bytes):
        raise SnapshotBuildError(
            "core_parse_failed",
            f"retained semantic reader returned non-bytes for {evidence.relative_path}",
        )
    return value


def _entry_object(
    entry: Mapping[str, Any],
    *,
    storage_kind: StorageKind,
    spool_max_size: int,
) -> VerifiedEvidenceObject:
    return VerifiedEvidenceObject(
        relative_path=entry["path"],
        roles=entry["roles"],
        expected_size=entry["size_bytes"],
        expected_sha256=entry["sha256"],
        storage_kind=storage_kind,
        spool_max_size=spool_max_size,
    )


def _bind_bootstrap_run(
    *,
    snapshot: VerifiedBundleSnapshot,
    entry: Mapping[str, Any],
    captured: CapturedBundleFile,
    parsed_run: dict[str, Any],
    spool_max_size: int,
) -> None:
    evidence = _entry_object(
        entry,
        storage_kind=StorageKind.MEMORY,
        spool_max_size=spool_max_size,
    )
    snapshot.add_evidence(evidence)
    try:
        evidence.begin_acquisition(identity=captured.file_identity)
        evidence.append_acquired_bytes(captured.exact_bytes)
        evidence.finish_acquisition()
        evidence.seal()
    except (OSError, SnapshotStateError) as exc:
        if evidence.state is not EvidenceObjectState.SEALED:
            try:
                evidence.mark_failed(str(exc))
            except SnapshotStateError:
                pass
        raise SnapshotBuildError(
            "run_fingerprint_mismatch",
            "captured run.json bytes do not match the captured index entry",
        ) from exc
    snapshot.cache_parsed_record(RUN_RECORD_PATH, parsed_run)


def _run_hook(callback: Callable[..., None] | None, *args: Any) -> None:
    if callback is not None:
        callback(*args)


def open_schema_one_snapshot(
    run_dir: str | os.PathLike[str],
    *,
    chunk_size: int = DEFAULT_ACQUISITION_CHUNK_SIZE,
    spool_max_size: int = 8 * 1024 * 1024,
    _hooks: _SnapshotBuildTestHooks | None = None,
) -> VerificationSession:
    """Build and return an open, complete, sealed schema-1 snapshot session."""

    if isinstance(spool_max_size, bool) or not isinstance(spool_max_size, int):
        raise ValueError("spool_max_size must be a positive integer")
    if spool_max_size <= 0:
        raise ValueError("spool_max_size must be a positive integer")
    hooks = _hooks or _SnapshotBuildTestHooks()
    try:
        root_identity = capture_bundle_root_identity(run_dir)
    except EvidenceAcquisitionError as exc:
        raise SnapshotBuildError("bootstrap_capture_failed", str(exc)) from exc

    try:
        run_capture = capture_bundle_file_once(
            bundle_root=run_dir,
            expected_root_identity=root_identity,
            relative_path=RUN_RECORD_PATH,
            chunk_size=chunk_size,
        )
    except EvidenceAcquisitionError as exc:
        raise SnapshotBuildError("bootstrap_capture_failed", str(exc)) from exc
    _run_hook(hooks.after_run_bootstrap_capture, run_capture)
    parsed_run = _parse_json_bytes(
        run_capture.exact_bytes,
        label=RUN_RECORD_PATH,
        expected_container=dict,
    )
    if not isinstance(parsed_run, dict):
        raise SnapshotBuildError("core_parse_failed", "run.json root must be an object")
    schema = parsed_run.get("schema_version", 0)
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise SnapshotBuildError(
            "core_parse_failed",
            "run.json schema_version must be an integer",
        )
    if schema != 1:
        raise SchemaOneSnapshotNotApplicable(schema)

    try:
        index_capture = capture_bundle_file_once(
            bundle_root=run_dir,
            expected_root_identity=root_identity,
            relative_path=EVIDENCE_INDEX_FILENAME,
            chunk_size=chunk_size,
        )
    except EvidenceAcquisitionError as exc:
        raise SnapshotBuildError("bootstrap_capture_failed", str(exc)) from exc
    _run_hook(hooks.after_index_bootstrap_capture, index_capture)
    try:
        parsed_index = _parse_json_bytes(
            index_capture.exact_bytes,
            label=EVIDENCE_INDEX_FILENAME,
            expected_container=dict,
            error_category="index_parse_failed",
        )
        if not isinstance(parsed_index, dict):
            raise SnapshotBuildError(
                "index_parse_failed",
                "evidence.index.json root must be an object",
            )
        snapshot = VerifiedBundleSnapshot(
            bundle_root=os.fspath(run_dir),
            root_identity=root_identity,
            index_bytes=index_capture.exact_bytes,
            parsed_index=parsed_index,
        )
    except SnapshotBuildError:
        raise
    except ConfigError as exc:
        category = (
            "index_noncanonical"
            if "canonical" in str(exc) or "serialization" in str(exc)
            else "index_parse_failed"
        )
        raise SnapshotBuildError(category, str(exc)) from exc

    entries = snapshot.parsed_index["entries"]
    entries_by_path = {entry["path"]: entry for entry in entries}
    run_entry = entries_by_path.get(RUN_RECORD_PATH)
    if run_entry is None:
        raise SnapshotBuildError(
            "run_not_indexed",
            "captured evidence index does not contain run.json",
        )
    missing_core = sorted(CORE_SEMANTIC_PATHS - set(entries_by_path))
    if missing_core:
        raise SnapshotBuildError(
            "required_core_not_indexed",
            "captured evidence index is missing required core records: "
            + ", ".join(missing_core),
        )

    session = VerificationSession(snapshot)
    session.__enter__()
    try:
        _bind_bootstrap_run(
            snapshot=snapshot,
            entry=run_entry,
            captured=run_capture,
            parsed_run=parsed_run,
            spool_max_size=spool_max_size,
        )

        for entry in entries:
            path = entry["path"]
            if path == RUN_RECORD_PATH:
                continue
            storage_kind = classify_snapshot_storage(path, entry["roles"])
            evidence = _entry_object(
                entry,
                storage_kind=storage_kind,
                spool_max_size=spool_max_size,
            )
            snapshot.add_evidence(evidence)
            _run_hook(hooks.before_indexed_entry_acquire, path, evidence)
            try:
                acquire_bundle_evidence(
                    bundle_root=run_dir,
                    snapshot=snapshot,
                    evidence=evidence,
                    chunk_size=chunk_size,
                )
                evidence.seal()
            except (EvidenceAcquisitionError, SnapshotStateError) as exc:
                raise SnapshotBuildError(
                    "indexed_acquisition_failed",
                    f"cannot acquire indexed evidence {path}: {exc}",
                ) from exc
            if path in CORE_SEMANTIC_PATHS:
                parsed = _parse_core_bytes(path, _read_retained_bytes(evidence))
                snapshot.cache_parsed_record(path, parsed)

        try:
            snapshot.complete_acquisition()
            snapshot.seal()
            snapshot.require_established_evidence_root()
        except SnapshotStateError as exc:
            raise SnapshotBuildError("snapshot_incomplete", str(exc)) from exc
        return session
    except BaseException:
        session.close()
        raise
