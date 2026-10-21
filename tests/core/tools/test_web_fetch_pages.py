"""web_fetch saved pages: bounded parts, continuations and search without fetching
again, and refs owned by one Agent in one Session for a limited time."""

import json
import re
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from core.tools import _web_fetch_pages as pages
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


def read_on_calls(content):
    """The read-on calls that end the passages of a find result."""
    return [json.loads(call) for call in re.findall(r"^Read on with (\{.*\})$", content, re.M)]


def shown_range(data):
    match = re.fullmatch(r"characters (\d+)-(\d+) of (\d+)(?: of the whole page)?", data["shown"])
    assert match is not None, data["shown"]
    return int(match[1]), int(match[2]), int(match[3])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "factor",
    [
        pytest.param(None, id="uncalibrated"),
        # The calling Model counts twice the shared estimate, so parts hold half.
        pytest.param(2.0, id="calibrated-model"),
    ],
)
async def test_continuations_recover_all_unicode_text_with_one_fetch(tmp_path, monkeypatch, factor):
    body = "日本語 🧭 café\n" * 2500 + "THE END"
    calls = []

    def respond(url):
        calls.append(url)
        return make_result(text=body, headers={"content-type": "text/plain"}, url=url)

    install_http_get(monkeypatch, respond)
    calibration = {} if factor is None else {"input_estimate_factor": {"p/model": factor}.get}
    tool = web_fetch_registry(**calibration)
    context = replace(make_context(tmp_path), agent_model="p/model")
    max_tokens = pages.MAX_CONTENT_TOKENS / (factor or 1.0)
    response = await tool.dispatch(context, {"url": "https://example.com/long"})
    parts = []
    while True:
        data = response["data"]
        assert response["ok"] and estimate_tokens(data["content"])[0] <= max_tokens
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
        + " Terms apply." * 200
        + "</footer>"
    )
    install_http_get(monkeypatch, lambda url: make_result(text=html, url=url))
    tool, context = web_fetch_registry(), make_context(tmp_path)
    first = await tool.dispatch(context, {"url": "https://example.com/"})
    assert first["data"]["content"] == "Short article"
    assert "Only the main content" in first["data"]["note"]
    monkeypatch.setattr(
        "core.tools._public_http._http_get",
        AsyncMock(side_effect=AssertionError("ref must not fetch")),
    )
    whole = await tool.dispatch(context, {"ref": first["data"]["ref"], "scope": "page"})
    assert "Policy details" in whole["data"]["content"]
    assert "Only the main content" not in whole["data"].get("note", "")
    found = await tool.dispatch(context, {"ref": first["data"]["ref"], "find": "zebra42"})
    passage, _, _ = found["data"]["content"].partition("\nRead on with ")
    assert "ZEBRA42 license" in passage
    # The passage's read-on call continues the page right where the passage stopped.
    (read_on,) = read_on_calls(found["data"]["content"])
    following = await tool.dispatch(context, read_on)
    assert "ZEBRA42" not in following["data"]["content"]
    assert "ZEBRA42 license" + " Terms apply." * 200 in passage + following["data"]["content"]
    # The ref copied with the main content's letter names the call that reads on
    # (an Agent sent .m. for a passage's .p. ref, 2026-10-13).
    slipped = await tool.dispatch(context, {"ref": read_on["ref"].replace(".p.", ".m.")})
    assert slipped["error"]["code"] == "invalid_arguments"
    assert json.dumps(read_on) in slipped["error"]["message"]


