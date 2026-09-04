"""Bounded, lossless access to original records in the current compacted Session."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

from core.sessions import (
    ChatSession,
    ChatSessionManager,
    SessionAddress,
    SessionHistoryCheckpoint,
    SessionHistoryRecord,
    SessionHistorySnapshot,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
    offload_tool_handler,
    result_count_fact_builder,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.history")

HISTORY_TOOL_NAME = "history"
HISTORY_TOOL_DESCRIPTION = (
    "Recover original records from this Session after Compaction. Use overview to inspect "
    "available checkpoint sections, search to find matching records, read to retrieve "
    "canonical records chronologically, or around to retrieve complete records near a known "
    "message id. This Tool is available only after the Session has a Compaction checkpoint."
)
HISTORY_ACTIONS = ("overview", "search", "read", "around")
HISTORY_SUPPORTED_ROLES = (
    "system",
    "user",
    "assistant",
    "tool",
    "note",
    "error",
    "run_summary",
    "agent_takeover",
)
HISTORY_DEFAULT_ROLES = ("user", "assistant", "error")
HISTORY_MATCH_MODES = ("all_terms", "phrase", "any_term")
HISTORY_DIRECTIONS = ("start", "end")
HISTORY_RESULT_MAX_BYTES = 50 * 1024
HISTORY_SEARCH_EXCERPT_CHARS = 320
HISTORY_OVERVIEW_SUMMARY_CHARS = 320
HISTORY_CURSOR_VERSION = 2
HISTORY_SCAN_BATCH_SIZE = 128

_HISTORY_CHECKPOINT_PARAMETER: JsonObject = {
    "type": "integer",
    "minimum": 1,
    "description": (
        "For search, read, and around. 1-based Compaction checkpoint section to restrict "
        "results to; omit to include all earlier history."
    ),
}
_HISTORY_ROLES_PARAMETER: JsonObject = {
    "type": "array",
    "items": {"type": "string", "enum": list(HISTORY_SUPPORTED_ROLES)},
    "description": (
        "Message roles for search, read, and around. Omit to include user, assistant, and error."
    ),
}
_HISTORY_LIMIT_PARAMETER: JsonObject = {
    "type": "integer",
    "minimum": 1,
    "maximum": 100,
    "description": (
        "Maximum items in this page. For overview and search the default is 10; "
        "for read the default is 20."
    ),
}
_HISTORY_CURSOR_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Continuation returned by the same action. When set, send only action and cursor."
    ),
}

HISTORY_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(HISTORY_ACTIONS),
            "description": "Operation to perform.",
        },
        "query": {
            "type": "string",
            "minLength": 1,
            "description": "Text to find. Required for search unless cursor is set.",
        },
        "message_id": {
            "type": "string",
            "minLength": 1,
            "description": "Anchor Message id. Required for around unless cursor is set.",
        },
        "checkpoint": _HISTORY_CHECKPOINT_PARAMETER,
        "roles": _HISTORY_ROLES_PARAMETER,
        "match": {
            "type": "string",
            "enum": list(HISTORY_MATCH_MODES),
            "description": (
                "Search matching mode: every term, the complete phrase, or any term. "
                "Omit for every term."
            ),
        },
        "direction": {
            "type": "string",
            "enum": list(HISTORY_DIRECTIONS),
            "description": "Read from the oldest or newest records. Omit for oldest first.",
        },
        "limit": _HISTORY_LIMIT_PARAMETER,
        "before": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "Records before the around anchor. Omit for 2.",
        },
        "after": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "Records after the around anchor. Omit for 2.",
        },
        "cursor": _HISTORY_CURSOR_PARAMETER,
    },
    "required": ["action"],
}

_ACTION_FIELDS = {
    "overview": frozenset({"action", "limit", "cursor"}),
    "search": frozenset({"action", "query", "checkpoint", "roles", "match", "limit", "cursor"}),
    "read": frozenset({"action", "checkpoint", "roles", "direction", "limit", "cursor"}),
    "around": frozenset(
        {"action", "message_id", "checkpoint", "roles", "before", "after", "cursor"}
    ),
}
_CURSOR_KEYS = frozenset(
    {
        "v",
        "session_id",
        "generation_id",
        "action",
        "snapshot_seq",
        "snapshot_id",
        "snapshot_ordinal",
        "checkpoint",
        "checkpoint_seq",
        "checkpoint_id",
        "roles",
        "direction",
        "query",
        "match",
        "limit",
        "before",
        "after",
        "message_id",
        "next_seq",
        "within_offset",
    }
)


class _HistoryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Snapshot:
    generation_id: str
    checkpoints: tuple[SessionHistoryCheckpoint, ...]

    @property
    def latest(self) -> SessionHistoryCheckpoint:
        return self.checkpoints[-1]


@dataclass(frozen=True)
class _SourceItem:
    sequence: int
    data: JsonObject


@dataclass(frozen=True)
class _Request:
    action: str
    checkpoint: int | None
    roles: tuple[str, ...]
    direction: str
    query: str | None
    match: str
    limit: int
    before: int
    after: int
    message_id: str | None
    next_sequence: int | None = None
    within_offset: int = 0


def register_history_tool(registry: ToolRegistry, sessions: ChatSessionManager) -> None:
    """Register the Session-scoped History tool bound to canonical Session storage."""
    registry.register(
        name=HISTORY_TOOL_NAME,
        description=HISTORY_TOOL_DESCRIPTION,
        parameters=HISTORY_TOOL_PARAMETERS,
        handler=offload_tool_handler(make_history_handler(sessions)),
        family="sessions",
        activation="session_grant",
        result_schema={
            "type": "object",
            "required": ["items", "has_more", "formatted_bytes"],
        },
        session_scoped=True,
        parallel_safe=True,
        open_input_schema=True,
        display=ToolDisplay(
            parts_builder=_history_display_parts,
            fact_builder=result_count_fact_builder("items", at_least_field="has_more"),
            hidden_argument_keys=("query", "message_id", "cursor"),
        ),
    )


def make_history_handler(sessions: ChatSessionManager):
    """Build the runtime-bound History handler."""

    def history_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        started = time.perf_counter()
        raw_action = arguments.get("action")
        if not isinstance(raw_action, str) or raw_action not in HISTORY_ACTIONS:
            return tool_failure(
                "invalid_arguments",
                f"action must be one of: {', '.join(HISTORY_ACTIONS)}",
            )
        action = raw_action
        try:
            _validate_history_action_arguments(arguments, action)
        except _HistoryError as error:
            return tool_failure(error.code, str(error))
        checkpoint: int | None = None
        direction = ""
        try:
            try:
                session = sessions.get(
                    SessionAddress(
                        project_id=context.project_id,
                        agent_id=context.agent_id,
                        session_id=context.session_id,
                    )
                )
            except Exception as error:
                raise _HistoryError(
                    "history_session_error", "Unable to read canonical Session history."
                ) from error

            cursor_payload = _cursor_payload(arguments, context.session_id)
            snapshot_sequence = (
                cursor_payload.get("snapshot_seq") if cursor_payload is not None else None
            )
            if snapshot_sequence is not None and (
                isinstance(snapshot_sequence, bool) or not isinstance(snapshot_sequence, int)
            ):
                raise _HistoryError("invalid_cursor", "History cursor is invalid.")
            resolved = session.resolve_history_snapshot(snapshot_sequence=snapshot_sequence)
            if resolved is None:
                if cursor_payload is not None:
                    raise _HistoryError("invalid_cursor", "History cursor is invalid.")
                raise _HistoryError(
                    "history_unavailable",
                    "History is unavailable until this Session has a successful Compaction.",
                )
            snapshot = _Snapshot(
                generation_id=resolved.generation_id,
                checkpoints=resolved.checkpoints,
            )
            request = (
                _request_from_cursor(cursor_payload, snapshot, context.session_id)
                if cursor_payload is not None
                else _request_from_arguments(arguments, snapshot)
            )
            action = request.action
            checkpoint = request.checkpoint
            direction = request.direction
            source = _source_items(session, snapshot, request)
            _validate_cursor_position(request, source)
            data = _render_page(snapshot, request, source, context.session_id)
            result = tool_success(data)
            _log_history(
                action=action,
                checkpoint=checkpoint,
                direction=direction,
                count=len(data["items"]),
                formatted_bytes=int(data["formatted_bytes"]),
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_code=None,
            )
            return result
        except _HistoryError as error:
            _log_history(
                action=action,
                checkpoint=checkpoint,
                direction=direction,
                count=0,
                formatted_bytes=0,
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_code=error.code,
            )
            return tool_failure(error.code, str(error))
        except Exception:
            _LOGGER.error("History execution failed unexpectedly", exc_info=True)
            return tool_failure(
                "history_session_error", "Unable to read canonical Session history."
            )

    return history_handler


def _sanitize_record(data: JsonObject) -> JsonObject | None:
    role = data.get("role")
    if role == "compaction_checkpoint":
        return None
    if role == "tool" and data.get("name") == HISTORY_TOOL_NAME:
        return None
    if role != "assistant":
        return dict(data)

    tool_calls = data.get("tool_calls")
    if isinstance(tool_calls, list):
        remaining = [
            dict(tool_call)
            for tool_call in tool_calls
            if isinstance(tool_call, dict) and tool_call.get("name") != HISTORY_TOOL_NAME
        ]
        data = dict(data)
        if remaining:
            data["tool_calls"] = remaining
        else:
            data.pop("tool_calls", None)
    if not any(data.get(key) for key in ("content", "reasoning", "reasoning_meta", "tool_calls")):
        return None
    return dict(data)


def _cursor_payload(arguments: JsonObject, session_id: str) -> JsonObject | None:
    cursor = arguments.get("cursor")
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor.strip():
        raise _HistoryError("invalid_arguments", "cursor must be a non-blank string")
    if set(arguments) != {"action", "cursor"}:
        raise _HistoryError(
            "invalid_arguments", "A cursor continuation accepts only action and cursor."
        )
    payload = _decode_cursor(cursor)
    if payload.get("session_id") != session_id:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if payload.get("action") != arguments.get("action"):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    return payload


def _request_from_arguments(arguments: JsonObject, snapshot: _Snapshot) -> _Request:
    action = arguments.get("action")
    if not isinstance(action, str) or action not in HISTORY_ACTIONS:
        raise _HistoryError("invalid_arguments", "action must be a supported History action")
    unsupported = sorted(set(arguments) - _ACTION_FIELDS[action])
    if unsupported:
        raise _HistoryError(
            "invalid_arguments",
            f"Unsupported arguments for {action}: {', '.join(unsupported)}",
        )
    checkpoint = _optional_checkpoint(arguments.get("checkpoint"), snapshot)
    roles = _roles(arguments.get("roles"))
    query: str | None = None
    match = "all_terms"
    direction = "start"
    limit = 10 if action in {"overview", "search"} else 20
    before = 2
    after = 2
    message_id: str | None = None

    if action == "search":
        raw_query = arguments.get("query")
        if not isinstance(raw_query, str) or not raw_query.strip():
            raise _HistoryError("invalid_arguments", "search requires a non-blank query")
        query = raw_query.strip()
        match = _enum(arguments.get("match", match), HISTORY_MATCH_MODES, "match")
    if action == "read":
        direction = _enum(arguments.get("direction", direction), HISTORY_DIRECTIONS, "direction")
    if action in {"overview", "search", "read"}:
        limit = _bounded_int(arguments.get("limit", limit), "limit", 1, 100)
    if action == "around":
        raw_message_id = arguments.get("message_id")
        if not isinstance(raw_message_id, str) or not raw_message_id.strip():
            raise _HistoryError("invalid_arguments", "around requires a non-blank message_id")
        message_id = raw_message_id.strip()
        before = _bounded_int(arguments.get("before", before), "before", 0, 100)
        after = _bounded_int(arguments.get("after", after), "after", 0, 100)

    return _Request(
        action=action,
        checkpoint=checkpoint,
        roles=roles,
        direction=direction,
        query=query,
        match=match,
        limit=limit,
        before=before,
        after=after,
        message_id=message_id,
    )


def _validate_history_action_arguments(arguments: JsonObject, action: str) -> None:
    unsupported = sorted(set(arguments) - _ACTION_FIELDS[action])
    if unsupported:
        raise _HistoryError(
            "invalid_arguments",
            f"Unsupported arguments for {action}: {', '.join(unsupported)}",
        )
    if "cursor" in arguments:
        cursor = arguments.get("cursor")
        if not isinstance(cursor, str) or not cursor.strip():
            raise _HistoryError("invalid_arguments", "cursor must be a non-blank string")
        if set(arguments) != {"action", "cursor"}:
            raise _HistoryError(
                "invalid_arguments",
                "A cursor continuation accepts only action and cursor.",
            )
        return
    if action == "search":
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise _HistoryError("invalid_arguments", "search requires a non-blank query")
    if action == "around":
        message_id = arguments.get("message_id")
        if not isinstance(message_id, str) or not message_id.strip():
            raise _HistoryError("invalid_arguments", "around requires a non-blank message_id")


def _request_from_cursor(
    payload: JsonObject,
    snapshot: _Snapshot,
    session_id: str,
) -> _Request:
    if set(payload) != _CURSOR_KEYS or payload.get("v") != HISTORY_CURSOR_VERSION:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if payload.get("session_id") != session_id:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if payload.get("generation_id") != snapshot.generation_id:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if payload.get("snapshot_seq") != snapshot.latest.sequence:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if payload.get("snapshot_id") != snapshot.latest.message_id:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if payload.get("snapshot_ordinal") != snapshot.latest.ordinal:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    action = payload.get("action")
    if not isinstance(action, str) or action not in HISTORY_ACTIONS:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    roles = _cursor_roles(payload.get("roles"))
    checkpoint = payload.get("checkpoint")
    if checkpoint is not None and (
        isinstance(checkpoint, bool) or not isinstance(checkpoint, int) or checkpoint < 1
    ):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    selected = _checkpoint(snapshot, checkpoint, cursor=True)
    if payload.get("checkpoint_seq") != (selected.sequence if selected is not None else None):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if payload.get("checkpoint_id") != (selected.message_id if selected is not None else None):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")

    direction = payload.get("direction")
    match = payload.get("match")
    query = payload.get("query")
    message_id = payload.get("message_id")
    if direction not in HISTORY_DIRECTIONS or match not in HISTORY_MATCH_MODES:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if query is not None and not isinstance(query, str):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if message_id is not None and not isinstance(message_id, str):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    limit = _cursor_int(payload.get("limit"), minimum=1, maximum=100)
    before = _cursor_int(payload.get("before"), minimum=0, maximum=100)
    after = _cursor_int(payload.get("after"), minimum=0, maximum=100)
    next_sequence = payload.get("next_seq")
    if next_sequence is not None:
        next_sequence = _cursor_int(next_sequence, minimum=0)
    within_offset = _cursor_int(payload.get("within_offset"), minimum=0)
    if next_sequence is None:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if action == "overview" and (
        checkpoint is not None
        or roles != HISTORY_DEFAULT_ROLES
        or direction != "start"
        or query is not None
        or match != "all_terms"
        or before != 2
        or after != 2
        or message_id is not None
    ):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if action == "search" and (
        not isinstance(query, str)
        or not query.strip()
        or direction != "start"
        or before != 2
        or after != 2
        or message_id is not None
    ):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if action == "read" and (
        query is not None
        or match != "all_terms"
        or before != 2
        or after != 2
        or message_id is not None
    ):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if action == "around" and (
        query is not None
        or match != "all_terms"
        or direction != "start"
        or limit != 20
        or not isinstance(message_id, str)
        or not message_id.strip()
    ):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    return _Request(
        action=action,
        checkpoint=checkpoint,
        roles=roles,
        direction=direction,
        query=query,
        match=match,
        limit=limit,
        before=before,
        after=after,
        message_id=message_id,
        next_sequence=next_sequence,
        within_offset=within_offset,
    )


def _source_items(
    session: ChatSession,
    snapshot: _Snapshot,
    request: _Request,
) -> list[_SourceItem]:
    amount = request.limit if request.action != "around" else request.before + request.after + 1
    if request.action == "overview":
        checkpoints = list(snapshot.checkpoints)
        if request.next_sequence is not None:
            start = next(
                (
                    index
                    for index, checkpoint in enumerate(checkpoints)
                    if checkpoint.sequence == request.next_sequence
                ),
                None,
            )
            if start is None:
                return []
            checkpoints = checkpoints[start:]
        selected = checkpoints[: amount + 1]
        sections = [
            (
                -1
                if checkpoint.ordinal == 1
                else snapshot.checkpoints[checkpoint.ordinal - 2].sequence,
                checkpoint.sequence,
            )
            for checkpoint in selected
        ]
        stats = session.history_section_stats(
            SessionHistorySnapshot(snapshot.generation_id, snapshot.checkpoints),
            sections=sections,
            excluded_tool_name=HISTORY_TOOL_NAME,
        )
        if stats is None:
            raise _HistoryError("invalid_cursor", "History cursor is invalid.")
        return [
            _SourceItem(
                checkpoint.sequence,
                {
                    "checkpoint": checkpoint.ordinal,
                    "checkpoint_id": checkpoint.message_id,
                    "timestamp": checkpoint.timestamp,
                    "start_timestamp": stats[checkpoint.sequence].start_timestamp,
                    "end_timestamp": stats[checkpoint.sequence].end_timestamp,
                    "eligible_count": stats[checkpoint.sequence].eligible_count,
                    "summary": _bounded_preview(checkpoint.summary, HISTORY_OVERVIEW_SUMMARY_CHARS),
                },
            )
            for checkpoint in selected
        ]

    lower_sequence, upper_sequence = _history_bounds(snapshot, request.checkpoint)
    resolved_snapshot = SessionHistorySnapshot(snapshot.generation_id, snapshot.checkpoints)
    if request.action == "around":
        assert request.message_id is not None
        around = session.load_history_around(
            resolved_snapshot,
            lower_sequence=lower_sequence,
            upper_sequence=upper_sequence,
            roles=request.roles,
            message_id=request.message_id,
            before=request.before,
            after=request.after,
            excluded_tool_name=HISTORY_TOOL_NAME,
        )
        if around is None:
            raise _HistoryError("invalid_cursor", "History cursor is invalid.")
        exists, records = around
        if not exists:
            raise _HistoryError("message_not_found", "History message was not found.")
        if not records:
            raise _HistoryError(
                "anchor_outside_scope", "History message is outside the selected scope."
            )
        around_source = [_record_source_item(snapshot, record) for record in records]
        if request.next_sequence is not None:
            start = next(
                (
                    index
                    for index, item in enumerate(around_source)
                    if item.sequence == request.next_sequence
                ),
                None,
            )
            if start is None:
                return []
            around_source = around_source[start:]
        return around_source

    if request.action == "read":
        read_records = session.load_history_records(
            resolved_snapshot,
            lower_sequence=lower_sequence,
            upper_sequence=upper_sequence,
            roles=request.roles,
            direction=request.direction,
            cursor_sequence=request.next_sequence,
            limit=amount + 1,
            excluded_tool_name=HISTORY_TOOL_NAME,
        )
        if read_records is None:
            raise _HistoryError("invalid_cursor", "History cursor is invalid.")
        return [_record_source_item(snapshot, record) for record in read_records]

    assert request.action == "search"
    source: list[_SourceItem] = []
    scan_sequence = request.next_sequence
    while len(source) < amount + 1:
        search_records = session.load_history_records(
            resolved_snapshot,
            lower_sequence=lower_sequence,
            upper_sequence=upper_sequence,
            roles=request.roles,
            direction="start",
            cursor_sequence=scan_sequence,
            limit=HISTORY_SCAN_BATCH_SIZE,
            excluded_tool_name=HISTORY_TOOL_NAME,
        )
        if search_records is None:
            raise _HistoryError("invalid_cursor", "History cursor is invalid.")
        if not search_records:
            break
        for record in search_records:
            data = _sanitized_record(record)
            search_text = _record_search_text(data)
            if _matches(search_text, request.query or "", request.match):
                source.append(
                    _SourceItem(
                        record.sequence,
                        {
                            "message_id": str(data["id"]),
                            "role": str(data["role"]),
                            "timestamp": str(data["timestamp"]),
                            "checkpoint": _record_checkpoint(snapshot, record.sequence),
                            "excerpt": _search_excerpt(
                                search_text, request.query or "", request.match
                            ),
                        },
                    )
                )
                if len(source) >= amount + 1:
                    break
        if len(source) >= amount + 1 or len(search_records) < HISTORY_SCAN_BATCH_SIZE:
            break
        scan_sequence = search_records[-1].sequence + 1
    return source


def _history_bounds(snapshot: _Snapshot, ordinal: int | None) -> tuple[int, int]:
    selected = _checkpoint(snapshot, ordinal)
    if selected is None:
        return -1, snapshot.latest.sequence
    lower = -1 if selected.ordinal == 1 else snapshot.checkpoints[selected.ordinal - 2].sequence
    return lower, selected.sequence


def _record_checkpoint(snapshot: _Snapshot, sequence: int) -> int:
    return 1 + sum(checkpoint.sequence < sequence for checkpoint in snapshot.checkpoints)


def _sanitized_record(record: SessionHistoryRecord) -> JsonObject:
    sanitized = _sanitize_record(record.message.to_dict())
    if sanitized is None:
        raise _HistoryError("history_session_error", "History record is malformed.")
    return sanitized


def _record_source_item(snapshot: _Snapshot, record: SessionHistoryRecord) -> _SourceItem:
    return _SourceItem(
        record.sequence,
        {
            "checkpoint": _record_checkpoint(snapshot, record.sequence),
            "message": _sanitized_record(record),
        },
    )


def _render_page(
    snapshot: _Snapshot,
    request: _Request,
    source: list[_SourceItem],
    session_id: str,
) -> JsonObject:
    amount = request.limit if request.action != "around" else request.before + request.after + 1
    target_end = min(len(source), amount)
    items: list[JsonObject] = []
    index = 0
    within_offset = request.within_offset
    while index < target_end:
        item = source[index]
        if within_offset == 0:
            next_index = index + 1
            has_more = next_index < len(source)
            candidate = _page_data(
                snapshot,
                request,
                [*items, item.data],
                session_id,
                next_sequence=source[next_index].sequence if has_more else None,
                within_offset=0,
                has_more=has_more,
            )
            if _serialized_result_bytes(candidate) <= HISTORY_RESULT_MAX_BYTES:
                items.append(item.data)
                index = next_index
                continue
            if items:
                return _page_data(
                    snapshot,
                    request,
                    items,
                    session_id,
                    next_sequence=item.sequence,
                    within_offset=0,
                    has_more=True,
                )
            if request.action not in {"read", "around"}:
                raise _HistoryError(
                    "history_session_error", "History item exceeds the result safety limit."
                )

        return _segmented_record_page(
            snapshot,
            request,
            source,
            index,
            within_offset,
            session_id,
        )

    return _page_data(
        snapshot,
        request,
        items,
        session_id,
        next_sequence=source[index].sequence if index < len(source) else None,
        within_offset=0,
        has_more=index < len(source),
    )


def _segmented_record_page(
    snapshot: _Snapshot,
    request: _Request,
    source: list[_SourceItem],
    index: int,
    offset: int,
    session_id: str,
) -> JsonObject:
    item = source[index]
    message = item.data.get("message")
    if not isinstance(message, dict):
        raise _HistoryError("history_session_error", "History record is malformed.")
    record_json = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    if offset >= len(record_json):
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")

    low = offset + 1
    high = len(record_json)
    best: JsonObject | None = None
    while low <= high:
        end = (low + high) // 2
        incomplete = end < len(record_json)
        next_index = index if incomplete else index + 1
        has_more = incomplete or next_index < len(source)
        next_sequence = (
            item.sequence
            if incomplete
            else (source[next_index].sequence if next_index < len(source) else None)
        )
        segment = {
            "checkpoint": item.data["checkpoint"],
            "message_id": message.get("id"),
            "role": message.get("role"),
            "timestamp": message.get("timestamp"),
            "segment": {
                "start": offset,
                "end": end,
                "complete": not incomplete,
                "record_json": record_json[offset:end],
            },
        }
        candidate = _page_data(
            snapshot,
            request,
            [segment],
            session_id,
            next_sequence=next_sequence,
            within_offset=end if incomplete else 0,
            has_more=has_more,
        )
        if _serialized_result_bytes(candidate) <= HISTORY_RESULT_MAX_BYTES:
            best = candidate
            low = end + 1
        else:
            high = end - 1
    if best is None:
        raise _HistoryError(
            "history_session_error", "History metadata exceeds the result safety limit."
        )
    return best


def _page_data(
    snapshot: _Snapshot,
    request: _Request,
    items: list[JsonObject],
    session_id: str,
    *,
    next_sequence: int | None,
    within_offset: int,
    has_more: bool,
) -> JsonObject:
    selected = _checkpoint(snapshot, request.checkpoint)
    data: JsonObject = {
        "snapshot": {
            "checkpoint": snapshot.latest.ordinal,
            "checkpoint_id": snapshot.latest.message_id,
            "timestamp": snapshot.latest.timestamp,
        },
        "scope": {
            "checkpoint": request.checkpoint,
            "checkpoint_id": selected.message_id if selected is not None else None,
        },
        "items": items,
        "has_more": has_more,
        "formatted_bytes": 0,
    }
    if has_more:
        data["next_cursor"] = _encode_cursor(
            _cursor_for(
                snapshot,
                request,
                session_id,
                next_sequence=next_sequence,
                within_offset=within_offset,
            )
        )
    return _with_formatted_bytes(data)


def _cursor_for(
    snapshot: _Snapshot,
    request: _Request,
    session_id: str,
    *,
    next_sequence: int | None,
    within_offset: int,
) -> JsonObject:
    selected = _checkpoint(snapshot, request.checkpoint)
    return {
        "v": HISTORY_CURSOR_VERSION,
        "session_id": session_id,
        "generation_id": snapshot.generation_id,
        "action": request.action,
        "snapshot_seq": snapshot.latest.sequence,
        "snapshot_id": snapshot.latest.message_id,
        "snapshot_ordinal": snapshot.latest.ordinal,
        "checkpoint": request.checkpoint,
        "checkpoint_seq": selected.sequence if selected is not None else None,
        "checkpoint_id": selected.message_id if selected is not None else None,
        "roles": list(request.roles),
        "direction": request.direction,
        "query": request.query,
        "match": request.match,
        "limit": request.limit,
        "before": request.before,
        "after": request.after,
        "message_id": request.message_id,
        "next_seq": next_sequence,
        "within_offset": within_offset,
    }


def _validate_cursor_position(request: _Request, source: list[_SourceItem]) -> None:
    if request.next_sequence is None:
        if request.within_offset:
            raise _HistoryError("invalid_cursor", "History cursor is invalid.")
        return
    if not source or source[0].sequence != request.next_sequence:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if request.within_offset and request.action not in {"read", "around"}:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")


def _checkpoint(
    snapshot: _Snapshot,
    ordinal: int | None,
    *,
    cursor: bool = False,
) -> SessionHistoryCheckpoint | None:
    if ordinal is None:
        return None
    selected = next(
        (checkpoint for checkpoint in snapshot.checkpoints if checkpoint.ordinal == ordinal),
        None,
    )
    if selected is not None:
        return selected
    if cursor:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    raise _HistoryError(
        "checkpoint_not_found",
        f"Checkpoint {ordinal} was not found; available checkpoints: 1-{snapshot.latest.ordinal}.",
    )


def _optional_checkpoint(value: Any, snapshot: _Snapshot) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _HistoryError("invalid_arguments", "checkpoint must be a positive integer")
    _checkpoint(snapshot, value)
    return value


def _roles(value: Any) -> tuple[str, ...]:
    if value is None:
        return HISTORY_DEFAULT_ROLES
    if not isinstance(value, list) or not all(isinstance(role, str) for role in value):
        raise _HistoryError("invalid_arguments", "roles must be an array of role names")
    if len(set(value)) != len(value) or any(role not in HISTORY_SUPPORTED_ROLES for role in value):
        raise _HistoryError("invalid_arguments", "roles contains an unsupported or duplicate role")
    return tuple(value)


def _cursor_roles(value: Any) -> tuple[str, ...]:
    try:
        return _roles(value)
    except _HistoryError as error:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.") from error


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _HistoryError("invalid_arguments", f"{field} must be between {minimum} and {maximum}")
    return value


def _cursor_int(value: Any, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    if maximum is not None and value > maximum:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.")
    return value


def _enum(value: Any, values: tuple[str, ...], field: str) -> str:
    if not isinstance(value, str) or value not in values:
        raise _HistoryError("invalid_arguments", f"{field} is invalid")
    return value


def _record_search_text(data: JsonObject) -> str:
    parts: list[str] = []
    content = data.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            for key in ("text", "filename", "media_type", "path"):
                if isinstance(block.get(key), str):
                    parts.append(str(block[key]))
    for key in ("reasoning", "name", "error_kind", "status"):
        if isinstance(data.get(key), str):
            parts.append(str(data[key]))
    tool_calls = data.get("tool_calls")
    if isinstance(tool_calls, list):
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            if isinstance(tool_call.get("name"), str):
                parts.append(str(tool_call["name"]))
            arguments = tool_call.get("arguments")
            if isinstance(arguments, dict):
                parts.append(
                    json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                )
    return "\n".join(parts)


def _matches(text: str, query: str, match: str) -> bool:
    haystack = _compact_text(text).casefold()
    if not haystack:
        return False
    compact_query = _compact_text(query).casefold()
    if match == "phrase":
        return compact_query in haystack
    terms = compact_query.split()
    if match == "any_term":
        return any(term in haystack for term in terms)
    return all(term in haystack for term in terms)


def _search_excerpt(text: str, query: str, match: str) -> str:
    compact = _compact_text(text)
    if not compact:
        return ""
    folded = compact.casefold()
    compact_query = _compact_text(query).casefold()
    if match == "phrase":
        index = folded.find(compact_query)
    else:
        indexes = [found for term in compact_query.split() if (found := folded.find(term)) >= 0]
        index = min(indexes) if indexes else 0
    start = max(index - HISTORY_SEARCH_EXCERPT_CHARS // 3, 0)
    leading = start > 0
    body_limit = HISTORY_SEARCH_EXCERPT_CHARS - (3 if leading else 0)
    end = min(start + body_limit, len(compact))
    trailing = end < len(compact)
    if trailing:
        body_limit -= 3
        start = max(index - body_limit // 3, 0)
        leading = start > 0
        if not leading:
            body_limit += 3
        end = min(start + body_limit, len(compact))
        trailing = end < len(compact)
    excerpt = compact[start:end]
    if leading:
        excerpt = f"...{excerpt}"
    if trailing:
        excerpt = f"{excerpt}..."
    return excerpt


def _compact_text(text: str) -> str:
    return " ".join(text.split())


def _bounded_preview(text: str, limit: int) -> str:
    compact = _compact_text(text)
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3]}..."


def _encode_cursor(payload: JsonObject) -> str:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(body).digest()
    return base64.urlsafe_b64encode(body + digest).decode("ascii").rstrip("=")


def _decode_cursor(token: str) -> JsonObject:
    try:
        padded = token + "=" * (-len(token) % 4)
        encoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        if len(encoded) <= hashlib.sha256().digest_size:
            raise ValueError
        body = encoded[: -hashlib.sha256().digest_size]
        digest = encoded[-hashlib.sha256().digest_size :]
        if not hmac.compare_digest(hashlib.sha256(body).digest(), digest):
            raise ValueError
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (binascii.Error, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise _HistoryError("invalid_cursor", "History cursor is invalid.") from error


def _with_formatted_bytes(data: JsonObject) -> JsonObject:
    result = dict(data)
    previous = -1
    for _ in range(4):
        size = len(
            json.dumps(
                tool_success(result),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        result["formatted_bytes"] = size
        if size == previous:
            break
        previous = size
    return result


def _serialized_result_bytes(data: JsonObject) -> int:
    finalized = _with_formatted_bytes(data)
    return len(
        json.dumps(
            tool_success(finalized),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _history_display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    action = arguments.get("action")
    if not isinstance(action, str) or action not in HISTORY_ACTIONS:
        return ()
    parts = [ToolDisplayPart(action, truncate="never", tooltip="none")]
    checkpoint = arguments.get("checkpoint")
    if isinstance(checkpoint, int) and not isinstance(checkpoint, bool):
        detail = f"checkpoint {checkpoint}"
    elif action != "overview":
        detail = "all earlier history"
    else:
        detail = ""
    direction = arguments.get("direction")
    if isinstance(direction, str) and direction:
        detail = " · ".join(value for value in (detail, direction) if value)
    if detail:
        parts.append(ToolDisplayPart(detail))
    return tuple(parts)


def _log_history(
    *,
    action: str,
    checkpoint: int | None,
    direction: str,
    count: int,
    formatted_bytes: int,
    duration_ms: int,
    error_code: str | None,
) -> None:
    _LOGGER.info(
        "History action=%s checkpoint=%s direction=%s count=%d bytes=%d duration_ms=%d error=%s",
        action or "unknown",
        checkpoint if checkpoint is not None else "all",
        direction or "none",
        count,
        formatted_bytes,
        duration_ms,
        error_code or "none",
    )


__all__ = [
    "HISTORY_ACTIONS",
    "HISTORY_DEFAULT_ROLES",
    "HISTORY_RESULT_MAX_BYTES",
    "HISTORY_SUPPORTED_ROLES",
    "HISTORY_TOOL_DESCRIPTION",
    "HISTORY_TOOL_NAME",
    "HISTORY_TOOL_PARAMETERS",
    "make_history_handler",
    "register_history_tool",
]
