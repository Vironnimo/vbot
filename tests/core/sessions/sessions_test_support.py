"""Shared helpers for Session behavior tests; the ``manager`` fixture is in conftest."""

from __future__ import annotations

from core.sessions import SessionAddress


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
