"""web_fetch saved pages: bounded parts, continuations and search without fetching
again, and refs owned by one Agent in one Session for a limited time."""

import json
import re
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from core.tools import _web_fetch_pages as pages
from core.tools.contracts import ToolContractError
from core.utils.tokens import estimate_json_tokens, estimate_tokens
from tests.core.tools.web_fetch_test_support import (
    fetch,
    install_http_get,
    make_context,
    make_result,
    web_fetch_registry,
)
from tests.core.tools.web_fetch_test_support import stub_dns_resolution as stub_dns_resolution
from tests.core.tools.web_fetch_test_support import stub_http_session as stub_http_session


def continuation(data):
    """The follow-up call a result's more line offers first."""
    match = re.search(r"Continue with (\{.*?\})[,.]", data["more"])
    assert match is not None, data["more"]
    return json.loads(match[1])


def shown_range(data):
    match = re.fullmatch(r"characters (\d+)-(\d+) of (\d+)(?: of the whole page)?", data["shown"])
    assert match is not None, data["shown"]
    return int(match[1]), int(match[2]), int(match[3])


@pytest.mark.asyncio
async def test_continuations_recover_all_unicode_text_with_one_fetch(tmp_path, monkeypatch):
    body = "日本語 🧭 café\n" * 2500 + "THE END"
    calls = []

    def respond(url):
        calls.append(url)
        return make_result(text=body, headers={"content-type": "text/plain"}, url=url)

    install_http_get(monkeypatch, respond)
    tool = web_fetch_registry()
    context = make_context(tmp_path)
    response = await tool.dispatch(context, {"url": "https://example.com/long"})
    parts = []
    while True:
        data = response["data"]
        assert response["ok"] and estimate_tokens(data["content"])[0] <= pages.MAX_CONTENT_TOKENS
        assert estimate_json_tokens(response)[0] < 4500
        start, end, total = shown_range(data)
        assert start == sum(map(len, parts)) + 1
        assert end - start + 1 == len(data["content"]) and total == len(body)
        parts.append(data["content"])
        if "more" not in data:
            break
        assert data["more"].startswith(f"{total - end} more characters. Continue with ")
        assert f'search the page with {{"ref": "{data["ref"]}", "find": "..."}}' in data["more"]
        follow_up = continuation(data)
        assert set(follow_up) == {"ref"}
        # A copied continuation object under "next" is accepted as the call itself.
        response = await tool.dispatch(context, {"next": follow_up})
    assert "".join(parts) == body
    assert len(calls) == 1 and len(parts) > 1


@pytest.mark.asyncio
async def test_find_searches_full_page_and_match_reference_reads_more(tmp_path, monkeypatch):
    html = (
        "<main>Short article</main><footer>"
        + "Policy details. " * 2500
        + "ZEBRA42 license"
        + "</footer>"
    )
    install_http_get(monkeypatch, lambda url: make_result(text=html, url=url))
    tool, context = web_fetch_registry(), make_context(tmp_path)
    first = await tool.dispatch(context, {"url": "https://example.com/"})
    assert first["data"]["content"] == "Short article"
    monkeypatch.setattr(
        "core.tools._public_http._http_get",
        AsyncMock(side_effect=AssertionError("ref must not fetch")),
    )
    found = await tool.dispatch(context, {"ref": first["data"]["ref"], "find": "zebra42"})
    assert "ZEBRA42 license" in found["data"]["content"]
    match = re.search(r"\[ref ([^\]]+)\]", found["data"]["content"])
    assert match is not None
    full = await tool.dispatch(context, {"ref": match[1]})
    assert "ZEBRA42" in full["data"]["content"]


@pytest.mark.asyncio
async def test_search_continuations_recover_all_matches_without_fetching(tmp_path, monkeypatch):
    body = "\n".join("Filler " * 100 + f"Needle{i:02d}" for i in range(30))
    network = AsyncMock(return_value=make_result(text=body))
    monkeypatch.setattr("core.tools._public_http._http_get", network)
    tool, context = web_fetch_registry(), make_context(tmp_path)
    response = await tool.dispatch(context, {"url": "https://example.com/", "find": "Needle"})
    found = set()
    while True:
        assert response["ok"]
        data = response["data"]
        found.update(re.findall(r"Needle[0-9]{2}", data["content"]))
        assert 'matching "Needle"' in data["shown"]
        if "more" not in data:
            break
        assert data["more"].startswith("More matches. Continue with ")
        follow_up = continuation(data)
        assert set(follow_up) == {"ref", "find"}
        response = await tool.dispatch(context, follow_up)
    assert found == {f"Needle{i:02d}" for i in range(30)}
    network.assert_awaited_once()


