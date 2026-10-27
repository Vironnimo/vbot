"""One Session search specification shared by service, store and query execution."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property

from core.sessions._types import SessionSearchOrder
from core.sessions.search_content import LiteralSearchQuery
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
    def literal_query(self) -> LiteralSearchQuery:
        return LiteralSearchQuery(self.query, self.match_mode)

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
