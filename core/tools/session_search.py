"""Built-in session_search tool for finding persisted chat history."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import cast

from core.chat.errors import ChatSessionError
from core.recall import (
    JsonlSessionRecallBackend,
    RecallBackend,
    RecallMatchMode,
    RecallRequest,
    RecallSortMode,
)
from core.recall.jsonl import (
    SESSION_RECALL_DEFAULT_ROLES,
    SESSION_RECALL_MATCH_MODES,
    SESSION_RECALL_SORT_MODES,
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

SESSION_SEARCH_TOOL_NAME = "session_search"
SESSION_SEARCH_TOOL_DESCRIPTION = (
    "Search persisted chat sessions by keywords, time range, role, agent, or session. "
    "Without a query, returns recent matching session summaries."
)
SESSION_SEARCH_DEFAULT_LIMIT = 20
SESSION_SEARCH_MAX_LIMIT = 100
SESSION_SEARCH_MAX_CONTEXT_MESSAGES = 2
SESSION_SEARCH_DEFAULT_BOOKEND_MESSAGES = 2
SESSION_SEARCH_MAX_BOOKEND_MESSAGES = 5
SESSION_SEARCH_DEFAULT_ROLES = SESSION_RECALL_DEFAULT_ROLES
SESSION_SEARCH_SUPPORTED_ROLES = SESSION_RECALL_SUPPORTED_ROLES
SESSION_SEARCH_MATCH_MODES = SESSION_RECALL_MATCH_MODES
SESSION_SEARCH_SORT_MODES = SESSION_RECALL_SORT_MODES

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
        "around_message_id": {
            "type": "string",
            "description": (
                "With session_id, return an anchored context window around this message id."
            ),
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
                "Message roles to search. Defaults to conversation: user, assistant, errors, and "
                "compaction checkpoints. Tool results are opt-in — include 'tool' explicitly to "
                "search them; include 'note' explicitly for kernel notes."
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
        "bookends": {
            "type": "number",
            "description": (
                "Session start/end messages to include for orientation (default 2, max 5)."
            ),
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


@dataclass(frozen=True)
class _ParsedSearchRequest:
    agent_id: str
    session_id: str | None
    around_message_id: str | None
    query: str | None
    since: datetime | None
    until: datetime | None
    roles: tuple[str, ...]
    match_mode: RecallMatchMode
    limit: int
    context_messages: int
    bookend_messages: int
    sort: RecallSortMode

    def to_recall_request(self) -> RecallRequest:
        return RecallRequest(
            agent_id=self.agent_id,
            session_id=self.session_id,
            around_message_id=self.around_message_id,
            query=self.query,
            since=self.since,
            until=self.until,
            roles=self.roles,
            match_mode=self.match_mode,
            limit=self.limit,
            context_messages=self.context_messages,
            bookend_messages=self.bookend_messages,
            sort=self.sort,
        )


def make_session_search_handler(recall_backend: RecallBackend):
    """Create a session_search tool handler bound to a recall backend."""

    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return session_search_handler(context, arguments, recall_backend)

    return handler


def session_search_handler(
    context: ToolContext,
    arguments: JsonObject,
    recall_backend: RecallBackend | ChatSessionManager,
) -> JsonObject:
    """Handle a session_search tool call and return a stable vBot result envelope."""
    if isinstance(recall_backend, ChatSessionManager):
        recall_backend = JsonlSessionRecallBackend(recall_backend)

    unknown_arguments = set(arguments) - _ALLOWED_ARGUMENTS
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

    try:
        request = _parse_search_request(context, arguments).to_recall_request()
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))

    try:
        if request.around_message_id is not None:
            return tool_success(recall_backend.scroll(request))
        if request.query is None:
            return tool_success(recall_backend.browse(request))
        return tool_success(recall_backend.search(request))
    except ChatSessionError as error:
        return tool_failure("session_search_error", str(error))


def _parse_search_request(context: ToolContext, arguments: JsonObject) -> _ParsedSearchRequest:
    agent_id = _optional_string(arguments.get("agent_id"), field_name="agent_id")
    if agent_id is None:
        agent_id = context.agent_id
    if not agent_id:
        raise ValueError("agent_id must be a non-empty string")

    query = _optional_query(arguments.get("query"))
    session_id = _optional_string(arguments.get("session_id"), field_name="session_id")
    around_message_id = _optional_string(
        arguments.get("around_message_id"),
        field_name="around_message_id",
    )
    if around_message_id is not None and session_id is None:
        raise ValueError("around_message_id requires session_id")
    if around_message_id is not None and query is not None:
        raise ValueError("around_message_id cannot be combined with query")

    since = _optional_datetime(arguments.get("since"), field_name="since", end_of_day=False)
    until = _optional_datetime(arguments.get("until"), field_name="until", end_of_day=True)
    if since is not None and until is not None and since > until:
        raise ValueError("since must be earlier than or equal to until")

    return _ParsedSearchRequest(
        agent_id=agent_id,
        session_id=session_id,
        around_message_id=around_message_id,
        query=query,
        since=since,
        until=until,
        roles=_roles(arguments.get("roles")),
        match_mode=cast(
            RecallMatchMode,
            _enum_value(
                arguments.get("match"),
                field_name="match",
                supported=SESSION_SEARCH_MATCH_MODES,
                default="all_terms",
            ),
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
            default=2 if around_message_id is not None else 0,
            minimum=0,
            maximum=SESSION_SEARCH_MAX_CONTEXT_MESSAGES,
        ),
        bookend_messages=_integer_value(
            arguments.get("bookends"),
            field_name="bookends",
            default=SESSION_SEARCH_DEFAULT_BOOKEND_MESSAGES,
            minimum=0,
            maximum=SESSION_SEARCH_MAX_BOOKEND_MESSAGES,
        ),
        sort=cast(
            RecallSortMode,
            _enum_value(
                arguments.get("sort"),
                field_name="sort",
                supported=SESSION_SEARCH_SORT_MODES,
                default="newest",
            ),
        ),
    )


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


def register_session_search_tool(
    registry: ToolRegistry,
    recall_backend: RecallBackend | ChatSessionManager,
) -> None:
    """Register the session_search tool with a vBot tool registry."""
    if isinstance(recall_backend, ChatSessionManager):
        recall_backend = JsonlSessionRecallBackend(recall_backend)
    registry.register(
        SESSION_SEARCH_TOOL_NAME,
        SESSION_SEARCH_TOOL_DESCRIPTION,
        SESSION_SEARCH_TOOL_PARAMETERS,
        make_session_search_handler(recall_backend),
        display=ToolDisplay(summary_fields=("query", "session_id")),
    )


__all__ = [
    "SESSION_SEARCH_DEFAULT_LIMIT",
    "SESSION_SEARCH_DEFAULT_BOOKEND_MESSAGES",
    "SESSION_SEARCH_DEFAULT_ROLES",
    "SESSION_SEARCH_MATCH_MODES",
    "SESSION_SEARCH_MAX_BOOKEND_MESSAGES",
    "SESSION_SEARCH_MAX_CONTEXT_MESSAGES",
    "SESSION_SEARCH_MAX_LIMIT",
    "SESSION_SEARCH_SORT_MODES",
    "SESSION_SEARCH_SUPPORTED_ROLES",
    "SESSION_SEARCH_TOOL_DESCRIPTION",
    "SESSION_SEARCH_TOOL_NAME",
    "SESSION_SEARCH_TOOL_PARAMETERS",
    "make_session_search_handler",
    "register_session_search_tool",
    "session_search_handler",
]
