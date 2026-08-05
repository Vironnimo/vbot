"""Agent-facing Session discovery and lossless canonical Message retrieval."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import inspect
import json
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as datetime_time
from typing import Any

from core.recall import (
    RECALL_BACKEND_JSONL_SCAN,
    JsonlSessionRecallBackend,
    RecallRequest,
    RecallSearchCapabilities,
    RecallSearchError,
    RecallSearchHit,
    RecallSearchPage,
    RecallSearchRequest,
    SupportsRecallSearch,
)
from core.recall.jsonl import (
    SESSION_RECALL_DEFAULT_ROLES,
)
from core.sessions import ChatSessionManager
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.session_search")

SESSION_SEARCH_TOOL_NAME = "session_search"
SESSION_READ_TOOL_NAME = "session_read"
SESSION_SEARCH_DEFAULT_LIMIT = 10
SESSION_SEARCH_MAX_LIMIT = 100
SESSION_SEARCH_RESULT_MAX_BYTES = 50 * 1024
SESSION_SEARCH_CURSOR_VERSION = 2

_BASE_DESCRIPTION = (
    "Find persisted Sessions and relevant conversation passages. Omit query to list recent "
    "Sessions; provide query to search. Results include exact session_read references. "
    "Continue a page with cursor by itself."
)
SESSION_SEARCH_TOOL_DESCRIPTION = _BASE_DESCRIPTION
SESSION_READ_TOOL_DESCRIPTION = (
    "Read exact canonical Messages from one persisted Session. Omit both Message boundaries to "
    "read the whole Session, provide start_message_id and/or end_message_id for an inclusive "
    "range, and continue oversized results with cursor by itself. Records are returned losslessly."
)


class _SessionSearchError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Cursor:
    action: str
    arguments: JsonObject
    offset: int
    within_offset: int
    snapshot_id: str | None


def build_session_search_parameters() -> JsonObject:
    return {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "Text or meaning to find; omit to list recent Sessions.",
            },
            "period": {
                "type": "string",
                "minLength": 2,
                "description": (
                    "Inclusive ISO-8601 start/end interval. Either endpoint may be empty, "
                    "for example 2026-07-25/2026-07-26 or 2026-07-25T00:00:00+02:00/."
                ),
            },
            "agent_id": {
                "type": "string",
                "minLength": 1,
                "description": "Agent whose Sessions to find; defaults to the current Agent.",
            },
            "session_id": {
                "type": "string",
                "minLength": 1,
                "description": "Restrict discovery or search to one Session.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": SESSION_SEARCH_MAX_LIMIT,
                "default": SESSION_SEARCH_DEFAULT_LIMIT,
                "description": "Maximum results in this page.",
            },
            "cursor": {
                "type": "string",
                "minLength": 1,
                "description": "Opaque continuation; when set, omit every other argument.",
            },
        },
        "required": [],
    }


def build_session_read_parameters() -> JsonObject:
    return {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "minLength": 1,
                "description": "Session to read. Required for a new read; omit with cursor.",
            },
            "agent_id": {
                "type": "string",
                "minLength": 1,
                "description": "Agent that owns the Session. Omit for the current Agent.",
            },
            "start_message_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "First Message of a new inclusive range. Omit to start at the beginning."
                ),
            },
            "end_message_id": {
                "type": "string",
                "minLength": 1,
                "description": "Last Message of a new inclusive range. Omit to read to the end.",
            },
            "cursor": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Previous session_read continuation. When set, omit every other field."
                ),
            },
        },
        "required": [],
    }


SESSION_SEARCH_TOOL_PARAMETERS = build_session_search_parameters()
SESSION_READ_TOOL_PARAMETERS = build_session_read_parameters()


def build_session_search_description(recall_backend: Any) -> str:
    capabilities = _search_capabilities(recall_backend)
    return f"{_BASE_DESCRIPTION} Active search behavior: {capabilities.guidance}"


def make_session_search_handler(
    recall_backend: Any,
    sessions: ChatSessionManager | None = None,
    backend_name: str | None = None,
):
    resolved_sessions = sessions or _backend_sessions(recall_backend)
    resolved_name = backend_name or _backend_name(recall_backend)

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await session_search_handler(
            context,
            arguments,
            recall_backend,
            sessions=resolved_sessions,
            backend_name=resolved_name,
        )

    return handler


async def session_search_handler(
    context: ToolContext,
    arguments: JsonObject,
    recall_backend: Any,
    *,
    sessions: ChatSessionManager | None = None,
    backend_name: str | None = None,
) -> JsonObject:
    started = time.perf_counter()
    action = "list"
    resolved_sessions = sessions or _backend_sessions(recall_backend)
    resolved_name = backend_name or _backend_name(recall_backend)
    try:
        if not isinstance(arguments, dict):
            raise _SessionSearchError("invalid_arguments", "arguments must be an object")
        cursor = _parse_cursor(
            arguments,
            context,
            resolved_name,
            allowed_actions=frozenset({"list", "search"}),
        )
        if cursor is not None:
            effective = dict(cursor.arguments)
            action = cursor.action
        else:
            _validate_session_search_fields(arguments)
            action = "search" if "query" in arguments else "list"
            effective = {"action": action, **arguments}
        capabilities = _search_capabilities(recall_backend)
        if action == "list" and resolved_sessions is None:
            raise _SessionSearchError(
                "session_search_unavailable", "Canonical Session storage is unavailable."
            )
        if action == "list":
            assert resolved_sessions is not None
            data = await _list_sessions(context, effective, resolved_sessions, cursor)
        else:
            data = await _search_sessions(
                context,
                effective,
                recall_backend,
                resolved_name,
                capabilities,
                cursor,
                resolved_sessions,
            )
        result = tool_success(data)
        _LOGGER.info(
            "session_search action=%s backend=%s count=%s has_more=%s "
            "formatted_bytes=%s duration_ms=%s",
            action,
            resolved_name,
            len(data.get("items", [])) if isinstance(data.get("items"), list) else 0,
            data.get("has_more", False),
            data.get("formatted_bytes", 0),
            round((time.perf_counter() - started) * 1000),
        )
        return result
    except RecallSearchError as error:
        return tool_failure(error.code, str(error))
    except _SessionSearchError as error:
        return tool_failure(error.code, str(error))
    except Exception:
        _LOGGER.error("session_search failed unexpectedly", exc_info=True)
        return tool_failure("session_search_error", "Unable to access persisted Sessions.")


def make_session_read_handler(sessions: ChatSessionManager | None):
    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await session_read_handler(context, arguments, sessions)

    return handler


async def session_read_handler(
    context: ToolContext,
    arguments: JsonObject,
    sessions: ChatSessionManager | None,
) -> JsonObject:
    started = time.perf_counter()
    try:
        if not isinstance(arguments, dict):
            raise _SessionSearchError("invalid_arguments", "arguments must be an object")
        if sessions is None:
            raise _SessionSearchError(
                "session_read_unavailable", "Canonical Session storage is unavailable."
            )
        cursor = _parse_cursor(
            arguments,
            context,
            None,
            allowed_actions=frozenset({"read"}),
        )
        if cursor is not None:
            effective = dict(cursor.arguments)
        else:
            _validate_session_read_fields(arguments)
            effective = {"action": "read", **arguments}
        data = await _read_session(context, effective, sessions, cursor)
        result = tool_success(data)
        _LOGGER.info(
            "session_read count=%s has_more=%s formatted_bytes=%s duration_ms=%s",
            len(data.get("items", [])) if isinstance(data.get("items"), list) else 0,
            data.get("has_more", False),
            data.get("formatted_bytes", 0),
            round((time.perf_counter() - started) * 1000),
        )
        return result
    except _SessionSearchError as error:
        return tool_failure(error.code, str(error))
    except Exception:
        _LOGGER.error("session_read failed unexpectedly", exc_info=True)
        return tool_failure("session_read_error", "Unable to read the persisted Session.")


def register_session_search_tool(
    registry: ToolRegistry,
    recall_backend: Any,
    sessions: ChatSessionManager | None = None,
    backend_name: str | None = None,
) -> None:
    if isinstance(recall_backend, ChatSessionManager):
        sessions = recall_backend
        recall_backend = JsonlSessionRecallBackend(recall_backend)
        backend_name = RECALL_BACKEND_JSONL_SCAN
    resolved_sessions = sessions or _backend_sessions(recall_backend)
    registry.register(
        SESSION_SEARCH_TOOL_NAME,
        build_session_search_description(recall_backend),
        SESSION_SEARCH_TOOL_PARAMETERS,
        make_session_search_handler(recall_backend, sessions, backend_name),
        open_input_schema=True,
        result_schema={
            "type": "object",
            "required": ["items", "has_more", "formatted_bytes"],
        },
        parallel_safe=True,
        display=ToolDisplay(
            parts_builder=_display_search_parts,
            hidden_argument_keys=("query", "cursor"),
        ),
    )
    registry.register(
        SESSION_READ_TOOL_NAME,
        SESSION_READ_TOOL_DESCRIPTION,
        SESSION_READ_TOOL_PARAMETERS,
        make_session_read_handler(resolved_sessions),
        open_input_schema=True,
        result_schema={
            "type": "object",
            "required": ["session_id", "session", "items", "has_more", "formatted_bytes"],
        },
        parallel_safe=True,
        display=ToolDisplay(
            parts_builder=_display_read_parts,
            hidden_argument_keys=("start_message_id", "end_message_id", "cursor"),
        ),
    )


async def _list_sessions(
    context: ToolContext,
    arguments: JsonObject,
    sessions: ChatSessionManager,
    cursor: _Cursor | None,
) -> JsonObject:
    agent_id = _agent_id(arguments, context)
    limit = _limit(arguments)
    offset = cursor.offset if cursor is not None else 0
    summaries = await asyncio.to_thread(sessions.list_with_metadata, agent_id, context.project_id)
    snapshot = await asyncio.to_thread(
        _session_list_snapshot, sessions, agent_id, context.project_id, summaries
    )
    _validate_snapshot(cursor, snapshot)
    selected_session_id = _optional_string(arguments.get("session_id"))
    if selected_session_id is not None:
        summaries = [
            summary for summary in summaries if str(summary.get("id")) == selected_session_id
        ]
    since, until = _parse_period(arguments.get("period"))
    period_refs: dict[str, tuple[str, str]] = {}
    if since is not None or until is not None:
        summaries, period_refs = await asyncio.to_thread(
            _filter_session_summaries_by_period,
            sessions,
            agent_id,
            context.project_id,
            summaries,
            since,
            until,
        )
    summaries.sort(key=lambda item: str(item.get("last_active_at") or ""), reverse=True)
    source = [
        _session_summary(
            agent_id,
            summary,
            period_ref=period_refs.get(str(summary.get("id") or "")),
        )
        for summary in summaries
    ]
    page = source[offset : offset + limit]
    has_more = offset + len(page) < len(source)
    normalized = {"action": "list", "agent_id": agent_id, "limit": limit}
    for key in ("period", "session_id"):
        if key in arguments:
            normalized[key] = arguments[key]
    data = _page_data(page, has_more=has_more)
    if has_more:
        data["next_cursor"] = _cursor_token(
            "list", normalized, offset + len(page), 0, snapshot, context, None
        )
    return _with_formatted_bytes(data)


async def _read_session(
    context: ToolContext,
    arguments: JsonObject,
    sessions: ChatSessionManager,
    cursor: _Cursor | None,
) -> JsonObject:
    agent_id = _agent_id(arguments, context)
    session_id = _required_string(arguments, "session_id")
    start_message_id = _optional_string(arguments.get("start_message_id"))
    end_message_id = _optional_string(arguments.get("end_message_id"))
    try:
        session = sessions.get(agent_id, session_id, context.project_id)
        messages = await asyncio.to_thread(session.load)
        stat = await asyncio.to_thread(session.path.stat)
        metadata = await asyncio.to_thread(
            sessions.get_metadata, agent_id, session_id, context.project_id
        )
    except Exception as error:
        raise _SessionSearchError(
            "session_not_found", f"Session not found: {session_id}"
        ) from error
    snapshot = f"{session_id}:{stat.st_mtime_ns}:{stat.st_size}"
    _validate_snapshot(cursor, snapshot)
    indices = {str(message.id): index for index, message in enumerate(messages)}
    if start_message_id is not None and start_message_id not in indices:
        raise _SessionSearchError("message_not_found", f"Message not found: {start_message_id}")
    if end_message_id is not None and end_message_id not in indices:
        raise _SessionSearchError("message_not_found", f"Message not found: {end_message_id}")
    first = indices[start_message_id] if start_message_id is not None else 0
    last = indices[end_message_id] if end_message_id is not None else len(messages) - 1
    if messages and last < first:
        raise _SessionSearchError("invalid_arguments", "end_message_id precedes start_message_id")
    source = [message.to_dict() for message in messages[first : last + 1]]
    normalized: JsonObject = {
        "action": "read",
        "agent_id": agent_id,
        "session_id": session_id,
    }
    if start_message_id is not None:
        normalized["start_message_id"] = start_message_id
    if end_message_id is not None:
        normalized["end_message_id"] = end_message_id
    session_details = _session_details(agent_id, session_id, metadata, messages)
    next_index = cursor.offset if cursor is not None else 0
    within_offset = cursor.within_offset if cursor is not None else 0
    return _render_read_page(
        source,
        next_index,
        within_offset,
        normalized,
        snapshot,
        context,
        session_id,
        session_details,
    )


async def _search_sessions(
    context: ToolContext,
    arguments: JsonObject,
    recall_backend: Any,
    backend_name: str,
    capabilities: RecallSearchCapabilities,
    cursor: _Cursor | None,
    sessions: ChatSessionManager | None,
) -> JsonObject:
    query = _required_string(arguments, "query")
    agent_id = _agent_id(arguments, context)
    limit = _limit(arguments)
    roles = SESSION_RECALL_DEFAULT_ROLES
    match_mode = "all_terms"
    raw_order = capabilities.default_order
    since, until = _parse_period(arguments.get("period"))
    offset = cursor.offset if cursor is not None else 0
    request = RecallSearchRequest(
        agent_id=agent_id,
        project_id=context.project_id,
        session_id=_optional_string(arguments.get("session_id")),
        query=query,
        since=since,
        until=until,
        roles=roles,
        match_mode=match_mode,  # type: ignore[arg-type]
        order=str(raw_order),  # type: ignore[arg-type]
        offset=offset,
        limit=limit,
        snapshot_id=cursor.snapshot_id if cursor is not None else None,
    )
    if isinstance(recall_backend, SupportsRecallSearch):
        page = await _call_search_page(recall_backend, request)
        read_refs = await asyncio.to_thread(
            _read_refs_for_hits,
            list(page.hits),
            agent_id=agent_id,
            project_id=context.project_id,
            sessions=sessions,
        )
        normalized = _normalized_search_arguments(
            arguments,
            agent_id=agent_id,
            query=query,
            limit=limit,
        )
        return _render_search_page(
            page,
            normalized,
            offset,
            context,
            backend_name,
            read_refs,
        )
    return await _legacy_search(
        context,
        arguments,
        recall_backend,
        backend_name,
        agent_id,
        query,
        roles,
        match_mode,
        str(raw_order),
        since,
        until,
        limit,
    )


async def _call_search_page(backend: Any, request: RecallSearchRequest) -> RecallSearchPage:
    method = backend.search_page
    if inspect.iscoroutinefunction(method):
        result = await method(request)
    else:
        result = await asyncio.to_thread(method, request)
        if inspect.isawaitable(result):
            result = await result
    if not isinstance(result, RecallSearchPage):
        raise _SessionSearchError(
            "invalid_backend_result", "Recall backend returned an invalid typed search page."
        )
    return result


async def _legacy_search(
    context: ToolContext,
    arguments: JsonObject,
    backend: Any,
    backend_name: str,
    agent_id: str,
    query: str,
    roles: tuple[str, ...],
    match_mode: str,
    order: str,
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> JsonObject:
    request = RecallRequest(
        agent_id=agent_id,
        session_id=_optional_string(arguments.get("session_id")),
        around_message_id=None,
        query=query,
        since=since,
        until=until,
        roles=roles,
        match_mode=match_mode,  # type: ignore[arg-type]
        limit=limit,
        context_messages=0,
        bookend_messages=0,
        sort="oldest" if order == "oldest" else "newest",
        project_id=context.project_id,
    )
    method = getattr(backend, "search", None)
    if method is None:
        raise _SessionSearchError("invalid_backend", "Recall backend has no search operation.")
    if inspect.iscoroutinefunction(method):
        payload = await method(request)
    else:
        payload = await asyncio.to_thread(method, request)
        if inspect.isawaitable(payload):
            payload = await payload
    if not isinstance(payload, dict):
        raise _SessionSearchError(
            "invalid_backend_result", "Legacy Recall backend returned a non-object result."
        )
    return _with_formatted_bytes(
        {
            "backend": backend_name,
            "result_type": "backend_defined",
            "ranking": "backend_defined",
            "items": [{"backend_result": payload}],
            "has_more": False,
        }
    )


def _render_search_page(
    page: RecallSearchPage,
    normalized: JsonObject,
    offset: int,
    context: ToolContext,
    backend_name: str,
    read_refs: list[JsonObject],
) -> JsonObject:
    hits = list(page.hits)
    if len(read_refs) != len(hits):
        raise _SessionSearchError(
            "session_search_error", "Search read references do not match the result page."
        )
    count = len(hits)
    while count > 0:
        items = [
            _hit_item(
                hit,
                offset + index + 1,
                excerpt_chars=0,
                read_ref=read_refs[index],
            )
            for index, hit in enumerate(hits[:count])
        ]
        has_more = count < len(hits) or page.has_more
        data = _search_data(page, backend_name, items, has_more=has_more)
        if has_more:
            data["next_cursor"] = _cursor_token(
                "search",
                normalized,
                offset + count,
                0,
                page.snapshot_id,
                context,
                backend_name,
            )
        if _serialized_result_bytes(data) <= SESSION_SEARCH_RESULT_MAX_BYTES:
            break
        count -= 1
    if count == 0 and hits:
        raise _SessionSearchError(
            "session_search_error", "Search result metadata exceeds the result safety limit."
        )
    selected = hits[:count]
    if not selected:
        return _with_formatted_bytes(_search_data(page, backend_name, [], has_more=False))

    maximum = max(len(hit.text) for hit in selected)
    low = 1
    high = maximum
    best: JsonObject | None = None
    while low <= high:
        excerpt_chars = (low + high) // 2
        items = [
            _hit_item(
                hit,
                offset + index + 1,
                excerpt_chars=excerpt_chars,
                read_ref=read_refs[index],
            )
            for index, hit in enumerate(selected)
        ]
        has_more = count < len(hits) or page.has_more
        candidate = _search_data(page, backend_name, items, has_more=has_more)
        if has_more:
            candidate["next_cursor"] = _cursor_token(
                "search",
                normalized,
                offset + count,
                0,
                page.snapshot_id,
                context,
                backend_name,
            )
        if _serialized_result_bytes(candidate) <= SESSION_SEARCH_RESULT_MAX_BYTES:
            best = candidate
            low = excerpt_chars + 1
        else:
            high = excerpt_chars - 1
    if best is None:
        raise _SessionSearchError(
            "session_search_error", "Search excerpts exceed the result safety limit."
        )
    return _with_formatted_bytes(best)


def _search_data(
    page: RecallSearchPage,
    backend_name: str,
    items: list[JsonObject],
    *,
    has_more: bool,
) -> JsonObject:
    data: JsonObject = {
        "backend": backend_name,
        "result_type": page.result_type,
        "ranking": page.ranking,
        "items": items,
        "has_more": has_more,
        "searched_sessions": page.total_candidate_sessions,
    }
    if page.degraded:
        data["degraded"] = True
        data["degradation_reason"] = page.degradation_reason
    return data


def _hit_item(
    hit: RecallSearchHit,
    rank: int,
    *,
    excerpt_chars: int,
    read_ref: JsonObject,
) -> JsonObject:
    start, end = _excerpt_bounds(hit, excerpt_chars)
    item: JsonObject = {
        "rank": rank,
        "agent_id": read_ref["agent_id"],
        "session_id": hit.session_id,
        "message_id": hit.message_id,
        "role": hit.role,
        "timestamp": hit.timestamp,
        "excerpt": {
            "text": hit.text[start:end],
            "source_start": start,
            "source_end": end,
            "leading_truncated": start > 0,
            "trailing_truncated": end < len(hit.text),
        },
    }
    if hit.result_type == "passage":
        item["passage_id"] = hit.passage_id
        item["end_timestamp"] = hit.end_timestamp
    item["read_ref"] = read_ref
    if hit.sources:
        item["sources"] = list(hit.sources)
    return item


def _read_refs_for_hits(
    hits: list[RecallSearchHit],
    *,
    agent_id: str,
    project_id: str | None,
    sessions: ChatSessionManager | None,
) -> list[JsonObject]:
    loaded: dict[str, list[Any] | None] = {}
    refs: list[JsonObject] = []
    for hit in hits:
        start_message_id = hit.start_message_id or hit.message_id
        end_message_id = hit.end_message_id or hit.message_id
        messages = loaded.get(hit.session_id)
        if hit.session_id not in loaded:
            messages = _load_search_hit_session(
                sessions,
                agent_id,
                hit.session_id,
                project_id,
            )
            loaded[hit.session_id] = messages
        derived = _conversation_range(
            messages,
            start_message_id,
            end_message_id,
        )
        if derived is not None:
            start_message_id, end_message_id = derived
        refs.append(
            {
                "agent_id": agent_id,
                "session_id": hit.session_id,
                "start_message_id": start_message_id,
                "end_message_id": end_message_id,
            }
        )
    return refs


def _load_search_hit_session(
    sessions: ChatSessionManager | None,
    agent_id: str,
    session_id: str,
    project_id: str | None,
) -> list[Any] | None:
    if sessions is None:
        return None
    try:
        return sessions.get(agent_id, session_id, project_id).load()
    except Exception:
        return None


def _conversation_range(
    messages: list[Any] | None,
    start_message_id: str,
    end_message_id: str,
) -> tuple[str, str] | None:
    if not messages:
        return None
    start = next(
        (index for index, message in enumerate(messages) if str(message.id) == start_message_id),
        None,
    )
    end = next(
        (index for index, message in enumerate(messages) if str(message.id) == end_message_id),
        None,
    )
    if start is None or end is None or end < start:
        return None
    while start > 0 and str(messages[start].role) != "user":
        start -= 1
        if str(messages[start].role) == "user":
            break
    while end + 1 < len(messages) and str(messages[end + 1].role) != "user":
        end += 1
    return str(messages[start].id), str(messages[end].id)


def _excerpt_bounds(hit: RecallSearchHit, excerpt_chars: int) -> tuple[int, int]:
    if excerpt_chars <= 0 or not hit.text:
        return 0, 0
    if len(hit.text) <= excerpt_chars:
        return 0, len(hit.text)
    anchor = hit.match_start if hit.match_start is not None else len(hit.text) // 2
    start = max(anchor - excerpt_chars // 3, 0)
    end = min(start + excerpt_chars, len(hit.text))
    start = max(end - excerpt_chars, 0)
    return start, end


def _render_read_page(
    source: list[JsonObject],
    next_index: int,
    within_offset: int,
    normalized: JsonObject,
    snapshot: str,
    context: ToolContext,
    session_id: str,
    session_details: JsonObject,
) -> JsonObject:
    items: list[JsonObject] = []
    index = next_index
    while index < len(source):
        message = source[index]
        if within_offset == 0:
            candidate_items = [*items, {"message": message}]
            has_more = index + 1 < len(source)
            candidate = _read_data(
                session_id,
                session_details,
                candidate_items,
                has_more=has_more,
            )
            if has_more:
                candidate["next_cursor"] = _cursor_token(
                    "read", normalized, index + 1, 0, snapshot, context, None
                )
            if _serialized_result_bytes(candidate) <= SESSION_SEARCH_RESULT_MAX_BYTES:
                items.append({"message": message})
                index += 1
                continue
            if items:
                data = _read_data(session_id, session_details, items, has_more=True)
                data["next_cursor"] = _cursor_token(
                    "read", normalized, index, 0, snapshot, context, None
                )
                return _with_formatted_bytes(data)
        return _segmented_read_record(
            source,
            index,
            within_offset,
            normalized,
            snapshot,
            context,
            session_id,
            session_details,
        )
    return _with_formatted_bytes(_read_data(session_id, session_details, items, has_more=False))


def _segmented_read_record(
    source: list[JsonObject],
    index: int,
    offset: int,
    normalized: JsonObject,
    snapshot: str,
    context: ToolContext,
    session_id: str,
    session_details: JsonObject,
) -> JsonObject:
    message = source[index]
    record_json = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    if offset >= len(record_json):
        raise _SessionSearchError("invalid_cursor", "Read cursor is invalid.")
    low = offset + 1
    high = len(record_json)
    best: JsonObject | None = None
    while low <= high:
        end = (low + high) // 2
        incomplete = end < len(record_json)
        next_index = index if incomplete else index + 1
        has_more = incomplete or next_index < len(source)
        item: JsonObject = {
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
        candidate = _read_data(
            session_id,
            session_details,
            [item],
            has_more=has_more,
        )
        if has_more:
            candidate["next_cursor"] = _cursor_token(
                "read",
                normalized,
                next_index,
                end if incomplete else 0,
                snapshot,
                context,
                None,
            )
        if _serialized_result_bytes(candidate) <= SESSION_SEARCH_RESULT_MAX_BYTES:
            best = candidate
            low = end + 1
        else:
            high = end - 1
    if best is None:
        raise _SessionSearchError(
            "session_read_error", "Read metadata exceeds the result safety limit."
        )
    return _with_formatted_bytes(best)


def _read_data(
    session_id: str,
    session_details: JsonObject,
    items: list[JsonObject],
    *,
    has_more: bool,
) -> JsonObject:
    return {
        "session_id": session_id,
        "session": session_details,
        "items": items,
        "has_more": has_more,
    }


def _page_data(items: list[JsonObject], *, has_more: bool) -> JsonObject:
    return {"result_type": "session", "items": items, "has_more": has_more}


def _session_summary(
    agent_id: str,
    summary: JsonObject,
    *,
    period_ref: tuple[str, str] | None = None,
) -> JsonObject:
    item: JsonObject = {
        "agent_id": agent_id,
        "session_id": summary.get("id"),
        "created_at": summary.get("created_at"),
        "last_active_at": summary.get("last_active_at"),
        "title": summary.get("title") or summary.get("auto_title"),
    }
    if period_ref is not None:
        item["read_ref"] = {
            "agent_id": agent_id,
            "session_id": summary.get("id"),
            "start_message_id": period_ref[0],
            "end_message_id": period_ref[1],
        }
    return item


def _message_ref(message: Any) -> JsonObject:
    return {
        "message_id": str(message.id),
        "timestamp": str(message.timestamp),
        "role": str(message.role),
    }


def _session_details(
    agent_id: str,
    session_id: str,
    metadata: JsonObject,
    messages: list[Any],
) -> JsonObject:
    return {
        "agent_id": agent_id,
        "session_id": session_id,
        "metadata": metadata,
        "message_count": len(messages),
        "role_counts": dict(Counter(str(message.role) for message in messages)),
        "first_message": _message_ref(messages[0]) if messages else None,
        "last_message": _message_ref(messages[-1]) if messages else None,
    }


def _normalized_search_arguments(
    arguments: JsonObject,
    *,
    agent_id: str,
    query: str,
    limit: int,
) -> JsonObject:
    normalized: JsonObject = {
        "action": "search",
        "agent_id": agent_id,
        "query": query,
        "limit": limit,
    }
    for key in ("session_id", "period"):
        if key in arguments:
            normalized[key] = arguments[key]
    return normalized


def _search_capabilities(backend: Any) -> RecallSearchCapabilities:
    method = getattr(backend, "search_capabilities", None)
    if callable(method):
        result = method()
        if isinstance(result, RecallSearchCapabilities):
            return result
    guidance_method = getattr(backend, "describe_search", None)
    guidance = (
        guidance_method() if callable(guidance_method) else "Backend-defined search behavior."
    )
    return RecallSearchCapabilities(
        result_type="backend_defined",
        guidance=str(guidance),
        default_order="relevance",
    )


def _backend_sessions(backend: Any) -> ChatSessionManager | None:
    value = getattr(backend, "sessions", None)
    return value if isinstance(value, ChatSessionManager) else None


def _backend_name(backend: Any) -> str:
    known = {
        "JsonlSessionRecallBackend": RECALL_BACKEND_JSONL_SCAN,
        "SqliteFtsRecallBackend": "sqlite_fts",
        "VectorRecallBackend": "vector",
        "HybridRecallBackend": "hybrid",
    }
    class_name = backend.__class__.__name__
    return known.get(class_name, class_name.removesuffix("RecallBackend").lower() or "backend")


def _validate_session_search_fields(arguments: JsonObject) -> None:
    allowed = {"query", "period", "agent_id", "session_id", "limit"}
    unsupported = sorted(set(arguments) - allowed)
    if unsupported:
        raise _SessionSearchError(
            "invalid_arguments",
            f"Unsupported session_search arguments: {', '.join(unsupported)}",
        )
    for key in ("query", "agent_id", "session_id"):
        if key in arguments:
            _required_string(arguments, key)
    if "limit" in arguments:
        _limit(arguments)
    if "period" in arguments:
        if arguments["period"] is None:
            raise _SessionSearchError(
                "invalid_arguments", "period must be an ISO-8601 start/end interval"
            )
        _parse_period(arguments["period"])


def _validate_session_read_fields(arguments: JsonObject) -> None:
    allowed = {"session_id", "agent_id", "start_message_id", "end_message_id"}
    unsupported = sorted(set(arguments) - allowed)
    if unsupported:
        raise _SessionSearchError(
            "invalid_arguments",
            f"Unsupported session_read arguments: {', '.join(unsupported)}",
        )
    _required_string(arguments, "session_id")
    for key in ("agent_id", "start_message_id", "end_message_id"):
        if key in arguments:
            _required_string(arguments, key)


def _parse_cursor(
    arguments: JsonObject,
    context: ToolContext,
    backend_name: str | None,
    *,
    allowed_actions: frozenset[str],
) -> _Cursor | None:
    raw = arguments.get("cursor")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise _SessionSearchError("invalid_arguments", "cursor must be a non-blank string")
    if set(arguments) != {"cursor"}:
        raise _SessionSearchError("invalid_arguments", "A cursor continuation accepts only cursor.")
    payload = _decode_cursor(raw)
    required = {
        "v",
        "action",
        "arguments",
        "offset",
        "within_offset",
        "snapshot_id",
        "project_id",
        "backend",
    }
    if set(payload) != required or payload.get("v") != SESSION_SEARCH_CURSOR_VERSION:
        raise _SessionSearchError("invalid_cursor", "Session cursor is invalid.")
    action = payload.get("action")
    if not isinstance(action, str) or action not in allowed_actions:
        raise _SessionSearchError("invalid_cursor", "Session cursor is invalid.")
    if payload.get("project_id") != context.project_id:
        raise _SessionSearchError("invalid_cursor", "Session cursor is invalid.")
    cursor_backend = payload.get("backend")
    if action == "search" and cursor_backend != backend_name:
        raise _SessionSearchError(
            "stale_cursor", "Recall backend changed; repeat the original search."
        )
    values = payload.get("arguments")
    if not isinstance(values, dict) or values.get("action") != action:
        raise _SessionSearchError("invalid_cursor", "Session cursor is invalid.")
    return _Cursor(
        action=str(action),
        arguments=dict(values),
        offset=_cursor_int(payload.get("offset")),
        within_offset=_cursor_int(payload.get("within_offset")),
        snapshot_id=payload.get("snapshot_id")
        if isinstance(payload.get("snapshot_id"), str)
        else None,
    )


def _cursor_token(
    action: str,
    arguments: JsonObject,
    offset: int,
    within_offset: int,
    snapshot_id: str | None,
    context: ToolContext,
    backend_name: str | None,
) -> str:
    return _encode_cursor(
        {
            "v": SESSION_SEARCH_CURSOR_VERSION,
            "action": action,
            "arguments": arguments,
            "offset": offset,
            "within_offset": within_offset,
            "snapshot_id": snapshot_id,
            "project_id": context.project_id,
            "backend": backend_name,
        }
    )


def _encode_cursor(payload: JsonObject) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    digest = hashlib.sha256(body).digest()
    return base64.urlsafe_b64encode(body + digest).decode("ascii").rstrip("=")


def _decode_cursor(token: str) -> JsonObject:
    try:
        padded = token + "=" * (-len(token) % 4)
        encoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        digest_size = hashlib.sha256().digest_size
        if len(encoded) <= digest_size:
            raise ValueError
        body = encoded[:-digest_size]
        digest = encoded[-digest_size:]
        if not hmac.compare_digest(hashlib.sha256(body).digest(), digest):
            raise ValueError
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (binascii.Error, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise _SessionSearchError("invalid_cursor", "Session cursor is invalid.") from error


def _validate_snapshot(cursor: _Cursor | None, current: str) -> None:
    if cursor is not None and cursor.snapshot_id != current:
        raise _SessionSearchError("stale_cursor", "Session source changed; repeat the action.")


def _session_list_snapshot(
    sessions: ChatSessionManager,
    agent_id: str,
    project_id: str | None,
    summaries: list[JsonObject],
) -> str:
    parts: list[str] = []
    for summary in sorted(summaries, key=lambda item: str(item.get("id", ""))):
        session_id = str(summary["id"])
        stat = sessions.get(agent_id, session_id, project_id).path.stat()
        parts.append(f"{session_id}:{stat.st_mtime_ns}:{stat.st_size}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _filter_session_summaries_by_period(
    sessions: ChatSessionManager,
    agent_id: str,
    project_id: str | None,
    summaries: list[JsonObject],
    since: datetime | None,
    until: datetime | None,
) -> tuple[list[JsonObject], dict[str, tuple[str, str]]]:
    selected: list[JsonObject] = []
    refs: dict[str, tuple[str, str]] = {}
    for summary in summaries:
        session_id = str(summary.get("id") or "")
        if not session_id:
            continue
        try:
            messages = sessions.get(agent_id, session_id, project_id).load()
        except Exception:
            continue
        matching = [
            message
            for message in messages
            if str(message.role) in SESSION_RECALL_DEFAULT_ROLES
            and _timestamp_in_period(message.timestamp, since, until)
        ]
        if not matching:
            continue
        selected.append(summary)
        derived = _conversation_range(
            messages,
            str(matching[0].id),
            str(matching[-1].id),
        )
        if derived is not None:
            refs[session_id] = derived
    return selected, refs


def _timestamp_in_period(
    value: Any,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    timestamp = _timestamp_utc(value)
    if timestamp is None:
        return False
    return (since is None or timestamp >= since) and (until is None or timestamp <= until)


def _timestamp_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    try:
        return _parse_datetime(value, "Message timestamp", end_of_day=False)
    except _SessionSearchError:
        return None


def _with_formatted_bytes(data: JsonObject) -> JsonObject:
    result = dict(data)
    previous = -1
    for _ in range(4):
        size = len(
            json.dumps(tool_success(result), ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        result["formatted_bytes"] = size
        if size == previous:
            break
        previous = size
    return result


def _serialized_result_bytes(data: JsonObject) -> int:
    finalized = _with_formatted_bytes(data)
    return len(
        json.dumps(tool_success(finalized), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _agent_id(arguments: JsonObject, context: ToolContext) -> str:
    raw = arguments.get("agent_id")
    return raw.strip() if isinstance(raw, str) and raw.strip() else context.agent_id


def _limit(arguments: JsonObject) -> int:
    return _bounded_int(
        arguments.get("limit", SESSION_SEARCH_DEFAULT_LIMIT),
        "limit",
        1,
        SESSION_SEARCH_MAX_LIMIT,
    )


def _required_string(arguments: JsonObject, key: str) -> str:
    value = _optional_string(arguments.get(key))
    if value is None:
        raise _SessionSearchError("invalid_arguments", f"{key} must be a non-blank string")
    return value


def _optional_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise _SessionSearchError("invalid_arguments", f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise _SessionSearchError("invalid_arguments", f"{name} must be an integer") from error
    if parsed < minimum or parsed > maximum:
        raise _SessionSearchError(
            "invalid_arguments", f"{name} must be between {minimum} and {maximum}"
        )
    return parsed


def _cursor_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _SessionSearchError("invalid_cursor", "Session search cursor is invalid.")
    return value


def _parse_datetime(value: Any, name: str, *, end_of_day: bool) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _SessionSearchError("invalid_arguments", f"{name} must be an ISO-8601 string")
    raw = value.strip()
    try:
        if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            parsed_date = datetime.fromisoformat(raw).date()
            boundary = datetime_time.max if end_of_day else datetime_time.min
            return datetime.combine(parsed_date, boundary, tzinfo=UTC)
        normalized = raw.removesuffix("Z") + "+00:00" if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise _SessionSearchError("invalid_arguments", f"{name} must be valid ISO-8601") from error
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_period(value: Any) -> tuple[datetime | None, datetime | None]:
    if value is None:
        return None, None
    if not isinstance(value, str) or not value.strip():
        raise _SessionSearchError(
            "invalid_arguments", "period must be an ISO-8601 start/end interval"
        )
    raw = value.strip()
    if raw.count("/") != 1:
        raise _SessionSearchError(
            "invalid_arguments", "period must contain one start/end separator '/'"
        )
    start_raw, end_raw = raw.split("/", 1)
    if not start_raw and not end_raw:
        raise _SessionSearchError("invalid_arguments", "period must contain at least one endpoint")
    since = _parse_datetime(start_raw or None, "period start", end_of_day=False)
    until = _parse_datetime(end_raw or None, "period end", end_of_day=True)
    if since is not None and until is not None and since > until:
        raise _SessionSearchError("invalid_arguments", "period start must not be after period end")
    return since, until


def _display_search_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    if "cursor" in arguments:
        return (ToolDisplayPart("continue", truncate="never", tooltip="none"),)
    session_id = _optional_string(arguments.get("session_id"))
    parts = [ToolDisplayPart("find", truncate="never", tooltip="none")]
    if session_id:
        parts.append(ToolDisplayPart(session_id, kind="identifier", truncate="middle"))
    return tuple(parts)


def _display_read_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    if "cursor" in arguments:
        return (ToolDisplayPart("continue", truncate="never", tooltip="none"),)
    session_id = _optional_string(arguments.get("session_id"))
    if not session_id:
        return ()
    return (ToolDisplayPart(session_id, kind="identifier", truncate="middle"),)


__all__ = [
    "SESSION_SEARCH_CURSOR_VERSION",
    "SESSION_SEARCH_DEFAULT_LIMIT",
    "SESSION_SEARCH_MAX_LIMIT",
    "SESSION_SEARCH_RESULT_MAX_BYTES",
    "SESSION_READ_TOOL_DESCRIPTION",
    "SESSION_READ_TOOL_NAME",
    "SESSION_READ_TOOL_PARAMETERS",
    "SESSION_SEARCH_TOOL_DESCRIPTION",
    "SESSION_SEARCH_TOOL_NAME",
    "SESSION_SEARCH_TOOL_PARAMETERS",
    "build_session_read_parameters",
    "build_session_search_description",
    "build_session_search_parameters",
    "make_session_read_handler",
    "make_session_search_handler",
    "register_session_search_tool",
    "session_read_handler",
    "session_search_handler",
]
