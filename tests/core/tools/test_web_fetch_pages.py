"""Behavioral regressions: information retention, bounded Context, and continuation."""

import json
import re
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from core.tools import _web_fetch_pages as pages
from core.tools._web_fetch_html import extract_views
from core.tools.contracts import ToolContractError
from core.tools.tools import ToolRegistry
from core.tools.web_fetch import register_web_fetch_tool
from core.utils.tokens import estimate_json_tokens, estimate_tokens
from tests.core.tools.web_fetch_helpers import install_http_get, make_context, make_result
from tests.core.tools.web_fetch_helpers import stub_dns_resolution as stub_dns_resolution
from tests.core.tools.web_fetch_helpers import stub_http_session as stub_http_session


def registry():
    result = ToolRegistry()
    register_web_fetch_tool(result, attachment_store=None)
    return result


def continuation(data):
    """The follow-up call a result's more line offers first."""
    match = re.search(r"Continue with (\{.*?\})[,.]", data["more"])
    assert match is not None, data["more"]
    return json.loads(match[1])


def shown_range(data):
    match = re.fullmatch(r"characters (\d+)-(\d+) of (\d+)(?: of the whole page)?", data["shown"])
    assert match is not None, data["shown"]
    return int(match[1]), int(match[2]), int(match[3])


def test_retains_nested_code_lists_tables_and_meaningful_containers():
    html = """<head><base href="https://example.com/docs/"><title>Reference</title></head>
    <body><nav><a href="/">Navigation</a></nav><main>
    <div class="thread-container">Thread fact</div><div id="download-container">Download fact</div>
    <article class="gdpr">GDPR fact</article>
    <ol start="3"><li>Install <pre><code class="language-sh">pip install useful
    echo `done`</code></pre>Then <b>start</b> it.<ul><li>Child A</li><li>Child B</li></ul></li></ol>
    <table><caption>Prices in EUR per month</caption><tr><th>Plan</th><th>Price</th></tr>
    <tr><td rowspan="2">Basic</td><td>12</td></tr><tr><td>15</td></tr></table>
    <a href="install">Install guide</a>
    <img data-src="chart.png">
    <img alt="Trend" src="data:image/png;base64,AAAA">
    <p style="display: none !important">Stale price 99</p>
    <div class="ad-container">Advertisement</div>
    </main><footer>License fact</footer></body>"""
    main, page, metadata, warnings = extract_views(html, "https://example.com/start")
    for expected in (
        "Thread fact",
        "Download fact",
        "GDPR fact",
        "pip install useful",
        "echo `done`",
        "Child A",
        "Child B",
        "Prices in EUR per month",
        "rowspan=2",
        "https://example.com/docs/install",
        "https://example.com/docs/chart.png",
        "Trend",
    ):
        assert expected in main
    assert "3." in main and "Then **start** it." in main
    assert "License fact" not in main and "License fact" in page
    assert "data:image" not in page and "Advertisement" not in page and "Stale price" not in page
    assert metadata["title"] == "Reference" and warnings


def test_deep_html_keeps_text_with_explicit_layout_limitation():
    main, _, _, warnings = extract_views(
        "<div>" * 250 + "Important fact" + "</div>" * 250, "https://example.com/"
    )
    assert "Important fact" in main
    assert any("layout" in value for value in warnings)


def test_fallback_main_selection_and_text_mode_keep_facts_without_image_targets():
    html = (
        "<nav>Many links</nav><p>Actual article</p>"
        '<img src="chart.png" alt="Sales chart"><footer>Terms</footer>'
    )
    main, page, _, _ = extract_views(html, "https://example.com/", include_links=False)
    assert "Actual article" in main and "Sales chart" in main
    assert "Many links" not in main and "Terms" not in main
    assert "Many links" in page and "Terms" in page
    assert "chart.png" not in page


