"""Capture source, environment, input, and artifact evidence."""

from __future__ import annotations

import glob
import hashlib
import importlib.metadata
import os
import platform
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError
from .io import fingerprint
from .manifest import LoadedManifest, substitute


def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        process = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return process.returncode, process.stdout.strip()


def capture_source(manifest: LoadedManifest) -> dict[str, Any]:
    project = manifest.data["project"]
    code, commit = _git(manifest.project_root, "rev-parse", "HEAD")
    if code != 0:
        return {
            "available": False,
            "root": str(manifest.project_root),
            "expected_repo": project.get("repo"),
            "expected_ref": project.get("ref"),
        }

    _, branch = _git(manifest.project_root, "branch", "--show-current")
    _, remote = _git(manifest.project_root, "remote", "get-url", "origin")
    _, status = _git(manifest.project_root, "status", "--porcelain", "--untracked-files=all")
    _, diff = _git(manifest.project_root, "diff", "--binary", "HEAD")
    dirty = bool(status)
    return {
        "available": True,
        "root": str(manifest.project_root),
        "commit": commit,
        "branch": branch or None,
        "remote": remote or None,
        "dirty": dirty,
        "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest() if dirty else None,
        "expected_repo": project.get("repo"),
        "expected_ref": project.get("ref"),
        "allow_dirty": bool(project.get("allow_dirty", True)),
    }


def capture_environment() -> dict[str, Any]:
    distributions = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            distributions.append({"name": name, "version": distribution.version})
    result: dict[str, Any] = {
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "logical_cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "python_executable": os.path.realpath(os.sys.executable),
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "packages": sorted(distributions, key=lambda item: item["name"].lower()),
    }
    try:
        import torch

        result["torch"] = {
            "version": torch.__version__,
            "cuda_available": torch.cuda.is_available(),
            "cuda_version": torch.version.cuda,
            "device_count": torch.cuda.device_count() if torch.cuda.is_available() else 0,
            "devices": [torch.cuda.get_device_name(index) for index in range(torch.cuda.device_count())]
            if torch.cuda.is_available()
            else [],
        }
    except (ImportError, OSError, RuntimeError, AttributeError) as exc:
        result["torch"] = {"available": False, "reason": type(exc).__name__}
    return result


def _resolve_path(value: str, project_root: Path, context: Mapping[str, Any]) -> Path:
    resolved = Path(substitute(value, context)).expanduser()
    return resolved if resolved.is_absolute() else project_root / resolved


def capture_inputs(manifest: LoadedManifest, context: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    for item in manifest.data.get("inputs", []):
        path = _resolve_path(item["path"], manifest.project_root, context)
        record = {
            "id": item["id"],
            "declared_path": item["path"],
            "input_kind": item.get("kind", "unspecified"),
            "required": bool(item.get("required", True)),
            **fingerprint(path),
        }
        records.append(record)
        if record["required"] and not record["exists"]:
            missing.append(f"{record['id']} ({record['path']})")
    if missing:
        raise ConfigError("required inputs are missing: " + ", ".join(missing))
    return records


def capture_artifacts(manifest: LoadedManifest, context: Mapping[str, Any]) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    for step in manifest.data["run"]["steps"]:
        for pattern in step.get("artifacts", []):
            resolved_pattern = _resolve_path(pattern, manifest.project_root, context)
            matches = sorted(Path(match) for match in glob.glob(str(resolved_pattern), recursive=True))
            declarations.append(
                {
                    "step_id": step["id"],
                    "declared_path": pattern,
                    "resolved_pattern": str(resolved_pattern),
                    "matches": [fingerprint(path) for path in matches],
                }
            )
    return declarations