@pytest.mark.asyncio
async def test_redundant_url_must_name_the_saved_page(tmp_path, monkeypatch):
    final_url = "https://example.com/reference"
    network = AsyncMock(return_value=make_result(text="Needle fact. " * 1500, url=final_url))
    monkeypatch.setattr("core.tools._public_http._http_get", network)
    tool, context = web_fetch_registry(), make_context(tmp_path)
    first = await tool.dispatch(context, {"url": "https://example.com/redirect"})
    ref = first["data"]["ref"]
    found = await tool.dispatch(context, {"url": final_url, "ref": ref, "find": "Needle"})
    assert found["ok"] and "Needle fact" in found["data"]["content"]
    continued = await tool.dispatch(context, {"url": final_url, **continuation(first["data"])})
    assert continued["ok"] and shown_range(continued["data"])[0] > 1
    # The address that was requested names the same saved page as its final URL.
    requested = await tool.dispatch(context, {"url": "https://example.com/redirect", "ref": ref})
    assert requested["ok"]
    rejected = await tool.dispatch(context, {"url": "https://example.com/other", "ref": ref})
    assert rejected["error"]["code"] == "invalid_arguments"
    assert rejected["error"]["message"] == (
        f"ref {ref} is the saved page of {final_url}, not https://example.com/other. Pass "
        "only ref to read the saved page, or only url to fetch the other address."
    )
    with pytest.raises(ToolContractError, match='"fresh" is not a parameter'):
        await tool.dispatch(context, {"url": final_url, "ref": ref, "fresh": True})
    network.assert_awaited_once()


@pytest.mark.asyncio
async def test_refs_are_refused_across_sessions_agents_expiry_and_traversal(tmp_path, monkeypatch):
    install_http_get(monkeypatch, lambda url: make_result(text="A real page", url=url))
    tool, context = web_fetch_registry(), make_context(tmp_path)
    first = await tool.dispatch(context, {"url": "https://example.com/"})
    ref = first["data"]["ref"]
    for changed in (replace(context, session_id="other"), replace(context, agent_id="other")):
        result = await tool.dispatch(changed, {"ref": ref})
        assert result["error"]["code"] == "page_expired"
    result = await tool.dispatch(context, {"ref": "../settings"})
    assert result["error"]["code"] == "invalid_ref"
    assert result["error"]["message"] == (
        '"../settings" is not a web_fetch ref. Refs come from earlier web_fetch results and '
        "look like tmp_7k2m9x4q1b3c or tmp_7k2m9x4q1b3c.m.12000. To fetch a page, pass its "
        "address as url."
    )
    now = pages.time.time()
    monkeypatch.setattr(pages.time, "time", lambda: now + 73 * 3600)
    expired = await tool.dispatch(context, {"ref": ref})
    assert expired["error"]["code"] == "page_expired"
    assert expired["error"]["message"] == (
        f"Saved page {ref} is not available: saved pages expire after 72 hours and can only "
        "be read in the conversation that fetched them. Fetch the page again with url."
    )


@pytest.mark.asyncio
async def test_saved_limit_is_reported_without_claiming_completeness(tmp_path, monkeypatch):
    monkeypatch.setattr(pages, "MAX_SAVED_CHARS", 1500)
    install_http_get(monkeypatch, lambda url: make_result(text="Text " * 1000, url=url))
    result = await fetch(tmp_path, {"url": "https://example.com/"})
    assert len(result["data"]["content"]) == 1500 and "more" not in result["data"]
    assert "Saved content is partial" in result["data"]["note"]


@pytest.mark.asyncio
async def test_the_user_sees_the_page_text_or_the_passages_and_whether_more_remains(
    tmp_path, monkeypatch
):
    body = "\n".join("Filler " * 100 + f"Needle{i:02d}" for i in range(30))
    install_http_get(
        monkeypatch, lambda url: make_result(text=body, headers={"content-type": "text/plain"})
    )
    tool, context = web_fetch_registry(), make_context(tmp_path)
    content = {"type": "text", "source": {"from": "result", "path": ["data", "content"]}}

    read = {"url": "https://example.com/"}
    page = await tool.dispatch(context, read)
    found = {"ref": page["data"]["ref"], "find": "Needle"}
    passages = await tool.dispatch(context, found)

    # The text is read from the result, not copied; refs and follow-up calls stay raw.
    assert tool.display_for_call("web_fetch", read, result=page)["details"] == [
        {**content, "label": "page"},
        {"type": "notice", "level": "info", "text": f"Shows {page['data']['shown']}."},
    ]
    assert tool.display_for_call("web_fetch", found, result=passages)["details"] == [
        {**content, "label": "results"},
        {"type": "notice", "level": "info", "text": "More matches follow on the page."},
    ]
