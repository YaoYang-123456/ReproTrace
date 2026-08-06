from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from reprotrace.capture import SourceCapture
from reprotrace.diffing import compare_bundles
from reprotrace.errors import ConfigError
from reprotrace.io import read_json
from reprotrace.runner import run_manifest
from reprotrace.verifier import verify_bundle


EXAMPLE = Path(__file__).parents[1] / "examples" / "tiny"


def initialize_repository(project: Path, *paths: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "add", *paths], cwd=project, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=project, check=True)


def make_tiny_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy(EXAMPLE / "experiment.py", project / "experiment.py")
    shutil.copy(EXAMPLE / "input.txt", project / "input.txt")
    manifest = {
        "schema_version": 0,
        "project": {"name": "tiny-test", "root": "."},
        "claim": {"id": "mean", "protocol": "strict"},
        "inputs": [{"id": "numbers", "kind": "dataset", "path": "input.txt", "required": True}],
        "run": {
            "output_root": ".evidence",
            "seed": 42,
            "steps": [
                {
                    "id": "calculate",
                    "argv": [
                        "{python}",
                        "experiment.py",
                        "--input",
                        "input.txt",
                        "--output",
                        "{run_dir}/artifacts/metrics.csv",
                        "--metadata",
                        "{run_dir}/artifacts/metadata.json",
                        "--seed",
                        "{seed}",
                    ],
                    "timeout_seconds": 30,
                    "artifacts": [
                        "{run_dir}/artifacts/metrics.csv",
                        "{run_dir}/artifacts/metadata.json",
                    ],
                }
            ],
        },
        "metrics": [
            {
                "id": "mean_score",
                "extractor": "csv",
                "path": "{run_dir}/artifacts/metrics.csv",
                "column": "score",
                "select": "last",
                "expected": 3.0,
                "atol": 0.0,
                "rtol": 0.0,
            }
        ],
    }
    path = project / "reprotrace.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


def test_tiny_run_creates_passing_bundle(tmp_path: Path) -> None:
    manifest = make_tiny_project(tmp_path)
    run_dir, verification = run_manifest(manifest)

    assert verification["passed"] is True
    assert read_json(run_dir / "metrics.json")[0]["actual"] == 3.0
    assert (run_dir / "commands.jsonl").is_file()
    assert (run_dir / "report.md").is_file()
    assert "Decision:** `passed`" in (run_dir / "report.md").read_text(encoding="utf-8")


def test_tampered_artifact_fails_verification(tmp_path: Path) -> None:
    manifest = make_tiny_project(tmp_path)
    run_dir, _ = run_manifest(manifest)
    (run_dir / "artifacts" / "metrics.csv").write_text("score\n999\n", encoding="utf-8")

    verification = verify_bundle(run_dir)

    assert verification["passed"] is False
    assert any(check["category"] == "artifact" and not check["passed"] for check in verification["checks"])


def test_dry_run_never_executes_command(tmp_path: Path) -> None:
    project = tmp_path / "dry"
    project.mkdir()
    marker = project / "executed.txt"
    manifest = {
        "schema_version": 0,
        "project": {"name": "dry", "root": "."},
        "run": {
            "output_root": ".evidence",
            "steps": [
                {
                    "id": "must-not-run",
                    "argv": [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')"],
                }
            ],
        },
    }
    path = project / "reprotrace.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    run_dir, verification = run_manifest(path, dry_run=True)

    assert not marker.exists()
    assert verification["status"] == "planned"
    assert verification["preflight_passed"] is True
    assert read_json(run_dir / "commands.json")[0]["status"] == "planned"


def test_dry_run_records_expanded_step_environment(tmp_path: Path) -> None:
    manifest = make_tiny_project(tmp_path)
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["run"]["steps"][0]["env"] = {"PYTHONPATH": "{project_root}/src"}
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    run_dir, verification = run_manifest(manifest, dry_run=True)

    command = read_json(run_dir / "commands.json")[0]
    assert verification["preflight_passed"] is True
    assert command["environment_overrides"]["PYTHONPATH"] == f"{manifest.parent.resolve()}/src"


def test_source_is_captured_before_evidence_directory_creation(tmp_path: Path, monkeypatch) -> None:
    manifest = make_tiny_project(tmp_path)
    observed: dict[str, bool] = {}

    def capture_before_output(loaded_manifest):
        observed["output_root_exists"] = loaded_manifest.output_root.exists()
        return SourceCapture(
            record={"available": False, "root": str(loaded_manifest.project_root)},
            files={},
            worktree_root=None,
        )

    monkeypatch.setattr("reprotrace.runner.capture_source", capture_before_output)

    run_manifest(manifest, dry_run=True)

    assert observed == {"output_root_exists": False}


def test_external_manifest_output_keeps_audited_checkout_clean(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "tracked.txt").write_text("source\n", encoding="utf-8")
    initialize_repository(checkout, "tracked.txt")

    adapter = tmp_path / "adapter"
    adapter.mkdir()
    manifest = adapter / "reprotrace.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 0,
                "project": {"name": "external", "root": ".", "allow_dirty": False},
                "run": {
                    "output_root": ".evidence",
                    "steps": [{"id": "noop", "argv": [sys.executable, "-V"]}],
                },
            }
        ),
        encoding="utf-8",
    )

    run_dir, verification = run_manifest(manifest, dry_run=True, project_root=checkout)

    assert run_dir.parent == (adapter / ".evidence").resolve()
    assert read_json(run_dir / "source.json")["dirty"] is False
    assert (run_dir / "source.patch").read_bytes() == b""
    assert (run_dir / "source.status").read_bytes() == b""
    assert verification["preflight_passed"] is True
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""


