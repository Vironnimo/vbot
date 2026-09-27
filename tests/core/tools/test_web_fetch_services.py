"""web_fetch fetch services: vendor wire contracts and opt-in routing, without paid
network calls. Each service request is billable, so it is never repeated."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

import core.tools._public_http as public_http
from core.tools import _web_fetch_services as services
from tests.core.tools.web_fetch_test_support import (
    fetch,
    install_http_get,
    make_context,
    make_result,
    web_fetch_registry,
)
from tests.core.tools.web_fetch_test_support import stub_dns_resolution as stub_dns_resolution
from tests.core.tools.web_fetch_test_support import stub_http_session as stub_http_session

URL = "https://example.com/article"
TAVILY = "https://api.tavily.com/extract"
EXA = "https://api.exa.ai/contents"
_SERVICE_NOTE = (
    "Saved content covers only the service's extraction; inaccessible or interactive "
    "sections may be missing."
)


def _service(provider: str, mode: str, key: str | None = "fixture-key") -> dict[str, Any]:
    """Registration options selecting one fetch service in Settings."""
    return {
        "credential_resolver": lambda _variable: key,
        "settings_loader": lambda: {"provider": provider, "mode": mode},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,endpoint,response,field",
    [
        (
            "firecrawl",
            "https://api.firecrawl.dev/v2/scrape",
            {
                "success": True,
                "data": {
                    "markdown": "Full page",
                    "metadata": {"title": "Example", "sourceURL": URL},
                },
            },
            "url",
        ),
        ("tavily", TAVILY, {"results": [{"url": URL, "raw_content": "Full page"}]}, "urls"),
        ("exa", EXA, {"results": [{"url": URL, "text": "Full page"}]}, "ids"),
        (
            "parallel",
            "https://api.parallel.ai/v1/extract",
            {"results": [{"url": URL, "full_content": "Full page"}]},
            "urls",
        ),
    ],
)
async def test_preferred_service_gets_one_full_page_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    endpoint: str,
    response: dict[str, Any],
    field: str,
) -> None:
    direct = AsyncMock()
    monkeypatch.setattr(public_http, "_http_get", direct)
    with respx.mock() as router:
        route = router.post(endpoint).respond(json=response)
        result = await fetch(tmp_path, {"url": URL}, **_service(provider, "prefer"))

    assert route.call_count == 1
    request = route.calls[0].request
    payload = json.loads(request.content)
    assert payload[field] == (URL if field == "url" else [URL])
    assert request.headers.get("authorization", request.headers.get("x-api-key")) in {
        "Bearer fixture-key",
        "fixture-key",
    }
    if provider == "firecrawl":
        assert payload["onlyMainContent"] is False and payload["maxAge"] == 0
    elif provider == "tavily":
        assert "query" not in payload and payload["extract_depth"] == "advanced"
    elif provider == "exa":
        assert payload["text"] is True and payload["maxAgeHours"] == 0
    else:
        assert payload["advanced_settings"]["full_content"] is True
    assert result["ok"] and result["data"]["content"] == "Full page"
    assert result["data"]["note"] == _SERVICE_NOTE
    direct.assert_not_awaited()


def _time_out(route: respx.Route) -> None:
    route.mock(side_effect=httpx.ReadTimeout("uncertain"))


def _status(status: int) -> Callable[[respx.Route], None]:
    def configure(route: respx.Route) -> None:
        route.respond(status, text="credential-like-body-must-stay-private")

    return configure


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "provider", "configure", "max_bytes", "reason"),
    [
        (
            TAVILY,
            "tavily",
            _time_out,
            None,
            "tavily request failed or timed out. It was not automatically repeated; a retry "
            "may be billed again.",
        ),
        (TAVILY, "tavily", _status(401), None, "tavily returned HTTP 401. Check the API key."),
        (
            TAVILY,
            "tavily",
            _status(429),
            None,
            "tavily returned HTTP 429. Check the service quota and account.",
        ),
        (
            TAVILY,
            "tavily",
            _status(503),
            None,
            "tavily returned HTTP 503. Try another source or try later.",
        ),
        (
            EXA,
            "exa",
            lambda route: route.respond(json={"results": []}),
            None,
            "exa could not extract the page.",
        ),
        (
            EXA,
            "exa",
            lambda route: route.respond(content=b"x" * 31),
            30,
            "exa response exceeds the 12 MB limit.",
        ),
    ],
    ids=["timeout", "unauthorized", "quota", "unavailable", "empty", "oversized"],
)
async def test_failed_preferred_service_is_not_repeated_and_direct_fetch_answers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    provider: str,
    configure: Callable[[respx.Route], None],
    max_bytes: int | None,
    reason: str,
) -> None:
    if max_bytes is not None:
        monkeypatch.setattr(services, "_MAX_BYTES", max_bytes)
    install_http_get(monkeypatch, lambda url: make_result(text="Direct content", url=url))
    with respx.mock() as router:
        route = router.post(endpoint)
        configure(route)
        result = await fetch(tmp_path, {"url": URL}, **_service(provider, "prefer"))

    assert route.call_count == 1
    assert result["ok"] and result["data"]["content"] == "Direct content"
    assert result["data"]["note"] == f"Service unavailable; used direct fetch. {reason}"
    serialized = json.dumps(result)
    assert "fixture-key" not in serialized and "credential-like" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fallback", "prefer"])
async def test_service_page_is_saved_and_followups_never_bill_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    install_http_get(monkeypatch, lambda url: make_result(status_code=403, url=url))
    tool = web_fetch_registry(**_service("tavily", mode))
    context = make_context(tmp_path)
    page = {"results": [{"url": URL, "raw_content": "Service content " * 1000}]}
    with respx.mock() as router:
        route = router.post(TAVILY).respond(json=page)
        first = await tool.dispatch(context, {"url": URL})
        assert first["ok"] and first["data"]["content"].startswith("Service content")
        found = re.search(r"Continue with (\{.*?\})", first["data"]["more"])
        assert found is not None
        second = await tool.dispatch(context, json.loads(found[1]))

    assert second["ok"] and second["data"]["content"].startswith("Service content")
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_failed_recovery_keeps_the_direct_failure_and_adds_the_service_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_http_get(monkeypatch, lambda url: make_result(status_code=403, url=url))

    result = await fetch(tmp_path, {"url": URL}, **_service("firecrawl", "fallback", key=None))

    assert result["error"]["code"] == "access_denied"
    assert result["error"]["message"].endswith(
        "Try another source. The firecrawl fetch service also failed: The firecrawl fetch "
        "service is selected in Settings, but FIRECRAWL_API_KEY is not set in the .env file "
        "of the vBot data directory. Tell the user: they can add the key, or set Web Fetch "
        "back to Direct in Settings."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "status", "location", "options"),
    [
        ("http://127.0.0.1/", 200, None, _service("tavily", "fallback")),
        (URL, 302, "http://127.0.0.1/", _service("tavily", "fallback")),
        (URL, 302, "https://user:password@public.example/", _service("tavily", "fallback")),
        (URL, 404, None, _service("tavily", "fallback")),
        # Direct stays the default even when a service key exists.
        (URL, 403, None, {"credential_resolver": lambda _variable: "fixture-key"}),
    ],
    ids=["private", "redirect-private", "redirect-credentials", "missing", "default-direct"],
)
async def test_no_service_for_blocked_targets_missing_pages_or_direct_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    status: int,
    location: str | None,
    options: dict[str, Any],
) -> None:
    install_http_get(
        monkeypatch,
        lambda target: make_result(
            status_code=status,
            headers={"location": location} if location else {},
            text="Blocked",
            url=target,
        ),
    )
    with respx.mock(assert_all_called=False) as router:
        route = router.post(TAVILY).respond(json={})
        result = await fetch(tmp_path, {"url": url}, **options)

    assert not result["ok"]
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_cancellation_propagates_without_direct_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    direct = AsyncMock()
    monkeypatch.setattr(public_http, "_http_get", direct)
    requests: list[httpx.Request] = []

    def cancel(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise asyncio.CancelledError

    # respx records no call when the side effect cancels, so count requests here.
    with respx.mock(assert_all_called=False) as router:
        router.post("https://api.parallel.ai/v1/extract").mock(side_effect=cancel)
        with pytest.raises(asyncio.CancelledError):
            await fetch(tmp_path, {"url": URL}, **_service("parallel", "prefer"))

    assert len(requests) == 1
    direct.assert_not_awaited()
