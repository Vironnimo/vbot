"""One Session search specification shared by service, store and query execution."""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache, cached_property

from core.sessions._types import SessionSearchOrder
from core.sessions.search_content import LiteralSearchQuery
from core.utils.timestamps import canonical_timestamp


@cache
def _fold_boundaries() -> tuple[re.Pattern[str], tuple[str, ...], tuple[str, ...]]:
    """Spellings SQLite's simple case folding can miss in full-folded text."""
    # SQLite tokenizes whole code points. A full fold can expand (ß to ss),
    # so even just the first or last character of such a fold can match.
    # SQLite and Python also ship different Unicode case tables. Keep
    # uncased scripts (e.g. CJK) and ordinary ASCII indexable without
    # assuming SQLite knows every newer non-ASCII case pair.
    folds = {
        fold
        for n in range(128, sys.maxunicode + 1)
        if (char := chr(n)) != (fold := char.casefold())
    }
    expansions = {fold for fold in folds if len(fold) > 1}
    simple = "".join(sorted(fold for fold in folds if len(fold) == 1))
    prefixes = {fold[:i] for fold in expansions for i in range(1, len(fold))}
    suffixes = {fold[i:] for fold in expansions for i in range(1, len(fold))}
    spellings = [re.escape(fold) for fold in sorted(expansions, key=len, reverse=True)]
    pattern = re.compile("|".join([*spellings, f"[{re.escape(simple)}]"]))
    return pattern, tuple(suffixes), tuple(prefixes)


@dataclass(frozen=True)
class SessionSearchRequest:
    """Normalize and match one bounded search without depending on database state."""

    query: str
    project_id: str | None
    agent_id: str | None
    session_id: str | None = None
    match_mode: str = "all_terms"
    order: SessionSearchOrder = "relevance"
    limit: int = 1_000
    roles: Sequence[str] | None = None
    since: str | None = None
    until: str | None = None
    excluded_session_ids: Sequence[str] = ()
    include_subagents: bool = False
    use_fts: bool = True
    timeline_only: bool = False

    def __post_init__(self) -> None:
        if self.timeline_only and not (self.agent_id and self.session_id):
            raise ValueError("Timeline search requires an exact Session address")

    @cached_property
    def literal_query(self) -> LiteralSearchQuery:
        return LiteralSearchQuery(self.query, self.match_mode)

    @cached_property
    def timeline_index_query(self) -> LiteralSearchQuery:
        # Use a guaranteed substring of each term to find candidates; the
        # original literal query still decides membership, including short terms.
        expansions, left, right = _fold_boundaries()
        anchors = []
        for term in self.literal_query.folded_values:
            for piece in expansions.split(term):
                while piece.startswith(left):
                    piece = piece[1:]
                while piece.endswith(right):
                    piece = piece[:-1]
                if len(piece) >= 3:
                    anchors.append(piece)
        return LiteralSearchQuery(" ".join(anchors))

    @property
    def empty(self) -> bool:
        return (
            not self.literal_query.compact
            or self.limit <= 0
            or (self.roles is not None and not self.roles)
        )

    @property
    def tool_inclusive(self) -> bool:
        return self.roles is None or "tool" in self.roles

    def since_timestamp(self) -> str | None:
        return None if self.since is None else canonical_timestamp(self.since)

    def until_timestamp(self) -> str | None:
        return None if self.until is None else canonical_timestamp(self.until)
