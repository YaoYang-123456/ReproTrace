"""Command-line interface for ReproTrace."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .diffing import compare_bundles
from .errors import ConfigError, ReproTraceError
from .io import read_json
from .reporting import generate_report
from .runner import run_manifest
from .verifier import verify_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reprotrace", description="Evidence-first research execution traces")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="execute a manifest and record an evidence bundle")
    run.add_argument("manifest")
    run.add_argument("--dry-run", action="store_true", help="record a resolved plan without executing it")
    run.add_argument("--seed", type=int, help="override run.seed")
    run.add_argument("--project-root", help="override project.root (useful for reusable manifests)")

    verify = subparsers.add_parser("verify", help="re-check a recorded evidence bundle")
    verify.add_argument("run_dir")
    verify.add_argument("--json", action="store_true", help="print the full verification result")

    report = subparsers.add_parser("report", help="regenerate the Markdown report")
    report.add_argument("run_dir")

    diff = subparsers.add_parser("diff", help="compare two evidence bundles")
    diff.add_argument("left")
    diff.add_argument("right")
    return parser


def _contract_display(value: object) -> str:
    return str(value).replace("_", " ").upper()


def _print_verification_summary(
    verification: dict[str, object],
    bundle: str | Path,
) -> None:
    not_established = verification.get("not_established", {})
    if not isinstance(not_established, dict):
        not_established = {}
    evidence_root = verification.get("evidence_root_sha256")
    print(f"verification: {_contract_display(verification.get('verification_status'))}")
    print(f"checks: {'PASS' if verification.get('checks_passed') is True else 'FAIL'}")
    print(f"assurance: {verification.get('assurance_level')}")
    print(f"recorded execution: {verification.get('execution_record_status')}")
    print(f"declared result: {verification.get('result_status')}")
    print(
        "execution authenticity: "
        f"{_contract_display(not_established.get('execution_authenticity', 'not_established'))}"
    )
    print(
        "independent replay: "
        f"{_contract_display(not_established.get('independent_replay', 'not_performed'))}"
    )
    print(
        "scientific reproduction: "
        f"{_contract_display(not_established.get('scientific_reproduction', 'not_established'))}"
    )
    print(f"evidence root: {evidence_root if evidence_root else 'NOT VERIFIED'}")
    print(f"bundle: {Path(bundle).expanduser().resolve()}")


def _is_legacy_bundle(run_dir: str | Path) -> bool:
    run = read_json(Path(run_dir).expanduser().resolve() / "run.json")
    return isinstance(run, dict) and run.get("schema_version", 0) == 0


def verification_exit_code(
    verification: dict[str, object],
    *,
    dry_run: bool,
    legacy_bundle: bool = False,
) -> int:
    """Map verified semantics to a CLI result without treating `passed` as canonical."""

    if dry_run:
        return 0 if verification.get("preflight_passed") is True else 1
    if (
        verification.get("verification_status") != "complete"
        or verification.get("checks_passed") is not True
    ):
        return 1
    if legacy_bundle:
        compatibility = verification.get("compatibility")
        return (
            0
            if isinstance(compatibility, dict)
            and compatibility.get("legacy_passed") is True
            else 1
        )
    if verification.get("execution_record_status") != "recorded_success":
        return 1
    return 0 if verification.get("result_status") in {"matched", "not_evaluated"} else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            run_dir, verification = run_manifest(
                args.manifest,
                dry_run=args.dry_run,
                seed=args.seed,
                project_root=args.project_root,
            )
            _print_verification_summary(verification, run_dir)
            return verification_exit_code(verification, dry_run=args.dry_run)
        if args.command == "verify":
            verification = verify_bundle(args.run_dir)
            generate_report(args.run_dir, _verification=verification)
            if args.json:
                print(json.dumps(verification, indent=2, sort_keys=True))
            else:
                _print_verification_summary(verification, args.run_dir)
            return verification_exit_code(
                verification,
                dry_run=False,
                legacy_bundle=_is_legacy_bundle(args.run_dir),
            )
        if args.command == "report":
            verification = verify_bundle(args.run_dir)
            report_path = generate_report(args.run_dir, _verification=verification)
            _print_verification_summary(verification, args.run_dir)
            print(f"report: {report_path}")
            return verification_exit_code(
                verification,
                dry_run=verification.get("execution_record_status") == "not_run",
                legacy_bundle=_is_legacy_bundle(args.run_dir),
            )
        if args.command == "diff":
            result = compare_bundles(args.left, args.right)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["identical"] else 1
    except (ConfigError, ReproTraceError) as exc:
        print(f"reprotrace: {exc}", file=sys.stderr)
        return 2
    return 2