@pytest.mark.asyncio
async def test_find_in_code_shows_nearby_matches_once_and_reads_on_after_each_passage(
    tmp_path, monkeypatch
):
    lines = [f"int line{i:03d} = {i};" for i in range(400)]
    for i in (100, 103, 110, 300):
        lines[i] += " /* needle */"
    install_http_get(
        monkeypatch,
        lambda url: make_result(text="\n".join(lines), headers={"content-type": "text/plain"}),
    )
    tool, context = web_fetch_registry(), make_context(tmp_path)

    found = await tool.dispatch(context, {"url": "https://example.com/a.c", "find": "needle"})

    data = found["data"]
    # Matches a few lines apart share one passage; no line is shown twice.
    assert data["shown"].startswith("2 passages")
    shown = [line for line in data["content"].split("\n") if line.startswith("int ")]
    assert len(shown) == len(set(shown)) and lines[110] in shown and lines[300] in shown
    # Each passage ends with the call that reads on from the next whole line.
    for call in read_on_calls(data["content"]):
        following = await tool.dispatch(context, call)
        assert following["data"]["content"].split("\n")[0] in lines
    # A plain text page has no navigation or footers to name when nothing matches.
    missing = await tool.dispatch(context, {"ref": data["ref"], "find": "absent"})
    assert "navigation" not in missing["data"]["content"]


_ELEMENT_PAGE = """<main>
<p><b>Iron</b> is a <a href="/wiki/Element">chemical element</a>; it has symbol Fe.</p>
<table>
<tr><th>Property</th><th>Value</th></tr>
<tr><td><b>Discovery date</b></td><td>approx 3500BC</td></tr>
<tr><td><a href="/n">Name</a>, <a href="/s">Symbol</a>, <a href="/z">Ordnungszahl</a></td>
<td>Eisen, Fe, 26</td></tr>
<tr><td><b>Density</b></td><td>7.87</td></tr>
</table>
<ul>
<li><a href="/group">Group name</a>: Lanthanoid</li>
<li><a href="/melting"><img src="data:image/png;base64,AA" alt="spark table">
 Melting point</a>: 1811 K</li>
<li>Density of solid: 968&nbsp;kg&#8239;m&#8209;3,
 measured by &ldquo;Smith&rdquo; at &minus;5&nbsp;&deg;C</li>
<li>Isotope <sup>56</sup>Fe at 3.0&times;10<sup>5</sup> K in H<sub>2</sub>O</li>
</ul>
</main>"""


@pytest.mark.parametrize(
    ("find", "place"),
    [
        ("Iron is a chemical element", "**Iron** is a [chemical element]"),
        ("Discovery date approx 3500BC", "| **Discovery date** | approx 3500BC |"),
        ("name, symbol, ordnungszahl eisen", "[Name](/n), [Symbol]"),
        ("Group name: Lanthanoid", "[Group name](/group): Lanthanoid"),
        ("Melting point: 1811", "Melting point](/melting): 1811"),
        # Superscripts and subscripts match written either way.
        ("56Fe at 3.0×105 K in H2O", "⁵⁶Fe at 3.0×10⁵ K in H₂O"),
        ("⁵⁶Fe at 3.0×10^5 K in H₂O", "⁵⁶Fe at 3.0×10⁵ K in H₂O"),
        ('968 kg m-3, measured by "Smith" at -5 °C', "968 kg"),
        # Markdown copied from a result finds its place in the readable text too.
        ("| **Density** | 7.87 |", "| **Density** | 7.87 |"),
        ("**Density** 7.87", "| **Density** | 7.87 |"),
        ("[Group name](/group): Lanthanoid", "Group name](/group)"),
        # A link of the page's own site, named with the site.
        ("[Group name](https://example.com/group): Lanthanoid", "Group name](/group)"),
        # Every other character still counts.
        ("Discovery date approx 3600BC", None),
        ("Iron is a chemical elements", None),
        ("Density 7.871", None),
        ("[Group name](https://example.com/other): Lanthanoid", None),
        ("968kg", None),
        ("57Fe", None),
    ],
)
@pytest.mark.asyncio
async def test_find_reads_the_page_text_past_its_markdown(tmp_path, monkeypatch, find, place):
    install_http_get(monkeypatch, lambda url: make_result(text=_ELEMENT_PAGE, url=url))

    found = await fetch(tmp_path, {"url": "https://example.com/iron", "find": find})

    data = found["data"]
    if place is None:
        assert "shown" not in data and "Read on with" not in data["content"]
    else:
        assert data["shown"].startswith("1 passage") and place in data["content"]


