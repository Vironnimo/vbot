"""The durable Sub-Agent tree, read from the Parent link of each Sub-Agent Session.

A Sub-Agent Session stores its Parent link in Session metadata
(``subagent_parent``): the public Sub-Agent id and the Parent Session's address.
Every question about the tree (who is whose child, how deep a Session is, which
Sub-Agent an id names) is answered from those links, so the tree survives
restarts and needs no in-memory registry. All functions here are blocking.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.sessions import (
    SUBAGENT_PARENT_META_KEY,
    SUBAGENT_SESSION_META_KEY,
    SUBAGENT_TAKEN_OVER_AT_META_KEY,
    SessionAddress,
)

if TYPE_CHECKING:
    from core.sessions import ChatSessionManager

# Bounds every walk, so a corrupted link chain or an unexpectedly wide tree
# cannot stall a Tool call.
_MAX_TREE_SIZE = 1_000
_MAX_CHAIN_LENGTH = 64
_TITLE_KEYS = ("title", "auto_title")


@dataclass(frozen=True)
class SubAgentLink:
    """One Sub-Agent Session and its link to the Parent Session."""

    id: str
    session: SessionAddress
    parent: SessionAddress
    title: str
    taken_over_at: str | None


def read_link(sessions: ChatSessionManager, address: SessionAddress) -> SubAgentLink | None:
    """Return the Parent link of *address*, or ``None`` for a Session without one."""
    try:
        metadata = sessions.get_metadata(address)
    except Exception:
        return None
    return _link_from_metadata(address, metadata)


def find_link(sessions: ChatSessionManager, subagent_id: str) -> SubAgentLink | None:
    """Return the live Sub-Agent Session with public id *subagent_id*."""
    address = sessions.subagent_session(subagent_id)
    return read_link(sessions, address) if address is not None else None


def children(sessions: ChatSessionManager, parent: SessionAddress) -> list[SubAgentLink]:
    """Return the Sub-Agents linked directly to *parent*, oldest first."""
    links = (read_link(sessions, address) for address in sessions.subagent_children(parent))
    return [link for link in links if link is not None and link.parent == parent]


def descendants(sessions: ChatSessionManager, root: SessionAddress) -> list[SubAgentLink]:
    """Return every Sub-Agent below *root*, breadth first, each child after its Parent."""
    found: list[SubAgentLink] = []
    seen = {root}
    pending = deque([root])
    while pending and len(found) < _MAX_TREE_SIZE:
        for link in children(sessions, pending.popleft()):
            if link.session in seen:
                continue
            seen.add(link.session)
            found.append(link)
            pending.append(link.session)
    return found[:_MAX_TREE_SIZE]


def ancestors(sessions: ChatSessionManager, address: SessionAddress) -> list[SessionAddress]:
    """Return the Parent Sessions above *address*, nearest first."""
    chain: list[SessionAddress] = []
    seen = {address}
    link = read_link(sessions, address)
    while link is not None and len(chain) < _MAX_CHAIN_LENGTH and link.parent not in seen:
        chain.append(link.parent)
        seen.add(link.parent)
        link = read_link(sessions, link.parent)
    return chain


def link_session(
    sessions: ChatSessionManager,
    address: SessionAddress,
    *,
    subagent_id: str,
    parent: SessionAddress,
    run_id: str,
    tool_call_id: str | None,
    tool_call_index: int | None,
) -> None:
    """Store the Parent link of a new Sub-Agent Session."""

    def update(metadata: dict[str, Any]) -> None:
        metadata[SUBAGENT_SESSION_META_KEY] = True
        metadata[SUBAGENT_PARENT_META_KEY] = {
            "id": subagent_id,
            "agent_id": parent.agent_id,
            "session_id": parent.session_id,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "tool_call_index": tool_call_index,
            "project_id": parent.project_id,
        }

    sessions.mutate_metadata(address, update)


def _link_from_metadata(address: SessionAddress, metadata: dict[str, Any]) -> SubAgentLink | None:
    raw = metadata.get(SUBAGENT_PARENT_META_KEY)
    if not isinstance(raw, dict):
        return None
    subagent_id, agent_id, session_id = raw.get("id"), raw.get("agent_id"), raw.get("session_id")
    project_id = raw.get("project_id")
    if not (
        isinstance(subagent_id, str)
        and subagent_id
        and isinstance(agent_id, str)
        and agent_id
        and isinstance(session_id, str)
        and session_id
    ):
        return None
    if project_id is not None and not isinstance(project_id, str):
        return None
    taken_over_at = metadata.get(SUBAGENT_TAKEN_OVER_AT_META_KEY)
    return SubAgentLink(
        id=subagent_id,
        session=address,
        parent=SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id),
        title=next(
            (value for key in _TITLE_KEYS if isinstance(value := metadata.get(key), str) and value),
            "",
        ),
        taken_over_at=taken_over_at if isinstance(taken_over_at, str) else None,
    )


__all__ = [
    "SubAgentLink",
    "ancestors",
    "children",
    "descendants",
    "find_link",
    "link_session",
    "read_link",
]
