from __future__ import annotations

import pytest

from reprotrace.protocol import join_protocol_path


@pytest.mark.parametrize(
    ("root", "child", "expected"),
    [
        (
            "/tmp/project/.evidence",
            "run-123",
            "/tmp/project/.evidence/run-123",
        ),
        (".reprotrace/runs", "run-123", ".reprotrace/runs/run-123"),
        (
            r"C:\project\.evidence",
            "run-123",
            r"C:\project\.evidence\run-123",
        ),
        (
            r"\\server\share\runs",
            "run-123",
            r"\\server\share\runs\run-123",
        ),
        (r"\rooted\runs", "run-123", r"\rooted\runs\run-123"),
        (r"C:runs", "run-123", r"C:runs\run-123"),
    ],
)
def test_join_protocol_path_preserves_recorded_path_style(
    root: str,
    child: str,
    expected: str,
) -> None:
    assert join_protocol_path(root, child) == expected
