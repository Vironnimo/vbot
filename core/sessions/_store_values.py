"""Session rows, the metadata facade, scope and value validation."""
# ruff: noqa: E501

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from core.chat.errors import ChatSessionError
from core.sessions.errors import (
    SessionNotFoundError,
    SessionStoreCorruptError,
)
from core.utils.ids import new_id
from core.utils.timestamps import canonical_timestamp

if TYPE_CHECKING:
    from core.sessions._types import SessionAddress


_LOGGER = logging.getLogger("vbot.sessions")
_DESCRIPTOR_SOURCE_BATCH_SIZE = 900
JsonObject = dict[str, Any]

# The Session columns every state read selects; metadata facade reads add the
# derived values in ``_SESSION_METADATA_SQL``.
_SESSION_STATE_COLUMNS = """
    s.session_key, s.generation_id, s.project_id, s.agent_id, s.session_id, s.state,
    s.created_at, s.archived_at, s.next_seq, s.history_revision, s.state_revision,
    s.cursor_floor_seq, s.last_activity_at, s.last_entry_id, s.fork_parent_key,
    s.forked_at, s.fork_point_seq, s.title, s.auto_title, s.auto_title_initialized,
    s.source_channel_id, s.platform, s.platform_conv_id, s.is_subagent,
    s.subagent_parent_id, s.subagent_parent_project_id, s.subagent_parent_agent_id,
    s.subagent_parent_session_id, s.subagent_parent_run_id,
    s.subagent_parent_tool_call_id, s.subagent_parent_tool_call_index,
    s.list_visibility_mask, s.latest_completion_run_id, s.latest_completion_status,
    s.latest_completion_at, s.read_completion_run_id, s.prompt_cache_affinity_id,
    s.seen_skills_initialized, s.compaction_policy_json, s.metadata_json
"""
# Derived facade values: the direct fork source's address and the Run kinds.
_DERIVED_METADATA_COLUMNS = """
    fork_parent.project_id AS fork_parent_project_id,
    fork_parent.agent_id AS fork_parent_agent_id,
    fork_parent.session_id AS fork_parent_session_id,
    (SELECT json_group_array(k.run_kind) FROM session_run_kinds AS k
     WHERE k.session_key = s.session_key) AS run_kinds_json
"""
_DERIVED_METADATA_JOIN = (
    "LEFT JOIN sessions AS fork_parent ON fork_parent.session_key = s.fork_parent_key"
)
_SESSION_LIST_COLUMNS = f"""
    s.project_id,
    s.agent_id,
    s.session_id,
    s.created_at,
    s.last_activity_at,
    s.title,
    s.auto_title,
    s.source_channel_id,
    s.platform,
    s.platform_conv_id,
    s.is_subagent,
    s.subagent_parent_id,
    s.subagent_parent_project_id,
    s.subagent_parent_agent_id,
    s.subagent_parent_session_id,
    s.subagent_parent_run_id,
    s.subagent_parent_tool_call_id,
    s.subagent_parent_tool_call_index,
    s.forked_at,
    s.fork_point_seq,
    s.compaction_policy_json,
    s.latest_completion_run_id,
    s.latest_completion_status,
    s.latest_completion_at,
    s.read_completion_run_id,
    {_DERIVED_METADATA_COLUMNS}
"""
_SESSION_LIST_BACKGROUND_KINDS = (
    "cron",
    "reflection",
    "memory_reflection",
    "skill_reflection",
)
_RECALL_VALID_RUN_KINDS = (
    "user",
    "channel",
    "calendar",
    "cron",
    "reflection",
    "memory_reflection",
    "skill_reflection",
    "subagent",
    "system",
)
_RECALL_REFLECTION_RUN_KINDS = (
    "reflection",
    "memory_reflection",
    "skill_reflection",
)
_RECALL_USER_FACING_RUN_KINDS = ("user", "channel", "cron", "calendar")
# Summary values a Session list may add on request, by name.
_SUMMARY_METADATA_COLUMNS = {
    "seen_skills": (
        "CASE WHEN s.seen_skills_initialized = 1 THEN "
        "(SELECT json_group_array(k.skill_name) FROM (SELECT skill_name FROM session_seen_skills "
        "WHERE session_key = s.session_key ORDER BY skill_name) AS k) END"
    )
}
_LIST_VISIBILITY_SUBAGENT_SESSION = 1 << 0
_LIST_VISIBILITY_BACKGROUND = 1 << 1
_LIST_VISIBILITY_CRON = 1 << 2
_LIST_VISIBILITY_MEMORY_REFLECTION = 1 << 3
_LIST_VISIBILITY_SKILL_REFLECTION = 1 << 4
_LIST_VISIBILITY_REFLECTION = 1 << 5
_LIST_VISIBILITY_VALID_RUN_KINDS = 1 << 6
_LIST_VISIBILITY_USER_FACING = 1 << 7
_LIST_VISIBILITY_SUBAGENT_RUN_KIND = 1 << 8
_LIST_VISIBILITY_SUBAGENT_PARENT = 1 << 9