_FILLER = "<p>" + "Filler sentence. " * 40 + "</p>"
_WORDS_PAGE = _FILLER.join(
    [
        "<main><p>Anemias are caused by iron deficiency.</p>",
        "<p>The environment changes.</p>",
        "<p>Magnetic fields.</p>",
        "<p>鉄は元素です。</p></main>",
    ]
)


@pytest.mark.parametrize(
    ("find", "places", "named"),
    [
        # Found only inside a longer word, which the result names (Swarm run 2026-10-12).
        ("used by", ["caused by"], "caused by"),
        # A match inside a word counts, so stems and compounds are found.
        ("iron", ["iron deficiency", "environment"], None),
        ("magnet", ["Magnetic"], None),
        # Scripts without spaces between words have no longer words to name.
        ("元素です", ["鉄は元素です"], None),
    ],
)
@pytest.mark.asyncio
async def test_find_names_the_longer_words_that_hold_every_match(
    tmp_path, monkeypatch, find, places, named
):
    install_http_get(monkeypatch, lambda url: make_result(text=_WORDS_PAGE, url=url))

    found = await fetch(tmp_path, {"url": "https://example.com/words", "find": find})

    data = found["data"]
    assert data["shown"].startswith(f"{len(places)} passage")
    assert all(place in data["content"] for place in places)
    if named is not None:
        assert f'"{named}"' in data["shown"]
    else:
        assert data["shown"].count('"') == 2  # only the search text itself is quoted


_RULES_PAGE = _FILLER.join(
    [
        "<main><p>Every request needs approval first.</p>",
        "<p>Use mode=read|write for files.</p>",
        "<p>Rules deny network access.</p></main>",
    ]
)


@pytest.mark.parametrize(
    ("find", "places", "alternatives"),
    [
        # "|" between words separates alternatives, as in the regular expressions
        # of search Tools; an Agent sent 7 such finds that all missed (2026-10-21).
        ("approval|deny", ["needs approval", "deny network"], ["approval", "deny"]),
        ("absent|missing", [], ["absent", "missing"]),
        # Text the page holds with its "|" is found as written.
        ("read|write", ["mode=read|write"], []),
        # Between spaces, "|" separates table cells.
        ("approval | deny", [], []),
    ],
)
@pytest.mark.asyncio
async def test_find_reads_a_pipe_between_words_as_alternatives(
    tmp_path, monkeypatch, find, places, alternatives
):
    install_http_get(monkeypatch, lambda url: make_result(text=_RULES_PAGE, url=url))

    found = await fetch(tmp_path, {"url": "https://example.com/rules", "find": find})

    data = found["data"]
    if places:
        assert data["shown"].startswith(f"{len(places)} passage")
        assert all(place in data["content"] for place in places)
    else:
        assert "shown" not in data
    # The result names each alternative it searched for.
    named = data.get("shown", data["content"])
    assert all(f'"{alternative}"' in named for alternative in alternatives)
    assert (f'"{find}"' in named) == (not alternatives)


