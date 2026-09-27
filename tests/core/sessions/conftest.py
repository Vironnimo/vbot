"""Shared current-format data-directory setup for Session tests."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.database import write_bootstrap_marker
from core.sessions import ChatSessionManager


@pytest.fixture(autouse=True)
def current_format_data_directory(tmp_path: Path) -> None:
    """Authorize the pytest data root instead of relying on Runtime heuristics."""
    write_bootstrap_marker(tmp_path)


@pytest.fixture
def manager(tmp_path: Path, current_session_store_template: Path) -> Iterator[ChatSessionManager]:
    """A Session manager over a copy of the worker's empty current-format store."""
    for name in ("data-store.json", "sessions.db"):
        shutil.copy2(current_session_store_template / name, tmp_path)
    sessions = ChatSessionManager(tmp_path)
    yield sessions
    sessions.close()
