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
from core.tools.contracts import _load_json_value
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
    "Include delegated Sessions owned by the selected Agent. To search another Agent, "
    "set agent_id too. Omit to exclude delegated Sessions."
)
_DESCRIPTION_SUFFIX = (
    "Searches User and Assistant text and labeled conversation summaries in past Sessions. "
    "Returns matching excerpts with selected conversation context, not full transcripts. "
    "Tool Results and reasoning are not searched."
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
                    "Time range as ISO-8601 start/end, for example 2026-07-01/2026-07-31. "
                    "Both dates are included; either endpoint may be empty. Dates and "
                    "timestamps without an offset use UTC. Omit for all dates. "
                    "Filters matches; selected context may fall outside the range."
                ),
            },
            "agent_id": {
                "type": "string",
                "minLength": 1,
                "description": "Id of the Agent whose Sessions to search. Use the named Agent "
                "when one is specified. Omit for the current Agent.",
            },
            "session_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Session id from a result or the user, to search within that past "
                    "conversation. "
                    "Omit to search across Sessions. The current Session cannot be searched."
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
    return f"Search past conversations. {_DESCRIPTION_SUFFIX} {capabilities.guidance}"


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
        arguments = _normalize_search_arguments(arguments)
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
        return tool_failure(error.code, _backend_error_message(error))
    except _SessionSearchError as error:
        return tool_failure(error.code, str(error))
    except Exception:
        _LOGGER.error("session_search failed unexpectedly", exc_info=True)
        return tool_failure(
            "session_search_error",
            "Session search failed to read saved conversations. This is not a no-match result. "
            "Use another available way to inspect the saved conversations, or report the failure.",
        )


def _backend_error_message(error: RecallSearchError) -> str:
    if error.code == "semantic_unavailable":
        return (
            "Semantic search is unavailable. Changing query terms will not restore it. "
            "Use another available way to read saved conversations, or ask for the search "
            "configuration or service to be checked."
        )
    if error.code == "hybrid_unavailable":
        return (
            "Both keyword and semantic search are unavailable. Use another available way "
            "to read saved conversations, or report that search is unavailable."
        )
    if error.code == "stale_cursor":
        return "Saved conversations changed during search. Repeat the search to use current data."
    return (
        "Session search failed. Use another available way to read saved "
        "conversations, or report the failure."
    )


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
        handler_validates_arguments=True,
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
            "session_id refers to the current conversation, which session_search excludes. "
            "Use the Messages already in your context. To find past conversations, omit "
            "session_id or use a past Session id.",
        )
    roles = SESSION_RECALL_DEFAULT_ROLES
    match_mode = "all_terms"
    raw_order = capabilities.default_order
    since, until = _parse_period(arguments.get("period"))
    if sessions is None:
        raise _SessionSearchError(
            "session_search_unavailable",
            "Saved Session data is unavailable. Changing the query will not restore access; "
            "report this limitation if no other way to read the saved conversations is available.",
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
        summaries={str(summary["id"]): summary for summary in visible_summaries},
    )
    data = _render_search_page(
        page, targets, session_contexts, project_id=context.project_id, agent_id=agent_id
    )
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
            "invalid_backend_result",
            "Session search returned an unusable result. Report the search "
            "failure; do not treat it as an empty search.",
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
        "invalid_backend",
        "Session search is not correctly configured. Report the "
        "configuration problem; changing the query will not fix it.",
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


