from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

import reprotrace.metrics as metrics_module
from reprotrace.evidence import canonical_evidence_index_bytes, write_evidence_index
from reprotrace.errors import ConfigError
from reprotrace.io import sha256_bytes
from reprotrace.metrics import (
    extract_metrics_from_evidence,
    extract_metrics_from_snapshot,
)
from reprotrace.runner import run_manifest
from reprotrace.snapshot import (
    EvidenceObjectState,
    SnapshotStateError,
    StorageKind,
    VerificationSession,
    VerifiedBundleSnapshot,
    VerifiedEvidenceObject,
)
from reprotrace.snapshot_builder import open_schema_one_snapshot


CORE_ROLES: dict[str, list[str]] = {
    "run.json": ["record"],
    "source.json": ["record", "source"],
    "environment.json": ["environment", "record"],
    "inputs.json": ["input_record", "record"],
    "commands.json": ["command_record", "record"],
    "artifacts.json": ["artifact_record", "record"],
    "metrics.json": ["metric_record", "record"],
    "manifest.resolved.yaml": ["record", "resolved_manifest"],
    "metric_sources.json": ["metric_source_record", "record"],
}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _csv_metric(
    metric_id: str = "score",
    *,
    expected: float = 2.0,
    select: str = "last",
    column: str = "score",
    atol: float = 0.0,
    rtol: float = 0.0,
) -> dict[str, Any]:
    return {
        "id": metric_id,
        "extractor": "csv",
        "path": f"producer/{metric_id}.csv",
        "column": column,
        "select": select,
        "expected": expected,
        "atol": atol,
        "rtol": rtol,
    }


def _regex_metric(
    metric_id: str = "score",
    *,
    expected: float = 2.0,
    select: str = "last",
    pattern: str = r"score=([0-9.eE+-]+)",
    group: int | str = 1,
) -> dict[str, Any]:
    return {
        "id": metric_id,
        "extractor": "log_regex",
        "path": f"producer/{metric_id}.log",
        "pattern": pattern,
        "group": group,
        "select": select,
        "expected": expected,
        "atol": 0.0,
        "rtol": 0.0,
    }


def _source(
    evidence_path: str,
    payload: bytes,
    *,
    origin_path: str = "Z:/producer/source",
    roles: list[str] | None = None,
    include_in_index: bool = True,
    declared_size: int | None = None,
    declared_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "evidence_path": evidence_path,
        "payload": payload,
        "origin_path": origin_path,
        "roles": ["metric_source"] if roles is None else roles,
        "include_in_index": include_in_index,
        "declared_size": declared_size,
        "declared_sha256": declared_sha256,
    }


def _resolved_manifest(specifications: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 0,
        "project": {"name": "snapshot-metrics", "root": "/producer/project"},
        "run": {
            "output_root": "/producer/runs",
            "steps": [{"id": "noop", "argv": ["python", "noop.py"]}],
        },
        "metrics": specifications,
    }


