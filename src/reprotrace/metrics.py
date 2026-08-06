"""Safe metric extraction from text evidence."""

from __future__ import annotations

import csv
import glob
import math
import re
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError
from .manifest import LoadedManifest, substitute


def _matches(value: str, manifest: LoadedManifest, context: Mapping[str, Any]) -> list[Path]:
    resolved = Path(substitute(value, context)).expanduser()
    if not resolved.is_absolute():
        resolved = manifest.project_root / resolved
    return sorted(Path(match).resolve() for match in glob.glob(str(resolved), recursive=True) if Path(match).is_file())


def _select(values: list[float], selector: str) -> float:
    if not values:
        raise ConfigError("metric extractor found no numeric values")
    if selector == "last":
        return values[-1]
    if selector == "max":
        return max(values)
    if selector == "min":
        return min(values)
    raise ConfigError(f"unsupported metric selector: {selector}")


def _csv_values(paths: list[Path], column: str) -> list[float]:
    values: list[float] = []
    for path in paths:
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None or column not in reader.fieldnames:
                    raise ConfigError(f"CSV metric column {column!r} not found in {path}")
                for row in reader:
                    raw = row.get(column)
                    if raw not in (None, ""):
                        values.append(float(raw))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"cannot extract CSV metric from {path}: {exc}") from exc
    return values


def _regex_values(paths: list[Path], pattern: str, group: int | str) -> list[float]:
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"invalid metric regular expression: {exc}") from exc
    values: list[float] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ConfigError(f"cannot read metric log {path}: {exc}") from exc
        for match in compiled.finditer(text):
            try:
                values.append(float(match.group(group)))
            except (IndexError, KeyError, ValueError) as exc:
                raise ConfigError(f"cannot parse regex group {group!r} as a number in {path}") from exc
    return values


def extract_metrics(manifest: LoadedManifest, context: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for specification in manifest.data.get("metrics", []):
        paths = _matches(specification["path"], manifest, context)
        if not paths:
            raise ConfigError(f"metric {specification['id']!r} matched no files: {specification['path']}")
        if specification["extractor"] == "csv":
            values = _csv_values(paths, specification["column"])
        else:
            values = _regex_values(paths, specification["pattern"], specification.get("group", 1))
        actual = _select(values, specification.get("select", "last"))
        expected = float(specification["expected"])
        atol = float(specification.get("atol", 0.0))
        rtol = float(specification.get("rtol", 0.0))
        passed = math.isclose(actual, expected, abs_tol=atol, rel_tol=rtol)
        records.append(
            {
                "id": specification["id"],
                "extractor": specification["extractor"],
                "source_paths": [str(path) for path in paths],
                "select": specification.get("select", "last"),
                "sample_count": len(values),
                "actual": actual,
                "expected": expected,
                "atol": atol,
                "rtol": rtol,
                "absolute_error": abs(actual - expected),
                "passed": passed,
            }
        )
    return records
