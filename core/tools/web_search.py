"""Built-in web_search tool with selectable first-party search providers."""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx
import idna

from core.search_config import (
    DEFAULT_SEARXNG_BASE_URL,
    DEFAULT_WEB_SEARCH_COUNT,
    DEFAULT_WEB_SEARCH_PROVIDER,
    FIRST_PARTY_WEB_SEARCH_PROVIDERS,
    MAX_WEB_SEARCH_COUNT,
    MAX_WEB_SEARCH_PAGE,
    MIN_WEB_SEARCH_COUNT,
    WEB_SEARCH_PROVIDER_DUCKDUCKGO,
    WEB_SEARCH_PROVIDER_EXA,
    WEB_SEARCH_PROVIDER_FIRECRAWL,
    WEB_SEARCH_PROVIDER_SEARXNG,
    WEB_SEARCH_PROVIDER_SERPER,
    WEB_SEARCH_PROVIDER_TAVILY,
)
from core.tools.arguments import ToolArgumentError, optional_int
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayField,
    ToolRegistry,
    result_count_fact_builder,
    tool_failure,
    tool_success,
)
from core.utils.http_status import HttpRequestFailure, is_retryable_status, parse_retry_after
from core.utils.logging import get_logger
from core.utils.retry import MAX_RETRIES, sleep_for_retry

_LOGGER = get_logger("tools.web_search")

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_DUCKDUCKGO_ENDPOINT = "https://html.duckduckgo.com/html"
_EXA_ENDPOINT = "https://api.exa.ai/search"
_FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v2/search"
_SERPER_ENDPOINT = "https://google.serper.dev/search"
_TAVILY_ENDPOINT = "https://api.tavily.com/search"

_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_MAX_RESPONSE_SIZE_LABEL = "5 MB"

_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_DDG_RESULT_ANCHOR_PATTERN = re.compile(
    r'<a\b(?=[^>]*\bclass="[^"]*\bresult__a\b[^"]*")([^>]*)>([\s\S]*?)</a>',
    re.IGNORECASE,
)
_DDG_NEXT_RESULT_PATTERN = re.compile(
    r'<a\b(?=[^>]*\bclass="[^"]*\bresult__a\b[^"]*")[^>]*>',
    re.IGNORECASE,
)
_DDG_SNIPPET_PATTERN = re.compile(
    r'<a\b(?=[^>]*\bclass="[^"]*\bresult__snippet\b[^"]*")[^>]*>'
    r"([\s\S]*?)</a>",
    re.IGNORECASE,
)
_DDG_HREF_PATTERN = re.compile(r'\bhref="([^"]*)"', re.IGNORECASE)
_DDG_CHALLENGE_PATTERN = re.compile(
    r'g-recaptcha|are you a human|id="challenge-form"|name="challenge"',
    re.IGNORECASE,
)
_DOMAIN_LABEL_PATTERN = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)\Z")

_MAX_DOMAIN_FILTERS = 10

_SEARXNG_DOMAIN_WARNING = (
    "domain-filter completeness depends on the configured SearXNG engines; "
    "returned results are still restricted to applied_domains"
)
_SEARXNG_RECENCY_WARNING = "recency enforcement depends on the configured SearXNG engines"
_SEARXNG_PAGINATION_WARNING = (
    "SearXNG page size is instance-configured and may exceed count; "
    "results between pages may be unreachable"
)
_BRAVE_DOMAIN_PAGING_WARNING = (
    "more_results_available is omitted with domain filters because it reflects "
    "the unfiltered result space; paging may return empty pages"
)
_TAVILY_PAGINATION_WARNING = (
    "tavily does not support result paging; results are always the first page"
)
_EXA_PAGINATION_WARNING = "exa does not support result paging; results are always the first page"
_EXA_RECENCY_WARNING = "exa recency filtering may exclude results without a published date"
_EXA_RECENCY_WINDOW_DAYS = {"day": 1, "month": 30, "year": 365}
_FIRECRAWL_PAGINATION_WARNING = (
    "firecrawl does not support result paging; results are always the first page"
)
_FIRECRAWL_RECENCY_MAP = {"day": "qdr:d", "month": "qdr:m", "year": "qdr:y"}
_SERPER_PAGE_SIZE = 10
# DuckDuckGo has no search API: the html endpoint is fetched with a browser
# user agent and the result anchors are parsed (same approach as OpenClaw's
# duckduckgo extension). Safe search stays on the documented moderate default.
_DUCKDUCKGO_SAFE_SEARCH = "-1"
# DuckDuckGo answers HTTP 202 with an empty page when it rate-limits.
_DUCKDUCKGO_RATE_LIMIT_STATUS = 202
_DUCKDUCKGO_RECENCY_WARNING = (
    "duckduckgo does not support recency filtering; results are unfiltered by age"
)
_DUCKDUCKGO_PAGINATION_WARNING = (
    "duckduckgo serves a single result page; pages beyond the fetched results are empty"
)
_SERPER_MAX_PAGES_PER_CALL = 5
_SERPER_RECENCY_MAP = {"day": "qdr:d", "month": "qdr:m", "year": "qdr:y"}

_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

_RECENCY_VALUES = ("day", "month", "year")
_BRAVE_RECENCY_MAP: dict[str, str] = {
    "day": "pd",
    "month": "pm",
    "year": "py",
}

