"""Guarded lifecycle and publication for canonical derived outputs."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .acquisition import EvidenceAcquisitionError, capture_bundle_root_identity
from .errors import ConfigError
from .io import write_bytes_atomic
from .snapshot import BundleRootIdentity, SessionState, VerificationSession


DERIVED_OUTPUT_INVALIDATION_ORDER = ("verification.json", "report.md")
DERIVED_OUTPUT_PATHS = frozenset(DERIVED_OUTPUT_INVALIDATION_ORDER)


@dataclass(slots=True)
class _DerivedOutputLifecycleTestHooks:
    after_guard_capture: Callable[[DerivedOutputLifecycleGuard], None] | None = None
    after_output_invalidated: (
        Callable[[DerivedOutputLifecycleGuard, str], None] | None
    ) = None
    after_invalidation: Callable[[DerivedOutputLifecycleGuard], None] | None = None


@dataclass(frozen=True, slots=True)
class DerivedOutputLifecycleGuard:
    """Operation-local authority for the two canonical derived outputs only."""

    run_dir: Path
    root_identity: BundleRootIdentity

    def require_current_root(self) -> None:
        try:
            current = capture_bundle_root_identity(self.run_dir)
        except EvidenceAcquisitionError as exc:
            raise ConfigError(
                "cannot refresh derived outputs; current bundle root identity "
                f"is unavailable: {exc}"
            ) from exc
        if not current.file_identity.available:
            raise ConfigError(
                "cannot refresh derived outputs; current bundle root identity "
                "is unavailable"
            )
        if current != self.root_identity:
            raise ConfigError(
                "cannot refresh derived outputs; current bundle root identity differs "
                "from the operation-start identity"
            )

    def require_session_identity(self, session: VerificationSession) -> None:
        if not isinstance(session, VerificationSession):
            raise TypeError("derived output lifecycle binding requires a VerificationSession")
        identity = session.snapshot.root_metadata.identity
        if identity is None or not identity.file_identity.available:
            raise ConfigError(
                "cannot refresh derived outputs; verification session bundle root "
                "identity is unavailable"
            )
        if identity != self.root_identity:
            raise ConfigError(
                "cannot refresh derived outputs; verification session bundle root "
                "identity differs from the operation-start identity"
            )
        self.require_current_root()


def _run_lifecycle_hook(callback: Callable[..., None] | None, *args: Any) -> None:
    if callback is not None:
        callback(*args)


def _canonical_output_path(
    guard: DerivedOutputLifecycleGuard,
    relative_path: str,
) -> Path:
    if relative_path not in DERIVED_OUTPUT_PATHS:
        raise ValueError(f"unsupported derived output path: {relative_path!r}")
    return guard.run_dir / relative_path


def _is_windows_reparse_point(status: os.stat_result) -> bool:
    attributes = getattr(status, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _invalidate_canonical_output(
    guard: DerivedOutputLifecycleGuard,
    relative_path: str,
) -> None:
    guard.require_current_root()
    candidate = _canonical_output_path(guard, relative_path)
    try:
        status = os.lstat(candidate)
    except FileNotFoundError:
        guard.require_current_root()
        return
    except OSError as exc:
        raise ConfigError(
            f"cannot inspect canonical derived output {relative_path}: {exc}"
        ) from exc

    is_symlink = stat.S_ISLNK(status.st_mode)
    if not is_symlink and _is_windows_reparse_point(status):
        raise ConfigError(
            "cannot invalidate canonical derived output; unsupported Windows "
            f"reparse object: {relative_path}"
        )
    if not is_symlink and not stat.S_ISREG(status.st_mode):
        raise ConfigError(
            "cannot invalidate canonical derived output; expected a regular file "
            f"or final symlink: {relative_path}"
        )

    guard.require_current_root()
    try:
        os.unlink(candidate)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ConfigError(
            f"cannot invalidate canonical derived output {relative_path}: {exc}"
        ) from exc
    guard.require_current_root()


def begin_derived_output_refresh(
    run_dir: str | Path,
    *,
    _hooks: _DerivedOutputLifecycleTestHooks | None = None,
) -> DerivedOutputLifecycleGuard:
    """Invalidate canonical outputs before one serialized write-intending attempt."""

    directory = Path(run_dir).expanduser().absolute()
    try:
        root_identity = capture_bundle_root_identity(directory)
    except EvidenceAcquisitionError as exc:
        raise ConfigError(
            "cannot begin derived output refresh; bundle root identity is unavailable: "
            f"{exc}"
        ) from exc
    if not root_identity.file_identity.available:
        raise ConfigError(
            "cannot begin derived output refresh; bundle root identity is unavailable"
        )

    guard = DerivedOutputLifecycleGuard(directory, root_identity)
    hooks = _hooks or _DerivedOutputLifecycleTestHooks()
    _run_lifecycle_hook(hooks.after_guard_capture, guard)
    for relative_path in DERIVED_OUTPUT_INVALIDATION_ORDER:
        _invalidate_canonical_output(guard, relative_path)
        _run_lifecycle_hook(hooks.after_output_invalidated, guard, relative_path)

    guard.require_current_root()
    for relative_path in DERIVED_OUTPUT_INVALIDATION_ORDER:
        candidate = _canonical_output_path(guard, relative_path)
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ConfigError(
                f"cannot confirm canonical derived output absence {relative_path}: {exc}"
            ) from exc
        raise ConfigError(
            f"canonical derived output remains after invalidation: {relative_path}"
        )
    guard.require_current_root()
    _run_lifecycle_hook(hooks.after_invalidation, guard)
    return guard


def _encode_derived_json(relative_path: str, value: Any) -> bytes:
    try:
        return (
            json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"cannot serialize strict JSON derived output {relative_path}: {exc}"
        ) from exc


def ensure_session_root_identity(
    session: VerificationSession,
    run_dir: str | Path,
) -> None:
    """Fail unless the named bundle root is still the session's captured root."""

    if not isinstance(session, VerificationSession):
        raise TypeError("derived output write requires a VerificationSession")
    if session.state is not SessionState.OPEN or not session.snapshot.session_active:
        raise ConfigError("cannot write derived output from an inactive verification session")
    if not session.snapshot.sealed:
        raise ConfigError("cannot write derived output before snapshot sealing")
    session.snapshot.require_established_evidence_root()
    expected = session.snapshot.root_metadata.identity
    if expected is None or not expected.file_identity.available:
        raise ConfigError("cannot write derived output; captured bundle root identity is unavailable")
    try:
        current = capture_bundle_root_identity(run_dir)
    except EvidenceAcquisitionError as exc:
        raise ConfigError(
            f"cannot write derived output; current bundle root identity is unavailable: {exc}"
        ) from exc
    if not current.file_identity.available:
        raise ConfigError("cannot write derived output; current bundle root identity is unavailable")
    if current != expected:
        raise ConfigError(
            "cannot write derived output; current bundle root identity differs "
            "from the verification session"
        )


