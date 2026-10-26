"""Immutable access indexes and SQL projections inside ChannelStateStore."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field

from core.channels._member_tools import DEFAULT_MEMBER_TOOLS, normalize_member_tools
from core.channels.config import ChannelNotFoundError
from core.chat.messages import GroupRole
from core.config_validation import JsonObject
from core.tools.availability import ToolAccess


@dataclass(frozen=True)
class _AccessRoles:
    """One Channel's own identity and additional admins, as ``role_for`` reads them."""

    self_user_id: str | None
    admins: frozenset[tuple[str, str]]
    member_tools: Mapping[str, ToolAccess] = field(default_factory=dict)
    scope_redirects: Mapping[str, str] = field(default_factory=dict)

    def current_scope(self, scope_id: str) -> str:
        return self.scope_redirects.get(scope_id, scope_id)

    def tools_for(self, scope_id: str) -> ToolAccess:
        return self.member_tools.get(
            self.current_scope(scope_id), self.member_tools.get("", DEFAULT_MEMBER_TOOLS)
        )

    def role_for(self, access_scope_id: str, user_id: str) -> GroupRole:
        if (
            user_id == self.self_user_id
            or (self.current_scope(access_scope_id), user_id) in self.admins
        ):
            return "admin"
        return "member"


_NO_ACCESS_ROLES = _AccessRoles(self_user_id=None, admins=frozenset())


def _registered_self_user_id(connection: sqlite3.Connection, channel_id: str) -> str | None:
    row = connection.execute(
        "SELECT self_user_id FROM channels WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    if row is None:
        raise ChannelNotFoundError(f"Channel not found: {channel_id}")
    return None if row[0] is None else str(row[0])


def _load_all_roles(connection: sqlite3.Connection) -> dict[str, _AccessRoles]:
    self_ids = {
        str(channel_id): self_user_id
        for channel_id, self_user_id in connection.execute(
            "SELECT channel_id, self_user_id FROM channels"
        )
    }
    admins: dict[str, set[tuple[str, str]]] = {channel_id: set() for channel_id in self_ids}
    for channel_id, access_scope_id, user_id in connection.execute(
        "SELECT channel_id, access_scope_id, user_id FROM channel_admins"
    ):
        admins.setdefault(str(channel_id), set()).add((str(access_scope_id), str(user_id)))
    return {
        channel_id: _AccessRoles(
            self_user_id=self_ids.get(channel_id),
            admins=frozenset(admins[channel_id]),
            member_tools=_load_member_tools(connection, channel_id),
        )
        for channel_id in admins
    }


def _load_channel_roles(connection: sqlite3.Connection, channel_id: str) -> _AccessRoles | None:
    row = connection.execute(
        "SELECT self_user_id FROM channels WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    if row is None:
        return None
    admins = frozenset(
        (str(access_scope_id), str(user_id))
        for access_scope_id, user_id in connection.execute(
            "SELECT access_scope_id, user_id FROM channel_admins WHERE channel_id = ?",
            (channel_id,),
        )
    )
    return _AccessRoles(
        self_user_id=row[0], admins=admins, member_tools=_load_member_tools(connection, channel_id)
    )


def _load_member_tools(
    connection: sqlite3.Connection, channel_id: str, scope_id: str | None = None
) -> dict[str, ToolAccess]:
    restriction = (
        "" if scope_id is None else " AND (p.access_scope_id = '' OR p.access_scope_id = ?)"
    )
    parameters = (channel_id,) if scope_id is None else (channel_id, scope_id)
    rows = connection.execute(
        "SELECT p.access_scope_id, p.mode, t.tool_name FROM channel_member_tool_policies p "
        "LEFT JOIN channel_member_tools t ON t.channel_id = p.channel_id "
        "AND t.access_scope_id = p.access_scope_id WHERE p.channel_id = ?"
        + restriction
        + " ORDER BY p.access_scope_id, t.tool_name",
        parameters,
    )
    values: dict[str, dict[str, object]] = {}
    names: dict[str, list[str]] = {}
    for scope, mode, name in rows:
        values[scope] = {"mode": mode}
        if name is not None:
            names.setdefault(scope, []).append(name)
    return {
        scope: normalize_member_tools(
            {**value, **({"allowed": names.get(scope, [])} if value["mode"] == "selected" else {})}
        )
        for scope, value in values.items()
    }


def _access_projection(
    connection: sqlite3.Connection, channel_id: str
) -> tuple[JsonObject, _AccessRoles]:
    self_user_id = _registered_self_user_id(connection, channel_id)
    admins: dict[str, set[str]] = {}
    for access_scope_id, user_id in connection.execute(
        "SELECT access_scope_id, user_id FROM channel_admins WHERE channel_id = ?",
        (channel_id,),
    ):
        admins.setdefault(str(access_scope_id), set()).add(str(user_id))
    participants: dict[str, list[tuple[str, str, str]]] = {}
    for access_scope_id, user_id, display_name, last_seen_at in connection.execute(
        "SELECT access_scope_id, user_id, display_name, last_seen_at "
        "FROM channel_participants WHERE channel_id = ? ORDER BY access_scope_id, user_id",
        (channel_id,),
    ):
        participants.setdefault(str(access_scope_id), []).append(
            (str(user_id), str(display_name), str(last_seen_at))
        )
    policies = _load_member_tools(connection, channel_id)
    roles = _AccessRoles(
        self_user_id=self_user_id,
        member_tools=policies,
        admins=frozenset(
            (access_scope_id, user_id)
            for access_scope_id, user_ids in admins.items()
            for user_id in user_ids
        ),
    )
    groups: list[JsonObject] = []
    for access_scope_id in sorted((set(admins) | set(participants) | set(policies)) - {""}):
        admin_ids = set(admins.get(access_scope_id, ()))
        if self_user_id is not None:
            admin_ids.add(self_user_id)
        groups.append(
            {
                "access_scope_id": access_scope_id,
                "member_tools": policies[access_scope_id].to_dict()
                if access_scope_id in policies
                else None,
                "effective_member_tools": roles.tools_for(access_scope_id).to_dict(),
                "admin_user_ids": sorted(admin_ids),
                "participants": [
                    {
                        "user_id": user_id,
                        "display_name": display_name,
                        "last_seen_at": last_seen_at,
                        "role": roles.role_for(access_scope_id, user_id),
                    }
                    for user_id, display_name, last_seen_at in participants.get(access_scope_id, ())
                ],
            }
        )
    projection: JsonObject = {
        "channel_id": channel_id,
        "self_user_id": self_user_id,
        "member_tools": roles.tools_for("").to_dict(),
        "groups": groups,
    }
    return projection, roles