_ALLOWED_ARGUMENTS = frozenset({"query", "domains", "count", "page", "recency"})

WEB_SEARCH_TOOL_NAME = "web_search"
WEB_SEARCH_TOOL_DESCRIPTION = (
    "Search the public web using the configured provider. Returns structured "
    "results with title, URL, short description, and page age when available."
)
WEB_SEARCH_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "description": "Search query; provider search operators are passed through.",
        },
        "domains": {
            "type": "array",
            "description": (
                "Restrict results to these hostnames and their subdomains. Use hostnames "
                "without a scheme or path."
            ),
            "items": {"type": "string", "minLength": 1, "maxLength": 253},
            "minItems": 1,
            "maxItems": _MAX_DOMAIN_FILTERS,
            "uniqueItems": True,
        },
        "count": {
            "type": "integer",
            "description": "Maximum results to return. Omit to use the configured default.",
            "minimum": MIN_WEB_SEARCH_COUNT,
            "maximum": MAX_WEB_SEARCH_COUNT,
        },
        "page": {
            "type": "integer",
            "description": (
                "Result page to fetch. Request the next page when more results are "
                "available. Page size is provider-dependent and may exceed count."
            ),
            "minimum": 1,
            "maximum": MAX_WEB_SEARCH_PAGE,
            "default": 1,
        },
        "recency": {
            "type": "string",
            "enum": list(_RECENCY_VALUES),
            "description": (
                "Maximum result age: day, month, or year. Omit for no recency restriction."
            ),
        },
    },
    "required": ["query"],
}


class _ResponseTooLargeError(Exception):
    """Raised before a search response can exceed its in-memory limit."""


def _declared_response_size(headers: Mapping[str, str]) -> int | None:
    """Return a valid declared body size, when the provider sent one."""
    value = headers.get("content-length")
    if value is None:
        return None
    try:
        size = int(value)
    except ValueError:
        return None
    return size if size >= 0 else None


async def _bounded_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """Stream one GET into a bounded buffer before exposing JSON helpers."""
    async with client.stream("GET", url, params=params, headers=headers) as response:
        declared_size = _declared_response_size(response.headers)
        if declared_size is not None and declared_size > _MAX_RESPONSE_BYTES:
            raise _ResponseTooLargeError(
                f"provider response exceeds the {_MAX_RESPONSE_SIZE_LABEL} limit"
            )

        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                raise _ResponseTooLargeError(
                    f"provider response exceeds the {_MAX_RESPONSE_SIZE_LABEL} limit"
                )
            body.extend(chunk)

        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=bytes(body),
            request=response.request,
            extensions=response.extensions,
        )


async def _bounded_post(
    client: httpx.AsyncClient,
    url: str,
    *,
    payload: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """Stream one JSON POST into a bounded buffer before exposing JSON helpers."""
    async with client.stream("POST", url, json=dict(payload), headers=headers) as response:
        declared_size = _declared_response_size(response.headers)
        if declared_size is not None and declared_size > _MAX_RESPONSE_BYTES:
            raise _ResponseTooLargeError(
                f"provider response exceeds the {_MAX_RESPONSE_SIZE_LABEL} limit"
            )

        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                raise _ResponseTooLargeError(
                    f"provider response exceeds the {_MAX_RESPONSE_SIZE_LABEL} limit"
                )
            body.extend(chunk)

        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=bytes(body),
            request=response.request,
            extensions=response.extensions,
        )


async def _post_json_bounded(
    url: str,
    *,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    provider_label: str,
    auth_key_hint: str | None = None,
    extra_retryable_statuses: Collection[int] | None = None,
) -> tuple[Any | None, HttpRequestFailure | None]:
    """POST one JSON search body and decode the response through shared policy.

    Search POSTs are side-effect-free but billed per attempt, so only the
    shared transient set (429/502/503/504, never 500) is retried — the same
    rule ``is_retryable_status`` encodes for non-idempotent requests.
    """
    async with httpx.AsyncClient(headers=_BROWSER_HEADERS, timeout=_REQUEST_TIMEOUT) as client:
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await _bounded_post(client, url, payload=payload, headers=headers)
            except httpx.RequestError as error:
                if attempt >= MAX_RETRIES:
                    _LOGGER.warning("%s web search request failed: %s", provider_label, error)
                    return None, HttpRequestFailure(
                        f"request failed: {error}",
                        retryable=True,
                        attempts_made=MAX_RETRIES + 1,
                    )
                await sleep_for_retry(attempt)
                continue

            if response.status_code >= 400:
                if (
                    is_retryable_status(
                        response.status_code,
                        idempotent=False,
                        extra=extra_retryable_statuses,
                    )
                    and attempt < MAX_RETRIES
                ):
                    await sleep_for_retry(attempt, parse_retry_after(response.headers))
                    continue
                detail = _extract_error_detail(response)
                if response.status_code in {401, 403} and auth_key_hint:
                    detail = f"{detail}; check {auth_key_hint}"
                _LOGGER.warning(
                    "%s web search request failed: HTTP %s: %s",
                    provider_label,
                    response.status_code,
                    detail,
                )
                retryable = is_retryable_status(
                    response.status_code,
                    idempotent=False,
                    extra=extra_retryable_statuses,
                )
                return None, HttpRequestFailure(
                    f"HTTP {response.status_code}: {detail}",
                    retryable=retryable,
                    attempts_made=(MAX_RETRIES + 1) if retryable else None,
                )

            try:
                return response.json(), None
            except ValueError:
                return None, HttpRequestFailure("provider returned invalid JSON")

    return None, HttpRequestFailure("request failed")


