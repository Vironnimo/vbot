"""Durable Channel state in the canonical ``channels.db`` database.

``ChannelStateStore`` owns everything a Channel remembers besides its
configuration: the registry of known Channels with their own platform
identity, per-group admins and seen participants, the conversation routing
pointers, Run-button origin bindings, the inbound receipt window of the
socket platforms and the Telegram polling watermark.

Every state row belongs to a registered Channel. Writes for an unregistered
Channel are refused, so a late save cannot recreate state after the Channel was
deleted, and unregistering removes all its rows in one transaction. The
registry records the platform whose ids a Channel's state holds; moving the
Channel to another platform resets that state, because the ids mean nothing
there.

Blocking methods run on the calling thread and are meant for worker threads;
the ``async`` methods run on the database's own worker pool, and ``run_async``
runs a caller's own unit of blocking state work there. ``role_for`` reads
an in-memory index of own identities and admins that every committed access
mutation refreshes, so the per-Tool-dispatch role check never touches storage.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from core.channels._state_schema import DATABASE_NAME, channel_database_spec
from core.channels.adapter import (
    TELEGRAM_UPDATE_OFFSET_TTL_SECONDS,
    RunButtonBinding,
    RunButtonClaim,
    main_conversation_id,
)
from core.channels.config import (
    ChannelConfigError,
    ChannelError,
    ChannelNotFoundError,
    _normalize_channel_id,
)
from core.chat.messages import GroupRole
from core.config_validation import JsonObject
from core.database import Database, canonical_database_path, open_database
from core.utils.timestamps import format_canonical_timestamp, utc_now_timestamp

_Result = TypeVar("_Result")

# Socket platforms may redeliver recent events after a reconnect; remembering the
# newest receipts per Channel bounds both the dedupe window and the table.
RECEIVED_MESSAGE_WINDOW = 4096

_CONVERSATION_KINDS = frozenset({"direct", "group"})


@dataclass(frozen=True)
class _AccessRoles:
    """One Channel's own identity and additional admins, as ``role_for`` reads them."""

    self_user_id: str | None
    admins: frozenset[tuple[str, str]]

    def role_for(self, access_scope_id: str, user_id: str) -> GroupRole:
        if user_id == self.self_user_id or (access_scope_id, user_id) in self.admins:
            return "admin"
        return "member"


_NO_ACCESS_ROLES = _AccessRoles(self_user_id=None, admins=frozenset())


