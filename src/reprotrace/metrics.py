"""Capture and extract metrics from ordered, bundle-local text evidence."""

from __future__ import annotations

import csv
import glob
import io
import math
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Iterator, Mapping, Sequence

from .errors import ConfigError
from .evidence import normalize_bundle_path, resolve_bundle_file
from .io import sha256_bytes, sha256_file, write_bytes_atomic
from .manifest import SAFE_ID, LoadedManifest, substitute, validate_manifest
from .snapshot import (
    EvidenceFingerprint,
    EvidenceObjectState,
    SessionState,
    SnapshotStateError,
    StorageKind,
    VerificationSession,
)


METRIC_SOURCES_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ResolvedMetricSources:
    """One manifest metric and its origin sources in extraction order."""

    metric_id: str
    declared_path: str
    paths: tuple[Path, ...]


@dataclass(frozen=True)
class _MetricReaderSource:
    """One ordered metric source exposed through a fresh binary reader."""

    label: str
    open_reader: Callable[[], BinaryIO]


def empty_metric_sources_record() -> dict[str, Any]:
    return {"schema_version": METRIC_SOURCES_SCHEMA_VERSION, "metrics": []}


def _matches(value: str, manifest: LoadedManifest, context: Mapping[str, Any]) -> list[Path]:
    resolved = Path(substitute(value, context)).expanduser()
    if not resolved.is_absolute():
        resolved = manifest.project_root / resolved
    return sorted(
        Path(match).resolve()
        for match in glob.glob(str(resolved), recursive=True)
        if Path(match).is_file()
    )


def resolve_origin_metric_sources(
    manifest: LoadedManifest,
    context: Mapping[str, Any],
) -> list[ResolvedMetricSources]:
    """Resolve each metric's origin files using the existing ordered glob semantics."""

    resolved_metrics: list[ResolvedMetricSources] = []
    for specification in manifest.data.get("metrics", []):
        paths = _matches(specification["path"], manifest, context)
        if not paths:
            raise ConfigError(
                f"metric {specification['id']!r} matched no files: {specification['path']}"
            )
        resolved_metrics.append(
            ResolvedMetricSources(
                metric_id=specification["id"],
                declared_path=specification["path"],
                paths=tuple(paths),
            )
        )
    return resolved_metrics


def _select(values: list[float], selector: str) -> float:
    if not values:
        raise ConfigError("metric extractor found no numeric values")
    if selector == "last":
        return values[-1]
    if selector == "max":
        return max(values)
    if selector == "min":
        return min(values)
    raise ConfigError(f"unsupported metric selector: {selector}")


def _finite_metric_value(value: Any, *, context: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"invalid {context}; metric value must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConfigError(f"invalid {context}; metric value must be numeric") from exc
    if not math.isfinite(number):
        raise ConfigError(f"invalid {context}; metric value must be finite")
    return number


@contextmanager
def _open_text_metric_source(
    source: _MetricReaderSource,
    *,
    errors: str,
    newline: str | None,
    failure_prefix: str,
) -> Iterator[io.TextIOWrapper]:
    try:
        with source.open_reader() as binary:
            with io.TextIOWrapper(
                binary,
                encoding="utf-8",
                errors=errors,
                newline=newline,
            ) as text:
                yield text
    except ConfigError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"{failure_prefix} {source.label}: {exc}") from exc


def _csv_values(sources: Sequence[_MetricReaderSource], column: str) -> list[float]:
    values: list[float] = []
    for source in sources:
        try:
            with _open_text_metric_source(
                source,
                errors="strict",
                newline="",
                failure_prefix="cannot extract CSV metric from",
            ) as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None or column not in reader.fieldnames:
                    raise ConfigError(
                        f"CSV metric column {column!r} not found in {source.label}"
                    )
                for row in reader:
                    raw = row.get(column)
                    if raw not in (None, ""):
                        values.append(
                            _finite_metric_value(
                                raw,
                                context=(
                                    f"CSV metric column {column!r} in {source.label}"
                                ),
                            )
                        )
        except csv.Error as exc:
            raise ConfigError(
                f"cannot parse CSV metric from {source.label}: {exc}"
            ) from exc
    return values