def _normalize_text(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def _canonicalize_domain(raw: str) -> tuple[str | None, str | None]:
    text = raw.strip().rstrip(".").lower()
    if not text:
        return None, "must be a non-empty domain"

    try:
        domain = idna.encode(text, uts46=True, std3_rules=True).decode("ascii")
    except idna.IDNAError:
        return None, "must be a valid domain"

    if len(domain) > 253:
        return None, "must be at most 253 characters"

    labels = domain.split(".")
    if any(_DOMAIN_LABEL_PATTERN.fullmatch(label) is None for label in labels):
        return None, "must be a hostname without a scheme, port, path, query, or wildcard"

    return domain, None


def _normalize_domains(raw: Any) -> tuple[list[str], str | None]:
    if raw is None:
        return [], None

    if not isinstance(raw, list):
        return [], "domains must be an array of domain strings"
    if not raw:
        return [], "domains must contain at least one domain when provided"
    if len(raw) > _MAX_DOMAIN_FILTERS:
        return [], f"domains must contain at most {_MAX_DOMAIN_FILTERS} domains"

    normalized: list[str] = []
    seen: set[str] = set()
    for index, raw_domain in enumerate(raw):
        if not isinstance(raw_domain, str):
            return [], f"domains[{index}] must be a string"
        domain, error = _canonicalize_domain(raw_domain)
        if error is not None or domain is None:
            return [], f"domains[{index}] {error or 'must be a valid domain'}"
        if domain not in seen:
            seen.add(domain)
            normalized.append(domain)

    return normalized, None


def _build_search_query(query: str, domains: list[str]) -> str:
    if not domains:
        return query
    domain_expression = " OR ".join(f"site:{domain}" for domain in domains)
    return f"{query} {domain_expression}"


def _url_matches_domains(url: str, domains: list[str]) -> bool:
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return False
    if hostname is None:
        return False

    canonical_hostname, error = _canonicalize_domain(hostname)
    if error is not None or canonical_hostname is None:
        return False
    return any(
        canonical_hostname == domain or canonical_hostname.endswith(f".{domain}")
        for domain in domains
    )


def _restrict_results_to_domains(
    results: list[dict[str, Any]],
    domains: list[str],
    count: int,
) -> list[dict[str, Any]]:
    if not domains:
        return results[:count]
    return [
        result
        for result in results
        if _url_matches_domains(_normalize_text(result.get("url")), domains)
    ][:count]


def _clean_snippet(raw: Any) -> str:
    """Normalize provider display text: drop HTML tags and unescape entities.

    Brave decorates titles/descriptions with highlight markup and HTML
    entities; that is pure noise for a model, so result text is flattened to
    plain text before it enters the envelope.
    """
    text = _normalize_text(raw)
    if not text:
        return ""
    return html.unescape(_HTML_TAG_PATTERN.sub("", text)).strip()


def _decode_duckduckgo_url(raw_href: Any) -> str:
    """Unwrap a DuckDuckGo redirect link to the direct target URL."""
    href = html.unescape(_normalize_text(raw_href))
    if not href:
        return ""
    prefixed = f"https:{href}" if href.startswith("//") else href
    try:
        pairs = parse_qsl(urlsplit(prefixed).query, keep_blank_values=True)
    except ValueError:
        return href
    for name, value in pairs:
        if name == "uddg" and value:
            return value
    return href


def _is_duckduckgo_challenge(html_text: str) -> bool:
    """Detect a bot-detection page, which carries no result anchors."""
    if _DDG_RESULT_ANCHOR_PATTERN.search(html_text):
        return False
    return _DDG_CHALLENGE_PATTERN.search(html_text) is not None


def _parse_duckduckgo_results(html_text: str) -> list[dict[str, str]]:
    """Parse result anchors and their snippets from a DDG html response."""
    results: list[dict[str, str]] = []
    for match in _DDG_RESULT_ANCHOR_PATTERN.finditer(html_text):
        raw_attributes = match.group(1) or ""
        raw_title = match.group(2) or ""
        href_match = _DDG_HREF_PATTERN.search(raw_attributes)
        raw_href = href_match.group(1) if href_match else ""
        trailing = html_text[match.end() :]
        next_result = _DDG_NEXT_RESULT_PATTERN.search(trailing)
        scope = trailing[: next_result.start()] if next_result else trailing
        snippet_match = _DDG_SNIPPET_PATTERN.search(scope)
        raw_snippet = snippet_match.group(1) if snippet_match else ""
        title = _clean_snippet(raw_title)
        url = _decode_duckduckgo_url(raw_href)
        if title and url:
            results.append(
                {
                    "title": title,
                    "url": url,
                    "description": _clean_snippet(raw_snippet),
                }
            )
    return results


def _normalize_recency(raw: Any) -> tuple[str, str | None]:
    if raw is None:
        return "", None
    if not isinstance(raw, str) or raw not in _RECENCY_VALUES:
        return "", f"recency must be one of: {', '.join(_RECENCY_VALUES)}"
    return raw, None


def _standardize_results(raw_results: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_results, list):
        return []

    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_results, start=1):
        if not isinstance(raw, dict):
            continue

        title = _clean_snippet(raw.get("title"))
        url = _normalize_text(raw.get("url"))
        description = _clean_snippet(raw.get("description"))
        if not title and not url and not description:
            continue

        entry: dict[str, Any] = {
            "rank": index,
            "title": title,
            "url": url,
            "description": description,
            "content_trust": "untrusted_web_content",
        }
        page_age = _normalize_text(raw.get("page_age")) or _normalize_text(raw.get("age"))
        if page_age:
            entry["page_age"] = page_age
        normalized.append(entry)

    return normalized


