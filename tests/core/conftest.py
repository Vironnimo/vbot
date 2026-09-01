"""Shared current-format data-directory setup for core tests."""

from __future__ import annotations

import shutil

import pytest

from core.sessions import ChatSessionManager
from core.sessions.format import write_bootstrap_marker


@pytest.fixture(scope="session")
def current_session_store_template(tmp_path_factory):
    """Build one empty current-format store per test worker."""
    template = tmp_path_factory.mktemp("current-session-store")
    write_bootstrap_marker(template)
    manager = ChatSessionManager(template)
    manager.close()
    return template


@pytest.fixture
def current_format_data_directory(tmp_path, current_session_store_template):
    """Clone an empty current-format store for tests that consume Sessions."""
    shutil.copy2(current_session_store_template / "session-store.json", tmp_path)
    shutil.copy2(current_session_store_template / "sessions.db", tmp_path)
