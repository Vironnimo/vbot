"""Generation 1 conversion of the Channel state into ``channels.db``.

Before Generation 1 every Channel kept its state in files beside
``channels/<id>/channel.json``:

- ``access.json``: ``{"version": 1, "self_user_id", "groups": {scope:
  {"admin_user_ids": [...], "participants": {user: {"display_name",
  "last_seen_at"}}}}}``;
- ``run-button-bindings.json``: ``{"version": 1, "bindings": {id:
  {"platform_target", "thread_id", "origin_session_id", "original_button_data",
  "created_at", "consumed"}}}``;
- ``polling.json``: ``{"version": 1, "last_update_id"}``, the Telegram polling
  watermark, which did not name the bot whose update ids it counted;
- ``received.json``: the newest inbound receipts of a socket platform, oldest
  first.

A conversation moved by ``/new``, a bound Run tap or a chat migration kept its
routing pointer as ``active_session_id`` in the metadata of its anchor Session
in ``sessions.db``, next to the ``conversation_kind`` of every Channel Session.

This area registers every Channel directory that holds a ``channel.json``
with the platform that config names (none when it cannot be read), stages its
state and the routing pointers of its current Agent in a new ``channels.db``,
and retires every state file. Old timestamps become canonical UTC; a consumed
binding takes its file time as ``consumed_at``, and receipts get increasing
times that end at their file time. Invalid entries are skipped and reported,
and so are pointers of an anchor Session that belongs to another Agent than the
one the Channel routes to. A polling watermark is dropped and reported: the
watermark in ``channels.db`` applies only to the bot it names, so the first
start of the Channel may see Telegram redeliver updates it never confirmed.
Session metadata itself is converted by the Session area, which drops the
retired routing keys.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core.channels import channel_database_spec
from core.channels.config import (
    ALLOWED_CHANNEL_PLATFORMS,
    ChannelConfigError,
    _normalize_channel_id,
)
from core.channels.state import RECEIVED_MESSAGE_WINDOW
from core.database import open_offline_database
from core.utils.timestamps import (
    canonical_timestamp,
    format_canonical_timestamp,
    utc_now_timestamp,
)
from scripts.converters.persistence_generation_1._context import ConversionContext

AREA = "channels"
TARGET_DATABASE = "channels.db"
SOURCE_SESSIONS_DATABASE = "sessions.db"

_CHANNELS_DIRECTORY = "channels"
_CONFIG_FILE = "channel.json"
_ACCESS_FILE = "access.json"
_BINDINGS_FILE = "run-button-bindings.json"
_POLLING_FILE = "polling.json"
_RECEIVED_FILE = "received.json"
_STATE_FILES = (_ACCESS_FILE, _BINDINGS_FILE, _POLLING_FILE, _RECEIVED_FILE)
_STATE_FILE_VERSION = 1
_CONVERSATION_KINDS = frozenset({"direct", "group"})


class _UnreadableError(Exception):
    """A state file that cannot be converted at all."""


@dataclass(slots=True)
class _Rows:
    """Everything staged into ``channels.db``, in insertion order."""

    # Channel id to the platform its config names.
    platforms: dict[str, str | None] = field(default_factory=dict)
    # Channel id to its own platform identity.
    channels: dict[str, str | None] = field(default_factory=dict)
    admins: list[tuple[str, str, str]] = field(default_factory=list)
    participants: list[tuple[str, str, str, str, str]] = field(default_factory=list)
    conversations: list[tuple[str, str, str, str, str]] = field(default_factory=list)
    run_buttons: list[tuple[str, str, str, str | None, str, str, str, str | None]] = field(
        default_factory=list
    )
    received: list[tuple[str, str, str]] = field(default_factory=list)


def convert(context: ConversionContext) -> None:
    """Stage ``channels.db`` from the source Channel state and retire the state files."""
    rows = _Rows()
    agents: dict[str, str | None] = {}
    for directory in _channel_directories(context):
        channel_id = _registered_channel_id(context, directory)
        if channel_id is not None:
            agents[channel_id], rows.platforms[channel_id] = _channel_config(context, directory)
            _convert_channel_state(context, rows, channel_id, directory)
        for name in _STATE_FILES:
            if (directory / name).is_file():
                context.retire(_relative(context, directory / name))
    _convert_routing_pointers(context, rows, agents)

    target = context.staged(TARGET_DATABASE)
    # A repeated run replaces its own staged output instead of appending to it.
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{target}{suffix}").unlink(missing_ok=True)
    database = open_offline_database(channel_database_spec(target))
    try:
        database.write(lambda connection: _insert(connection, rows))
    finally:
        database.close()
    for key, amount in (
        ("channels", len(rows.channels)),
        ("admins", len(rows.admins)),
        ("participants", len(rows.participants)),
        ("conversations", len(rows.conversations)),
        ("run_buttons", len(rows.run_buttons)),
        ("received", len(rows.received)),
    ):
        context.report.count(AREA, key, amount)


def _channel_directories(context: ConversionContext) -> list[Path]:
    root = context.source_path(_CHANNELS_DIRECTORY)
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def _registered_channel_id(context: ConversionContext, directory: Path) -> str | None:
    """Return the Channel id the application registers for this directory, if any."""
    relative = _relative(context, directory)
    if not (directory / _CONFIG_FILE).is_file():
        if any((directory / name).is_file() for name in _STATE_FILES):
            context.report.skip(AREA, relative, "state without channel.json dropped")
        return None
    try:
        return _normalize_channel_id(directory.name)
    except ChannelConfigError as error:
        context.report.skip(AREA, relative, f"state of an invalid Channel id dropped ({error})")
        return None


def _channel_config(context: ConversionContext, directory: Path) -> tuple[str | None, str | None]:
    """Return the Agent id and the platform a Channel's config names, where readable."""
    path = directory / _CONFIG_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    platform = payload.get("platform")
    agent_id = payload.get("agent_id")
    if isinstance(agent_id, str) and agent_id.strip():
        agent_id = agent_id.strip()
    else:
        context.report.skip(
            AREA,
            _relative(context, path),
            "no readable agent_id; the Channel's routing pointers are dropped",
        )
        agent_id = None
    if not isinstance(platform, str) or platform not in ALLOWED_CHANNEL_PLATFORMS:
        platform = None
    return agent_id, platform