@pytest.mark.asyncio
async def test_find_in_plain_text_keeps_its_asterisks_and_pipes(tmp_path, monkeypatch):
    code = "def call(x, **kwargs):\n    return x | kwargs\n"
    install_http_get(
        monkeypatch,
        lambda url: make_result(text=code, url=url, headers={"content-type": "text/plain"}),
    )
    tool, context = web_fetch_registry(), make_context(tmp_path)
    ref = (await tool.dispatch(context, {"url": "https://example.com/a.py"}))["data"]["ref"]

    spaced = await tool.dispatch(context, {"ref": ref, "find": "return  x |\tkwargs"})
    unmarked = await tool.dispatch(context, {"ref": ref, "find": "x, kwargs"})

    assert spaced["data"]["shown"].startswith("1 passage")
    assert "shown" not in unmarked["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["main", "page"])
async def test_search_continuations_recover_all_matches_without_fetching(
    tmp_path, monkeypatch, scope
):
    body = "\n".join("Filler " * 100 + f"Needle{i:02d}" for i in range(30))
    network = AsyncMock(return_value=make_result(text=body))
    monkeypatch.setattr("core.tools._public_http._http_get", network)
    tool, context = web_fetch_registry(), make_context(tmp_path)
    response = await tool.dispatch(
        context, {"url": "https://example.com/", "find": "Needle", "scope": scope}
    )
    found = set()
    cursors = set()
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
        assert follow_up["ref"] not in cursors
        cursors.add(follow_up["ref"])
        response = await tool.dispatch(context, follow_up)
    assert found == {f"Needle{i:02d}" for i in range(30)}
    network.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("find", [None, "Needle"])
@pytest.mark.parametrize("extra", [0, 1])
async def test_a_reference_at_or_beyond_the_end_has_no_readable_content(
    tmp_path, monkeypatch, find, extra
):
    body = "Needle\n" * 10
    network = AsyncMock(return_value=make_result(text=body, headers={"content-type": "text/plain"}))
    monkeypatch.setattr("core.tools._public_http._http_get", network)
    tool, context = web_fetch_registry(), make_context(tmp_path)
    page = await tool.dispatch(context, {"url": "https://example.com/"})
    ref = page["data"]["ref"]
    args = {"ref": f"{ref}.m.{len(body) + extra}"}
    if find:
        args["find"] = find
    result = await tool.dispatch(context, args)
    assert not result["ok"]
    assert result["error"]["code"] == "invalid_arguments"
    # Its suggested restart is usable even when both views end at this position.
    restart = json.loads(re.search(r"(\{.*\})", result["error"]["message"])[1])
    read = await tool.dispatch(context, restart)
    assert read["ok"] and read["data"]["content"] == body
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
    # An unknown field fetches nothing again; the result says it had no effect.
    stale = await tool.dispatch(context, {"url": final_url, "ref": ref, "fresh": True})
    assert stale["data"]["note"].startswith('"fresh" had no effect')
    network.assert_awaited_once()


def mistyped(ref):
    """The ref with one character changed, as a copy slip makes."""
    return "tmp_" + ("b" if ref[4] == "a" else "a") + ref[5:]


@pytest.mark.parametrize(
    ("requested", "final", "redirect"),
    [
        ("https://www.example.org/table/element/26/iron", "https://table.example.org/", "other"),
        ("https://example.com/docs/page?id=26", "https://example.com/docs/page", "other"),
        ("https://example.com/manual/chapter-3", "https://example.com/tools/overview", "other"),
        ("https://example.com/docs/guide/install", "https://example.com/docs/guide", "other"),
        (
            "https://example.eu/en/getting-started/api-service_en",
            "https://example.eu/en/using-the-tool/api-service_en",
            "moved",
        ),
        ("https://shop.example/p/old-name/1283/", "https://shop.example/p/new-name/1283/", "moved"),
        ("http://example.com/Docs/A%20B/", "https://www.example.com/docs/a b", None),
        ("https://example.com/docs/", "https://example.com/docs/index.html", None),
        ("https://example.com/r/x/", "https://example.com/r/x/?rdt=5&utm_source=feed", None),
    ],
)
@pytest.mark.asyncio
async def test_a_redirect_to_another_page_is_named_with_the_requested_url(
    tmp_path, monkeypatch, requested, final, redirect
):
    install_http_get(monkeypatch, lambda url: make_result(text="Landing page", url=final))
    tool, context = web_fetch_registry(), make_context(tmp_path)

    fetched = await tool.dispatch(context, {"url": requested})
    saved = await tool.dispatch(context, {"url": requested, "ref": fetched["data"]["ref"]})

    note = {
        "other": f"This is not the requested page: {requested} redirected here.",
        "moved": f"The page moved here from {requested}.",
        None: None,
    }[redirect]
    for data in (fetched["data"], saved["data"]):
        assert data["url"] == final
        assert data.get("note") == note


@pytest.mark.asyncio
async def test_refs_are_refused_across_sessions_agents_expiry_and_traversal(tmp_path, monkeypatch):
    install_http_get(monkeypatch, lambda url: make_result(text="A real page", url=url))
    tool, context = web_fetch_registry(), make_context(tmp_path)
    first = await tool.dispatch(context, {"url": "https://example.com/"})
    ref = first["data"]["ref"]
    for changed in (replace(context, session_id="other"), replace(context, agent_id="other")):
        result = await tool.dispatch(changed, {"ref": ref, "find": "real"})
        assert result["error"]["code"] == "invalid_ref"
        # Another conversation's page is never offered or named.
        assert "example.com" not in result["error"]["message"]
    result = await tool.dispatch(context, {"ref": "../settings"})
    assert result["error"]["code"] == "invalid_ref"
    # A mistyped ref offers the conversation's page with the closest ref as a ready call.
    unknown = await tool.dispatch(context, {"ref": mistyped(ref) + ".m.5", "find": "real"})
    assert unknown["error"]["code"] == "invalid_ref"
    assert json.dumps({"ref": ref + ".m.5", "find": "real"}) in unknown["error"]["message"]
    now = pages.time.time()
    monkeypatch.setattr(pages.time, "time", lambda: now + 73 * 3600)
    expired = await tool.dispatch(context, {"ref": ref, "find": "real"})
    assert expired["error"]["code"] == "page_expired"
    # An expired page of this conversation names the call that fetches it again.
    assert (
        json.dumps({"url": "https://example.com/", "find": "real"}) in (expired["error"]["message"])
    )
    # Nor is an expired page offered for a mistyped ref.
    unknown = await tool.dispatch(context, {"ref": mistyped(ref)})
    assert unknown["error"]["code"] == "invalid_ref" and ref not in unknown["error"]["message"]


@pytest.mark.asyncio
async def test_an_unusable_ref_beside_url_fetches_the_url_and_says_why(tmp_path, monkeypatch):
    network = AsyncMock(
        side_effect=lambda session, url, max_bytes: make_result(text="Iron page", url=url)
    )
    monkeypatch.setattr("core.tools._public_http._http_get", network)
    tool, context = web_fetch_registry(), make_context(tmp_path)
    url = "https://example.com/iron"
    ref = (await tool.dispatch(context, {"url": url}))["data"]["ref"]

    for unusable in (mistyped(ref), "not-a-ref"):
        found = await tool.dispatch(context, {"ref": unusable, "url": url, "find": "iron"})
        assert found["ok"] and "Iron page" in found["data"]["content"]
        assert found["data"]["ref"] != ref and unusable in found["data"]["note"]
    assert network.await_count == 3
    # A usable ref still reads the saved page without fetching.
    saved = await tool.dispatch(context, {"ref": ref, "url": url})
    assert saved["data"]["ref"] == ref and "note" not in saved["data"]
    assert network.await_count == 3


@pytest.mark.asyncio
async def test_saved_limit_is_reported_without_claiming_completeness(tmp_path, monkeypatch):
    monkeypatch.setattr(pages, "MAX_SAVED_CHARS", 1500)
    install_http_get(monkeypatch, lambda url: make_result(text="Text " * 1000, url=url))
    tool, context = web_fetch_registry(), make_context(tmp_path)
    result = await tool.dispatch(context, {"url": "https://example.com/"})
    assert len(result["data"]["content"]) == 1500 and "more" not in result["data"]
    assert "Saved content is partial" in result["data"]["note"]
    # A search covers only the saved part and says so.
    ref = result["data"]["ref"]
    missing = await tool.dispatch(context, {"ref": ref, "find": "Absent"})
    assert missing["data"]["content"].startswith(
        'No word-for-word match for "Absent" in the saved part of the page (1500 characters)'
    )
    found = await tool.dispatch(context, {"ref": ref, "find": "Text"})
    assert found["data"]["shown"].endswith('matching "Text" in the saved part of the page')


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
