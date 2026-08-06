"""Human-readable report generation from evidence files."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .io import read_json


def _value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def generate_report(run_dir: str | Path) -> Path:
    directory = Path(run_dir).expanduser().resolve()
    names = ["run", "source", "environment", "inputs", "commands", "artifacts", "metrics", "verification"]
    evidence: dict[str, Any] = {}
    missing = []
    for name in names:
        path = directory / f"{name}.json"
        if not path.is_file():
            missing.append(path.name)
        else:
            evidence[name] = read_json(path)
    if missing:
        raise ConfigError(f"cannot report invalid evidence bundle; missing: {', '.join(missing)}")

    run = evidence["run"]
    source = evidence["source"]
    verification = evidence["verification"]
    lines = [
        f"# ReproTrace report: {run.get('project_name', run.get('run_id'))}",
        "",
        f"**Decision:** `{verification['status']}`  ",
        f"**Run ID:** `{run.get('run_id')}`  ",
        f"**Dry run:** {_value(run.get('dry_run'))}  ",
        f"**Started:** {_value(run.get('started_at'))}  ",
        f"**Finished:** {_value(run.get('finished_at'))}",
        "",
        "## Claim",
        "",
        f"- ID: `{run.get('claim', {}).get('id', 'unspecified')}`",
        f"- Source: {_value(run.get('claim', {}).get('source'))}",
        f"- Locator: {_value(run.get('claim', {}).get('locator'))}",
        f"- Protocol: {_value(run.get('claim', {}).get('protocol'))}",
        "",
        "## Source and environment",
        "",
        f"- Commit: `{_value(source.get('commit'))}`",
        f"- Branch: `{_value(source.get('branch'))}`",
        f"- Remote: {_value(source.get('remote'))}",
        f"- Dirty: {_value(source.get('dirty'))}",
        f"- Python: `{evidence['environment'].get('python')}`",
        f"- Platform: `{evidence['environment'].get('platform')}`",
        "",
        "## Commands",
        "",
        "| Step | Status | Exit | Elapsed (s) | Command |",
        "|---|---:|---:|---:|---|",
    ]
    for command in evidence["commands"]:
        argv = " ".join(shlex.quote(item) for item in command.get("argv", []))
        escaped_argv = argv.replace("|", "\\|")
        lines.append(
            f"| {command.get('step_id')} | {command.get('status')} | {_value(command.get('return_code'))} "
            f"| {_value(command.get('elapsed_seconds'))} | `{escaped_argv}` |"
        )

    lines.extend(["", "## Metrics", "", "| Metric | Actual | Expected | atol | rtol | Pass |", "|---|---:|---:|---:|---:|---:|"])
    if evidence["metrics"]:
        for metric in evidence["metrics"]:
            lines.append(
                f"| {metric['id']} | {metric.get('actual')} | {metric.get('expected')} | "
                f"{metric.get('atol')} | {metric.get('rtol')} | {_value(metric.get('passed'))} |"
            )
    else:
        lines.append("| — | — | — | — | — | — |")

    checks = verification.get("checks", [])
    passed = sum(1 for check in checks if check.get("passed"))
    lines.extend(
        [
            "",
            "## Verification",
            "",
            f"{passed}/{len(checks)} checks passed.",
            "",
        ]
    )
    for check in checks:
        marker = "PASS" if check.get("passed") else "FAIL"
        detail = check.get("reason") or ""
        lines.append(f"- **{marker}** `{check.get('id')}` {detail}".rstrip())

    lines.extend(
        [
            "",
            "## Evidence inventory",
            "",
            f"- Inputs: {len(evidence['inputs'])}",
            f"- Artifact declarations: {len(evidence['artifacts'])}",
            f"- Commands: {len(evidence['commands'])}",
            "",
            "This report describes recorded evidence. It does not extend the claim beyond the manifest's stated protocol.",
            "",
        ]
    )
    path = directory / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
