"""Canonical Session scan backend for persisted Sessions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from core.chat.content_blocks import FileBlock, FileMentionBlock, MediaBlock, TextBlock
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
    SessionSearchHit,
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
CANONICAL_FALLBACK_PARTIAL_REASON = (
    "Search could not check all eligible Messages. Results are incomplete. Narrow period or "
    "session_id; an empty result does not establish that no matching text exists."
)


def _fts_fallback_reason(order: RecallOrder, *, complete: bool) -> str:
    """Explain a scan that replaced the keyword index, naming the order it used."""

    scan_order = "oldest-first" if order == "oldest" else "newest-first"
    if not complete:
        return (
            "Keyword search used a fallback scan and could not check all eligible Messages. "
            f"Results are incomplete and {scan_order}. Narrow period or session_id; an empty "
            "result does not establish that no matching text exists."
        )
    reason = f"Keyword search used a fallback scan with substring matching and {scan_order} order."
    if order == "relevance":
        reason += " Relevance ranking was unavailable."
    return reason


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
    to the request selection and those versions. ``creation_orders`` gives each
    candidate's creation order (larger is newer), which attributes history a
    fork shares with its origin to one Session.
    """

    live_session_ids: frozenset[str]
    candidates: dict[str, tuple[str, int]]
    snapshot_id: str
    creation_orders: dict[str, int]


class CanonicalSessionRecallBackend:
    """Recall backend that scans canonical Session history in the Session store.

    Its Message pages come from ``ChatSessionManager.search_messages``, which
    checks eligible Messages by Message time in SQL within a candidate budget;
    subclasses reuse the same page shaping with FTS candidates.
    """

    def __init__(self, sessions: ChatSessionManager) -> None:
        self.sessions = sessions

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
        return await self.sessions.run_async(self._search_page, request, use_fts=False)

    def _search_page(self, request: RecallSearchRequest, *, use_fts: bool) -> RecallSearchPage:
        """Return one Message page of exact literal matches.

        One hit beyond the page proves ``has_more``. With ``use_fts`` the
        Session store enumerates FTS candidates; otherwise, or when FTS is
        unavailable, it scans eligible Messages by Message time.
        """

        scope = self._read_scope(request)
        _check_snapshot(request, scope.snapshot_id)
        result = self.sessions.search_messages(
            request.query,
            project_id=request.project_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            match_mode=request.match_mode,
            order=request.order,
            limit=request.offset + request.limit + 1,
            roles=request.roles,
            since=None if request.since is None else request.since.isoformat(),
            until=None if request.until is None else request.until.isoformat(),
            excluded_session_ids=request.excluded_session_ids,
            include_subagents=request.include_subagents,
            use_fts=use_fts,
        )
        selected = result.hits[request.offset : request.offset + request.limit]
        time_order = "newest" if request.order == "relevance" else request.order
        fts_failed = result.fallback_reason in {"fts_unavailable", "fts_error"}
        if result.method == "fts":
            ranking = "bm25" if request.order == "relevance" else f"message_time_{time_order}"
        elif use_fts:
            ranking = f"substring_scan_{time_order}"
        else:
            ranking = f"message_time_{time_order}"
        return RecallSearchPage(
            hits=tuple(message_hit(hit, request) for hit in selected),
            result_type="message",
            ranking=ranking,
            snapshot_id=scope.snapshot_id,
            has_more=len(result.hits) > request.offset + request.limit,
            total_candidate_sessions=len(scope.candidates),
            degraded=fts_failed or not result.complete,
            degradation_reason=(
                _fts_fallback_reason(request.order, complete=result.complete)
                if fts_failed
                else CANONICAL_FALLBACK_PARTIAL_REASON
                if not result.complete
                else None
            ),
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
        creation_orders: dict[str, int] = {}
        for revision in self.sessions.list_history_revisions(request.agent_id, request.project_id):
            session_id = revision.address.session_id
            live.add(session_id)
            if (
                revision.recall_visibility in admitted
                and session_id not in excluded
                and (request.session_id is None or session_id == request.session_id)
            ):
                candidates[session_id] = (revision.generation_id, revision.history_revision)
                creation_orders[session_id] = revision.creation_order
        return RecallScope(
            live_session_ids=frozenset(live),
            candidates=candidates,
            snapshot_id=self._selection_snapshot(request, candidates),
            creation_orders=creation_orders,
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


def message_hit(hit: SessionSearchHit, request: RecallSearchRequest) -> RecallSearchHit:
    """Present one exact Session search hit as a Recall Message hit."""

    match_start, match_end = first_match_span(hit.text, request.query, request.match_mode)
    return RecallSearchHit(
        result_type="message",
        session_id=hit.address.session_id,
        message_id=hit.message_id,
        role=hit.role,
        timestamp=hit.timestamp,
        text=hit.text,
        score=hit.rank,
        match_start=match_start,
        match_end=match_end,
    )


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


def compact_text(text: str) -> str:
    return _WHITESPACE_PATTERN.sub(" ", text).strip()
