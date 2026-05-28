"""Built-in session_search tool for finding persisted chat history."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

from core.chat.content_blocks import FileBlock, MediaBlock, TextBlock
from core.chat.errors import ChatSessionError
from core.sessions import ChatSessionManager, is_skill_context_note
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)

SESSION_SEARCH_TOOL_NAME = "session_search"
SESSION_SEARCH_TOOL_DESCRIPTION = (
    "Search persisted chat sessions by keywords, time range, role, agent, or session. "
    "Without a query, returns recent matching session summaries."
)
SESSION_SEARCH_DEFAULT_LIMIT = 20
SESSION_SEARCH_MAX_LIMIT = 100
SESSION_SEARCH_MAX_CONTEXT_MESSAGES = 2
SESSION_SEARCH_SNIPPET_CHARS = 320
SESSION_SEARCH_CONTEXT_SNIPPET_CHARS = 180

SESSION_SEARCH_DEFAULT_ROLES = (
    "user",
    "assistant",
    "tool",
    "error",
    "compaction_checkpoint",
)
SESSION_SEARCH_SUPPORTED_ROLES = (*SESSION_SEARCH_DEFAULT_ROLES, "note")
SESSION_SEARCH_MATCH_MODES = ("all_terms", "any_term", "phrase")
SESSION_SEARCH_SORT_MODES = ("newest", "oldest")

SESSION_SEARCH_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Case-insensitive keywords or phrase to search for. Omit to list sessions."
            ),
        },
        "agent_id": {
            "type": "string",
            "description": "Agent whose sessions to search (default: current agent).",
        },
        "session_id": {
            "type": "string",
            "description": "Restrict search to one session id.",
        },
        "since": {
            "type": "string",
            "description": "Inclusive UTC ISO-8601 timestamp or YYYY-MM-DD lower bound.",
        },
        "until": {
            "type": "string",
            "description": "Inclusive UTC ISO-8601 timestamp or YYYY-MM-DD upper bound.",
        },
        "roles": {
            "type": "array",
            "items": {"type": "string", "enum": list(SESSION_SEARCH_SUPPORTED_ROLES)},
            "description": (
                "Message roles to search. Defaults to visible history plus compaction checkpoints; "
                "include 'note' explicitly for kernel notes."
            ),
        },
        "match": {
            "type": "string",
            "enum": list(SESSION_SEARCH_MATCH_MODES),
            "description": "Query matching mode: all_terms (default), any_term, or phrase.",
        },
        "limit": {
            "type": "number",
            "description": "Maximum matches or session summaries to return (default 20, max 100).",
        },
        "context": {
            "type": "number",
            "description": "Messages before and after each match to include (default 0, max 2).",
        },
        "sort": {
            "type": "string",
            "enum": list(SESSION_SEARCH_SORT_MODES),
            "description": "Sort sessions by activity: newest (default) or oldest.",
        },
    },
    "additionalProperties": False,
}

_ALLOWED_ARGUMENTS = set(SESSION_SEARCH_TOOL_PARAMETERS["properties"])
_DATE_ONLY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WHITESPACE_PATTERN = re.compile(r"\s+")


@dataclass(frozen=True)
class _SearchRequest:
    agent_id: str
    session_id: str | None
    query: str | None
    since: datetime | None
    until: datetime | None
    roles: tuple[str, ...]
    match_mode: str
    limit: int
    context_messages: int
    sort: str


def make_session_search_handler(sessions: ChatSessionManager):
    """Create a session_search tool handler bound to a session manager."""

    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return session_search_handler(context, arguments, sessions)

    return handler


def session_search_handler(
    context: ToolContext,
    arguments: JsonObject,
    sessions: ChatSessionManager,
) -> JsonObject:
    """Handle a session_search tool call and return a stable vBot result envelope."""
    unknown_arguments = set(arguments) - _ALLOWED_ARGUMENTS
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

    try:
        request = _parse_search_request(context, arguments)
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))

    try:
        summaries = _candidate_session_summaries(sessions, request)
        if request.query is None:
            return tool_success(_session_summary_result(request, summaries))
        return tool_success(_message_search_result(sessions, request, summaries))
    except ChatSessionError as error:
        return tool_failure("session_search_error", str(error))


def _parse_search_request(context: ToolContext, arguments: JsonObject) -> _SearchRequest:
    agent_id = _optional_string(arguments.get("agent_id"), field_name="agent_id")
    if agent_id is None:
        agent_id = context.agent_id
    if not agent_id:
        raise ValueError("agent_id must be a non-empty string")

    query = _optional_query(arguments.get("query"))

    since = _optional_datetime(arguments.get("since"), field_name="since", end_of_day=False)
    until = _optional_datetime(arguments.get("until"), field_name="until", end_of_day=True)
    if since is not None and until is not None and since > until:
        raise ValueError("since must be earlier than or equal to until")

    return _SearchRequest(
        agent_id=agent_id,
        session_id=_optional_string(arguments.get("session_id"), field_name="session_id"),
        query=query,
        since=since,
        until=until,
        roles=_roles(arguments.get("roles")),
        match_mode=_enum_value(
            arguments.get("match"),
            field_name="match",
            supported=SESSION_SEARCH_MATCH_MODES,
            default="all_terms",
        ),
        limit=_integer_value(
            arguments.get("limit"),
            field_name="limit",
            default=SESSION_SEARCH_DEFAULT_LIMIT,
            minimum=1,
            maximum=SESSION_SEARCH_MAX_LIMIT,
        ),
        context_messages=_integer_value(
            arguments.get("context"),
            field_name="context",
            default=0,
            minimum=0,
            maximum=SESSION_SEARCH_MAX_CONTEXT_MESSAGES,
        ),
        sort=_enum_value(
            arguments.get("sort"),
            field_name="sort",
            supported=SESSION_SEARCH_SORT_MODES,
            default="newest",
        ),
    )


def _candidate_session_summaries(
    sessions: ChatSessionManager,
    request: _SearchRequest,
) -> list[JsonObject]:
    summaries = [
        summary
        for summary in sessions.list_with_metadata(request.agent_id)
        if _session_matches_request(summary, request)
    ]
    summaries.sort(
        key=lambda summary: _timestamp_sort_key(summary.get("last_active_at")),
        reverse=request.sort == "newest",
    )
    return summaries


def _session_matches_request(summary: JsonObject, request: _SearchRequest) -> bool:
    session_id = summary.get("id")
    if request.session_id is not None and session_id != request.session_id:
        return False

    created_at = _parse_persisted_timestamp(summary.get("created_at"))
    last_active_at = _parse_persisted_timestamp(summary.get("last_active_at"))
    if request.since is not None and last_active_at is not None and last_active_at < request.since:
        return False
    return not (request.until is not None and created_at is not None and created_at > request.until)


def _session_summary_result(request: _SearchRequest, summaries: list[JsonObject]) -> JsonObject:
    limited_summaries = summaries[: request.limit]
    sessions_payload = [
        _session_payload(request.agent_id, summary) for summary in limited_summaries
    ]
    truncated = len(summaries) > request.limit
    return {
        "content": _render_session_summaries(request, sessions_payload, truncated=truncated),
        "sessions": sessions_payload,
        "truncated": truncated,
        "total_candidates": len(summaries),
        "request": _request_payload(request),
    }


def _message_search_result(
    sessions: ChatSessionManager,
    request: _SearchRequest,
    summaries: list[JsonObject],
) -> JsonObject:
    matches: list[JsonObject] = []
    searched_sessions = 0
    truncated = False

    for summary in summaries:
        session_id = str(summary["id"])
        messages = sessions.get(request.agent_id, session_id).load()
        searched_sessions += 1
        for message_index, message in enumerate(messages):
            if not _message_matches_request(message, request):
                continue
            text = _message_search_text(message)
            if not _text_matches_query(text, request):
                continue
            if len(matches) >= request.limit:
                truncated = True
                break
            matches.append(
                _message_match_payload(
                    request,
                    summary,
                    messages,
                    message_index,
                    text,
                )
            )
        if truncated:
            break

    return {
        "content": _render_message_matches(request, matches, truncated=truncated),
        "matches": matches,
        "truncated": truncated,
        "searched_sessions": searched_sessions,
        "total_candidate_sessions": len(summaries),
        "request": _request_payload(request),
    }


def _message_matches_request(message: Any, request: _SearchRequest) -> bool:
    if message.role not in request.roles:
        return False
    if is_skill_context_note(message):
        return False

    timestamp = _parse_persisted_timestamp(message.timestamp)
    if request.since is not None and timestamp is not None and timestamp < request.since:
        return False
    return not (request.until is not None and timestamp is not None and timestamp > request.until)


def _message_match_payload(
    request: _SearchRequest,
    summary: JsonObject,
    messages: list[Any],
    message_index: int,
    text: str,
) -> JsonObject:
    message = messages[message_index]
    payload: JsonObject = {
        "agent_id": request.agent_id,
        "session_id": summary["id"],
        "message_id": message.id,
        "timestamp": message.timestamp,
        "role": message.role,
        "snippet": _snippet(text, request, SESSION_SEARCH_SNIPPET_CHARS),
    }
    if request.context_messages > 0:
        payload["context"] = _context_payload(messages, message_index, request.context_messages)
    return payload


def _context_payload(messages: list[Any], message_index: int, context_messages: int) -> JsonObject:
    return {
        "before": _neighbor_context(messages, message_index, -1, context_messages),
        "after": _neighbor_context(messages, message_index, 1, context_messages),
    }


def _neighbor_context(
    messages: list[Any],
    message_index: int,
    direction: int,
    context_messages: int,
) -> list[JsonObject]:
    neighbors: list[JsonObject] = []
    index = message_index + direction
    while 0 <= index < len(messages) and len(neighbors) < context_messages:
        message = messages[index]
        if _is_context_message(message):
            neighbors.append(
                {
                    "message_id": message.id,
                    "timestamp": message.timestamp,
                    "role": message.role,
                    "snippet": _trim_text(
                        _compact_text(_message_search_text(message)),
                        SESSION_SEARCH_CONTEXT_SNIPPET_CHARS,
                    ),
                }
            )
        index += direction
    if direction < 0:
        neighbors.reverse()
    return neighbors


def _is_context_message(message: Any) -> bool:
    return message.role in SESSION_SEARCH_DEFAULT_ROLES and not is_skill_context_note(message)


def _message_search_text(message: Any) -> str:
    parts = [
        _content_to_text(message.content),
        message.reasoning or "",
        message.name or "",
        message.error_kind or "",
        _tool_calls_text(message.tool_calls),
    ]
    return "\n".join(part for part in parts if part)


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_content_block_to_text(block) for block in content)
    return ""


def _content_block_to_text(block: Any) -> str:
    if isinstance(block, TextBlock):
        return block.text
    if isinstance(block, (FileBlock, MediaBlock)):
        return f"{block.filename} {block.media_type}"
    return ""


def _tool_calls_text(tool_calls: Any) -> str:
    if not tool_calls:
        return ""
    parts: list[str] = []
    for tool_call in tool_calls:
        try:
            arguments = json.dumps(tool_call.arguments, ensure_ascii=False, sort_keys=True)
        except TypeError:
            arguments = str(tool_call.arguments)
        parts.append(f"{tool_call.name} {arguments}")
    return "\n".join(parts)


def _text_matches_query(text: str, request: _SearchRequest) -> bool:
    if request.query is None:
        return True
    haystack = _compact_text(text).casefold()
    if not haystack:
        return False
    if request.match_mode == "phrase":
        return _compact_text(request.query).casefold() in haystack

    terms = _query_terms(request.query)
    if request.match_mode == "any_term":
        return any(term in haystack for term in terms)
    return all(term in haystack for term in terms)


def _snippet(text: str, request: _SearchRequest, limit: int) -> str:
    compact = _compact_text(text)
    if not compact:
        return ""

    index = _first_match_index(compact, request)
    if index < 0:
        return _trim_text(compact, limit)

    start = max(index - limit // 3, 0)
    end = min(start + limit, len(compact))
    start = max(end - limit, 0)
    snippet = compact[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(compact):
        snippet += "..."
    return snippet


def _first_match_index(text: str, request: _SearchRequest) -> int:
    if request.query is None:
        return -1
    haystack = text.casefold()
    if request.match_mode == "phrase":
        return haystack.find(_compact_text(request.query).casefold())

    indexes = [index for term in _query_terms(request.query) if (index := haystack.find(term)) >= 0]
    return min(indexes) if indexes else -1


def _query_terms(query: str) -> list[str]:
    return [term.casefold() for term in _compact_text(query).split(" ") if term]


def _session_payload(agent_id: str, summary: JsonObject) -> JsonObject:
    metadata = {
        key: value
        for key, value in summary.items()
        if key not in {"id", "created_at", "last_active_at"}
    }
    return {
        "agent_id": agent_id,
        "session_id": summary["id"],
        "created_at": summary.get("created_at"),
        "last_active_at": summary.get("last_active_at"),
        "metadata": metadata,
    }


def _request_payload(request: _SearchRequest) -> JsonObject:
    payload: JsonObject = {
        "agent_id": request.agent_id,
        "session_id": request.session_id,
        "query": request.query,
        "since": request.since.isoformat() if request.since is not None else None,
        "until": request.until.isoformat() if request.until is not None else None,
        "roles": list(request.roles),
        "match": request.match_mode,
        "limit": request.limit,
        "context": request.context_messages,
        "sort": request.sort,
    }
    return payload


def _render_session_summaries(
    request: _SearchRequest,
    sessions_payload: list[JsonObject],
    *,
    truncated: bool,
) -> str:
    if not sessions_payload:
        return "No sessions matched the supplied filters."

    lines = [f"Found {len(sessions_payload)} session(s) for agent {request.agent_id}."]
    for index, session in enumerate(sessions_payload, start=1):
        lines.append(
            f"[{index}] {session['session_id']} last_active={session.get('last_active_at')} "
            f"created={session.get('created_at')}{_metadata_suffix(session)}"
        )
    if truncated:
        lines.append(f"[Results limited to {request.limit} sessions.]")
    return "\n".join(lines)


def _render_message_matches(
    request: _SearchRequest,
    matches: list[JsonObject],
    *,
    truncated: bool,
) -> str:
    if not matches:
        return f"No session messages matched query: {request.query}"

    lines = [f"Found {len(matches)} match(es) for query: {request.query}"]
    for index, match in enumerate(matches, start=1):
        lines.append(
            f"[{index}] {match['session_id']} {match['timestamp']} "
            f"{match['role']} {match['message_id']}"
        )
        lines.append(str(match["snippet"]))
        context = match.get("context")
        if isinstance(context, dict):
            for side in ("before", "after"):
                for item in context.get(side, []):
                    lines.append(f"  {side}: {item['timestamp']} {item['role']} {item['snippet']}")
    if truncated:
        lines.append(f"[Results limited to {request.limit} matches.]")
    return "\n".join(lines)


def _metadata_suffix(session: JsonObject) -> str:
    metadata = session.get("metadata")
    if not isinstance(metadata, dict) or not metadata:
        return ""
    rendered = ", ".join(f"{key}={value}" for key, value in sorted(metadata.items()))
    return f" metadata: {rendered}"


def _optional_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_query(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("query must be a string")
    return value.strip() or None


def _optional_datetime(value: object, *, field_name: str, end_of_day: bool) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    try:
        return _parse_datetime(value.strip(), end_of_day=end_of_day)
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp or YYYY-MM-DD") from error


def _roles(value: object) -> tuple[str, ...]:
    if value is None:
        return SESSION_SEARCH_DEFAULT_ROLES
    if not isinstance(value, list) or not value:
        raise ValueError("roles must be a non-empty list of message roles")

    roles: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("roles must be a non-empty list of message roles")
        role = item.strip()
        if role not in SESSION_SEARCH_SUPPORTED_ROLES:
            supported = ", ".join(SESSION_SEARCH_SUPPORTED_ROLES)
            raise ValueError(f"roles must contain only supported roles: {supported}")
        if role not in roles:
            roles.append(role)
    if not roles:
        raise ValueError("roles must be a non-empty list of message roles")
    return tuple(roles)


def _enum_value(
    value: object,
    *,
    field_name: str,
    supported: tuple[str, ...],
    default: str,
) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or value not in supported:
        supported_values = ", ".join(supported)
        raise ValueError(f"{field_name} must be one of: {supported_values}")
    return value


def _integer_value(
    value: object,
    *,
    field_name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer between {minimum} and {maximum}")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    else:
        raise ValueError(f"{field_name} must be an integer between {minimum} and {maximum}")
    if number < minimum or number > maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return number


def _parse_datetime(value: str, *, end_of_day: bool) -> datetime:
    if _DATE_ONLY_PATTERN.fullmatch(value):
        parsed_date = date.fromisoformat(value)
        boundary = time.max if end_of_day else time.min
        return datetime.combine(parsed_date, boundary, tzinfo=UTC)

    normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_persisted_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _parse_datetime(value, end_of_day=False)
    except ValueError:
        return None


def _timestamp_sort_key(value: object) -> datetime:
    return _parse_persisted_timestamp(value) or datetime.min.replace(tzinfo=UTC)


def _compact_text(text: str) -> str:
    return _WHITESPACE_PATTERN.sub(" ", text).strip()


def _trim_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[: max(limit - 3, 0)]}..."


def register_session_search_tool(registry: ToolRegistry, sessions: ChatSessionManager) -> None:
    """Register the session_search tool with a vBot tool registry."""
    registry.register(
        SESSION_SEARCH_TOOL_NAME,
        SESSION_SEARCH_TOOL_DESCRIPTION,
        SESSION_SEARCH_TOOL_PARAMETERS,
        make_session_search_handler(sessions),
        display=ToolDisplay(summary_fields=("query", "session_id")),
    )


__all__ = [
    "SESSION_SEARCH_DEFAULT_LIMIT",
    "SESSION_SEARCH_MAX_CONTEXT_MESSAGES",
    "SESSION_SEARCH_MAX_LIMIT",
    "SESSION_SEARCH_TOOL_DESCRIPTION",
    "SESSION_SEARCH_TOOL_NAME",
    "SESSION_SEARCH_TOOL_PARAMETERS",
    "make_session_search_handler",
    "register_session_search_tool",
    "session_search_handler",
]