def _regex_values(
    sources: Sequence[_MetricReaderSource],
    pattern: str,
    group: int | str,
) -> list[float]:
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"invalid metric regular expression: {exc}") from exc
    values: list[float] = []
    for source in sources:
        with _open_text_metric_source(
            source,
            errors="replace",
            newline=None,
            failure_prefix="cannot read metric log",
        ) as handle:
            text = handle.read()
        for match in compiled.finditer(text):
            try:
                raw = match.group(group)
            except (IndexError, KeyError, TypeError) as exc:
                raise ConfigError(
                    f"cannot parse regex group {group!r} as a number in "
                    f"{source.label}"
                ) from exc
            values.append(
                _finite_metric_value(
                    raw,
                    context=f"regex group {group!r} in {source.label}",
                )
            )
    return values


def _extract_metric_from_readers(
    specification: Mapping[str, Any],
    sources: Sequence[_MetricReaderSource],
) -> dict[str, Any]:
    if not sources:
        raise ConfigError(
            f"metric {specification.get('id')!r} has no bundle-local evidence sources"
        )
    if specification["extractor"] == "csv":
        values = _csv_values(sources, specification["column"])
    elif specification["extractor"] == "log_regex":
        values = _regex_values(
            sources,
            specification["pattern"],
            specification.get("group", 1),
        )
    else:
        raise ConfigError(
            f"unsupported metric extractor: {specification.get('extractor')}"
        )
    selector = specification.get("select", "last")
    return {
        "actual": _select(values, selector),
        "sample_count": len(values),
        "select": selector,
    }


def extract_metric_from_evidence(
    specification: Mapping[str, Any],
    source_paths: Sequence[Path],
) -> dict[str, Any]:
    """Pure extraction from explicitly ordered evidence paths, never origin metadata."""

    sources = [
        _MetricReaderSource(
            label=str(path),
            open_reader=lambda path=path: path.open("rb"),
        )
        for path in source_paths
    ]
    return _extract_metric_from_readers(specification, sources)


def _bundle_root(path: str | Path) -> Path:
    try:
        root = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ConfigError(f"cannot resolve metric evidence bundle root {path}: {exc}") from exc
    if not root.is_dir():
        raise ConfigError(f"invalid metric evidence bundle root; expected a directory: {root}")
    return root


def _evidence_metadata(bundle_root: Path, evidence_path: str) -> tuple[int, str]:
    candidate = resolve_bundle_file(
        bundle_root,
        evidence_path,
        label="metric source evidence",
    )
    try:
        size = candidate.stat().st_size
        digest = sha256_file(candidate)
    except OSError as exc:
        raise ConfigError(f"cannot fingerprint metric source evidence {candidate}: {exc}") from exc
    return size, digest


def _capture_external_source(
    bundle_root: Path,
    metric_id: str,
    ordinal: int,
    origin: Path,
) -> str:
    try:
        captured_bytes = origin.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot snapshot metric source {origin}: {exc}") from exc
    content_hash = sha256_bytes(captured_bytes)
    evidence_path = normalize_bundle_path(
        f"raw/metrics/{metric_id}/{content_hash[:12]}-{ordinal:04d}.source",
        label="metric source evidence",
    )
    destination = bundle_root.joinpath(*PurePosixPath(evidence_path).parts)
    try:
        write_bytes_atomic(destination, captured_bytes)
    except OSError as exc:
        raise ConfigError(f"cannot snapshot metric source {origin} to {destination}: {exc}") from exc
    return evidence_path


