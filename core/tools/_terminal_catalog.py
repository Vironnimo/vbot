"""Terminal catalog ordering, groups and operator summaries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from core.tools.terminal_store import (
    TERMINAL_AGENT_GROUP_ID_PREFIX,
    TERMINAL_FINISHED_GROUP_ID,
    TERMINAL_MANUAL_GROUP_ID,
    TerminalGroup,
    TerminalOperatorStore,
    agent_group_id,
    launch_history_document,
    validate_group_name,
)
from core.utils.ids import new_id
from core.utils.logging import get_logger
from core.utils.paths import model_path

from ._terminal_state import (
    TerminalChangedCallback,
    TerminalManagerError,
    TerminalNotFoundError,
    TerminalOwner,
    TerminalSession,
    _utc_now,
)

_LOGGER = get_logger("tools.terminal_manager")


class TerminalCatalog:
    """Own retained Terminal catalog ordering, groups and operator presentation."""

    def __init__(
        self,
        sessions: dict[str, TerminalSession],
        store: TerminalOperatorStore,
        terminate: Callable[..., Awaitable[None]],
    ) -> None:
        self._sessions = sessions
        self._operator_store = store
        self._terminate_session = terminate
        self._changed_callbacks: list[TerminalChangedCallback] = []

    def add_changed_callback(self, callback: TerminalChangedCallback) -> Callable[[], None]:
        """Notify transport edges when operator-visible Terminal state changes."""
        self._changed_callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._changed_callbacks:
                self._changed_callbacks.remove(callback)

        return unsubscribe

    def list_for_operator(self) -> list[dict[str, Any]]:
        """Return retained Terminal Sessions grouped and ordered for display."""
        rank: dict[str, int] = {
            group["group_id"]: index for index, group in enumerate(self.list_groups_for_operator())
        }

        def sort_key(session: TerminalSession) -> tuple[int, int, int, float]:
            group_id = self._session_group(session).group_id
            group = self._operator_store.groups.get(group_id)
            position = len(group.order) + 1 if group is not None else -1
            if group is not None:
                try:
                    position = group.order.index(session.terminal_id)
                except ValueError:
                    position = len(group.order) + 1
            stamp = session.finished_at if session.finished_at else session.started_at
            return (rank.get(group_id, 10**9), position, 0, -stamp.timestamp())

        sessions = sorted(self._sessions.values(), key=sort_key)
        return [self._operator_summary(session) for session in sessions]

    def list_operator_launch_history(self) -> list[dict[str, Any]]:
        """Return newest-first manual launch configurations for operator reuse."""
        return launch_history_document(self._operator_store.launch_history)

    def list_groups_for_operator(self) -> list[dict[str, Any]]:
        """Return operator-visible groups: user/agent groups, then the shared
        manual automatic group, then one automatic group per active Agent."""
        groups = list(self._operator_store.groups.values())
        automatic = [
            self._automatic_group(TERMINAL_MANUAL_GROUP_ID)
            if self._automatic_group_terminals(TERMINAL_MANUAL_GROUP_ID)
            else None,
            *(
                self._automatic_group(agent_group_id(owner.agent_id))
                for owner in self._distinct_agent_owners()
                if self._automatic_group_terminals(agent_group_id(owner.agent_id))
            ),
        ]
        for group in automatic:
            if group is not None and not any(
                existing.group_id == group.group_id for existing in groups
            ):
                groups.append(group)
        if self._finished_group_terminals():
            groups.append(self._finished_group())
        return [self._group_summary(group) for group in groups]

    def create_group_for_operator(self, name: str) -> dict[str, Any]:
        """Create one durable user group with a unique name."""
        name = validate_group_name(name)
        if self._operator_store.group_name_taken(name):
            raise TerminalManagerError(f"A Terminal group named '{name}' already exists")
        group = TerminalGroup(
            group_id=new_id(
                "grp", claim=lambda candidate: candidate not in self._operator_store.groups
            ),
            name=name,
            kind="user",
            order=[],
            created_at=_utc_now(),
        )
        self._operator_store.groups[group.group_id] = group
        self._operator_store.persist_groups()
        self._notify_changed("")
        _LOGGER.info("Created Terminal group group=%s name=%s", group.group_id, group.name)
        return self._group_summary(group)

    def rename_group_for_operator(self, group_id: str, name: str) -> dict[str, Any]:
        """Rename one user or agent group; automatic groups are fixed."""
        group = self._require_group(group_id)
        if group.kind == "automatic" or group.kind == "finished":
            raise TerminalManagerError("This Terminal group cannot be renamed")
        name = validate_group_name(name)
        if self._operator_store.group_name_taken(name, exclude=group_id):
            raise TerminalManagerError(f"A Terminal group named '{name}' already exists")
        previous = group.name
        group.name = name
        if group.kind == "user":
            self._operator_store.persist_groups()
        self._notify_changed("")
        _LOGGER.info("Renamed Terminal group group=%s from=%s to=%s", group_id, previous, name)
        return self._group_summary(group)

    async def delete_group_for_operator(self, group_id: str) -> dict[str, Any]:
        """Remove one user or agent group and kill every live terminal in it.

        Killed terminals stay in the retained catalog and appear in the
        finished group, exactly like an explicit kill.
        """
        group = self._require_group(group_id)
        if group.kind == "automatic" or group.kind == "finished":
            raise TerminalManagerError("This Terminal group cannot be deleted")
        terminals = [
            session
            for session in self._sessions.values()
            if self._session_group(session).group_id == group_id
        ]
        for session in terminals:
            if session.state not in {"exited", "error"}:
                await self._terminate_session(session, suppress_attention=True)
        del self._operator_store.groups[group_id]
        if group.kind == "user":
            self._operator_store.persist_groups()
        self._notify_changed("")
        _LOGGER.info(
            "Deleted Terminal group group=%s name=%s terminals=%d",
            group_id,
            group.name,
            len(terminals),
        )
        return {"group_id": group_id, "name": group.name, "terminals_killed": len(terminals)}

    def set_group_order_for_operator(self, group_id: str, order: Sequence[str]) -> dict[str, Any]:
        """Persist one user-set Terminal order; missing ids are appended."""
        group = self._require_group(group_id)
        if not isinstance(order, list) or any(
            not isinstance(item, str) or not item for item in order
        ):
            raise ValueError("order must be a list of terminal ids")
        seen: set[str] = set()
        for terminal_id in order:
            if terminal_id in seen:
                raise ValueError("order must not contain duplicate terminal ids")
            seen.add(terminal_id)
        members = self._group_members(group.group_id)
        unknown = seen - {session.terminal_id for session in members}
        if unknown:
            raise TerminalManagerError("order contains terminals that do not belong to this group")
        ordered = list(order)
        ordered.extend(
            session.terminal_id for session in members if session.terminal_id not in seen
        )
        group.order = ordered
        if group.kind == "user":
            self._operator_store.persist_groups()
        self._notify_changed("")
        return {"group_id": group_id, "order": list(ordered)}

    def resolve_or_create_agent_group(self, name: str) -> TerminalGroup:
        """Return the group with this name or create a non-durable Agent group.

        The lookup spans user and agent groups, so an Agent reuses the group
        the operator already set up (or another Agent created).
        """
        name = validate_group_name(name)
        existing = self._operator_store.group_by_name(name)
        if existing is not None:
            return existing
        group = TerminalGroup(
            group_id=new_id(
                "grp", claim=lambda candidate: candidate not in self._operator_store.groups
            ),
            name=name,
            kind="agent",
            order=[],
            created_at=_utc_now(),
            source=None,
        )
        self._operator_store.groups[group.group_id] = group
        self._notify_changed("")
        _LOGGER.info("Created Agent Terminal group group=%s name=%s", group.group_id, name)
        return group

    def _session_group(self, session: TerminalSession) -> TerminalGroup:
        """Return the operator-visible group a Terminal Session belongs to."""
        if session.state in {"exited", "error"} and session.finished_at is not None:
            return self._finished_group()
        group_id = session.group_id
        if group_id is not None and group_id in self._operator_store.groups:
            return self._operator_store.groups[group_id]
        if session.owner is None:
            return self._automatic_group(TERMINAL_MANUAL_GROUP_ID)
        return self._automatic_group(agent_group_id(session.owner.agent_id))

    def _group_members(self, group_id: str) -> list[TerminalSession]:
        return [
            session
            for session in self._sessions.values()
            if self._session_group(session).group_id == group_id
        ]

    def _automatic_group(self, group_id: str) -> TerminalGroup:
        name = (
            "Manual"
            if group_id == TERMINAL_MANUAL_GROUP_ID
            else f"Agent {group_id.removeprefix(TERMINAL_AGENT_GROUP_ID_PREFIX)}"
        )
        return TerminalGroup(
            group_id=group_id,
            name=name,
            kind="automatic",
            order=[],
            created_at=_utc_now(),
        )

    def _automatic_group_terminals(self, group_id: str) -> bool:
        return any(
            self._session_group(session).group_id == group_id for session in self._sessions.values()
        )

    def _finished_group(self) -> TerminalGroup:
        return TerminalGroup(
            group_id=TERMINAL_FINISHED_GROUP_ID,
            name="Finished",
            kind="finished",
            order=[],
            created_at=_utc_now(),
        )

    def _finished_group_terminals(self) -> bool:
        return any(session.finished_at is not None for session in self._sessions.values())

    def _distinct_agent_owners(self) -> list[TerminalOwner]:
        owners: dict[tuple[str | None, str], TerminalOwner] = {}
        for session in self._sessions.values():
            owner = session.owner
            if owner is None:
                continue
            owners[(owner.project_id, owner.agent_id)] = owner
        return sorted(
            owners.values(),
            key=lambda owner: (owner.project_id or "", owner.agent_id),
        )

    def _group_summary(self, group: TerminalGroup) -> dict[str, Any]:
        members = self._group_members(group.group_id)
        return {
            "group_id": group.group_id,
            "name": group.name,
            "kind": group.kind,
            "terminal_count": len(members),
            "live_count": sum(session.state not in {"exited", "error"} for session in members),
            "order": list(group.order),
            "source": group.source,
        }

    def _require_group(self, group_id: str) -> TerminalGroup:
        if not isinstance(group_id, str) or not group_id:
            raise ValueError("group_id must be a non-empty string")
        group = self._operator_store.groups.get(group_id)
        if group is None:
            raise TerminalNotFoundError(f"Terminal group not found: {group_id}")
        return group

    def _append_group_terminal(self, group_id: str, terminal_id: str) -> None:
        group = self._operator_store.groups.get(group_id)
        if group is None or terminal_id in group.order:
            return
        group.order.append(terminal_id)

    def _get_for_operator(self, terminal_id: str) -> TerminalSession:
        session = self._sessions.get(terminal_id)
        if session is None:
            raise TerminalNotFoundError(f"Terminal Session not found: {terminal_id}")
        return session

    def _operator_summary(self, session: TerminalSession) -> dict[str, Any]:
        attention = session.attention
        owner = session.owner
        attachment = session.attachment
        return {
            "terminal_id": session.terminal_id,
            "group_id": self._session_group(session).group_id,
            "state": session.state,
            "command": Path(session.command).name or session.command,
            "name": session.name,
            "arguments": list(session.arguments),
            "launch_command": session.launch_command,
            "launch_args": list(session.launch_arguments),
            "title": session.renderer.title,
            "workdir": model_path(session.cwd),
            "pid": session.adapter.pid,
            "exit_code": session.exit_code,
            "started_at": session.started_at.isoformat(),
            "finished_at": session.finished_at.isoformat() if session.finished_at else None,
            "columns": session.renderer.columns,
            "rows": session.renderer.rows,
            "screen_revision": session.renderer.revision,
            "owner": (
                {
                    "project_id": owner.project_id,
                    "agent_id": owner.agent_id,
                    "session_id": owner.session_id,
                }
                if owner is not None
                else None
            ),
            "attachment": (
                {
                    "project_id": attachment.project_id,
                    "agent_id": attachment.agent_id,
                    "session_id": attachment.session_id,
                }
                if attachment is not None
                else None
            ),
            "attention": (
                {
                    "revision": attention.revision,
                    "kind": attention.kind,
                    "summary": attention.summary,
                    "created_at": attention.created_at.isoformat(),
                }
                if attention is not None
                else None
            ),
        }

    def _notify_changed(self, terminal_id: str) -> None:
        for callback in list(self._changed_callbacks):
            try:
                callback(terminal_id)
            except Exception:
                _LOGGER.exception("Terminal changed callback failed for terminal=%s", terminal_id)
