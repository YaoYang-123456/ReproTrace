"""Capture source, environment, input, and artifact evidence."""

from __future__ import annotations

import glob
import importlib.metadata
import os
import platform
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError
from .evidence import normalize_bundle_path, resolve_bundle_file
from .io import fingerprint, sha256_bytes
from .manifest import LoadedManifest, substitute


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    error: str | None = None


@dataclass(frozen=True)
class SourceCapture:
    record: dict[str, Any]
    files: dict[str, bytes]
    worktree_root: Path | None


def _git_bytes(root: Path, *args: str) -> GitResult:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        process = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=False,
            timeout=15,
            env=environment,
        )
    except FileNotFoundError:
        return GitResult(127, b"", b"", "git_unavailable")
    except OSError as exc:
        return GitResult(127, b"", b"", f"launch_error:{type(exc).__name__}")
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, bytes) else b""
        stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
        return GitResult(124, stdout, stderr, "timeout")
    return GitResult(process.returncode, process.stdout, process.stderr)


def _display_bytes(value: bytes) -> str:
    return value.rstrip(b"\r\n").decode("utf-8", errors="backslashreplace")


def _diagnostic(result: GitResult) -> str | None:
    if result.error:
        return result.error
    if result.stderr:
        return f"non-authoritative utf-8/backslashreplace stderr: {_display_bytes(result.stderr)}"
    return None


def _capture_failure(name: str, result: GitResult) -> ConfigError:
    detail = _diagnostic(result)
    suffix = f"; {detail}" if detail else ""
    return ConfigError(f"cannot capture Git {name}; exit code {result.returncode}{suffix}")


def _find_git_marker(root: Path) -> Path | None:
    """Find a .git file or directory without interpreting Git stderr."""

    for directory in (root, *root.parents):
        marker = directory / ".git"
        try:
            marker_status = marker.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ConfigError(f"cannot inspect potential Git marker {marker}: {type(exc).__name__}") from exc
        if stat.S_ISDIR(marker_status.st_mode) or stat.S_ISREG(marker_status.st_mode):
            return marker
    return None


def _unavailable_source(manifest: LoadedManifest, reason: str, result: GitResult | None = None) -> SourceCapture:
    project = manifest.data["project"]
    record: dict[str, Any] = {
        "schema_version": 1,
        "available": False,
        "reason": reason,
        "root": str(manifest.project_root),
        "expected_repo": project.get("repo"),
        "expected_ref": project.get("ref"),
        "allow_dirty": bool(project.get("allow_dirty", True)),
        "git": {
            "version": None,
            "display_encoding": "utf-8",
            "display_errors": "backslashreplace",
            "display_authoritative": False,
        },
        "summary": {
            "tracked_changes": None,
            "untracked_file_count": None,
        },
        "coverage": {
            "replay": "unavailable",
            "tracked_changes": "not_captured",
            "untracked_paths": "not_captured",
            "untracked_contents": "not_captured",
            "ignored_files": "not_captured",
            "submodule_worktree_contents": "not_captured",
        },
        "git_status": {},
        "git_patch": {},
    }
    if result is not None and _diagnostic(result):
        record["diagnostic"] = _diagnostic(result)
    return SourceCapture(record=record, files={}, worktree_root=None)


