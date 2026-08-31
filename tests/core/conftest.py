"""Shared current-format data-directory setup for core tests."""

from __future__ import annotations

import pytest

from core.sessions.format import write_bootstrap_marker


@pytest.fixture(autouse=True)
def current_format_data_directory(tmp_path):
    """Authorize the pytest root before core services construct Sessions."""
    write_bootstrap_marker(tmp_path)