def _standardize_searxng_results(raw_results: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_results, list):
        return []

    normalized: list[dict[str, Any]] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue

        title = _clean_snippet(raw.get("title"))
        url = _normalize_text(raw.get("url"))
        description = _clean_snippet(raw.get("content"))
        if not description:
            description = _clean_snippet(raw.get("description"))
        if not title and not url and not description:
            continue

        entry: dict[str, Any] = {
            "rank": len(normalized) + 1,
            "title": title,
            "url": url,
            "description": description,
            "content_trust": "untrusted_web_content",
        }
        page_age = _normalize_text(raw.get("publishedDate"))
        if page_age:
            entry["page_age"] = page_age
        normalized.append(entry)

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


async def _search_brave(
    *,
    api_key: str,
    query: str,
    domains: list[str],
    count: int,
    page: int,
    recency: str,
) -> tuple[dict[str, Any] | None, HttpRequestFailure | None]:
    search_query = _build_search_query(query, domains)

    # text_decorations off: Brave otherwise wraps snippets in highlight markup.
    params: dict[str, Any] = {"q": search_query, "count": count, "text_decorations": "false"}
    if page > 1:
        # Brave paginates with a zero-based page offset in units of `count`.
        params["offset"] = page - 1
    if recency:
        params["freshness"] = _BRAVE_RECENCY_MAP[recency]

    async with httpx.AsyncClient(headers=_BROWSER_HEADERS, timeout=_REQUEST_TIMEOUT) as client:
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await _bounded_get(
                    client,
                    _BRAVE_ENDPOINT,
                    params=params,
                    headers={"X-Subscription-Token": api_key},
                )
            except httpx.RequestError as error:
                if attempt >= MAX_RETRIES:
                    _LOGGER.warning("Brave web search request failed: %s", error)
                    return None, HttpRequestFailure(
                        f"request failed: {error}",
                        retryable=True,
                        attempts_made=MAX_RETRIES + 1,
                    )
                await sleep_for_retry(attempt)
                continue

            if response.status_code >= 400:
                # GET is idempotent — safe to repeat (includes a transient 500).
                if (
                    is_retryable_status(response.status_code, idempotent=True)
                    and attempt < MAX_RETRIES
                ):
                    await sleep_for_retry(attempt, parse_retry_after(response.headers))
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
                    attempts_made=(MAX_RETRIES + 1) if retryable else None,
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

            results = _restrict_results_to_domains(
                _standardize_results(raw_results),
                domains,
                count,
            )
            normalized_payload: dict[str, Any] = {
                "provider": "brave",
                "query": query,
                "count_requested": count,
                "page": page,
                "result_count": len(results),
                "results": results,
                "content_trust": "untrusted_web_content",
            }
            if domains:
                normalized_payload["applied_domains"] = domains
            if recency:
                normalized_payload["recency"] = recency
            # more_results_available reflects Brave's unfiltered result space.
            # With domain filters applied, result_count can be 0 while
            # more_results_available is true, luring the agent into paging
            # through empty results. Suppress it and warn instead.
            if domains:
                normalized_payload["warnings"] = [_BRAVE_DOMAIN_PAGING_WARNING]
            elif isinstance(payload, dict) and isinstance(payload.get("query"), dict):
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
    domains: list[str],
    count: int,
    page: int,
    recency: str,
) -> tuple[dict[str, Any] | None, HttpRequestFailure | None]:
    endpoint, endpoint_error = _build_searxng_endpoint(base_url)
    if endpoint_error is not None:
        return None, HttpRequestFailure(endpoint_error)
    if endpoint is None:
        return None, HttpRequestFailure("SearXNG endpoint could not be built")

    search_query = _build_search_query(query, domains)
    params: dict[str, Any] = {
        "q": search_query,
        "format": "json",
        "pageno": page,
        "safesearch": 0,
        "categories": "general",
    }
    if recency:
        params["time_range"] = recency

    async with httpx.AsyncClient(headers=_BROWSER_HEADERS, timeout=_REQUEST_TIMEOUT) as client:
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await _bounded_get(client, endpoint, params=params)
            except httpx.RequestError as error:
                if attempt >= MAX_RETRIES:
                    _LOGGER.warning("SearXNG web search request failed: %s", error)
                    return None, HttpRequestFailure(
                        f"request failed: {error}",
                        retryable=True,
                        attempts_made=MAX_RETRIES + 1,
                    )
                await sleep_for_retry(attempt)
                continue

            if response.status_code >= 400:
                # GET is idempotent — safe to repeat (includes a transient 500).
                if (
                    is_retryable_status(response.status_code, idempotent=True)
                    and attempt < MAX_RETRIES
                ):
                    await sleep_for_retry(attempt, parse_retry_after(response.headers))
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
                    attempts_made=(MAX_RETRIES + 1) if retryable else None,
                )

            try:
                payload = response.json()
            except ValueError:
                return None, HttpRequestFailure("provider returned invalid JSON")

            raw_results = payload.get("results") if isinstance(payload, dict) else None
            results = _restrict_results_to_domains(
                _standardize_searxng_results(raw_results),
                domains,
                count,
            )
            normalized_payload: dict[str, Any] = {
                "provider": WEB_SEARCH_PROVIDER_SEARXNG,
                "query": query,
                "count_requested": count,
                "page": page,
                "result_count": len(results),
                "results": results,
                "content_trust": "untrusted_web_content",
            }
            if domains:
                normalized_payload["applied_domains"] = domains
            if recency:
                normalized_payload["recency"] = recency
            warnings: list[str] = []
            if domains:
                warnings.append(_SEARXNG_DOMAIN_WARNING)
            if recency:
                warnings.append(_SEARXNG_RECENCY_WARNING)
            if page > 1:
                warnings.append(_SEARXNG_PAGINATION_WARNING)
            if warnings:
                normalized_payload["warnings"] = warnings
            return normalized_payload, None

    return None, HttpRequestFailure("request failed")