def capture_source(manifest: LoadedManifest) -> SourceCapture:
    project = manifest.data["project"]
    repository = _git_bytes(manifest.project_root, "rev-parse", "--is-inside-work-tree")
    if repository.error == "git_unavailable":
        return _unavailable_source(manifest, "git_unavailable", repository)
    if repository.error:
        raise _capture_failure("repository state", repository)
    if repository.returncode != 0:
        if _find_git_marker(manifest.project_root) is not None:
            raise _capture_failure("repository state", repository)
        return _unavailable_source(manifest, "not_git_repository", repository)
    if repository.stdout.strip() != b"true":
        return _unavailable_source(manifest, "not_git_repository", repository)

    top_level_result = _git_bytes(manifest.project_root, "rev-parse", "--show-toplevel")
    if top_level_result.returncode != 0:
        raise _capture_failure("worktree root", top_level_result)
    worktree_root = Path(os.fsdecode(top_level_result.stdout.rstrip(b"\r\n"))).resolve()

    head = _git_bytes(worktree_root, "rev-parse", "--verify", "--quiet", "HEAD")
    if head.returncode != 0:
        if head.error:
            raise _capture_failure("HEAD", head)
        symbolic_head = _git_bytes(worktree_root, "symbolic-ref", "--quiet", "HEAD")
        if symbolic_head.returncode == 0:
            return _unavailable_source(manifest, "unborn_head", head)
        if symbolic_head.error:
            raise _capture_failure("symbolic HEAD", symbolic_head)
        raise _capture_failure("HEAD", head)
    try:
        commit = head.stdout.strip().decode("ascii")
    except UnicodeDecodeError as exc:
        raise ConfigError("cannot capture Git HEAD; commit id is not ASCII") from exc

    version = _git_bytes(worktree_root, "--version")
    if version.returncode != 0:
        raise _capture_failure("version", version)
    branch_result = _git_bytes(worktree_root, "branch", "--show-current")
    if branch_result.returncode != 0:
        raise _capture_failure("branch", branch_result)
    remote_result = _git_bytes(worktree_root, "config", "--get", "remote.origin.url")
    if remote_result.returncode not in (0, 1):
        raise _capture_failure("origin remote", remote_result)

    status_args = ("status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
    status_result = _git_bytes(worktree_root, *status_args)
    if status_result.returncode != 0:
        raise _capture_failure("status", status_result)

    patch_args = (
        "-c",
        "core.quotePath=true",
        "-c",
        "diff.orderFile=/dev/null",
        "-c",
        "diff.suppressBlankEmpty=false",
        "diff",
        "--binary",
        "--full-index",
        "--unified=3",
        "--inter-hunk-context=0",
        "--diff-algorithm=myers",
        "--no-indent-heuristic",
        "--no-renames",
        "--submodule=short",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "HEAD",
        "--",
    )
    patch_result = _git_bytes(worktree_root, *patch_args)
    if patch_result.returncode != 0:
        raise _capture_failure("patch", patch_result)

    untracked_result = _git_bytes(
        worktree_root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
    )
    if untracked_result.returncode != 0:
        raise _capture_failure("untracked file list", untracked_result)

    status = status_result.stdout
    patch = patch_result.stdout
    untracked_count = sum(1 for item in untracked_result.stdout.split(b"\0") if item)
    status_sha256 = sha256_bytes(status)
    patch_sha256 = sha256_bytes(patch)
    record = {
        "schema_version": 1,
        "available": True,
        "root": str(manifest.project_root),
        "worktree_root": str(worktree_root),
        "commit": commit,
        "branch": _display_bytes(branch_result.stdout) or None,
        "remote": _display_bytes(remote_result.stdout) if remote_result.returncode == 0 else None,
        "dirty": bool(status),
        "diff_sha256": patch_sha256 if patch else None,
        "expected_repo": project.get("repo"),
        "expected_ref": project.get("ref"),
        "allow_dirty": bool(project.get("allow_dirty", True)),
        "git": {
            "version": _display_bytes(version.stdout),
            "display_encoding": "utf-8",
            "display_errors": "backslashreplace",
            "display_authoritative": False,
        },
        "summary": {
            "tracked_changes": bool(patch),
            "untracked_file_count": untracked_count,
        },
        "coverage": {
            "replay": "partial",
            "tracked_changes": "git_patch",
            "untracked_paths": "git_status_only",
            "untracked_contents": "not_captured",
            "ignored_files": "not_captured",
            "submodule_worktree_contents": "not_captured",
        },
        "git_status": {
            "path": "source.status",
            "format": "git-status-porcelain-v1-z",
            "content_encoding": "binary",
            "argv": ["git", *status_args],
            "size_bytes": len(status),
            "sha256": status_sha256,
        },
        "git_patch": {
            "path": "source.patch",
            "format": "git-diff",
            "format_version": 1,
            "content_encoding": "binary",
            "base_commit": commit,
            "scope": "tracked-index-and-worktree-against-head",
            "binary_deltas": True,
            "untracked_contents_included": False,
            "argv": ["git", *patch_args],
            "size_bytes": len(patch),
            "sha256": patch_sha256,
        },
    }
    return SourceCapture(
        record=record,
        files={"source.status": status, "source.patch": patch},
        worktree_root=worktree_root,
    )


def validate_output_root(manifest: LoadedManifest, source: SourceCapture) -> None:
    """Reject evidence output that would dirty the audited Git worktree."""

    if source.record.get("available") is not True:
        return
    worktree_root = source.worktree_root
    if worktree_root is None:
        raise ConfigError("cannot validate evidence output isolation; captured Git worktree root is missing")
    try:
        normalized_root = worktree_root.resolve(strict=True)
    except OSError as exc:
        raise ConfigError(
            f"cannot validate evidence output isolation; captured Git worktree root is unavailable: {type(exc).__name__}"
        ) from exc
    if normalized_root != worktree_root or not worktree_root.is_dir():
        raise ConfigError("cannot validate evidence output isolation; captured Git worktree root is not canonical")
    try:
        relative_output = manifest.output_root.relative_to(worktree_root)
    except ValueError:
        return

    ignored = False
    if relative_output != Path("."):
        ignore_probe = relative_output / ".reprotrace-probe"
        ignore_result = _git_bytes(
            worktree_root,
            "check-ignore",
            "--quiet",
            "--no-index",
            "--",
            ignore_probe.as_posix(),
        )
        if ignore_result.returncode == 0:
            ignored = True
        elif ignore_result.returncode == 1 and ignore_result.error is None:
            ignored = False
        else:
            raise _capture_failure("output ignore state", ignore_result)
    if not ignored:
        raise ConfigError(
            "run.output_root would create evidence inside the audited Git worktree "
            f"at an unignored path: {manifest.output_root}"
        )


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
            "path_scope": "external",
            "evidence_path": None,
        }
        records.append(record)
        if record["required"] and not record["exists"]:
            missing.append(f"{record['id']} ({record['path']})")
    if missing:
        raise ConfigError("required inputs are missing: " + ", ".join(missing))
    return records


def _scope_fingerprint(record: dict[str, Any], bundle_root: Path) -> dict[str, Any]:
    """Classify a fingerprint without dereferencing it during later verification."""

    scoped = dict(record)
    scoped.update(path_scope="external", evidence_path=None)
    if record.get("exists") is not True or record.get("kind") != "file":
        return scoped
    root = bundle_root.expanduser().resolve(strict=True)
    candidate = Path(record["path"]).expanduser().resolve(strict=True)
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError:
        return scoped
    evidence_path = normalize_bundle_path(relative, label="artifact evidence")
    resolve_bundle_file(root, evidence_path, label="artifact evidence")
    scoped.update(path_scope="bundle", evidence_path=evidence_path)
    return scoped


def capture_artifacts(
    manifest: LoadedManifest,
    context: Mapping[str, Any],
    bundle_root: Path,
) -> list[dict[str, Any]]:
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
                    "matches": [
                        _scope_fingerprint(fingerprint(path), bundle_root) for path in matches
                    ],
                }
            )
    return declarations
