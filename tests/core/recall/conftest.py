"""Authorize temporary roots used by Recall integration tests."""

from __future__ import annotations

import shutil

import pytest


@pytest.fixture(autouse=True)
def current_format_data_directory(tmp_path, current_session_store_template):
    """Clone the current empty store; Recall tests do not exercise schema bootstrap."""
    shutil.copy2(current_session_store_template / "session-store.json", tmp_path)
    shutil.copy2(current_session_store_template / "sessions.db", tmp_path)
