from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reprotrace.errors import ConfigError
from reprotrace.io import write_json
from reprotrace.manifest import load_manifest
from reprotrace.protocol import bundle_artifact_path_matches


def test_rejects_shell_string(tmp_path: Path) -> None:
    manifest = {
        "schema_version": 0,
        "project": {"name": "unsafe", "root": "."},
        "run": {"steps": [{"id": "bad", "argv": "echo unsafe"}]},
    }
    path = tmp_path / "reprotrace.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    with pytest.raises(ConfigError, match="list of strings"):
        load_manifest(path)


def test_project_root_override(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    path = adapter / "reprotrace.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 0,
                "project": {"name": "portable", "root": "/does/not/exist"},
                "run": {
                    "output_root": ".evidence",
                    "steps": [{"id": "ok", "argv": ["python", "-V"]}],
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_manifest(path, project_root=checkout)
    assert loaded.project_root == checkout.resolve()
    assert loaded.output_root == (adapter / ".evidence").resolve()


def test_rejects_step_id_path_traversal(tmp_path: Path) -> None:
    path = tmp_path / "reprotrace.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 0,
                "project": {"name": "unsafe", "root": "."},
                "run": {"steps": [{"id": "../escape", "argv": ["python", "-V"]}]},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unsafe characters"):
        load_manifest(path)


def _numeric_manifest() -> dict[str, object]:
    return {
        "schema_version": 0,
        "project": {"name": "numeric", "root": "."},
        "run": {
            "steps": [
                {
                    "id": "run",
                    "argv": ["python", "-V"],
                    "timeout_seconds": 10,
                }
            ]
        },
        "metrics": [
            {
                "id": "score",
                "extractor": "csv",
                "path": "metrics.csv",
                "column": "score",
                "expected": 1.0,
                "atol": 0.0,
                "rtol": 0.0,
            }
        ],
    }


def test_metric_expected_rejects_boolean_numeric_alias(tmp_path: Path) -> None:
    manifest = _numeric_manifest()
    manifest["metrics"][0]["expected"] = True  # type: ignore[index]
    path = tmp_path / "reprotrace.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    with pytest.raises(ConfigError, match="expected must be a finite number"):
        load_manifest(path)


@pytest.mark.parametrize("field", ["atol", "rtol"])
@pytest.mark.parametrize("value", [float("inf"), float("nan"), -1.0])
def test_metric_tolerance_requires_finite_non_negative_domain(
    tmp_path: Path, field: str, value: float
) -> None:
    manifest = _numeric_manifest()
    manifest["metrics"][0][field] = value  # type: ignore[index]
    path = tmp_path / "reprotrace.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    with pytest.raises(ConfigError, match=f"{field} must be finite and non-negative"):
        load_manifest(path)


@pytest.mark.parametrize("value", [True, float("inf"), float("nan"), 0.0])
def test_timeout_requires_finite_positive_non_boolean_domain(
    tmp_path: Path, value: object
) -> None:
    manifest = _numeric_manifest()
    manifest["run"]["steps"][0]["timeout_seconds"] = value  # type: ignore[index]
    path = tmp_path / "reprotrace.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    with pytest.raises(ConfigError, match="timeout_seconds must be a finite positive number"):
        load_manifest(path)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_json_evidence_writer_rejects_non_finite_numbers(
    tmp_path: Path, value: float
) -> None:
    with pytest.raises(ConfigError, match="strict JSON"):
        write_json(tmp_path / "record.json", {"value": value})
    assert not (tmp_path / "record.json").exists()


def test_bundle_artifact_glob_uses_posix_segment_semantics() -> None:
    assert bundle_artifact_path_matches("artifacts/*.txt", "artifacts/result.txt")
    assert not bundle_artifact_path_matches(
        "artifacts/*.txt", "artifacts/nested/result.txt"
    )
    assert bundle_artifact_path_matches(
        "artifacts/**/*.txt", "artifacts/nested/result.txt"
    )
    assert bundle_artifact_path_matches(
        "artifacts/**/*.txt", "artifacts/result.txt"
    )
