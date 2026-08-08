"""Verifier-private immutable evidence snapshot lifecycle model.

Stage 6.2a deliberately models ownership and state only.  It does not acquire
live bundle paths or participate in the production verifier yet.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, BinaryIO

from .errors import ConfigError
from .evidence import canonical_evidence_index_bytes, normalize_bundle_path
from .io import sha256_bytes


class SnapshotStateError(RuntimeError):
    """Raised when a snapshot lifecycle operation is not valid."""


class SemanticEvidenceUnavailable(SnapshotStateError):
    """Raised when evidence has no retained semantic representation."""


class StorageKind(str, Enum):
    MEMORY = "memory"
    SPOOL = "spool"
    INTEGRITY_ONLY = "integrity_only"


class EvidenceObjectState(str, Enum):
    NEW = "new"
    ACQUIRING = "acquiring"
    ACQUIRED = "acquired"
    SEALED = "sealed"
    FAILED = "failed"


class SnapshotState(str, Enum):
    NEW = "new"
    ACQUIRING = "acquiring"
    ACQUIRED = "acquired"
    SEALED = "sealed"


class SessionState(str, Enum):
    NEW = "new"
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class EvidenceFingerprint:
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("evidence size must be a non-negative integer")
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or self.sha256 != self.sha256.lower()
        ):
            raise ValueError("evidence sha256 must be lowercase SHA-256 hex")
        try:
            int(self.sha256, 16)
        except ValueError as exc:
            raise ValueError("evidence sha256 must be lowercase SHA-256 hex") from exc


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Structured stat identity used only to defend one acquisition."""

    mechanism: str
    device: int | None
    file_id: int | None
    file_type: str
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mechanism, str) or not self.mechanism:
            raise ValueError("file identity mechanism must be a non-empty string")
        if self.file_type not in {"regular", "directory", "other"}:
            raise ValueError(f"unsupported file identity type: {self.file_type!r}")
        if self.unavailable_reason is None:
            if (
                isinstance(self.device, bool)
                or not isinstance(self.device, int)
                or self.device < 0
            ):
                raise ValueError("file identity device must be a non-negative integer")
            if (
                isinstance(self.file_id, bool)
                or not isinstance(self.file_id, int)
                or self.file_id <= 0
            ):
                raise ValueError("file identity id must be a positive integer")
        else:
            if not isinstance(self.unavailable_reason, str) or not self.unavailable_reason:
                raise ValueError("identity unavailability requires a diagnostic reason")
            if self.device is not None or self.file_id is not None:
                raise ValueError("unavailable file identity cannot contain identity values")

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    @classmethod
    def unavailable(cls, *, file_type: str, reason: str) -> FileIdentity:
        return cls(
            mechanism="unavailable",
            device=None,
            file_id=None,
            file_type=file_type,
            unavailable_reason=reason,
        )


@dataclass(frozen=True, slots=True)
class BundleRootIdentity:
    """Structured identity of the resolved bundle-root directory."""

    file_identity: FileIdentity

    def __post_init__(self) -> None:
        if self.file_identity.file_type != "directory":
            raise ValueError("bundle root identity must describe a directory")


@dataclass(frozen=True, slots=True)
class BundleRootMetadata:
    display_path: str
    identity: BundleRootIdentity | None = None


@dataclass(frozen=True, slots=True)
class CleanupDiagnostic:
    resource: str
    exception_type: str
    message: str


class _ReadOnlyBytesIO(io.BytesIO):
    """A fresh in-memory view that cannot mutate its source object."""

    def write(self, value: bytes) -> int:
        raise io.UnsupportedOperation("semantic evidence reader is read-only")

    def writelines(self, lines: Any) -> None:
        raise io.UnsupportedOperation("semantic evidence reader is read-only")

    def truncate(self, size: int | None = None) -> int:
        raise io.UnsupportedOperation("semantic evidence reader is read-only")

    def getbuffer(self) -> memoryview:
        return memoryview(self.getvalue())


class _SpoolReader(io.RawIOBase):
    """Independent-position read-only view over one verifier-owned spool."""

    def __init__(self, owner: VerifiedEvidenceObject) -> None:
        super().__init__()
        self._owner = owner
        self._position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        self._checkClosed()
        return self._position

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self._checkClosed()
        if whence == os.SEEK_SET:
            position = offset
        elif whence == os.SEEK_CUR:
            position = self._position + offset
        elif whence == os.SEEK_END:
            position = self._owner.semantic_size + offset
        else:
            raise ValueError(f"unsupported seek whence: {whence}")
        if position < 0:
            raise ValueError("negative semantic evidence seek position")
        self._position = position
        return position

    def read(self, size: int = -1) -> bytes:
        self._checkClosed()
        value = self._owner._read_spool_at(self._position, size)
        self._position += len(value)
        return value

    def readinto(self, buffer: Any) -> int:
        value = self.read(len(buffer))
        buffer[: len(value)] = value
        return len(value)

    def write(self, value: bytes) -> int:
        raise io.UnsupportedOperation("semantic evidence reader is read-only")


