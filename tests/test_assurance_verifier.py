from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from reprotrace.evidence import read_evidence_index, write_evidence_index
from reprotrace.io import read_json, sha256_file, write_json
from reprotrace.runner import run_manifest
from reprotrace.verifier import verify_bundle


def make_manifest(
    project: Path,
    *,
    expected: float = 3.0,
    atol: float = 0.0,
    metrics: bool = True,
    exit_code: int = 0,
    extra_artifact: bool = False,
) -> Path:
    project.mkdir(parents=True, exist_ok=True)
    (project / "input.txt").write_text("producer input\n", encoding="utf-8")
    if exit_code:
        command = f"raise SystemExit({exit_code})"
    else:
        statements = [
            "import os; from pathlib import Path; "
            "p=Path(os.environ['REPROTRACE_RUN_DIR'])/'artifacts'/'metrics.csv'; "
            "p.write_text('score\\n3.0\\n', encoding='utf-8')"
        ]
        if extra_artifact:
            statements.append(
                "q=Path(os.environ['REPROTRACE_RUN_DIR'])/'artifacts'/'notes.txt'; "
                "q.write_text('notes\\n', encoding='utf-8')"
            )
        command = "; ".join(statements)
    artifact_paths = []
    if metrics and not exit_code:
        artifact_paths.append("{run_dir}/artifacts/metrics.csv")
    if extra_artifact and not exit_code:
        artifact_paths.append("{run_dir}/artifacts/notes.txt")
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
                    **({"artifacts": artifact_paths} if artifact_paths else {}),
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


def assert_declaration_closure_failure(
    verification: dict[str, object], check_id: str
) -> None:
    assert check_by_id(verification, check_id)["passed"] is False
    assert verification["checks_passed"] is False
    assert verification["verification_status"] == "incomplete"
    assert verification["assurance_level"] == "recorded"
    assert verification["evidence_root_sha256"] is None


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


def test_v4_raw_metric_tamper_fails_derivation_after_integrity_reconstruction(
    tmp_path: Path,
) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    raw = run_dir / "artifacts" / "metrics.csv"
    raw.write_text("score\n4.0\n", encoding="utf-8")
    size_bytes = raw.stat().st_size
    digest = sha256_file(raw)
    sources = read_json(run_dir / "metric_sources.json")
    sources["metrics"][0]["sources"][0]["size_bytes"] = size_bytes
    sources["metrics"][0]["sources"][0]["sha256"] = digest
    write_json(run_dir / "metric_sources.json", sources)
    artifacts = read_json(run_dir / "artifacts.json")
    artifacts[0]["matches"][0]["size_bytes"] = size_bytes
    artifacts[0]["matches"][0]["sha256"] = digest
    write_json(run_dir / "artifacts.json", artifacts)
    refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)
    derived = check_by_id(verification, "metric:score:derived-match")

    assert check_by_id(verification, "bundle:index")["passed"] is True
    assert check_by_id(verification, "bundle:closure")["passed"] is True
    assert all(
        check["passed"]
        for check in verification["contract_checks"]
        if check["kind"] == "integrity"
    )
    assert derived["passed"] is False
    assert derived["recomputed_actual"] == 4.0
    assert derived["recorded_actual"] == 3.0
    assert verification["assurance_level"] == "bundle_integrity_checked"
    assert verification["result_status"] == "indeterminate"


def test_d1_missing_manifest_input_record_cannot_be_reindexed_away(tmp_path: Path) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    write_json(run_dir / "inputs.json", [])
    refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert check_by_id(verification, "bundle:closure")["passed"] is True
    assert_declaration_closure_failure(verification, "input:declaration-closure")


def test_d2_extra_input_record_fails_manifest_declaration_closure(tmp_path: Path) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    inputs = read_json(run_dir / "inputs.json")
    extra = dict(inputs[0])
    extra["id"] = "unexpected"
    inputs.append(extra)
    write_json(run_dir / "inputs.json", inputs)
    refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert_declaration_closure_failure(verification, "input:declaration-closure")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "renamed"),
        ("declared_path", "other.txt"),
        ("input_kind", "checkpoint"),
        ("required", False),
    ],
)
def test_d3_modified_input_declaration_fails_manifest_closure(
    tmp_path: Path, field: str, value: object
) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    inputs = read_json(run_dir / "inputs.json")
    inputs[0][field] = value
    write_json(run_dir / "inputs.json", inputs)
    refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert_declaration_closure_failure(verification, "input:declaration-closure")


