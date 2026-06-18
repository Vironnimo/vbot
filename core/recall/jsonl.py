"""JSONL scan recall backend for persisted Sessions."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from core.chat.content_blocks import FileBlock, MediaBlock, TextBlock
from core.recall.recall import JsonObject, RecallRequest
from core.sessions import ChatSessionManager, is_skill_context_note

# Roles that count as a real conversation message. Used for vector chunk
# anchoring (which message to center a result on) and as the full set of roles
# a caller is allowed to search. Request-independent: it does not shrink with
# what a given search asked for.
SESSION_RECALL_CONVERSATION_ROLES = (
    "user",
    "assistant",
    "tool",
    "error",
    "compaction_checkpoint",
)
# Roles a search matches when the caller does not pass ``roles``. Tool results
# are opt-in: they embed poorly (ANSI dumps, JSON run envelopes, directory
# listings) and drowned out conversation in results, so a search reaches them
# only when the caller explicitly asks via ``roles: ["tool"]``. Errors stay in
# the default — they are low-volume and occasionally the thing being looked for.
SESSION_RECALL_DEFAULT_ROLES = (
    "user",
    "assistant",
    "error",
    "compaction_checkpoint",
)
SESSION_RECALL_SUPPORTED_ROLES = (*SESSION_RECALL_CONVERSATION_ROLES, "note")
SESSION_RECALL_MATCH_MODES = ("all_terms", "any_term", "phrase")
SESSION_RECALL_SORT_MODES = ("newest", "oldest")
SESSION_RECALL_SNIPPET_CHARS = 320
SESSION_RECALL_CONTEXT_SNIPPET_CHARS = 180

# Backend-specific guidance appended to the session_search tool description so the
# agent knows how queries behave for the active backend. Each backend returns its
# own fragment from ``describe_search``; this is the literal-substring default
# shared by the JSONL scan and the FTS backend (both match case-insensitive
# substrings). The vector and hybrid backends override it.
SESSION_RECALL_LITERAL_SEARCH_GUIDANCE = (
    "This backend matches literal case-insensitive substrings — choose distinctive "
    "keywords from text you remember. It does not match by meaning, so synonyms or "
    "paraphrases will not match."
)

# Name of the built-in recall tool whose results are persisted into sessions
# as ``role="tool"`` messages. Indexing or returning those results creates a
# feedback loop where every search matches its own prior output, so they are
# excluded from recall (the JSONL scan, context/bookends, and the semantic
# index). This duplicates ``core.tools.session_search.SESSION_SEARCH_TOOL_NAME``
# because recall is a lower layer than tools and cannot import it without an
# import cycle; a test in ``test_session_search`` asserts the two stay in sync.
RECALL_TOOL_RESULT_NAME = "session_search"

_WHITESPACE_PATTERN = re.compile(r"\s+")


class JsonlSessionRecallBackend:
    """Recall backend that scans canonical JSONL Sessions on demand."""

    def __init__(self, sessions: ChatSessionManager) -> None:
        self.sessions = sessions

    def browse(self, request: RecallRequest) -> JsonObject:
        summaries = self.candidate_session_summaries(request)
        return self.session_summary_result(request, summaries)

    def overview(self, request: RecallRequest) -> JsonObject:
        summaries = self.candidate_session_summaries(request)
        return self.session_overview_result(request, summaries)

    def search(self, request: RecallRequest) -> JsonObject:
        summaries = self.candidate_session_summaries(request)
        return self.message_search_result(request, summaries)

    def scroll(self, request: RecallRequest) -> JsonObject:
        summaries = self.candidate_session_summaries(request)
        return self.anchored_view_result(request, summaries)

    def describe_search(self) -> str:
        """Backend-specific guidance for the session_search tool description.

        Appended to the generic tool description so the agent knows how queries
        behave for the active backend. The JSONL scan matches literal substrings;
        the FTS backend inherits this fragment, and the vector/hybrid backends
        override it.
        """
        return SESSION_RECALL_LITERAL_SEARCH_GUIDANCE

    def candidate_session_summaries(self, request: RecallRequest) -> list[JsonObject]:
        summaries = [
            summary
            for summary in self.sessions.list_with_metadata(request.agent_id, request.project_id)
            if session_matches_request(summary, request)
        ]
        summaries.sort(
            key=lambda summary: timestamp_sort_key(summary.get("last_active_at")),
            reverse=request.sort == "newest",
        )
        return summaries

    def session_summary_result(
        self,
        request: RecallRequest,
        summaries: list[JsonObject],
    ) -> JsonObject:
        limited_summaries = summaries[: request.limit]
        sessions_payload = [
            session_payload(request.agent_id, summary) for summary in limited_summaries
        ]
        truncated = len(summaries) > request.limit
        return {
            "content": render_session_summaries(request, sessions_payload, truncated=truncated),
            "sessions": sessions_payload,
            "truncated": truncated,
            "total_candidates": len(summaries),
            "request": request_payload(request),
        }

    def session_overview_result(
        self,
        request: RecallRequest,
        summaries: list[JsonObject],
    ) -> JsonObject:
        summary = summaries[0] if summaries else None
        if summary is None or request.session_id is None:
            return empty_session_overview(request)

        messages = self.sessions.get(
            request.agent_id, request.session_id, request.project_id
        ).load()
        eligible_indices = [
            index
            for index, message in enumerate(messages)
            if is_eligible_context(message, request.roles)
        ]
        start_indices, end_indices = overview_bookend_indices(
            eligible_indices, request.bookend_messages
        )
        bookend_start = [message_preview_payload(messages[index]) for index in start_indices]
        bookend_end = [message_preview_payload(messages[index]) for index in end_indices]
        total = len(eligible_indices)
        omitted = total - len(start_indices) - len(end_indices)
        return {
            "content": render_session_overview(request, bookend_start, bookend_end, total, omitted),
            "session": session_payload(request.agent_id, summary),
            "bookend_start": bookend_start,
            "bookend_end": bookend_end,
            "total_messages": total,
            "truncated": omitted > 0,
            "request": request_payload(request),
        }

    def message_search_result(
        self,
        request: RecallRequest,
        summaries: list[JsonObject],
    ) -> JsonObject:
        matches: list[JsonObject] = []
        searched_sessions = 0
        truncated = False

        for summary in summaries:
            session_id = str(summary["id"])
            messages = self.sessions.get(request.agent_id, session_id, request.project_id).load()
            searched_sessions += 1
            for message_index, message in enumerate(messages):
                if not message_matches_request(message, request):
                    continue
                text = message_search_text(message)
                if not text_matches_query(text, request):
                    continue
                if len(matches) >= request.limit:
                    truncated = True
                    break
                matches.append(
                    message_match_payload(
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
            "content": render_message_matches(request, matches, truncated=truncated),
            "matches": matches,
            "truncated": truncated,
            "searched_sessions": searched_sessions,
            "total_candidate_sessions": len(summaries),
            "request": request_payload(request),
        }

    def anchored_view_result(
        self,
        request: RecallRequest,
        summaries: list[JsonObject],
    ) -> JsonObject:
        summary = summaries[0] if summaries else None
        if summary is None or request.session_id is None or request.around_message_id is None:
            return empty_anchored_view(request)

        messages = self.sessions.get(
            request.agent_id, request.session_id, request.project_id
        ).load()
        anchor_index = message_index_by_id(messages, request.around_message_id)
        if anchor_index is None:
            return empty_anchored_view(request)
        # The anchor was requested by explicit id, so surface it regardless of the
        # request's role/time filters (those still gate the neighbours and bookends
        # via is_eligible_context). Only the never-surfaced categories are excluded.
        anchor = messages[anchor_index]
        if is_skill_context_note(anchor) or is_recall_artifact_message(anchor):
            return empty_anchored_view(request)

        window = window_payload(messages, anchor_index, request.context_messages, request.roles)
        bookends = bookend_payload(
            messages,
            anchor_index,
            request.context_messages,
            request.bookend_messages,
            request.roles,
        )
        return {
            "content": render_anchored_view(request, window, bookends),
            "session": session_payload(request.agent_id, summary),
            "around_message_id": request.around_message_id,
            "window": window,
            "bookend_start": bookends["bookend_start"],
            "bookend_end": bookends["bookend_end"],
            "truncated": False,
            "request": request_payload(request),
        }


def session_matches_request(summary: JsonObject, request: RecallRequest) -> bool:
    session_id = summary.get("id")
    if request.session_id is not None and session_id != request.session_id:
        return False

    created_at = parse_persisted_timestamp(summary.get("created_at"))
    last_active_at = parse_persisted_timestamp(summary.get("last_active_at"))
    if request.since is not None and last_active_at is not None and last_active_at < request.since:
        return False
    return not (request.until is not None and created_at is not None and created_at > request.until)


def empty_anchored_view(request: RecallRequest) -> JsonObject:
    return {
        "content": f"No message found for anchored session search: {request.around_message_id}",
        "session": None,
        "around_message_id": request.around_message_id,
        "window": [],
        "bookend_start": [],
        "bookend_end": [],
        "truncated": False,
        "request": request_payload(request),
    }


def empty_session_overview(request: RecallRequest) -> JsonObject:
    return {
        "content": f"No session found: {request.session_id}",
        "session": None,
        "bookend_start": [],
        "bookend_end": [],
        "total_messages": 0,
        "truncated": False,
        "request": request_payload(request),
    }


def overview_bookend_indices(
    eligible_indices: list[int],
    bookend_messages: int,
) -> tuple[list[int], list[int]]:
    """Split a session's eligible message indices into start and end bookends.

    Returns the first ``bookend_messages`` indices and the last ``bookend_messages``
    indices, with any overlap removed so a short session never reports the same
    message in both halves (e.g. four messages with ``bookends=3`` yields a
    three-message start and a one-message end, not a duplicated middle).
    """

    if bookend_messages <= 0:
        return [], []
    start = eligible_indices[:bookend_messages]
    start_set = set(start)
    end = [index for index in eligible_indices[-bookend_messages:] if index not in start_set]
    return start, end


def message_index_by_id(messages: list[Any], message_id: str) -> int | None:
    for index, message in enumerate(messages):
        if message.id == message_id:
            return index
    return None


def is_recall_artifact_message(message: Any) -> bool:
    """True for a persisted ``session_search`` result — the recall tool's own output.

    Such a message is derived recall output, not conversation content. Indexing
    or returning it makes a search match its own prior results, so it is
    excluded from matches, context/bookends, and the semantic index.
    """

    return (
        getattr(message, "role", "") == "tool"
        and getattr(message, "name", None) == RECALL_TOOL_RESULT_NAME
    )


def message_matches_request(message: Any, request: RecallRequest) -> bool:
    if message.role not in request.roles:
        return False
    if is_skill_context_note(message):
        return False
    if is_recall_artifact_message(message):
        return False

    timestamp = parse_persisted_timestamp(message.timestamp)
    if request.since is not None and timestamp is not None and timestamp < request.since:
        return False
    return not (request.until is not None and timestamp is not None and timestamp > request.until)


def message_match_payload(
    request: RecallRequest,
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
        "snippet": snippet(text, request, SESSION_RECALL_SNIPPET_CHARS),
    }
    if request.context_messages > 0:
        payload["context"] = context_payload(
            messages, message_index, request.context_messages, request.roles
        )
    payload["window"] = window_payload(
        messages, message_index, request.context_messages, request.roles
    )
    if request.bookend_messages > 0:
        payload.update(
            bookend_payload(
                messages,
                message_index,
                request.context_messages,
                request.bookend_messages,
                request.roles,
            )
        )
    return payload


def window_payload(
    messages: list[Any],
    message_index: int,
    context_messages: int,
    roles: tuple[str, ...],
) -> list[JsonObject]:
    return [
        message_preview_payload(messages[index])
        for index in window_indices(messages, message_index, context_messages, roles)
    ]


def bookend_payload(
    messages: list[Any],
    message_index: int,
    context_messages: int,
    bookend_messages: int,
    roles: tuple[str, ...],
) -> JsonObject:
    if bookend_messages <= 0:
        return {"bookend_start": [], "bookend_end": []}

    window_indices_value = window_indices(messages, message_index, context_messages, roles)
    window_start = min(window_indices_value) if window_indices_value else message_index
    window_end = max(window_indices_value) if window_indices_value else message_index
    start_items: list[JsonObject] = []
    for message in messages[:window_start]:
        if is_eligible_context(message, roles):
            start_items.append(message_preview_payload(message))
            if len(start_items) >= bookend_messages:
                break

    end_items: list[JsonObject] = []
    for message in reversed(messages[window_end + 1 :]):
        if is_eligible_context(message, roles):
            end_items.append(message_preview_payload(message))
            if len(end_items) >= bookend_messages:
                break
    end_items.reverse()
    return {"bookend_start": start_items, "bookend_end": end_items}


def message_preview_payload(message: Any) -> JsonObject:
    return {
        "message_id": message.id,
        "timestamp": message.timestamp,
        "role": message.role,
        "snippet": trim_text(
            compact_text(message_search_text(message)),
            SESSION_RECALL_CONTEXT_SNIPPET_CHARS,
        ),
    }


def window_indices(
    messages: list[Any],
    message_index: int,
    context_messages: int,
    roles: tuple[str, ...],
) -> list[int]:
    return [
        *neighbor_context_indices(messages, message_index, -1, context_messages, roles),
        message_index,
        *neighbor_context_indices(messages, message_index, 1, context_messages, roles),
    ]


def context_payload(
    messages: list[Any],
    message_index: int,
    context_messages: int,
    roles: tuple[str, ...],
) -> JsonObject:
    return {
        "before": neighbor_context(messages, message_index, -1, context_messages, roles),
        "after": neighbor_context(messages, message_index, 1, context_messages, roles),
    }


def neighbor_context(
    messages: list[Any],
    message_index: int,
    direction: int,
    context_messages: int,
    roles: tuple[str, ...],
) -> list[JsonObject]:
    return [
        message_preview_payload(messages[index])
        for index in neighbor_context_indices(
            messages,
            message_index,
            direction,
            context_messages,
            roles,
        )
    ]


def neighbor_context_indices(
    messages: list[Any],
    message_index: int,
    direction: int,
    context_messages: int,
    roles: tuple[str, ...],
) -> list[int]:
    neighbors: list[int] = []
    index = message_index + direction
    while 0 <= index < len(messages) and len(neighbors) < context_messages:
        message = messages[index]
        if is_eligible_context(message, roles):
            neighbors.append(index)
        index += direction
    if direction < 0:
        neighbors.reverse()
    return neighbors


def is_eligible_context(message: Any, roles: tuple[str, ...]) -> bool:
    """True when *message* may appear as a neighbor/context for a search.

    Eligibility follows the request: a message shows as surrounding context
    only when its role is one the caller asked for (so a default search that
    excludes ``tool`` never leaks a tool result in via a bookend), and never
    when it is a skill-context note or the recall tool's own output.
    """

    return (
        message.role in roles
        and not is_skill_context_note(message)
        and not is_recall_artifact_message(message)
    )


def is_context_message(message: Any) -> bool:
    """True when *message* is a real conversation message, ignoring the request.

    Used for vector chunk anchoring, where the question is "is this a real
    message worth centering a result on" independent of any one search. Read-time
    context eligibility uses :func:`is_eligible_context` with the request's roles.
    """

    return is_eligible_context(message, SESSION_RECALL_CONVERSATION_ROLES)


def message_search_text(message: Any) -> str:
    parts = [
        content_to_text(message.content),
        message.reasoning or "",
        message.name or "",
        message.error_kind or "",
        tool_calls_text(message.tool_calls),
    ]
    return "\n".join(part for part in parts if part)


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(content_block_to_text(block) for block in content)
    return ""


def content_block_to_text(block: Any) -> str:
    if isinstance(block, TextBlock):
        return block.text
    if isinstance(block, (FileBlock, MediaBlock)):
        return f"{block.filename} {block.media_type}"
    return ""


def tool_calls_text(tool_calls: Any) -> str:
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


def text_matches_query(text: str, request: RecallRequest) -> bool:
    if request.query is None:
        return True
    haystack = compact_text(text).casefold()
    if not haystack:
        return False
    if request.match_mode == "phrase":
        return compact_text(request.query).casefold() in haystack

    terms = query_terms(request.query)
    if request.match_mode == "any_term":
        return any(term in haystack for term in terms)
    return all(term in haystack for term in terms)


def snippet(text: str, request: RecallRequest, limit: int) -> str:
    compact = compact_text(text)
    if not compact:
        return ""

    index = first_match_index(compact, request)
    if index < 0:
        return trim_text(compact, limit)

    start = max(index - limit // 3, 0)
    end = min(start + limit, len(compact))
    start = max(end - limit, 0)
    snippet_text = compact[start:end]
    if start > 0:
        snippet_text = "..." + snippet_text
    if end < len(compact):
        snippet_text += "..."
    return snippet_text


def first_match_index(text: str, request: RecallRequest) -> int:
    if request.query is None:
        return -1
    haystack = text.casefold()
    if request.match_mode == "phrase":
        return haystack.find(compact_text(request.query).casefold())

    indexes = [index for term in query_terms(request.query) if (index := haystack.find(term)) >= 0]
    return min(indexes) if indexes else -1


def query_terms(query: str) -> list[str]:
    return [term.casefold() for term in compact_text(query).split(" ") if term]


def session_payload(agent_id: str, summary: JsonObject) -> JsonObject:
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


def request_payload(request: RecallRequest) -> JsonObject:
    payload: JsonObject = {
        "agent_id": request.agent_id,
        "session_id": request.session_id,
        "around_message_id": request.around_message_id,
        "query": request.query,
        "since": request.since.isoformat() if request.since is not None else None,
        "until": request.until.isoformat() if request.until is not None else None,
        "roles": list(request.roles),
        "match": request.match_mode,
        "limit": request.limit,
        "context": request.context_messages,
        "bookends": request.bookend_messages,
        "sort": request.sort,
    }
    return payload


def render_session_summaries(
    request: RecallRequest,
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
            f"created={session.get('created_at')}{metadata_suffix(session)}"
        )
    if truncated:
        lines.append(f"[Results limited to {request.limit} sessions.]")
    return "\n".join(lines)


def render_message_matches(
    request: RecallRequest,
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


def render_anchored_view(
    request: RecallRequest,
    window: list[JsonObject],
    bookends: JsonObject,
) -> str:
    if not window:
        return f"No message found for anchored session search: {request.around_message_id}"

    lines = [f"Anchored view for {request.session_id} around message {request.around_message_id}."]
    for item in bookends.get("bookend_start", []):
        lines.append(f"start: {item['timestamp']} {item['role']} {item['snippet']}")
    for item in window:
        lines.append(f"window: {item['timestamp']} {item['role']} {item['snippet']}")
    for item in bookends.get("bookend_end", []):
        lines.append(f"end: {item['timestamp']} {item['role']} {item['snippet']}")
    return "\n".join(lines)


def render_session_overview(
    request: RecallRequest,
    bookend_start: list[JsonObject],
    bookend_end: list[JsonObject],
    total: int,
    omitted: int,
) -> str:
    lines = [f"Session {request.session_id} for agent {request.agent_id}: {total} message(s)."]
    if total == 0:
        lines.append("No messages match the requested roles.")
        return "\n".join(lines)
    for item in bookend_start:
        lines.append(f"start: {item['timestamp']} {item['role']} {item['snippet']}")
    if omitted > 0:
        lines.append(f"... {omitted} message(s) omitted ...")
    for item in bookend_end:
        lines.append(f"end: {item['timestamp']} {item['role']} {item['snippet']}")
    return "\n".join(lines)


def metadata_suffix(session: JsonObject) -> str:
    metadata = session.get("metadata")
    if not isinstance(metadata, dict) or not metadata:
        return ""
    rendered = ", ".join(f"{key}={value}" for key, value in sorted(metadata.items()))
    return f" metadata: {rendered}"


def parse_persisted_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def timestamp_sort_key(value: object) -> datetime:
    return parse_persisted_timestamp(value) or datetime.min.replace(tzinfo=UTC)


def compact_text(text: str) -> str:
    return _WHITESPACE_PATTERN.sub(" ", text).strip()


def trim_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[: max(limit - 3, 0)]}..."
