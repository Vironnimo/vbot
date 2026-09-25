"""web_fetch accepts clear calls in other shapes and refuses unclear ones before fetching."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from core.tools.contracts import ToolContractError
from core.tools.tools import ToolRegistry
from core.tools.web_fetch import register_web_fetch_tool
from tests.core.tools.web_fetch_helpers import install_http_get, make_context, make_result
from tests.core.tools.web_fetch_helpers import stub_dns_resolution as stub_dns_resolution
from tests.core.tools.web_fetch_helpers import stub_http_session as stub_http_session

PAGE = "https://example.com/guide"


@pytest.fixture
def fetched(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve a short text page per URL and record every requested address."""
    requested: list[str] = []

    def respond(url: str):
        requested.append(url)
        if url.endswith("/missing"):
            return make_result(status_code=404, url=url)
        if url.endswith("/long"):
            return make_result(text="Long text. " * 3000, url=url)
        if url.endswith("/html"):
            return make_result(
                text='<html><body><main><p>Para <a href="/x">link</a></p></main></body></html>',
                headers={"content-type": "text/html"},
                url=url,
            )
        return make_result(text=f"Content of {url}", url=url)

    install_http_get(monkeypatch, respond)
    return requested


async def dispatch(tmp_path: Path, arguments: Any) -> dict[str, Any]:
    registry = ToolRegistry()
    register_web_fetch_tool(registry, attachment_store=None)
    return await registry.dispatch(make_context(tmp_path), arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "url"),
    [
        ({"link": PAGE}, PAGE),
        ({"URI": PAGE}, PAGE),
        ({"website": PAGE}, PAGE),
        ({"url": "example.com/guide"}, PAGE),
        ({"url": "www.example.com/guide?x=1"}, "https://www.example.com/guide?x=1"),
        ({"url": "//example.com/guide"}, PAGE),
        ({"url": f"  <{PAGE}>  "}, PAGE),
        ({"url": f'"{PAGE}"'}, PAGE),
        ({"url": f"[the guide]({PAGE})"}, PAGE),
        ({"url": "https:/example.com/guide"}, PAGE),
        ({"url": "http://example.com/guide"}, "http://example.com/guide"),
        ({"url": [PAGE]}, PAGE),
        ({"urls": [PAGE]}, PAGE),
        ({"urls": PAGE}, PAGE),
        ({"ref": PAGE}, PAGE),
    ],
)
async def test_clear_addresses_are_fetched_as_meant(
    tmp_path: Path, fetched: list[str], arguments: dict[str, Any], url: str
) -> None:
    result = await dispatch(tmp_path, arguments)

    assert result["ok"], result
    assert fetched == [url]
    assert result["data"]["content"] == f"Content of {url}"
    assert "note" not in result["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"url": "the guide page"}, "This is not a web address."),
        ({"url": "ftp://example.com/guide"}, "not ftp: URLs"),
    ],
)
async def test_text_that_is_no_web_address_is_refused_without_fetching(
    tmp_path: Path, fetched: list[str], arguments: dict[str, Any], message: str
) -> None:
    result = await dispatch(tmp_path, arguments)

    assert result["error"]["code"] == "invalid_url"
    assert message in result["error"]["message"]
    assert fetched == []


@pytest.mark.asyncio
async def test_several_urls_are_fetched_together_with_a_section_each(
    tmp_path: Path, fetched: list[str]
) -> None:
    second = "https://example.com/missing"
    result = await dispatch(tmp_path, {"urls": [PAGE, f"<{second}>", PAGE]})

    assert result["ok"]
    assert sorted(fetched) == sorted([PAGE, second])
    content = result["data"]["content"]
    first_section, second_section = content.split("\n\n[2/2] ")
    assert first_section.startswith(f"[1/2] {PAGE}\nref: tmp_")
    assert first_section.endswith(f"\n\nContent of {PAGE}")
    assert second_section == (
        f"{second}\nError (page_not_found): HTTP 404: there is no page at {second}. Check the "
        "address; the page may have moved or been removed."
    )


