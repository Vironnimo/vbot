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
    SUBAGENT_REPLY_TARGET_META_KEY,
    SUBAGENT_SESSION_META_KEY,
    SessionAddress,
)
from core.sessions.errors import SessionNotFoundError

if TYPE_CHECKING:
    from core.sessions import ChatSessionManager

# Ancestor lookup is used to enforce the configured nesting limit.
_MAX_CHAIN_LENGTH = 64
_TITLE_KEYS = ("title", "auto_title")


@dataclass(frozen=True)
class SubAgentLink:
    """One Sub-Agent Session and its link to the Parent Session."""

    id: str
    session: SessionAddress
    parent: SessionAddress
    title: str
    reply_target: str
    parent_generation_id: str | None = None


def read_link(
    sessions: ChatSessionManager, address: SessionAddress, *, strict: bool = False
) -> SubAgentLink | None:
    """Return the Parent link, propagating unavailable metadata when ``strict``.

    A missing Session ends the tree in either mode. Callers making destructive
    decisions must distinguish that absence from a failed metadata read.
    """
    try:
        snapshot = sessions.subagent_link_metadata(address)
    except SessionNotFoundError:
        return None
    except Exception:
        if strict:
            raise
        return None
    if snapshot is None:
        return None
    return _link_from_metadata(
        address, snapshot.metadata, parent_generation_id=snapshot.parent_generation_id
    )


def find_link(sessions: ChatSessionManager, subagent_id: str) -> SubAgentLink | None:
    """Return the live Sub-Agent Session with public id *subagent_id*."""
    address = sessions.subagent_session(subagent_id)
    return read_link(sessions, address) if address is not None else None


def children(
    sessions: ChatSessionManager, parent: SessionAddress, *, strict: bool = False
) -> list[SubAgentLink]:
    """Return the Sub-Agents linked directly to *parent*, oldest first."""
    links = (
        read_link(sessions, address, strict=strict)
        for address in sessions.subagent_children(parent)
    )
    return [link for link in links if link is not None and link.parent == parent]


def descendants(
    sessions: ChatSessionManager, root: SessionAddress, *, strict: bool = False
) -> list[SubAgentLink]:
    """Walk the complete subtree breadth first.

    Control operations need every descendant regardless of historical tree size.
    A visited set prevents corrupt cycles and duplicate links from repeating work.
    """
    found: list[SubAgentLink] = []
    seen = {root}
    pending = deque([root])
    while pending:
        for link in children(sessions, pending.popleft(), strict=strict):
            if link.session in seen:
                continue
            seen.add(link.session)
            found.append(link)
            pending.append(link.session)
    return found


def ancestors(
    sessions: ChatSessionManager, address: SessionAddress, *, strict: bool = False
) -> list[SessionAddress]:
    """Return Parents nearest first; ``strict`` refuses an unreadable chain."""
    chain: list[SessionAddress] = []
    seen = {address}
    link = read_link(sessions, address, strict=strict)
    while link is not None and len(chain) < _MAX_CHAIN_LENGTH and link.parent not in seen:
        chain.append(link.parent)
        seen.add(link.parent)
        link = read_link(sessions, link.parent, strict=strict)
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

    values = _link_metadata(
        subagent_id=subagent_id,
        parent=parent,
        run_id=run_id,
        tool_call_id=tool_call_id,
        tool_call_index=tool_call_index,
    )
    sessions.mutate_metadata(address, lambda metadata: metadata.update(values))


def _link_metadata(
    *,
    subagent_id: str,
    parent: SessionAddress,
    run_id: str,
    tool_call_id: str | None,
    tool_call_index: int | None,
) -> dict[str, Any]:
    """Build the Parent-link fields for creation or explicit linking."""
    return {
        SUBAGENT_SESSION_META_KEY: True,
        SUBAGENT_PARENT_META_KEY: {
            "id": subagent_id,
            "agent_id": parent.agent_id,
            "session_id": parent.session_id,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "tool_call_index": tool_call_index,
            "project_id": parent.project_id,
        },
    }


def _link_from_metadata(
    address: SessionAddress,
    metadata: dict[str, Any],
    *,
    parent_generation_id: str | None = None,
) -> SubAgentLink | None:
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
    return SubAgentLink(
        id=subagent_id,
        session=address,
        parent=SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id),
        title=next(
            (value for key in _TITLE_KEYS if isinstance(value := metadata.get(key), str) and value),
            "",
        ),
        reply_target="user" if metadata.get(SUBAGENT_REPLY_TARGET_META_KEY) == "user" else "parent",
        parent_generation_id=parent_generation_id,
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
