from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import yaml
import pytest

from reprotrace.errors import ConfigError
from reprotrace.io import read_json, sha256_file
from reprotrace.manifest import load_manifest, runtime_context
from reprotrace.metrics import (
    capture_metric_sources,
    extract_metrics,
    extract_metrics_from_evidence,
    validate_metric_sources_record,
)
from reprotrace.runner import run_manifest


def write_manifest(
    project: Path,
    *,
    command: str,
    metrics: list[dict[str, object]],
) -> Path:
    manifest = project / "reprotrace.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 0,
                "project": {"name": "metric-evidence", "root": "."},
                "run": {
                    "output_root": ".evidence",
                    "steps": [
                        {
                            "id": "produce",
                            "argv": [sys.executable, "-c", command],
                        }
                    ],
                },
                "metrics": metrics,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return manifest


def csv_metric(path: str, *, expected: float, select: str = "last") -> dict[str, object]:
    return {
        "id": "score",
        "extractor": "csv",
        "path": path,
        "column": "score",
        "select": select,
        "expected": expected,
        "atol": 0.0,
        "rtol": 0.0,
    }


def test_external_csv_is_snapshotted_and_extracted_from_bundle(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    command = (
        "from pathlib import Path; "
        "p=Path('output/metrics.csv'); p.parent.mkdir(); "
        "p.write_text('score\\n7.5\\n', encoding='utf-8')"
    )
    manifest = write_manifest(
        project,
        command=command,
        metrics=[csv_metric("output/metrics.csv", expected=7.5)],
    )

    run_dir, verification = run_manifest(manifest)

    metric_sources = read_json(run_dir / "metric_sources.json")
    source = metric_sources["metrics"][0]["sources"][0]
    captured = run_dir / Path(source["evidence_path"])
    origin = project / "output" / "metrics.csv"
    metric = read_json(run_dir / "metrics.json")[0]
    assert metric_sources["schema_version"] == 1
    assert metric_sources["metrics"][0]["declared_path"] == "output/metrics.csv"
    assert source["ordinal"] == 0
    assert source["origin_path"] == str(origin.resolve())
    assert source["evidence_path"].startswith("raw/metrics/score/")
    assert captured.read_bytes() == origin.read_bytes()
    assert source["size_bytes"] == captured.stat().st_size
    assert source["sha256"] == sha256_file(captured)
    assert metric["actual"] == 7.5
    assert metric["source_paths"] == [str(origin.resolve())]
    assert metric["source_evidence_paths"] == [source["evidence_path"]]
    assert verification["assurance_level"] == "metric_derivations_recomputed"
    assert verification["coverage"]["metric_sources"]["captured"] == 1
    assert verification["result_status"] == "matched"


def test_extraction_is_independent_of_origin_after_capture(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    origin = project / "origin.csv"
    origin.write_text("score\n4.25\n", encoding="utf-8")
    manifest_path = write_manifest(
        project,
        command="pass",
        metrics=[csv_metric("origin.csv", expected=4.25)],
    )
    manifest = load_manifest(manifest_path)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    context = runtime_context(manifest, bundle, 0)

    metric_sources = capture_metric_sources(manifest, context, bundle)
    origin.unlink()
    metrics = extract_metrics_from_evidence(manifest.data["metrics"], bundle, metric_sources)

    assert metrics[0]["actual"] == 4.25
    assert not origin.exists()


def test_multi_source_order_survives_nonlexical_evidence_names(tmp_path: Path) -> None:
    project = tmp_path / "project"
    sources = project / "sources"
    sources.mkdir(parents=True)
    candidates = []
    for value in range(1, 33):
        payload = f"score\n{value}.0\n".encode("utf-8")
        candidates.append((hashlib.sha256(payload).hexdigest(), float(value), payload))
    first = max(candidates, key=lambda item: item[0])
    second = min(candidates, key=lambda item: item[0])
    (sources / "a.csv").write_bytes(first[2])
    (sources / "b.csv").write_bytes(second[2])
    manifest_path = write_manifest(
        project,
        command="pass",
        metrics=[csv_metric("sources/*.csv", expected=second[1])],
    )
    manifest = load_manifest(manifest_path)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    context = runtime_context(manifest, bundle, 0)

    origin_metrics = extract_metrics(manifest, context)
    metric_sources = capture_metric_sources(manifest, context, bundle)
    source_records = metric_sources["metrics"][0]["sources"]
    evidence_paths = [source["evidence_path"] for source in source_records]
    captured_metrics = extract_metrics_from_evidence(
        manifest.data["metrics"],
        bundle,
        metric_sources,
    )

    assert [source["ordinal"] for source in source_records] == [0, 1]
    assert evidence_paths != sorted(evidence_paths)
    assert origin_metrics[0]["actual"] == second[1]
    assert captured_metrics[0]["actual"] == origin_metrics[0]["actual"]
    assert captured_metrics[0]["source_evidence_paths"] == evidence_paths


def test_missing_external_source_records_failure_without_fake_metric(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manifest = write_manifest(
        project,
        command="pass",
        metrics=[csv_metric("missing.csv", expected=1.0)],
    )

    run_dir, verification = run_manifest(manifest)

    run = read_json(run_dir / "run.json")
    assert "matched no files" in run["evidence_error"]
    assert read_json(run_dir / "metric_sources.json") == {"schema_version": 1, "metrics": []}
    assert read_json(run_dir / "metrics.json") == []
    assert verification["status"] == "failed"
    assert verification["assurance_level"] == "recorded"


def test_zero_metric_run_writes_empty_metric_source_schema(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manifest = write_manifest(project, command="pass", metrics=[])

    run_dir, verification = run_manifest(manifest)

    assert read_json(run_dir / "metric_sources.json") == {"schema_version": 1, "metrics": []}
    assert read_json(run_dir / "metrics.json") == []
    assert verification["status"] == "passed"
    assert verification["assurance_level"] == "bundle_integrity_checked"


def test_metric_source_schema_rejects_duplicate_metric_ids() -> None:
    metric = {
        "id": "score",
        "declared_path": "origin.csv",
        "sources": [
            {
                "ordinal": 0,
                "origin_path": "C:/producer/origin.csv",
                "evidence_path": "raw/metrics/score/source.source",
                "size_bytes": 1,
                "sha256": hashlib.sha256(b"x").hexdigest(),
            }
        ],
    }

    with pytest.raises(ConfigError, match="duplicate metric source id"):
        validate_metric_sources_record({"schema_version": 1, "metrics": [metric, dict(metric)]})


def test_metric_source_schema_rejects_nonsequential_ordinal() -> None:
    record = {
        "schema_version": 1,
        "metrics": [
            {
                "id": "score",
                "declared_path": "origin.csv",
                "sources": [
                    {
                        "ordinal": 1,
                        "origin_path": "C:/producer/origin.csv",
                        "evidence_path": "raw/metrics/score/source.source",
                        "size_bytes": 1,
                        "sha256": hashlib.sha256(b"x").hexdigest(),
                    }
                ],
            }
        ],
    }

    with pytest.raises(ConfigError, match="ordinal must equal 0"):
        validate_metric_sources_record(record)
