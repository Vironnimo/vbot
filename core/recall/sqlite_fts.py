"""SQLite FTS5 Recall backend for Session search.

Message pages come from the Session store's own FTS5 search. The backend keeps
no derived index of its own.
"""

from __future__ import annotations

from typing import override

from core.recall.canonical import CanonicalSessionRecallBackend
from core.recall.recall import (
    RecallBackendContext,
    RecallSearchCapabilities,
    RecallSearchPage,
    RecallSearchRequest,
)


class SqliteFtsRecallBackend(CanonicalSessionRecallBackend):
    """Recall backend over the Session store's own FTS5 Message index."""

    def __init__(self, context: RecallBackendContext) -> None:
        super().__init__(context.sessions)
        self.data_dir = context.data_dir
        self.logger = context.logger

    @staticmethod
    @override
    def search_capabilities() -> RecallSearchCapabilities:
        query_description = (
            "Distinctive words to find, ignoring case. Every "
            "whitespace-separated term must occur. One- or "
            "two-character terms require whole-token matching for the query; otherwise terms "
            "also match inside words. Matches are ranked by text relevance."
        )
        return RecallSearchCapabilities(
            result_type="message",
            guidance=query_description,
            tool_summary=("Find text from past conversations, ranked by relevance."),
            query_description=query_description,
            match_argument="match",
            match_modes=("all_terms", "any_term", "phrase"),
            order_modes=("relevance", "newest", "oldest"),
            default_order="relevance",
            supports_roles=True,
        )

    @override
    async def search_page(self, request: RecallSearchRequest) -> RecallSearchPage:
        return await self.sessions.run_async(self._search_page, request, use_fts=True)
