"""Recall tests start from the current empty store and share one Session manager."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.sessions import ChatSessionManager


@pytest.fixture(autouse=True)
def current_format_data_directory(tmp_path: Path, current_session_store_template: Path) -> None:
    """Clone the current empty store; Recall tests do not exercise schema bootstrap."""
    shutil.copy2(current_session_store_template / "data-store.json", tmp_path)
    shutil.copy2(current_session_store_template / "sessions.db", tmp_path)


@pytest.fixture
def sessions(tmp_path: Path, current_format_data_directory: None) -> Iterator[ChatSessionManager]:
    manager = ChatSessionManager(tmp_path)
    yield manager
    manager.close()
