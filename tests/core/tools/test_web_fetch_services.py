"""Vendor wire contracts and opt-in routing without paid network calls."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from core.tools import _web_fetch_services as services
from core.tools.web_fetch import make_web_fetch_handler
from tests.core.tools.web_fetch_helpers import install_http_get, make_context, make_result
from tests.core.tools.web_fetch_helpers import stub_dns_resolution as stub_dns_resolution
from tests.core.tools.web_fetch_helpers import stub_http_session as stub_http_session

URL = "https://example.com/article"


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
        (
            "tavily",
            "https://api.tavily.com/extract",
            {"results": [{"url": URL, "raw_content": "Full page"}]},
            "urls",
        ),
        (
            "exa",
            "https://api.exa.ai/contents",
            {"results": [{"url": URL, "text": "Full page"}]},
            "ids",
        ),
        (
            "parallel",
            "https://api.parallel.ai/v1/extract",
            {"results": [{"url": URL, "full_content": "Full page"}]},
            "urls",
        ),
    ],
)
async def test_vendor_requests_full_page_and_normalizes_result(provider, endpoint, response, field):
    with respx.mock() as router:
        route = router.post(endpoint).respond(json=response)
        result = await services.fetch_service(provider, "fixture-key", URL, "markdown")
    request = route.calls[0].request
    payload = json.loads(request.content)
    assert payload[field] == (URL if field == "url" else [URL])
    assert request.headers.get("authorization", request.headers.get("x-api-key")) in {
        "Bearer fixture-key",
        "fixture-key",
    }
    assert result["content"] == "Full page" and result["source"] == provider
    assert route.call_count == 1
    if provider == "firecrawl":
        assert payload["onlyMainContent"] is False and payload["maxAge"] == 0
    elif provider == "tavily":
        assert "query" not in payload and payload["extract_depth"] == "advanced"
    elif provider == "exa":
        assert payload["text"] is True and payload["maxAgeHours"] == 0
    else:
        assert payload["advanced_settings"]["full_content"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ReadTimeout("uncertain"), 401, 429, 503])
async def test_billable_requests_are_not_automatically_replayed(failure):
    with respx.mock() as router:
        route = router.post("https://api.tavily.com/extract")
        if isinstance(failure, int):
            route.respond(failure, text="credential-like-body-must-stay-private")
        else:
            route.mock(side_effect=failure)
        with pytest.raises(services.FetchServiceError) as error:
            await services.fetch_service("tavily", "fixture-key", URL, "markdown")
        assert route.call_count == 1
        assert "fixture-key" not in str(error.value) and "credential-like" not in str(error.value)


@pytest.mark.asyncio
async def test_provider_bound_and_empty_response_failures(monkeypatch):
    with respx.mock() as router:
        route = router.post("https://api.exa.ai/contents").respond(json={"results": []})
        with pytest.raises(services.FetchServiceError):
            await services.fetch_service("exa", "key", URL, "markdown")
        monkeypatch.setattr(services, "_MAX_BYTES", 30)
        route.respond(content=b"x" * 31)
        with pytest.raises(services.FetchServiceError, match="limit"):
            await services.fetch_service("exa", "key", URL, "markdown")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fallback", "prefer"])
async def test_service_success_is_saved_and_followups_never_bill_again(tmp_path, monkeypatch, mode):
    install_http_get(monkeypatch, lambda url: make_result(status_code=403, url=url))
    service = AsyncMock(
        return_value={"content": "Service content " * 1000, "url": URL, "source": "tavily"}
    )
    monkeypatch.setattr("core.tools.web_fetch.fetch_service", service)
    tool = make_web_fetch_handler(
        None,
        credential_resolver=lambda _: "key",
        settings_loader=lambda: {"provider": "tavily", "mode": mode},
    )
    context = make_context(tmp_path)
    first = await tool(context, {"url": URL})
    assert first["ok"]
    second = await tool(context, first["data"]["next"])
    assert second["ok"] and second["data"]["source"] == "tavily"
    service.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url,status,location",
    [
        ("http://127.0.0.1/", 200, None),
        (URL, 302, "http://127.0.0.1/"),
        (URL, 302, "https://user:password@public.example/"),
        (URL, 404, None),
    ],
)
async def test_no_service_on_blocked_targets_or_missing_pages(
    tmp_path, monkeypatch, url, status, location
):
    install_http_get(
        monkeypatch,
        lambda target: make_result(
            status_code=status,
            headers={"location": location} if location else {},
            text="Blocked",
            url=target,
        ),
    )
    service = AsyncMock()
    monkeypatch.setattr("core.tools.web_fetch.fetch_service", service)
    tool = make_web_fetch_handler(
        None,
        credential_resolver=lambda _: "key",
        settings_loader=lambda: {"provider": "tavily", "mode": "fallback"},
    )
    result = await tool(make_context(tmp_path), {"url": url})
    assert not result["ok"]
    service.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_direct_does_not_enable_service_because_key_exists(tmp_path, monkeypatch):
    install_http_get(monkeypatch, lambda url: make_result(status_code=403, url=url))
    service = AsyncMock()
    monkeypatch.setattr("core.tools.web_fetch.fetch_service", service)
    result = await make_web_fetch_handler(None, credential_resolver=lambda _: "key")(
        make_context(tmp_path), {"url": URL}
    )
    assert not result["ok"]
    service.assert_not_awaited()


@pytest.mark.asyncio
async def test_preferred_failure_recovers_directly_with_source_and_warning(tmp_path, monkeypatch):
    install_http_get(monkeypatch, lambda url: make_result(text="Direct content", url=url))
    service = AsyncMock(side_effect=services.FetchServiceError("Service temporarily unavailable"))
    monkeypatch.setattr("core.tools.web_fetch.fetch_service", service)
    tool = make_web_fetch_handler(
        None,
        credential_resolver=lambda _: "key",
        settings_loader=lambda: {"provider": "exa", "mode": "prefer"},
    )
    result = await tool(make_context(tmp_path), {"url": URL})
    assert result["ok"] and result["data"]["source"] == "direct"
    assert any("Service unavailable" in item for item in result["data"]["warnings"])


@pytest.mark.asyncio
async def test_cancellation_propagates_without_direct_replay(tmp_path, monkeypatch):
    direct = AsyncMock()
    monkeypatch.setattr("core.tools.web_fetch._http_get", direct)
    monkeypatch.setattr(
        "core.tools.web_fetch.fetch_service", AsyncMock(side_effect=asyncio.CancelledError)
    )
    tool = make_web_fetch_handler(
        None,
        credential_resolver=lambda _: "key",
        settings_loader=lambda: {"provider": "parallel", "mode": "prefer"},
    )
    with pytest.raises(asyncio.CancelledError):
        await tool(make_context(tmp_path), {"url": URL})
    direct.assert_not_awaited()