class VerifiedEvidenceObject:
    """One acquired evidence object owned by a verification session."""

    __slots__ = (
        "_relative_path",
        "_roles",
        "_expected",
        "_observed",
        "_storage_kind",
        "_state",
        "_closed",
        "_failure_reason",
        "_acquisition_identity",
        "_hasher",
        "_acquired_size",
        "_memory_buffer",
        "_memory_payload",
        "_spool",
        "_spool_max_size",
        "_spool_rolled_to_disk",
        "_spool_lock",
        "_snapshot_claimed",
    )

    def __init__(
        self,
        *,
        relative_path: str,
        roles: Sequence[str],
        expected_size: int,
        expected_sha256: str,
        storage_kind: StorageKind,
        spool_max_size: int = 8 * 1024 * 1024,
    ) -> None:
        self._relative_path = normalize_bundle_path(
            relative_path, label="verified evidence"
        )
        if isinstance(roles, (str, bytes)) or not roles:
            raise ValueError("verified evidence roles must be a non-empty sequence")
        if not all(isinstance(role, str) and role for role in roles):
            raise ValueError("verified evidence roles must contain non-empty strings")
        self._roles = tuple(sorted(set(roles)))
        self._expected = EvidenceFingerprint(expected_size, expected_sha256)
        self._observed: EvidenceFingerprint | None = None
        try:
            self._storage_kind = StorageKind(storage_kind)
        except ValueError as exc:
            raise ValueError(f"unsupported evidence storage kind: {storage_kind!r}") from exc
        if (
            isinstance(spool_max_size, bool)
            or not isinstance(spool_max_size, int)
            or spool_max_size <= 0
        ):
            raise ValueError("spool_max_size must be a positive integer")
        self._spool_max_size = spool_max_size
        self._state = EvidenceObjectState.NEW
        self._closed = False
        self._failure_reason: str | None = None
        self._acquisition_identity: FileIdentity | None = None
        self._hasher: Any | None = None
        self._acquired_size = 0
        self._memory_buffer: bytearray | None = None
        self._memory_payload: bytes | None = None
        self._spool: BinaryIO | None = None
        self._spool_rolled_to_disk = False
        self._spool_lock = threading.RLock()
        self._snapshot_claimed = False

    @property
    def relative_path(self) -> str:
        return self._relative_path

    @property
    def roles(self) -> tuple[str, ...]:
        return self._roles

    @property
    def expected_fingerprint(self) -> EvidenceFingerprint:
        return self._expected

    @property
    def observed_fingerprint(self) -> EvidenceFingerprint | None:
        return self._observed

    @property
    def storage_kind(self) -> StorageKind:
        return self._storage_kind

    @property
    def state(self) -> EvidenceObjectState:
        return self._state

    @property
    def sealed(self) -> bool:
        return self._state is EvidenceObjectState.SEALED

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def acquisition_identity(self) -> FileIdentity | None:
        return self._acquisition_identity

    @property
    def failure_reason(self) -> str | None:
        return self._failure_reason

    @property
    def spool_rolled_to_disk(self) -> bool:
        return self._spool_rolled_to_disk

    @property
    def snapshot_owned(self) -> bool:
        return self._snapshot_claimed

    @property
    def semantic_size(self) -> int:
        if self._observed is None:
            raise SnapshotStateError("evidence acquisition has not completed")
        return self._observed.size_bytes

    @property
    def acquired_and_validated(self) -> bool:
        return (
            not self._closed
            and self._state in {EvidenceObjectState.ACQUIRED, EvidenceObjectState.SEALED}
            and self._observed == self._expected
        )

    def _require_open(self) -> None:
        if self._closed:
            raise SnapshotStateError(
                f"verified evidence object is closed: {self._relative_path}"
            )

    def begin_acquisition(self, *, identity: FileIdentity | None = None) -> None:
        self._require_open()
        if self._state is not EvidenceObjectState.NEW:
            raise SnapshotStateError(
                f"evidence acquisition cannot begin from state {self._state.value}"
            )
        if identity is not None:
            if not isinstance(identity, FileIdentity):
                raise TypeError("acquisition identity must be a FileIdentity or null")
            if not identity.available:
                raise ValueError("unavailable file identity cannot begin acquisition")
        self._acquisition_identity = identity
        self._hasher = hashlib.sha256()
        self._acquired_size = 0
        if self._storage_kind is StorageKind.MEMORY:
            self._memory_buffer = bytearray()
        elif self._storage_kind is StorageKind.SPOOL:
            self._spool = tempfile.SpooledTemporaryFile(
                max_size=self._spool_max_size,
                mode="w+b",
            )
        self._state = EvidenceObjectState.ACQUIRING

    def append_acquired_bytes(self, chunk: bytes | bytearray | memoryview) -> None:
        self._require_open()
        if self._state is not EvidenceObjectState.ACQUIRING:
            raise SnapshotStateError("evidence bytes can only be appended while acquiring")
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("acquired evidence chunks must be bytes-like")
        value = bytes(chunk)
        if self._hasher is None:
            raise SnapshotStateError("evidence acquisition digest is unavailable")
        self._hasher.update(value)
        self._acquired_size += len(value)
        if self._storage_kind is StorageKind.MEMORY:
            if self._memory_buffer is None:
                raise SnapshotStateError("memory acquisition buffer is unavailable")
            self._memory_buffer.extend(value)
        elif self._storage_kind is StorageKind.SPOOL:
            if self._spool is None:
                raise SnapshotStateError("spool acquisition storage is unavailable")
            self._spool.write(value)
            if (
                self._acquired_size > self._spool_max_size
                and not self._spool_rolled_to_disk
            ):
                self._spool.rollover()
                self._spool_rolled_to_disk = True

    def finish_acquisition(self) -> EvidenceFingerprint:
        self._require_open()
        if self._state is not EvidenceObjectState.ACQUIRING or self._hasher is None:
            raise SnapshotStateError("evidence acquisition is not in progress")
        observed = EvidenceFingerprint(
            self._acquired_size,
            self._hasher.hexdigest(),
        )
        self._observed = observed
        self._hasher = None
        if self._storage_kind is StorageKind.MEMORY:
            if self._memory_buffer is None:
                raise SnapshotStateError("memory acquisition buffer is unavailable")
            self._memory_payload = bytes(self._memory_buffer)
            self._memory_buffer = None
        elif self._storage_kind is StorageKind.SPOOL:
            if self._spool is None:
                raise SnapshotStateError("spool acquisition storage is unavailable")
            self._spool.flush()
            self._spool.seek(0)

        if observed != self._expected:
            self._failure_reason = "observed fingerprint does not match evidence index"
            self._state = EvidenceObjectState.FAILED
            raise SnapshotStateError(
                f"evidence fingerprint mismatch for {self._relative_path}"
            )
        self._state = EvidenceObjectState.ACQUIRED
        return observed

    def acquire_bytes(
        self,
        value: bytes,
        *,
        identity: FileIdentity | None = None,
    ) -> EvidenceFingerprint:
        self.begin_acquisition(identity=identity)
        self.append_acquired_bytes(value)
        return self.finish_acquisition()

    def mark_failed(self, reason: str) -> None:
        self._require_open()
        if self._state is EvidenceObjectState.SEALED:
            raise SnapshotStateError("sealed evidence cannot be marked failed")
        if not isinstance(reason, str) or not reason:
            raise ValueError("failure reason must be a non-empty string")
        self._failure_reason = reason
        self._hasher = None
        self._memory_buffer = None
        self._memory_payload = None
        self._state = EvidenceObjectState.FAILED

    def seal(self) -> None:
        self._require_open()
        if self._state is not EvidenceObjectState.ACQUIRED:
            raise SnapshotStateError(
                f"evidence can only be sealed from acquired state, not {self._state.value}"
            )
        if self._observed != self._expected:
            raise SnapshotStateError("unvalidated evidence cannot be sealed")
        self._state = EvidenceObjectState.SEALED

    def open_reader(self) -> BinaryIO:
        self._require_open()
        if self._state is not EvidenceObjectState.SEALED:
            raise SnapshotStateError("semantic evidence is unavailable before sealing")
        if self._storage_kind is StorageKind.INTEGRITY_ONLY:
            raise SemanticEvidenceUnavailable(
                f"integrity-only evidence has no semantic reader: {self._relative_path}"
            )
        if self._storage_kind is StorageKind.MEMORY:
            if self._memory_payload is None:
                raise SnapshotStateError("sealed memory evidence payload is unavailable")
            return _ReadOnlyBytesIO(self._memory_payload)
        if self._spool is None:
            raise SnapshotStateError("sealed spool evidence payload is unavailable")
        return _SpoolReader(self)

    def _read_spool_at(self, position: int, size: int) -> bytes:
        self._require_open()
        if self._state is not EvidenceObjectState.SEALED or self._spool is None:
            raise SnapshotStateError("sealed spool evidence payload is unavailable")
        with self._spool_lock:
            self._spool.seek(position)
            value = self._spool.read(size)
        if not isinstance(value, bytes):
            raise SnapshotStateError("spool evidence returned non-bytes content")
        return value

    def _claim_snapshot(self) -> None:
        if self._snapshot_claimed:
            raise SnapshotStateError(
                f"verified evidence already belongs to a snapshot: {self._relative_path}"
            )
        self._snapshot_claimed = True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._memory_buffer = None
        self._memory_payload = None
        spool = self._spool
        self._spool = None
        if spool is not None:
            spool.close()