def _write_metric_bundle(
    parent: Path,
    specifications: list[dict[str, Any]],
    source_map: dict[str, list[dict[str, Any]]],
    *,
    name: str = "bundle",
) -> tuple[Path, dict[str, Any]]:
    bundle = parent / name
    bundle.mkdir(parents=True)
    declarations: list[dict[str, object]] = []
    written_sources: dict[str, tuple[bytes, list[str]]] = {}
    metric_records: list[dict[str, Any]] = []

    specification_by_id = {item["id"]: item for item in specifications}
    for metric_id, source_definitions in source_map.items():
        source_records: list[dict[str, Any]] = []
        for ordinal, definition in enumerate(source_definitions):
            evidence_path = definition["evidence_path"]
            payload = definition["payload"]
            roles = definition["roles"]
            if definition["include_in_index"]:
                previous = written_sources.get(evidence_path)
                if previous is None:
                    candidate = bundle.joinpath(*evidence_path.split("/"))
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    candidate.write_bytes(payload)
                    declarations.append({"path": evidence_path, "roles": roles})
                    written_sources[evidence_path] = (payload, roles)
                else:
                    assert previous == (payload, roles)
            source_records.append(
                {
                    "ordinal": ordinal,
                    "origin_path": definition["origin_path"],
                    "evidence_path": evidence_path,
                    "size_bytes": (
                        len(payload)
                        if definition["declared_size"] is None
                        else definition["declared_size"]
                    ),
                    "sha256": (
                        sha256_bytes(payload)
                        if definition["declared_sha256"] is None
                        else definition["declared_sha256"]
                    ),
                }
            )
        metric_records.append(
            {
                "id": metric_id,
                "declared_path": specification_by_id.get(
                    metric_id, {"path": f"producer/{metric_id}"}
                )["path"],
                "sources": source_records,
            }
        )

    metric_sources = {"schema_version": 1, "metrics": metric_records}
    core_payloads = {
        "run.json": _json_bytes({"schema_version": 1, "status": "executed"}),
        "source.json": _json_bytes({"available": False}),
        "environment.json": _json_bytes({"python": "fixture"}),
        "inputs.json": _json_bytes([]),
        "commands.json": _json_bytes([]),
        "artifacts.json": _json_bytes([]),
        "metrics.json": _json_bytes([]),
        "manifest.resolved.yaml": yaml.safe_dump(
            _resolved_manifest(specifications), sort_keys=False
        ).encode("utf-8"),
        "metric_sources.json": _json_bytes(metric_sources),
    }
    for path, payload in core_payloads.items():
        (bundle / path).write_bytes(payload)
        declarations.append({"path": path, "roles": CORE_ROLES[path]})
    write_evidence_index(bundle, declarations)
    return bundle, metric_sources


def _extract(
    bundle: Path,
) -> tuple[VerificationSession, list[dict[str, Any]]]:
    session = open_schema_one_snapshot(bundle)
    try:
        records = extract_metrics_from_snapshot(session)
    except BaseException:
        session.close()
        raise
    return session, records


def _check_by_id(verification: dict[str, Any], check_id: str) -> dict[str, Any]:
    return next(check for check in verification["checks"] if check["id"] == check_id)


def test_basic_snapshot_csv_metric(tmp_path: Path) -> None:
    specification = _csv_metric(expected=2.0, atol=0.1, rtol=0.01)
    source = _source(
        "raw/score.csv",
        b"step,score\n0,1.0\n1,\n2,2.0\n",
        origin_path="/absent/producer/score.csv",
    )
    bundle, _ = _write_metric_bundle(tmp_path, [specification], {"score": [source]})

    session, records = _extract(bundle)
    try:
        assert records == [
            {
                "id": "score",
                "extractor": "csv",
                "source_paths": ["/absent/producer/score.csv"],
                "source_evidence_paths": ["raw/score.csv"],
                "select": "last",
                "sample_count": 2,
                "actual": 2.0,
                "expected": 2.0,
                "atol": 0.1,
                "rtol": 0.01,
                "absolute_error": 0.0,
                "passed": True,
            }
        ]
    finally:
        session.close()


def test_basic_snapshot_regex_metric(tmp_path: Path) -> None:
    specification = _regex_metric(expected=2.5)
    source = _source(
        "logs/score.log",
        b"score=1.5\nnoise\nscore=2.5\n",
        origin_path="C:/absent/score.log",
    )
    bundle, _ = _write_metric_bundle(tmp_path, [specification], {"score": [source]})

    session, records = _extract(bundle)
    try:
        assert records[0]["actual"] == 2.5
        assert records[0]["sample_count"] == 2
        assert records[0]["source_paths"] == ["C:/absent/score.log"]
        assert records[0]["source_evidence_paths"] == ["logs/score.log"]
        assert records[0]["passed"] is True
    finally:
        session.close()


def test_live_csv_deleted_after_snapshot_does_not_affect_extraction(tmp_path: Path) -> None:
    specification = _csv_metric(expected=4.0)
    source = _source("raw/score.csv", b"score\n4.0\n")
    bundle, _ = _write_metric_bundle(tmp_path, [specification], {"score": [source]})
    session = open_schema_one_snapshot(bundle)
    try:
        (bundle / "raw" / "score.csv").unlink()
        assert extract_metrics_from_snapshot(session)[0]["actual"] == 4.0
    finally:
        session.close()


def test_live_regex_replacement_does_not_affect_extraction(tmp_path: Path) -> None:
    specification = _regex_metric(expected=3.0)
    source = _source("logs/score.log", b"score=3.0\n")
    bundle, _ = _write_metric_bundle(tmp_path, [specification], {"score": [source]})
    session = open_schema_one_snapshot(bundle)
    try:
        (bundle / "logs" / "score.log").write_bytes(b"score=999.0\n")
        assert extract_metrics_from_snapshot(session)[0]["actual"] == 3.0
    finally:
        session.close()