@pytest.mark.asyncio
async def test_several_urls_share_one_page_budget(tmp_path: Path, fetched: list[str]) -> None:
    urls = [f"https://example.com/{name}/long" for name in "abc"]
    result = await dispatch(tmp_path, {"urls": urls})

    sections = re.split(r"\n\n(?=\[\d/3\] )", result["data"]["content"])
    assert len(sections) == 3 and len(fetched) == 3
    for section in sections:
        assert "\nshown: characters 1-8000 of 33000\n" in section
        assert 'Continue with {"ref": "tmp_' in section


@pytest.mark.asyncio
async def test_too_many_urls_are_refused_before_fetching(
    tmp_path: Path, fetched: list[str]
) -> None:
    urls = [f"https://example.com/{index}" for index in range(6)]

    with pytest.raises(ToolContractError) as error:
        await dispatch(tmp_path, {"urls": urls})

    assert str(error.value) == (
        "web_fetch reads at most 5 URLs per call; received 6. Split them across calls of "
        "up to 5 URLs each."
    )
    assert fetched == []


@pytest.mark.asyncio
async def test_all_urls_failing_is_a_failure_naming_each(
    tmp_path: Path, fetched: list[str]
) -> None:
    urls = ["https://example.com/a/missing", "https://example.com/b/missing"]
    result = await dispatch(tmp_path, {"urls": urls})

    assert result["error"]["code"] == "page_not_found"
    assert result["error"]["message"].splitlines()[0] == "None of the pages could be fetched."
    assert all(url in result["error"]["message"] for url in urls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        # Claude Code WebFetch
        {"url": PAGE, "prompt": "Which versions are supported?"},
        {"url": PAGE, "question": "Which versions are supported?"},
        # Gemini CLI web_fetch names the page inside its prompt.
        {"prompt": f"Summarize {PAGE}."},
    ],
)
async def test_prompts_are_accepted_and_the_page_text_is_returned(
    tmp_path: Path, fetched: list[str], arguments: dict[str, Any]
) -> None:
    result = await dispatch(tmp_path, arguments)

    assert fetched == [PAGE]
    assert result["data"]["content"] == f"Content of {PAGE}"
    assert result["data"]["note"] == (
        "web_fetch returns page text and does not answer prompts; read the content for the answer."
    )


@pytest.mark.asyncio
async def test_urls_named_in_a_prompt_are_all_fetched(tmp_path: Path, fetched: list[str]) -> None:
    other = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    result = await dispatch(
        tmp_path, {"prompt": f"Compare {PAGE}, and [Python]({other}), then summarize."}
    )

    assert sorted(fetched) == sorted([PAGE, other])
    assert f"[2/2] {other}" in result["data"]["content"]


@pytest.mark.asyncio
async def test_a_prompt_without_an_address_asks_for_url(tmp_path: Path, fetched: list[str]) -> None:
    result = await dispatch(tmp_path, {"prompt": "Find the pricing page"})

    assert result["error"]["code"] == "invalid_arguments"
    assert result["error"]["message"] == (
        'Pass url to fetch a page, for example {"url": "https://example.com/page"}, or ref '
        "to read a page fetched earlier."
    )
    assert fetched == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "shown"),
    [
        ({"url": "https://example.com/long", "max_length": 3000}, "characters 1-3000 of 33000"),
        ({"url": "https://example.com/long", "limit": "5000"}, "characters 1-5000 of 33000"),
        ({"url": "https://example.com/long", "maxChars": 50}, "characters 1-200 of 33000"),
    ],
)
async def test_length_limits_in_other_spellings_size_the_part(
    tmp_path: Path, fetched: list[str], arguments: dict[str, Any], shown: str
) -> None:
    result = await dispatch(tmp_path, arguments)

    assert result["data"]["shown"] == shown
    assert "note" not in result["data"]