class VerifiedBundleSnapshot:
    """One logical byte snapshot described by one canonical evidence index."""

    def __init__(
        self,
        *,
        bundle_root: str,
        index_bytes: bytes,
        parsed_index: Mapping[str, Any],
        root_identity: BundleRootIdentity | None = None,
    ) -> None:
        if not isinstance(bundle_root, str) or not bundle_root:
            raise ValueError("bundle_root metadata must be a non-empty string")
        exact_index_bytes = bytes(index_bytes)
        canonical = canonical_evidence_index_bytes(parsed_index)
        if exact_index_bytes != canonical:
            raise ConfigError(
                "verified snapshot requires exact canonical evidence.index.json bytes"
            )
        normalized_index = json.loads(canonical.decode("utf-8"))
        self._root_metadata = BundleRootMetadata(bundle_root, root_identity)
        self._index_bytes = exact_index_bytes
        self._parsed_index = normalized_index
        self._candidate_root = sha256_bytes(exact_index_bytes)
        self._expected_entries = {
            entry["path"]: entry for entry in normalized_index["entries"]
        }
        self._objects: dict[str, VerifiedEvidenceObject] = {}
        self._parsed_records: dict[str, Any] = {}
        self._state = SnapshotState.NEW
        self._session_claimed = False
        self._session_active = False
        self._session_closed = False
        self._cleanup_diagnostics: list[CleanupDiagnostic] = []

    @property
    def root_metadata(self) -> BundleRootMetadata:
        return self._root_metadata

    @property
    def index_bytes(self) -> bytes:
        return self._index_bytes

    @property
    def parsed_index(self) -> dict[str, Any]:
        return copy.deepcopy(self._parsed_index)

    @property
    def candidate_evidence_root(self) -> str:
        return self._candidate_root

    @property
    def established_evidence_root(self) -> str | None:
        if self._state is SnapshotState.SEALED:
            return self._candidate_root
        return None

    @property
    def state(self) -> SnapshotState:
        return self._state

    @property
    def acquisition_complete(self) -> bool:
        return self._state in {SnapshotState.ACQUIRED, SnapshotState.SEALED}

    @property
    def sealed(self) -> bool:
        return self._state is SnapshotState.SEALED

    @property
    def session_closed(self) -> bool:
        return self._session_closed

    @property
    def session_claimed(self) -> bool:
        return self._session_claimed

    @property
    def session_active(self) -> bool:
        return self._session_active and not self._session_closed

    @property
    def objects(self) -> Mapping[str, VerifiedEvidenceObject]:
        return MappingProxyType(dict(self._objects))

    @property
    def cleanup_diagnostics(self) -> tuple[CleanupDiagnostic, ...]:
        return tuple(self._cleanup_diagnostics)

    def _require_session_open(self) -> None:
        if self._session_closed:
            raise SnapshotStateError("verification session is closed")

    def add_evidence(self, evidence: VerifiedEvidenceObject) -> None:
        self._require_session_open()
        if self._state not in {SnapshotState.NEW, SnapshotState.ACQUIRING}:
            raise SnapshotStateError("evidence cannot be added after acquisition completion")
        path = evidence.relative_path
        if path in self._objects:
            raise SnapshotStateError(f"duplicate acquired evidence path: {path}")
        expected = self._expected_entries.get(path)
        if expected is None:
            raise SnapshotStateError(f"evidence path is not declared by the index: {path}")
        if evidence.roles != tuple(expected["roles"]):
            raise SnapshotStateError(f"evidence roles do not match the index: {path}")
        if evidence.expected_fingerprint != EvidenceFingerprint(
            expected["size_bytes"], expected["sha256"]
        ):
            raise SnapshotStateError(
                f"evidence expected fingerprint does not match the index: {path}"
            )
        if evidence.closed:
            raise SnapshotStateError(f"closed evidence cannot enter a snapshot: {path}")
        evidence._claim_snapshot()
        self._objects[path] = evidence
        self._state = SnapshotState.ACQUIRING

    def complete_acquisition(self) -> None:
        self._require_session_open()
        if self._state not in {SnapshotState.NEW, SnapshotState.ACQUIRING}:
            raise SnapshotStateError(
                f"snapshot acquisition cannot complete from state {self._state.value}"
            )
        expected_paths = set(self._expected_entries)
        actual_paths = set(self._objects)
        missing = sorted(expected_paths - actual_paths)
        if missing:
            raise SnapshotStateError(
                f"snapshot acquisition is incomplete; missing evidence: {', '.join(missing)}"
            )
        invalid = sorted(
            path
            for path, evidence in self._objects.items()
            if not evidence.acquired_and_validated
        )
        if invalid:
            raise SnapshotStateError(
                "snapshot acquisition contains failed or unvalidated evidence: "
                + ", ".join(invalid)
            )
        self._state = SnapshotState.ACQUIRED

    def seal(self) -> None:
        self._require_session_open()
        if self._state is not SnapshotState.ACQUIRED:
            raise SnapshotStateError("snapshot must be fully acquired before sealing")
        unsealed = sorted(
            path
            for path, evidence in self._objects.items()
            if not evidence.sealed or evidence.closed
        )
        if unsealed:
            raise SnapshotStateError(
                "snapshot contains unsealed evidence: " + ", ".join(unsealed)
            )
        self._state = SnapshotState.SEALED

    def require_established_evidence_root(self) -> str:
        established = self.established_evidence_root
        if established is None:
            raise SnapshotStateError(
                "evidence root is not established before full acquisition and sealing"
            )
        return established

    def cache_parsed_record(self, relative_path: str, value: Any) -> None:
        self._require_session_open()
        path = normalize_bundle_path(relative_path, label="parsed snapshot record")
        evidence = self._objects.get(path)
        if evidence is None or not evidence.sealed:
            raise SnapshotStateError(
                f"parsed records require sealed snapshot evidence: {path}"
            )
        if evidence.storage_kind is StorageKind.INTEGRITY_ONLY:
            raise SemanticEvidenceUnavailable(
                f"integrity-only evidence cannot have a parsed record: {path}"
            )
        if path in self._parsed_records:
            raise SnapshotStateError(f"parsed snapshot record already cached: {path}")
        self._parsed_records[path] = copy.deepcopy(value)

    def parsed_record(self, relative_path: str) -> Any:
        self._require_session_open()
        path = normalize_bundle_path(relative_path, label="parsed snapshot record")
        if path not in self._parsed_records:
            raise SnapshotStateError(f"parsed snapshot record is not cached: {path}")
        return copy.deepcopy(self._parsed_records[path])

    def _claim_session(self) -> None:
        if self._session_claimed:
            raise SnapshotStateError("verified snapshot already has an owning session")
        self._session_claimed = True

    def _activate_session(self) -> None:
        if self._session_closed:
            raise SnapshotStateError("closed verification session cannot be activated")
        self._session_active = True

    def _release_resources(self) -> tuple[CleanupDiagnostic, ...]:
        if self._session_closed:
            return tuple(self._cleanup_diagnostics)
        self._session_active = False
        self._session_closed = True
        for path in sorted(self._objects):
            try:
                self._objects[path].close()
            except Exception as exc:  # cleanup is best-effort and diagnostic only
                self._cleanup_diagnostics.append(
                    CleanupDiagnostic(
                        resource=path,
                        exception_type=type(exc).__name__,
                        message=str(exc),
                    )
                )
        self._parsed_records.clear()
        return tuple(self._cleanup_diagnostics)


class VerificationSession:
    """Explicit owner for one verified snapshot and all retained resources."""

    def __init__(self, snapshot: VerifiedBundleSnapshot) -> None:
        snapshot._claim_session()
        self._snapshot = snapshot
        self._state = SessionState.NEW

    @property
    def snapshot(self) -> VerifiedBundleSnapshot:
        return self._snapshot

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def cleanup_diagnostics(self) -> tuple[CleanupDiagnostic, ...]:
        return self._snapshot.cleanup_diagnostics

    def __enter__(self) -> VerificationSession:
        if self._state is SessionState.CLOSED:
            raise SnapshotStateError("closed verification session cannot be reopened")
        self._snapshot._activate_session()
        self._state = SessionState.OPEN
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.close()
        return False

    def close(self) -> None:
        if self._state is SessionState.CLOSED:
            return
        self._snapshot._release_resources()
        self._state = SessionState.CLOSED
