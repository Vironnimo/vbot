"""Agent-facing Session discovery and lossless canonical Message retrieval."""

from __future__ import annotations

import hashlib
import inspect
import json
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from datetime import time as datetime_time
from typing import Any

from core.debug.redaction import redact_json_body
from core.recall import (
    RECALL_BACKEND_CANONICAL_SCAN,
    CanonicalSessionRecallBackend,
    RecallRequest,
    RecallSearchCapabilities,
    RecallSearchError,
    RecallSearchHit,
    RecallSearchPage,
    RecallSearchRequest,
    SupportsRecallSearch,
)
from core.recall.canonical import (
    SESSION_RECALL_DEFAULT_ROLES,
    SESSION_RECALL_LITERAL_SEARCH_GUIDANCE,
    SESSION_RECALL_LITERAL_TOOL_SUMMARY,
    compact_text,
    message_search_text,
)
from core.runs import RunKind
from core.sessions import (
    FORK_SOURCE_META_KEY,
    SESSION_RUN_KINDS_META_KEY,
    ChatSessionError,
    ChatSessionManager,
    SessionAddress,
    SessionDescriptorSource,
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
SESSION_SEARCH_DEFAULT_LIMIT = 10
SESSION_SEARCH_RESULT_MAX_BYTES = 50 * 1024
SESSION_SEARCH_EXCERPT_MAX_CHARS = 800
SESSION_READ_INLINE_TOOL_RESULT_MAX_BYTES = 4 * 1024
SESSION_READ_TOOL_RESULT_PREVIEW_CHARS = 800
SESSION_READ_USER_ANCHOR_EXCERPT_MAX_CHARS = 160
SESSION_DESCRIPTOR_EXCERPT_MAX_CHARS = 240
SESSION_DESCRIPTOR_TITLE_MAX_CHARS = 200
SESSION_DESCRIPTOR_PLATFORM_MAX_CHARS = 64
SESSION_DESCRIPTOR_AGENT_ID_MAX_CHARS = 64
SESSION_DESCRIPTOR_SESSION_ID_MAX_CHARS = 128
SESSION_DESCRIPTOR_PROJECT_ID_MAX_CHARS = 128
SESSION_DESCRIPTOR_TIMESTAMP_MAX_CHARS = 64
SUBAGENT_SESSION_METADATA_FLAG = "is_subagent_session"
SUBAGENT_PARENT_METADATA_KEY = "subagent_parent"
CHANNEL_PLATFORM_METADATA_KEY = "platform"
_VALID_RUN_KINDS = frozenset(kind.value for kind in RunKind)
_REFLECTION_RUN_KINDS = frozenset(
    {
        RunKind.REFLECTION.value,
        RunKind.MEMORY_REFLECTION.value,
        RunKind.SKILL_REFLECTION.value,
    }
)
_USER_FACING_RUN_KINDS = frozenset(
    {
        RunKind.USER.value,
        RunKind.CHANNEL.value,
        RunKind.CRON.value,
    }
)
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


class _SessionSearchError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _SearchSessionContext:
    descriptor: JsonObject


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
    if isinstance(recall_backend, SupportsRecallSearch):
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
    if len(visible_summaries) != len(summaries):
        raise _SessionSearchError(
            "session_search_unavailable",
            "The selected Recall backend cannot enforce internal Session visibility.",
        )
    return await _legacy_search(
        context,
        arguments,
        recall_backend,
        agent_id,
        query,
        roles,
        match_mode,
        str(raw_order),
        since,
        until,
        SESSION_SEARCH_DEFAULT_LIMIT,
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


async def _legacy_search(
    context: ToolContext,
    arguments: JsonObject,
    backend: Any,
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
        payload = await run_tool_worker(method, request)
        if inspect.isawaitable(payload):
            payload = await payload
    if not isinstance(payload, dict):
        raise _SessionSearchError(
            "invalid_backend_result", "Legacy Recall backend returned a non-object result."
        )
    return {
        "result_type": "backend_defined",
        "items": [{"backend_result": payload}],
        "has_more": False,
    }


def _render_search_page(
    page: RecallSearchPage,
    read_refs: list[JsonObject],
    session_contexts: dict[str, _SearchSessionContext],
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
                index + 1,
                excerpt_chars=0,
                read_ref=read_refs[index],
            )
            for index, hit in enumerate(hits[:count])
        ]
        has_more = count < len(hits) or page.has_more
        data = _search_data(
            page,
            items,
            _session_descriptors_for_hits(hits[:count], session_contexts),
            has_more=has_more,
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
        return _search_data(page, [], [], has_more=False)

    maximum = max(len(hit.text) for hit in selected)
    low = 1
    high = min(maximum, SESSION_SEARCH_EXCERPT_MAX_CHARS)
    best: JsonObject | None = None
    while low <= high:
        excerpt_chars = (low + high) // 2
        items = [
            _hit_item(
                hit,
                index + 1,
                excerpt_chars=excerpt_chars,
                read_ref=read_refs[index],
            )
            for index, hit in enumerate(selected)
        ]
        has_more = count < len(hits) or page.has_more
        candidate = _search_data(
            page,
            items,
            _session_descriptors_for_hits(selected, session_contexts),
            has_more=has_more,
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
    return best


def _search_data(
    page: RecallSearchPage,
    items: list[JsonObject],
    session_descriptors: list[JsonObject],
    *,
    has_more: bool,
) -> JsonObject:
    data: JsonObject = {
        "result_type": page.result_type,
        "items": items,
        "sessions": session_descriptors,
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


def _search_context_for_hits(
    hits: list[RecallSearchHit],
    *,
    agent_id: str,
    project_id: str | None,
    sessions: ChatSessionManager | None,
    include_subagents: bool,
) -> tuple[list[JsonObject], dict[str, _SearchSessionContext]]:
    loaded: dict[str, _SearchSessionContext] = {}
    refs: list[JsonObject] = []
    addresses = {
        hit.session_id: SessionAddress(
            project_id=project_id,
            agent_id=agent_id,
            session_id=hit.session_id,
        )
        for hit in hits
    }
    try:
        sources = {} if sessions is None else sessions.descriptor_sources(tuple(addresses.values()))
    except Exception:
        sources = {}
    for hit in hits:
        if hit.session_id not in loaded:
            source = sources.get(addresses[hit.session_id])
            loaded[hit.session_id] = _search_hit_session_context(
                agent_id,
                hit.session_id,
                source,
            )
        read_ref: JsonObject = {
            "agent_id": agent_id,
            "session_id": hit.session_id,
            "message_id": hit.message_id,
        }
        if (
            include_subagents
            and loaded[hit.session_id].descriptor.get("is_subagent_session") is True
        ):
            read_ref["include_subagents"] = True
        refs.append(read_ref)
    return refs, loaded


def _search_hit_session_context(
    agent_id: str,
    session_id: str,
    source: SessionDescriptorSource | None,
) -> _SearchSessionContext:
    if source is None:
        return _SearchSessionContext(
            descriptor=_session_descriptor(agent_id, session_id, {}, None),
        )
    return _SearchSessionContext(
        descriptor=_session_descriptor(
            agent_id,
            session_id,
            source.metadata,
            None,
            message_count=source.message_count,
            first_user_message=source.first_user_message,
        ),
    )


def _session_descriptors_for_hits(
    hits: list[RecallSearchHit],
    contexts: dict[str, _SearchSessionContext],
) -> list[JsonObject]:
    seen: set[str] = set()
    descriptors: list[JsonObject] = []
    for hit in hits:
        if hit.session_id in seen:
            continue
        seen.add(hit.session_id)
        context = contexts.get(hit.session_id)
        if context is not None:
            descriptors.append(context.descriptor)
    return descriptors


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


def _project_read_items(
    messages: list[Any],
    first: int,
    last: int,
    *,
    exact_tool_result: bool,
    agent_id: str,
    session_id: str,
    current_agent_id: str,
    include_subagents: bool,
) -> list[JsonObject]:
    items: list[JsonObject] = []
    for message_index in range(first, last + 1):
        message = messages[message_index].to_dict()
        item: JsonObject = {"message_index": message_index, "message": message}
        if not exact_tool_result and _replace_large_tool_result(message):
            read_ref: JsonObject = {
                "session_id": session_id,
                "message_id": str(message["id"]),
            }
            if agent_id != current_agent_id:
                read_ref["agent_id"] = agent_id
            if include_subagents:
                read_ref["include_subagents"] = True
            item["read_ref"] = read_ref
        items.append(item)
    return items


def _user_anchor_index(messages: list[Any]) -> list[JsonObject]:
    anchors: list[JsonObject] = []
    for message_index, message in enumerate(messages):
        if str(message.role) != "user":
            continue
        text = compact_text(message_search_text(message))
        end = min(len(text), SESSION_READ_USER_ANCHOR_EXCERPT_MAX_CHARS)
        anchors.append(
            {
                "message_index": message_index,
                "message_id": str(message.id),
                "timestamp": str(message.timestamp),
                "excerpt": {
                    "text": text[:end],
                    "trailing_truncated": end < len(text),
                },
            }
        )
    return anchors


def _replace_large_tool_result(message: JsonObject) -> bool:
    if message.get("role") != "tool" or not isinstance(message.get("content"), str):
        return False
    content = str(message["content"])
    original_bytes = len(content.encode("utf-8"))
    if original_bytes <= SESSION_READ_INLINE_TOOL_RESULT_MAX_BYTES:
        return False
    try:
        value = redact_json_body(json.loads(content))
        preview_source = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        preview_source = content
    marker = {
        "_vbot_referenced_tool_result": True,
        "original_bytes": original_bytes,
        "preview": _bounded_preview(preview_source, SESSION_READ_TOOL_RESULT_PREVIEW_CHARS),
    }
    message["content"] = json.dumps(marker, ensure_ascii=False, separators=(",", ":"))
    return True


def _bounded_preview(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = f"...[{len(value)} chars omitted]..."
    remaining = max(limit - len(marker), 0)
    head = (remaining * 2) // 3
    tail = remaining - head
    return f"{value[:head]}{marker}{value[-tail:] if tail else ''}"


def _render_read_selection(
    items: list[JsonObject],
    continuation: str | None,
    session_id: str,
    session_details: JsonObject,
    *,
    selection_key: str,
    selection_details: JsonObject,
    user_anchors: list[JsonObject] | None = None,
) -> JsonObject:
    complete = _read_data(
        session_id,
        session_details,
        items,
        has_more=False,
        selection_details=selection_details,
        user_anchors=user_anchors,
    )
    if _serialized_result_bytes(complete) <= SESSION_SEARCH_RESULT_MAX_BYTES:
        if continuation is not None:
            raise _SessionSearchError(
                "invalid_continuation",
                "Read continuation token is invalid for this selection.",
            )
        return complete

    selection: JsonObject | list[JsonObject]
    selection = items if user_anchors is None else {"user_anchors": user_anchors, "items": items}
    selection_json = json.dumps(selection, ensure_ascii=False, separators=(",", ":"))
    offset = (
        0
        if continuation is None
        else _read_continuation_offset(continuation, selection_key, selection_json)
    )
    if not selection_json:
        if continuation is not None:
            raise _SessionSearchError(
                "invalid_continuation",
                "Read continuation token is invalid for this selection.",
            )
        return complete
    if offset >= len(selection_json):
        raise _SessionSearchError(
            "invalid_continuation",
            "Read continuation token is invalid for this selection.",
        )

    low = offset + 1
    high = len(selection_json)
    best: JsonObject | None = None
    while low <= high:
        end = (low + high) // 2
        has_more = end < len(selection_json)
        candidate = _read_data(
            session_id,
            session_details,
            [
                {
                    "segment": {
                        "start": offset,
                        "end": end,
                        "complete": not has_more,
                        "selection_json": selection_json[offset:end],
                    }
                }
            ],
            has_more=has_more,
            selection_details=selection_details,
        )
        if has_more:
            candidate["next_continuation"] = _read_continuation_token(
                end, selection_key, selection_json
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
    return best


def _read_continuation_token(offset: int, selection_key: str, selection_json: str) -> str:
    digest = _read_selection_digest(selection_key, selection_json)
    return f"r1:{offset}:{digest}"


def _read_continuation_offset(token: str, selection_key: str, selection_json: str) -> int:
    parts = token.split(":")
    expected_digest = _read_selection_digest(selection_key, selection_json)
    if len(parts) != 3 or parts[0] != "r1" or parts[2] != expected_digest:
        raise _SessionSearchError(
            "invalid_continuation",
            "Read continuation token is invalid for this selection.",
        )
    try:
        offset = int(parts[1])
    except ValueError as error:
        raise _SessionSearchError(
            "invalid_continuation",
            "Read continuation token is invalid for this selection.",
        ) from error
    if offset < 0:
        raise _SessionSearchError(
            "invalid_continuation",
            "Read continuation token is invalid for this selection.",
        )
    return offset


def _read_selection_digest(selection_key: str, selection_json: str) -> str:
    value = f"{selection_key}\0{selection_json}".encode()
    return hashlib.sha256(value).hexdigest()


def _read_data(
    session_id: str,
    session_details: JsonObject,
    items: list[JsonObject],
    *,
    has_more: bool,
    selection_details: JsonObject,
    user_anchors: list[JsonObject] | None = None,
) -> JsonObject:
    data: JsonObject = {
        "session_id": session_id,
        "session": session_details,
        "selection": selection_details,
        "items": items,
        "has_more": has_more,
    }
    if user_anchors is not None:
        data["user_anchors"] = user_anchors
    return data


def _page_data(items: list[JsonObject], *, has_more: bool) -> JsonObject:
    return {"result_type": "session", "items": items, "has_more": has_more}


def _render_list_page(
    items: list[JsonObject],
    *,
    total_count: int,
) -> JsonObject:
    count = len(items)
    while count > 0:
        has_more = count < total_count
        data = _page_data(items[:count], has_more=has_more)
        if _serialized_result_bytes(data) <= SESSION_SEARCH_RESULT_MAX_BYTES:
            return data
        count -= 1
    if items:
        raise _SessionSearchError(
            "session_search_error", "Session metadata exceeds the result safety limit."
        )
    return _page_data([], has_more=False)


def _session_summary_items(
    sessions: ChatSessionManager,
    agent_id: str,
    project_id: str | None,
    summaries: list[JsonObject],
) -> list[JsonObject]:
    items: list[JsonObject] = []
    addresses = {
        str(summary.get("id") or ""): SessionAddress(
            project_id=project_id,
            agent_id=agent_id,
            session_id=str(summary.get("id") or ""),
        )
        for summary in summaries
    }
    try:
        sources = sessions.descriptor_sources(tuple(addresses.values()))
    except Exception:
        sources = {}
    for summary in summaries:
        session_id = str(summary.get("id") or "")
        message_count: int | None = None
        first_user_message: Any | None = None
        source = sources.get(addresses[session_id])
        if source is not None:
            message_count = source.message_count
            first_user_message = source.first_user_message
        items.append(
            _session_summary(
                agent_id,
                summary,
                None,
                message_count=message_count,
                first_user_message=first_user_message,
            )
        )
    return items


def _session_summary(
    agent_id: str,
    summary: JsonObject,
    messages: list[Any] | None,
    *,
    message_count: int | None = None,
    first_user_message: Any | None = None,
) -> JsonObject:
    session_id = str(summary.get("id") or "")
    item = _session_descriptor(
        agent_id,
        session_id,
        summary,
        messages,
        message_count=message_count,
        first_user_message=first_user_message,
    )
    item.update(
        {
            "created_at": summary.get("created_at"),
            "last_active_at": summary.get("last_active_at"),
        }
    )
    return item


def _session_descriptor(
    agent_id: str,
    session_id: str,
    metadata: JsonObject,
    messages: list[Any] | None,
    *,
    message_count: int | None = None,
    first_user_message: Any | None = None,
) -> JsonObject:
    if messages is not None:
        message_count = len(messages)
        first_user_message = next(
            (message for message in messages if str(message.role) == "user"),
            None,
        )
    run_kinds = _session_run_kinds(metadata)
    return {
        "agent_id": agent_id,
        "session_id": session_id,
        "title": _descriptor_text(
            metadata.get("title") or metadata.get("auto_title"),
            SESSION_DESCRIPTOR_TITLE_MAX_CHARS,
        ),
        "run_kinds": run_kinds,
        "is_subagent_session": _is_subagent_session(metadata, run_kinds),
        "subagent_parent": _session_address(metadata.get(SUBAGENT_PARENT_METADATA_KEY)),
        "platform": _descriptor_text(
            metadata.get(CHANNEL_PLATFORM_METADATA_KEY),
            SESSION_DESCRIPTOR_PLATFORM_MAX_CHARS,
        ),
        "fork_source": _fork_source(metadata.get(FORK_SOURCE_META_KEY)),
        "message_count": message_count,
        "first_user_excerpt": _user_message_excerpt(first_user_message),
    }


def _session_run_kinds(metadata: JsonObject) -> list[str] | None:
    raw = metadata.get(SESSION_RUN_KINDS_META_KEY)
    if not isinstance(raw, list) or not raw:
        return None
    if any(not isinstance(value, str) or value not in _VALID_RUN_KINDS for value in raw):
        return None
    return list(dict.fromkeys(raw))


def _is_subagent_session(metadata: JsonObject, run_kinds: list[str] | None) -> bool | None:
    if run_kinds is not None and RunKind.SUBAGENT.value in run_kinds:
        return True
    explicit = metadata.get(SUBAGENT_SESSION_METADATA_FLAG)
    if isinstance(explicit, bool):
        return explicit
    if run_kinds is not None:
        return False
    return None


def _session_address(value: Any) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    agent_id = _descriptor_identifier(value.get("agent_id"), SESSION_DESCRIPTOR_AGENT_ID_MAX_CHARS)
    session_id = _descriptor_identifier(
        value.get("session_id"), SESSION_DESCRIPTOR_SESSION_ID_MAX_CHARS
    )
    if agent_id is None or session_id is None:
        return None
    return {
        "agent_id": agent_id,
        "session_id": session_id,
        "project_id": _descriptor_identifier(
            value.get("project_id"), SESSION_DESCRIPTOR_PROJECT_ID_MAX_CHARS
        ),
    }


def _fork_source(value: Any) -> JsonObject | None:
    address = _session_address(value)
    if address is None:
        return None
    assert isinstance(value, dict)
    address["forked_at"] = _descriptor_text(
        value.get("forked_at"), SESSION_DESCRIPTOR_TIMESTAMP_MAX_CHARS
    )
    return address


def _descriptor_identifier(value: Any, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > max_chars:
        return None
    return normalized


def _descriptor_text(value: Any, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = compact_text(value)
    if not normalized:
        return None
    return normalized[:max_chars]


def _user_message_excerpt(message: Any | None) -> JsonObject | None:
    if message is None:
        return None
    text = compact_text(message_search_text(message))
    if not text:
        return None
    end = min(len(text), SESSION_DESCRIPTOR_EXCERPT_MAX_CHARS)
    return {
        "text": text[:end],
        "trailing_truncated": end < len(text),
    }


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
    details = _session_descriptor(agent_id, session_id, metadata, messages)
    details.update(
        {
            "role_counts": dict(Counter(str(message.role) for message in messages)),
            "first_message": _message_ref(messages[0]) if messages else None,
            "last_message": _message_ref(messages[-1]) if messages else None,
        }
    )
    return details


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


def _serialized_result_bytes(data: JsonObject) -> int:
    return len(
        json.dumps(tool_success(data), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
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
