"""Literal Passage ranking shared by Hybrid and unavailable semantic search."""

from core.recall._passage_catalog import Candidates, StoredPassage
from core.recall.canonical import RecallScope, first_match_span
from core.recall.passage_index import TRIGRAM_RANKING, LiteralQuery, PassageIndex
from core.recall.recall import RecallSearchHit, RecallSearchPage, RecallSearchRequest

LITERAL_ONLY_REASON = (
    "Only keyword search succeeded; paraphrases may be missing. "
    "Use words likely to appear in the conversation; rephrasing cannot "
    "restore semantic search."
)


class PreparedLiteralSearch:
    """Literal Passage ranking over a catalog refreshed once for one search."""

    def __init__(
        self,
        index: PassageIndex,
        request: RecallSearchRequest,
        scope: RecallScope,
        query: LiteralQuery | None,
    ) -> None:
        self._index = index
        self._request = request
        self._scope = scope
        self._query = query

    async def page(self, offset: int, limit: int) -> RecallSearchPage:
        query = self._query
        if query is None or not self._scope.candidates:
            return RecallSearchPage(
                hits=(),
                result_type="passage",
                ranking=TRIGRAM_RANKING if query is None else query.ranking,
                snapshot_id=self._scope.snapshot_id,
                has_more=False,
                total_candidate_sessions=len(self._scope.candidates),
            )
        request = self._request
        candidates = Candidates.of(request.agent_id, request.project_id, self._scope)
        try:
            matches = await self._index.literal_search(
                request, candidates, query, offset + limit + 1
            )
        except Exception as error:
            await self._index.discard_if_damaged(error)
            raise
        hits = tuple(
            _literal_hit(passage, session_id, rank, request)
            for passage, session_id, rank in matches[offset : offset + limit]
        )
        return RecallSearchPage(
            hits=hits,
            result_type="passage",
            ranking=query.ranking,
            snapshot_id=self._scope.snapshot_id,
            has_more=len(matches) > offset + limit,
            total_candidate_sessions=len(self._scope.candidates),
        )


def _literal_hit(
    passage: StoredPassage, session_id: str, rank: float, request: RecallSearchRequest
) -> RecallSearchHit:
    start, end = first_match_span(passage.text, request.query, request.match_mode)
    return RecallSearchHit(
        result_type="passage",
        session_id=session_id,
        message_id=passage.start_message_id,
        role=passage.start_role,
        timestamp=passage.start_timestamp,
        text=passage.text,
        score=rank,
        passage_id=passage.passage_id,
        start_message_id=passage.start_message_id,
        end_message_id=passage.end_message_id,
        end_timestamp=passage.end_timestamp,
        match_start=start,
        match_end=end,
        sources=("literal",),
    )