def test_live_metric_source_outside_redirect_does_not_affect_extraction(
    tmp_path: Path,
) -> None:
    specification = _csv_metric(expected=5.0)
    source = _source("raw/score.csv", b"score\n5.0\n")
    bundle, _ = _write_metric_bundle(tmp_path, [specification], {"score": [source]})
    outside = tmp_path / "outside.csv"
    outside.write_bytes(b"score\n999.0\n")
    session = open_schema_one_snapshot(bundle)
    live = bundle / "raw" / "score.csv"
    try:
        live.unlink()
        try:
            live.symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"file symlink creation unavailable: {exc}")
        assert extract_metrics_from_snapshot(session)[0]["actual"] == 5.0
    finally:
        session.close()


def test_snapshot_extraction_performs_no_live_path_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specification = _csv_metric(expected=6.0)
    source = _source("raw/score.csv", b"score\n6.0\n")
    bundle, _ = _write_metric_bundle(tmp_path, [specification], {"score": [source]})
    session = open_schema_one_snapshot(bundle)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("snapshot extraction attempted a live path operation")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(metrics_module, "resolve_bundle_file", forbidden)
    monkeypatch.setattr(metrics_module, "sha256_file", forbidden)
    monkeypatch.setattr(os, "stat", forbidden)
    try:
        records = extract_metrics_from_snapshot(session)
        monkeypatch.undo()
        assert records[0]["actual"] == 6.0
    finally:
        monkeypatch.undo()
        session.close()


def test_multiple_sources_follow_metric_source_ordinal_order(tmp_path: Path) -> None:
    specification = _csv_metric(expected=2.0, select="last")
    sources = [
        _source("raw/z-first.csv", b"score\n9.0\n", origin_path="first.csv"),
        _source("raw/a-second.csv", b"score\n2.0\n", origin_path="second.csv"),
    ]
    bundle, _ = _write_metric_bundle(tmp_path, [specification], {"score": sources})

    session, records = _extract(bundle)
    try:
        assert records[0]["actual"] == 2.0
        assert records[0]["sample_count"] == 2
        assert records[0]["source_evidence_paths"] == [
            "raw/z-first.csv",
            "raw/a-second.csv",
        ]
        assert records[0]["source_paths"] == ["first.csv", "second.csv"]
    finally:
        session.close()


def test_two_metrics_can_share_one_retained_source(tmp_path: Path) -> None:
    specifications = [
        _csv_metric("last_score", expected=2.0, select="last"),
        _csv_metric("max_score", expected=2.0, select="max"),
    ]
    shared = _source("raw/shared.csv", b"score\n1.0\n2.0\n")
    bundle, _ = _write_metric_bundle(
        tmp_path,
        specifications,
        {"last_score": [shared], "max_score": [shared]},
    )

    session, records = _extract(bundle)
    try:
        assert [record["id"] for record in records] == ["last_score", "max_score"]
        assert [record["sample_count"] for record in records] == [2, 2]
        assert [record["actual"] for record in records] == [2.0, 2.0]
    finally:
        session.close()


def test_snapshot_csv_missing_column_fails(tmp_path: Path) -> None:
    specification = _csv_metric(column="missing")
    source = _source("raw/score.csv", b"score\n2.0\n")
    bundle, _ = _write_metric_bundle(tmp_path, [specification], {"score": [source]})

    with open_schema_one_snapshot(bundle) as session:
        with pytest.raises(ConfigError, match="column 'missing' not found"):
            extract_metrics_from_snapshot(session)


@pytest.mark.parametrize("token", ["NaN", "Inf", "-Inf"])
def test_snapshot_csv_nonfinite_value_fails(tmp_path: Path, token: str) -> None:
    specification = _csv_metric()
    source = _source("raw/score.csv", f"score\n{token}\n".encode("utf-8"))
    bundle, _ = _write_metric_bundle(tmp_path, [specification], {"score": [source]})

    with open_schema_one_snapshot(bundle) as session:
        with pytest.raises(ConfigError, match="finite"):
            extract_metrics_from_snapshot(session)


