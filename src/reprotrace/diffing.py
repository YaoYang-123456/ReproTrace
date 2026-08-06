"""Focused comparison of two evidence bundles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import ConfigError
from .io import read_json


def _load(directory: Path, name: str) -> Any:
    path = directory / f"{name}.json"
    if not path.is_file():
        raise ConfigError(f"invalid evidence bundle {directory}; missing {path.name}")
    return read_json(path)


def _by_id(items: list[dict[str, Any]], key: str = "id") -> dict[str, dict[str, Any]]:
    return {str(item.get(key)): item for item in items}


def _artifact_hashes(declarations: list[dict[str, Any]]) -> dict[str, str | None]:
    result = {}
    for declaration_index, declaration in enumerate(declarations):
        prefix = f"{declaration.get('step_id')}:{declaration_index}:{declaration.get('declared_path')}"
        for match_index, match in enumerate(declaration.get("matches", [])):
            result[f"{prefix}:{match_index}"] = match.get("sha256")
    return result


def compare_bundles(left: str | Path, right: str | Path) -> dict[str, Any]:
    left_dir = Path(left).expanduser().resolve()
    right_dir = Path(right).expanduser().resolve()
    left_run, right_run = _load(left_dir, "run"), _load(right_dir, "run")
    left_source, right_source = _load(left_dir, "source"), _load(right_dir, "source")
    left_environment, right_environment = _load(left_dir, "environment"), _load(right_dir, "environment")
    left_inputs, right_inputs = _by_id(_load(left_dir, "inputs")), _by_id(_load(right_dir, "inputs"))
    left_metrics, right_metrics = _by_id(_load(left_dir, "metrics")), _by_id(_load(right_dir, "metrics"))
    left_commands, right_commands = _by_id(_load(left_dir, "commands"), "step_id"), _by_id(_load(right_dir, "commands"), "step_id")
    comparisons = {
        "seed": (left_run.get("seed"), right_run.get("seed")),
        "source.commit": (left_source.get("commit"), right_source.get("commit")),
        "source.dirty": (left_source.get("dirty"), right_source.get("dirty")),
        "environment.python": (left_environment.get("python"), right_environment.get("python")),
        "environment.platform": (left_environment.get("platform"), right_environment.get("platform")),
        "environment.torch": (left_environment.get("torch"), right_environment.get("torch")),
        "inputs": (
            {key: value.get("sha256") for key, value in left_inputs.items()},
            {key: value.get("sha256") for key, value in right_inputs.items()},
        ),
        "commands": (
            {
                key: {"argv": value.get("requested_argv", value.get("argv")), "return_code": value.get("return_code")}
                for key, value in left_commands.items()
            },
            {
                key: {"argv": value.get("requested_argv", value.get("argv")), "return_code": value.get("return_code")}
                for key, value in right_commands.items()
            },
        ),
        "artifacts": (_artifact_hashes(_load(left_dir, "artifacts")), _artifact_hashes(_load(right_dir, "artifacts"))),
        "metrics": (
            {key: {"actual": value.get("actual"), "passed": value.get("passed")} for key, value in left_metrics.items()},
            {key: {"actual": value.get("actual"), "passed": value.get("passed")} for key, value in right_metrics.items()},
        ),
    }
    differences = [
        {"field": field, "left": values[0], "right": values[1]}
        for field, values in comparisons.items()
        if values[0] != values[1]
    ]
    return {
        "left": str(left_dir),
        "right": str(right_dir),
        "identical": not differences,
        "differences": differences,
    }
