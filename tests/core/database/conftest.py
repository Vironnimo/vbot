"""Shared fixtures for kernel tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.database import write_bootstrap_marker


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A freshly initialized data directory that authorizes every canonical database."""
    root = tmp_path / "data"
    root.mkdir()
    write_bootstrap_marker(root)
    return root
