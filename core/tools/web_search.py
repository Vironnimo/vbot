"""Built-in web_search tool with selectable first-party search providers."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from core.search_config import (
    DEFAULT_SEARXNG_BASE_URL,
    DEFAULT_WEB_SEARCH_PROVIDER,
    FIRST_PARTY_WEB_SEARCH_PROVIDERS,
    WEB_SEARCH_PROVIDER_SEARXNG,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.http_status import HttpRequestFailure, is_retryable_status
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.web_search")

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

_DEFAULT_COUNT = 5
_MIN_COUNT = 1
_MAX_COUNT = 20

_RETRY_MAX_RETRIES = 3
_RETRY_INITIAL_DELAY_SECONDS = 1.0
_RETRY_BACKOFF_FACTOR = 2
_RETRY_JITTER_FACTOR = 0.5
_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

_BRAVE_FRESHNESS_MAP: dict[str, str] = {
    "pd": "pd",
    "day": "pd",
    "d": "pd",
    "pw": "pw",
    "week": "pw",
    "w": "pw",
    "pm": "pm",
    "month": "pm",
    "m": "pm",
    "py": "py",
    "year": "py",
    "y": "py",
}
_SEARXNG_TIME_RANGE_MAP: dict[str, str] = {
    "pd": "day",
    "day": "day",
    "d": "day",
    "pm": "month",
    "month": "month",
    "m": "month",
    "py": "year",
    "year": "year",
    "y": "year",
}

_ALLOWED_ARGUMENTS = frozenset({"query", "count", "freshness", "date_after", "date_before"})

WEB_SEARCH_TOOL_NAME = "web_search"
WEB_SEARCH_TOOL_DESCRIPTION = (
    "Search the public web using the configured search provider and return "
    "structured results with title, url, and description."
)
WEB_SEARCH_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Search query text.",
        },
        "count": {
            "type": "integer",
            "description": "Maximum number of results to return (1-20).",
            "minimum": _MIN_COUNT,
            "maximum": _MAX_COUNT,
            "default": _DEFAULT_COUNT,
        },
        "freshness": {
            "type": "string",
            "description": (
                "Optional recency filter. Supports day/week/month/year (or d/w/m/y, "
                "pd/pw/pm/py) or YYYY-MM-DDtoYYYY-MM-DD."
            ),
        },
        "date_after": {
            "type": "string",
            "description": "Optional lower date bound (YYYY-MM-DD).",
        },
        "date_before": {
            "type": "string",
            "description": "Optional upper date bound (YYYY-MM-DD).",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


def _normalize_text(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def _normalize_count(raw: Any) -> int:
    resolved = _DEFAULT_COUNT

    if isinstance(raw, bool):
        resolved = _DEFAULT_COUNT
    elif isinstance(raw, int):
        resolved = raw
    elif isinstance(raw, float) and raw.is_integer():
        resolved = int(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if text:
            try:
                resolved = int(text)
            except ValueError:
                resolved = _DEFAULT_COUNT

    if resolved < _MIN_COUNT:
        return _MIN_COUNT
    if resolved > _MAX_COUNT:
        return _MAX_COUNT
    return resolved


def _normalize_date(raw: Any, field_name: str) -> tuple[str, str | None]:
    text = _normalize_text(raw)
    if not text:
        return "", None

    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return "", f"{field_name} must be in YYYY-MM-DD format"

    return parsed.isoformat(), None


def _parse_date_range_token(value: str) -> tuple[str, str] | None:
    compact = value.strip().replace(" ", "")
    if "to" not in compact:
        return None

    start_raw, end_raw = compact.split("to", 1)
    start_raw = start_raw.strip()
    end_raw = end_raw.strip()
    if not start_raw or not end_raw:
        return None

    start_date, start_error = _normalize_date(start_raw, "freshness")
    end_date, end_error = _normalize_date(end_raw, "freshness")
    if start_error is not None or end_error is not None:
        return None
    if start_date > end_date:
        return None
    return start_date, end_date


def _build_brave_filters(
    freshness: str,
    date_after: str,
    date_before: str,
) -> tuple[dict[str, str], list[str], str | None]:
    warnings: list[str] = []
    filters: dict[str, str] = {}

    if date_after and date_before:
        filters["freshness"] = f"{date_after}to{date_before}"
        if freshness:
            warnings.append("freshness ignored because date_after/date_before were provided")
        return filters, warnings, None

    if date_after or date_before:
        warnings.append(
            "brave applies date filters only when both date_after and date_before are set"
        )

    if not freshness:
        return filters, warnings, None

    mapped = _BRAVE_FRESHNESS_MAP.get(freshness)
    if mapped is not None:
        filters["freshness"] = mapped
        return filters, warnings, None

    parsed_range = _parse_date_range_token(freshness)
    if parsed_range is not None:
        start_date, end_date = parsed_range
        filters["freshness"] = f"{start_date}to{end_date}"
        return filters, warnings, None

    return (
        filters,
        warnings,
        "freshness must be one of day/week/month/year (or d/w/m/y, pd/pw/pm/py) "
        "or YYYY-MM-DDtoYYYY-MM-DD",
    )


def _build_searxng_filters(
    freshness: str,
    date_after: str,
    date_before: str,
) -> tuple[dict[str, str], list[str], str | None]:
    warnings: list[str] = []
    filters: dict[str, str] = {}

    if date_after or date_before:
        warnings.append("searxng does not support exact date ranges; date filters ignored")

    if not freshness:
        return filters, warnings, None

    mapped = _SEARXNG_TIME_RANGE_MAP.get(freshness)
    if mapped is not None:
        filters["time_range"] = mapped
        return filters, warnings, None

    if freshness in {"pw", "week", "w"}:
        warnings.append("searxng does not support week time_range; freshness ignored")
        return filters, warnings, None

    parsed_range = _parse_date_range_token(freshness)
    if parsed_range is not None:
        warnings.append("searxng does not support exact date ranges; freshness ignored")
        return filters, warnings, None

    return (
        filters,
        warnings,
        "freshness must be one of day/week/month/year (or d/w/m/y, pd/pw/pm/py) "
        "or YYYY-MM-DDtoYYYY-MM-DD",
    )


def _standardize_results(raw_results: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_results, list):
        return []

    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_results, start=1):
        if not isinstance(raw, dict):
            continue

        title = _normalize_text(raw.get("title"))
        url = _normalize_text(raw.get("url"))
        description = _normalize_text(raw.get("description"))
        if not title and not url and not description:
            continue

        normalized.append(
            {
                "rank": index,
                "title": title,
                "url": url,
                "description": description,
                "content_trust": "untrusted_web_content",
            }
        )

    return normalized


def _standardize_searxng_results(raw_results: Any, count: int) -> list[dict[str, Any]]:
    if not isinstance(raw_results, list):
        return []

    normalized: list[dict[str, Any]] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue

        title = _normalize_text(raw.get("title"))
        url = _normalize_text(raw.get("url"))
        description = _normalize_text(raw.get("content"))
        if not description:
            description = _normalize_text(raw.get("description"))
        if not title and not url and not description:
            continue

        normalized.append(
            {
                "rank": len(normalized) + 1,
                "title": title,
                "url": url,
                "description": description,
                "content_trust": "untrusted_web_content",
            }
        )
        if len(normalized) >= count:
            break

    return normalized


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            message = _normalize_text(detail.get("error", detail.get("message")))
            if message:
                return message

        message = _normalize_text(payload.get("error", payload.get("message")))
        if message:
            return message

    fallback = _normalize_text(response.text)
    if fallback:
        return fallback[:300]
    return response.reason_phrase or "request failed"


async def _sleep_for_retry(attempt: int) -> None:
    base_delay = _RETRY_INITIAL_DELAY_SECONDS * (_RETRY_BACKOFF_FACTOR**attempt)
    jitter = random.uniform(0, base_delay * _RETRY_JITTER_FACTOR)
    await asyncio.sleep(base_delay + jitter)


async def _search_brave(
    *,
    api_key: str,
    query: str,
    count: int,
    freshness: str,
    date_after: str,
    date_before: str,
) -> tuple[dict[str, Any] | None, HttpRequestFailure | None]:
    filters, warnings, filter_error = _build_brave_filters(freshness, date_after, date_before)
    if filter_error is not None:
        return None, HttpRequestFailure(filter_error)

    params: dict[str, Any] = {"q": query, "count": count}
    params.update(filters)

    async with httpx.AsyncClient(headers=_BROWSER_HEADERS, timeout=_REQUEST_TIMEOUT) as client:
        for attempt in range(_RETRY_MAX_RETRIES + 1):
            try:
                response = await client.get(
                    _BRAVE_ENDPOINT,
                    params=params,
                    headers={"X-Subscription-Token": api_key},
                )
            except httpx.RequestError as error:
                if attempt >= _RETRY_MAX_RETRIES:
                    _LOGGER.warning("Brave web search request failed: %s", error)
                    return None, HttpRequestFailure(
                        f"request failed: {error}",
                        retryable=True,
                        attempts_made=_RETRY_MAX_RETRIES + 1,
                    )
                await _sleep_for_retry(attempt)
                continue

            if response.status_code >= 400:
                # GET is idempotent — safe to repeat (includes a transient 500).
                if (
                    is_retryable_status(response.status_code, idempotent=True)
                    and attempt < _RETRY_MAX_RETRIES
                ):
                    await _sleep_for_retry(attempt)
                    continue
                detail = _extract_error_detail(response)
                _LOGGER.warning(
                    "Brave web search request failed: HTTP %s: %s",
                    response.status_code,
                    detail,
                )
                # A retryable status only reaches here after retries were exhausted.
                retryable = is_retryable_status(response.status_code, idempotent=True)
                return None, HttpRequestFailure(
                    f"HTTP {response.status_code}: {detail}",
                    retryable=retryable,
                    attempts_made=(_RETRY_MAX_RETRIES + 1) if retryable else None,
                )

            try:
                payload = response.json()
            except ValueError:
                return None, HttpRequestFailure("provider returned invalid JSON")

            raw_results = None
            if isinstance(payload, dict):
                web_payload = payload.get("web")
                if isinstance(web_payload, dict):
                    raw_results = web_payload.get("results")

            results = _standardize_results(raw_results)
            normalized_payload: dict[str, Any] = {
                "provider": "brave",
                "query": query,
                "count_requested": count,
                "result_count": len(results),
                "results": results,
                "content_trust": "untrusted_web_content",
            }
            if filters:
                normalized_payload["filters"] = filters
            if warnings:
                normalized_payload["warnings"] = warnings
            if isinstance(payload, dict) and isinstance(payload.get("query"), dict):
                more_results = payload.get("query", {}).get("more_results_available")
                if isinstance(more_results, bool):
                    normalized_payload["more_results_available"] = more_results

            return normalized_payload, None

    return None, HttpRequestFailure("request failed")


def _build_searxng_endpoint(base_url: str) -> tuple[str | None, str | None]:
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, "SearXNG base_url must be an http or https URL"

    base_path = parsed.path.rstrip("/")
    search_path = f"{base_path}/search" if base_path else "/search"
    endpoint = urlunsplit((parsed.scheme, parsed.netloc, search_path, "", ""))
    return endpoint, None


async def _search_searxng(
    *,
    base_url: str,
    query: str,
    count: int,
    freshness: str,
    date_after: str,
    date_before: str,
) -> tuple[dict[str, Any] | None, HttpRequestFailure | None]:
    endpoint, endpoint_error = _build_searxng_endpoint(base_url)
    if endpoint_error is not None:
        return None, HttpRequestFailure(endpoint_error)
    if endpoint is None:
        return None, HttpRequestFailure("SearXNG endpoint could not be built")

    filters, warnings, filter_error = _build_searxng_filters(freshness, date_after, date_before)
    if filter_error is not None:
        return None, HttpRequestFailure(filter_error)

    params: dict[str, Any] = {
        "q": query,
        "format": "json",
        "pageno": 1,
        "safesearch": 0,
        "categories": "general",
    }
    params.update(filters)

    async with httpx.AsyncClient(headers=_BROWSER_HEADERS, timeout=_REQUEST_TIMEOUT) as client:
        for attempt in range(_RETRY_MAX_RETRIES + 1):
            try:
                response = await client.get(endpoint, params=params)
            except httpx.RequestError as error:
                if attempt >= _RETRY_MAX_RETRIES:
                    _LOGGER.warning("SearXNG web search request failed: %s", error)
                    return None, HttpRequestFailure(
                        f"request failed: {error}",
                        retryable=True,
                        attempts_made=_RETRY_MAX_RETRIES + 1,
                    )
                await _sleep_for_retry(attempt)
                continue

            if response.status_code >= 400:
                # GET is idempotent — safe to repeat (includes a transient 500).
                if (
                    is_retryable_status(response.status_code, idempotent=True)
                    and attempt < _RETRY_MAX_RETRIES
                ):
                    await _sleep_for_retry(attempt)
                    continue
                detail = _extract_error_detail(response)
                if response.status_code == 403:
                    detail = f"{detail}; ensure SearXNG search formats include json"
                _LOGGER.warning(
                    "SearXNG web search request failed: HTTP %s: %s",
                    response.status_code,
                    detail,
                )
                # A retryable status only reaches here after retries were exhausted.
                retryable = is_retryable_status(response.status_code, idempotent=True)
                return None, HttpRequestFailure(
                    f"HTTP {response.status_code}: {detail}",
                    retryable=retryable,
                    attempts_made=(_RETRY_MAX_RETRIES + 1) if retryable else None,
                )

            try:
                payload = response.json()
            except ValueError:
                return None, HttpRequestFailure("provider returned invalid JSON")

            raw_results = payload.get("results") if isinstance(payload, dict) else None
            results = _standardize_searxng_results(raw_results, count)
            normalized_payload: dict[str, Any] = {
                "provider": WEB_SEARCH_PROVIDER_SEARXNG,
                "query": query,
                "count_requested": count,
                "result_count": len(results),
                "results": results,
                "content_trust": "untrusted_web_content",
            }
            if filters:
                normalized_payload["filters"] = filters
            if warnings:
                normalized_payload["warnings"] = warnings
            return normalized_payload, None

    return None, HttpRequestFailure("request failed")


def _normalize_web_search_settings(raw_settings: Any) -> tuple[dict[str, Any] | None, str | None]:
    if raw_settings is None:
        raw_settings = {}
    if not isinstance(raw_settings, Mapping):
        return None, "web_search settings must be an object"

    provider = raw_settings.get("provider", DEFAULT_WEB_SEARCH_PROVIDER)
    if not isinstance(provider, str) or provider not in FIRST_PARTY_WEB_SEARCH_PROVIDERS:
        allowed = ", ".join(sorted(FIRST_PARTY_WEB_SEARCH_PROVIDERS))
        return None, f"web_search provider must be one of: {allowed}"

    searxng = raw_settings.get("searxng", {})
    if searxng is None:
        searxng = {}
    if not isinstance(searxng, Mapping):
        return None, "web_search.searxng must be an object"

    base_url = searxng.get("base_url", DEFAULT_SEARXNG_BASE_URL)
    if not isinstance(base_url, str) or not base_url.strip():
        return None, "web_search.searxng.base_url must be a non-empty string"

    return {
        "provider": provider,
        "searxng": {"base_url": base_url.strip()},
    }, None


def _resolve_web_search_settings(
    settings_resolver: Callable[[], Mapping[str, Any]] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    if settings_resolver is None:
        return _normalize_web_search_settings(None)

    try:
        raw_settings = settings_resolver()
    except Exception as error:
        _LOGGER.error("web_search settings resolver crashed unexpectedly", exc_info=error)
        return None, f"web_search settings could not be loaded: {error}"
    return _normalize_web_search_settings(raw_settings)


async def web_search_handler(
    context: ToolContext,
    arguments: JsonObject,
    credential_resolver: Callable[[str], str],
    settings_resolver: Callable[[], Mapping[str, Any]] | None = None,
) -> JsonObject:
    """Handle a web_search tool call in the stable vBot envelope."""
    del context

    unknown_arguments = sorted(set(arguments) - _ALLOWED_ARGUMENTS)
    if unknown_arguments:
        names = ", ".join(unknown_arguments)
        return tool_failure("validation_error", f"Unknown argument(s): {names}", retryable=False)

    query = _normalize_text(arguments.get("query"))
    if not query:
        return tool_failure("validation_error", "query must be a non-empty string", retryable=False)

    if "count" in arguments:
        raw_count = arguments.get("count")
        if not isinstance(raw_count, int) or isinstance(raw_count, bool):
            return tool_failure("validation_error", "count must be an integer", retryable=False)
        if raw_count < _MIN_COUNT or raw_count > _MAX_COUNT:
            return tool_failure(
                "validation_error",
                f"count must be between {_MIN_COUNT} and {_MAX_COUNT}",
                retryable=False,
            )
    else:
        raw_count = _DEFAULT_COUNT
    count = _normalize_count(raw_count)

    freshness = _normalize_text(arguments.get("freshness")).lower()
    date_after, after_error = _normalize_date(arguments.get("date_after"), "date_after")
    if after_error is not None:
        return tool_failure("validation_error", after_error, retryable=False)

    date_before, before_error = _normalize_date(arguments.get("date_before"), "date_before")
    if before_error is not None:
        return tool_failure("validation_error", before_error, retryable=False)

    if date_after and date_before and date_after > date_before:
        return tool_failure(
            "validation_error", "date_after must be on or before date_before", retryable=False
        )

    _, _, filter_error = _build_brave_filters(freshness, date_after, date_before)
    if filter_error is not None:
        return tool_failure("validation_error", filter_error, retryable=False)

    settings, settings_error = _resolve_web_search_settings(settings_resolver)
    if settings_error is not None:
        return tool_failure("configuration_error", settings_error, retryable=False)
    if settings is None:
        return tool_failure(
            "configuration_error", "web search settings could not be resolved", retryable=False
        )

    provider = settings["provider"]
    if provider == WEB_SEARCH_PROVIDER_SEARXNG:
        payload, search_failure = await _search_searxng(
            base_url=settings["searxng"]["base_url"],
            query=query,
            count=count,
            freshness=freshness,
            date_after=date_after,
            date_before=date_before,
        )
        return _search_result_envelope(payload, search_failure)

    api_key = _normalize_text(credential_resolver("BRAVE_API_KEY"))
    if not api_key:
        return tool_failure(
            "missing_api_key",
            "web_search requires BRAVE_API_KEY to be configured",
            retryable=False,
        )

    payload, search_failure = await _search_brave(
        api_key=api_key,
        query=query,
        count=count,
        freshness=freshness,
        date_after=date_after,
        date_before=date_before,
    )
    return _search_result_envelope(payload, search_failure)


def _search_result_envelope(
    payload: dict[str, Any] | None,
    failure: HttpRequestFailure | None,
) -> JsonObject:
    """Map a provider search outcome onto the stable tool result envelope."""
    if failure is not None:
        return tool_failure(
            "provider_request_failed",
            failure.message,
            retryable=failure.retryable,
            attempts_made=failure.attempts_made,
        )
    if payload is None:
        return tool_failure("provider_request_failed", "web search failed", retryable=False)
    return tool_success(payload)


def register_web_search_tool(
    registry: ToolRegistry,
    credential_resolver: Callable[[str], str],
    settings_resolver: Callable[[], Mapping[str, Any]] | None = None,
) -> None:
    """Register the configurable web_search tool with a vBot tool registry."""

    async def _handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await web_search_handler(
            context,
            arguments,
            credential_resolver,
            settings_resolver,
        )

    registry.register(
        WEB_SEARCH_TOOL_NAME,
        WEB_SEARCH_TOOL_DESCRIPTION,
        WEB_SEARCH_TOOL_PARAMETERS,
        _handler,
        display=ToolDisplay(summary_fields=("query",)),
    )


__all__ = [
    "WEB_SEARCH_TOOL_DESCRIPTION",
    "WEB_SEARCH_TOOL_NAME",
    "WEB_SEARCH_TOOL_PARAMETERS",
    "register_web_search_tool",
    "web_search_handler",
]