def test_d4_missing_nonmetric_artifact_declaration_cannot_be_reindexed_away(
    tmp_path: Path,
) -> None:
    run_dir, _ = run_manifest(
        make_manifest(tmp_path / "project", extra_artifact=True)
    )
    artifacts = read_json(run_dir / "artifacts.json")
    artifacts = [
        declaration
        for declaration in artifacts
        if declaration["declared_path"] != "{run_dir}/artifacts/notes.txt"
    ]
    write_json(run_dir / "artifacts.json", artifacts)
    index = read_evidence_index(run_dir)
    declarations = [
        {"path": entry["path"], "roles": entry["roles"]}
        for entry in index["entries"]
        if entry["path"] != "artifacts/notes.txt"
    ]
    write_evidence_index(run_dir, declarations)

    verification = verify_bundle(run_dir, write=False)

    assert check_by_id(verification, "bundle:closure")["passed"] is True
    assert_declaration_closure_failure(verification, "artifact:declaration-closure")


def test_d5_extra_artifact_declaration_fails_manifest_closure(tmp_path: Path) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    artifacts = read_json(run_dir / "artifacts.json")
    artifacts.append(
        {
            "step_id": "produce",
            "declared_path": "unexpected.txt",
            "resolved_pattern": str(tmp_path / "unexpected.txt"),
            "matches": [],
        }
    )
    write_json(run_dir / "artifacts.json", artifacts)
    refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert_declaration_closure_failure(verification, "artifact:declaration-closure")


@pytest.mark.parametrize(
    ("field", "value"),
    [("step_id", "other-step"), ("declared_path", "{run_dir}/artifacts/renamed.txt")],
)
def test_d6_modified_artifact_step_or_path_fails_manifest_closure(
    tmp_path: Path, field: str, value: str
) -> None:
    manifest = make_manifest(tmp_path / "project", extra_artifact=True)
    if field == "step_id":
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        data["run"]["steps"].append(
            {"id": "other-step", "argv": [sys.executable, "-c", "pass"]}
        )
        manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    run_dir, _ = run_manifest(manifest)
    artifacts = read_json(run_dir / "artifacts.json")
    artifacts[-1][field] = value
    write_json(run_dir / "artifacts.json", artifacts)
    refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert_declaration_closure_failure(verification, "artifact:declaration-closure")


@pytest.mark.parametrize("record_type", ["input", "artifact"])
def test_d7_duplicate_declaration_record_fails_manifest_closure(
    tmp_path: Path, record_type: str
) -> None:
    run_dir, _ = run_manifest(
        make_manifest(tmp_path / "project", extra_artifact=True)
    )
    filename = f"{record_type}s.json"
    records = read_json(run_dir / filename)
    records.append(dict(records[-1]))
    write_json(run_dir / filename, records)
    refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert_declaration_closure_failure(
        verification, f"{record_type}:declaration-closure"
    )


def test_zero_artifact_matches_is_not_a_canonical_schema_failure(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path / "project", metrics=False)
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["run"]["steps"][0]["artifacts"] = [
        "{run_dir}/artifacts/optional.txt"
    ]
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    _, verification = run_manifest(manifest)

    assert check_by_id(verification, "artifact:declaration-closure")["passed"] is True
    assert check_by_id(verification, "artifact:bundle-scope-closure")["passed"] is True
    assert verification["verification_status"] == "complete"
    assert verification["checks_passed"] is True
    assert verification["assurance_level"] == "bundle_integrity_checked"
    assert verification["passed"] is False


def test_bundle_artifact_cannot_be_reclassified_as_external(tmp_path: Path) -> None:
    run_dir, _ = run_manifest(
        make_manifest(tmp_path / "project", extra_artifact=True)
    )
    artifacts = read_json(run_dir / "artifacts.json")
    notes = artifacts[-1]["matches"][0]
    notes["path_scope"] = "external"
    notes["evidence_path"] = None
    write_json(run_dir / "artifacts.json", artifacts)
    index = read_evidence_index(run_dir)
    declarations = []
    for entry in index["entries"]:
        roles = list(entry["roles"])
        if entry["path"] == "artifacts/notes.txt":
            roles = [role for role in roles if role != "artifact"]
        if roles:
            declarations.append({"path": entry["path"], "roles": roles})
    write_evidence_index(run_dir, declarations)

    verification = verify_bundle(run_dir, write=False)

    assert check_by_id(verification, "bundle:closure")["passed"] is True
    assert_declaration_closure_failure(verification, "artifact:bundle-scope-closure")


