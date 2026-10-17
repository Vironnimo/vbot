"""Terminal catalog: views, ordering, call names and operator summaries.

The operator sees Terminals in views. A Terminal an Agent Session started or
holds is shown under its conversation (the root Session of its Sub-Agent tree);
one the operator started and no Agent holds is shown under the operator's own
terminals. The operator can pin a Terminal to a durable user view or to another
conversation's view instead.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from core.tools.terminal_store import (
    TERMINAL_MANUAL_GROUP_ID,
    TerminalGroup,
    TerminalOperatorStore,
    launch_history_document,
    validate_group_name,
)
from core.utils.ids import new_id
from core.utils.logging import get_logger
from core.utils.paths import model_path

from ._terminal_state import (
    TerminalChangedCallback,
    TerminalInfo,
    TerminalManagerError,
    TerminalNotFoundError,
    TerminalOwner,
    TerminalSessionView,
    _utc_now,
)

_LOGGER = get_logger("tools.terminal_manager")

# Call names, handed out in this order: the first free one goes to a new
# Terminal. Ordinary first names, short and easy to tell apart when spoken;
# the first ones start with different letters.
TERMINAL_CALL_NAMES = (
    "Anna",
    "Ben",
    "Clara",
    "David",
    "Emil",
    "Frida",
    "Greta",
    "Hugo",
    "Ida",
    "Jonas",
    "Karl",
    "Lena",
    "Max",
    "Nora",
    "Oskar",
    "Paul",
    "Rosa",
    "Simon",
    "Theo",
    "Vera",
    "Willi",
    "Zoe",
    "Felix",
    "Hanna",
    "Leon",
    "Marie",
    "Jakob",
    "Elsa",
    "Otto",
    "Julia",
    "Moritz",
    "Lotte",
)
# A conversation's view: ``session:<project id>/<agent id>/<session id>``.
TERMINAL_SESSION_GROUP_ID_PREFIX = "session:"
# Sub-Agent trees are shallow; the walk to a root stops here in any case.
_MAX_TREE_DEPTH = 32

type SessionViewResolver = Callable[[TerminalOwner], TerminalSessionView | None]


class TerminalCatalog:
    """Own operator views, Terminal ordering, call names and operator summaries.

    It sees Terminal Sessions only as ``TerminalInfo`` views from *terminals*
    and pins them through *pin* (a terminal id and a view id or ``None``).
    *session_view* names the conversation an Agent Session belongs to;
    without it, every Agent Session is its own conversation without a title.
    *reserved_names* are names a call name must not take, such as Agent names.
    """

    def __init__(
        self,
        store: TerminalOperatorStore,
        terminals: Callable[[], Iterable[TerminalInfo]],
        pin: Callable[[str, str | None], None],
        *,
        session_view: SessionViewResolver | None = None,
        reserved_names: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self._operator_store = store
        self._terminals = terminals
        self._pin = pin
        self._session_view = session_view
        self._reserved_names = reserved_names
        # Remembered conversations, each with the generation it was resolved in;
        # forgetting starts a new generation, also for a resolve under way.
        self._views: dict[TerminalOwner, tuple[int, TerminalSessionView]] = {}
        self._views_generation = 0
        # User-set orders of views that are not durable user groups.
        self._orders: dict[str, list[str]] = {}
        self._changed_callbacks: list[TerminalChangedCallback] = []

    def add_changed_callback(self, callback: TerminalChangedCallback) -> Callable[[], None]:
        """Notify transport edges when operator-visible Terminal state changes."""
        self._changed_callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._changed_callbacks:
                self._changed_callbacks.remove(callback)

        return unsubscribe

    # Views

    def list_for_operator(self) -> list[dict[str, Any]]:
        """Return retained Terminal Sessions in view order, each in its user-set order."""
        terminals = list(self._terminals())
        groups = self._view_ids(terminals)
        rank = {group_id: index for index, group_id in enumerate(groups)}

        def sort_key(info: TerminalInfo) -> tuple[int, int, float]:
            group_id = self._view_id(info)
            order = self._order(group_id)
            try:
                position = order.index(info.terminal_id)
            except ValueError:
                position = len(order)
            return (rank.get(group_id, len(rank)), position, info.started_at.timestamp())

        return [self.summary(info) for info in sorted(terminals, key=sort_key)]

    def list_groups_for_operator(self) -> list[dict[str, Any]]:
        """Return the operator's views: conversations in the order their first
        Terminal started, then the durable user views, then the operator's own
        terminals. Views without Terminals are left out, except user views."""
        terminals = list(self._terminals())
        return [self._group_summary(group_id, terminals) for group_id in self._view_ids(terminals)]

    def list_operator_launch_history(self) -> list[dict[str, Any]]:
        """Return newest-first manual launch configurations for operator reuse."""
        return launch_history_document(self._operator_store.launch_history)

    def create_group_for_operator(self, name: str) -> dict[str, Any]:
        """Create one durable user view with a unique name."""
        name = validate_group_name(name)
        if self._operator_store.group_name_taken(name):
            raise TerminalManagerError(f"A Terminal group named '{name}' already exists")
        group = TerminalGroup(
            group_id=new_id("grp", claim=self._operator_store.group_id_available),
            name=name,
            order=[],
            created_at=_utc_now(),
        )
        self._operator_store.groups[group.group_id] = group
        self._operator_store.persist_groups()
        self.notify_changed("")
        # Group names are operator text: lines name the group by its id only.
        _LOGGER.info("Created Terminal group (group=%s)", group.group_id)
        return self._group_summary(group.group_id, list(self._terminals()))

    def rename_group_for_operator(self, group_id: str, name: str) -> dict[str, Any]:
        """Rename one user view; the other views take their names from elsewhere."""
        group = self.require_group(group_id)
        name = validate_group_name(name)
        if self._operator_store.group_name_taken(name, exclude=group_id):
            raise TerminalManagerError(f"A Terminal group named '{name}' already exists")
        group.name = name
        self._operator_store.persist_groups()
        self.notify_changed("")
        _LOGGER.info("Renamed Terminal group (group=%s)", group_id)
        return self._group_summary(group_id, list(self._terminals()))

    def delete_group_for_operator(self, group_id: str) -> dict[str, Any]:
        """Remove one user view; its Terminals keep running in their own views."""
        self.require_group(group_id)
        pinned = [info for info in self._terminals() if info.group_id == group_id]
        for info in pinned:
            self._pin(info.terminal_id, None)
        group = self._operator_store.groups.pop(group_id)
        self._operator_store.persist_groups()
        self.notify_changed("")
        _LOGGER.info("Deleted Terminal group (group=%s terminals=%d)", group_id, len(pinned))
        return {"group_id": group_id, "name": group.name, "terminals_moved": len(pinned)}

    def set_group_order_for_operator(self, group_id: str, order: Sequence[str]) -> dict[str, Any]:
        """Set one view's Terminal order; missing members are appended.

        The order of a user view persists; other views keep theirs in memory.
        """
        if not isinstance(group_id, str) or not group_id:
            raise ValueError("group_id must be a non-empty string")
        if not isinstance(order, list) or any(
            not isinstance(item, str) or not item for item in order
        ):
            raise ValueError("order must be a list of terminal ids")
        if len(set(order)) != len(order):
            raise ValueError("order must not contain duplicate terminal ids")
        members = [
            info.terminal_id for info in self._terminals() if self._view_id(info) == group_id
        ]
        if not members and group_id not in self._operator_store.groups:
            raise TerminalNotFoundError(f"Terminal group not found: {group_id}")
        if set(order) - set(members):
            raise TerminalManagerError("order contains terminals that do not belong to this group")
        ordered = [*order, *(terminal_id for terminal_id in members if terminal_id not in order)]
        group = self._operator_store.groups.get(group_id)
        if group is not None:
            group.order = ordered
            self._operator_store.persist_groups()
        else:
            self._orders[group_id] = ordered
        self.notify_changed("")
        return {"group_id": group_id, "order": list(ordered)}

    def validate_pin(self, group_id: str | None) -> None:
        """Refuse a pin target that names no user view and no conversation."""
        if group_id is None:
            return
        if group_id in self._operator_store.groups:
            return
        if session_group_owner(group_id) is not None:
            return
        raise TerminalNotFoundError(f"Terminal group not found: {group_id}")

    def pinned(self, group_id: str | None, terminal_id: str) -> None:
        """Place a Terminal just pinned to *group_id* at the end of that view's order."""
        if group_id is None:
            return
        order = self._order(group_id)
        if terminal_id in order:
            order.remove(terminal_id)
        order.append(terminal_id)
        group = self._operator_store.groups.get(group_id)
        if group is not None:
            self._operator_store.persist_groups()

    def require_group(self, group_id: str) -> TerminalGroup:
        if not isinstance(group_id, str) or not group_id:
            raise ValueError("group_id must be a non-empty string")
        group = self._operator_store.groups.get(group_id)
        if group is None:
            raise TerminalNotFoundError(f"Terminal group not found: {group_id}")
        return group

    def forget_session_views(self) -> None:
        """Drop remembered conversation titles after a Session title changed.

        Safe from any thread: Session titles change on store worker threads.
        """
        self._views_generation += 1
        if not self._views:
            return
        self._views.clear()
        self.notify_changed("")

    # Call names

    def allocate_call_name(self) -> str:
        """Return the first call name no retained Terminal and no reserved name uses."""
        taken = {info.call_name.casefold() for info in self._terminals()}
        if self._reserved_names is not None:
            taken.update(name.casefold() for name in self._reserved_names())
        for name in TERMINAL_CALL_NAMES:
            if name.casefold() not in taken:
                return name
        round_number = 2
        while True:
            for name in TERMINAL_CALL_NAMES:
                candidate = f"{name} {round_number}"
                if candidate.casefold() not in taken:
                    return candidate
            round_number += 1

    # Summaries

    def summary(self, info: TerminalInfo) -> dict[str, Any]:
        """The operator summary of one Terminal, as the WebUI and RPC show it."""
        attention = info.attention
        return {
            "terminal_id": info.terminal_id,
            "group_id": self._view_id(info),
            # The view it shows in without a pin.
            "home_group_id": self._home_view_id(info),
            "pinned": info.group_id is not None and self._pin_valid(info.group_id),
            "state": info.state,
            "command": Path(info.command).name or info.command,
            "call_name": info.call_name,
            "arguments": list(info.arguments),
            "launch_command": info.launch_command,
            "launch_args": list(info.launch_arguments),
            "title": info.title,
            "workdir": model_path(info.cwd),
            "pid": info.pid,
            "exit_code": info.exit_code,
            "started_at": info.started_at.isoformat(),
            "finished_at": info.finished_at.isoformat() if info.finished_at else None,
            "columns": info.columns,
            "rows": info.rows,
            "screen_revision": info.screen_revision,
            "owner": _owner_data(info.owner),
            "attachment": _owner_data(info.attachment),
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

    def notify_changed(self, terminal_id: str) -> None:
        """Tell transport edges that operator-visible Terminal state changed."""
        for callback in list(self._changed_callbacks):
            try:
                callback(terminal_id)
            except Exception:
                _LOGGER.exception("Terminal changed callback failed for terminal=%s", terminal_id)

    # Internals

    def _view_id(self, info: TerminalInfo) -> str:
        """The view a Terminal is shown in: its pin, else its conversation, else
        the operator's own terminals."""
        if info.group_id is not None and self._pin_valid(info.group_id):
            return info.group_id
        return self._home_view_id(info)

    def _home_view_id(self, info: TerminalInfo) -> str:
        scope = info.attachment or info.lifecycle_owner or info.owner
        if scope is None:
            return TERMINAL_MANUAL_GROUP_ID
        return session_group_id(self._view(scope).root)

    def _pin_valid(self, group_id: str) -> bool:
        return group_id in self._operator_store.groups or session_group_owner(group_id) is not None

    def _view(self, scope: TerminalOwner) -> TerminalSessionView:
        generation = self._views_generation
        cached = self._views.get(scope)
        if cached is not None and cached[0] == generation:
            return cached[1]
        view = self._resolve_view(scope)
        self._views[scope] = (generation, view)
        return view

    def _resolve_view(self, scope: TerminalOwner) -> TerminalSessionView:
        if self._session_view is None:
            return TerminalSessionView(root=scope, title=None)
        try:
            view = self._session_view(scope)
        except Exception:
            _LOGGER.exception(
                "Could not resolve the conversation of a Terminal (agent=%s session=%s)",
                scope.agent_id,
                scope.session_id,
            )
            view = None
        return view or TerminalSessionView(root=scope, title=None)

    def _view_ids(self, terminals: list[TerminalInfo]) -> list[str]:
        conversations: dict[str, float] = {}
        manual = False
        for info in terminals:
            group_id = self._view_id(info)
            if group_id == TERMINAL_MANUAL_GROUP_ID:
                manual = True
            elif group_id not in self._operator_store.groups:
                stamp = info.started_at.timestamp()
                conversations[group_id] = min(conversations.get(group_id, stamp), stamp)
        ordered = sorted(conversations, key=lambda group_id: (conversations[group_id], group_id))
        ordered.extend(self._operator_store.groups)
        if manual:
            ordered.append(TERMINAL_MANUAL_GROUP_ID)
        return ordered

    def _order(self, group_id: str) -> list[str]:
        group = self._operator_store.groups.get(group_id)
        if group is not None:
            return group.order
        return self._orders.setdefault(group_id, [])

    def _group_summary(self, group_id: str, terminals: list[TerminalInfo]) -> dict[str, Any]:
        members = [info for info in terminals if self._view_id(info) == group_id]
        summary: dict[str, Any] = {
            "group_id": group_id,
            "terminal_count": len(members),
            "live_count": sum(not info.finished for info in members),
            "working_count": sum(info.state == "working" for info in members),
            "order": list(self._order(group_id)),
        }
        group = self._operator_store.groups.get(group_id)
        if group is not None:
            summary.update(kind="user", name=group.name, session=None)
        elif group_id == TERMINAL_MANUAL_GROUP_ID:
            summary.update(kind="manual", name=None, session=None)
        else:
            root = session_group_owner(group_id)
            assert root is not None
            summary.update(kind="session", name=self._view(root).title, session=_owner_data(root))
        return summary


def resolve_root_view(
    scope: TerminalOwner,
    parent: Callable[[TerminalOwner], TerminalOwner | None],
    title: Callable[[TerminalOwner], str | None],
) -> TerminalSessionView:
    """Walk *scope* up its Sub-Agent tree to the root Session and name it."""
    root = scope
    seen = {scope}
    for _ in range(_MAX_TREE_DEPTH):
        above = parent(root)
        if above is None or above in seen:
            break
        seen.add(above)
        root = above
    return TerminalSessionView(root=root, title=title(root))


def session_group_id(root: TerminalOwner) -> str:
    """Return the view id of one conversation, named by its root Session address."""
    return (
        f"{TERMINAL_SESSION_GROUP_ID_PREFIX}"
        f"{root.project_id or ''}/{root.agent_id}/{root.session_id}"
    )


def session_group_owner(group_id: str) -> TerminalOwner | None:
    """Return the root Session address a conversation view id names, else ``None``."""
    if not group_id.startswith(TERMINAL_SESSION_GROUP_ID_PREFIX):
        return None
    parts = group_id.removeprefix(TERMINAL_SESSION_GROUP_ID_PREFIX).split("/")
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    return TerminalOwner(project_id=parts[0] or None, agent_id=parts[1], session_id=parts[2])


def _owner_data(owner: TerminalOwner | None) -> dict[str, Any] | None:
    if owner is None:
        return None
    return {
        "project_id": owner.project_id,
        "agent_id": owner.agent_id,
        "session_id": owner.session_id,
    }


__all__ = [
    "TERMINAL_CALL_NAMES",
    "TERMINAL_SESSION_GROUP_ID_PREFIX",
    "SessionViewResolver",
    "TerminalCatalog",
    "resolve_root_view",
    "session_group_id",
    "session_group_owner",
]
