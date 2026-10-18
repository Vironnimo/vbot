"""Shared resolution workers and exact Session override/binding rules."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.projects._runtime_agent import AgentOverrides, AgentResolutionError, RuntimeAgent
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.agents.temporary import TemporaryAgent
    from core.sessions import SessionAddress


# Project Agent resolution and the Project and Model checks applied to other
# Agents read Project, repository and configuration files; the async variants
# run them here. Identity Agent reads and temporary bindings run on the Session
# database's pool instead. The first resolution in a Project whose Team is not
# cached yet also lists its Session-owning Agents here, for the scan report.
_RESOLUTION_WORKERS = BoundedWorkerPool(name="agent-resolution", max_workers=4)


def _session_address(project_id: str | None, agent_id: str, session_id: str) -> SessionAddress:
    from core.sessions import SessionAddress

    return SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)


def _require_one_override_source(
    session_id: str | None, new_session_overrides: AgentOverrides | None
) -> None:
    if session_id is not None and new_session_overrides is not None:
        raise ValueError("an existing Session already has its own Agent overrides")


def _require_temporary_binding(agent: TemporaryAgent | None) -> TemporaryAgent:
    if agent is None:
        raise AgentResolutionError("temporary Session binding is unavailable")
    return agent


def _skill_binding_key(agent: RuntimeAgent) -> str | None:
    """Return the Session metadata key binding *agent* to a Skill subject, if any.

    Only Sessions of the Librarian have one.
    """
    from core.agents import SKILL_AGENT_ID_KEY, is_librarian

    return SKILL_AGENT_ID_KEY if is_librarian(agent) else None