def test_extra_index_entry_fails_exact_evidence_closure(tmp_path: Path) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    (run_dir / "orphan.txt").write_text("not referenced\n", encoding="utf-8")
    index = read_evidence_index(run_dir)
    declarations = [
        {"path": entry["path"], "roles": entry["roles"]}
        for entry in index["entries"]
    ]
    declarations.append({"path": "orphan.txt", "roles": ["record"]})
    write_evidence_index(run_dir, declarations)

    verification = verify_bundle(run_dir, write=False)

    assert check_by_id(verification, "bundle:index")["passed"] is True
    assert check_by_id(verification, "bundle:closure")["passed"] is False
    assert verification["assurance_level"] == "recorded"


def test_index_role_mismatch_fails_exact_evidence_closure(tmp_path: Path) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    index = read_evidence_index(run_dir)
    declarations = [
        {
            "path": entry["path"],
            "roles": (
                ["unexpected"]
                if entry["path"] == "environment.json"
                else entry["roles"]
            ),
        }
        for entry in index["entries"]
    ]
    write_evidence_index(run_dir, declarations)

    verification = verify_bundle(run_dir, write=False)

    assert check_by_id(verification, "bundle:index")["passed"] is True
    closure = check_by_id(verification, "bundle:closure")
    assert closure["passed"] is False
    assert closure["role_mismatches"] == ["environment.json"]
    assert verification["assurance_level"] == "recorded"


@pytest.mark.parametrize("action", ["missing", "modified"])
def test_a1_a2_forged_success_or_tampered_command_log_cannot_raise_assurance(
    tmp_path: Path, action: str
) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project", metrics=False))
    log = run_dir / "logs" / "produce.stdout.log"
    if action == "missing":
        log.unlink()
    else:
        log.write_text("tampered\n", encoding="utf-8")

    verification = verify_bundle(run_dir, write=False)

    assert check_by_id(verification, "closure:command-log:produce:stdout")["passed"] is False
    assert verification["execution_record_status"] == "recorded_success"
    assert verification["assurance_level"] == "recorded"
    assert verification["verification_status"] == "incomplete"
    assert verification["checks_passed"] is False
    assert verification["evidence_root_sha256"] is None
    assert verification["not_established"] == {
        "execution_authenticity": "not_established",
        "independent_replay": "not_performed",
        "scientific_reproduction": "not_established",
    }


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
    trap_dir = tmp_path / "origin-trap"
    trap_dir.mkdir()
    trap = trap_dir / "trap.txt"
    trap.write_text("score\n999.0\n", encoding="utf-8")

    run = read_json(run_dir / "run.json")
    run["project_root"] = str(trap_dir)
    write_json(run_dir / "run.json", run)
    resolved_manifest_path = run_dir / "manifest.resolved.yaml"
    resolved_manifest = yaml.safe_load(resolved_manifest_path.read_text(encoding="utf-8"))
    resolved_manifest["project"]["root"] = str(trap_dir)
    resolved_manifest["run"]["output_root"] = str(trap_dir)
    resolved_manifest_path.write_text(
        yaml.safe_dump(resolved_manifest, sort_keys=False), encoding="utf-8"
    )
    inputs = read_json(run_dir / "inputs.json")
    inputs[0]["path"] = str(trap)
    write_json(run_dir / "inputs.json", inputs)
    commands = read_json(run_dir / "commands.json")
    commands[0]["cwd"] = str(trap_dir)
    commands[0]["stdout_path"] = str(trap)
    commands[0]["stderr_path"] = str(trap)
    write_json(run_dir / "commands.json", commands)
    artifacts = read_json(run_dir / "artifacts.json")
    artifacts[0]["resolved_pattern"] = str(trap)
    artifacts[0]["matches"][0]["path"] = str(trap)
    write_json(run_dir / "artifacts.json", artifacts)
    sources = read_json(run_dir / "metric_sources.json")
    sources["metrics"][0]["sources"][0]["origin_path"] = str(trap)
    write_json(run_dir / "metric_sources.json", sources)
    metrics = read_json(run_dir / "metrics.json")
    metrics[0]["source_paths"] = [str(trap)]
    write_json(run_dir / "metrics.json", metrics)
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


