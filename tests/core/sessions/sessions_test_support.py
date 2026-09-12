"""Shared fixtures and fakes for sessions behavior tests."""

from __future__ import annotations

import shutil

import pytest

from core.sessions import (
    ChatSessionManager,
    SessionAddress,
)


def _address(agent_id: str, session_id: str, project_id: str | None = None) -> SessionAddress:
    return SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)


def _continuation_start() -> dict[str, object]:
    return {
        "version": 1,
        "type": "run_started",
        "checkpoint_id": "checkpoint-one",
        "run_id": "run-one",
        "origin_run_id": "run-one",
        "timestamp": "2026-08-31T12:00:00+00:00",
        "request": "continue this work",
    }


@pytest.fixture
def manager(tmp_path, current_session_store_template):
    shutil.copy2(current_session_store_template / "session-store.json", tmp_path)
    shutil.copy2(current_session_store_template / "sessions.db", tmp_path)
    sessions = ChatSessionManager(tmp_path)
    yield sessions
    sessions.close()
