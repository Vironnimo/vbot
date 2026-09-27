"""web_search registration, Settings and calls: the advertised schema, clear calls
written for other search Tools, refusals before searching, and the result view."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.tools.contracts import ToolContractError
from core.tools.tools import ToolRegistry
from core.tools.web_search import (
    WEB_SEARCH_TOOL_DESCRIPTION,
    WEB_SEARCH_TOOL_NAME,
    WEB_SEARCH_TOOL_PARAMETERS,
    register_web_search_tool,
)
from tests.core.tools.web_search_test_support import (
    BRAVE_ENDPOINT,
    EXA_ENDPOINT,
    FIRECRAWL_ENDPOINT,
    PERPLEXITY_ENDPOINT,
    TAVILY_ENDPOINT,
    assert_failure_envelope,
    assert_success_envelope,
    make_context,
    request_json,
    search,
    web_search_registry,
)

WEEK_NOTE = (
    "Results are limited to the past week, the shortest available window that covers the "
    "requested period; check result dates."
)


def _schema_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in [key, *_schema_strings(item)]]
    if isinstance(value, list):
        return [text for item in value for text in _schema_strings(item)]
    return []


@pytest.fixture
def brave() -> Any:
    """Answer Brave searches with results from three sites and record each request."""
    with respx.mock(assert_all_called=False) as router:
        route = router.get(BRAVE_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {"title": "Docs", "url": "https://docs.python.org/3/", "age": "2d"},
                            {"title": "Thread", "url": "https://www.reddit.com/r/python/"},
                            {"title": "News", "url": "https://example.com/news"},
                        ]
                    }
                },
            )
        )
        yield route


def sent(route: Any) -> dict[str, str]:
    assert route.call_count == 1
    return dict(route.calls[0].request.url.params)


def test_registration_advertises_the_search_fields_without_credentials() -> None:
    registry = web_search_registry()

    tool = registry.get("web_search")
    assert tool.name == WEB_SEARCH_TOOL_NAME == "web_search"
    assert tool.description == WEB_SEARCH_TOOL_DESCRIPTION
    assert tool.parameters == WEB_SEARCH_TOOL_PARAMETERS
    assert tool.open_input_schema is True

    [definition] = registry.provider_definitions(["web_search"])
    assert definition["name"] == "web_search"
    assert definition["description"] == WEB_SEARCH_TOOL_DESCRIPTION
    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["query"]
    assert "additionalProperties" not in parameters
    properties = parameters["properties"]
    assert set(properties) == {"query", "domains", "count", "page", "recency"}
    assert properties["domains"]["items"] == {"type": "string", "minLength": 1}
    assert (properties["domains"]["minItems"], properties["domains"]["maxItems"]) == (1, 10)
    assert (properties["count"]["minimum"], properties["count"]["maximum"]) == (1, 20)
    page = properties["page"]
    assert (page["minimum"], page["maximum"], page["default"]) == (1, 10, 1)
    assert properties["recency"]["enum"] == ["day", "week", "month", "year"]
    assert properties["recency"]["description"]
    assert all("default" not in schema for name, schema in properties.items() if name != "page")
    strings = _schema_strings(WEB_SEARCH_TOOL_PARAMETERS)
    for key in ("BRAVE", "TAVILY", "EXA", "SERPER", "FIRECRAWL", "PERPLEXITY"):
        assert all(f"{key}_API_KEY" not in value for value in strings)


def test_row_shows_the_description_or_the_search_words() -> None:
    registry = web_search_registry()

    def primary(arguments: dict[str, Any]) -> list[str]:
        return [
            part["value"] for part in registry.display_for_call("web_search", arguments)["primary"]
        ]

    assert primary({"description": "Find the release notes", "query": "vBot notes"}) == [
        "Find the release notes"
    ]
    assert primary({"q": "vbot"}) == ["vbot"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "label", "key"),
    [
        (None, "Brave Search", "BRAVE_API_KEY"),
        ("perplexity", "Perplexity", "PERPLEXITY_API_KEY"),
        ("firecrawl", "Firecrawl", "FIRECRAWL_API_KEY"),
        ("serper", "Serper", "SERPER_API_KEY"),
        ("tavily", "Tavily", "TAVILY_API_KEY"),
        ("exa", "Exa", "EXA_API_KEY"),
    ],
)
async def test_a_provider_without_its_key_tells_the_user_how_to_set_it_up(
    tmp_path: Path, provider: str | None, label: str, key: str
) -> None:
    result = await search(tmp_path, {"query": "vbot"}, provider=provider, credentials=lambda _: "")

    error = assert_failure_envelope(result, "missing_api_key")
    assert error["message"] == (
        f"Web search is not set up: the selected provider, {label}, needs {key} in the .env "
        "file of the vBot data directory. Tell the user: they can add the key, or choose "
        "another provider in Settings under Web search (DuckDuckGo needs no key)."
    )
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_invalid_settings_are_a_configuration_failure(tmp_path: Path) -> None:
    result = await search(tmp_path, {"query": "vbot"}, settings={"default_count": 0})

    error = assert_failure_envelope(result, "configuration_error")
    assert error["message"] == (
        "Web search settings are invalid (web_search.default_count must be an integer "
        "between 1 and 20). Tell the user to check Settings under Web search."
    )


@pytest.mark.asyncio
async def test_crashing_settings_are_logged_and_a_configuration_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def crash() -> dict[str, Any]:
        raise RuntimeError("settings backend exploded")

    registry = ToolRegistry()
    register_web_search_tool(registry, lambda _key: "key", crash)

    with caplog.at_level(logging.ERROR, logger="vbot.tools.web_search"):
        result = await registry.dispatch(make_context(tmp_path), {"query": "vbot"})

    error = assert_failure_envelope(result, "configuration_error")
    assert error["message"] == (
        "Web search settings are invalid (web_search settings could not be loaded: settings "
        "backend exploded). Tell the user to check Settings under Web search."
    )
    [record] = [
        record
        for record in caplog.records
        if "settings resolver crashed unexpectedly" in record.getMessage()
    ]
    assert record.levelno == logging.ERROR and record.exc_info is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "params"),
    [
        ({"q": "python"}, {"q": "python"}),
        ({"search_query": "python", "num_results": 5}, {"q": "python", "count": "5"}),
        ({"query": ["python"], "maxResults": "5"}, {"q": "python", "count": "5"}),
        ({"query": "python", "limit": 5}, {"q": "python", "count": "5"}),
        ({"query": "python", "numResults": 5}, {"q": "python", "count": "5"}),
        ({"query": "python", "page_number": 2}, {"q": "python", "offset": "1"}),
        ({"query": "python", "freshness": "pw"}, {"q": "python", "freshness": "pw"}),
        ({"query": "python", "time_range": "past_week"}, {"q": "python", "freshness": "pw"}),
        ({"query": "python", "tbs": "qdr:m"}, {"q": "python", "freshness": "pm"}),
        ({"query": "python", "recency": "Last 24 hours"}, {"q": "python", "freshness": "pd"}),
        ({"query": "python", "recency": "7d"}, {"q": "python", "freshness": "pw"}),
        ({"query": "python", "since": "1 year ago"}, {"q": "python", "freshness": "py"}),
        ({"query": "python", "days": 30}, {"q": "python", "freshness": "pm"}),
        (
            {"query": "python", "count": "", "page": None, "recency": "any", "date_after": ""},
            {"q": "python"},
        ),
        ({"query": "python", "domains": ""}, {"q": "python"}),
        ({"query": "python", "domains": []}, {"q": "python"}),
    ],
)
async def test_other_spellings_run_the_same_search(
    tmp_path: Path, brave: Any, arguments: dict[str, Any], params: dict[str, str]
) -> None:
    result = await search(tmp_path, arguments)

    assert_success_envelope(result)
    request = sent(brave)
    assert {key: request[key] for key in params} == params
    for absent in {"offset", "freshness"} - set(params):
        assert absent not in request
    assert "note" not in result["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "query", "data"),
    [
        (
            # Claude Code WebSearch
            {
                "query": "asyncio",
                "allowed_domains": ["docs.python.org"],
                "blocked_domains": ["reddit.com"],
            },
            "asyncio site:docs.python.org -site:reddit.com",
            {"domains": "docs.python.org", "excluded_domains": "reddit.com"},
        ),
        (
            {"query": "asyncio", "site": "https://docs.python.org/"},
            "asyncio site:docs.python.org",
            {"domains": "docs.python.org"},
        ),
        (
            {"query": "asyncio", "include_domains": "docs.python.org, example.com"},
            "asyncio site:docs.python.org OR site:example.com",
            {"domains": "docs.python.org, example.com"},
        ),
        (
            # OpenClaw-style deny entries
            {"query": "asyncio", "domain_filter": ["-reddit.com", "*.python.org"]},
            "asyncio site:python.org -site:reddit.com",
            {"domains": "python.org", "excluded_domains": "reddit.com"},
        ),
        (
            {"query": "asyncio (site:docs.python.org OR site:example.com) -site:reddit.com"},
            "asyncio site:docs.python.org OR site:example.com -site:reddit.com",
            {"domains": "docs.python.org, example.com", "excluded_domains": "reddit.com"},
        ),
    ],
)
async def test_site_restrictions_in_any_form_filter_the_results(
    tmp_path: Path, brave: Any, arguments: dict[str, Any], query: str, data: dict[str, str]
) -> None:
    result = await search(tmp_path, arguments)

    assert sent(brave)["q"] == query
    shown = assert_success_envelope(result)
    assert {key: shown[key] for key in data} == data
    assert "reddit.com" not in shown["content"]
    assert "https://docs.python.org/3/" in shown["content"]


@pytest.mark.asyncio
async def test_site_operators_become_native_filters_for_other_providers(tmp_path: Path) -> None:
    with respx.mock() as router:
        route = router.post(TAVILY_ENDPOINT).respond(
            200,
            json={
                "results": [
                    {"title": "Docs", "url": "https://docs.python.org/3/", "content": "Guide"},
                    {"title": "Leak", "url": "https://other.test/", "content": "Off site"},
                ]
            },
        )
        result = await search(
            tmp_path, {"query": 'asyncio "task group" site:python.org'}, provider="tavily"
        )

    body = request_json(route.calls[0].request)
    assert body["query"] == 'asyncio "task group"'
    assert body["include_domains"] == ["python.org"]
    assert assert_success_envelope(result) == {
        "domains": "python.org",
        "content": "1. Docs\nhttps://docs.python.org/3/\nGuide",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "freshness", "note"),
    [
        ({"query": "python", "days": 3}, "pw", WEEK_NOTE),
        ({"query": "python", "recency": "3 days"}, "pw", WEEK_NOTE),
        ({"query": "python", "recency": "week", "days": 5}, "pw", WEEK_NOTE),
        (
            {"query": "python", "days": 400},
            None,
            "Results are not limited by age: the longest available window is the past year, "
            "and the requested period is longer; check result dates.",
        ),
    ],
)
async def test_other_periods_use_the_shortest_covering_window_with_a_note(
    tmp_path: Path, brave: Any, arguments: dict[str, Any], freshness: str | None, note: str
) -> None:
    result = await search(tmp_path, arguments)

    assert sent(brave).get("freshness") == freshness
    assert result["data"]["note"] == note


@pytest.mark.asyncio
async def test_a_start_date_uses_the_shortest_covering_window(tmp_path: Path, brave: Any) -> None:
    start = (datetime.now(UTC) - timedelta(days=10)).date().isoformat()

    result = await search(tmp_path, {"query": "python", "published_after": start})

    assert sent(brave)["freshness"] == "pm"
    data = assert_success_envelope(result)
    assert data["recency"] == "month"
    assert data["note"] == WEEK_NOTE.replace("week", "month")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"queries": ["python asyncio", "python threading"]},
            "web_search runs one query per call; received 2. Make one web_search call per "
            "query; the calls can run at the same time.",
        ),
        (
            {"query": "python", "date_before": "2024-01-01"},
            "web_search cannot return only results published before a date: it can limit "
            "results to the past day, week, month or year (recency). Remove date_before, or "
            "name the period in query, for example 2024.",
        ),
        (
            {"query": "python", "freshness": "2024-01-01to2024-06-30"},
            "web_search cannot limit results to a date range",
        ),
        (
            {"query": "python", "since": "last release"},
            'date_after must be a date such as 2026-09-01; received "last release".',
        ),
        (
            {"query": "python", "domains": ["docs.python.org/3/library"]},
            "domains take site names such as example.com, not addresses with a path "
            '("docs.python.org/3/library"). Pass {"domains": ["docs.python.org"]} and put '
            "words from the path in query.",
        ),
        ({"query": "python", "domains": [{"domain": "example.com"}]}, '"domains[0]" must be'),
        (
            {"query": "python", "domains": [f"site{index}.example" for index in range(11)]},
            '"domains" allows at most 10 items; received 11.',
        ),
        ({"q": "python", "query": "rust"}, "Conflicting values for query"),
        ({"query": "python", "freshness": "week", "recency": "day"}, "Conflicting values"),
        ({"query": "python", "recency": "decade"}, '"recency" must be one of'),
        ({"query": "python", "count": 0}, '"count" must be at least 1; received 0.'),
        ({"query": "python", "count": 21}, '"count" must be at most 20; received 21.'),
        ({"query": "python", "page": 0}, '"page" must be at least 1; received 0.'),
        ({"query": "python", "page": 11}, '"page" must be at most 10; received 11.'),
    ],
)
async def test_unclear_or_impossible_requests_are_refused_before_searching(
    tmp_path: Path, brave: Any, arguments: dict[str, Any], message: str
) -> None:
    resolved: list[str] = []

    def credentials(key: str) -> str:
        resolved.append(key)
        return "key"

    with pytest.raises(ToolContractError) as error:
        await search(tmp_path, arguments, credentials=credentials)

    assert message in str(error.value)
    assert brave.call_count == 0
    assert resolved == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"query": "   "},
            'Pass the search words as query, for example {"query": "python asyncio"}.',
        ),
        (
            {"query": "python", "domains": ["bad domain.example"]},
            "domains[0] must be a valid domain",
        ),
        (
            {"query": "python", "recency": "week", "days": 30},
            "The result-age arguments disagree (days means month, recency means week). "
            "Pass one of them.",
        ),
        (
            {"query": "python", "date_after": "2999-01-01"},
            "date_after 2999-01-01 is in the future, so no result can match. Pass a past "
            "date, or omit date_after.",
        ),
        (
            {"query": "python", "domains": ["python.org"], "blocked_domains": ["python.org"]},
            "python.org is both required and excluded. Keep it in domains or in "
            "exclude_domains, not both.",
        ),
    ],
)
async def test_empty_or_contradictory_requests_fail_without_searching(
    tmp_path: Path, brave: Any, arguments: dict[str, Any], message: str
) -> None:
    result = await search(tmp_path, arguments)

    error = assert_failure_envelope(result, "invalid_arguments")
    assert error["message"] == message
    assert error["retryable"] is False
    assert brave.call_count == 0


@pytest.mark.asyncio
async def test_results_read_as_a_numbered_list_and_count_for_the_row(tmp_path: Path) -> None:
    registry = web_search_registry()
    context = make_context(tmp_path)
    long_text = "word " * 150
    with respx.mock() as router:
        router.get(BRAVE_ENDPOINT).respond(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Release\n notes",
                            "url": "https://example.com/a",
                            "description": long_text,
                            "page_age": "2026-09-01T08:30:00Z",
                        },
                        {"url": "https://example.com/b"},
                    ]
                }
            },
        )
        result = await registry.dispatch(context, {"query": "release notes"})

    content = assert_success_envelope(result)["content"]
    first, second = content.split("\n\n")
    title, url, detail = first.split("\n")
    assert (title, url) == ("1. Release notes", "https://example.com/a")
    assert detail.startswith("2026-09-01 - word word")
    assert detail.endswith("...") and len(detail) <= len("2026-09-01 - ") + 403
    assert second == "2. https://example.com/b"
    assert context.presentation_facts == [
        {"kind": "count", "value": 2, "unit": "results", "at_least": False}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "method", "endpoint", "empty", "label"),
    [
        ("tavily", "POST", TAVILY_ENDPOINT, {"results": []}, "Tavily"),
        ("exa", "POST", EXA_ENDPOINT, {"results": []}, "Exa"),
        (
            "firecrawl",
            "POST",
            FIRECRAWL_ENDPOINT,
            {"success": True, "data": {"web": []}},
            "Firecrawl",
        ),
        ("perplexity", "POST", PERPLEXITY_ENDPOINT, {"results": []}, "Perplexity"),
    ],
)
async def test_providers_without_paging_say_a_later_page_repeats_the_first(
    tmp_path: Path, provider: str, method: str, endpoint: str, empty: dict[str, Any], label: str
) -> None:
    with respx.mock() as router:
        route = router.request(method, endpoint).respond(200, json=empty)
        result = await search(tmp_path, {"query": "vbot", "page": 2}, provider=provider)

    assert assert_success_envelope(result) == {
        "note": f"{label} cannot page results; these are the first results again, not page 2.",
        "content": "No results found. Try other or fewer search words.",
    }
    assert route.call_count == 1
    assert "page" not in request_json(route.calls[0].request)