# Metadata facade keys stored in dedicated columns.
_TITLE_KEYS = ("title", "auto_title")
_CHANNEL_KEYS = ("source_channel_id", "platform", "platform_conv_id")
_AUTO_TITLE_INITIALIZED_KEY = "auto_title_initialized"
_SUBAGENT_FLAG_KEY = "is_subagent_session"
_SUBAGENT_PARENT_KEY = "subagent_parent"
_COMPACTION_POLICY_KEY = "compaction_policy"
# Facade keys derived from relations; a write may repeat but never change them.
_FORK_SOURCE_KEY = "fork_source"
_RUN_KINDS_KEY = "run_kinds"
# Subagent parent fields in facade order, each with its column.
_SUBAGENT_PARENT_FIELDS = (
    ("id", "subagent_parent_id"),
    ("agent_id", "subagent_parent_agent_id"),
    ("session_id", "subagent_parent_session_id"),
    ("run_id", "subagent_parent_run_id"),
    ("tool_call_id", "subagent_parent_tool_call_id"),
    ("tool_call_index", "subagent_parent_tool_call_index"),
    ("project_id", "subagent_parent_project_id"),
)
# Prompt state has dedicated Session APIs and never enters open metadata.
_PROMPT_STATE_KEYS = frozenset({"seen_skills", "prompt_cache_affinity_id"})
_PROMPT_PIN_KEY_PREFIX = "pinned_"
# Column names a metadata write assigns, in ``_MetadataStorage.columns`` order.
_METADATA_WRITE_COLUMNS = (
    "title",
    "auto_title",
    "auto_title_initialized",
    "source_channel_id",
    "platform",
    "platform_conv_id",
    "is_subagent",
    *(column for _key, column in _SUBAGENT_PARENT_FIELDS),
    "compaction_policy_json",
    "metadata_json",
    "list_visibility_mask",
)


@dataclass(frozen=True)
class _MetadataStorage:
    """The persisted form of one metadata facade value."""

    columns: tuple[Any, ...]