def write_session_derived_bytes(
    session: VerificationSession,
    run_dir: str | Path,
    relative_path: str,
    value: bytes,
) -> Path:
    """Atomically write one approved derived output after a fresh root check."""

    if relative_path not in DERIVED_OUTPUT_PATHS:
        raise ValueError(f"unsupported derived output path: {relative_path!r}")
    if not isinstance(value, bytes):
        raise TypeError("derived output payload must be bytes")
    ensure_session_root_identity(session, run_dir)
    destination = Path(run_dir) / relative_path
    write_bytes_atomic(destination, value)
    return destination


def write_session_derived_json(
    session: VerificationSession,
    run_dir: str | Path,
    relative_path: str,
    value: Any,
) -> Path:
    """Serialize strict JSON and write it as a root-identity-bound output."""

    encoded = _encode_derived_json(relative_path, value)
    return write_session_derived_bytes(
        session,
        run_dir,
        relative_path,
        encoded,
    )


def write_guarded_derived_bytes(
    guard: DerivedOutputLifecycleGuard,
    relative_path: str,
    value: bytes,
) -> Path:
    """Publish one fixed canonical output under operation-start root authority."""

    if not isinstance(guard, DerivedOutputLifecycleGuard):
        raise TypeError("guarded derived output write requires a lifecycle guard")
    if not isinstance(value, bytes):
        raise TypeError("derived output payload must be bytes")
    destination = _canonical_output_path(guard, relative_path)
    guard.require_current_root()
    write_bytes_atomic(destination, value)
    guard.require_current_root()
    return destination


def write_guarded_derived_json(
    guard: DerivedOutputLifecycleGuard,
    relative_path: str,
    value: Any,
) -> Path:
    """Serialize strict JSON and publish it under lifecycle root authority."""

    return write_guarded_derived_bytes(
        guard,
        relative_path,
        _encode_derived_json(relative_path, value),
    )