def test_snapshot_csv_invalid_utf8_fails_closed(tmp_path: Path) -> None:
    specification = _csv_metric()
    source = _source("raw/score.csv", b"score\n\xff\n")
    bundle, _ = _write_metric_bundle(tmp_path, [specification], {"score": [source]})

    with open_schema_one_snapshot(bundle) as session:
        with pytest.raises(ConfigError, match="cannot extract CSV metric"):
            extract_metrics_from_snapshot(session)


def test_snapshot_regex_invalid_expression_fails(tmp_path: Path) -> None:
    specification = _regex_metric(pattern="(")
    source = _source("logs/score.log", b"score=2.0\n")
    bundle, _ = _write_metric_bundle(tmp_path, [specification], {"score": [source]})

    with open_schema_one_snapshot(bundle) as session:
        with pytest.raises(ConfigError, match="invalid metric regular expression"):
            extract_metrics_from_snapshot(session)


@pytest.mark.parametrize("group", [2, "missing"])
def test_snapshot_regex_missing_or_invalid_group_fails(
    tmp_path: Path,
    group: int | str,
) -> None:
    specification = _regex_metric(group=group)
    source = _source("logs/score.log", b"score=2.0\n")
    bundle, _ = _write_metric_bundle(tmp_path, [specification], {"score": [source]})

    with open_schema_one_snapshot(bundle) as session:
        with pytest.raises(ConfigError, match="cannot parse regex group"):
            extract_metrics_from_snapshot(session)


@pytest.mark.parametrize("token", ["NaN", "Inf", "-Inf"])
def test_snapshot_regex_nonfinite_value_fails(tmp_path: Path, token: str) -> None:
    specification = _regex_metric(pattern=r"score=([^\s]+)")
    source = _source("logs/score.log", f"score={token}\n".encode("utf-8"))
    bundle, _ = _write_metric_bundle(tmp_path, [specification], {"score": [source]})

    with open_schema_one_snapshot(bundle) as session:
        with pytest.raises(ConfigError, match="finite"):
            extract_metrics_from_snapshot(session)


def test_snapshot_metric_with_no_numeric_values_fails(tmp_path: Path) -> None:
    specification = _csv_metric()
    source = _source("raw/score.csv", b"score\n\n")
    bundle, _ = _write_metric_bundle(tmp_path, [specification], {"score": [source]})

    with open_schema_one_snapshot(bundle) as session:
        with pytest.raises(ConfigError, match="found no numeric values"):
            extract_metrics_from_snapshot(session)


def test_snapshot_selector_last(tmp_path: Path) -> None:
    specification = _csv_metric(expected=2.0, select="last")
    source = _source("raw/score.csv", b"score\n3.0\n1.0\n2.0\n")
    bundle, _ = _write_metric_bundle(tmp_path, [specification], {"score": [source]})

    session, records = _extract(bundle)
    try:
        assert records[0]["actual"] == 2.0
    finally:
        session.close()


def test_snapshot_selector_min(tmp_path: Path) -> None:
    specification = _csv_metric(expected=1.0, select="min")
    source = _source("raw/score.csv", b"score\n3.0\n1.0\n2.0\n")
    bundle, _ = _write_metric_bundle(tmp_path, [specification], {"score": [source]})

    session, records = _extract(bundle)
    try:
        assert records[0]["actual"] == 1.0
    finally:
        session.close()


def test_snapshot_selector_max(tmp_path: Path) -> None:
    specification = _csv_metric(expected=3.0, select="max")
    source = _source("raw/score.csv", b"score\n3.0\n1.0\n2.0\n")
    bundle, _ = _write_metric_bundle(tmp_path, [specification], {"score": [source]})

    session, records = _extract(bundle)
    try:
        assert records[0]["actual"] == 3.0
    finally:
        session.close()


def test_snapshot_metric_sources_missing_manifest_id_fails(tmp_path: Path) -> None:
    bundle, _ = _write_metric_bundle(tmp_path, [_csv_metric()], {})

    with open_schema_one_snapshot(bundle) as session:
        with pytest.raises(ConfigError, match="missing: score"):
            extract_metrics_from_snapshot(session)


