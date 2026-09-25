"""web_search runs clear calls written for other search Tools and refuses unclear ones."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.tools.contracts import ToolContractError
from core.tools.tools import ToolRegistry
from core.tools.web_search import register_web_search_tool
from tests.core.tools.web_search_helpers import (
    _BRAVE_ENDPOINT,
    _TAVILY_ENDPOINT,
    _fake_credential_resolver,
    _read_json_body,
    assert_failure_envelope,
    assert_success_envelope,
    make_context,
)

WEEK_NOTE = (
    "Results are limited to the past week, the shortest available window that covers the "
    "requested period; check result dates."
)


@pytest.fixture
def brave() -> Any:
    """Answer Brave searches with results from three sites and record each request."""
    with respx.mock(assert_all_called=False) as router:
        route = router.get(_BRAVE_ENDPOINT).mock(
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


async def dispatch(tmp_path: Path, arguments: Any, provider: str = "brave") -> dict[str, Any]:
    registry = ToolRegistry()
    register_web_search_tool(registry, _fake_credential_resolver, lambda: {"provider": provider})
    return await registry.dispatch(make_context(tmp_path), arguments)


def sent(route: Any) -> dict[str, str]:
    assert route.call_count == 1
    return dict(route.calls[0].request.url.params)


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
    ],
)
async def test_other_spellings_run_the_same_search(
    tmp_path: Path, brave: Any, arguments: dict[str, Any], params: dict[str, str]
) -> None:
    result = await dispatch(tmp_path, arguments)

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
    result = await dispatch(tmp_path, arguments)

    assert sent(brave)["q"] == query
    shown = assert_success_envelope(result)
    assert {key: shown[key] for key in data} == data
    assert "reddit.com" not in shown["content"]
    assert "https://docs.python.org/3/" in shown["content"]


@pytest.mark.asyncio
async def test_site_operators_become_native_filters_for_other_providers(tmp_path: Path) -> None:
    with respx.mock() as router:
        route = router.post(_TAVILY_ENDPOINT).respond(
            200,
            json={
                "results": [
                    {"title": "Docs", "url": "https://docs.python.org/3/", "content": "Guide"},
                    {"title": "Leak", "url": "https://other.test/", "content": "Off site"},
                ]
            },
        )
        result = await dispatch(
            tmp_path, {"query": 'asyncio "task group" site:python.org'}, provider="tavily"
        )

    body = _read_json_body(route.calls[0].request)
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
    result = await dispatch(tmp_path, arguments)

    assert sent(brave).get("freshness") == freshness
    assert result["data"]["note"] == note


@pytest.mark.asyncio
async def test_a_start_date_uses_the_shortest_covering_window(tmp_path: Path, brave: Any) -> None:
    start = (datetime.now(UTC) - timedelta(days=10)).date().isoformat()

    result = await dispatch(tmp_path, {"query": "python", "published_after": start})

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
            "web_search cannot return only results published before a date",
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
        ({"q": "python", "query": "rust"}, "Conflicting values for query"),
        ({"query": "python", "freshness": "week", "recency": "day"}, "Conflicting values"),
        ({"query": "python", "recency": "decade"}, '"recency" must be one of'),
    ],
)
async def test_unclear_or_impossible_requests_are_refused_before_searching(
    tmp_path: Path, brave: Any, arguments: dict[str, Any], message: str
) -> None:
    with pytest.raises(ToolContractError) as error:
        await dispatch(tmp_path, arguments)

    assert message in str(error.value)
    assert brave.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
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
async def test_contradictory_filters_fail_without_searching(
    tmp_path: Path, brave: Any, arguments: dict[str, Any], message: str
) -> None:
    result = await dispatch(tmp_path, arguments)

    error = assert_failure_envelope(result, "invalid_arguments")
    assert error["message"] == message
    assert brave.call_count == 0


@pytest.mark.asyncio
async def test_results_read_as_a_numbered_list_and_count_for_the_row(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_web_search_tool(registry, _fake_credential_resolver)
    context = make_context(tmp_path)
    long_text = "word " * 150
    with respx.mock() as router:
        router.get(_BRAVE_ENDPOINT).respond(
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
