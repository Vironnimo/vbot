"""Canonical Session scan backend for persisted Sessions."""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.chat.content_blocks import FileBlock, FileMentionBlock, MediaBlock, TextBlock
from core.chat.messages import ChatMessage
from core.recall.recall import (
    RecallMatchMode,
    RecallOrder,
    RecallSearchCapabilities,
    RecallSearchError,
    RecallSearchHit,
    RecallSearchPage,
    RecallSearchRequest,
)
from core.sessions import (
    ChatSessionManager,
    SessionAddress,
    is_skill_context_note,
    recall_visibilities,
)

# Roles indexed by the Sessions-owned canonical search projection.
SESSION_RECALL_CONVERSATION_ROLES = (
    "user",
    "assistant",
    "tool",
    "error",
    "compaction_checkpoint",
)
# Ordinary recall searches conversation content and labeled summaries.
# Tool Results and operational errors require explicit lower-level diagnostic reads.
SESSION_RECALL_DEFAULT_ROLES = (
    "user",
    "assistant",
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
    "Search could not check all eligible Messages. Results are incomplete. Narrow period or "
    "session_id; an empty result does not establish that no matching text exists."
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
    "substring; synonyms and paraphrases do not match. Matches "
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


@dataclass(frozen=True)
class RecallScope:
    """Live Sessions of one Recall scope and the request's candidates among them.

    ``candidates`` maps each candidate Session id to its canonical
    ``(generation_id, history_revision)``; ``snapshot_id`` binds a continuation
    to the request selection and those versions.
    """

    live_session_ids: frozenset[str]
    candidates: dict[str, tuple[str, int]]
    snapshot_id: str


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
        scope = self._read_scope(request)
        _check_snapshot(request, scope.snapshot_id)

        candidates = self._search_candidates(request, sorted(scope.candidates))
        scan_complete = True
        if self._search_scan_limit is not None:
            # Budget eligible Messages globally by canonical time, before matching
            # text. Session list order and ineligible rows must not consume it.
            selected = heapq.nlargest(
                self._search_scan_limit + 1, candidates, key=lambda item: item[:3]
            )
            scan_complete = len(selected) <= self._search_scan_limit
            candidates = iter(selected[: self._search_scan_limit])

        ranked: list[tuple[datetime, str, int, RecallSearchHit]] = []
        for timestamp, session_id, message_index, message in candidates:
            text = message_search_text(message)
            if not text_matches_search_request(text, request):
                continue
            match_start, match_end = first_match_span(text, request.query, request.match_mode)
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
            snapshot_id=scope.snapshot_id,
            has_more=end < len(ranked),
            total_candidate_sessions=len(scope.candidates),
            degraded=not scan_complete,
            degradation_reason=(CANONICAL_FALLBACK_PARTIAL_REASON if not scan_complete else None),
        )

    def _search_candidates(
        self, request: RecallSearchRequest, session_ids: list[str]
    ) -> Iterator[tuple[datetime, str, int, ChatMessage]]:
        for session_id in session_ids:
            messages = self.sessions.get(_session_address(request, session_id)).load_active()
            for message_index, message in enumerate(messages):
                if message_matches_search_request(message, request):
                    yield (
                        timestamp_sort_key(message.timestamp),
                        session_id,
                        message_index,
                        message,
                    )

    def _read_scope(self, request: RecallSearchRequest) -> RecallScope:
        """Read every live Session version of the scope in one Session-store query.

        Candidates keep the Sessions whose Recall visibility the request admits,
        minus its exclusions and outside its optional Session filter. Recall
        tracks only canonical history, so metadata-only changes do not
        invalidate a continuation.
        """

        admitted = recall_visibilities(include_subagents=request.include_subagents)
        excluded = set(request.excluded_session_ids)
        live: set[str] = set()
        candidates: dict[str, tuple[str, int]] = {}
        for revision in self.sessions.list_history_revisions(request.agent_id, request.project_id):
            session_id = revision.address.session_id
            live.add(session_id)
            if (
                revision.recall_visibility in admitted
                and session_id not in excluded
                and (request.session_id is None or session_id == request.session_id)
            ):
                candidates[session_id] = (revision.generation_id, revision.history_revision)
        return RecallScope(
            live_session_ids=frozenset(live),
            candidates=candidates,
            snapshot_id=self._selection_snapshot(request, candidates),
        )

    def _selection_snapshot(
        self, request: RecallSearchRequest, history: dict[str, tuple[str, int]]
    ) -> str:
        """Bind a continuation to the selection and the candidates' canonical versions."""

        fingerprint = [
            f"{session_id}:{generation_id}:{revision}"
            for session_id, (generation_id, revision) in sorted(history.items())
        ]
        selection = {
            "backend": type(self).__name__,
            "agent_id": request.agent_id,
            "project_id": request.project_id,
            "session_id": request.session_id,
            "excluded_session_ids": sorted(set(request.excluded_session_ids)),
            "include_subagents": request.include_subagents,
            "query": request.query,
            "since": request.since.isoformat() if request.since is not None else None,
            "until": request.until.isoformat() if request.until is not None else None,
            "roles": sorted(set(request.roles)),
            "match_mode": request.match_mode,
            "order": request.order,
            "history": fingerprint,
        }
        # Offset and page size may change while traversing the same selection.
        payload = json.dumps(selection, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check_snapshot(request: RecallSearchRequest, snapshot_id: str) -> None:
    """Reject a continuation whose selection or source history changed."""

    if request.snapshot_id is not None and request.snapshot_id != snapshot_id:
        raise RecallSearchError("stale_cursor", "Session search source changed; repeat the search.")


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
    return content_to_text(message.content)


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
