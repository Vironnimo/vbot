"""Authorize isolated Runtime data directories for direct Runtime tests."""

from __future__ import annotations

import shutil

import pytest


def _clone_current_store(template, destination) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template / "session-store.json", destination / "session-store.json")
    shutil.copy2(template / "sessions.db", destination / "sessions.db")


@pytest.fixture(autouse=True)
def current_format_runtime_data_directory(tmp_path, current_session_store_template):
    """Clone the empty store for conventional Runtime roots used by tests."""
    _clone_current_store(current_session_store_template, tmp_path)
    _clone_current_store(current_session_store_template, tmp_path / "data")
