"""Authorize isolated Runtime data directories for direct Runtime tests."""

from __future__ import annotations

import pytest

from core.sessions.format import write_bootstrap_marker


@pytest.fixture(autouse=True)
def current_format_runtime_data_directory(tmp_path):
    """Prepare the conventional nested Runtime data root before startup."""
    data_dir = tmp_path / "data"
    if data_dir.exists():
        write_bootstrap_marker(data_dir)