def test_snapshot_metric_sources_extra_id_fails(tmp_path: Path) -> None:
    extra = _source("raw/extra.csv", b"score\n1.0\n")
    bundle, _ = _write_metric_bundle(tmp_path, [], {"extra": [extra]})

    with open_schema_one_snapshot(bundle) as session:
        with pytest.raises(ConfigError, match="unexpected: extra"):
            extract_metrics_from_snapshot(session)


def test_snapshot_metric_source_object_missing_fails(tmp_path: Path) -> None:
    source = _source(
        "raw/missing.csv",
        b"score\n2.0\n",
        include_in_index=False,
    )
    bundle, _ = _write_metric_bundle(tmp_path, [_csv_metric()], {"score": [source]})

    with open_schema_one_snapshot(bundle) as session:
        with pytest.raises(ConfigError, match="absent from verified snapshot"):
            extract_metrics_from_snapshot(session)


def test_snapshot_metric_source_role_is_required(tmp_path: Path) -> None:
    source = _source("raw/score.csv", b"score\n2.0\n", roles=["artifact"])
    bundle, _ = _write_metric_bundle(tmp_path, [_csv_metric()], {"score": [source]})

    with open_schema_one_snapshot(bundle) as session:
        with pytest.raises(ConfigError, match="lacks metric_source role"):
            extract_metrics_from_snapshot(session)


def test_snapshot_metric_source_fingerprint_must_match_semantic_record(
    tmp_path: Path,
) -> None:
    source = _source(
        "raw/score.csv",
        b"score\n2.0\n",
        declared_sha256="0" * 64,
    )
    bundle, _ = _write_metric_bundle(tmp_path, [_csv_metric()], {"score": [source]})

    with open_schema_one_snapshot(bundle) as session:
        with pytest.raises(ConfigError, match="fingerprint does not match"):
            extract_metrics_from_snapshot(session)


def test_snapshot_metric_source_requires_established_object_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source("raw/score.csv", b"score\n2.0\n")
    bundle, _ = _write_metric_bundle(tmp_path, [_csv_metric()], {"score": [source]})
    session = open_schema_one_snapshot(bundle)
    target = session.snapshot.objects["raw/score.csv"]
    original_property = VerifiedEvidenceObject.acquired_and_validated

    def integrity(self: VerifiedEvidenceObject) -> bool:
        if self is target:
            return False
        assert original_property.fget is not None
        return original_property.fget(self)

    monkeypatch.setattr(
        VerifiedEvidenceObject,
        "acquired_and_validated",
        property(integrity),
    )
    try:
        with pytest.raises(SnapshotStateError, match="integrity is not established"):
            extract_metrics_from_snapshot(session)
    finally:
        session.close()


def test_snapshot_metric_extraction_rejects_closed_session(tmp_path: Path) -> None:
    source = _source("raw/score.csv", b"score\n2.0\n")
    bundle, _ = _write_metric_bundle(tmp_path, [_csv_metric()], {"score": [source]})
    session = open_schema_one_snapshot(bundle)
    session.close()

    with pytest.raises(SnapshotStateError, match="active verification session"):
        extract_metrics_from_snapshot(session)


def test_snapshot_metric_extraction_rejects_active_unsealed_snapshot() -> None:
    index = {"schema_version": 1, "entries": []}
    encoded = canonical_evidence_index_bytes(index)
    snapshot = VerifiedBundleSnapshot(
        bundle_root="display-only",
        index_bytes=encoded,
        parsed_index=index,
    )
    session = VerificationSession(snapshot)
    with session:
        with pytest.raises(SnapshotStateError, match="fully sealed"):
            extract_metrics_from_snapshot(session)


def test_snapshot_metric_extraction_requires_cached_manifest() -> None:
    index = {"schema_version": 1, "entries": []}
    encoded = canonical_evidence_index_bytes(index)
    snapshot = VerifiedBundleSnapshot(
        bundle_root="display-only",
        index_bytes=encoded,
        parsed_index=index,
    )
    session = VerificationSession(snapshot)
    with session:
        snapshot.complete_acquisition()
        snapshot.seal()
        with pytest.raises(SnapshotStateError, match="not cached"):
            extract_metrics_from_snapshot(session)


