"""Built-in web_search tool with selectable first-party search providers."""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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
    WEB_SEARCH_PROVIDER_SEARXNG,
)
from core.tools.arguments import ToolArgumentError, optional_int
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.http_status import HttpRequestFailure, is_retryable_status, parse_retry_after
from core.utils.logging import get_logger
from core.utils.retry import MAX_RETRIES, sleep_for_retry

_LOGGER = get_logger("tools.web_search")

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_DOMAIN_LABEL_PATTERN = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)\Z")

_MAX_DOMAIN_FILTERS = 10

_SEARXNG_DOMAIN_WARNING = (
    "domain-filter completeness depends on the configured SearXNG engines; "
    "returned results are still restricted to applied_domains"
)
_SEARXNG_RECENCY_WARNING = "recency enforcement depends on the configured SearXNG engines"

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

_RECENCY_VALUES = ("day", "month", "year")
_BRAVE_RECENCY_MAP: dict[str, str] = {
    "day": "pd",
    "month": "pm",
    "year": "py",
}

_ALLOWED_ARGUMENTS = frozenset({"query", "domains", "count", "page", "recency"})

WEB_SEARCH_TOOL_NAME = "web_search"
WEB_SEARCH_TOOL_DESCRIPTION = (
    "Search the public web using the configured search provider and return "
    "structured results with title, url, description, and page age where "
    "available. Descriptions are short snippets - use web_fetch on a result "
    "url to read the full page. Supports domain restriction (domains), "
    "a provider-neutral recency window (recency), and pagination (page). "
    "Search operators in query are passed through to the configured provider."
)
WEB_SEARCH_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "description": "Search query text.",
        },
        "domains": {
            "type": "array",
            "description": (
                "Optional domains that every returned result must belong to. "
                "Use hostnames without a scheme or path (for example, example.com). "
                "A domain includes its subdomains; a specific subdomain narrows the scope."
            ),
            "items": {"type": "string", "minLength": 1, "maxLength": 253},
            "minItems": 1,
            "maxItems": _MAX_DOMAIN_FILTERS,
            "uniqueItems": True,
        },
        "count": {
            "type": "integer",
            "description": (
                "Maximum number of results to return (1-20). Omit to use the configured default."
            ),
            "minimum": MIN_WEB_SEARCH_COUNT,
            "maximum": MAX_WEB_SEARCH_COUNT,
        },
        "page": {
            "type": "integer",
            "description": (
                "Result page to fetch (1-based). Request the next page when "
                "more_results_available is true and the first page was not enough."
            ),
            "minimum": 1,
            "maximum": MAX_WEB_SEARCH_PAGE,
            "default": 1,
        },
        "recency": {
            "type": "string",
            "enum": list(_RECENCY_VALUES),
            "description": (
                "Optional provider-neutral age window for results: day, month, or year. "
                "The configured search provider determines page age. Omit for no recency "
                "restriction."
            ),
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


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
                response = await client.get(
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
                response = await client.get(endpoint, params=params)
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
    if provider == WEB_SEARCH_PROVIDER_SEARXNG:
        payload, search_failure = await _search_searxng(
            base_url=settings["searxng"]["base_url"],
            query=query,
            domains=domains,
            count=count,
            page=page,
            recency=recency,
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
        domains=domains,
        count=count,
        page=page,
        recency=recency,
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
        result_schema={"type": "object", "required": ["query", "results"]},
        display=ToolDisplay(summary_fields=("query",)),
        parallel_safe=True,
    )


__all__ = [
    "WEB_SEARCH_TOOL_DESCRIPTION",
    "WEB_SEARCH_TOOL_NAME",
    "WEB_SEARCH_TOOL_PARAMETERS",
    "register_web_search_tool",
    "web_search_handler",
]