async def _get_bounded_with_retry(
    url: str,
    *,
    params: Mapping[str, Any],
    provider_label: str,
) -> tuple[httpx.Response | None, HttpRequestFailure | None]:
    """GET one bounded response through the shared idempotent retry policy."""
    async with httpx.AsyncClient(headers=_BROWSER_HEADERS, timeout=_REQUEST_TIMEOUT) as client:
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await _bounded_get(client, url, params=params)
            except httpx.RequestError as error:
                if attempt >= MAX_RETRIES:
                    _LOGGER.warning("%s web search request failed: %s", provider_label, error)
                    return None, HttpRequestFailure(
                        f"request failed: {error}",
                        retryable=True,
                        attempts_made=MAX_RETRIES + 1,
                    )
                await sleep_for_retry(attempt)
                continue

            if response.status_code >= 400:
                # GET is idempotent — safe to repeat (includes a transient 500).
                if (
                    is_retryable_status(response.status_code, idempotent=True)
                    and attempt < MAX_RETRIES
                ):
                    await sleep_for_retry(attempt, parse_retry_after(response.headers))
                    continue
                detail = _extract_error_detail(response)
                _LOGGER.warning(
                    "%s web search request failed: HTTP %s: %s",
                    provider_label,
                    response.status_code,
                    detail,
                )
                # A retryable status only reaches here after retries were exhausted.
                retryable = is_retryable_status(response.status_code, idempotent=True)
                return None, HttpRequestFailure(
                    f"HTTP {response.status_code}: {detail}",
                    retryable=retryable,
                    attempts_made=(MAX_RETRIES + 1) if retryable else None,
                )

            return response, None

    return None, HttpRequestFailure("request failed")


