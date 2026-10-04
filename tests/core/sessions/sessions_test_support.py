"""Shared helpers for Session behavior tests; the ``manager`` fixture is in conftest."""

from __future__ import annotations

from core.sessions import SessionAddress


def _address(agent_id: str, session_id: str, project_id: str | None = None) -> SessionAddress:
    return SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
