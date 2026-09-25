"""Internal Web Search provider request shaping and result parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from core.tools._web_search_common import (
    _build_search_query,
    _clean_snippet,
    _normalize_text,
    _restrict_results_to_domains,
)
from core.tools._web_search_transport import (
    _request_bounded,
    _request_json,
)
from core.utils.http_status import HttpRequestFailure
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.web_search")


_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


_DUCKDUCKGO_ENDPOINT = "https://html.duckduckgo.com/html"


_EXA_ENDPOINT = "https://api.exa.ai/search"


_FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v2/search"


_PERPLEXITY_ENDPOINT = "https://api.perplexity.ai/search"


_SERPER_ENDPOINT = "https://google.serper.dev/search"


_TAVILY_ENDPOINT = "https://api.tavily.com/search"


_SEARXNG_DOMAIN_WARNING = (
    "Some SearXNG engines ignore site restrictions, so fewer results than requested may remain."
)


_SEARXNG_RECENCY_WARNING = "Some SearXNG engines ignore the recency limit; check result dates."


_SEARXNG_PAGINATION_WARNING = (
    "SearXNG uses its own page size, so results between pages may be skipped."
)


_EXA_RECENCY_WARNING = "Exa leaves out pages without a publication date when recency is set."


_EXA_RECENCY_WINDOW_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


def _no_paging_warning(provider_label: str, page: int) -> str:
    return (
        f"{provider_label} cannot page results; these are the first results again, not page {page}."
    )


_FIRECRAWL_RECENCY_MAP = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}


_SERPER_PAGE_SIZE = 10


# DuckDuckGo has no search API: the html endpoint is fetched with a browser
# user agent and the result anchors are parsed (same approach as OpenClaw's
# duckduckgo extension). Safe search stays on the documented moderate default.
_DUCKDUCKGO_SAFE_SEARCH = "-1"


# DuckDuckGo answers HTTP 202 with an empty page when it rate-limits.
_DUCKDUCKGO_RATE_LIMIT_STATUS = 202


_DUCKDUCKGO_PAGINATION_WARNING = (
    "DuckDuckGo returns one result list; later pages only split it and may be empty."
)


def _duckduckgo_recency_warning(recency: str) -> str:
    return (
        "DuckDuckGo cannot limit results by age, so these results are not limited to the "
        f"past {recency}; check result dates."
    )


_SERPER_MAX_PAGES_PER_CALL = 5


_SERPER_RECENCY_MAP = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}


_BRAVE_RECENCY_MAP: dict[str, str] = {
    "day": "pd",
    "week": "pw",
    "month": "pm",
    "year": "py",
}


def _decode_duckduckgo_url(raw_href: Any) -> str:
    """Unwrap a DuckDuckGo redirect link to the direct target URL."""
    href = _normalize_text(raw_href)
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


def _parse_duckduckgo_results(html_text: str) -> tuple[list[dict[str, str]], bool]:
    """Read DDG result nodes once, keeping each snippet with its preceding link."""
    soup = BeautifulSoup(html_text, "html.parser")
    results: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    has_result_anchor = False
    for node in soup.select("a.result__a, .result__snippet"):
        if "result__a" in node.get_attribute_list("class"):
            has_result_anchor = True
            # Even an invalid anchor ends the preceding result's snippet scope.
            current = None
            title = node.get_text().strip()
            url = _decode_duckduckgo_url(node.get("href"))
            if title and url:
                current = {"title": title, "url": url, "description": ""}
                results.append(current)
        elif current is not None:
            current["description"] = node.get_text().strip()
            current = None
    challenge = not has_result_anchor and (
        soup.select_one(".g-recaptcha, #challenge-form, [name=challenge]") is not None
        or "are you a human" in soup.get_text().lower()
    )
    return results, challenge


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
        }
        page_age = _normalize_text(raw.get("publishedDate"))
        if page_age:
            entry["page_age"] = page_age
        normalized.append(entry)

    return normalized


async def _search_brave(
    *,
    api_key: str,
    query: str,
    domains: list[str],
    exclude: list[str],
    count: int,
    page: int,
    recency: str,
) -> tuple[dict[str, Any] | None, HttpRequestFailure | None]:
    search_query = _build_search_query(query, domains, exclude)

    # text_decorations off: Brave otherwise wraps snippets in highlight markup.
    params: dict[str, Any] = {"q": search_query, "count": count, "text_decorations": "false"}
    if page > 1:
        # Brave paginates with a zero-based page offset in units of `count`.
        params["offset"] = page - 1
    if recency:
        params["freshness"] = _BRAVE_RECENCY_MAP[recency]

    payload, failure = await _request_json(
        "GET",
        _BRAVE_ENDPOINT,
        params=params,
        headers={"X-Subscription-Token": api_key},
        provider_label="Brave Search",
        credential_key="BRAVE_API_KEY",
    )
    if failure is not None:
        return None, failure

    raw_results = None
    if isinstance(payload, dict):
        web_payload = payload.get("web")
        if isinstance(web_payload, dict):
            raw_results = web_payload.get("results")

    results = _restrict_results_to_domains(
        _standardize_results(raw_results), domains, count, exclude
    )
    normalized_payload: dict[str, Any] = {"results": results}
    if recency:
        normalized_payload["recency"] = recency
    # more_results_available describes Brave's unfiltered result space: with
    # site filters the filtered page can be empty while it is still true, so
    # it would lure the Agent into paging through empty pages.
    if not domains and not exclude and isinstance(payload, dict):
        query_info = payload.get("query")
        if isinstance(query_info, dict) and query_info.get("more_results_available") is True:
            normalized_payload["more_results_available"] = True

    return normalized_payload, None


def _build_searxng_endpoint(base_url: str) -> tuple[str | None, str | None]:
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, (
            f"The SearXNG address in Settings ({base_url}) is not an http or https URL. Tell "
            "the user to fix it in Settings under Web search."
        )

    base_path = parsed.path.rstrip("/")
    search_path = f"{base_path}/search" if base_path else "/search"
    endpoint = urlunsplit((parsed.scheme, parsed.netloc, search_path, "", ""))
    return endpoint, None


async def _search_searxng(
    *,
    base_url: str,
    query: str,
    domains: list[str],
    exclude: list[str],
    count: int,
    page: int,
    recency: str,
) -> tuple[dict[str, Any] | None, HttpRequestFailure | None]:
    endpoint, endpoint_error = _build_searxng_endpoint(base_url)
    if endpoint_error is not None:
        return None, HttpRequestFailure(endpoint_error)
    if endpoint is None:
        return None, HttpRequestFailure("SearXNG endpoint could not be built")

    search_query = _build_search_query(query, domains, exclude)
    params: dict[str, Any] = {
        "q": search_query,
        "format": "json",
        "pageno": page,
        "safesearch": 0,
        "categories": "general",
    }
    if recency:
        params["time_range"] = recency

    payload, failure = await _request_json(
        "GET",
        endpoint,
        params=params,
        provider_label="SearXNG",
        status_hints={
            403: (
                "Tell the user to allow the json format under search.formats in the "
                "SearXNG instance's settings."
            )
        },
        unreachable_hint=(
            f"Tell the user the SearXNG instance at {base_url} is not reachable; they can "
            "start it or change its address in Settings under Web search."
        ),
    )
    if failure is not None:
        return None, failure

    raw_results = payload.get("results") if isinstance(payload, dict) else None
    results = _restrict_results_to_domains(
        _standardize_searxng_results(raw_results), domains, count, exclude
    )
    normalized_payload: dict[str, Any] = {"results": results}
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


async def _search_duckduckgo(
    *,
    query: str,
    domains: list[str],
    exclude: list[str],
    count: int,
    page: int,
    recency: str,
) -> tuple[dict[str, Any] | None, HttpRequestFailure | None]:
    # DuckDuckGo has no search API: one html response is fetched and parsed,
    # then sliced into count/page windows client-side. Like Brave, domain
    # scoping rides on the site: operator with a post-filter guarantee.
    search_query = _build_search_query(query, domains, exclude)
    params: dict[str, Any] = {"q": search_query, "kp": _DUCKDUCKGO_SAFE_SEARCH}
    response, failure = await _request_bounded(
        "GET",
        _DUCKDUCKGO_ENDPOINT,
        params=params,
        provider_label="DuckDuckGo",
    )
    if failure is not None or response is None:
        return None, failure

    if response.status_code == _DUCKDUCKGO_RATE_LIMIT_STATUS:
        _LOGGER.warning("DuckDuckGo web search rate-limited: HTTP 202")
        return None, HttpRequestFailure(
            "DuckDuckGo is limiting requests (it answered with an empty page). Wait a while "
            "before searching again; if this keeps happening, tell the user they can choose "
            "another search provider in Settings under Web search.",
            retryable=True,
        )

    html_text = response.text
    parsed, challenge = _parse_duckduckgo_results(html_text)
    if not parsed and challenge:
        _LOGGER.warning("DuckDuckGo web search returned a bot-detection challenge")
        return None, HttpRequestFailure(
            "DuckDuckGo answered with a bot-detection challenge instead of results. Wait a "
            "while before searching again; if this keeps happening, tell the user they can "
            "choose another search provider in Settings under Web search.",
            retryable=True,
        )

    start = (page - 1) * count
    results = _restrict_results_to_domains(
        [{"rank": index, **row} for index, row in enumerate(parsed, start=1)][
            start : start + count
        ],
        domains,
        count,
        exclude,
    )
    envelope: dict[str, Any] = {"results": results}
    # recency is echoed nowhere: DuckDuckGo cannot filter by age at all.
    warnings: list[str] = []
    if recency:
        warnings.append(_duckduckgo_recency_warning(recency))
    if page > 1:
        warnings.append(_DUCKDUCKGO_PAGINATION_WARNING)
    if warnings:
        envelope["warnings"] = warnings
    return envelope, None


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
    exclude: list[str],
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
    elif exclude:
        # Exclusions inside included domains rely on the post-filter below.
        payload["excludeDomains"] = exclude
    if recency:
        payload["startPublishedDate"] = _exa_start_published_date(recency)

    response_payload, failure = await _request_json(
        "POST",
        _EXA_ENDPOINT,
        payload=payload,
        headers={"x-api-key": api_key},
        provider_label="Exa",
        credential_key="EXA_API_KEY",
    )
    if failure is not None:
        return None, failure

    raw_results = response_payload.get("results") if isinstance(response_payload, dict) else None
    results = _restrict_results_to_domains(
        _standardize_exa_results(raw_results), domains, count, exclude
    )
    envelope: dict[str, Any] = {"results": results}
    if recency:
        envelope["recency"] = recency
    warnings: list[str] = []
    if recency:
        warnings.append(_EXA_RECENCY_WARNING)
    if page > 1:
        warnings.append(_no_paging_warning("Exa", page))
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
    exclude: list[str],
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

    response_payload, failure = await _request_json(
        "POST",
        _FIRECRAWL_ENDPOINT,
        payload=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        provider_label="Firecrawl",
        credential_key="FIRECRAWL_API_KEY",
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
        return None, HttpRequestFailure(
            f"Firecrawl could not run the search: {detail}", retryable=False
        )

    results = _restrict_results_to_domains(
        _standardize_firecrawl_results(_resolve_firecrawl_items(response_payload)),
        domains,
        count,
        exclude,
    )
    envelope: dict[str, Any] = {"results": results}
    if recency:
        envelope["recency"] = recency
    if page > 1:
        envelope["warnings"] = [_no_paging_warning("Firecrawl", page)]
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
    exclude: list[str],
    count: int,
    page: int,
    recency: str,
) -> tuple[dict[str, Any] | None, HttpRequestFailure | None]:
    # Serper serves one 10-result Google page per request, so count/page
    # slices are assembled by fanning out over the minimal covering pages.
    # Like Brave, domain scoping rides on Google's site: operator.
    search_query = _build_search_query(query, domains, exclude)
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
        response_payload, failure = await _request_json(
            "POST",
            _SERPER_ENDPOINT,
            payload=payload,
            headers=headers,
            provider_label="Serper",
            credential_key="SERPER_API_KEY",
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
        _standardize_serper_results(collected[:count]), domains, count, exclude
    )
    envelope: dict[str, Any] = {"results": results}
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
    exclude: list[str],
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
        # Tavily's time_range accepts day/week/month/year directly.
        payload["time_range"] = recency
    if domains:
        payload["include_domains"] = domains
        payload["include_domains_mode"] = "filter"
    if exclude:
        payload["exclude_domains"] = exclude

    response_payload, failure = await _request_json(
        "POST",
        _TAVILY_ENDPOINT,
        payload=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        provider_label="Tavily",
        credential_key="TAVILY_API_KEY",
    )
    if failure is not None:
        return None, failure

    raw_results = response_payload.get("results") if isinstance(response_payload, dict) else None
    results = _restrict_results_to_domains(
        _standardize_tavily_results(raw_results), domains, count, exclude
    )
    envelope: dict[str, Any] = {"results": results}
    if recency:
        envelope["recency"] = recency
    if page > 1:
        envelope["warnings"] = [_no_paging_warning("Tavily", page)]
    return envelope, None


def _standardize_perplexity_results(raw_results: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_results, list):
        return []

    normalized: list[dict[str, Any]] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue

        title = _clean_snippet(raw.get("title"))
        url = _normalize_text(raw.get("url"))
        description = _clean_snippet(raw.get("snippet"))
        if not title and not url and not description:
            continue

        entry: dict[str, Any] = {
            "rank": len(normalized) + 1,
            "title": title,
            "url": url,
            "description": description,
        }
        page_age = _normalize_text(raw.get("date")) or _normalize_text(raw.get("last_updated"))
        if page_age:
            entry["page_age"] = page_age
        normalized.append(entry)

    return normalized


async def _search_perplexity(
    *,
    api_key: str,
    query: str,
    domains: list[str],
    exclude: list[str],
    count: int,
    page: int,
    recency: str,
) -> tuple[dict[str, Any] | None, HttpRequestFailure | None]:
    # Perplexity's Search API filters domains and recency natively, and its
    # web-search maximum (20) matches the tool schema cap, so count, domains,
    # and recency pass through directly; the post-filter guarantees the
    # contract and applies exclusions.
    payload: dict[str, Any] = {"query": query, "max_results": count}
    if domains:
        payload["search_domain_filter"] = domains
    if recency:
        payload["search_recency_filter"] = recency

    response_payload, failure = await _request_json(
        "POST",
        _PERPLEXITY_ENDPOINT,
        payload=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        provider_label="Perplexity",
        credential_key="PERPLEXITY_API_KEY",
    )
    if failure is not None:
        return None, failure

    raw_results = response_payload.get("results") if isinstance(response_payload, dict) else None
    results = _restrict_results_to_domains(
        _standardize_perplexity_results(raw_results), domains, count, exclude
    )
    envelope: dict[str, Any] = {"results": results}
    if recency:
        envelope["recency"] = recency
    if page > 1:
        envelope["warnings"] = [_no_paging_warning("Perplexity", page)]
    return envelope, None
