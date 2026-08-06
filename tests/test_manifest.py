from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reprotrace.errors import ConfigError
from reprotrace.manifest import load_manifest


def test_rejects_shell_string(tmp_path: Path) -> None:
    manifest = {
        "schema_version": 0,
        "project": {"name": "unsafe", "root": "."},
        "run": {"steps": [{"id": "bad", "argv": "echo unsafe"}]},
    }
    path = tmp_path / "reprotrace.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    with pytest.raises(ConfigError, match="list of strings"):
        load_manifest(path)


def test_project_root_override(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    path = adapter / "reprotrace.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 0,
                "project": {"name": "portable", "root": "/does/not/exist"},
                "run": {
                    "output_root": ".evidence",
                    "steps": [{"id": "ok", "argv": ["python", "-V"]}],
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_manifest(path, project_root=checkout)
    assert loaded.project_root == checkout.resolve()
    assert loaded.output_root == (adapter / ".evidence").resolve()


def test_rejects_step_id_path_traversal(tmp_path: Path) -> None:
    path = tmp_path / "reprotrace.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 0,
                "project": {"name": "unsafe", "root": "."},
                "run": {"steps": [{"id": "../escape", "argv": ["python", "-V"]}]},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unsafe characters"):
        load_manifest(path)
