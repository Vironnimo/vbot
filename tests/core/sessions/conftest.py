"""Shared explicit current-format data-directory setup for Session tests."""

from __future__ import annotations

import pytest

from core.sessions.format import write_bootstrap_marker


@pytest.fixture(autouse=True)
def current_format_data_directory(tmp_path):
    """Authorize the pytest data root instead of relying on Runtime heuristics."""
    write_bootstrap_marker(tmp_path)
