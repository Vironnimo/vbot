"""Shared configuration constants for web search provider selection."""

from __future__ import annotations

WEB_SEARCH_PROVIDER_BRAVE = "brave"
WEB_SEARCH_PROVIDER_DUCKDUCKGO = "duckduckgo"
WEB_SEARCH_PROVIDER_SEARXNG = "searxng"
WEB_SEARCH_PROVIDER_TAVILY = "tavily"
WEB_SEARCH_PROVIDER_EXA = "exa"
WEB_SEARCH_PROVIDER_SERPER = "serper"
WEB_SEARCH_PROVIDER_FIRECRAWL = "firecrawl"
WEB_SEARCH_PROVIDER_PERPLEXITY = "perplexity"
DEFAULT_WEB_SEARCH_PROVIDER = WEB_SEARCH_PROVIDER_BRAVE
FIRST_PARTY_WEB_SEARCH_PROVIDERS = frozenset(
    {
        WEB_SEARCH_PROVIDER_BRAVE,
        WEB_SEARCH_PROVIDER_DUCKDUCKGO,
        WEB_SEARCH_PROVIDER_SEARXNG,
        WEB_SEARCH_PROVIDER_TAVILY,
        WEB_SEARCH_PROVIDER_EXA,
        WEB_SEARCH_PROVIDER_SERPER,
        WEB_SEARCH_PROVIDER_FIRECRAWL,
        WEB_SEARCH_PROVIDER_PERPLEXITY,
    }
)
DEFAULT_SEARXNG_BASE_URL = "http://localhost:8888"

# Result-count bounds shared by the tool schema and the settings layer. The
# default is deliberately generous for agent usage: results are cheap snippet
# tokens, and a wider first page saves follow-up searches against the provider
# quota. 20 is Brave's per-request maximum.
DEFAULT_WEB_SEARCH_COUNT = 12
MIN_WEB_SEARCH_COUNT = 1
MAX_WEB_SEARCH_COUNT = 20
# Brave paginates via a zero-based page offset capped at 9 → pages 1..10.
MAX_WEB_SEARCH_PAGE = 10

__all__ = [
    "DEFAULT_SEARXNG_BASE_URL",
    "DEFAULT_WEB_SEARCH_COUNT",
    "DEFAULT_WEB_SEARCH_PROVIDER",
    "FIRST_PARTY_WEB_SEARCH_PROVIDERS",
    "MAX_WEB_SEARCH_COUNT",
    "MAX_WEB_SEARCH_PAGE",
    "MIN_WEB_SEARCH_COUNT",
    "WEB_SEARCH_PROVIDER_BRAVE",
    "WEB_SEARCH_PROVIDER_DUCKDUCKGO",
    "WEB_SEARCH_PROVIDER_EXA",
    "WEB_SEARCH_PROVIDER_FIRECRAWL",
    "WEB_SEARCH_PROVIDER_PERPLEXITY",
    "WEB_SEARCH_PROVIDER_SERPER",
    "WEB_SEARCH_PROVIDER_SEARXNG",
    "WEB_SEARCH_PROVIDER_TAVILY",
]
