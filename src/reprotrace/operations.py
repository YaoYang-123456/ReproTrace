"""One-session verification and report orchestration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .derived_outputs import (
    _DerivedOutputLifecycleTestHooks,
    begin_derived_output_refresh,
    write_guarded_derived_bytes,
    write_guarded_derived_json,
    write_session_derived_bytes,
    write_session_derived_json,
)
from .reporting import render_report
from .snapshot import VerificationSession
from .verifier import (
    _open_schema_one_snapshot_for_production,
    _verify_legacy_directory,
    snapshot_report_records,
    verify_snapshot_session,
)


@dataclass(frozen=True, slots=True)
class BundleReportResult:
    verification: dict[str, Any]
    report_path: Path
    legacy_bundle: bool
    dry_run: bool


@dataclass(slots=True)
class _VerifyReportTestHooks:
    lifecycle: _DerivedOutputLifecycleTestHooks | None = None
    after_snapshot_open: Callable[[VerificationSession], None] | None = None
    after_verification_before_report: (
        Callable[[VerificationSession, Mapping[str, Any]], None] | None
    ) = None
    before_verification_write: (
        Callable[[VerificationSession, Mapping[str, Any]], None] | None
    ) = None
    before_report_write: (
        Callable[[VerificationSession, Mapping[str, Any], str], None] | None
    ) = None


def verify_and_report_bundle(
    run_dir: str | Path,
    *,
    _hooks: _VerifyReportTestHooks | None = None,
) -> BundleReportResult:
    """Verify and render one bundle without splitting schema-1 session authority."""

    directory = Path(run_dir).expanduser().absolute()
    hooks = _hooks or _VerifyReportTestHooks()
    with begin_derived_output_refresh(directory, _hooks=hooks.lifecycle) as lifecycle:
        session = _open_schema_one_snapshot_for_production(directory)
        if session is None:
            lifecycle.require_current_root()
            verification, evidence = _verify_legacy_directory(directory)
            report_text = render_report(evidence, verification)
            write_guarded_derived_json(lifecycle, "verification.json", verification)
            report_path = write_guarded_derived_bytes(
                lifecycle,
                "report.md",
                report_text.encode("utf-8"),
            )
            lifecycle.require_current_root()
            return BundleReportResult(
                verification=verification,
                report_path=report_path,
                legacy_bundle=True,
                dry_run=evidence["run"].get("dry_run") is True,
            )

        try:
            lifecycle.require_session_identity(session)
            if hooks.after_snapshot_open is not None:
                hooks.after_snapshot_open(session)
            verification = verify_snapshot_session(session)
            if hooks.after_verification_before_report is not None:
                hooks.after_verification_before_report(session, verification)
            evidence = snapshot_report_records(session)
            report_text = render_report(evidence, verification)
            if hooks.before_verification_write is not None:
                hooks.before_verification_write(session, verification)
            write_session_derived_json(
                session,
                lifecycle,
                "verification.json",
                verification,
            )
            if hooks.before_report_write is not None:
                hooks.before_report_write(session, verification, report_text)
            report_path = write_session_derived_bytes(
                session,
                lifecycle,
                "report.md",
                report_text.encode("utf-8"),
            )
            lifecycle.require_current_root()
            return BundleReportResult(
                verification=verification,
                report_path=report_path,
                legacy_bundle=False,
                dry_run=evidence["run"].get("dry_run") is True,
            )
        finally:
            session.close()