@pytest.mark.asyncio
async def test_continuations_recover_all_unicode_text_with_one_fetch(tmp_path, monkeypatch):
    body = "日本語 🧭 café\n" * 2500 + "THE END"
    calls = []

    def respond(url):
        calls.append(url)
        return make_result(text=body, headers={"content-type": "text/plain"}, url=url)

    install_http_get(monkeypatch, respond)
    tool = registry()
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
    tool, context = registry(), make_context(tmp_path)
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
async def test_redundant_url_must_match_owned_saved_final_page(tmp_path, monkeypatch):
    final_url = "https://example.com/reference"
    fetch = AsyncMock(return_value=make_result(text="Needle fact. " * 1500, url=final_url))
    monkeypatch.setattr("core.tools._public_http._http_get", fetch)
    tool, context = registry(), make_context(tmp_path)
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
    for changed in (replace(context, session_id="other"), replace(context, agent_id="other")):
        rejected = await tool.dispatch(changed, {"url": final_url, "ref": ref})
        assert rejected["error"]["code"] == "page_expired"
    rejected = await tool.dispatch(context, {"url": final_url, "ref": ref, "output": "raw"})
    assert not rejected["ok"]
    with pytest.raises(ToolContractError, match='"fresh" is not a parameter'):
        await tool.dispatch(context, {"url": final_url, "ref": ref, "fresh": True})
    now = pages.time.time()
    monkeypatch.setattr(pages.time, "time", lambda: now + 73 * 3600)
    expired = await tool.dispatch(context, {"url": final_url, "ref": ref})
    assert expired["error"]["code"] == "page_expired"
    assert expired["error"]["message"] == (
        f"Saved page {ref} is not available: saved pages expire after 72 hours and can only "
        "be read in the conversation that fetched them. Fetch the page again with url."
    )
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_continuations_recover_all_matches_without_fetching(tmp_path, monkeypatch):
    body = "\n".join("Filler " * 100 + f"Needle{i:02d}" for i in range(30))
    fetch = AsyncMock(return_value=make_result(text=body))
    monkeypatch.setattr("core.tools._public_http._http_get", fetch)
    tool, context = registry(), make_context(tmp_path)
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
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_references_reject_cross_session_cross_agent_expiry_and_traversal(
    tmp_path, monkeypatch
):
    install_http_get(monkeypatch, lambda url: make_result(text="A real page", url=url))
    tool, context = registry(), make_context(tmp_path)
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"url": "https://example.com", "ref": "tmp_test"},
        {"ref": "tmp_test", "output": "raw"},
        {"ref": "tmp_test", "offset": -1},
    ],
)
async def test_rejects_ambiguous_or_invalid_requests_before_network(
    tmp_path, monkeypatch, arguments
):
    fetch = AsyncMock(side_effect=AssertionError("must not fetch"))
    monkeypatch.setattr("core.tools._public_http._http_get", fetch)
    # Handler and schema may reject at different layers; both prevent effects.
    try:
        result = await registry().dispatch(make_context(tmp_path), arguments)
        assert not result["ok"]
    except ValueError:
        pass
    fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "html",
    [
        "<div id='root'></div><script>runApp()</script>",
        "\ufeff \n<!-- app -->\n<div id='root'></div><script>runApp()</script>",
        "<h1>Please enable JavaScript</h1>",
        "<title>Access denied</title><p>Verify you are human</p>",
    ],
)
async def test_unusable_pages_are_failures_including_missing_content_type(
    tmp_path, monkeypatch, html
):
    install_http_get(monkeypatch, lambda url: make_result(text=html, url=url))
    result = await registry().dispatch(make_context(tmp_path), {"url": "https://example.com/"})
    assert not result["ok"]


@pytest.mark.asyncio
async def test_saved_limit_is_reported_without_claiming_completeness(tmp_path, monkeypatch):
    monkeypatch.setattr(pages, "MAX_SAVED_CHARS", 1500)
    install_http_get(monkeypatch, lambda url: make_result(text="Text " * 1000, url=url))
    result = await registry().dispatch(make_context(tmp_path), {"url": "https://example.com/"})
    assert len(result["data"]["content"]) == 1500 and "more" not in result["data"]
    assert "Saved content is partial" in result["data"]["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "html",
    [
        "<title>Access denied</title><article>How to diagnose an access denied "
        "error in your application.</article>",
        "<h1>How to enable JavaScript</h1><p>Open browser settings to enable JavaScript.</p>",
    ],
)
async def test_articles_about_access_errors_are_not_treated_as_walls(tmp_path, monkeypatch, html):
    install_http_get(monkeypatch, lambda url: make_result(text=html, url=url))
    result = await registry().dispatch(make_context(tmp_path), {"url": "https://example.com/"})
    assert result["ok"] and result["data"]["content"]


def test_code_breaks_and_svg_labels_remain_readable():
    main, _, _, _ = extract_views(
        "<pre><code>first<br>second `tick`</code></pre><svg><text>2026: 42 units</text></svg>",
        "https://example.com/",
    )
    assert "first\nsecond `tick`" in main and "2026: 42 units" in main
