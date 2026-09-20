"""Shared web_fetch configuration facts for Settings and the Tool."""

from collections.abc import Mapping
from typing import Any

WEB_FETCH_PROVIDERS = ("direct", "firecrawl", "tavily", "exa", "parallel")
WEB_FETCH_MODES = ("fallback", "prefer")
DEFAULT_WEB_FETCH_SETTINGS = {"provider": "direct", "mode": "fallback"}
WEB_FETCH_CREDENTIALS = {
    "firecrawl": "FIRECRAWL_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "exa": "EXA_API_KEY",
    "parallel": "PARALLEL_API_KEY",
}
WEB_FETCH_PRICING = {
    "firecrawl": "https://www.firecrawl.dev/pricing",
    "tavily": "https://docs.tavily.com/documentation/api-credits",
    "exa": "https://exa.ai/pricing",
    "parallel": "https://docs.parallel.ai/getting-started/pricing",
}


def parse_web_fetch_settings(value: Any, *, partial: bool = False) -> dict[str, Any]:
    """Validate the small selection contract without importing persistence."""
    if not isinstance(value, Mapping):
        raise ValueError("web_fetch must be an object")
    if unknown := set(value) - {"provider", "mode"}:
        raise ValueError("Unsupported web_fetch settings: " + ", ".join(sorted(unknown)))
    result = {} if partial else dict(DEFAULT_WEB_FETCH_SETTINGS)
    for key, allowed in (("provider", WEB_FETCH_PROVIDERS), ("mode", WEB_FETCH_MODES)):
        if key in value:
            if not isinstance(value[key], str) or value[key] not in allowed:
                raise ValueError(f"web_fetch.{key} must be one of: {', '.join(allowed)}")
            result[key] = value[key]
    return result