def _session_list_visibility_mask(metadata: JsonObject) -> int:
    mask = 0
    if metadata.get(_SUBAGENT_FLAG_KEY) is True:
        mask |= _LIST_VISIBILITY_SUBAGENT_SESSION
    if isinstance(metadata.get(_SUBAGENT_PARENT_KEY), dict):
        mask |= _LIST_VISIBILITY_SUBAGENT_PARENT

    run_kinds = metadata.get(_RUN_KINDS_KEY)
    valid_run_kinds = (
        isinstance(run_kinds, list)
        and bool(run_kinds)
        and all(isinstance(kind, str) and kind in _RECALL_VALID_RUN_KINDS for kind in run_kinds)
    )
    if not valid_run_kinds:
        return mask
    mask |= _LIST_VISIBILITY_VALID_RUN_KINDS
    kinds = set(cast(list[str], run_kinds))
    if kinds & set(_RECALL_REFLECTION_RUN_KINDS):
        mask |= _LIST_VISIBILITY_REFLECTION
    if kinds & set(_RECALL_USER_FACING_RUN_KINDS):
        mask |= _LIST_VISIBILITY_USER_FACING
    if "cron" in kinds:
        mask |= _LIST_VISIBILITY_CRON
    if "memory_reflection" in kinds:
        mask |= _LIST_VISIBILITY_MEMORY_REFLECTION
    if "skill_reflection" in kinds:
        mask |= _LIST_VISIBILITY_SKILL_REFLECTION
    if "reflection" in kinds:
        mask |= _LIST_VISIBILITY_REFLECTION
    if "subagent" in kinds:
        mask |= _LIST_VISIBILITY_SUBAGENT_RUN_KIND

    platform = metadata.get("platform")
    platform_conversation = metadata.get("platform_conv_id")
    is_channel = (
        isinstance(platform, str)
        and bool(platform.strip())
        and isinstance(platform_conversation, str)
        and bool(platform_conversation.strip())
    )
    if not is_channel and kinds <= set(_SESSION_LIST_BACKGROUND_KINDS):
        mask |= _LIST_VISIBILITY_BACKGROUND
    return mask


def _is_reserved_prompt_key(key: str) -> bool:
    return key in _PROMPT_STATE_KEYS or key.startswith(_PROMPT_PIN_KEY_PREFIX)


