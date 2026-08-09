"""Guarded lifecycle and publication for canonical derived outputs."""

from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .acquisition import (
    EvidenceAcquisitionError,
    _identity_from_stat,
    capture_bundle_root_identity,
)
from .errors import ConfigError
from .snapshot import BundleRootIdentity, SessionState, VerificationSession


DERIVED_OUTPUT_INVALIDATION_ORDER = ("verification.json", "report.md")
DERIVED_OUTPUT_PATHS = frozenset(DERIVED_OUTPUT_INVALIDATION_ORDER)


@dataclass(slots=True)
class _DerivedOutputLifecycleTestHooks:
    after_identity_capture: (
        Callable[[Path, BundleRootIdentity], None] | None
    ) = None
    after_guard_capture: Callable[[DerivedOutputLifecycleGuard], None] | None = None
    before_canonical_unlink: (
        Callable[[DerivedOutputLifecycleGuard, str], None] | None
    ) = None
    after_output_invalidated: (
        Callable[[DerivedOutputLifecycleGuard, str], None] | None
    ) = None
    after_invalidation: Callable[[DerivedOutputLifecycleGuard], None] | None = None
    before_temp_create: (
        Callable[[DerivedOutputLifecycleGuard, str], None] | None
    ) = None
    before_atomic_replace: (
        Callable[[DerivedOutputLifecycleGuard, str, str], None] | None
    ) = None