def _convert_channel_state(
    context: ConversionContext, rows: _Rows, channel_id: str, directory: Path
) -> None:
    rows.channels[channel_id] = None
    polling = directory / _POLLING_FILE
    if polling.is_file():
        context.report.skip(
            AREA,
            _relative(context, polling),
            "polling watermark dropped: it does not name its Telegram bot",
        )
    for name, reader in (
        (_ACCESS_FILE, _read_access),
        (_BINDINGS_FILE, _read_run_buttons),
        (_RECEIVED_FILE, _read_received),
    ):
        path = directory / name
        if not path.is_file():
            continue
        relative = _relative(context, path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            file_time = _file_time(path)
            reader(context, rows, channel_id, relative, payload, file_time)
        except (OSError, UnicodeError, ValueError, _UnreadableError) as error:
            context.report.skip(AREA, relative, f"unreadable state dropped ({error})")


def _read_access(
    context: ConversionContext,
    rows: _Rows,
    channel_id: str,
    relative: str,
    payload: Any,
    file_time: datetime,
) -> None:
    document = _versioned_object(payload)
    self_user_id = document.get("self_user_id")
    if self_user_id is not None and (not isinstance(self_user_id, str) or not self_user_id):
        context.report.skip(AREA, relative, "invalid self_user_id dropped")
        self_user_id = None
    groups = document.get("groups")
    if not isinstance(groups, dict):
        raise _UnreadableError("groups must be an object")
    for scope_id, group in sorted(groups.items()):
        item = f"{relative}#{scope_id}"
        if not isinstance(scope_id, str) or not scope_id or not isinstance(group, dict):
            context.report.skip(AREA, item, "invalid group dropped")
            continue
        admins = group.get("admin_user_ids", [])
        if not isinstance(admins, list):
            context.report.skip(AREA, item, "invalid admin_user_ids dropped")
            admins = []
        for user_id in sorted({user for user in admins if isinstance(user, str) and user}):
            rows.admins.append((channel_id, scope_id, user_id))
        if any(not isinstance(user, str) or not user for user in admins):
            context.report.skip(AREA, item, "invalid admin user ids dropped")
        participants = group.get("participants", {})
        if not isinstance(participants, dict):
            context.report.skip(AREA, item, "invalid participants dropped")
            participants = {}
        for user_id, participant in sorted(participants.items()):
            if not isinstance(user_id, str) or not user_id or not isinstance(participant, dict):
                context.report.skip(AREA, f"{item}/{user_id}", "invalid participant dropped")
                continue
            display_name = participant.get("display_name")
            if not isinstance(display_name, str) or not display_name.strip():
                display_name = user_id
            last_seen_at = _canonical_time(participant.get("last_seen_at"))
            if last_seen_at is None:
                context.report.skip(
                    AREA, f"{item}/{user_id}", "invalid last_seen_at replaced by the file time"
                )
                last_seen_at = format_canonical_timestamp(file_time)
            rows.participants.append(
                (channel_id, scope_id, user_id, display_name.strip(), last_seen_at)
            )
    rows.channels[channel_id] = self_user_id


def _read_run_buttons(
    context: ConversionContext,
    rows: _Rows,
    channel_id: str,
    relative: str,
    payload: Any,
    file_time: datetime,
) -> None:
    bindings = _versioned_object(payload).get("bindings")
    if not isinstance(bindings, dict):
        raise _UnreadableError("bindings must be an object")
    for binding_id, binding in sorted(bindings.items()):
        item = f"{relative}#{binding_id}"
        try:
            _validate_binding(binding)
        except _UnreadableError as error:
            context.report.skip(AREA, item, f"invalid binding dropped ({error})")
            continue
        created_at = _canonical_time(binding.get("created_at"))
        if created_at is None:
            context.report.skip(AREA, item, "invalid created_at replaced by the file time")
            created_at = format_canonical_timestamp(file_time)
        rows.run_buttons.append(
            (
                channel_id,
                binding_id,
                binding["platform_target"],
                binding.get("thread_id"),
                binding["origin_session_id"],
                json.dumps(binding["original_button_data"], ensure_ascii=False),
                created_at,
                # The last write of the file is the latest possible claim time.
                format_canonical_timestamp(file_time) if binding["consumed"] else None,
            )
        )


def _validate_binding(binding: Any) -> None:
    if not isinstance(binding, dict):
        raise _UnreadableError("binding must be an object")
    platform_target = binding.get("platform_target")
    thread_id = binding.get("thread_id")
    origin_session_id = binding.get("origin_session_id")
    button_data = binding.get("original_button_data")
    if not isinstance(platform_target, str) or not platform_target:
        raise _UnreadableError("invalid platform_target")
    if thread_id is not None and not isinstance(thread_id, str):
        raise _UnreadableError("invalid thread_id")
    if not isinstance(origin_session_id, str) or not origin_session_id:
        raise _UnreadableError("invalid origin_session_id")
    if (
        not isinstance(button_data, list)
        or not button_data
        or not all(isinstance(data, str) and data.split(":", 1)[0] == "run" for data in button_data)
    ):
        raise _UnreadableError("invalid original_button_data")
    if not isinstance(binding.get("consumed"), bool):
        raise _UnreadableError("invalid consumed state")


def _read_received(
    context: ConversionContext,
    rows: _Rows,
    channel_id: str,
    relative: str,
    payload: Any,
    file_time: datetime,
) -> None:
    if not isinstance(payload, list):
        raise _UnreadableError("receipts must be a JSON array")
    receipts = [item for item in payload if isinstance(item, str) and item]
    if len(receipts) != len(payload):
        context.report.skip(AREA, relative, "invalid receipts dropped")
    # Oldest first; a repeated receipt keeps its newest position.
    ordered = list(dict.fromkeys(reversed(receipts)))[:RECEIVED_MESSAGE_WINDOW]
    for age, message_ref in enumerate(ordered):
        received_at = format_canonical_timestamp(file_time - timedelta(microseconds=age))
        rows.received.append((channel_id, message_ref, received_at))


def _convert_routing_pointers(
    context: ConversionContext, rows: _Rows, agents: dict[str, str | None]
) -> None:
    """Stage the ``active_session_id`` pointers of anchor Sessions as conversations."""
    path = context.source_path(SOURCE_SESSIONS_DATABASE)
    if not path.is_file():
        return
    try:
        sessions = _live_channel_sessions(path)
    except sqlite3.Error as error:
        context.report.skip(AREA, SOURCE_SESSIONS_DATABASE, f"routing pointers dropped ({error})")
        return
    updated_at = utc_now_timestamp()
    for (agent_id, session_id), (source_channel_id, metadata) in sorted(sessions.items()):
        active_session_id = metadata.get("active_session_id")
        if not isinstance(active_session_id, str) or not active_session_id:
            continue
        item = f"{SOURCE_SESSIONS_DATABASE}#{agent_id}/{session_id}"
        channel_id = _anchor_channel(session_id, source_channel_id, agents)
        if channel_id is None:
            context.report.skip(AREA, item, "routing pointer of no configured Channel dropped")
            continue
        channel_agent_id = agents[channel_id]
        if channel_agent_id is None:
            context.report.skip(AREA, item, "routing pointer of a Channel without Agent dropped")
            continue
        if channel_agent_id != agent_id:
            # Routing only ever looked at anchors of the Channel's current Agent.
            context.report.skip(
                AREA, item, f"routing pointer of an Agent {channel_id} no longer uses dropped"
            )
            continue
        kind = _conversation_kind(metadata)
        if kind is None:
            active = sessions.get((agent_id, active_session_id))
            kind = _conversation_kind(active[1]) if active is not None else None
        if kind is None:
            context.report.skip(AREA, item, "unknown conversation kind recorded as direct")
            kind = "direct"
        rows.conversations.append((channel_id, session_id, kind, active_session_id, updated_at))


def _live_channel_sessions(
    path: Path,
) -> dict[tuple[str, str], tuple[str | None, dict[str, Any]]]:
    """Read the Channel-relevant columns of every live Identity Agent Session."""
    sessions: dict[tuple[str, str], tuple[str | None, dict[str, Any]]] = {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
        for agent_id, session_id, source_channel_id, metadata_json in database.execute(
            "SELECT agent_id, session_id, source_channel_id, metadata_json FROM sessions "
            "WHERE status = 'live' AND project_id = ''"
        ):
            try:
                metadata = json.loads(metadata_json)
            except (TypeError, ValueError):
                continue
            if isinstance(metadata, dict):
                sessions[(str(agent_id), str(session_id))] = (source_channel_id, metadata)
    return sessions


def _anchor_channel(
    session_id: str, source_channel_id: str | None, agents: dict[str, str | None]
) -> str | None:
    """Return the Channel whose derived anchor ids start like ``session_id``."""
    candidates = [channel_id for channel_id in agents if session_id.startswith(f"ch-{channel_id}-")]
    if source_channel_id in candidates:
        return source_channel_id
    return max(candidates, key=len, default=None)


def _conversation_kind(metadata: dict[str, Any]) -> str | None:
    kind = metadata.get("conversation_kind")
    return kind if kind in _CONVERSATION_KINDS else None


def _insert(connection: sqlite3.Connection, rows: _Rows) -> None:
    statements: Iterable[tuple[str, list[Any]]] = (
        (
            "INSERT INTO channels (channel_id, platform, self_user_id) VALUES (?, ?, ?)",
            [
                (channel_id, rows.platforms.get(channel_id), self_user_id)
                for channel_id, self_user_id in rows.channels.items()
            ],
        ),
        (
            "INSERT INTO channel_admins (channel_id, access_scope_id, user_id) VALUES (?, ?, ?)",
            rows.admins,
        ),
        (
            "INSERT INTO channel_participants "
            "(channel_id, access_scope_id, user_id, display_name, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?)",
            rows.participants,
        ),
        (
            "INSERT INTO channel_conversations "
            "(channel_id, conversation_id, conversation_kind, active_session_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            rows.conversations,
        ),
        (
            "INSERT INTO channel_run_buttons "
            "(channel_id, binding_id, platform_target, thread_id, origin_session_id, "
            "button_data_json, created_at, consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows.run_buttons,
        ),
        (
            "INSERT INTO channel_received (channel_id, message_ref, received_at) VALUES (?, ?, ?)",
            rows.received,
        ),
    )
    for statement, values in statements:
        connection.executemany(statement, values)


def _versioned_object(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("version") != _STATE_FILE_VERSION:
        raise _UnreadableError("unsupported state file version")
    return payload


def _canonical_time(value: Any) -> str | None:
    """Return an aware ISO 8601 time as canonical UTC text, or ``None``."""
    if not isinstance(value, str):
        return None
    try:
        return canonical_timestamp(value)
    except ValueError:
        return None


def _file_time(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def _relative(context: ConversionContext, path: Path) -> str:
    return path.relative_to(context.source).as_posix()
