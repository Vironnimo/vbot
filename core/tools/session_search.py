"""Conversation search with bounded canonical context."""

from __future__ import annotations

import calendar
import inspect
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, tzinfo
from datetime import time as datetime_time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    SESSION_SEARCH_DEFAULT_LIMIT as SESSION_SEARCH_DEFAULT_LIMIT,
)
from core.tools._session_recall_results import (
    SESSION_SEARCH_RESULT_MAX_BYTES as SESSION_SEARCH_RESULT_MAX_BYTES,
)
from core.tools._session_recall_results import (
    _render_search_page,
    _search_context_for_hits,
    _serialized_result_bytes,
    _SessionSearchError,
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
                    "Both ends are included and either may be empty; a single date or month "
                    "means all of it. Dates and times without an offset are local time. Omit "
                    "for all dates. Filters matches; selected context may fall outside the range."
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


@dataclass(frozen=True)
class _Zone:
    """The timezone that reads dates and times given without an offset."""

    tz: tzinfo
    name: str


_UTC_ZONE = _Zone(UTC, "UTC")


@dataclass(frozen=True)
class _Period:
    since: datetime | None
    until: datetime | None
    # The inclusive range searched, as the Model should read it back.
    text: str


def make_session_search_handler(
    recall_backend: Any,
    sessions: ChatSessionManager | None = None,
    timezone_name_loader: Callable[[], str] | None = None,
):
    resolved_sessions = sessions or _backend_sessions(recall_backend)

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await session_search_handler(
            context,
            arguments,
            recall_backend,
            sessions=resolved_sessions,
            timezone_name_loader=timezone_name_loader,
        )

    return handler


async def session_search_handler(
    context: ToolContext,
    arguments: JsonObject,
    recall_backend: Any,
    *,
    sessions: ChatSessionManager | None = None,
    timezone_name_loader: Callable[[], str] | None = None,
) -> JsonObject:
    """Search past Sessions.

    ``timezone_name_loader`` returns the configured IANA timezone that reads
    period dates and times without an offset; without it they read as UTC.
    """
    started = time.perf_counter()
    resolved_sessions = sessions or _backend_sessions(recall_backend)
    resolved_name = _backend_name(recall_backend)
    try:
        # The loader reads Settings; keep that file access off the Event Loop.
        zone = (
            await run_tool_worker(_local_zone, timezone_name_loader)
            if timezone_name_loader is not None
            else _UTC_ZONE
        )
        arguments = _normalize_search_arguments(arguments, zone)
        if not isinstance(arguments, dict):
            raise _SessionSearchError("invalid_arguments", "arguments must be an object")
        _validate_session_search_fields(arguments, zone)
        capabilities = _search_capabilities(recall_backend)
        data = await _search_sessions(
            context, arguments, recall_backend, capabilities, resolved_sessions, zone
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
    *,
    timezone_name_loader: Callable[[], str] | None = None,
) -> None:
    if isinstance(recall_backend, ChatSessionManager):
        sessions = recall_backend
        recall_backend = CanonicalSessionRecallBackend(recall_backend)
    registry.register(
        SESSION_SEARCH_TOOL_NAME,
        build_session_search_description(recall_backend),
        build_session_search_parameters(recall_backend),
        make_session_search_handler(recall_backend, sessions, timezone_name_loader),
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


async def _search_sessions(
    context: ToolContext,
    arguments: JsonObject,
    recall_backend: Any,
    capabilities: RecallSearchCapabilities,
    sessions: ChatSessionManager | None,
    zone: _Zone,
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
    period = _parse_period(arguments.get("period"), zone)
    since, until = (period.since, period.until) if period is not None else (None, None)
    limit, notes = _limit(arguments.get("limit"))
    if sessions is None:
        raise _SessionSearchError(
            "session_search_unavailable",
            "Saved Session data is unavailable. Changing the query will not restore access; "
            "report this limitation if no other way to read the saved conversations is available.",
        )
    include_subagents = arguments.get("include_subagents") is True
    # Backends admit only Recall-visible Sessions; the current conversation is
    # excluded explicitly because it belongs to the searching Agent.
    excluded_session_ids = (context.session_id,) if agent_id == context.agent_id else ()
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
        limit=limit,
        snapshot_id=None,
        excluded_session_ids=excluded_session_ids,
        include_subagents=include_subagents,
    )
    page = await _call_search_page(recall_backend, request)
    hits, targets, session_contexts = await run_tool_worker(
        _search_context_for_hits,
        list(page.hits),
        agent_id=agent_id,
        project_id=context.project_id,
        sessions=sessions,
        include_subagents=include_subagents,
        excluded_session_ids=excluded_session_ids,
    )
    if len(hits) != len(page.hits):
        page = replace(page, hits=tuple(hits))
    data = _render_search_page(
        page,
        targets,
        session_contexts,
        project_id=context.project_id,
        agent_id=agent_id,
        period=period.text if period is not None else None,
        limit=limit,
        notes=notes,
    )
    return data


def _limit(value: Any) -> tuple[int, list[str]]:
    """The requested hit count (validated before) capped at the Tool's maximum."""
    if value is None:
        return SESSION_SEARCH_DEFAULT_LIMIT, []
    requested = int(value)
    if requested > SESSION_SEARCH_DEFAULT_LIMIT:
        return SESSION_SEARCH_DEFAULT_LIMIT, [
            f"limit is at most {SESSION_SEARCH_DEFAULT_LIMIT}; this search returned up to "
            f"{SESSION_SEARCH_DEFAULT_LIMIT} matches."
        ]
    return requested, []


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


def _normalize_search_arguments(arguments: Any, zone: _Zone = _UTC_ZONE) -> JsonObject:
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
            entries.extend(_normalize_search_arguments(values.pop(wrapper), zone).items())
    entries.extend(values.items())
    fields = (
        "query",
        "period",
        "agent_id",
        "session_id",
        "include_subagents",
        "limit",
        "since",
        "until",
    )
    spellings = {key.replace("_", ""): key for key in fields}
    spellings.update(
        q="query",
        qurey="query",
        searchquery="query",
        agent="agent_id",
        session="session_id",
        maxresults="limit",
        topk="limit",
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
        if field == "limit" and isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
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
        if "period" in normalized and _period_bounds(normalized["period"], zone) != (
            _period_bounds(interval, zone)
        ):
            raise _SessionSearchError(
                "invalid_arguments",
                "Provide one time range using period; remove the competing since/until range.",
            )
        normalized["period"] = interval
    return normalized


_PAGING_FIELDS = frozenset({"page", "offset", "cursor", "next_cursor"})


def _validate_session_search_fields(arguments: JsonObject, zone: _Zone = _UTC_ZONE) -> None:
    allowed = {"query", "period", "agent_id", "session_id", "include_subagents", "limit"}
    unsupported = sorted(set(arguments) - allowed)
    if unsupported:
        paging = (
            " There are no result pages; narrow query, period or session_id to see other matches."
            if _PAGING_FIELDS.intersection(unsupported)
            else ""
        )
        raise _SessionSearchError(
            "invalid_arguments",
            f"Unsupported arguments: {', '.join(unsupported)}. Use query with optional period, "
            "agent_id, session_id and include_subagents. This Tool searches text; it cannot "
            f"list Sessions or read a full transcript.{paging}",
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
        _parse_period(arguments["period"], zone)
    if "include_subagents" in arguments and not isinstance(arguments["include_subagents"], bool):
        raise _SessionSearchError("invalid_arguments", "include_subagents must be a boolean")
    limit = arguments.get("limit")
    if "limit" in arguments and (
        not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
    ):
        raise _SessionSearchError(
            "invalid_arguments",
            f"limit must be a whole number from 1 to {SESSION_SEARCH_DEFAULT_LIMIT}; omit it "
            f"for up to {SESSION_SEARCH_DEFAULT_LIMIT} matches.",
        )


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


def _local_zone(timezone_name_loader: Callable[[], str] | None) -> _Zone:
    if timezone_name_loader is None:
        return _UTC_ZONE
    try:
        name = timezone_name_loader()
        return _Zone(ZoneInfo(name), name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        _LOGGER.warning("session_search could not load the configured timezone; using UTC")
        return _UTC_ZONE


_MONTH = re.compile(r"\d{4}-\d{2}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse_datetime(
    value: str, name: str, *, end_of_range: bool, zone: _Zone
) -> tuple[datetime, bool]:
    """Parse one period endpoint; also report whether it was read in local time.

    A date or month covers all of it; a value without an offset is local time.
    """
    raw = value.strip()
    try:
        if _MONTH.fullmatch(raw) or _DAY.fullmatch(raw):
            year, month = int(raw[:4]), int(raw[5:7])
            if len(raw) == 10:
                first = last = date.fromisoformat(raw)
            else:
                first = date(year, month, 1)
                last = date(year, month, calendar.monthrange(year, month)[1])
            day, boundary = (
                (last, datetime_time.max) if end_of_range else (first, datetime_time.min)
            )
            return datetime.combine(day, boundary, tzinfo=zone.tz), True
        normalized = raw[:-1] + "+00:00" if raw[-1:] in ("Z", "z") else raw
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise _SessionSearchError(
            "invalid_arguments",
            f"{name} '{raw}' is not an ISO-8601 date or time, such as 2026-07-01 or "
            "2026-07-01T09:00.",
        ) from error
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone.tz), True
    return parsed, False


def _parse_period(value: Any, zone: _Zone = _UTC_ZONE) -> _Period | None:
    """Resolve a period to UTC bounds plus the range text the result states."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _SessionSearchError(
            "invalid_arguments", "period must be an ISO-8601 start/end interval"
        )
    raw = value.strip()
    if "/" not in raw and (_MONTH.fullmatch(raw) or _DAY.fullmatch(raw)):
        raw = f"{raw}/{raw}"
    if raw.count("/") != 1:
        hint = f' For everything from {raw} on, use "{raw}/".' if "/" not in raw else ""
        raise _SessionSearchError(
            "invalid_arguments",
            "period must use start/end, such as 2026-07-01/2026-07-31. Omit "
            f"period to search all dates.{hint}",
        )
    start_raw, end_raw = (part.strip() for part in raw.split("/", 1))
    if not start_raw and not end_raw:
        raise _SessionSearchError("invalid_arguments", "period must contain at least one endpoint")
    start = _endpoint(start_raw, "period start", end_of_range=False, zone=zone)
    end = _endpoint(end_raw, "period end", end_of_range=True, zone=zone)
    if start is not None and end is not None and start[0] > end[0]:
        start = _endpoint(end_raw, "period start", end_of_range=False, zone=zone)
        end = _endpoint(start_raw, "period end", end_of_range=True, zone=zone)
    local = any(bound is not None and bound[1] for bound in (start, end))
    text = "/".join(
        bound[0].isoformat(timespec="seconds") if bound is not None else ""
        for bound in (start, end)
    )
    return _Period(
        since=start[0].astimezone(UTC) if start is not None else None,
        until=end[0].astimezone(UTC) if end is not None else None,
        text=f"{text} ({zone.name})" if local else text,
    )


def _endpoint(
    raw: str, name: str, *, end_of_range: bool, zone: _Zone
) -> tuple[datetime, bool] | None:
    if not raw:
        return None
    return _parse_datetime(raw, name, end_of_range=end_of_range, zone=zone)


def _period_bounds(value: Any, zone: _Zone) -> tuple[datetime | None, datetime | None]:
    period = _parse_period(value, zone)
    return (period.since, period.until) if period is not None else (None, None)


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
