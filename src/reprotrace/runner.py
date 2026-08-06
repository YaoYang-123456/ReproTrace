"""Experiment execution and evidence-bundle orchestration."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .capture import capture_artifacts, capture_environment, capture_inputs, capture_source
from .errors import ConfigError
from .io import utc_now, write_json, write_yaml
from .manifest import (
    LoadedManifest,
    load_manifest,
    redacted_environment,
    redacted_manifest,
    runtime_context,
    substitute,
)
from .metrics import extract_metrics
from .reporting import generate_report
from .verifier import verify_bundle


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(3)}"


def _resolved_command(
    manifest: LoadedManifest,
    step: dict[str, Any],
    context: dict[str, Any],
    run_dir: Path,
) -> tuple[list[str], Path, dict[str, str]]:
    argv = [substitute(value, context) for value in step["argv"]]
    cwd_value = substitute(step.get("cwd", str(manifest.project_root)), context)
    cwd = Path(cwd_value).expanduser()
    if not cwd.is_absolute():
        cwd = manifest.project_root / cwd
    cwd = cwd.resolve()
    if not cwd.is_dir():
        raise ConfigError(f"step {step['id']!r} working directory does not exist: {cwd}")
    overrides = {key: substitute(value, context) for key, value in step.get("env", {}).items()}
    overrides.update(
        {
            "REPROTRACE_RUN_DIR": str(run_dir),
            "REPROTRACE_PROJECT_ROOT": str(manifest.project_root),
            "REPROTRACE_STEP_ID": step["id"],
            "REPROTRACE_SEED": str(context["seed"]),
        }
    )
    return argv, cwd, overrides


def _write_commands(run_dir: Path, commands: list[dict[str, Any]]) -> None:
    write_json(run_dir / "commands.json", commands)
    with (run_dir / "commands.jsonl").open("w", encoding="utf-8") as handle:
        for command in commands:
            handle.write(json.dumps(command, sort_keys=True) + "\n")


def _planned_commands(
    manifest: LoadedManifest, context: dict[str, Any], run_dir: Path
) -> list[dict[str, Any]]:
    commands = []
    for step in manifest.data["run"]["steps"]:
        argv, cwd, overrides = _resolved_command(manifest, step, context, run_dir)
        commands.append(
            {
                "step_id": step["id"],
                "status": "planned",
                "requested_argv": step["argv"],
                "argv": argv,
                "cwd": str(cwd),
                "environment_overrides": redacted_environment(overrides),
                "timeout_seconds": step.get("timeout_seconds"),
                "stdout_path": str(run_dir / "logs" / f"{step['id']}.stdout.log"),
                "stderr_path": str(run_dir / "logs" / f"{step['id']}.stderr.log"),
            }
        )
    return commands


def _execute_commands(
    manifest: LoadedManifest, context: dict[str, Any], run_dir: Path
) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    for step in manifest.data["run"]["steps"]:
        argv, cwd, overrides = _resolved_command(manifest, step, context, run_dir)
        stdout_path = run_dir / "logs" / f"{step['id']}.stdout.log"
        stderr_path = run_dir / "logs" / f"{step['id']}.stderr.log"
        started_at = utc_now()
        started_clock = time.monotonic()
        status = "completed"
        return_code: int | None = None
        error: str | None = None
        environment = os.environ.copy()
        environment.update(overrides)
        try:
            with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
                process = subprocess.run(
                    argv,
                    cwd=cwd,
                    env=environment,
                    stdout=stdout,
                    stderr=stderr,
                    check=False,
                    timeout=step.get("timeout_seconds"),
                )
            return_code = process.returncode
            if return_code != 0:
                status = "failed"
        except subprocess.TimeoutExpired as exc:
            status = "timeout"
            error = str(exc)
        except OSError as exc:
            status = "launch_error"
            error = str(exc)
        record = {
            "step_id": step["id"],
            "status": status,
            "requested_argv": step["argv"],
            "argv": argv,
            "cwd": str(cwd),
            "environment_overrides": redacted_environment(overrides),
            "timeout_seconds": step.get("timeout_seconds"),
            "started_at": started_at,
            "finished_at": utc_now(),
            "elapsed_seconds": round(time.monotonic() - started_clock, 6),
            "return_code": return_code,
            "error": error,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
        commands.append(record)
        if status != "completed":
            break
    return commands


def run_manifest(
    manifest_path: str | Path,
    *,
    dry_run: bool = False,
    seed: int | None = None,
    project_root: str | Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    manifest = load_manifest(manifest_path, project_root=project_root)
    chosen_seed = int(manifest.data["run"].get("seed", 0) if seed is None else seed)
    run_id = _new_run_id()
    run_dir = manifest.output_root / run_id
    context = runtime_context(manifest, run_dir, chosen_seed)
    inputs = capture_inputs(manifest, context)
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "logs").mkdir()
    (run_dir / "artifacts").mkdir()
    run_record: dict[str, Any] = {
        "schema_version": 0,
        "run_id": run_id,
        "project_name": manifest.data["project"]["name"],
        "claim": manifest.data.get("claim", {}),
        "manifest_path": str(manifest.path),
        "project_root": str(manifest.project_root),
        "seed": chosen_seed,
        "dry_run": dry_run,
        "status": "preparing",
        "started_at": utc_now(),
        "finished_at": None,
    }
    write_json(run_dir / "run.json", run_record)
    write_yaml(run_dir / "manifest.resolved.yaml", redacted_manifest(manifest.data))
    write_json(run_dir / "source.json", capture_source(manifest))
    write_json(run_dir / "environment.json", capture_environment())
    write_json(run_dir / "inputs.json", inputs)

    if dry_run:
        commands = _planned_commands(manifest, context, run_dir)
        _write_commands(run_dir, commands)
        write_json(run_dir / "artifacts.json", [])
        write_json(run_dir / "metrics.json", [])
        run_record.update(status="planned", finished_at=utc_now())
        write_json(run_dir / "run.json", run_record)
        verification = verify_bundle(run_dir)
        generate_report(run_dir)
        return run_dir, verification

    commands = _execute_commands(manifest, context, run_dir)
    _write_commands(run_dir, commands)
    write_json(run_dir / "artifacts.json", capture_artifacts(manifest, context))
    metrics: list[dict[str, Any]] = []
    if commands and all(item["status"] == "completed" for item in commands):
        try:
            metrics = extract_metrics(manifest, context)
        except ConfigError as exc:
            run_record["evidence_error"] = str(exc)
    write_json(run_dir / "metrics.json", metrics)
    run_record.update(
        status="executed" if commands and all(item["status"] == "completed" for item in commands) else "execution_failed",
        finished_at=utc_now(),
    )
    write_json(run_dir / "run.json", run_record)
    verification = verify_bundle(run_dir)
    generate_report(run_dir)
    return run_dir, verification
