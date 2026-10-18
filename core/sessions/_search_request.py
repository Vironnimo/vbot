"""One Session search specification shared by service, store and query execution."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property

from core.sessions._types import SessionSearchOrder
from core.utils.timestamps import canonical_timestamp


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

    @cached_property
    def compact(self) -> str:
        return re.sub(r"\s+", " ", self.query).strip() if self.query else ""

    @cached_property
    def _folded(self) -> str:
        return self.compact.casefold()

    @cached_property
    def _folded_terms(self) -> tuple[str, ...]:
        return tuple(term for term in self._folded.split(" ") if term)

    @property
    def empty(self) -> bool:
        return not self.compact or self.limit <= 0 or (self.roles is not None and not self.roles)

    def matches(self, text: str) -> bool:
        haystack = " ".join(text.split()).casefold()
        folded = self._folded
        if self.match_mode == "phrase":
            return folded in haystack
        terms = self._folded_terms
        if self.match_mode == "any_term":
            return any(term in haystack for term in terms)
        return all(term in haystack for term in terms)

    @property
    def tool_inclusive(self) -> bool:
        return self.roles is None or "tool" in self.roles

    @property
    def trigram_supported(self) -> bool:
        if self.match_mode == "phrase":
            return len(self.compact) >= 3
        terms = [term for term in self.compact.split(" ") if term]
        return bool(terms) and all(len(term) >= 3 for term in terms)

    def fts_expression(self) -> str:
        """Keep Unicode spelling for SQLite; case folding belongs to literal matching."""
        if self.match_mode == "phrase":
            expression = '"' + self.compact.replace('"', '""') + '"'
        else:
            expression = (" OR " if self.match_mode == "any_term" else " AND ").join(
                '"' + term.replace('"', '""') + '"' for term in self.compact.split(" ") if term
            )
        return "{content search_text} : (" + expression + ")"

    def since_timestamp(self) -> str | None:
        return None if self.since is None else canonical_timestamp(self.since)

    def until_timestamp(self) -> str | None:
        return None if self.until is None else canonical_timestamp(self.until)