@pytest.mark.parametrize(
    ("field", "value"),
    [("expected", 4.0), ("atol", 1.0), ("rtol", 0.25)],
)
def test_a6_resolved_protocol_tamper_with_valid_rehash_fails_derived_consistency(
    tmp_path: Path, field: str, value: float
) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))
    resolved_path = run_dir / "manifest.resolved.yaml"
    resolved = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    resolved["metrics"][0][field] = value
    resolved_path.write_text(
        yaml.safe_dump(resolved, sort_keys=False), encoding="utf-8"
    )
    refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert check_by_id(verification, "bundle:index")["passed"] is True
    assert check_by_id(verification, "bundle:closure")["passed"] is True
    assert all(
        check["passed"]
        for check in verification["contract_checks"]
        if check["kind"] == "integrity"
    )
    assert check_by_id(verification, "metric:score:derived-match")["passed"] is False
    assert verification["verification_status"] == "incomplete"
    assert verification["checks_passed"] is False
    assert verification["assurance_level"] == "bundle_integrity_checked"
    assert verification["result_status"] == "indeterminate"
    assert verification["evidence_root_sha256"] is not None


def test_coherent_producer_forgery_is_an_explicit_undetectable_boundary(
    tmp_path: Path,
) -> None:
    run_dir, _ = run_manifest(make_manifest(tmp_path / "project"))

    raw = run_dir / "artifacts" / "metrics.csv"
    raw.write_text("score\n4.0\n", encoding="utf-8")
    size_bytes = raw.stat().st_size
    digest = sha256_file(raw)

    metric_sources = read_json(run_dir / "metric_sources.json")
    metric_source = metric_sources["metrics"][0]["sources"][0]
    metric_source.update(
        size_bytes=size_bytes,
        sha256=digest,
        origin_path="C:/forged/project/metrics.csv",
    )
    write_json(run_dir / "metric_sources.json", metric_sources)

    artifacts = read_json(run_dir / "artifacts.json")
    artifacts[0]["matches"][0].update(
        size_bytes=size_bytes,
        sha256=digest,
        path="C:/forged/project/metrics.csv",
    )
    write_json(run_dir / "artifacts.json", artifacts)

    metrics = read_json(run_dir / "metrics.json")
    metrics[0].update(
        actual=4.0,
        expected=4.0,
        absolute_error=0.0,
        passed=True,
        source_paths=["C:/forged/project/metrics.csv"],
    )
    write_json(run_dir / "metrics.json", metrics)

    resolved_path = run_dir / "manifest.resolved.yaml"
    resolved = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    resolved["project"]["root"] = "C:/forged/project"
    resolved["run"]["output_root"] = "C:/forged/output"
    resolved["metrics"][0]["expected"] = 4.0
    resolved_path.write_text(
        yaml.safe_dump(resolved, sort_keys=False), encoding="utf-8"
    )

    commands = read_json(run_dir / "commands.json")
    commands[0].update(
        cwd="C:/forged/project",
        started_at="forged-start",
        finished_at="forged-finish",
        elapsed_seconds=1.0,
        status="completed",
        return_code=0,
    )
    write_json(run_dir / "commands.json", commands)
    (run_dir / "commands.jsonl").write_text(
        "".join(json.dumps(command, sort_keys=True) + "\n" for command in commands),
        encoding="utf-8",
    )
    (run_dir / "logs" / "produce.stdout.log").write_text(
        "forged stdout\n", encoding="utf-8"
    )
    (run_dir / "logs" / "produce.stderr.log").write_text(
        "forged stderr\n", encoding="utf-8"
    )

    inputs = read_json(run_dir / "inputs.json")
    inputs[0]["path"] = "C:/forged/project/input.txt"
    write_json(run_dir / "inputs.json", inputs)
    environment = read_json(run_dir / "environment.json")
    environment["platform"] = "forged-platform"
    write_json(run_dir / "environment.json", environment)
    run = read_json(run_dir / "run.json")
    run["project_root"] = "C:/forged/project"
    write_json(run_dir / "run.json", run)
    refresh_index(run_dir)

    verification = verify_bundle(run_dir, write=False)

    assert verification["verification_status"] == "complete"
    assert verification["checks_passed"] is True
    assert verification["assurance_level"] == "metric_derivations_recomputed"
    assert verification["execution_record_status"] == "recorded_success"
    assert verification["result_status"] == "matched"
    assert verification["evidence_root_sha256"] is not None
    assert verification["not_established"] == {
        "execution_authenticity": "not_established",
        "independent_replay": "not_performed",
        "scientific_reproduction": "not_established",
    }


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