@dataclass(slots=True)
class DerivedOutputLifecycleGuard:
    """Resource-owning mutation authority for the two canonical outputs."""

    run_dir: Path
    root_identity: BundleRootIdentity
    _mutation_root: Path
    _directory_fd: int | None
    _use_dir_fd: bool
    _hooks: _DerivedOutputLifecycleTestHooks

    @property
    def closed(self) -> bool:
        return self._directory_fd is None

    def fileno(self) -> int:
        self._require_open()
        assert self._directory_fd is not None
        return self._directory_fd

    def close(self) -> None:
        descriptor = self._directory_fd
        self._directory_fd = None
        if descriptor is not None:
            os.close(descriptor)

    def _require_open(self) -> None:
        if self.closed:
            raise ConfigError("derived output lifecycle authority is closed")

    def _entry_path(self, relative_path: str) -> Path:
        _require_canonical_output_name(relative_path)
        return self._mutation_root / relative_path

    def require_current_root(self) -> None:
        self._require_open()
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

    def lstat_canonical(self, relative_path: str) -> os.stat_result:
        """Inspect a final canonical entry without following it."""

        _require_canonical_output_name(relative_path)
        descriptor = self.fileno()
        if self._use_dir_fd:
            return os.stat(
                relative_path,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        return os.lstat(self._entry_path(relative_path))

    def unlink_canonical(self, relative_path: str) -> None:
        """Unlink one canonical entry through the pinned root authority."""

        _require_canonical_output_name(relative_path)
        descriptor = self.fileno()
        if self._use_dir_fd:
            os.unlink(relative_path, dir_fd=descriptor)
            return
        os.unlink(self._entry_path(relative_path))

    def _open_temporary(self, relative_path: str) -> tuple[int, str]:
        descriptor = self.fileno()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        for _ in range(128):
            temporary_name = f".{relative_path}.{secrets.token_hex(8)}.tmp"
            try:
                if self._use_dir_fd:
                    temporary_fd = os.open(
                        temporary_name,
                        flags,
                        0o600,
                        dir_fd=descriptor,
                    )
                else:
                    temporary_fd = os.open(
                        self._mutation_root / temporary_name,
                        flags,
                        0o600,
                    )
            except FileExistsError:
                continue
            return temporary_fd, temporary_name
        raise ConfigError(
            f"cannot allocate unique temporary derived output for {relative_path}"
        )

    def _replace_temporary(self, temporary_name: str, relative_path: str) -> None:
        descriptor = self.fileno()
        if self._use_dir_fd:
            os.replace(
                temporary_name,
                relative_path,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
            return
        os.replace(
            self._mutation_root / temporary_name,
            self._entry_path(relative_path),
        )

    def _cleanup_temporary(self, temporary_name: str) -> None:
        descriptor = self.fileno()
        try:
            if self._use_dir_fd:
                os.unlink(temporary_name, dir_fd=descriptor)
            else:
                os.unlink(self._mutation_root / temporary_name)
        except FileNotFoundError:
            pass

    def write_bytes_atomic(self, relative_path: str, value: bytes) -> Path:
        """Atomically publish exact bytes through the pinned root authority."""

        _require_canonical_output_name(relative_path)
        if not isinstance(value, bytes):
            raise TypeError("derived output payload must be bytes")

        self.require_current_root()
        _run_lifecycle_hook(self._hooks.before_temp_create, self, relative_path)
        temporary_fd: int | None = None
        temporary_name: str | None = None
        try:
            temporary_fd, temporary_name = self._open_temporary(relative_path)
            with os.fdopen(temporary_fd, "wb") as handle:
                temporary_fd = None
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())

            self.require_current_root()
            _run_lifecycle_hook(
                self._hooks.before_atomic_replace,
                self,
                temporary_name,
                relative_path,
            )
            self._replace_temporary(temporary_name, relative_path)
            temporary_name = None
            self.require_current_root()
        except OSError as exc:
            raise ConfigError(
                f"cannot publish canonical derived output {relative_path}: {exc}"
            ) from exc
        finally:
            if temporary_fd is not None:
                try:
                    os.close(temporary_fd)
                except OSError:
                    pass
            if temporary_name is not None:
                try:
                    self._cleanup_temporary(temporary_name)
                except OSError:
                    pass
        return self.run_dir / relative_path

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


def _require_canonical_output_name(relative_path: str) -> None:
    if relative_path not in DERIVED_OUTPUT_PATHS:
        raise ValueError(f"unsupported derived output path: {relative_path!r}")


def _open_windows_directory_authority(directory: Path) -> int:
    import ctypes
    import ctypes.wintypes as wintypes
    import msvcrt

    file_list_directory = 0x0001
    file_read_attributes = 0x0080
    file_share_read = 0x0001
    file_share_write = 0x0002
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    invalid_handle_value = ctypes.c_void_p(-1).value

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(directory),
        # Directory-list access makes Windows enforce this handle's sharing mode
        # for root rename/delete. Omitting FILE_SHARE_DELETE is intentional;
        # child reads and writes remain available through their own handles.
        file_list_directory | file_read_attributes,
        file_share_read | file_share_write,
        None,
        open_existing,
        file_flag_backup_semantics,
        None,
    )
    if handle == invalid_handle_value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(
            int(handle),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        close_handle(handle)
        raise


def _acquire_mutation_authority(
    directory: Path,
    root_identity: BundleRootIdentity,
    hooks: _DerivedOutputLifecycleTestHooks,
) -> DerivedOutputLifecycleGuard:
    _run_lifecycle_hook(hooks.after_identity_capture, directory, root_identity)
    try:
        mutation_root = directory.resolve(strict=True)
        if os.name == "nt":
            descriptor = _open_windows_directory_authority(mutation_root)
            use_dir_fd = False
        else:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            descriptor = os.open(mutation_root, flags)
            use_dir_fd = True
    except (OSError, RuntimeError) as exc:
        raise ConfigError(
            f"cannot acquire bundle-root mutation authority: {exc}"
        ) from exc

    try:
        opened_identity = BundleRootIdentity(_identity_from_stat(os.fstat(descriptor)))
        if not opened_identity.file_identity.available:
            raise ConfigError(
                "cannot acquire bundle-root mutation authority; opened root "
                "identity is unavailable"
            )
        if opened_identity != root_identity:
            raise ConfigError(
                "cannot acquire bundle-root mutation authority; opened root identity "
                "differs from the operation-start identity"
            )
        return DerivedOutputLifecycleGuard(
            run_dir=directory,
            root_identity=root_identity,
            _mutation_root=mutation_root,
            _directory_fd=descriptor,
            _use_dir_fd=use_dir_fd,
            _hooks=hooks,
        )
    except BaseException:
        os.close(descriptor)
        raise


def _is_windows_reparse_point(status: os.stat_result) -> bool:
    attributes = getattr(status, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _invalidate_canonical_output(
    guard: DerivedOutputLifecycleGuard,
    relative_path: str,
) -> None:
    guard.require_current_root()
    try:
        status = guard.lstat_canonical(relative_path)
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
    _run_lifecycle_hook(guard._hooks.before_canonical_unlink, guard, relative_path)
    try:
        guard.unlink_canonical(relative_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ConfigError(
            f"cannot invalidate canonical derived output {relative_path}: {exc}"
        ) from exc
    guard.require_current_root()


@contextmanager
def begin_derived_output_refresh(
    run_dir: str | Path,
    *,
    _hooks: _DerivedOutputLifecycleTestHooks | None = None,
) -> Iterator[DerivedOutputLifecycleGuard]:
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

    hooks = _hooks or _DerivedOutputLifecycleTestHooks()
    guard = _acquire_mutation_authority(directory, root_identity, hooks)
    try:
        _run_lifecycle_hook(hooks.after_guard_capture, guard)
        for relative_path in DERIVED_OUTPUT_INVALIDATION_ORDER:
            _invalidate_canonical_output(guard, relative_path)
            _run_lifecycle_hook(hooks.after_output_invalidated, guard, relative_path)

        guard.require_current_root()
        for relative_path in DERIVED_OUTPUT_INVALIDATION_ORDER:
            try:
                guard.lstat_canonical(relative_path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ConfigError(
                    "cannot confirm canonical derived output absence "
                    f"{relative_path}: {exc}"
                ) from exc
            raise ConfigError(
                f"canonical derived output remains after invalidation: {relative_path}"
            )
        guard.require_current_root()
        _run_lifecycle_hook(hooks.after_invalidation, guard)
        yield guard
    finally:
        guard.close()


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
    guard: DerivedOutputLifecycleGuard,
) -> None:
    """Bind an active schema-1 session to the lifecycle mutation authority."""

    if not isinstance(session, VerificationSession):
        raise TypeError("derived output write requires a VerificationSession")
    if not isinstance(guard, DerivedOutputLifecycleGuard):
        raise TypeError("derived output write requires a lifecycle guard")
    if session.state is not SessionState.OPEN or not session.snapshot.session_active:
        raise ConfigError("cannot write derived output from an inactive verification session")
    if not session.snapshot.sealed:
        raise ConfigError("cannot write derived output before snapshot sealing")
    session.snapshot.require_established_evidence_root()
    expected = session.snapshot.root_metadata.identity
    if expected is None or not expected.file_identity.available:
        raise ConfigError("cannot write derived output; captured bundle root identity is unavailable")
    if expected != guard.root_identity:
        raise ConfigError(
            "cannot write derived output; verification session bundle root identity "
            "differs from the lifecycle authority"
        )
    guard.require_current_root()


def write_session_derived_bytes(
    session: VerificationSession,
    guard: DerivedOutputLifecycleGuard,
    relative_path: str,
    value: bytes,
) -> Path:
    """Publish through one session-bound lifecycle mutation authority."""

    ensure_session_root_identity(session, guard)
    return guard.write_bytes_atomic(relative_path, value)


def write_session_derived_json(
    session: VerificationSession,
    guard: DerivedOutputLifecycleGuard,
    relative_path: str,
    value: Any,
) -> Path:
    """Serialize strict JSON and write it as a root-identity-bound output."""

    encoded = _encode_derived_json(relative_path, value)
    return write_session_derived_bytes(
        session,
        guard,
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
    return guard.write_bytes_atomic(relative_path, value)


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
