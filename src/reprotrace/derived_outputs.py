"""Root-identity-bound writes for derived schema-1 bundle outputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .acquisition import EvidenceAcquisitionError, capture_bundle_root_identity
from .errors import ConfigError
from .io import write_bytes_atomic
from .snapshot import SessionState, VerificationSession


DERIVED_OUTPUT_PATHS = frozenset({"verification.json", "report.md"})


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

    try:
        encoded = (
            json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"cannot serialize strict JSON derived output {relative_path}: {exc}"
        ) from exc
    return write_session_derived_bytes(
        session,
        run_dir,
        relative_path,
        encoded,
    )
