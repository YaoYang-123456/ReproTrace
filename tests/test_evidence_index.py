from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reprotrace.errors import ConfigError
from reprotrace.evidence import (
    EVIDENCE_INDEX_FILENAME,
    build_evidence_index,
    canonical_evidence_index_bytes,
    evidence_root_sha256,
    read_evidence_index,
    resolve_bundle_file,
    validate_evidence_index,
    write_evidence_index,
)
from reprotrace.io import sha256_bytes


def make_bundle(root: Path) -> Path:
    bundle = root / "bundle"
    (bundle / "logs").mkdir(parents=True)
    (bundle / "logs" / "step.stdout.log").write_bytes(b"stdout\n")
    (bundle / "source.patch").write_bytes(b"diff bytes\n")
    return bundle


def declarations() -> list[dict[str, object]]:
    return [
        {"path": "source.patch", "roles": ["source_patch"]},
        {"path": "logs/step.stdout.log", "roles": ["text_log", "command_log"]},
    ]


def test_canonical_serialization_is_deterministic(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    index = build_evidence_index(bundle, declarations())
    encoded = canonical_evidence_index_bytes(index)

    assert encoded == canonical_evidence_index_bytes(index)
    assert encoded == json.dumps(
        index,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert [entry["path"] for entry in index["entries"]] == [
        "logs/step.stdout.log",
        "source.patch",
    ]
    assert index["entries"][0]["roles"] == ["command_log", "text_log"]
    assert evidence_root_sha256(index) == sha256_bytes(encoded)


def test_logical_insertion_order_does_not_change_bytes_or_root(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    first = build_evidence_index(bundle, declarations())
    second_declarations = list(reversed(declarations()))
    second_declarations[0]["roles"] = ["command_log", "text_log"]
    second = build_evidence_index(bundle, second_declarations)

    assert canonical_evidence_index_bytes(first) == canonical_evidence_index_bytes(second)
    assert evidence_root_sha256(first) == evidence_root_sha256(second)


def test_write_and_read_use_exact_canonical_bytes(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    index, root_hash = write_evidence_index(bundle, declarations())

    encoded = (bundle / EVIDENCE_INDEX_FILENAME).read_bytes()
    assert encoded == canonical_evidence_index_bytes(index)
    assert root_hash == sha256_bytes(encoded)
    assert read_evidence_index(bundle) == index


def test_file_modification_fails_index_validation(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    index = build_evidence_index(bundle, declarations())
    (bundle / "source.patch").write_bytes(b"changed")

    validation = validate_evidence_index(bundle, index)

    assert validation["valid"] is False
    assert next(check for check in validation["checks"] if check["path"] == "source.patch")[
        "passed"
    ] is False


def test_missing_file_fails_index_validation(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    index = build_evidence_index(bundle, declarations())
    (bundle / "source.patch").unlink()

    validation = validate_evidence_index(bundle, index)

    check = next(check for check in validation["checks"] if check["path"] == "source.patch")
    assert validation["valid"] is False
    assert check["passed"] is False
    assert check["current"]["exists"] is False


def test_duplicate_normalized_path_is_rejected(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)

    with pytest.raises(ConfigError, match="duplicate normalized evidence path"):
        build_evidence_index(
            bundle,
            [
                {"path": "logs/step.stdout.log", "roles": ["command_log"]},
                {"path": "logs/./step.stdout.log", "roles": ["text_log"]},
            ],
        )


@pytest.mark.parametrize(
    "unsafe",
    [
        "",
        "../outside",
        "/absolute",
        "C:\\outside",
        "C:outside",
        "\\\\server\\share\\outside",
    ],
)
def test_bundle_resolver_rejects_unsafe_paths(tmp_path: Path, unsafe: str) -> None:
    bundle = make_bundle(tmp_path)

    with pytest.raises(ConfigError, match="invalid indexed evidence path"):
        resolve_bundle_file(bundle, unsafe, label="indexed evidence")


def test_bundle_resolver_requires_regular_file(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)

    with pytest.raises(ConfigError, match="expected a regular file"):
        resolve_bundle_file(bundle, "logs", label="indexed evidence")


def test_final_symlink_escape_is_rejected(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    outside = tmp_path / "outside.log"
    outside.write_bytes(b"outside")
    link = bundle / "linked.log"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(ConfigError, match="path escapes the evidence bundle"):
        resolve_bundle_file(bundle, "linked.log", label="indexed evidence")


def test_parent_symlink_escape_is_rejected(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evidence.log").write_bytes(b"outside")
    link = bundle / "linked-directory"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation unavailable: {exc}")

    with pytest.raises(ConfigError, match="path escapes the evidence bundle"):
        resolve_bundle_file(bundle, "linked-directory/evidence.log", label="indexed evidence")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_parent_junction_escape_is_rejected(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    outside = tmp_path / "junction-target"
    outside.mkdir()
    (outside / "evidence.log").write_bytes(b"outside")
    junction = bundle / "junction"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        check=False,
        capture_output=True,
        text=False,
    )
    assert created.returncode == 0, created.stderr.decode("utf-8", errors="backslashreplace")
    try:
        with pytest.raises(ConfigError, match="path escapes the evidence bundle"):
            resolve_bundle_file(bundle, "junction/evidence.log", label="indexed evidence")
    finally:
        os.rmdir(junction)


def test_evidence_root_is_stable_after_bundle_relocation(tmp_path: Path) -> None:
    first = make_bundle(tmp_path / "first-location")
    second = make_bundle(tmp_path / "different-location")

    first_index = build_evidence_index(first, declarations())
    second_index = build_evidence_index(second, declarations())

    assert canonical_evidence_index_bytes(first_index) == canonical_evidence_index_bytes(second_index)
    assert evidence_root_sha256(first_index) == evidence_root_sha256(second_index)


@pytest.mark.parametrize("excluded", ["evidence.index.json", "verification.json", "report.md"])
def test_self_derived_files_cannot_enter_index(tmp_path: Path, excluded: str) -> None:
    bundle = make_bundle(tmp_path)
    (bundle / excluded).write_bytes(b"self-derived")

    with pytest.raises(ConfigError, match="must not include self-derived file"):
        build_evidence_index(bundle, [{"path": f"./{excluded}", "roles": ["metadata"]}])


def test_index_metadata_tamper_fails_validation(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    index, original_root = write_evidence_index(bundle, declarations())
    tampered = json.loads(canonical_evidence_index_bytes(index).decode("utf-8"))
    tampered["entries"][0]["sha256"] = "0" * 64
    (bundle / EVIDENCE_INDEX_FILENAME).write_bytes(canonical_evidence_index_bytes(tampered))

    loaded = read_evidence_index(bundle)
    validation = validate_evidence_index(bundle, loaded)

    assert validation["valid"] is False
    assert validation["evidence_root_sha256"] != original_root


def test_noncanonical_index_serialization_is_rejected(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    index = build_evidence_index(bundle, declarations())
    (bundle / EVIDENCE_INDEX_FILENAME).write_text(
        json.dumps(index, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="serialization is not canonical"):
        read_evidence_index(bundle)