class ChannelStateStore:
    """Registry, access, routing and delivery state of every Channel."""

    def __init__(self, database: Database) -> None:
        self._database = database
        # Serializes access mutations with the refresh of the role index, so an
        # older commit can never install its roles over a newer one.
        self._access_lock = threading.Lock()
        self._roles: dict[str, _AccessRoles] = {}
        with database.read() as connection:
            self._roles = _load_all_roles(connection)

    @classmethod
    def open(cls, data_dir: str | Path) -> ChannelStateStore:
        """Open ``<data_dir>/channels.db`` under the canonical profile."""
        path = canonical_database_path(Path(data_dir).expanduser(), DATABASE_NAME)
        database = open_database(channel_database_spec(path))
        try:
            return cls(database)
        except BaseException:
            database.close()
            raise

    @property
    def database(self) -> Database:
        return self._database

    async def run_async(
        self, function: Callable[..., _Result], *arguments: Any, **keyword_arguments: Any
    ) -> _Result:
        """Run blocking Channel state work on the ``channels.db`` worker pool.

        For an async caller's own unit of state work, such as a Run-button claim
        whose compensation state the worker must record even when the caller is
        cancelled. A closed database raises ``DatabaseUnavailableError``.
        """
        return await self._database.run_async(function, *arguments, **keyword_arguments)

    def close(self) -> None:
        if not self._database.is_closed():
            self._database.close()

    # -- Registry -------------------------------------------------------------------

    def adopt(self, platforms: Mapping[str, str | None]) -> list[str]:
        """Register every configured Channel and record the platform of its state.

        ``platforms`` maps the id of each Channel that has configuration to the
        platform that configuration names, or to None when it cannot be read;
        None registers the Channel and keeps what was recorded. Existing state
        is kept unless it was recorded for another platform, which resets it as
        ``bind_platform`` does. Returns the ids whose state was reset.
        """
        normalized = {
            _normalize_channel_id(channel_id): None if platform is None else _platform(platform)
            for channel_id, platform in platforms.items()
        }
        if not normalized:
            return []

        def adopt(connection: sqlite3.Connection) -> list[str]:
            connection.executemany(
                "INSERT OR IGNORE INTO channels (channel_id, platform) VALUES (?, ?)",
                sorted(normalized.items()),
            )
            return [
                channel_id
                for channel_id, platform in sorted(normalized.items())
                if platform is not None and _bind_platform(connection, channel_id, platform)
            ]

        with self._access_lock:
            reset_ids = self._database.write(adopt)
            with self._database.read() as connection:
                self._roles = _load_all_roles(connection)
        return reset_ids

    def reset(self, channel_id: str, platform: str) -> None:
        """Register a new Channel on ``platform`` with empty state, dropping old rows of its id."""
        normalized_id = _normalize_channel_id(channel_id)
        normalized_platform = _platform(platform)

        def reset(connection: sqlite3.Connection) -> None:
            connection.execute("DELETE FROM channels WHERE channel_id = ?", (normalized_id,))
            connection.execute(
                "INSERT INTO channels (channel_id, platform) VALUES (?, ?)",
                (normalized_id, normalized_platform),
            )

        with self._access_lock:
            self._database.write(reset)
            self._install_roles(normalized_id, _NO_ACCESS_ROLES)

    def bind_platform(self, channel_id: str, platform: str) -> bool:
        """Record that a registered Channel's state now belongs to ``platform``.

        User, chat and message ids of one platform mean nothing on another, so a
        different platform drops the Channel's own identity, group access,
        conversation pointers, Run-button bindings, receipts and polling
        watermark in one transaction. Only the pointer of the shared direct
        conversation (``main_conversation_id``), whose anchor names no platform
        id, is kept. The same platform, or a first recorded one, keeps
        everything. Returns whether state was reset.
        """
        normalized_id = _normalize_channel_id(channel_id)
        normalized_platform = _platform(platform)

        def bind(connection: sqlite3.Connection) -> bool:
            _registered_self_user_id(connection, normalized_id)
            return _bind_platform(connection, normalized_id, normalized_platform)

        with self._access_lock:
            reset = self._database.write(bind)
            if reset:
                self._install_roles(normalized_id, _NO_ACCESS_ROLES)
        return reset

    def unregister(self, channel_id: str) -> None:
        """Delete a Channel and every state row it owns in one transaction."""
        normalized_id = _normalize_channel_id(channel_id)

        def unregister(connection: sqlite3.Connection) -> None:
            connection.execute("DELETE FROM channels WHERE channel_id = ?", (normalized_id,))

        with self._access_lock:
            self._database.write(unregister)
            self._install_roles(normalized_id, None)

    # -- Group access ---------------------------------------------------------------

    async def snapshot_participant_role(
        self,
        channel_id: str,
        access_scope_id: str,
        user_id: str,
        display_name: str,
    ) -> GroupRole:
        """Record one seen participant and return its role from the same transaction."""
        normalized_id = _normalize_channel_id(channel_id)
        scope_id = _normalize_platform_access_id(access_scope_id, "access_scope_id")
        normalized_user_id = _normalize_platform_access_id(user_id, "user_id")
        normalized_display_name = (
            display_name.strip() if isinstance(display_name, str) else normalized_user_id
        ) or normalized_user_id
        seen_at = utc_now_timestamp()

        def snapshot(connection: sqlite3.Connection) -> GroupRole:
            self_user_id = _registered_self_user_id(connection, normalized_id)
            connection.execute(
                "INSERT INTO channel_participants "
                "(channel_id, access_scope_id, user_id, display_name, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (channel_id, access_scope_id, user_id) DO UPDATE SET "
                "display_name = excluded.display_name, last_seen_at = excluded.last_seen_at",
                (normalized_id, scope_id, normalized_user_id, normalized_display_name, seen_at),
            )
            if normalized_user_id == self_user_id:
                return "admin"
            admin = connection.execute(
                "SELECT 1 FROM channel_admins "
                "WHERE channel_id = ? AND access_scope_id = ? AND user_id = ?",
                (normalized_id, scope_id, normalized_user_id),
            ).fetchone()
            return "admin" if admin is not None else "member"

        return await self._database.write_async(snapshot)

    def role_for(self, channel_id: str, access_scope_id: str, user_id: str) -> GroupRole:
        """Resolve the current role from the in-memory index without storage I/O."""
        roles = self._roles.get(_normalize_channel_id(channel_id), _NO_ACCESS_ROLES)
        return roles.role_for(
            _normalize_platform_access_id(access_scope_id, "access_scope_id"),
            _normalize_platform_access_id(user_id, "user_id"),
        )

    async def access_state(self, channel_id: str) -> JsonObject:
        """Return the own identity and every group's admins and seen participants."""
        normalized_id = _normalize_channel_id(channel_id)

        def read(connection: sqlite3.Connection) -> JsonObject:
            return _access_projection(connection, normalized_id)[0]

        return await self._database.read_async(read)

    async def set_self_user_id(self, channel_id: str, user_id: str) -> JsonObject:
        """Set the Channel account's own identity from its seen participants."""
        normalized_id = _normalize_channel_id(channel_id)
        normalized_user_id = _normalize_platform_access_id(user_id, "user_id")

        def update(connection: sqlite3.Connection) -> None:
            _registered_self_user_id(connection, normalized_id)
            seen = connection.execute(
                "SELECT 1 FROM channel_participants WHERE channel_id = ? AND user_id = ? LIMIT 1",
                (normalized_id, normalized_user_id),
            ).fetchone()
            if seen is None:
                raise ChannelConfigError(
                    f"Channel participant has not been seen: {normalized_user_id}"
                )
            connection.execute(
                "UPDATE channels SET self_user_id = ? WHERE channel_id = ?",
                (normalized_user_id, normalized_id),
            )

        return await self._database.run_async(self._mutate_access, normalized_id, update)

    async def grant_group_admin(
        self, channel_id: str, access_scope_id: str, user_id: str
    ) -> JsonObject:
        """Add one user to one group's additional admins, idempotently."""
        normalized_id = _normalize_channel_id(channel_id)
        scope_id = _normalize_platform_access_id(access_scope_id, "access_scope_id")
        normalized_user_id = _normalize_platform_access_id(user_id, "user_id")

        def grant(connection: sqlite3.Connection) -> None:
            _registered_self_user_id(connection, normalized_id)
            connection.execute(
                "INSERT OR IGNORE INTO channel_admins (channel_id, access_scope_id, user_id) "
                "VALUES (?, ?, ?)",
                (normalized_id, scope_id, normalized_user_id),
            )

        return await self._database.run_async(self._mutate_access, normalized_id, grant)

    async def revoke_group_admin(
        self, channel_id: str, access_scope_id: str, user_id: str
    ) -> JsonObject:
        """Remove one additional admin; the configured own identity stays admin."""
        normalized_id = _normalize_channel_id(channel_id)
        scope_id = _normalize_platform_access_id(access_scope_id, "access_scope_id")
        normalized_user_id = _normalize_platform_access_id(user_id, "user_id")

        def revoke(connection: sqlite3.Connection) -> None:
            _registered_self_user_id(connection, normalized_id)
            connection.execute(
                "DELETE FROM channel_admins "
                "WHERE channel_id = ? AND access_scope_id = ? AND user_id = ?",
                (normalized_id, scope_id, normalized_user_id),
            )

        return await self._database.run_async(self._mutate_access, normalized_id, revoke)

    def migrate_group_access(
        self, channel_id: str, old_access_scope_id: str, new_access_scope_id: str
    ) -> None:
        """Merge a group's admins and participants into its new scope after a platform move.

        Admins are united; for a participant seen in both scopes the newer
        sighting wins. The old scope keeps no state afterwards.
        """
        normalized_id = _normalize_channel_id(channel_id)
        old_scope_id = _normalize_platform_access_id(old_access_scope_id, "old_access_scope_id")
        new_scope_id = _normalize_platform_access_id(new_access_scope_id, "new_access_scope_id")
        if old_scope_id == new_scope_id:
            return

        def migrate(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT OR IGNORE INTO channel_admins (channel_id, access_scope_id, user_id) "
                "SELECT channel_id, ?, user_id FROM channel_admins "
                "WHERE channel_id = ? AND access_scope_id = ?",
                (new_scope_id, normalized_id, old_scope_id),
            )
            connection.execute(
                "INSERT INTO channel_participants "
                "(channel_id, access_scope_id, user_id, display_name, last_seen_at) "
                "SELECT channel_id, ?, user_id, display_name, last_seen_at "
                "FROM channel_participants WHERE channel_id = ? AND access_scope_id = ? "
                "ON CONFLICT (channel_id, access_scope_id, user_id) DO UPDATE SET "
                "display_name = excluded.display_name, last_seen_at = excluded.last_seen_at "
                "WHERE excluded.last_seen_at > channel_participants.last_seen_at",
                (new_scope_id, normalized_id, old_scope_id),
            )
            for table in ("channel_admins", "channel_participants"):
                connection.execute(
                    f"DELETE FROM {table} WHERE channel_id = ? AND access_scope_id = ?",
                    (normalized_id, old_scope_id),
                )

        with self._access_lock:
            self._database.write(migrate)
            with self._database.read() as connection:
                self._install_roles(normalized_id, _load_channel_roles(connection, normalized_id))

    def _mutate_access(
        self,
        channel_id: str,
        mutation: Callable[[sqlite3.Connection], None],
    ) -> JsonObject:
        def mutate(connection: sqlite3.Connection) -> tuple[JsonObject, _AccessRoles]:
            mutation(connection)
            return _access_projection(connection, channel_id)

        with self._access_lock:
            projection, roles = self._database.write(mutate)
            self._install_roles(channel_id, roles)
        return projection

    def _install_roles(self, channel_id: str, roles: _AccessRoles | None) -> None:
        updated = dict(self._roles)
        if roles is None:
            updated.pop(channel_id, None)
        else:
            updated[channel_id] = roles
        self._roles = updated

    # -- Conversation routing -------------------------------------------------------

    def active_session_id(self, channel_id: str, conversation_id: str) -> str | None:
        """Return the Session a conversation currently routes to, if it was moved."""
        normalized_id = _normalize_channel_id(channel_id)
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT active_session_id FROM channel_conversations "
                "WHERE channel_id = ? AND conversation_id = ?",
                (normalized_id, conversation_id),
            ).fetchone()
        return None if row is None else row[0]

    def point_conversation(
        self,
        channel_id: str,
        conversation_id: str,
        conversation_kind: str,
        session_id: str,
    ) -> str | None:
        """Route a conversation to ``session_id``; return the pointer it replaced."""
        normalized_id = _normalize_channel_id(channel_id)
        if conversation_kind not in _CONVERSATION_KINDS:
            raise ChannelError(f"Unknown Channel conversation kind: {conversation_kind}")
        updated_at = utc_now_timestamp()

        def point(connection: sqlite3.Connection) -> str | None:
            _registered_self_user_id(connection, normalized_id)
            row = connection.execute(
                "SELECT active_session_id FROM channel_conversations "
                "WHERE channel_id = ? AND conversation_id = ?",
                (normalized_id, conversation_id),
            ).fetchone()
            connection.execute(
                "INSERT INTO channel_conversations "
                "(channel_id, conversation_id, conversation_kind, active_session_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (channel_id, conversation_id) DO UPDATE SET "
                "conversation_kind = excluded.conversation_kind, "
                "active_session_id = excluded.active_session_id, "
                "updated_at = excluded.updated_at",
                (normalized_id, conversation_id, conversation_kind, session_id, updated_at),
            )
            return None if row is None else row[0]

        return self._database.write(point)

    def restore_conversation_pointer(
        self,
        channel_id: str,
        conversation_id: str,
        previous_session_id: str | None,
        *,
        expected_session_id: str,
    ) -> None:
        """Undo one pointer change unless later navigation replaced it."""
        normalized_id = _normalize_channel_id(channel_id)
        updated_at = utc_now_timestamp()

        def restore(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE channel_conversations SET active_session_id = ?, updated_at = ? "
                "WHERE channel_id = ? AND conversation_id = ? AND active_session_id = ?",
                (
                    previous_session_id,
                    updated_at,
                    normalized_id,
                    conversation_id,
                    expected_session_id,
                ),
            )

        self._database.write(restore)

    # -- Run-button origin bindings ---------------------------------------------------

    def save_run_button_binding(self, channel_id: str, binding: RunButtonBinding) -> None:
        """Persist one pending origin binding before its message is sent."""
        normalized_id = _normalize_channel_id(channel_id)

        def save(connection: sqlite3.Connection) -> None:
            _registered_self_user_id(connection, normalized_id)
            connection.execute(
                "INSERT INTO channel_run_buttons "
                "(channel_id, binding_id, platform_target, thread_id, origin_session_id, "
                "button_data_json, created_at, consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    normalized_id,
                    binding.id,
                    binding.platform_target,
                    binding.thread_id,
                    binding.origin_session_id,
                    json.dumps(list(binding.original_button_data), ensure_ascii=False),
                    binding.created_at,
                    utc_now_timestamp() if binding.consumed else None,
                ),
            )

        self._database.write(save)

    def discard_run_button_binding(self, channel_id: str, binding_id: str) -> None:
        """Remove a binding whose outbound platform send failed."""
        normalized_id = _normalize_channel_id(channel_id)

        def discard(connection: sqlite3.Connection) -> None:
            connection.execute(
                "DELETE FROM channel_run_buttons WHERE channel_id = ? AND binding_id = ?",
                (normalized_id, binding_id),
            )

        self._database.write(discard)

    def claim_run_button_binding(
        self,
        channel_id: str,
        binding_id: str,
        *,
        platform_target: str,
        thread_id: str | None,
    ) -> RunButtonClaim:
        """Consume a binding once, when its original target and thread tap a Run button."""
        normalized_id = _normalize_channel_id(channel_id)
        consumed_at = utc_now_timestamp()

        def claim(connection: sqlite3.Connection) -> RunButtonClaim:
            binding = _run_button_binding(connection, normalized_id, binding_id)
            if binding is None:
                return RunButtonClaim(status="missing")
            if binding.platform_target != platform_target or binding.thread_id != thread_id:
                return RunButtonClaim(status="target_mismatch", binding=binding)
            claimed = connection.execute(
                "UPDATE channel_run_buttons SET consumed_at = ? "
                "WHERE channel_id = ? AND binding_id = ? AND consumed_at IS NULL",
                (consumed_at, normalized_id, binding_id),
            ).rowcount
            if claimed != 1:
                return RunButtonClaim(status="consumed", binding=binding)
            return RunButtonClaim(
                status="claimed",
                binding=RunButtonBinding(
                    id=binding.id,
                    platform_target=binding.platform_target,
                    thread_id=binding.thread_id,
                    origin_session_id=binding.origin_session_id,
                    original_button_data=binding.original_button_data,
                    created_at=binding.created_at,
                    consumed=True,
                ),
            )

        return self._database.write(claim)

    def restore_run_button_binding(self, channel_id: str, binding_id: str) -> None:
        """Make a claimed binding retryable when its Run was not admitted."""
        normalized_id = _normalize_channel_id(channel_id)

        def restore(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE channel_run_buttons SET consumed_at = NULL "
                "WHERE channel_id = ? AND binding_id = ? AND consumed_at IS NOT NULL",
                (normalized_id, binding_id),
            )

        self._database.write(restore)

    # -- Inbound receipts -----------------------------------------------------------

    async def has_received(self, channel_id: str, message_ref: str) -> bool:
        """Return whether an inbound message was already handed to the conversation."""
        normalized_id = _normalize_channel_id(channel_id)

        def read(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                "SELECT 1 FROM channel_received WHERE channel_id = ? AND message_ref = ?",
                (normalized_id, message_ref),
            ).fetchone()
            return row is not None

        return await self._database.read_async(read)

    async def record_received(self, channel_id: str, message_ref: str) -> None:
        """Remember one handled inbound message, keeping the newest receipts per Channel."""
        normalized_id = _normalize_channel_id(channel_id)
        received_at = utc_now_timestamp()

        def record(connection: sqlite3.Connection) -> None:
            _registered_self_user_id(connection, normalized_id)
            connection.execute(
                "INSERT OR IGNORE INTO channel_received (channel_id, message_ref, received_at) "
                "VALUES (?, ?, ?)",
                (normalized_id, message_ref, received_at),
            )
            connection.execute(
                "DELETE FROM channel_received WHERE channel_id = ? AND message_ref IN ("
                "SELECT message_ref FROM channel_received WHERE channel_id = ? "
                "ORDER BY received_at DESC, message_ref DESC LIMIT -1 OFFSET ?)",
                (normalized_id, normalized_id, RECEIVED_MESSAGE_WINDOW),
            )

        await self._database.write_async(record)

    # -- Telegram polling watermark ---------------------------------------------------

    def load_update_offset(self, channel_id: str, bot_id: int) -> int:
        """Return the bot's fresh update high-water mark; 0 if unknown, expired or another bot's."""
        normalized_id = _normalize_channel_id(channel_id)
        normalized_bot_id = _telegram_id(bot_id, "Telegram bot id", minimum=1)
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT bot_id, last_update_id, updated_at FROM channel_polling "
                "WHERE channel_id = ?",
                (normalized_id,),
            ).fetchone()
        if row is None or row[0] != normalized_bot_id or row[2] <= _polling_expiry_cutoff():
            return 0
        return int(row[1])

    def save_update_offset(self, channel_id: str, bot_id: int, update_id: int) -> None:
        """Raise the bot's fresh high-water mark; another bot's or an expired mark yields to any."""
        normalized_id = _normalize_channel_id(channel_id)
        normalized_bot_id = _telegram_id(bot_id, "Telegram bot id", minimum=1)
        normalized_update_id = _telegram_id(update_id, "Telegram update id", minimum=0)
        updated_at = utc_now_timestamp()
        cutoff = _polling_expiry_cutoff()

        def save(connection: sqlite3.Connection) -> None:
            _registered_self_user_id(connection, normalized_id)
            connection.execute(
                "INSERT INTO channel_polling (channel_id, bot_id, last_update_id, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (channel_id) DO UPDATE SET "
                "bot_id = excluded.bot_id, last_update_id = excluded.last_update_id, "
                "updated_at = excluded.updated_at "
                "WHERE channel_polling.bot_id IS NOT excluded.bot_id "
                "OR excluded.last_update_id > channel_polling.last_update_id "
                "OR channel_polling.updated_at <= ?",
                (normalized_id, normalized_bot_id, normalized_update_id, updated_at, cutoff),
            )

        self._database.write(save)


