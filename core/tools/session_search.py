"""Conversation search with bounded canonical context."""

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
)
from core.recall.sqlite_fts import SqliteFtsRecallBackend
from core.sessions import (
    ChatSessionManager,
)
from core.tools._session_recall_results import (
    _REFLECTION_RUN_KINDS,
    _USER_FACING_RUN_KINDS,
    _is_subagent_session,
    _render_search_page,
    _search_context_for_hits,
    _serialized_result_bytes,
    _session_run_kinds,
    _SessionSearchError,
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
_SEARCH_INCLUDE_SUBAGENTS_DESCRIPTION = (
    "Also search work delegated to Sub-Agents. Omit to exclude it."
)
_DESCRIPTION_SUFFIX = (
    "Returns up to 10 matching excerpts with nearby User/Assistant context; "
    "Conversation summaries are labeled. Searches conversation content, not Tool Results or "
    "reasoning. Narrow with period or a returned session_id when more detail is needed. "
    "The current Session is unavailable."
)
SESSION_SEARCH_TOOL_DESCRIPTION = (
    f"{SqliteFtsRecallBackend.search_capabilities().tool_summary} {_DESCRIPTION_SUFFIX}"
)


def build_session_search_parameters(recall_backend: Any | None = None) -> JsonObject:
    query_description = SqliteFtsRecallBackend.search_capabilities().query_description
    if recall_backend is not None:
        capabilities = _search_capabilities(recall_backend)
        query_description = capabilities.query_description or (capabilities.guidance)
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
                    "Past Session to restrict query matching. Omit to search "
                    "across Sessions. The current Session is unavailable."
                ),
            },
            "include_subagents": {
                "type": "boolean",
                "description": _SEARCH_INCLUDE_SUBAGENTS_DESCRIPTION,
            },
        },
        "required": ["query"],
    }


SESSION_SEARCH_TOOL_PARAMETERS = build_session_search_parameters()


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
    resolved_sessions = sessions or _backend_sessions(recall_backend)
    resolved_name = _backend_name(recall_backend)
    try:
        if not isinstance(arguments, dict):
            raise _SessionSearchError("invalid_arguments", "arguments must be an object")
        _validate_session_search_fields(arguments)
        capabilities = _search_capabilities(recall_backend)
        data = await _search_sessions(
            context, arguments, recall_backend, capabilities, resolved_sessions
        )
        result = tool_success(data)
        _LOGGER.info(
            "session_search backend=%s count=%s has_more=%s formatted_bytes=%s duration_ms=%s",
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


def register_session_search_tool(
    registry: ToolRegistry,
    recall_backend: Any,
    sessions: ChatSessionManager | None = None,
) -> None:
    if isinstance(recall_backend, ChatSessionManager):
        sessions = recall_backend
        recall_backend = CanonicalSessionRecallBackend(recall_backend)
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
    targets, session_contexts = await run_tool_worker(
        _search_context_for_hits,
        list(page.hits),
        agent_id=agent_id,
        project_id=context.project_id,
        sessions=sessions,
        include_subagents=arguments.get("include_subagents") is True,
    )
    data = _render_search_page(page, targets, session_contexts, project_id=context.project_id)
    return data


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
    _required_string(arguments, "query")
    for key in ("agent_id", "session_id"):
        if key in arguments:
            _required_string(arguments, key)
    if "period" in arguments:
        if arguments["period"] is None:
            raise _SessionSearchError(
                "invalid_arguments", "period must be an ISO-8601 start/end interval"
            )
        _parse_period(arguments["period"])
    if "include_subagents" in arguments and not isinstance(arguments["include_subagents"], bool):
        raise _SessionSearchError("invalid_arguments", "include_subagents must be a boolean")


def _agent_id(arguments: JsonObject, context: ToolContext) -> str:
    raw = arguments.get("agent_id")
    return raw.strip() if isinstance(raw, str) and raw.strip() else context.agent_id


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


__all__ = [
    "SESSION_SEARCH_DEFAULT_LIMIT",
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
