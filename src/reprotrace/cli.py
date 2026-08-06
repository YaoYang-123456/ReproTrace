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
            print(run_dir)
            if args.dry_run:
                return 0 if verification["preflight_passed"] else 1
            return 0 if verification["passed"] else 1
        if args.command == "verify":
            verification = verify_bundle(args.run_dir)
            generate_report(args.run_dir)
            if args.json:
                print(json.dumps(verification, indent=2, sort_keys=True))
            else:
                print(f"{verification['status']}: {Path(args.run_dir).resolve()}")
            return 0 if verification["passed"] else 1
        if args.command == "report":
            print(generate_report(args.run_dir))
            return 0
        if args.command == "diff":
            result = compare_bundles(args.left, args.right)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["identical"] else 1
    except (ConfigError, ReproTraceError) as exc:
        print(f"reprotrace: {exc}", file=sys.stderr)
        return 2
    return 2