def _normalize_search_arguments(arguments: Any) -> JsonObject:
    """Recover clear search intent without dropping meaningful instructions."""
    if isinstance(arguments, str):
        try:
            arguments = _load_json_value(arguments)
        except ValueError as error:
            raise _SessionSearchError(
                "invalid_arguments", "Provide search arguments as an object with query text."
            ) from error
    if not isinstance(arguments, dict):
        raise _SessionSearchError(
            "invalid_arguments", "Provide search arguments as an object with query text."
        )
    values = dict(arguments)
    entries: list[tuple[str, Any]] = []
    for wrapper in ("request", "search"):
        if wrapper in values:
            entries.extend(_normalize_search_arguments(values.pop(wrapper)).items())
    entries.extend(values.items())
    fields = ("query", "period", "agent_id", "session_id", "include_subagents", "since", "until")
    spellings = {key.replace("_", ""): key for key in fields}
    spellings.update(
        q="query", qurey="query", searchquery="query", agent="agent_id", session="session_id"
    )
    normalized: JsonObject = {}
    for key, value in entries:
        spelling = str(key).strip().casefold().replace("_", "").replace("-", "").replace(" ", "")
        if spelling in {"action", "operation"}:
            if isinstance(value, str) and value.strip().casefold() == "search":
                continue
            raise _SessionSearchError(
                "invalid_arguments",
                "This Tool searches conversation text. Use query for search; use another "
                "available way to list Sessions or read a full transcript.",
            )
        field = spellings.get(spelling)
        if field is None:
            field = str(key)
        if field == "include_subagents":
            if isinstance(value, str):
                value = value.strip().casefold()
            if value in (True, "true", "yes", "1"):
                value = True
            elif value in (False, "false", "no", "0"):
                value = False
        if (
            field in {"query", "agent_id", "session_id"}
            and isinstance(value, int)
            and not isinstance(value, bool)
        ):
            value = str(value)
        if field in {"query", "agent_id", "session_id"} and isinstance(value, str):
            value = value.strip()
        if field in normalized and normalized[field] != value:
            raise _SessionSearchError(
                "invalid_arguments", f"Conflicting values for {field}; provide one intended value."
            )
        normalized[field] = value
    # Empty known selections have an owner-defined omission meaning, but only
    # after every alias has been compared. Unknown effects remain present.
    for field in fields:
        if field != "query" and field in normalized and normalized[field] in (None, ""):
            del normalized[field]
    if "since" in normalized or "until" in normalized:
        start, end = normalized.pop("since", ""), normalized.pop("until", "")
        if not isinstance(start, str) or not isinstance(end, str):
            raise _SessionSearchError(
                "invalid_arguments", "Use ISO-8601 dates for the search period."
            )
        interval = f"{start}/{end}"
        if "period" in normalized and _parse_period(normalized["period"]) != _parse_period(
            interval
        ):
            raise _SessionSearchError(
                "invalid_arguments",
                "Provide one time range using period; remove the competing since/until range.",
            )
        normalized["period"] = interval
    return normalized


def _validate_session_search_fields(arguments: JsonObject) -> None:
    allowed = {"query", "period", "agent_id", "session_id", "include_subagents"}
    unsupported = sorted(set(arguments) - allowed)
    if unsupported:
        raise _SessionSearchError(
            "invalid_arguments",
            f"Unsupported arguments: {', '.join(unsupported)}. Use query with optional period, "
            "agent_id, session_id and include_subagents. This Tool searches text; it cannot "
            "list Sessions or read a full transcript.",
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
        raise _SessionSearchError(
            "invalid_arguments",
            f"{key} must contain text."
            + (
                " Provide words to search for; this Tool has no listing mode."
                if key == "query"
                else f" Omit {key} when no specific target is needed."
            ),
        )
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
            "invalid_arguments",
            "period must use start/end, such as 2026-07-01/2026-07-31. Omit "
            "period to search all dates.",
        )
    start_raw, end_raw = raw.split("/", 1)
    if not start_raw and not end_raw:
        raise _SessionSearchError("invalid_arguments", "period must contain at least one endpoint")
    since = _parse_datetime(start_raw or None, "period start", end_of_day=False)
    until = _parse_datetime(end_raw or None, "period end", end_of_day=True)
    if since is not None and until is not None and since > until:
        since = _parse_datetime(end_raw, "period start", end_of_day=False)
        until = _parse_datetime(start_raw, "period end", end_of_day=True)
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
