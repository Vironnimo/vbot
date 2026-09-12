"""Canonical Session scan backend for persisted Sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, cast

from core.chat.content_blocks import FileBlock, FileMentionBlock, MediaBlock, TextBlock
from core.recall.recall import (
    JsonObject,
    RecallMatchMode,
    RecallOrder,
    RecallSearchCapabilities,
    RecallSearchError,
    RecallSearchHit,
    RecallSearchPage,
    RecallSearchRequest,
)
from core.sessions import ChatSessionManager, SessionAddress, is_skill_context_note

# Roles indexed by the Sessions-owned canonical search projection.
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
SESSION_RECALL_MATCH_MODES: tuple[RecallMatchMode, ...] = (
    "all_terms",
    "any_term",
    "phrase",
)
SESSION_RECALL_SORT_MODES: tuple[RecallOrder, ...] = ("newest", "oldest")
CANONICAL_FALLBACK_SCAN_LIMIT = 10_000
CANONICAL_FALLBACK_PARTIAL_REASON = (
    "The bounded canonical fallback reached its scan limit; results are partial."
)


def _session_address(request: Any, session_id: str) -> SessionAddress:
    """Address the one Session of a recall request's scope."""
    return SessionAddress(
        project_id=request.project_id, agent_id=request.agent_id, session_id=session_id
    )


# Backend-specific Agent guidance used to build the session_search Tool summary
# and query parameter description. The summary helps the Agent select the Tool;
# the parameter description explains how to construct a query for this backend.
SESSION_RECALL_LITERAL_TOOL_SUMMARY = (
    "Find persisted Sessions and literal matches in past conversations."
)
SESSION_RECALL_LITERAL_SEARCH_GUIDANCE = (
    "Literal terms to find. Every whitespace-separated term must occur as a case-insensitive "
    "substring; synonyms and paraphrases do not match. Omit to list recent Sessions. Matches "
    "are newest first."
)

# Names of the built-in recall tools whose results are persisted into sessions
# as ``role="tool"`` messages. Indexing or returning those results creates a
# feedback loop where every search matches its own prior output, so they are
# excluded from recall (the canonical scan and semantic
# index). This duplicates the Tool names from ``core.tools.session_search``
# because recall is a lower layer than tools and cannot import it without an
# import cycle; a test in ``test_session_search`` asserts the two stay in sync.
RECALL_TOOL_RESULT_NAMES = frozenset({"session_search", "session_read"})

_WHITESPACE_PATTERN = re.compile(r"\s+")