def _optional_text_value(metadata: JsonObject, key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ChatSessionError(f"Session metadata {key} must be a string")
    return value


def _subagent_parent_columns(value: Any) -> tuple[Any, ...]:
    if value is None:
        return (None,) * len(_SUBAGENT_PARENT_FIELDS)
    if not isinstance(value, dict):
        raise ChatSessionError("Session metadata subagent_parent must be an object")
    known = {key for key, _column in _SUBAGENT_PARENT_FIELDS}
    unknown = set(value) - known
    if unknown:
        raise ChatSessionError(
            "Session metadata subagent_parent has unsupported fields: " + ", ".join(sorted(unknown))
        )
    columns: list[Any] = []
    for key, _column in _SUBAGENT_PARENT_FIELDS:
        field = value.get(key)
        if key == "tool_call_index":
            if field is not None and (
                isinstance(field, bool) or not isinstance(field, int) or field < 0
            ):
                raise ChatSessionError("Session subagent_parent tool_call_index is invalid")
        elif field is not None and not isinstance(field, str):
            raise ChatSessionError(f"Session subagent_parent {key} must be a string")
        columns.append(field)
    return tuple(columns)


def _session_metadata_storage(metadata: JsonObject, derived: JsonObject) -> _MetadataStorage:
    """Split the metadata facade into its columns and the open metadata object.

    *derived* holds the current relation-backed values (``fork_source`` and
    ``run_kinds``); a write may repeat them but not change them. Prompt state
    keys are rejected: they have dedicated Session APIs.
    """
    if not isinstance(metadata, dict):
        raise ChatSessionError("session metadata must be an object")
    residual = dict(metadata)
    for key in (_FORK_SOURCE_KEY, _RUN_KINDS_KEY):
        if key in residual and residual.pop(key) != derived.get(key):
            raise ChatSessionError(f"Session metadata {key} is managed by Sessions")
    reserved = sorted(key for key in residual if _is_reserved_prompt_key(key))
    if reserved:
        raise ChatSessionError(
            "Session prompt state has dedicated APIs, not metadata: " + ", ".join(reserved)
        )
    titles = tuple(_optional_text_value(residual, key) for key in _TITLE_KEYS)
    channels = tuple(_optional_text_value(residual, key) for key in _CHANNEL_KEYS)
    initialized = residual.get(_AUTO_TITLE_INITIALIZED_KEY)
    if initialized is not None and not isinstance(initialized, bool):
        raise ChatSessionError("Session metadata auto_title_initialized must be a boolean")
    subagent = residual.get(_SUBAGENT_FLAG_KEY)
    if subagent is not None and not isinstance(subagent, bool):
        raise ChatSessionError("Session metadata is_subagent_session must be a boolean")
    parent = _subagent_parent_columns(residual.get(_SUBAGENT_PARENT_KEY))
    policy = residual.get(_COMPACTION_POLICY_KEY)
    if policy is not None and not isinstance(policy, dict):
        raise ChatSessionError("Session metadata compaction_policy must be an object")
    for key in (
        *_TITLE_KEYS,
        *_CHANNEL_KEYS,
        _AUTO_TITLE_INITIALIZED_KEY,
        _SUBAGENT_FLAG_KEY,
        _SUBAGENT_PARENT_KEY,
        _COMPACTION_POLICY_KEY,
    ):
        residual.pop(key, None)
    visibility = dict(metadata)
    visibility[_RUN_KINDS_KEY] = derived.get(_RUN_KINDS_KEY)
    return _MetadataStorage(
        (
            *titles,
            int(initialized is True),
            *channels,
            int(subagent is True),
            *parent,
            None if policy is None else _json_object(policy, "compaction policy"),
            _json_object(residual, "session metadata"),
            _session_list_visibility_mask(visibility),
        )
    )


def _derived_metadata_from_state(state: sqlite3.Row) -> JsonObject:
    """Return the relation-backed facade values of one metadata row."""
    derived: JsonObject = {}
    if state["fork_parent_session_id"] is not None:
        derived[_FORK_SOURCE_KEY] = {
            "agent_id": str(state["fork_parent_agent_id"]),
            "session_id": str(state["fork_parent_session_id"]),
            "project_id": str(state["fork_parent_project_id"]) or None,
            "forked_at": str(state["forked_at"]),
            "message_count": int(state["fork_point_seq"]),
        }
    run_kinds = _json_value_from_payload(
        str(state["run_kinds_json"] or "[]"), "Session run kinds", list
    )
    if run_kinds:
        derived[_RUN_KINDS_KEY] = sorted(run_kinds)
    return derived


def _session_metadata_from_state(state: sqlite3.Row) -> JsonObject:
    """Build the metadata facade from one row of ``_SESSION_METADATA_SQL``."""
    metadata = _json_from_payload(str(state["metadata_json"]), "session metadata")
    for key in _TITLE_KEYS + _CHANNEL_KEYS:
        if state[key] is not None:
            metadata[key] = str(state[key])
    if state["auto_title_initialized"]:
        metadata[_AUTO_TITLE_INITIALIZED_KEY] = True
    if state["is_subagent"]:
        metadata[_SUBAGENT_FLAG_KEY] = True
    parent = _subagent_parent_from_state(state)
    if parent is not None:
        metadata[_SUBAGENT_PARENT_KEY] = parent
    if state["compaction_policy_json"] is not None:
        metadata[_COMPACTION_POLICY_KEY] = _json_from_payload(
            str(state["compaction_policy_json"]), "Session compaction policy"
        )
    metadata.update(_derived_metadata_from_state(state))
    return metadata


def _subagent_parent_from_state(state: sqlite3.Row) -> JsonObject | None:
    values = {key: state[column] for key, column in _SUBAGENT_PARENT_FIELDS}
    if all(value is None for value in values.values()):
        return None
    return values


def _session_list_visibility_sql(
    *,
    include_subagents: bool,
    include_memory_reflections: bool,
    include_skill_reflections: bool,
    include_cron: bool,
    include_channels: bool,
) -> tuple[str, list[Any]]:
    """Return the ``sessions AS s`` predicate of one Session-list filter set."""
    is_subagent = (
        "((s.list_visibility_mask & "
        f"{_LIST_VISIBILITY_SUBAGENT_SESSION | _LIST_VISIBILITY_SUBAGENT_PARENT}) != 0)"
    )
    is_background = f"((s.list_visibility_mask & {_LIST_VISIBILITY_BACKGROUND}) != 0)"
    background_enabled = (
        f"((s.list_visibility_mask & {_LIST_VISIBILITY_CRON}) = 0 OR ? = 1) "
        f"AND ((s.list_visibility_mask & {_LIST_VISIBILITY_MEMORY_REFLECTION}) = 0 OR ? = 1) "
        f"AND ((s.list_visibility_mask & {_LIST_VISIBILITY_SKILL_REFLECTION}) = 0 OR ? = 1) "
        f"AND ((s.list_visibility_mask & {_LIST_VISIBILITY_REFLECTION}) = 0 OR (? = 1 OR ? = 1))"
    )
    visible = (
        "NOT EXISTS (SELECT 1 FROM temporary_session_bindings AS owner_binding "
        "WHERE owner_binding.session_key = s.session_key) AND "
        "(? = 1 OR COALESCE(TRIM(s.platform), '') = '' "
        "OR COALESCE(TRIM(s.platform_conv_id), '') = '') AND "
        f"(({is_subagent} AND ? = 1) OR (NOT {is_subagent} AND "
        f"(NOT {is_background} OR ({background_enabled}))))"
    )
    params: list[Any] = [
        int(include_channels),
        int(include_subagents),
        int(include_cron),
        int(include_memory_reflections),
        int(include_skill_reflections),
        int(include_memory_reflections),
        int(include_skill_reflections),
    ]
    return visible, params


_RECALL_SUBAGENT_MASK = _LIST_VISIBILITY_SUBAGENT_SESSION | _LIST_VISIBILITY_SUBAGENT_RUN_KIND


def _recall_visibility_case(alias: str) -> str:
    """The one definition of ``SessionRecallVisibility`` over ``list_visibility_mask``.

    Reflection kinds hide a Session even when it also carries User or Sub-Agent
    markers. Sub-Agent markers (flag or Run kind) make it a delegated Session.
    Otherwise Sessions without valid Run kinds and Sessions with a User-facing
    Run kind are conversations; the rest (system-only) stay hidden.
    """
    mask = f"{alias}.list_visibility_mask"
    return (
        "CASE"
        f" WHEN ({mask} & {_LIST_VISIBILITY_REFLECTION}) != 0 THEN 'hidden'"
        f" WHEN ({mask} & {_RECALL_SUBAGENT_MASK}) != 0 THEN 'subagent'"
        f" WHEN ({mask} & {_LIST_VISIBILITY_VALID_RUN_KINDS}) = 0"
        f" OR ({mask} & {_LIST_VISIBILITY_USER_FACING}) != 0 THEN 'conversation'"
        " ELSE 'hidden' END"
    )


_RECALL_VISIBILITY_SQL = _recall_visibility_case("s")


def _recall_visibility_sql(*, include_subagents: bool, alias: str = "s") -> str:
    """Return the predicate admitting the Sessions one search may return."""
    from core.sessions._types import recall_visibilities

    admitted = ", ".join(
        f"'{visibility}'" for visibility in recall_visibilities(include_subagents=include_subagents)
    )
    return f"{_recall_visibility_case(alias)} IN ({admitted})"


_SEARCH_RESULT_LIMIT = 1_000
# Candidates one Message search checks before it reports an incomplete result.
_SEARCH_CANDIDATE_LIMIT = 10_000


def _json_object(value: JsonObject, name: str) -> str:
    if not isinstance(value, dict):
        raise ChatSessionError(f"{name} must be an object")
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ChatSessionError(f"{name} must be JSON-serializable") from exc


def _key_list(keys: Sequence[int]) -> str:
    """Encode integer keys for ``IN (SELECT value FROM json_each(?))``."""
    return json.dumps([int(key) for key in keys], separators=(",", ":"))


def _json_list(values: Sequence[str]) -> str:
    """Encode text values for ``IN (SELECT value FROM json_each(?))``."""
    return json.dumps([str(value) for value in values], ensure_ascii=False, separators=(",", ":"))


def _canonical_json_payload(payload: str) -> str:
    """Normalize a stored JSON object before comparing protected configurations."""

    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SessionStoreCorruptError("invalid temporary Session config") from exc
    if not isinstance(value, dict):
        raise SessionStoreCorruptError("invalid temporary Session config")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_from_payload(value: str, name: str) -> JsonObject:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SessionStoreCorruptError(f"invalid {name}") from exc
    if not isinstance(decoded, dict):
        raise SessionStoreCorruptError(f"invalid {name}")
    return decoded


def _json_value_from_payload(value: str, name: str, expected_type: type[Any]) -> Any:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SessionStoreCorruptError(f"invalid {name}") from exc
    if not isinstance(decoded, expected_type):
        raise SessionStoreCorruptError(f"invalid {name}")
    return decoded


def _optional_json(value: Any, name: str) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ChatSessionError(f"{name} is not JSON-serializable") from exc


def _timestamp(value: str, name: str = "timestamp") -> str:
    """Return a canonical stored timestamp, rejecting values without an offset."""
    try:
        return canonical_timestamp(value)
    except ValueError as exc:
        raise ChatSessionError(f"{name} must be an ISO 8601 timestamp with an offset") from exc


def _optional_timestamp(value: Any, name: str = "timestamp") -> str | None:
    return None if value is None else _timestamp(value, name)


def _scope(address: SessionAddress) -> tuple[str, str, str]:
    return (address.project_id or "", address.agent_id, address.session_id)


def _address(row: sqlite3.Row) -> SessionAddress:
    from core.sessions._types import SessionAddress

    return SessionAddress(
        project_id=row["project_id"] or None,
        agent_id=row["agent_id"],
        session_id=row["session_id"],
    )


def _allocate_address(connection: sqlite3.Connection, scope: SessionAddress) -> SessionAddress:
    from core.sessions._types import SessionAddress

    def available(candidate: str) -> bool:
        # One probe per state: each address index is partial on its state.
        address = _scope(SessionAddress(scope.project_id, scope.agent_id, candidate))
        return (
            connection.execute(
                "SELECT 1 FROM sessions WHERE project_id = ? AND agent_id = ? "
                "AND session_id = ? AND state = 'live' "
                "UNION ALL SELECT 1 FROM sessions WHERE project_id = ? AND agent_id = ? "
                "AND session_id = ? AND state = 'archived' LIMIT 1",
                (*address, *address),
            ).fetchone()
            is None
        )

    return SessionAddress(scope.project_id, scope.agent_id, new_id("ses", claim=available))


_OWNER_MANAGED_ERROR = "This Session is managed by an Extension. Use that Extension to resume it."


def _reject_owner_managed_mutation(connection: sqlite3.Connection, state: sqlite3.Row) -> None:
    binding = connection.execute(
        "SELECT 1 FROM temporary_session_bindings WHERE session_key = ?",
        (state["session_key"],),
    ).fetchone()
    if binding is not None:
        raise ChatSessionError(_OWNER_MANAGED_ERROR)


def _reject_owner_managed_scope_mutation(
    connection: sqlite3.Connection, where: str, params: tuple[Any, ...]
) -> None:
    binding = connection.execute(
        "SELECT 1 FROM temporary_session_bindings AS b "
        "JOIN sessions AS s ON s.session_key = b.session_key WHERE " + where,
        params,
    ).fetchone()
    if binding is not None:
        raise ChatSessionError(_OWNER_MANAGED_ERROR)


_LIVE_ADDRESS = "s.project_id = ? AND s.agent_id = ? AND s.session_id = ? AND s.state = 'live'"


def _require_live(connection: sqlite3.Connection, address: SessionAddress) -> sqlite3.Row:
    row = _find_live(connection, address)
    if row is None:
        raise SessionNotFoundError(f"session does not exist: {address.session_id}")
    return row


def _find_live(connection: sqlite3.Connection, address: SessionAddress) -> sqlite3.Row | None:
    row = connection.execute(
        f"SELECT {_SESSION_STATE_COLUMNS} FROM sessions AS s WHERE {_LIVE_ADDRESS}",
        _scope(address),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def _state_by_key(connection: sqlite3.Connection, session_key: int) -> sqlite3.Row:
    row = connection.execute(
        f"SELECT {_SESSION_STATE_COLUMNS} FROM sessions AS s WHERE s.session_key = ?",
        (session_key,),
    ).fetchone()
    if row is None:
        raise SessionStoreCorruptError(f"Session row disappeared: {session_key}")
    return cast(sqlite3.Row, row)


def _metadata_row(connection: sqlite3.Connection, session_key: int) -> sqlite3.Row:
    """Read one Session's metadata facade inputs, derived values included."""
    row = connection.execute(
        f"SELECT {_SESSION_STATE_COLUMNS}, {_DERIVED_METADATA_COLUMNS} FROM sessions AS s "
        f"{_DERIVED_METADATA_JOIN} WHERE s.session_key = ?",
        (session_key,),
    ).fetchone()
    if row is None:
        raise SessionStoreCorruptError(f"Session row disappeared: {session_key}")
    return cast(sqlite3.Row, row)


def _live_metadata_row(connection: sqlite3.Connection, address: SessionAddress) -> sqlite3.Row:
    row = connection.execute(
        f"SELECT {_SESSION_STATE_COLUMNS}, {_DERIVED_METADATA_COLUMNS} FROM sessions AS s "
        f"{_DERIVED_METADATA_JOIN} WHERE {_LIVE_ADDRESS}",
        _scope(address),
    ).fetchone()
    if row is None:
        raise SessionNotFoundError(f"session does not exist: {address.session_id}")
    return cast(sqlite3.Row, row)


def _find_live_metadata_row(
    connection: sqlite3.Connection, address: SessionAddress
) -> sqlite3.Row | None:
    row = connection.execute(
        f"SELECT {_SESSION_STATE_COLUMNS}, {_DERIVED_METADATA_COLUMNS} FROM sessions AS s "
        f"{_DERIVED_METADATA_JOIN} WHERE {_LIVE_ADDRESS}",
        _scope(address),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def _metadata_storage_of(state: sqlite3.Row) -> _MetadataStorage:
    """Return the metadata storage one Session row currently holds."""
    return _MetadataStorage(tuple(state[column] for column in _METADATA_WRITE_COLUMNS))


def _write_metadata_storage(
    connection: sqlite3.Connection, session_key: int, storage: _MetadataStorage
) -> None:
    connection.execute(
        "UPDATE sessions SET "
        + ", ".join(f"{column} = ?" for column in _METADATA_WRITE_COLUMNS)
        + ", state_revision = state_revision + 1 WHERE session_key = ?",
        (*storage.columns, session_key),
    )


def _refresh_visibility(connection: sqlite3.Connection, session_key: int) -> None:
    """Recompute the list visibility mask after a relation it reads changed."""
    row = _metadata_row(connection, session_key)
    mask = _session_list_visibility_mask(_session_metadata_from_state(row))
    if mask != int(row["list_visibility_mask"]):
        connection.execute(
            "UPDATE sessions SET list_visibility_mask = ? WHERE session_key = ?",
            (mask, session_key),
        )


def _touch_state(connection: sqlite3.Connection, session_key: int) -> None:
    connection.execute(
        "UPDATE sessions SET state_revision = state_revision + 1 WHERE session_key = ?",
        (session_key,),
    )
