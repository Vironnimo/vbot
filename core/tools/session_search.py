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
    SESSION_RECALL_SUPPORTED_ROLES,
)
from core.sessions import ChatSessionManager
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.session_search")

SESSION_SEARCH_TOOL_NAME = "session_search"
SESSION_SEARCH_ACTIONS = ("list", "overview", "search", "read")
SESSION_SEARCH_DEFAULT_LIMIT = 10
SESSION_SEARCH_MAX_LIMIT = 100
SESSION_SEARCH_MAX_NEIGHBORS = 100
SESSION_SEARCH_RESULT_MAX_BYTES = 50 * 1024
SESSION_SEARCH_CURSOR_VERSION = 1

_BASE_DESCRIPTION = (
    "Discover persisted Sessions and retrieve canonical Messages. Choose an explicit action: "
    "list returns Session summaries; overview returns counts and boundary references; search "
    "uses the configured Recall backend and returns ranked excerpts with read references; read "
    "returns exact canonical Message records and losslessly segments oversized records. Cursor "
    "continuations accept only action and cursor."
)
SESSION_SEARCH_TOOL_DESCRIPTION = _BASE_DESCRIPTION

_DEFAULT_CAPABILITIES = RecallSearchCapabilities(
    result_type="message",
    guidance=(
        "Literal case-insensitive substring scan ordered by canonical Message time. "
        "Use read on a returned reference for the exact original Message."
    ),
    match_argument="match",
    match_modes=("all_terms", "any_term", "phrase"),
    order_modes=("newest", "oldest"),
    default_order="newest",
    supports_roles=True,
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


def build_session_search_parameters(
    capabilities: RecallSearchCapabilities | None = None,
) -> JsonObject:
    capabilities = capabilities or _DEFAULT_CAPABILITIES
    properties: JsonObject = {
        "action": {
            "type": "string",
            "enum": list(SESSION_SEARCH_ACTIONS),
            "description": "Operation to perform: list, overview, search, or read.",
        },
        "query": {"type": "string", "description": "Non-blank query for search."},
        "agent_id": {
            "type": "string",
            "description": "Agent whose Sessions to access; defaults to the current agent.",
        },
        "session_id": {
            "type": "string",
            "description": "Session to restrict search to, inspect, or read.",
        },
        "message_id": {
            "type": "string",
            "description": "One canonical Message to read exactly.",
        },
        "start_message_id": {
            "type": "string",
            "description": "First Message of an inclusive canonical read range.",
        },
        "end_message_id": {
            "type": "string",
            "description": "Last Message of an inclusive canonical read range.",
        },
        "before": {
            "type": "integer",
            "minimum": 0,
            "maximum": SESSION_SEARCH_MAX_NEIGHBORS,
            "description": "Additional canonical Messages before the read target; default 0.",
        },
        "after": {
            "type": "integer",
            "minimum": 0,
            "maximum": SESSION_SEARCH_MAX_NEIGHBORS,
            "description": "Additional canonical Messages after the read target; default 0.",
        },
        "since": {
            "type": "string",
            "description": "Inclusive UTC ISO-8601 timestamp or YYYY-MM-DD lower bound.",
        },
        "until": {
            "type": "string",
            "description": "Inclusive UTC ISO-8601 timestamp or YYYY-MM-DD upper bound.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": SESSION_SEARCH_MAX_LIMIT,
            "description": "Maximum results in this page; default 10, maximum 100.",
        },
        "cursor": {
            "type": "string",
            "description": "Opaque continuation from a previous call of the same action.",
        },
    }
    if capabilities.supports_roles:
        properties["roles"] = {
            "type": "array",
            "items": {"type": "string", "enum": list(SESSION_RECALL_SUPPORTED_ROLES)},
            "uniqueItems": True,
            "description": "Message roles eligible for literal search.",
        }
    if capabilities.match_argument is not None:
        description = (
            "Matching mode for the Hybrid literal arm."
            if capabilities.match_argument == "literal_match"
            else "Literal matching mode."
        )
        properties[capabilities.match_argument] = {
            "type": "string",
            "enum": list(capabilities.match_modes),
            "description": description,
        }
    if len(capabilities.order_modes) > 1:
        properties["order"] = {
            "type": "string",
            "enum": list(capabilities.order_modes),
            "description": "Backend-supported search ordering.",
        }
    return {
        "type": "object",
        "properties": properties,
        "required": ["action"],
        "additionalProperties": False,
    }


SESSION_SEARCH_TOOL_PARAMETERS = build_session_search_parameters()


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
    action = arguments.get("action") if isinstance(arguments.get("action"), str) else ""
    resolved_sessions = sessions or _backend_sessions(recall_backend)
    resolved_name = backend_name or _backend_name(recall_backend)
    try:
        if not isinstance(arguments, dict):
            raise _SessionSearchError("invalid_arguments", "arguments must be an object")
        cursor = _parse_cursor(arguments, context, resolved_name)
        effective = dict(cursor.arguments) if cursor is not None else dict(arguments)
        action = _required_action(effective)
        capabilities = _search_capabilities(recall_backend)
        _validate_action_fields(action, effective, capabilities)
        if action != "search" and resolved_sessions is None:
            raise _SessionSearchError(
                "session_search_unavailable", "Canonical Session storage is unavailable."
            )
        if action == "list":
            assert resolved_sessions is not None
            data = await _list_sessions(context, effective, resolved_sessions, cursor)
        elif action == "overview":
            assert resolved_sessions is not None
            data = await _overview_session(context, effective, resolved_sessions)
        elif action == "read":
            assert resolved_sessions is not None
            data = await _read_session(context, effective, resolved_sessions, cursor)
        else:
            data = await _search_sessions(
                context,
                effective,
                recall_backend,
                resolved_name,
                capabilities,
                cursor,
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
    capabilities = _search_capabilities(recall_backend)
    registry.register(
        SESSION_SEARCH_TOOL_NAME,
        build_session_search_description(recall_backend),
        build_session_search_parameters(capabilities),
        make_session_search_handler(recall_backend, sessions, backend_name),
        display=ToolDisplay(
            summary_builder=_display_summary,
            hidden_argument_keys=(
                "query",
                "message_id",
                "start_message_id",
                "end_message_id",
                "cursor",
            ),
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
    summaries.sort(key=lambda item: str(item.get("last_active_at") or ""), reverse=True)
    snapshot = await asyncio.to_thread(
        _session_list_snapshot, sessions, agent_id, context.project_id, summaries
    )
    _validate_snapshot(cursor, snapshot)
    source = [_session_summary(agent_id, summary) for summary in summaries]
    page = source[offset : offset + limit]
    has_more = offset + len(page) < len(source)
    normalized = {"action": "list", "agent_id": agent_id, "limit": limit}
    data = _page_data("list", page, has_more=has_more)
    if has_more:
        data["next_cursor"] = _cursor_token(
            "list", normalized, offset + len(page), 0, snapshot, context, None
        )
    return _with_formatted_bytes(data)


async def _overview_session(
    context: ToolContext,
    arguments: JsonObject,
    sessions: ChatSessionManager,
) -> JsonObject:
    agent_id = _agent_id(arguments, context)
    session_id = _required_string(arguments, "session_id")
    try:
        session = sessions.get(agent_id, session_id, context.project_id)
        messages = await asyncio.to_thread(session.load)
    except Exception as error:
        raise _SessionSearchError(
            "session_not_found", f"Session not found: {session_id}"
        ) from error
    metadata = await asyncio.to_thread(
        sessions.get_metadata, agent_id, session_id, context.project_id
    )
    role_counts = dict(Counter(str(message.role) for message in messages))
    data: JsonObject = {
        "action": "overview",
        "session": {
            "agent_id": agent_id,
            "session_id": session_id,
            "metadata": metadata,
            "message_count": len(messages),
            "role_counts": role_counts,
            "first_message": _message_ref(messages[0]) if messages else None,
            "last_message": _message_ref(messages[-1]) if messages else None,
        },
        "items": [],
        "has_more": False,
    }
    return _with_formatted_bytes(data)


async def _read_session(
    context: ToolContext,
    arguments: JsonObject,
    sessions: ChatSessionManager,
    cursor: _Cursor | None,
) -> JsonObject:
    agent_id = _agent_id(arguments, context)
    session_id = _required_string(arguments, "session_id")
    before = _bounded_int(arguments.get("before", 0), "before", 0, SESSION_SEARCH_MAX_NEIGHBORS)
    after = _bounded_int(arguments.get("after", 0), "after", 0, SESSION_SEARCH_MAX_NEIGHBORS)
    message_id = _optional_string(arguments.get("message_id"))
    start_message_id = _optional_string(arguments.get("start_message_id"))
    end_message_id = _optional_string(arguments.get("end_message_id"))
    if message_id is not None and (start_message_id is not None or end_message_id is not None):
        raise _SessionSearchError(
            "invalid_arguments", "read accepts message_id or a start/end range, not both"
        )
    if message_id is None and (start_message_id is None or end_message_id is None):
        raise _SessionSearchError(
            "invalid_arguments",
            "read requires message_id or both start_message_id and end_message_id",
        )
    try:
        session = sessions.get(agent_id, session_id, context.project_id)
        messages = await asyncio.to_thread(session.load)
        stat = await asyncio.to_thread(session.path.stat)
    except Exception as error:
        raise _SessionSearchError(
            "session_not_found", f"Session not found: {session_id}"
        ) from error
    snapshot = f"{session_id}:{stat.st_mtime_ns}:{stat.st_size}"
    _validate_snapshot(cursor, snapshot)
    indices = {str(message.id): index for index, message in enumerate(messages)}
    if message_id is not None:
        if message_id not in indices:
            raise _SessionSearchError("message_not_found", f"Message not found: {message_id}")
        first = last = indices[message_id]
    else:
        if start_message_id not in indices or end_message_id not in indices:
            raise _SessionSearchError("message_not_found", "Read range Message was not found.")
        first = indices[str(start_message_id)]
        last = indices[str(end_message_id)]
        if last < first:
            raise _SessionSearchError(
                "invalid_arguments", "end_message_id precedes start_message_id"
            )
    range_start = max(first - before, 0)
    range_end = min(last + after + 1, len(messages))
    source = [message.to_dict() for message in messages[range_start:range_end]]
    normalized: JsonObject = {
        "action": "read",
        "agent_id": agent_id,
        "session_id": session_id,
        "before": before,
        "after": after,
    }
    if message_id is not None:
        normalized["message_id"] = message_id
    else:
        normalized["start_message_id"] = start_message_id
        normalized["end_message_id"] = end_message_id
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
    )


async def _search_sessions(
    context: ToolContext,
    arguments: JsonObject,
    recall_backend: Any,
    backend_name: str,
    capabilities: RecallSearchCapabilities,
    cursor: _Cursor | None,
) -> JsonObject:
    query = _required_string(arguments, "query")
    agent_id = _agent_id(arguments, context)
    limit = _limit(arguments)
    roles = (
        _roles(arguments.get("roles"))
        if capabilities.supports_roles
        else SESSION_RECALL_DEFAULT_ROLES
    )
    match_mode = "all_terms"
    if capabilities.match_argument is not None:
        raw_match = arguments.get(capabilities.match_argument, "all_terms")
        if raw_match not in capabilities.match_modes:
            raise _SessionSearchError("invalid_arguments", "unsupported literal match mode")
        match_mode = str(raw_match)
    raw_order = arguments.get("order", capabilities.default_order)
    if raw_order not in capabilities.order_modes:
        raise _SessionSearchError("invalid_arguments", "unsupported search order")
    since = _parse_datetime(arguments.get("since"), "since", end_of_day=False)
    until = _parse_datetime(arguments.get("until"), "until", end_of_day=True)
    if since is not None and until is not None and since > until:
        raise _SessionSearchError("invalid_arguments", "since must not be after until")
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
        normalized = _normalized_search_arguments(
            arguments,
            capabilities,
            agent_id=agent_id,
            query=query,
            roles=roles,
            match_mode=match_mode,
            order=str(raw_order),
            limit=limit,
        )
        return _render_search_page(
            page,
            normalized,
            offset,
            context,
            backend_name,
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
            "action": "search",
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
) -> JsonObject:
    hits = list(page.hits)
    count = len(hits)
    while count > 0:
        items = [
            _hit_item(hit, offset + index + 1, excerpt_chars=0)
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
            _hit_item(hit, offset + index + 1, excerpt_chars=excerpt_chars)
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
        "action": "search",
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


def _hit_item(hit: RecallSearchHit, rank: int, *, excerpt_chars: int) -> JsonObject:
    start, end = _excerpt_bounds(hit, excerpt_chars)
    item: JsonObject = {
        "rank": rank,
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
        item["read_ref"] = {
            "session_id": hit.session_id,
            "start_message_id": hit.start_message_id or hit.message_id,
            "end_message_id": hit.end_message_id or hit.message_id,
        }
    else:
        item["read_ref"] = {"session_id": hit.session_id, "message_id": hit.message_id}
    if hit.sources:
        item["sources"] = list(hit.sources)
    return item


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
) -> JsonObject:
    items: list[JsonObject] = []
    index = next_index
    while index < len(source):
        message = source[index]
        if within_offset == 0:
            candidate_items = [*items, {"message": message}]
            has_more = index + 1 < len(source)
            candidate = _read_data(session_id, candidate_items, has_more=has_more)
            if has_more:
                candidate["next_cursor"] = _cursor_token(
                    "read", normalized, index + 1, 0, snapshot, context, None
                )
            if _serialized_result_bytes(candidate) <= SESSION_SEARCH_RESULT_MAX_BYTES:
                items.append({"message": message})
                index += 1
                continue
            if items:
                data = _read_data(session_id, items, has_more=True)
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
        )
    return _with_formatted_bytes(_read_data(session_id, items, has_more=False))


def _segmented_read_record(
    source: list[JsonObject],
    index: int,
    offset: int,
    normalized: JsonObject,
    snapshot: str,
    context: ToolContext,
    session_id: str,
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
        candidate = _read_data(session_id, [item], has_more=has_more)
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
            "session_search_error", "Read metadata exceeds the result safety limit."
        )
    return _with_formatted_bytes(best)


def _read_data(session_id: str, items: list[JsonObject], *, has_more: bool) -> JsonObject:
    return {
        "action": "read",
        "session_id": session_id,
        "items": items,
        "has_more": has_more,
    }


def _page_data(action: str, items: list[JsonObject], *, has_more: bool) -> JsonObject:
    return {"action": action, "items": items, "has_more": has_more}


def _session_summary(agent_id: str, summary: JsonObject) -> JsonObject:
    return {
        "agent_id": agent_id,
        "session_id": summary.get("id"),
        "created_at": summary.get("created_at"),
        "last_active_at": summary.get("last_active_at"),
        "title": summary.get("title") or summary.get("auto_title"),
    }


def _message_ref(message: Any) -> JsonObject:
    return {
        "message_id": str(message.id),
        "timestamp": str(message.timestamp),
        "role": str(message.role),
    }


def _normalized_search_arguments(
    arguments: JsonObject,
    capabilities: RecallSearchCapabilities,
    *,
    agent_id: str,
    query: str,
    roles: tuple[str, ...],
    match_mode: str,
    order: str,
    limit: int,
) -> JsonObject:
    normalized: JsonObject = {
        "action": "search",
        "agent_id": agent_id,
        "query": query,
        "limit": limit,
    }
    for key in ("session_id", "since", "until"):
        if key in arguments:
            normalized[key] = arguments[key]
    if capabilities.supports_roles:
        normalized["roles"] = list(roles)
    if capabilities.match_argument is not None:
        normalized[capabilities.match_argument] = match_mode
    if len(capabilities.order_modes) > 1:
        normalized["order"] = order
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


def _required_action(arguments: JsonObject) -> str:
    action = arguments.get("action")
    if not isinstance(action, str) or action not in SESSION_SEARCH_ACTIONS:
        raise _SessionSearchError(
            "invalid_arguments", "action must be list, overview, search, or read"
        )
    return action


def _validate_action_fields(
    action: str,
    arguments: JsonObject,
    capabilities: RecallSearchCapabilities,
) -> None:
    common = {"action"}
    allowed: dict[str, set[str]] = {
        "list": common | {"agent_id", "limit"},
        "overview": common | {"agent_id", "session_id"},
        "read": common
        | {
            "agent_id",
            "session_id",
            "message_id",
            "start_message_id",
            "end_message_id",
            "before",
            "after",
        },
        "search": common | {"query", "agent_id", "session_id", "since", "until", "limit", "order"},
    }
    if capabilities.supports_roles:
        allowed["search"].add("roles")
    if capabilities.match_argument is not None:
        allowed["search"].add(capabilities.match_argument)
    unsupported = sorted(set(arguments) - allowed[action])
    if unsupported:
        raise _SessionSearchError(
            "invalid_arguments", f"Unsupported arguments for {action}: {', '.join(unsupported)}"
        )


def _parse_cursor(
    arguments: JsonObject,
    context: ToolContext,
    backend_name: str,
) -> _Cursor | None:
    raw = arguments.get("cursor")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise _SessionSearchError("invalid_arguments", "cursor must be a non-blank string")
    if set(arguments) != {"action", "cursor"}:
        raise _SessionSearchError(
            "invalid_arguments", "A cursor continuation accepts only action and cursor."
        )
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
        raise _SessionSearchError("invalid_cursor", "Session search cursor is invalid.")
    action = payload.get("action")
    if action != arguments.get("action") or action not in SESSION_SEARCH_ACTIONS:
        raise _SessionSearchError("invalid_cursor", "Session search cursor is invalid.")
    if payload.get("project_id") != context.project_id:
        raise _SessionSearchError("invalid_cursor", "Session search cursor is invalid.")
    cursor_backend = payload.get("backend")
    if action == "search" and cursor_backend != backend_name:
        raise _SessionSearchError(
            "stale_cursor", "Recall backend changed; repeat the original search."
        )
    values = payload.get("arguments")
    if not isinstance(values, dict):
        raise _SessionSearchError("invalid_cursor", "Session search cursor is invalid.")
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
        raise _SessionSearchError("invalid_cursor", "Session search cursor is invalid.") from error


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


def _roles(value: Any) -> tuple[str, ...]:
    if value is None:
        return SESSION_RECALL_DEFAULT_ROLES
    if not isinstance(value, list) or not value:
        raise _SessionSearchError("invalid_arguments", "roles must be a non-empty array")
    roles: list[str] = []
    for role in value:
        if not isinstance(role, str) or role not in SESSION_RECALL_SUPPORTED_ROLES:
            raise _SessionSearchError("invalid_arguments", "roles contains an unsupported role")
        if role not in roles:
            roles.append(role)
    return tuple(roles)


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


def _display_summary(arguments: JsonObject) -> str:
    action = arguments.get("action")
    if not isinstance(action, str):
        return ""
    session_id = arguments.get("session_id")
    return f"{action} {session_id}".strip() if session_id else action


__all__ = [
    "SESSION_SEARCH_ACTIONS",
    "SESSION_SEARCH_CURSOR_VERSION",
    "SESSION_SEARCH_DEFAULT_LIMIT",
    "SESSION_SEARCH_MAX_LIMIT",
    "SESSION_SEARCH_RESULT_MAX_BYTES",
    "SESSION_SEARCH_TOOL_DESCRIPTION",
    "SESSION_SEARCH_TOOL_NAME",
    "SESSION_SEARCH_TOOL_PARAMETERS",
    "build_session_search_description",
    "build_session_search_parameters",
    "make_session_search_handler",
    "register_session_search_tool",
    "session_search_handler",
]