class CanonicalSessionRecallBackend:
    """Recall backend that scans canonical Session history on demand."""

    def __init__(
        self,
        sessions: ChatSessionManager,
        *,
        search_scan_limit: int | None = None,
    ) -> None:
        if search_scan_limit is not None and search_scan_limit <= 0:
            raise ValueError("search scan limit must be positive")
        self.sessions = sessions
        self._search_scan_limit = search_scan_limit

    def search_capabilities(self) -> RecallSearchCapabilities:
        return RecallSearchCapabilities(
            result_type="message",
            guidance=SESSION_RECALL_LITERAL_SEARCH_GUIDANCE,
            tool_summary=SESSION_RECALL_LITERAL_TOOL_SUMMARY,
            query_description=SESSION_RECALL_LITERAL_SEARCH_GUIDANCE,
            match_argument="match",
            match_modes=SESSION_RECALL_MATCH_MODES,
            order_modes=SESSION_RECALL_SORT_MODES,
            default_order="newest",
            supports_roles=True,
        )

    async def search_page(self, request: RecallSearchRequest) -> RecallSearchPage:
        return await asyncio.to_thread(self._search_page, request)

    def _search_page(self, request: RecallSearchRequest) -> RecallSearchPage:
        summaries = self._search_candidate_summaries(request)
        snapshot_id = self._search_snapshot(request, summaries)
        if request.snapshot_id is not None and request.snapshot_id != snapshot_id:
            raise RecallSearchError(
                "stale_cursor", "Session search source changed; repeat the search."
            )

        ranked: list[tuple[datetime, str, int, RecallSearchHit]] = []
        remaining = self._search_scan_limit
        scan_complete = True
        for summary in summaries:
            session_id = str(summary["id"])
            messages = self.sessions.get(_session_address(request, session_id)).load_active()
            selected_messages = messages
            if remaining is not None:
                if len(messages) > remaining:
                    selected_messages = messages[:remaining]
                    scan_complete = False
                remaining -= len(selected_messages)
            for message_index, message in enumerate(selected_messages):
                if not message_matches_search_request(message, request):
                    continue
                text = message_search_text(message)
                if not text_matches_search_request(text, request):
                    continue
                match_start, match_end = first_match_span(text, request.query, request.match_mode)
                timestamp = parse_persisted_timestamp(message.timestamp) or datetime.min.replace(
                    tzinfo=UTC
                )
                ranked.append(
                    (
                        timestamp,
                        session_id,
                        message_index,
                        RecallSearchHit(
                            result_type="message",
                            session_id=session_id,
                            message_id=str(message.id),
                            role=str(message.role),
                            timestamp=str(message.timestamp),
                            text=text,
                            score=0.0,
                            match_start=match_start,
                            match_end=match_end,
                        ),
                    )
                )
            if not scan_complete:
                break
        ranked.sort(
            key=lambda item: (item[0], item[1], item[2]),
            reverse=request.order == "newest",
        )
        start = request.offset
        end = min(start + request.limit, len(ranked))
        return RecallSearchPage(
            hits=tuple(item[3] for item in ranked[start:end]),
            result_type="message",
            ranking=f"message_time_{request.order}",
            snapshot_id=snapshot_id,
            has_more=end < len(ranked),
            total_candidate_sessions=len(summaries),
            degraded=not scan_complete,
            degradation_reason=(CANONICAL_FALLBACK_PARTIAL_REASON if not scan_complete else None),
        )

    def _search_candidate_summaries(self, request: RecallSearchRequest) -> list[JsonObject]:
        summaries = cast(
            list[JsonObject], self.sessions.list_summaries(request.agent_id, request.project_id)
        )
        return [
            summary
            for summary in summaries
            if str(summary.get("id")) not in request.excluded_session_ids
            and (request.session_id is None or str(summary.get("id")) == request.session_id)
        ]

    def _search_snapshot(self, request: RecallSearchRequest, summaries: list[JsonObject]) -> str:
        # Recall tracks only canonical history. Metadata-only changes must not
        # invalidate a continuation or trigger a rebuild of this projection.
        # One batched canonical-freshness query instead of one per Session.
        versions = self.sessions.list_history_versions(
            [_session_address(request, str(summary["id"])) for summary in summaries]
        )
        fingerprint: list[str] = []
        for summary in sorted(summaries, key=lambda item: str(item.get("id", ""))):
            session_id = str(summary["id"])
            version = versions.get(_session_address(request, session_id))
            if version is None:
                continue
            generation_id, revision = version
            fingerprint.append(f"{session_id}:{generation_id}:{revision}")
        return hashlib.sha256("\n".join(fingerprint).encode("utf-8")).hexdigest()


def is_recall_artifact_message(message: Any) -> bool:
    """True for a persisted Session Recall Tool result.

    Such a message is derived recall output, not conversation content. Indexing
    or returning it makes a search match its own prior results, so it is
    excluded from matches, context/bookends, and the semantic index.
    """

    return (
        getattr(message, "role", "") == "tool"
        and getattr(message, "name", None) in RECALL_TOOL_RESULT_NAMES
    )


def message_matches_search_request(message: Any, request: RecallSearchRequest) -> bool:
    if message.role not in request.roles:
        return False
    if is_skill_context_note(message) or is_recall_artifact_message(message):
        return False
    timestamp = parse_persisted_timestamp(message.timestamp)
    if request.since is not None and timestamp is not None and timestamp < request.since:
        return False
    return not (request.until is not None and timestamp is not None and timestamp > request.until)


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
    if isinstance(block, FileMentionBlock):
        # Index the mentioned path only: the snapshot text is repo content, not
        # conversation content, and would bloat the recall index.
        return block.path
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


def text_matches_search_request(text: str, request: RecallSearchRequest) -> bool:
    haystack = compact_text(text).casefold()
    if not haystack:
        return False
    if request.match_mode == "phrase":
        return compact_text(request.query).casefold() in haystack
    terms = query_terms(request.query)
    if request.match_mode == "any_term":
        return any(term in haystack for term in terms)
    return all(term in haystack for term in terms)


def first_match_span(text: str, query: str, match_mode: str) -> tuple[int | None, int | None]:
    """Locate a useful raw-source span without normalizing the returned text."""

    haystack = text.casefold()
    needles = (
        [query.casefold()]
        if match_mode == "phrase"
        else [term for term in query_terms(query) if term]
    )
    matches = [(haystack.find(needle), len(needle)) for needle in needles if needle]
    matches = [(index, length) for index, length in matches if index >= 0]
    if not matches:
        return None, None
    index, length = min(matches, key=lambda item: item[0])
    return index, index + length


def query_terms(query: str) -> list[str]:
    return [term.casefold() for term in compact_text(query).split(" ") if term]


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
