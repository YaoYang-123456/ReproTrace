"""Handle-bound acquisition of one verifier-owned evidence object.

Stage 6.2b keeps this primitive separate from the production verifier.  The
same opened descriptor supplies every byte used for hashing and retention.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .evidence import normalize_bundle_path, resolve_bundle_file
from .io import sha256_bytes
from .snapshot import (
    BundleRootIdentity,
    EvidenceFingerprint,
    EvidenceObjectState,
    FileIdentity,
    SnapshotStateError,
    VerifiedBundleSnapshot,
    VerifiedEvidenceObject,
)


DEFAULT_ACQUISITION_CHUNK_SIZE = 1024 * 1024


class EvidenceAcquisitionError(SnapshotStateError):
    """A fail-closed single-file acquisition error."""

    def __init__(self, category: str, detail: str) -> None:
        self.category = category
        self.detail = detail
        self.secondary_diagnostics: list[str] = []
        super().__init__(f"{category}: {detail}")


@dataclass(frozen=True, slots=True)
class CapturedBundleFile:
    """One memory-only bootstrap capture with no live-path semantic API."""

    relative_path: str
    exact_bytes: bytes
    observed_fingerprint: EvidenceFingerprint
    file_identity: FileIdentity
    root_identity: BundleRootIdentity


@dataclass(slots=True)
class _AcquisitionTestHooks:
    """Narrow deterministic race/I/O hooks; unused by the normal path."""

    after_precheck: Callable[[Path], None] | None = None
    after_open_before_postcheck: Callable[[int, Path], None] | None = None
    before_stream_read: Callable[[int], None] | None = None
    read_chunk: Callable[[int, int], bytes] = os.read


def _mode_file_type(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISDIR(mode):
        return "directory"
    return "other"


def _identity_from_stat(status: os.stat_result) -> FileIdentity:
    file_type = _mode_file_type(status.st_mode)
    device = getattr(status, "st_dev", None)
    file_id = getattr(status, "st_ino", None)
    if isinstance(device, bool) or not isinstance(device, int) or device < 0:
        return FileIdentity.unavailable(
            file_type=file_type,
            reason="os.stat did not provide a usable non-negative st_dev",
        )
    if isinstance(file_id, bool) or not isinstance(file_id, int) or file_id <= 0:
        return FileIdentity.unavailable(
            file_type=file_type,
            reason="os.stat did not provide a usable positive st_ino/file index",
        )
    return FileIdentity(
        mechanism="os.stat(st_dev,st_ino)",
        device=device,
        file_id=file_id,
        file_type=file_type,
    )


def _resolve_root(bundle_root: str | os.PathLike[str]) -> tuple[Path, FileIdentity]:
    try:
        resolved = Path(bundle_root).expanduser().resolve(strict=True)
        status = os.stat(resolved, follow_symlinks=False)
    except (OSError, RuntimeError) as exc:
        raise EvidenceAcquisitionError(
            "bundle_root_invalid",
            f"cannot resolve or inspect bundle root: {exc}",
        ) from exc
    if not stat.S_ISDIR(status.st_mode):
        raise EvidenceAcquisitionError(
            "bundle_root_invalid",
            f"bundle root is not a directory: {resolved}",
        )
    return resolved, _identity_from_stat(status)


def capture_bundle_root_identity(
    bundle_root: str | os.PathLike[str],
) -> BundleRootIdentity:
    """Capture the structured identity expected by later acquisitions."""

    _, identity = _resolve_root(bundle_root)
    return BundleRootIdentity(identity)


def _require_available_identity(identity: FileIdentity, *, label: str) -> None:
    if not identity.available:
        raise EvidenceAcquisitionError(
            "identity_unavailable",
            f"{label} identity is unavailable: {identity.unavailable_reason}",
        )


def _validate_root(
    bundle_root: str | os.PathLike[str],
    expected: BundleRootIdentity,
) -> Path:
    resolved, current = _resolve_root(bundle_root)
    _require_available_identity(expected.file_identity, label="expected bundle root")
    _require_available_identity(current, label="current bundle root")
    if current != expected.file_identity:
        raise EvidenceAcquisitionError(
            "bundle_root_unstable",
            "current bundle root identity differs from the session identity",
        )
    return resolved


def _inspect_candidate(root: Path, relative_path: str) -> tuple[Path, FileIdentity]:
    try:
        candidate = resolve_bundle_file(
            root,
            relative_path,
            label="snapshot evidence",
        )
        status = os.stat(candidate, follow_symlinks=False)
    except ConfigError as exc:
        raise EvidenceAcquisitionError("candidate_path_invalid", str(exc)) from exc
    except OSError as exc:
        raise EvidenceAcquisitionError(
            "candidate_inspection_failed",
            f"cannot inspect snapshot evidence {relative_path}: {exc}",
        ) from exc
    if not stat.S_ISREG(status.st_mode):
        raise EvidenceAcquisitionError(
            "candidate_not_regular",
            f"snapshot evidence is not a regular file: {relative_path}",
        )
    identity = _identity_from_stat(status)
    _require_available_identity(identity, label=f"candidate {relative_path}")
    return candidate, identity


def _opened_file_identity(descriptor: int, relative_path: str) -> FileIdentity:
    try:
        status = os.fstat(descriptor)
    except OSError as exc:
        raise EvidenceAcquisitionError(
            "opened_identity_failed",
            f"cannot fstat opened evidence {relative_path}: {exc}",
        ) from exc
    if not stat.S_ISREG(status.st_mode):
        raise EvidenceAcquisitionError(
            "opened_not_regular",
            f"opened evidence is not a regular file: {relative_path}",
        )
    identity = _identity_from_stat(status)
    _require_available_identity(identity, label=f"opened evidence {relative_path}")
    return identity


def _open_read_only(candidate: Path, relative_path: str) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise EvidenceAcquisitionError(
            "candidate_open_failed",
            f"cannot open snapshot evidence {relative_path}: {exc}",
        ) from exc
    try:
        os.set_inheritable(descriptor, False)
        return descriptor
    except OSError as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise EvidenceAcquisitionError(
            "candidate_open_failed",
            f"cannot make snapshot evidence descriptor non-inheritable: {exc}",
        ) from exc


def _run_hook(callback: Callable[..., None] | None, *args: Any) -> None:
    if callback is not None:
        callback(*args)


def _stream_handle_bound_file(
    *,
    bundle_root: str | os.PathLike[str],
    expected_root: BundleRootIdentity,
    relative_path: str,
    chunk_size: int,
    hooks: _AcquisitionTestHooks,
    begin_capture: Callable[[FileIdentity], None],
    consume_chunk: Callable[[bytes], None],
) -> FileIdentity:
    """Run one shared pre/open/fstat/post/read descriptor pipeline."""

    root = _validate_root(bundle_root, expected_root)
    relative_path = normalize_bundle_path(relative_path, label="snapshot evidence")
    candidate, preopen_identity = _inspect_candidate(root, relative_path)
    _run_hook(hooks.after_precheck, candidate)

    descriptor = _open_read_only(candidate, relative_path)
    primary_error: BaseException | None = None
    try:
        opened_identity = _opened_file_identity(descriptor, relative_path)
        if opened_identity != preopen_identity:
            raise EvidenceAcquisitionError(
                "candidate_identity_unstable",
                "opened file identity differs from the pre-open candidate identity",
            )

        _run_hook(hooks.after_open_before_postcheck, descriptor, candidate)
        post_root = _validate_root(bundle_root, expected_root)
        _, postopen_identity = _inspect_candidate(post_root, relative_path)
        if postopen_identity != opened_identity:
            raise EvidenceAcquisitionError(
                "candidate_identity_unstable",
                "post-open path identity differs from the opened file identity",
            )

        begin_capture(opened_identity)
        _run_hook(hooks.before_stream_read, descriptor)
        while True:
            try:
                chunk = hooks.read_chunk(descriptor, chunk_size)
            except OSError as exc:
                raise EvidenceAcquisitionError(
                    "read_failed",
                    f"descriptor read failed for {relative_path}: {exc}",
                ) from exc
            if not isinstance(chunk, bytes):
                raise EvidenceAcquisitionError(
                    "read_failed",
                    f"descriptor reader returned non-bytes content for {relative_path}",
                )
            if len(chunk) > chunk_size:
                raise EvidenceAcquisitionError(
                    "read_failed",
                    f"descriptor reader exceeded bounded chunk size for {relative_path}",
                )
            if not chunk:
                break
            try:
                consume_chunk(chunk)
            except OSError as exc:
                raise EvidenceAcquisitionError(
                    "retention_failed",
                    f"snapshot retention failed for {relative_path}: {exc}",
                ) from exc
        return opened_identity
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            diagnostic = f"descriptor close failed for {relative_path}: {exc}"
            if isinstance(primary_error, EvidenceAcquisitionError):
                primary_error.secondary_diagnostics.append(diagnostic)
            elif primary_error is None:
                raise EvidenceAcquisitionError(
                    "descriptor_close_failed",
                    diagnostic,
                ) from exc


def capture_bundle_file_once(
    *,
    bundle_root: str | os.PathLike[str],
    expected_root_identity: BundleRootIdentity,
    relative_path: str,
    chunk_size: int = DEFAULT_ACQUISITION_CHUNK_SIZE,
    _hooks: _AcquisitionTestHooks | None = None,
) -> CapturedBundleFile:
    """Capture one bootstrap file into exact immutable memory bytes."""

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("acquisition chunk_size must be a positive integer")
    normalized = normalize_bundle_path(relative_path, label="bootstrap evidence")
    retained = bytearray()
    identity = _stream_handle_bound_file(
        bundle_root=bundle_root,
        expected_root=expected_root_identity,
        relative_path=normalized,
        chunk_size=chunk_size,
        hooks=_hooks or _AcquisitionTestHooks(),
        begin_capture=lambda opened_identity: None,
        consume_chunk=retained.extend,
    )
    exact_bytes = bytes(retained)
    return CapturedBundleFile(
        relative_path=normalized,
        exact_bytes=exact_bytes,
        observed_fingerprint=EvidenceFingerprint(
            len(exact_bytes),
            sha256_bytes(exact_bytes),
        ),
        file_identity=identity,
        root_identity=expected_root_identity,
    )


def acquire_bundle_evidence(
    *,
    bundle_root: str | os.PathLike[str],
    snapshot: VerifiedBundleSnapshot,
    evidence: VerifiedEvidenceObject,
    chunk_size: int = DEFAULT_ACQUISITION_CHUNK_SIZE,
    _hooks: _AcquisitionTestHooks | None = None,
) -> None:
    """Acquire one already-owned object from one opened live descriptor.

    Success leaves the object in ``ACQUIRED`` state.  Every failure leaves it
    ``FAILED`` and owned by its snapshot/session for deterministic cleanup.
    No live evidence path is returned.
    """

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("acquisition chunk_size must be a positive integer")
    if not snapshot.session_claimed or not snapshot.session_active:
        raise SnapshotStateError(
            "snapshot must belong to an active verification session before acquisition"
        )
    if snapshot.objects.get(evidence.relative_path) is not evidence:
        raise SnapshotStateError(
            "evidence must be added to the session-owned snapshot before acquisition"
        )
    if evidence.state is not EvidenceObjectState.NEW:
        raise SnapshotStateError(
            f"live acquisition requires new evidence, not {evidence.state.value}"
        )

    hooks = _hooks or _AcquisitionTestHooks()
    try:
        expected_root = snapshot.root_metadata.identity
        if expected_root is None:
            raise EvidenceAcquisitionError(
                "identity_unavailable",
                "snapshot has no captured bundle root identity",
            )
        _stream_handle_bound_file(
            bundle_root=bundle_root,
            expected_root=expected_root,
            relative_path=evidence.relative_path,
            chunk_size=chunk_size,
            hooks=hooks,
            begin_capture=lambda identity: evidence.begin_acquisition(
                identity=identity
            ),
            consume_chunk=evidence.append_acquired_bytes,
        )
        try:
            evidence.finish_acquisition()
        except OSError as exc:
            raise EvidenceAcquisitionError(
                "retention_failed",
                f"snapshot retention finalization failed for {evidence.relative_path}: {exc}",
            ) from exc
        except SnapshotStateError as exc:
            raise EvidenceAcquisitionError(
                "fingerprint_mismatch",
                f"acquired bytes do not match the index for {evidence.relative_path}",
            ) from exc
    except Exception as exc:
        if evidence.state is not EvidenceObjectState.SEALED:
            detail = str(exc)
            try:
                evidence.mark_failed(detail)
            except SnapshotStateError:
                pass
        if isinstance(exc, EvidenceAcquisitionError):
            raise
        raise EvidenceAcquisitionError("acquisition_failed", str(exc)) from exc
