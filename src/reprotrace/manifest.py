"""Manifest loading, validation, and placeholder resolution."""

from __future__ import annotations

import copy
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .errors import ConfigError


SECRET_NAME = re.compile(r"(?:TOKEN|KEY|SECRET|PASSWORD)", re.IGNORECASE)
SUPPORTED_EXTRACTORS = {"csv", "log_regex"}
SUPPORTED_SELECTORS = {"last", "max", "min"}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class LoadedManifest:
    path: Path
    project_root: Path
    output_root: Path
    data: dict[str, Any]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def load_manifest(path: str | Path, project_root: str | Path | None = None) -> LoadedManifest:
    manifest_path = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read manifest {manifest_path}: {exc}") from exc
    _require(isinstance(raw, dict), "manifest must be a YAML mapping")
    validate_manifest(raw)

    data = copy.deepcopy(raw)
    if project_root is None:
        root_value = data["project"].get("root", ".")
        root = Path(root_value).expanduser()
        if not root.is_absolute():
            root = manifest_path.parent / root
    else:
        root = Path(project_root).expanduser()
    root = root.resolve()
    _require(root.is_dir(), f"project root does not exist or is not a directory: {root}")

    output_value = Path(data["run"].get("output_root", ".reprotrace/runs")).expanduser()
    output_root = output_value if output_value.is_absolute() else root / output_value
    output_root = output_root.resolve()

    data["project"]["root"] = str(root)
    data["run"]["output_root"] = str(output_root)
    return LoadedManifest(manifest_path, root, output_root, data)


def validate_manifest(data: Mapping[str, Any]) -> None:
    _require(data.get("schema_version") == 0, "schema_version must be 0")
    project = data.get("project")
    _require(isinstance(project, dict), "project must be a mapping")
    _require(isinstance(project.get("name"), str) and project["name"], "project.name is required")

    run = data.get("run")
    _require(isinstance(run, dict), "run must be a mapping")
    _require(isinstance(run.get("seed", 0), int), "run.seed must be an integer")
    _require(isinstance(run.get("steps"), list) and run["steps"], "run.steps must be a non-empty list")
    step_ids: set[str] = set()
    for index, step in enumerate(run["steps"]):
        prefix = f"run.steps[{index}]"
        _require(isinstance(step, dict), f"{prefix} must be a mapping")
        step_id = step.get("id")
        _require(isinstance(step_id, str) and step_id, f"{prefix}.id is required")
        _require(SAFE_ID.fullmatch(step_id) is not None, f"{prefix}.id contains unsafe characters")
        _require(step_id not in step_ids, f"duplicate step id: {step_id}")
        step_ids.add(step_id)
        argv = step.get("argv")
        _require(
            isinstance(argv, list) and argv and all(isinstance(item, str) for item in argv),
            f"{prefix}.argv must be a non-empty list of strings",
        )
        timeout = step.get("timeout_seconds")
        _require(timeout is None or (isinstance(timeout, (int, float)) and timeout > 0), f"{prefix}.timeout_seconds must be positive")
        env = step.get("env", {})
        _require(isinstance(env, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()), f"{prefix}.env must map strings to strings")
        artifacts = step.get("artifacts", [])
        _require(isinstance(artifacts, list) and all(isinstance(item, str) for item in artifacts), f"{prefix}.artifacts must be a list of paths")
        _require(isinstance(step.get("cwd", "."), str), f"{prefix}.cwd must be a string")

    inputs = data.get("inputs", [])
    _require(isinstance(inputs, list), "inputs must be a list")
    input_ids: set[str] = set()
    for index, item in enumerate(inputs):
        _require(isinstance(item, dict), f"inputs[{index}] must be a mapping")
        input_id = item.get("id")
        _require(isinstance(input_id, str) and input_id, f"inputs[{index}].id is required")
        _require(SAFE_ID.fullmatch(input_id) is not None, f"inputs[{index}].id contains unsafe characters")
        _require(input_id not in input_ids, f"duplicate input id: {input_id}")
        input_ids.add(input_id)
        _require(isinstance(item.get("path"), str) and item["path"], f"inputs[{index}].path is required")
        _require(isinstance(item.get("required", True), bool), f"inputs[{index}].required must be boolean")

    metrics = data.get("metrics", [])
    _require(isinstance(metrics, list), "metrics must be a list")
    metric_ids: set[str] = set()
    for index, metric in enumerate(metrics):
        prefix = f"metrics[{index}]"
        _require(isinstance(metric, dict), f"{prefix} must be a mapping")
        metric_id = metric.get("id")
        _require(isinstance(metric_id, str) and metric_id, f"{prefix}.id is required")
        _require(SAFE_ID.fullmatch(metric_id) is not None, f"{prefix}.id contains unsafe characters")
        _require(metric_id not in metric_ids, f"duplicate metric id: {metric_id}")
        metric_ids.add(metric_id)
        _require(metric.get("extractor") in SUPPORTED_EXTRACTORS, f"{prefix}.extractor must be csv or log_regex")
        _require(isinstance(metric.get("path"), str) and metric["path"], f"{prefix}.path is required")
        _require(metric.get("select", "last") in SUPPORTED_SELECTORS, f"{prefix}.select must be last, max, or min")
        _require(isinstance(metric.get("expected"), (int, float)), f"{prefix}.expected must be numeric")
        _require(
            isinstance(metric.get("atol", 0.0), (int, float)) and metric.get("atol", 0.0) >= 0,
            f"{prefix}.atol must be non-negative",
        )
        _require(
            isinstance(metric.get("rtol", 0.0), (int, float)) and metric.get("rtol", 0.0) >= 0,
            f"{prefix}.rtol must be non-negative",
        )
        if metric["extractor"] == "csv":
            _require(isinstance(metric.get("column"), str), f"{prefix}.column is required for csv")
        else:
            _require(isinstance(metric.get("pattern"), str), f"{prefix}.pattern is required for log_regex")


def substitute(value: str, context: Mapping[str, Any]) -> str:
    result = value
    for name, replacement in context.items():
        result = result.replace("{" + name + "}", str(replacement))
    unknown = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", result)
    if unknown:
        raise ConfigError(f"unknown placeholder(s) in {value!r}: {', '.join(sorted(set(unknown)))}")
    return result


def runtime_context(manifest: LoadedManifest, run_dir: Path, seed: int) -> dict[str, Any]:
    return {
        "python": sys.executable,
        "project_root": manifest.project_root,
        "run_dir": run_dir,
        "seed": seed,
    }


def redacted_manifest(data: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(data)
    for step in result.get("run", {}).get("steps", []):
        for key in list(step.get("env", {})):
            if SECRET_NAME.search(key):
                step["env"][key] = "<redacted>"
    return result


def redacted_environment(values: Mapping[str, str]) -> dict[str, str]:
    return {key: ("<redacted>" if SECRET_NAME.search(key) else value) for key, value in values.items()}