def capture_metric_sources(
    manifest: LoadedManifest,
    context: Mapping[str, Any],
    bundle_root: str | Path,
) -> dict[str, Any]:
    """Capture ordered metric sources, avoiding copies already inside the bundle."""

    root = _bundle_root(bundle_root)
    records: list[dict[str, Any]] = []
    for resolved_metric in resolve_origin_metric_sources(manifest, context):
        sources: list[dict[str, Any]] = []
        for ordinal, origin in enumerate(resolved_metric.paths):
            try:
                relative = origin.relative_to(root).as_posix()
            except ValueError:
                evidence_path = _capture_external_source(
                    root,
                    resolved_metric.metric_id,
                    ordinal,
                    origin,
                )
            else:
                evidence_path = normalize_bundle_path(relative, label="metric source evidence")
                resolve_bundle_file(root, evidence_path, label="metric source evidence")
            size, digest = _evidence_metadata(root, evidence_path)
            sources.append(
                {
                    "ordinal": ordinal,
                    "origin_path": str(origin),
                    "evidence_path": evidence_path,
                    "size_bytes": size,
                    "sha256": digest,
                }
            )
        records.append(
            {
                "id": resolved_metric.metric_id,
                "declared_path": resolved_metric.declared_path,
                "sources": sources,
            }
        )
    return validate_metric_sources_record(
        {"schema_version": METRIC_SOURCES_SCHEMA_VERSION, "metrics": records}
    )