async def _search_duckduckgo(
    *,
    query: str,
    domains: list[str],
    count: int,
    page: int,
    recency: str,
) -> tuple[dict[str, Any] | None, HttpRequestFailure | None]:
    # DuckDuckGo has no search API: one html response is fetched and parsed,
    # then sliced into count/page windows client-side. Like Brave, domain
    # scoping rides on the site: operator with a post-filter guarantee.
    search_query = _build_search_query(query, domains)
    params: dict[str, Any] = {"q": search_query, "kp": _DUCKDUCKGO_SAFE_SEARCH}
    response, failure = await _get_bounded_with_retry(
        _DUCKDUCKGO_ENDPOINT,
        params=params,
        provider_label="DuckDuckGo",
    )
    if failure is not None or response is None:
        return None, failure

    if response.status_code == _DUCKDUCKGO_RATE_LIMIT_STATUS:
        _LOGGER.warning("DuckDuckGo web search rate-limited: HTTP 202")
        return None, HttpRequestFailure(
            "DuckDuckGo rate-limited the request; "
            "try again later or switch to a different search provider",
            retryable=True,
        )

    html_text = response.text
    parsed = _parse_duckduckgo_results(html_text)
    if not parsed and _is_duckduckgo_challenge(html_text):
        _LOGGER.warning("DuckDuckGo web search returned a bot-detection challenge")
        return None, HttpRequestFailure(
            "DuckDuckGo returned a bot-detection challenge; "
            "try again later or switch to a different search provider",
            retryable=True,
        )

    start = (page - 1) * count
    results = _restrict_results_to_domains(
        _standardize_results(parsed)[start : start + count], domains, count
    )
    envelope: dict[str, Any] = {
        "provider": WEB_SEARCH_PROVIDER_DUCKDUCKGO,
        "query": query,
        "count": count,
        "page": page,
        "result_count": len(results),
        "results": results,
    }
    if domains:
        envelope["applied_domains"] = domains
    # recency is echoed nowhere: DuckDuckGo cannot filter by age at all.
    warnings: list[str] = []
    if recency:
        warnings.append(_DUCKDUCKGO_RECENCY_WARNING)
    if page > 1:
        warnings.append(_DUCKDUCKGO_PAGINATION_WARNING)
    if warnings:
        envelope["warnings"] = warnings
    return envelope, None


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

    default_count = raw_settings.get("default_count", DEFAULT_WEB_SEARCH_COUNT)
    if (
        isinstance(default_count, bool)
        or not isinstance(default_count, int)
        or not (MIN_WEB_SEARCH_COUNT <= default_count <= MAX_WEB_SEARCH_COUNT)
    ):
        return None, (
            "web_search.default_count must be an integer between "
            f"{MIN_WEB_SEARCH_COUNT} and {MAX_WEB_SEARCH_COUNT}"
        )

    return {
        "provider": provider,
        "default_count": default_count,
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


def _exa_start_published_date(recency: str) -> str:
    """Render a canonical recency window as Exa's ISO 8601 start date."""
    cutoff = datetime.now(UTC) - timedelta(days=_EXA_RECENCY_WINDOW_DAYS[recency])
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _standardize_exa_results(raw_results: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_results, list):
        return []

    normalized: list[dict[str, Any]] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue

        title = _clean_snippet(raw.get("title"))
        url = _normalize_text(raw.get("url"))
        highlights = raw.get("highlights")
        description = ""
        if isinstance(highlights, list):
            description = _clean_snippet(
                " ".join(
                    highlight
                    for highlight in highlights
                    if isinstance(highlight, str) and highlight.strip()
                )
            )
        if not title and not url and not description:
            continue

        entry: dict[str, Any] = {
            "rank": len(normalized) + 1,
            "title": title,
            "url": url,
            "description": description,
            "content_trust": "untrusted_web_content",
        }
        page_age = _normalize_text(raw.get("publishedDate"))
        if page_age:
            entry["page_age"] = page_age
        normalized.append(entry)

    return normalized


async def _search_exa(
    *,
    api_key: str,
    query: str,
    domains: list[str],
    count: int,
    page: int,
    recency: str,
) -> tuple[dict[str, Any] | None, HttpRequestFailure | None]:
    # Exa filters domains natively (its docs prefer includeDomains over a
    # site: operator), so the raw query is sent and the post-filter below
    # only guarantees the contract.
    payload: dict[str, Any] = {
        "query": query,
        "numResults": count,
        "contents": {"highlights": True},
    }
    if domains:
        payload["includeDomains"] = domains
    if recency:
        payload["startPublishedDate"] = _exa_start_published_date(recency)

    response_payload, failure = await _post_json_bounded(
        _EXA_ENDPOINT,
        payload=payload,
        headers={"x-api-key": api_key},
        provider_label="Exa",
        auth_key_hint="EXA_API_KEY",
    )
    if failure is not None:
        return None, failure

    raw_results = response_payload.get("results") if isinstance(response_payload, dict) else None
    results = _restrict_results_to_domains(_standardize_exa_results(raw_results), domains, count)
    envelope: dict[str, Any] = {
        "provider": WEB_SEARCH_PROVIDER_EXA,
        "query": query,
        "count": count,
        "page": page,
        "result_count": len(results),
        "results": results,
    }
    if recency:
        envelope["recency"] = recency
    warnings: list[str] = []
    if recency:
        warnings.append(_EXA_RECENCY_WARNING)
    if page > 1:
        warnings.append(_EXA_PAGINATION_WARNING)
    if warnings:
        envelope["warnings"] = warnings
    return envelope, None


def _resolve_firecrawl_items(response_payload: dict[str, Any]) -> list[Any]:
    """Return the first list-shaped result collection in a Firecrawl envelope."""
    data = response_payload.get("data")
    nested = data if isinstance(data, dict) else {}
    web = response_payload.get("web")
    web_results = web.get("results") if isinstance(web, dict) else None
    candidates = [
        data,
        response_payload.get("results"),
        nested.get("results"),
        nested.get("data"),
        nested.get("web"),
        web_results,
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return candidate
    return []


def _standardize_firecrawl_results(raw_results: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_results, list):
        return []

    normalized: list[dict[str, Any]] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue

        metadata = raw.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        url = _normalize_text(
            raw.get("url")
            or raw.get("sourceURL")
            or raw.get("sourceUrl")
            or metadata.get("sourceURL")
        )
        title = _clean_snippet(raw.get("title") or metadata.get("title"))
        description = _clean_snippet(
            raw.get("description") or raw.get("snippet") or raw.get("summary")
        )
        if not title and not url and not description:
            continue

        entry: dict[str, Any] = {
            "rank": len(normalized) + 1,
            "title": title,
            "url": url,
            "description": description,
            "content_trust": "untrusted_web_content",
        }
        page_age = _normalize_text(
            raw.get("publishedDate")
            or raw.get("published")
            or metadata.get("publishedTime")
            or metadata.get("publishedDate")
        )
        if page_age:
            entry["page_age"] = page_age
        normalized.append(entry)

    return normalized


async def _search_firecrawl(
    *,
    api_key: str,
    query: str,
    domains: list[str],
    count: int,
    page: int,
    recency: str,
) -> tuple[dict[str, Any] | None, HttpRequestFailure | None]:
    # Firecrawl filters domains natively, so the raw query is sent and the
    # post-filter below only guarantees the contract.
    payload: dict[str, Any] = {"query": query, "limit": count, "sources": ["web"]}
    if domains:
        payload["includeDomains"] = domains
    if recency:
        payload["tbs"] = _FIRECRAWL_RECENCY_MAP[recency]

    response_payload, failure = await _post_json_bounded(
        _FIRECRAWL_ENDPOINT,
        payload=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        provider_label="Firecrawl",
        auth_key_hint="FIRECRAWL_API_KEY",
        extra_retryable_statuses={408},
    )
    if failure is not None:
        return None, failure

    # Firecrawl reports request-level failures inside a 200 envelope.
    if not isinstance(response_payload, dict) or response_payload.get("success") is not True:
        detail = "search failed"
        if isinstance(response_payload, dict):
            error_text = _normalize_text(response_payload.get("error")) or _normalize_text(
                response_payload.get("message")
            )
            if error_text:
                detail = error_text
        _LOGGER.warning("Firecrawl web search request failed: %s", detail)
        return None, HttpRequestFailure(detail, retryable=False)

    results = _restrict_results_to_domains(
        _standardize_firecrawl_results(_resolve_firecrawl_items(response_payload)),
        domains,
        count,
    )
    envelope: dict[str, Any] = {
        "provider": WEB_SEARCH_PROVIDER_FIRECRAWL,
        "query": query,
        "count": count,
        "page": page,
        "result_count": len(results),
        "results": results,
    }
    if recency:
        envelope["recency"] = recency
    if page > 1:
        envelope["warnings"] = [_FIRECRAWL_PAGINATION_WARNING]
    return envelope, None


def _standardize_serper_results(raw_results: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_results, list):
        return []

    normalized: list[dict[str, Any]] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue

        title = _clean_snippet(raw.get("title"))
        url = _normalize_text(raw.get("link"))
        description = _clean_snippet(raw.get("snippet"))
        if not title and not url and not description:
            continue

        entry: dict[str, Any] = {
            "rank": len(normalized) + 1,
            "title": title,
            "url": url,
            "description": description,
            "content_trust": "untrusted_web_content",
        }
        page_age = _normalize_text(raw.get("date"))
        if page_age:
            entry["page_age"] = page_age
        normalized.append(entry)

    return normalized


async def _search_serper(
    *,
    api_key: str,
    query: str,
    domains: list[str],
    count: int,
    page: int,
    recency: str,
) -> tuple[dict[str, Any] | None, HttpRequestFailure | None]:
    # Serper serves one 10-result Google page per request, so count/page
    # slices are assembled by fanning out over the minimal covering pages.
    # Like Brave, domain scoping rides on Google's site: operator.
    search_query = _build_search_query(query, domains)
    headers = {"X-API-KEY": api_key}
    start = (page - 1) * count
    collected: list[Any] = []
    serper_page = start // _SERPER_PAGE_SIZE + 1
    skip = start % _SERPER_PAGE_SIZE
    fetched = 0
    while len(collected) < count and fetched < _SERPER_MAX_PAGES_PER_CALL:
        payload: dict[str, Any] = {
            "q": search_query,
            "num": min(_SERPER_PAGE_SIZE, count - len(collected) + skip),
            "page": serper_page,
        }
        if recency:
            payload["tbs"] = _SERPER_RECENCY_MAP[recency]
        response_payload, failure = await _post_json_bounded(
            _SERPER_ENDPOINT,
            payload=payload,
            headers=headers,
            provider_label="Serper",
            auth_key_hint="SERPER_API_KEY",
        )
        if failure is not None:
            return None, failure
        organic = response_payload.get("organic") if isinstance(response_payload, dict) else None
        if not isinstance(organic, list) or not organic:
            break
        collected.extend(organic[skip : skip + (count - len(collected))])
        skip = 0
        fetched += 1
        serper_page += 1
        if len(organic) < _SERPER_PAGE_SIZE:
            break
    results = _restrict_results_to_domains(
        _standardize_serper_results(collected[:count]), domains, count
    )
    envelope: dict[str, Any] = {
        "provider": WEB_SEARCH_PROVIDER_SERPER,
        "query": query,
        "count": count,
        "page": page,
        "result_count": len(results),
        "results": results,
    }
    if recency:
        envelope["recency"] = recency
    return envelope, None


def _standardize_tavily_results(raw_results: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_results, list):
        return []

    normalized: list[dict[str, Any]] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue

        title = _clean_snippet(raw.get("title"))
        url = _normalize_text(raw.get("url"))
        description = _clean_snippet(raw.get("content"))
        if not description:
            description = _clean_snippet(raw.get("description"))
        if not title and not url and not description:
            continue

        entry: dict[str, Any] = {
            "rank": len(normalized) + 1,
            "title": title,
            "url": url,
            "description": description,
            "content_trust": "untrusted_web_content",
        }
        page_age = _normalize_text(raw.get("published_date"))
        if page_age:
            entry["page_age"] = page_age
        normalized.append(entry)

    return normalized


async def _search_tavily(
    *,
    api_key: str,
    query: str,
    domains: list[str],
    count: int,
    page: int,
    recency: str,
) -> tuple[dict[str, Any] | None, HttpRequestFailure | None]:
    # Tavily filters domains natively, so the raw query is sent and the
    # post-filter below only guarantees the contract.
    payload: dict[str, Any] = {
        "query": query,
        "search_depth": "basic",
        "max_results": count,
        "include_answer": False,
        "include_images": False,
        "include_raw_content": False,
    }
    if recency:
        # Tavily's time_range accepts day/month/year directly.
        payload["time_range"] = recency
    if domains:
        payload["include_domains"] = domains
        payload["include_domains_mode"] = "filter"

    response_payload, failure = await _post_json_bounded(
        _TAVILY_ENDPOINT,
        payload=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        provider_label="Tavily",
        auth_key_hint="TAVILY_API_KEY",
    )
    if failure is not None:
        return None, failure

    raw_results = response_payload.get("results") if isinstance(response_payload, dict) else None
    results = _restrict_results_to_domains(_standardize_tavily_results(raw_results), domains, count)
    envelope: dict[str, Any] = {
        "provider": WEB_SEARCH_PROVIDER_TAVILY,
        "query": query,
        "count": count,
        "page": page,
        "result_count": len(results),
        "results": results,
    }
    if recency:
        envelope["recency"] = recency
    if page > 1:
        envelope["warnings"] = [_TAVILY_PAGINATION_WARNING]
    return envelope, None


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

    domains, domains_error = _normalize_domains(arguments.get("domains"))
    if domains_error is not None:
        return tool_failure("validation_error", domains_error, retryable=False)

    settings, settings_error = _resolve_web_search_settings(settings_resolver)
    if settings_error is not None:
        return tool_failure("configuration_error", settings_error, retryable=False)
    if settings is None:
        return tool_failure(
            "configuration_error", "web search settings could not be resolved", retryable=False
        )

    try:
        count = optional_int(
            arguments.get("count"),
            field_name="count",
            default=settings["default_count"],
            minimum=MIN_WEB_SEARCH_COUNT,
            maximum=MAX_WEB_SEARCH_COUNT,
        )
        page = optional_int(
            arguments.get("page"),
            field_name="page",
            default=1,
            minimum=1,
            maximum=MAX_WEB_SEARCH_PAGE,
        )
    except ToolArgumentError as error:
        return tool_failure("validation_error", str(error), retryable=False)

    recency, recency_error = _normalize_recency(arguments.get("recency"))
    if recency_error is not None:
        return tool_failure("validation_error", recency_error, retryable=False)

    provider = settings["provider"]
    try:
        if provider == WEB_SEARCH_PROVIDER_FIRECRAWL:
            api_key = _normalize_text(credential_resolver("FIRECRAWL_API_KEY"))
            if not api_key:
                return tool_failure(
                    "missing_api_key",
                    "web_search requires FIRECRAWL_API_KEY to be configured",
                    retryable=False,
                )
            payload, search_failure = await _search_firecrawl(
                api_key=api_key,
                query=query,
                domains=domains,
                count=count,
                page=page,
                recency=recency,
            )
        elif provider == WEB_SEARCH_PROVIDER_SEARXNG:
            payload, search_failure = await _search_searxng(
                base_url=settings["searxng"]["base_url"],
                query=query,
                domains=domains,
                count=count,
                page=page,
                recency=recency,
            )
        elif provider == WEB_SEARCH_PROVIDER_DUCKDUCKGO:
            payload, search_failure = await _search_duckduckgo(
                query=query,
                domains=domains,
                count=count,
                page=page,
                recency=recency,
            )
        elif provider == WEB_SEARCH_PROVIDER_EXA:
            api_key = _normalize_text(credential_resolver("EXA_API_KEY"))
            if not api_key:
                return tool_failure(
                    "missing_api_key",
                    "web_search requires EXA_API_KEY to be configured",
                    retryable=False,
                )
            payload, search_failure = await _search_exa(
                api_key=api_key,
                query=query,
                domains=domains,
                count=count,
                page=page,
                recency=recency,
            )
        elif provider == WEB_SEARCH_PROVIDER_SERPER:
            api_key = _normalize_text(credential_resolver("SERPER_API_KEY"))
            if not api_key:
                return tool_failure(
                    "missing_api_key",
                    "web_search requires SERPER_API_KEY to be configured",
                    retryable=False,
                )
            payload, search_failure = await _search_serper(
                api_key=api_key,
                query=query,
                domains=domains,
                count=count,
                page=page,
                recency=recency,
            )
        elif provider == WEB_SEARCH_PROVIDER_TAVILY:
            api_key = _normalize_text(credential_resolver("TAVILY_API_KEY"))
            if not api_key:
                return tool_failure(
                    "missing_api_key",
                    "web_search requires TAVILY_API_KEY to be configured",
                    retryable=False,
                )
            payload, search_failure = await _search_tavily(
                api_key=api_key,
                query=query,
                domains=domains,
                count=count,
                page=page,
                recency=recency,
            )
        else:
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
                domains=domains,
                count=count,
                page=page,
                recency=recency,
            )
    except _ResponseTooLargeError as error:
        return tool_failure("response_too_large", str(error), retryable=False)

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
        family="web",
        open_input_schema=True,
        result_schema={"type": "object", "required": ["query", "results"]},
        display=ToolDisplay(
            primary_candidates=(
                ToolDisplayField("description", kind="description", quote=True),
                ToolDisplayField("query", kind="query", quote=True),
            ),
            fact_builder=result_count_fact_builder("result_count"),
        ),
        parallel_safe=True,
    )


__all__ = [
    "WEB_SEARCH_TOOL_DESCRIPTION",
    "WEB_SEARCH_TOOL_NAME",
    "WEB_SEARCH_TOOL_PARAMETERS",
    "register_web_search_tool",
    "web_search_handler",
]
