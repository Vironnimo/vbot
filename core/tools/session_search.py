"""Agent-facing Session discovery and lossless canonical Message retrieval."""

from __future__ import annotations

import inspect
import time
from dataclasses import replace
from datetime import UTC, datetime
from datetime import time as datetime_time
from typing import Any

from core.recall import (
    RECALL_BACKEND_CANONICAL_SCAN,
    CanonicalSessionRecallBackend,
    RecallSearchCapabilities,
    RecallSearchError,
    RecallSearchPage,
    RecallSearchRequest,
)
from core.recall.canonical import (
    SESSION_RECALL_DEFAULT_ROLES,
    SESSION_RECALL_LITERAL_SEARCH_GUIDANCE,
    SESSION_RECALL_LITERAL_TOOL_SUMMARY,
)
from core.sessions import (
    ChatSessionError,
    ChatSessionManager,
    SessionAddress,
)
from core.tools._session_recall_results import (
    _REFLECTION_RUN_KINDS,
    _USER_FACING_RUN_KINDS,
    _is_subagent_session,
    _project_read_items,
    _render_list_page,
    _render_read_selection,
    _render_search_page,
    _search_context_for_hits,
    _serialized_result_bytes,
    _session_details,
    _session_run_kinds,
    _session_summary_items,
    _SessionSearchError,
    _user_anchor_index,
)
from core.tools._session_recall_results import (
    SESSION_SEARCH_DEFAULT_LIMIT as SESSION_SEARCH_DEFAULT_LIMIT,
)
from core.tools._session_recall_results import (
    SESSION_SEARCH_RESULT_MAX_BYTES as SESSION_SEARCH_RESULT_MAX_BYTES,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
    result_count_fact_builder,
    run_tool_worker,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.session_search")

SESSION_SEARCH_TOOL_NAME = "session_search"
SESSION_READ_TOOL_NAME = "session_read"
_SEARCH_INCLUDE_SUBAGENTS_DESCRIPTION = (
    "Also search work delegated to Sub-Agents. Defaults to false."
)
_READ_INCLUDE_SUBAGENTS_DESCRIPTION = (
    "Allow reading a Sub-Agent Session returned by session_search. Defaults to false; preserve "
    "the value supplied in read_ref."
)

_DESCRIPTION_SUFFIX = (
    "Delegated Sub-Agent work is excluded unless include_subagents is true. Omit query to list "
    "recent Sessions. Returns up to 10 excerpts with no paging; narrow with period or session_id. "
    "Use a returned read_ref with session_read when exact context matters. The current Session "
    "is unavailable."
)
SESSION_SEARCH_TOOL_DESCRIPTION = f"{SESSION_RECALL_LITERAL_TOOL_SUMMARY} {_DESCRIPTION_SUFFIX}"
SESSION_READ_TOOL_DESCRIPTION = (
    "Read exact content from a past Session returned by session_search. Pass the returned "
    "read_ref arguments unchanged. The current conversation cannot be read with this Tool."
)


def build_session_search_parameters(recall_backend: Any | None = None) -> JsonObject:
    query_description = SESSION_RECALL_LITERAL_SEARCH_GUIDANCE
    if recall_backend is not None:
        capabilities = _search_capabilities(recall_backend)
        query_description = capabilities.query_description or (
            f"{capabilities.guidance} Omit to list recent Sessions."
        )
    return {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": query_description,
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
                "description": "Agent whose Sessions to find. Omit for the current Agent.",
            },
            "session_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Past Session to restrict query matching. Requires query; omit to search "
                    "across Sessions. The current Session is unavailable."
                ),
            },
            "include_subagents": {
                "type": "boolean",
                "description": _SEARCH_INCLUDE_SUBAGENTS_DESCRIPTION,
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
                "description": "Past Session to read. The current Session is unavailable.",
            },
            "message_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Message to read. Omit for the latest block plus a User-anchor index; an "
                    "ordinary Message selects its block and a Tool Result selects that exact "
                    "Result."
                ),
            },
            "agent_id": {
                "type": "string",
                "minLength": 1,
                "description": "Agent that owns the Session. Omit for the current Agent.",
            },
            "continuation": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Continuation token returned by session_read for the same selection. Omit "
                    "for the first read; changing message_id or all_messages invalidates it."
                ),
            },
            "all_messages": {
                "type": "boolean",
                "description": (
                    "Return every conversation block in canonical order. Cannot be combined "
                    "with message_id; large Tool Results remain directly readable references."
                ),
            },
            "include_subagents": {
                "type": "boolean",
                "description": _READ_INCLUDE_SUBAGENTS_DESCRIPTION,
            },
        },
        "required": ["session_id"],
    }


