from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml

from reprotrace.evidence import read_evidence_index, write_evidence_index
from reprotrace.io import read_json, write_json
from reprotrace.runner import run_manifest
from reprotrace.verifier import verify_bundle


def make_manifest(
    project: Path,
    *,
    expected: float = 3.0,
    atol: float = 0.0,
    metrics: bool = True,
    exit_code: int = 0,
) -> Path:
    project.mkdir(parents=True, exist_ok=True)
    (project / "input.txt").write_text("producer input\n", encoding="utf-8")
    if exit_code:
        command = f"raise SystemExit({exit_code})"
    else:
        command = (
            "import os; from pathlib import Path; "
            "p=Path(os.environ['REPROTRACE_RUN_DIR'])/'artifacts'/'metrics.csv'; "
            "p.write_text('score\\n3.0\\n', encoding='utf-8')"
        )
    manifest: dict[str, object] = {
        "schema_version": 0,
        "project": {"name": "stage-four", "root": "."},
        "inputs": [
            {"id": "input", "kind": "dataset", "path": "input.txt", "required": True}
        ],
        "run": {
            "output_root": ".evidence",
            "steps": [
                {
                    "id": "produce",
                    "argv": [sys.executable, "-c", command],
                    **(
                        {"artifacts": ["{run_dir}/artifacts/metrics.csv"]}
                        if metrics and not exit_code
                        else {}
                    ),
                }
            ],
        },
    }
    if metrics:
        manifest["metrics"] = [
            {
                "id": "score",
                "extractor": "csv",
                "path": "{run_dir}/artifacts/metrics.csv",
                "column": "score",
                "select": "last",
                "expected": expected,
                "atol": atol,
                "rtol": 0.0,
            }
        ]
    path = project / "reprotrace.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


def refresh_index(run_dir: Path) -> str:
    index = read_evidence_index(run_dir)
    _, root = write_evidence_index(
        run_dir,
        [{"path": entry["path"], "roles": entry["roles"]} for entry in index["entries"]],
    )
    return root


def check_by_id(verification: dict[str, object], check_id: str) -> dict[str, object]:
    return next(
        check
        for check in verification["checks"]  # type: ignore[index]
        if check["id"] == check_id
    )


def test_v1_normal_schema_one_bundle_recomputes_metric(tmp_path: Path) -> None:
    run_dir, verification = run_manifest(make_manifest(tmp_path / "project"))

    run = read_json(run_dir / "run.json")
    index = read_evidence_index(run_dir)
    entries = {entry["path"]: entry for entry in index["entries"]}
    assert run["schema_version"] == 1
    assert verification["verification_status"] == "complete"
    assert verification["checks_passed"] is True
    assert verification["assurance_level"] == "metric_derivations_recomputed"
    assert verification["result_status"] == "matched"
    assert verification["evidence_root_sha256"]
    assert entries["logs/produce.stdout.log"]["roles"] == ["command_log"]
    assert entries["logs/produce.stderr.log"]["roles"] == ["command_log"]
    assert entries["artifacts/metrics.csv"]["roles"] == ["artifact", "metric_source"]
    assert "evidence.index.json" not in entries
    assert "verification.json" not in entries
    assert "report.md" not in entries


def test_v2_expectation_miss_does_not_reduce_assurance(tmp_path: Path) -> None:
    _, verification = run_manifest(make_manifest(tmp_path / "project", expected=4.0))

    assert verification["verification_status"] == "complete"
    assert verification["checks_passed"] is True
    assert verification["assurance_level"] == "metric_derivations_recomputed"
    assert verification["result_status"] == "not_matched"
    assert verification["passed"] is False
    assert check_by_id(verification, "metric:score:expectation")["passed"] is False


def test_v3_derived_metric_tamper_survives_index_but_fails_derivation(tmp_path: Path) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    metrics = read_json(run_dir / "metrics.json")
    metrics[0]["actual"] = 9.0
    metrics[0]["absolute_error"] = 6.0
    metrics[0]["passed"] = False
    write_json(run_dir / "metrics.json", metrics)
    refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert check_by_id(verification, "bundle:index")["passed"] is True
    assert check_by_id(verification, "metric:score:derived-match")["passed"] is False
    assert verification["assurance_level"] == "bundle_integrity_checked"
    assert verification["result_status"] == "indeterminate"
    assert verification["checks_passed"] is False