@pytest.mark.asyncio
async def test_a_limit_above_one_part_says_how_to_read_on(
    tmp_path: Path, fetched: list[str]
) -> None:
    result = await dispatch(tmp_path, {"url": "https://example.com/long", "max_chars": 100000})

    assert result["data"]["shown"].startswith("characters 1-")
    assert result["data"]["note"] == (
        "Each call shows at most about 4,000 tokens of page text; continue with the call in more."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "present", "absent"),
    [
        ({"format": "html"}, "<main>", "Para [link]"),
        ({"format": "text"}, "Para link", "https://example.com/x"),
        ({"extractMode": "markdown"}, "[link](https://example.com/x)", "<main>"),
        ({"output_format": "md", "timeout": 30}, "[link](https://example.com/x)", "<main>"),
    ],
)
async def test_format_names_of_other_harnesses_select_the_output(
    tmp_path: Path,
    fetched: list[str],
    arguments: dict[str, Any],
    present: str,
    absent: str,
) -> None:
    result = await dispatch(tmp_path, {"url": "https://example.com/html", **arguments})

    assert present in result["data"]["content"]
    assert absent not in result["data"]["content"]


@pytest.mark.asyncio
async def test_copied_continuations_and_ref_labels_read_the_saved_page(
    tmp_path: Path, fetched: list[str]
) -> None:
    registry = ToolRegistry()
    register_web_fetch_tool(registry, attachment_store=None)
    context = make_context(tmp_path)
    first = await registry.dispatch(context, {"url": "https://example.com/long"})
    more = first["data"]["more"]
    found = re.search(r'"ref": "(tmp_[^"]+)"', more)
    assert found is not None
    next_ref = found[1]

    for arguments in (
        {"next": {"ref": next_ref}},
        {"more": more},
        {"ref": f"[ref {next_ref}]"},
        {"reference": next_ref},
    ):
        result = await registry.dispatch(context, arguments)
        assert result["ok"], arguments
        assert result["data"]["shown"].startswith(f"characters {int(next_ref.split('.')[-1]) + 1}-")
    assert fetched == ["https://example.com/long"]

    with pytest.raises(ToolContractError, match="Conflicting values for ref"):
        await registry.dispatch(context, {"ref": first["data"]["ref"], "next": {"ref": next_ref}})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"urls": [PAGE, "https://example.com/other"], "ref": "tmp_0123456789ab"},
            "ref reads one saved page and urls fetch new pages; pass one of them.",
        ),
        (
            {"ref": "tmp_0123456789ab", "format": "text"},
            "output applies only when fetching a url; a saved page keeps the output it was "
            "fetched with. Omit output when using ref.",
        ),
        (
            {"url": PAGE, "find": "x" * 201},
            "find is limited to 200 characters; received 201. Search for a shorter, "
            "distinctive phrase.",
        ),
    ],
)
async def test_conflicting_or_oversized_requests_are_refused_before_fetching(
    tmp_path: Path, fetched: list[str], arguments: dict[str, Any], message: str
) -> None:
    result = await dispatch(tmp_path, arguments)

    assert result["error"] == {
        "code": "invalid_arguments",
        "message": message,
        "retryable": False,
    }
    assert fetched == []


def test_row_shows_the_address_whatever_the_argument_name() -> None:
    registry = ToolRegistry()
    register_web_fetch_tool(registry, attachment_store=None)

    shown = {
        str(arguments): [
            part["value"] for part in registry.display_for_call("web_fetch", arguments)["primary"]
        ]
        for arguments in (
            {"link": "example.com/a"},
            {"urls": ["https://a.example/", "https://b.example/"]},
            {"ref": "tmp_0123456789ab.m.100", "find": "price"},
        )
    }

    assert list(shown.values()) == [
        ["https://example.com/a"],
        ["https://a.example/, https://b.example/"],
        ["tmp_0123456789ab.m.100", "price"],
    ]