def test_snapshot_metric_extraction_requires_cached_metric_sources() -> None:
    manifest = _resolved_manifest([])
    payload = yaml.safe_dump(manifest, sort_keys=False).encode("utf-8")
    index = {
        "schema_version": 1,
        "entries": [
            {
                "path": "manifest.resolved.yaml",
                "roles": ["record", "resolved_manifest"],
                "size_bytes": len(payload),
                "sha256": sha256_bytes(payload),
            }
        ],
    }
    encoded = canonical_evidence_index_bytes(index)
    snapshot = VerifiedBundleSnapshot(
        bundle_root="display-only",
        index_bytes=encoded,
        parsed_index=index,
    )
    evidence = VerifiedEvidenceObject(
        relative_path="manifest.resolved.yaml",
        roles=["record", "resolved_manifest"],
        expected_size=len(payload),
        expected_sha256=sha256_bytes(payload),
        storage_kind=StorageKind.MEMORY,
    )
    session = VerificationSession(snapshot)
    with session:
        snapshot.add_evidence(evidence)
        evidence.acquire_bytes(payload)
        evidence.seal()
        snapshot.cache_parsed_record("manifest.resolved.yaml", manifest)
        snapshot.complete_acquisition()
        snapshot.seal()
        with pytest.raises(SnapshotStateError, match="not cached: metric_sources.json"):
            extract_metrics_from_snapshot(session)


def test_snapshot_metric_origin_path_may_be_absent(tmp_path: Path) -> None:
    absent_origin = tmp_path / "producer-was-deleted" / "score.csv"
    source = _source(
        "raw/score.csv",
        b"score\n7.0\n",
        origin_path=str(absent_origin),
    )
    bundle, _ = _write_metric_bundle(
        tmp_path,
        [_csv_metric(expected=7.0)],
        {"score": [source]},
    )

    session, records = _extract(bundle)
    try:
        assert absent_origin.exists() is False
        assert records[0]["actual"] == 7.0
        assert records[0]["source_paths"] == [str(absent_origin)]
    finally:
        session.close()


def test_snapshot_metric_extraction_is_relocation_independent(tmp_path: Path) -> None:
    source = _source("raw/score.csv", b"score\n8.0\n")
    origin, _ = _write_metric_bundle(
        tmp_path,
        [_csv_metric(expected=8.0)],
        {"score": [source]},
        name="origin-bundle",
    )
    relocated = tmp_path / "relocated-bundle"
    shutil.copytree(origin, relocated)
    shutil.rmtree(origin)

    session, records = _extract(relocated)
    try:
        assert records[0]["actual"] == 8.0
    finally:
        session.close()


def test_snapshot_csv_output_matches_legacy_path_extractor(tmp_path: Path) -> None:
    specification = _csv_metric(expected=2.0, select="max")
    source = _source("raw/score.csv", b"score\n1.0\n2.0\n")
    bundle, metric_sources = _write_metric_bundle(
        tmp_path,
        [specification],
        {"score": [source]},
    )
    legacy = extract_metrics_from_evidence([specification], bundle, metric_sources)

    session, snapshot_records = _extract(bundle)
    try:
        assert snapshot_records == legacy
    finally:
        session.close()


def test_snapshot_regex_output_matches_legacy_path_extractor(tmp_path: Path) -> None:
    specification = _regex_metric(
        expected=2.0,
        select="max",
        pattern=r"(?m)^score=([0-9.]+)$",
    )
    source = _source("logs/score.log", b"score=1.0\r\nscore=2.0\r\n")
    bundle, metric_sources = _write_metric_bundle(
        tmp_path,
        [specification],
        {"score": [source]},
    )
    legacy = extract_metrics_from_evidence([specification], bundle, metric_sources)

    session, snapshot_records = _extract(bundle)
    try:
        assert snapshot_records == legacy
    finally:
        session.close()


def test_tiny_snapshot_metric_matches_current_production_recomputation() -> None:
    repository = Path(__file__).resolve().parents[1]
    run_dir, verification = run_manifest(repository / "examples/tiny/reprotrace.yaml")

    with open_schema_one_snapshot(run_dir) as session:
        snapshot_records = extract_metrics_from_snapshot(session)

    production = _check_by_id(verification, "metric:mean_score:derived-match")
    assert production["passed"] is True
    assert snapshot_records[0]["actual"] == production["recomputed_actual"]
    assert snapshot_records[0]["sample_count"] == production["recomputed_sample_count"]
    assert snapshot_records[0]["source_evidence_paths"] == ["artifacts/metrics.csv"]