def test_v4_raw_metric_tamper_is_recomputed_after_index_refresh(tmp_path: Path) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    (run_dir / "artifacts" / "metrics.csv").write_text("score\n4.0\n", encoding="utf-8")
    refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)
    derived = check_by_id(verification, "metric:score:derived-match")

    assert check_by_id(verification, "bundle:index")["passed"] is True
    assert derived["passed"] is False
    assert derived["recomputed_actual"] == 4.0
    assert derived["recorded_actual"] == 3.0
    assert verification["result_status"] == "indeterminate"


@pytest.mark.parametrize("action", ["missing", "modified"])
def test_v5_command_log_integrity_is_required(tmp_path: Path, action: str) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project", metrics=False))
    log = run_dir / "logs" / "produce.stdout.log"
    if action == "missing":
        log.unlink()
    else:
        log.write_text("tampered\n", encoding="utf-8")

    verification = verify_bundle(run_dir, write=False)

    assert check_by_id(verification, "closure:command-log:produce:stdout")["passed"] is False
    assert verification["assurance_level"] == "recorded"
    assert verification["verification_status"] == "incomplete"
    assert verification["evidence_root_sha256"] is None


def test_v6_zero_metric_bundle_has_integrity_assurance(tmp_path: Path) -> None:
    _, verification = run_manifest(make_manifest(tmp_path / "project", metrics=False))

    assert verification["assurance_level"] == "bundle_integrity_checked"
    assert verification["result_status"] == "not_evaluated"
    assert verification["checks_passed"] is True
    assert verification["coverage"]["metric_sources"] == {
        "total": 0,
        "recorded": 0,
        "captured": 0,
        "source_files_captured": 0,
    }


def test_v7_dry_run_declared_metric_is_planning_integrity_only(tmp_path: Path) -> None:
    run_dir, verification = run_manifest(
        make_manifest(tmp_path / "project"),
        dry_run=True,
    )

    assert read_json(run_dir / "metric_sources.json") == {"schema_version": 1, "metrics": []}
    assert verification["execution_record_status"] == "not_run"
    assert verification["assurance_level"] == "bundle_integrity_checked"
    assert verification["result_status"] == "not_evaluated"
    assert verification["coverage"]["metric_sources"] == {
        "total": 1,
        "recorded": 0,
        "captured": 0,
        "source_files_captured": 0,
    }
    assert not any(entry["path"].startswith("logs/") for entry in read_evidence_index(run_dir)["entries"])


def test_v8_recorded_command_failure_is_orthogonal_to_integrity(tmp_path: Path) -> None:
    _, verification = run_manifest(make_manifest(tmp_path / "project", exit_code=7))

    assert verification["execution_record_status"] == "recorded_failure"
    assert verification["verification_status"] == "complete"
    assert verification["checks_passed"] is True
    assert verification["assurance_level"] == "bundle_integrity_checked"
    assert verification["result_status"] == "indeterminate"
    assert verification["coverage"]["metric_sources"]["total"] == 1
    assert verification["coverage"]["metric_sources"]["captured"] == 0
    assert verification["passed"] is False


def test_v9_relocated_bundle_verifies_after_origin_deletion(tmp_path: Path) -> None:
    project = tmp_path / "producer" / "project"
    original, before = run_manifest(make_manifest(project))
    relocated = tmp_path / "consumer" / "bundle"
    relocated.parent.mkdir()
    shutil.copytree(original, relocated)
    shutil.rmtree(tmp_path / "producer")

    after = verify_bundle(relocated, write=False)

    assert not original.exists()
    assert after["verification_status"] == "complete"
    assert after["evidence_root_sha256"] == before["evidence_root_sha256"]
    assert after["assurance_level"] == before["assurance_level"]
    assert after["result_status"] == before["result_status"]


