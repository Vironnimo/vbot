"""Behavioral regressions: information retention, bounded Context, and continuation."""

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
        assert data["offset"] == sum(map(len, parts))
        assert data["returned_chars"] == len(data["content"])
        parts.append(data["content"])
        if "next" not in data:
            assert "hint" not in data
            break
        assert "next" in data["hint"]
        assert set(data["next"]) == {"ref"}
        response = await tool.dispatch(context, data["next"])
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
        "core.tools.web_fetch._http_get",
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
    monkeypatch.setattr("core.tools.web_fetch._http_get", fetch)
    tool, context = registry(), make_context(tmp_path)
    first = await tool.dispatch(context, {"url": "https://example.com/redirect"})
    ref = first["data"]["ref"]
    found = await tool.dispatch(context, {"url": final_url, "ref": ref, "find": "Needle"})
    assert found["ok"] and "Needle fact" in found["data"]["content"]
    continued = await tool.dispatch(context, {"url": final_url, **first["data"]["next"]})
    assert continued["ok"] and continued["data"]["offset"] > 0
    for url in ("https://example.com/other", "https://example.com/redirect"):
        rejected = await tool.dispatch(context, {"url": url, "ref": ref})
        assert rejected["error"]["code"] == "validation_error"
    for changed in (replace(context, session_id="other"), replace(context, agent_id="other")):
        rejected = await tool.dispatch(changed, {"url": final_url, "ref": ref})
        assert rejected["error"]["code"] == "reference_error"
    rejected = await tool.dispatch(context, {"url": final_url, "ref": ref, "output": "raw"})
    assert not rejected["ok"]
    with pytest.raises(ToolContractError, match='"fresh" is not a parameter'):
        await tool.dispatch(context, {"url": final_url, "ref": ref, "fresh": True})
    now = pages.time.time()
    monkeypatch.setattr(pages.time, "time", lambda: now + 73 * 3600)
    expired = await tool.dispatch(context, {"url": final_url, "ref": ref})
    assert expired["error"]["code"] == "reference_error"
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_continuations_recover_all_matches_without_fetching(tmp_path, monkeypatch):
    body = "\n".join("Filler " * 100 + f"Needle{i:02d}" for i in range(30))
    fetch = AsyncMock(return_value=make_result(text=body))
    monkeypatch.setattr("core.tools.web_fetch._http_get", fetch)
    tool, context = registry(), make_context(tmp_path)
    response = await tool.dispatch(context, {"url": "https://example.com/", "find": "Needle"})
    found = set()
    while True:
        assert response["ok"]
        data = response["data"]
        found.update(re.findall(r"Needle[0-9]{2}", data["content"]))
        if "next" not in data:
            assert "hint" not in data
            break
        assert "search" in data["hint"] and "next" in data["hint"]
        assert set(data["next"]) == {"ref", "find"}
        response = await tool.dispatch(context, data["next"])
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
        assert result["error"]["code"] == "reference_error"
    result = await tool.dispatch(context, {"ref": "../settings"})
    assert result["error"]["code"] == "reference_error"
    now = pages.time.time()
    monkeypatch.setattr(pages.time, "time", lambda: now + 73 * 3600)
    expired = await tool.dispatch(context, {"ref": ref})
    assert expired["error"]["code"] == "reference_error"


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
    monkeypatch.setattr("core.tools.web_fetch._http_get", fetch)
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
    assert result["data"]["total_chars"] == 1500
    assert any("partial" in value for value in result["data"]["warnings"])


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
