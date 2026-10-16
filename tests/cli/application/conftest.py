"""Fixtures shared by the packaged application tests."""

from __future__ import annotations

import pytest

from cli.application import operations


@pytest.fixture(autouse=True)
def _outside_foreign_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every update request as if no foreign Windows Job held the test process.

    Test runners often run inside one (a coding agent's shell, a CI runner); the
    real inspection would make requests start a tray through WMI. Tests of that
    path patch ``operations.foreign_job`` themselves.
    """

    monkeypatch.setattr(operations, "foreign_job", lambda: None)
