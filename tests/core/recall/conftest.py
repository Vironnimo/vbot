"""Authorize temporary roots used by Recall integration tests."""

from __future__ import annotations

import pytest

from core.sessions.format import write_bootstrap_marker


@pytest.fixture(autouse=True)
def current_format_data_directory(tmp_path):
    write_bootstrap_marker(tmp_path)