def _polling_expiry_cutoff() -> str:
    return format_canonical_timestamp(
        datetime.now(UTC) - timedelta(seconds=TELEGRAM_UPDATE_OFFSET_TTL_SECONDS)
    )


def _telegram_id(value: int, name: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        qualifier = "positive" if minimum > 0 else "non-negative"
        raise ChannelError(f"{name} must be a {qualifier} integer")
    return value


def _platform(platform: str) -> str:
    if not isinstance(platform, str) or not platform.strip():
        raise ChannelConfigError("Channel platform must be a non-empty string")
    return platform.strip()


def _bind_platform(connection: sqlite3.Connection, channel_id: str, platform: str) -> bool:
    """Record ``platform`` for a registered Channel; reset state recorded for another one."""
    row = connection.execute(
        "SELECT platform FROM channels WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    recorded = row[0]
    if recorded == platform:
        return False
    if recorded is None:
        connection.execute(
            "UPDATE channels SET platform = ? WHERE channel_id = ?", (platform, channel_id)
        )
        return False
    kept = connection.execute(
        "SELECT conversation_id, conversation_kind, active_session_id, updated_at "
        "FROM channel_conversations WHERE channel_id = ? AND conversation_id = ?",
        (channel_id, main_conversation_id(channel_id)),
    ).fetchall()
    connection.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
    connection.execute(
        "INSERT INTO channels (channel_id, platform) VALUES (?, ?)", (channel_id, platform)
    )
    connection.executemany(
        "INSERT INTO channel_conversations "
        "(channel_id, conversation_id, conversation_kind, active_session_id, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [(channel_id, *row) for row in kept],
    )
    return True


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
            self_user_id=self_ids.get(channel_id), admins=frozenset(admins[channel_id])
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
    return _AccessRoles(self_user_id=row[0], admins=admins)


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
    roles = _AccessRoles(
        self_user_id=self_user_id,
        admins=frozenset(
            (access_scope_id, user_id)
            for access_scope_id, user_ids in admins.items()
            for user_id in user_ids
        ),
    )
    groups: list[JsonObject] = []
    for access_scope_id in sorted(set(admins) | set(participants)):
        admin_ids = set(admins.get(access_scope_id, ()))
        if self_user_id is not None:
            admin_ids.add(self_user_id)
        groups.append(
            {
                "access_scope_id": access_scope_id,
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
        "groups": groups,
    }
    return projection, roles


def _run_button_binding(
    connection: sqlite3.Connection, channel_id: str, binding_id: str
) -> RunButtonBinding | None:
    row = connection.execute(
        "SELECT platform_target, thread_id, origin_session_id, button_data_json, created_at, "
        "consumed_at FROM channel_run_buttons WHERE channel_id = ? AND binding_id = ?",
        (channel_id, binding_id),
    ).fetchone()
    if row is None:
        return None
    platform_target, thread_id, origin_session_id, button_data_json, created_at, consumed_at = row
    button_data = json.loads(button_data_json)
    if not all(isinstance(item, str) for item in button_data):
        raise ChannelError(f"Run-button binding {binding_id!r} has invalid button data")
    return RunButtonBinding(
        id=binding_id,
        platform_target=platform_target,
        thread_id=thread_id,
        origin_session_id=origin_session_id,
        original_button_data=tuple(button_data),
        created_at=created_at,
        consumed=consumed_at is not None,
    )


def _normalize_platform_access_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChannelConfigError(f"{field_name} must be a non-empty string")
    return value.strip()


__all__ = ["RECEIVED_MESSAGE_WINDOW", "ChannelStateStore"]