def test_v10_origin_metadata_is_not_used_for_schema_one_io(tmp_path: Path) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    trap = tmp_path / "trap.txt"
    trap.write_text("score\n999.0\n", encoding="utf-8")

    inputs = read_json(run_dir / "inputs.json")
    inputs[0]["path"] = str(trap)
    write_json(run_dir / "inputs.json", inputs)
    commands = read_json(run_dir / "commands.json")
    commands[0]["stdout_path"] = str(trap)
    commands[0]["stderr_path"] = str(trap)
    write_json(run_dir / "commands.json", commands)
    artifacts = read_json(run_dir / "artifacts.json")
    artifacts[0]["matches"][0]["path"] = str(trap)
    write_json(run_dir / "artifacts.json", artifacts)
    sources = read_json(run_dir / "metric_sources.json")
    sources["metrics"][0]["sources"][0]["origin_path"] = str(trap)
    write_json(run_dir / "metric_sources.json", sources)
    refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert verification["verification_status"] == "complete"
    assert verification["assurance_level"] == "metric_derivations_recomputed"
    assert verification["result_status"] == "matched"


def test_v11_derived_actual_uses_strict_not_scientific_tolerance(tmp_path: Path) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project", atol=1.0))
    metrics = read_json(run_dir / "metrics.json")
    metrics[0]["actual"] = 3.5
    metrics[0]["absolute_error"] = 0.5
    metrics[0]["passed"] = True
    write_json(run_dir / "metrics.json", metrics)
    refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert check_by_id(verification, "metric:score:derived-match")["passed"] is False
    assert verification["assurance_level"] == "bundle_integrity_checked"
    assert verification["result_status"] == "indeterminate"


def test_derived_sample_count_rejects_json_boolean_alias(tmp_path: Path) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    metrics = read_json(run_dir / "metrics.json")
    assert metrics[0]["sample_count"] == 1
    metrics[0]["sample_count"] = True
    write_json(run_dir / "metrics.json", metrics)
    refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert check_by_id(verification, "metric:score:derived-match")["passed"] is False


@pytest.mark.parametrize("damage", ["missing", "extra"])
def test_v12_metric_id_closure_rejects_missing_or_extra(tmp_path: Path, damage: str) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    metrics = read_json(run_dir / "metrics.json")
    if damage == "missing":
        metrics = []
    else:
        extra = dict(metrics[0])
        extra["id"] = "unexpected"
        metrics.append(extra)
    write_json(run_dir / "metrics.json", metrics)
    refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert check_by_id(verification, "metric:id-closure")["passed"] is False
    assert verification["checks_passed"] is False
    assert verification["assurance_level"] == "bundle_integrity_checked"
    assert verification["result_status"] == "indeterminate"


def test_metric_source_id_closure_is_required_for_bundle_integrity(tmp_path: Path) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    sources = read_json(run_dir / "metric_sources.json")
    sources["metrics"] = []
    write_json(run_dir / "metric_sources.json", sources)
    index = read_evidence_index(run_dir)
    declarations = []
    for entry in index["entries"]:
        roles = [role for role in entry["roles"] if role != "metric_source"]
        if roles:
            declarations.append({"path": entry["path"], "roles": roles})
    write_evidence_index(run_dir, declarations)

    verification = verify_bundle(run_dir, write=False)

    assert check_by_id(verification, "metric:source-closure")["passed"] is False
    assert verification["assurance_level"] == "recorded"
    assert verification["result_status"] == "indeterminate"


def test_legacy_schema_zero_never_dereferences_input_or_artifact_origins(tmp_path: Path) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    run = read_json(run_dir / "run.json")
    run["schema_version"] = 0
    write_json(run_dir / "run.json", run)
    missing = tmp_path / "does-not-exist"
    inputs = read_json(run_dir / "inputs.json")
    inputs[0]["path"] = str(missing)
    write_json(run_dir / "inputs.json", inputs)
    artifacts = read_json(run_dir / "artifacts.json")
    artifacts[0]["matches"][0]["path"] = str(missing)
    write_json(run_dir / "artifacts.json", artifacts)

    verification = verify_bundle(run_dir, write=False)

    assert verification["assurance_level"] == "recorded"
    assert verification["result_status"] == "not_evaluated"