def _validated_sha256(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        raise ConfigError(f"invalid {context}; sha256 must be lowercase SHA-256 hex")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ConfigError(f"invalid {context}; sha256 must be lowercase SHA-256 hex") from exc
    return value


def validate_metric_sources_record(value: Any) -> dict[str, Any]:
    """Validate schema 1 while preserving metric and source list order."""

    if not isinstance(value, dict):
        raise ConfigError("invalid metric_sources.json; root must be an object")
    if set(value) != {"schema_version", "metrics"}:
        raise ConfigError("invalid metric_sources.json; expected only schema_version and metrics")
    schema = value.get("schema_version")
    if (
        isinstance(schema, bool)
        or not isinstance(schema, int)
        or schema != METRIC_SOURCES_SCHEMA_VERSION
    ):
        raise ConfigError(f"unsupported metric_sources.json schema_version: {schema!r}")
    metrics = value.get("metrics")
    if not isinstance(metrics, list):
        raise ConfigError("invalid metric_sources.json; metrics must be an array")

    normalized_metrics: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for metric_index, metric in enumerate(metrics):
        if not isinstance(metric, dict) or set(metric) != {"id", "declared_path", "sources"}:
            raise ConfigError(
                f"invalid metric_sources.json metric {metric_index}; unexpected or missing fields"
            )
        metric_id = metric.get("id")
        if not isinstance(metric_id, str) or SAFE_ID.fullmatch(metric_id) is None:
            raise ConfigError(f"invalid metric_sources.json metric {metric_index}; invalid id")
        if metric_id in seen_ids:
            raise ConfigError(f"duplicate metric source id: {metric_id}")
        seen_ids.add(metric_id)
        declared_path = metric.get("declared_path")
        if not isinstance(declared_path, str) or not declared_path:
            raise ConfigError(
                f"invalid metric_sources.json metric {metric_index}; declared_path is required"
            )
        sources = metric.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ConfigError(
                f"invalid metric_sources.json metric {metric_index}; sources must be a non-empty array"
            )

        normalized_sources: list[dict[str, Any]] = []
        for source_index, source in enumerate(sources):
            context = f"metric_sources.json metric {metric_id!r} source {source_index}"
            if not isinstance(source, dict) or set(source) != {
                "ordinal",
                "origin_path",
                "evidence_path",
                "size_bytes",
                "sha256",
            }:
                raise ConfigError(f"invalid {context}; unexpected or missing fields")
            ordinal = source.get("ordinal")
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal != source_index:
                raise ConfigError(f"invalid {context}; ordinal must equal {source_index}")
            origin_path = source.get("origin_path")
            if not isinstance(origin_path, str) or not origin_path:
                raise ConfigError(f"invalid {context}; origin_path must be a non-empty string")
            evidence_path = source.get("evidence_path")
            normalized_path = normalize_bundle_path(evidence_path, label="metric source evidence")
            if evidence_path != normalized_path:
                raise ConfigError(f"invalid {context}; evidence_path must be canonical POSIX-style")
            size = source.get("size_bytes")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ConfigError(f"invalid {context}; size_bytes must be a non-negative integer")
            normalized_sources.append(
                {
                    "ordinal": ordinal,
                    "origin_path": origin_path,
                    "evidence_path": normalized_path,
                    "size_bytes": size,
                    "sha256": _validated_sha256(source.get("sha256"), context=context),
                }
            )
        normalized_metrics.append(
            {
                "id": metric_id,
                "declared_path": declared_path,
                "sources": normalized_sources,
            }
        )
    return {"schema_version": METRIC_SOURCES_SCHEMA_VERSION, "metrics": normalized_metrics}


def _verified_evidence_paths(
    bundle_root: Path,
    metric_record: Mapping[str, Any],
) -> list[Path]:
    paths: list[Path] = []
    for source in metric_record["sources"]:
        candidate = resolve_bundle_file(
            bundle_root,
            source["evidence_path"],
            label="metric source evidence",
        )
        size, digest = _evidence_metadata(bundle_root, source["evidence_path"])
        if size != source["size_bytes"] or digest != source["sha256"]:
            raise ConfigError(
                f"captured metric source changed before extraction: {source['evidence_path']}"
            )
        paths.append(candidate)
    return paths


def _derived_metric_record(
    specification: Mapping[str, Any],
    extraction: Mapping[str, Any],
    *,
    origin_paths: Sequence[str],
    evidence_paths: Sequence[str] | None,
) -> dict[str, Any]:
    actual = _finite_metric_value(extraction["actual"], context="extracted metric actual")
    expected = _finite_metric_value(specification["expected"], context="metric expected")
    atol = _finite_metric_value(specification.get("atol", 0.0), context="metric atol")
    rtol = _finite_metric_value(specification.get("rtol", 0.0), context="metric rtol")
    if atol < 0 or rtol < 0:
        raise ConfigError("metric tolerances must be non-negative")
    record = {
        "id": specification["id"],
        "extractor": specification["extractor"],
        "source_paths": list(origin_paths),
        "select": extraction["select"],
        "sample_count": extraction["sample_count"],
        "actual": actual,
        "expected": expected,
        "atol": atol,
        "rtol": rtol,
        "absolute_error": abs(actual - expected),
        "passed": math.isclose(actual, expected, abs_tol=atol, rel_tol=rtol),
    }
    if evidence_paths is not None:
        record["source_evidence_paths"] = list(evidence_paths)
    return record


def _metric_source_records_by_id(
    specifications: Sequence[Mapping[str, Any]],
    source_record: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    by_id = {metric["id"]: metric for metric in source_record["metrics"]}
    declared_ids = [specification["id"] for specification in specifications]
    declared_id_set = set(declared_ids)
    missing = [metric_id for metric_id in declared_ids if metric_id not in by_id]
    extra = [metric_id for metric_id in by_id if metric_id not in declared_id_set]
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected: {', '.join(extra)}")
        raise ConfigError(
            "metric source declarations do not match resolved manifest; "
            + "; ".join(details)
        )
    return by_id


def _snapshot_metric_reader_sources(
    session: VerificationSession,
    metric_record: Mapping[str, Any],
) -> list[_MetricReaderSource]:
    snapshot = session.snapshot
    objects = snapshot.objects
    readers: list[_MetricReaderSource] = []
    for source in metric_record["sources"]:
        evidence_path = source["evidence_path"]
        evidence = objects.get(evidence_path)
        if evidence is None:
            raise ConfigError(
                f"metric source evidence is absent from verified snapshot: {evidence_path}"
            )
        if "metric_source" not in evidence.roles:
            raise ConfigError(
                f"verified snapshot evidence lacks metric_source role: {evidence_path}"
            )
        if evidence.state is not EvidenceObjectState.SEALED or evidence.closed:
            raise SnapshotStateError(
                f"metric source evidence is not sealed and readable: {evidence_path}"
            )
        if evidence.storage_kind is StorageKind.INTEGRITY_ONLY:
            raise ConfigError(
                f"metric source evidence has no retained semantic representation: "
                f"{evidence_path}"
            )
        declared_fingerprint = EvidenceFingerprint(
            source["size_bytes"],
            source["sha256"],
        )
        if evidence.expected_fingerprint != declared_fingerprint:
            raise ConfigError(
                "metric_sources.json fingerprint does not match the evidence index for "
                f"{evidence_path}"
            )
        if (
            not evidence.acquired_and_validated
            or evidence.observed_fingerprint != evidence.expected_fingerprint
        ):
            raise SnapshotStateError(
                f"metric source evidence integrity is not established: {evidence_path}"
            )
        readers.append(
            _MetricReaderSource(
                label=evidence_path,
                open_reader=evidence.open_reader,
            )
        )
    return readers


def extract_metrics_from_snapshot(
    session: VerificationSession,
) -> list[dict[str, Any]]:
    """Derive metrics solely from one open, sealed verified snapshot."""

    if not isinstance(session, VerificationSession):
        raise TypeError("snapshot metric extraction requires a VerificationSession")
    snapshot = session.snapshot
    if session.state is not SessionState.OPEN or not snapshot.session_active:
        raise SnapshotStateError(
            "snapshot metric extraction requires an active verification session"
        )
    if not snapshot.sealed:
        raise SnapshotStateError(
            "snapshot metric extraction requires a fully sealed snapshot"
        )
    snapshot.require_established_evidence_root()

    resolved_manifest = snapshot.parsed_record("manifest.resolved.yaml")
    validate_manifest(resolved_manifest)
    specifications = resolved_manifest.get("metrics", [])
    metric_sources = validate_metric_sources_record(
        snapshot.parsed_record("metric_sources.json")
    )
    by_id = _metric_source_records_by_id(specifications, metric_sources)

    records: list[dict[str, Any]] = []
    for specification in specifications:
        metric_record = by_id[specification["id"]]
        reader_sources = _snapshot_metric_reader_sources(session, metric_record)
        extraction = _extract_metric_from_readers(specification, reader_sources)
        records.append(
            _derived_metric_record(
                specification,
                extraction,
                origin_paths=[
                    source["origin_path"] for source in metric_record["sources"]
                ],
                evidence_paths=[
                    source["evidence_path"] for source in metric_record["sources"]
                ],
            )
        )
    return records


def extract_metrics_from_evidence(
    specifications: Sequence[Mapping[str, Any]],
    bundle_root: str | Path,
    metric_sources: Any,
) -> list[dict[str, Any]]:
    """Build compatibility metric records strictly from captured bundle evidence."""

    root = _bundle_root(bundle_root)
    source_record = validate_metric_sources_record(metric_sources)
    by_id = _metric_source_records_by_id(specifications, source_record)

    records: list[dict[str, Any]] = []
    for specification in specifications:
        metric_record = by_id[specification["id"]]
        evidence_paths = _verified_evidence_paths(root, metric_record)
        extraction = extract_metric_from_evidence(specification, evidence_paths)
        records.append(
            _derived_metric_record(
                specification,
                extraction,
                origin_paths=[source["origin_path"] for source in metric_record["sources"]],
                evidence_paths=[source["evidence_path"] for source in metric_record["sources"]],
            )
        )
    return records


def extract_metrics(
    manifest: LoadedManifest,
    context: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Compatibility extraction from origins; the runner no longer uses this path."""

    resolved = resolve_origin_metric_sources(manifest, context)
    by_id = {metric.metric_id: metric for metric in resolved}
    records: list[dict[str, Any]] = []
    for specification in manifest.data.get("metrics", []):
        sources = by_id[specification["id"]]
        extraction = extract_metric_from_evidence(specification, sources.paths)
        records.append(
            _derived_metric_record(
                specification,
                extraction,
                origin_paths=[str(path) for path in sources.paths],
                evidence_paths=None,
            )
        )
    return records