def test_rejects_unignored_output_inside_audited_checkout(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    manifest = checkout / "reprotrace.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 0,
                "project": {"name": "unsafe-output", "root": "."},
                "run": {
                    "output_root": ".evidence",
                    "steps": [{"id": "noop", "argv": [sys.executable, "-V"]}],
                },
            }
        ),
        encoding="utf-8",
    )
    initialize_repository(checkout, "reprotrace.yaml")

    with pytest.raises(ConfigError, match="inside the audited Git worktree"):
        run_manifest(manifest, dry_run=True)

    assert not (checkout / ".evidence").exists()


def test_diff_reports_seed_and_artifact_changes(tmp_path: Path) -> None:
    manifest = make_tiny_project(tmp_path)
    left, _ = run_manifest(manifest, seed=1)
    right, _ = run_manifest(manifest, seed=2)

    result = compare_bundles(left, right)
    fields = {difference["field"] for difference in result["differences"]}

    assert result["identical"] is False
    assert "seed" in fields
    assert "artifacts" in fields


def test_diff_ignores_run_directory_for_equal_runs(tmp_path: Path) -> None:
    manifest = make_tiny_project(tmp_path)
    left, _ = run_manifest(manifest, seed=7)
    right, _ = run_manifest(manifest, seed=7)

    result = compare_bundles(left, right)

    assert result["identical"] is True


def test_log_regex_metric_uses_captured_stdout(tmp_path: Path) -> None:
    project = tmp_path / "regex"
    project.mkdir()
    manifest = {
        "schema_version": 0,
        "project": {"name": "regex", "root": "."},
        "run": {
            "output_root": ".evidence",
            "steps": [
                {
                    "id": "evaluate",
                    "argv": [sys.executable, "-c", "print('accuracy=91.25')"],
                }
            ],
        },
        "metrics": [
            {
                "id": "accuracy",
                "extractor": "log_regex",
                "path": "{run_dir}/logs/evaluate.stdout.log",
                "pattern": r"accuracy=([0-9.]+)",
                "expected": 91.25,
            }
        ],
    }
    path = project / "reprotrace.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    run_dir, verification = run_manifest(path)

    assert verification["passed"] is True
    assert read_json(run_dir / "metrics.json")[0]["actual"] == 91.25


def test_dry_run_detects_wrong_source_ref(tmp_path: Path) -> None:
    project = tmp_path / "repository"
    project.mkdir()
    path = project / "reprotrace.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 0,
                "project": {"name": "wrong-ref", "root": ".", "ref": "0" * 40},
                "run": {"output_root": ".evidence", "steps": [{"id": "noop", "argv": [sys.executable, "-V"]}]},
            }
        ),
        encoding="utf-8",
    )
    (project / ".gitignore").write_text(".evidence/\n", encoding="utf-8")
    initialize_repository(project, "reprotrace.yaml", ".gitignore")

    _, verification = run_manifest(path, dry_run=True)

    assert verification["status"] == "preflight_failed"
    assert verification["preflight_passed"] is False
    assert any(check["id"] == "source:ref" and not check["passed"] for check in verification["checks"])