SESSION_SEARCH_TOOL_PARAMETERS = build_session_search_parameters()
SESSION_READ_TOOL_PARAMETERS = build_session_read_parameters()


def build_session_search_description(recall_backend: Any) -> str:
    capabilities = _search_capabilities(recall_backend)
    if capabilities.tool_summary is not None:
        return f"{capabilities.tool_summary} {_DESCRIPTION_SUFFIX}"
    return (
        "Find persisted Sessions using backend-defined search behavior. "
        f"{_DESCRIPTION_SUFFIX} Active search behavior: {capabilities.guidance}"
    )


def make_session_search_handler(
    recall_backend: Any,
    sessions: ChatSessionManager | None = None,
):
    resolved_sessions = sessions or _backend_sessions(recall_backend)

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await session_search_handler(
            context,
            arguments,
            recall_backend,
            sessions=resolved_sessions,
        )

    return handler


async def session_search_handler(
    context: ToolContext,
    arguments: JsonObject,
    recall_backend: Any,
    *,
    sessions: ChatSessionManager | None = None,
) -> JsonObject:
    started = time.perf_counter()
    action = "list"
    resolved_sessions = sessions or _backend_sessions(recall_backend)
    resolved_name = _backend_name(recall_backend)
    try:
        if not isinstance(arguments, dict):
            raise _SessionSearchError("invalid_arguments", "arguments must be an object")
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
            data = await _list_sessions(context, effective, resolved_sessions)
        else:
            data = await _search_sessions(
                context,
                effective,
                recall_backend,
                capabilities,
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
            _serialized_result_bytes(data),
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
        _validate_session_read_fields(arguments)
        data = await _read_session(context, {"action": "read", **arguments}, sessions)
        result = tool_success(data)
        _LOGGER.info(
            "session_read count=%s has_more=%s formatted_bytes=%s duration_ms=%s",
            len(data.get("items", [])) if isinstance(data.get("items"), list) else 0,
            data.get("has_more", False),
            _serialized_result_bytes(data),
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
) -> None:
    if isinstance(recall_backend, ChatSessionManager):
        sessions = recall_backend
        recall_backend = CanonicalSessionRecallBackend(recall_backend)
    resolved_sessions = sessions or _backend_sessions(recall_backend)
    registry.register(
        SESSION_SEARCH_TOOL_NAME,
        build_session_search_description(recall_backend),
        build_session_search_parameters(recall_backend),
        make_session_search_handler(recall_backend, sessions),
        family="sessions",
        open_input_schema=True,
        result_schema={
            "type": "object",
            "required": ["items", "has_more"],
        },
        parallel_safe=True,
        display=ToolDisplay(
            parts_builder=_display_search_parts,
            fact_builder=result_count_fact_builder("items", at_least_field="has_more"),
            hidden_argument_keys=("query",),
        ),
    )
    registry.register(
        SESSION_READ_TOOL_NAME,
        SESSION_READ_TOOL_DESCRIPTION,
        SESSION_READ_TOOL_PARAMETERS,
        make_session_read_handler(resolved_sessions),
        family="sessions",
        activation="follows",
        activation_source=SESSION_SEARCH_TOOL_NAME,
        open_input_schema=True,
        result_schema={
            "type": "object",
            "required": ["session_id", "session", "items", "has_more"],
        },
        parallel_safe=True,
        display=ToolDisplay(
            parts_builder=_display_read_parts,
            fact_builder=result_count_fact_builder("items", at_least_field="has_more"),
            hidden_argument_keys=("message_id",),
        ),
    )


async def _list_sessions(
    context: ToolContext,
    arguments: JsonObject,
    sessions: ChatSessionManager,
) -> JsonObject:
    agent_id = _agent_id(arguments, context)
    since, until = _parse_period(arguments.get("period"))
    summaries = await run_tool_worker(
        sessions.list_recall_summaries,
        agent_id,
        context.project_id,
        include_subagents=arguments.get("include_subagents") is True,
        excluded_session_id=context.session_id if agent_id == context.agent_id else None,
        since=since,
        until=until,
        limit=SESSION_SEARCH_DEFAULT_LIMIT + 1,
    )
    selected_summaries = summaries[:SESSION_SEARCH_DEFAULT_LIMIT]
    page = await run_tool_worker(
        _session_summary_items,
        sessions,
        agent_id,
        context.project_id,
        selected_summaries,
    )
    return _render_list_page(
        page,
        total_count=len(summaries),
    )


def _visible_session_summaries(
    summaries: list[JsonObject],
    *,
    include_subagents: bool,
) -> list[JsonObject]:
    return [
        summary
        for summary in summaries
        if _session_is_recall_visible(summary, include_subagents=include_subagents)
    ]


def _session_is_recall_visible(metadata: JsonObject, *, include_subagents: bool) -> bool:
    run_kinds = _session_run_kinds(metadata)
    if run_kinds is not None and _REFLECTION_RUN_KINDS.intersection(run_kinds):
        return False
    if _session_is_subagent(metadata, run_kinds):
        return include_subagents
    if run_kinds is None:
        return True
    return bool(_USER_FACING_RUN_KINDS.intersection(run_kinds))


def _session_is_subagent(
    metadata: JsonObject,
    run_kinds: list[str] | None = None,
) -> bool:
    resolved_run_kinds = _session_run_kinds(metadata) if run_kinds is None else run_kinds
    return _is_subagent_session(metadata, resolved_run_kinds) is True


async def _read_session(
    context: ToolContext,
    arguments: JsonObject,
    sessions: ChatSessionManager,
) -> JsonObject:
    agent_id = _agent_id(arguments, context)
    session_id = _required_string(arguments, "session_id")
    if agent_id == context.agent_id and session_id == context.session_id:
        raise _SessionSearchError(
            "current_session_unavailable",
            "Current Session is unavailable through session_read; use the conversation context "
            "or history instead.",
        )
    message_id = _optional_string(arguments.get("message_id"))
    all_messages = arguments.get("all_messages") is True
    continuation = _optional_string(arguments.get("continuation"))
    address = SessionAddress(
        project_id=context.project_id, agent_id=agent_id, session_id=session_id
    )
    try:
        session = sessions.get(address)
    except ChatSessionError as error:
        raise _SessionSearchError(
            "session_not_found", f"Session not found: {session_id}"
        ) from error
    metadata = await run_tool_worker(sessions.get_metadata, address)
    include_subagents = arguments.get("include_subagents") is True
    if not _session_is_recall_visible(metadata, include_subagents=include_subagents):
        raise _SessionSearchError("session_not_found", f"Session not found: {session_id}")
    is_subagent_session = _session_is_subagent(metadata)
    messages = await run_tool_worker(session.load)
    indices = {str(message.id): index for index, message in enumerate(messages)}
    if message_id is not None and message_id not in indices:
        raise _SessionSearchError("message_not_found", f"Message not found: {message_id}")
    if all_messages:
        first, last, exact_tool_result = 0, len(messages) - 1, False
        selection_kind = "all_messages"
        selection_key = f"{agent_id}\0{session_id}\0all_messages"
    else:
        first, last, exact_tool_result = _read_selection(messages, indices, message_id)
        selection_kind = (
            "tool_result"
            if exact_tool_result
            else "conversation_block"
            if message_id is not None
            else "latest_block"
        )
        selection_key = f"{agent_id}\0{session_id}\0message:{message_id or 'latest'}"
    source = _project_read_items(
        messages,
        first,
        last,
        exact_tool_result=exact_tool_result,
        agent_id=agent_id,
        session_id=session_id,
        current_agent_id=context.agent_id,
        include_subagents=is_subagent_session,
    )
    user_anchors = _user_anchor_index(messages) if message_id is None and not all_messages else None
    selection_details: JsonObject = {
        "kind": selection_kind,
        "first_message_index": first if last >= first else None,
        "last_message_index": last if last >= first else None,
        "message_count": max(last - first + 1, 0),
    }
    session_details = _session_details(agent_id, session_id, metadata, messages)
    return _render_read_selection(
        source,
        continuation,
        session_id,
        session_details,
        selection_key=selection_key,
        selection_details=selection_details,
        user_anchors=user_anchors,
    )


def _read_selection(
    messages: list[Any],
    indices: dict[str, int],
    message_id: str | None,
) -> tuple[int, int, bool]:
    if not messages:
        return 0, -1, False
    anchor = indices[message_id] if message_id is not None else len(messages) - 1
    if str(messages[anchor].role) == "tool":
        return anchor, anchor, True
    first, last = _conversation_bounds(messages, anchor)
    return first, last, False


def _conversation_bounds(messages: list[Any], anchor: int) -> tuple[int, int]:
    first = anchor
    while first > 0 and str(messages[first].role) != "user":
        first -= 1
    last = anchor
    while last + 1 < len(messages) and str(messages[last + 1].role) != "user":
        last += 1
    return first, last


async def _search_sessions(
    context: ToolContext,
    arguments: JsonObject,
    recall_backend: Any,
    capabilities: RecallSearchCapabilities,
    sessions: ChatSessionManager | None,
) -> JsonObject:
    query = _required_string(arguments, "query")
    agent_id = _agent_id(arguments, context)
    session_id = _optional_string(arguments.get("session_id"))
    if agent_id == context.agent_id and session_id == context.session_id:
        raise _SessionSearchError(
            "current_session_unavailable",
            "Current Session is unavailable through session_search; use the conversation "
            "context or history instead.",
        )
    roles = SESSION_RECALL_DEFAULT_ROLES
    match_mode = "all_terms"
    raw_order = capabilities.default_order
    since, until = _parse_period(arguments.get("period"))
    if sessions is None:
        raise _SessionSearchError(
            "session_search_unavailable", "Canonical Session storage is unavailable."
        )
    summaries = await run_tool_worker(
        sessions.list_summaries,
        agent_id,
        context.project_id,
    )
    visible_summaries = _visible_session_summaries(
        summaries,
        include_subagents=arguments.get("include_subagents") is True,
    )
    visible_session_ids = {
        str(summary["id"]) for summary in visible_summaries if isinstance(summary.get("id"), str)
    }
    excluded_session_ids = {
        str(summary["id"])
        for summary in summaries
        if isinstance(summary.get("id"), str) and str(summary["id"]) not in visible_session_ids
    }
    if agent_id == context.agent_id:
        excluded_session_ids.add(context.session_id)
    request = RecallSearchRequest(
        agent_id=agent_id,
        project_id=context.project_id,
        session_id=session_id,
        query=query,
        since=since,
        until=until,
        roles=roles,
        match_mode=match_mode,  # type: ignore[arg-type]
        order=str(raw_order),  # type: ignore[arg-type]
        offset=0,
        limit=SESSION_SEARCH_DEFAULT_LIMIT,
        snapshot_id=None,
        excluded_session_ids=tuple(sorted(excluded_session_ids)),
    )
    page = await _call_search_page(recall_backend, request)
    page = _retain_visible_search_hits(page, visible_session_ids)
    read_refs, session_contexts = await run_tool_worker(
        _search_context_for_hits,
        list(page.hits),
        agent_id=agent_id,
        project_id=context.project_id,
        sessions=sessions,
        include_subagents=arguments.get("include_subagents") is True,
    )
    return _render_search_page(
        page,
        read_refs,
        session_contexts,
    )


async def _call_search_page(backend: Any, request: RecallSearchRequest) -> RecallSearchPage:
    method = backend.search_page
    if inspect.iscoroutinefunction(method):
        result = await method(request)
    else:
        result = await run_tool_worker(method, request)
        if inspect.isawaitable(result):
            result = await result
    if not isinstance(result, RecallSearchPage):
        raise _SessionSearchError(
            "invalid_backend_result", "Recall backend returned an invalid typed search page."
        )
    return result


def _retain_visible_search_hits(
    page: RecallSearchPage,
    visible_session_ids: set[str],
) -> RecallSearchPage:
    hits = tuple(hit for hit in page.hits if hit.session_id in visible_session_ids)
    if len(hits) == len(page.hits):
        return page
    return replace(page, hits=hits)


def _search_capabilities(backend: Any) -> RecallSearchCapabilities:
    method = getattr(backend, "search_capabilities", None)
    if callable(method):
        result = method()
        if isinstance(result, RecallSearchCapabilities):
            return result
    raise _SessionSearchError(
        "invalid_backend", "Recall backend must provide valid search capabilities."
    )


def _backend_sessions(backend: Any) -> ChatSessionManager | None:
    value = getattr(backend, "sessions", None)
    return value if isinstance(value, ChatSessionManager) else None


def _backend_name(backend: Any) -> str:
    known = {
        "CanonicalSessionRecallBackend": RECALL_BACKEND_CANONICAL_SCAN,
        "SqliteFtsRecallBackend": "sqlite_fts",
        "VectorRecallBackend": "vector",
        "HybridRecallBackend": "hybrid",
    }
    class_name = backend.__class__.__name__
    return known.get(class_name, class_name.removesuffix("RecallBackend").lower() or "backend")


def _validate_session_search_fields(arguments: JsonObject) -> None:
    allowed = {"query", "period", "agent_id", "session_id", "include_subagents"}
    unsupported = sorted(set(arguments) - allowed)
    if unsupported:
        raise _SessionSearchError(
            "invalid_arguments",
            f"Unsupported session_search arguments: {', '.join(unsupported)}",
        )
    for key in ("query", "agent_id", "session_id"):
        if key in arguments:
            _required_string(arguments, key)
    if "session_id" in arguments and "query" not in arguments:
        raise _SessionSearchError("invalid_arguments", "session_id requires query")
    if "period" in arguments:
        if arguments["period"] is None:
            raise _SessionSearchError(
                "invalid_arguments", "period must be an ISO-8601 start/end interval"
            )
        _parse_period(arguments["period"])
    if "include_subagents" in arguments and not isinstance(arguments["include_subagents"], bool):
        raise _SessionSearchError("invalid_arguments", "include_subagents must be a boolean")


def _validate_session_read_fields(arguments: JsonObject) -> None:
    allowed = {
        "session_id",
        "message_id",
        "agent_id",
        "continuation",
        "all_messages",
        "include_subagents",
    }
    unsupported = sorted(set(arguments) - allowed)
    if unsupported:
        raise _SessionSearchError(
            "invalid_arguments",
            f"Unsupported session_read arguments: {', '.join(unsupported)}",
        )
    _required_string(arguments, "session_id")
    for key in ("agent_id", "message_id", "continuation"):
        if key in arguments:
            _required_string(arguments, key)
    if "all_messages" in arguments and not isinstance(arguments["all_messages"], bool):
        raise _SessionSearchError("invalid_arguments", "all_messages must be a boolean")
    if "include_subagents" in arguments and not isinstance(arguments["include_subagents"], bool):
        raise _SessionSearchError("invalid_arguments", "include_subagents must be a boolean")
    if arguments.get("all_messages") is True and "message_id" in arguments:
        raise _SessionSearchError(
            "invalid_arguments", "all_messages cannot be combined with message_id"
        )


def _agent_id(arguments: JsonObject, context: ToolContext) -> str:
    raw = arguments.get("agent_id")
    return raw.strip() if isinstance(raw, str) and raw.strip() else context.agent_id


def _optional_non_negative_int(arguments: JsonObject, key: str) -> int | None:
    if key not in arguments:
        return None
    return _minimum_int(arguments[key], key, 0)


def _minimum_int(value: Any, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _SessionSearchError("invalid_arguments", f"{name} must be an integer")
    if value < minimum:
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise _SessionSearchError("invalid_arguments", f"{name} must be {qualifier}")
    return value


def _required_string(arguments: JsonObject, key: str) -> str:
    value = _optional_string(arguments.get(key))
    if value is None:
        raise _SessionSearchError("invalid_arguments", f"{key} must be a non-blank string")
    return value


def _optional_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


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
    return (ToolDisplayPart("find", truncate="never", tooltip="none"),)


def _display_read_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    session_id = _optional_string(arguments.get("session_id"))
    if not session_id:
        return ()
    return (ToolDisplayPart(session_id, kind="identifier", truncate="middle"),)


__all__ = [
    "SESSION_SEARCH_DEFAULT_LIMIT",
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
