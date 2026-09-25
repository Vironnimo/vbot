"""Interpret web_fetch arguments, including the shapes other harnesses use.

Everything here runs before validation: field aliases, URL cleanup, URLs
named inside a prompt, ``next`` objects copied from a result, and the legacy
output switches. A value either has one clear meaning or the call is rejected
with the reason; nothing is guessed.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

from core.tools._argument_repair import normalize_call_arguments
from core.tools._web_fetch_pages import REF_IN_TEXT
from core.tools.contracts import ToolContract, ToolContractError

MAX_URLS = 5
OUTPUTS = ("markdown", "text", "raw")

_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\((\S+?)\)")
_HAS_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*://", re.I)
_BROKEN_WEB_SCHEME = re.compile(r"(https?):/*(?=[^/])", re.I)
_HOST_START = re.compile(
    r"(?:(?:[a-z0-9-]+\.)+[a-z]{2,63}|localhost|\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?(?:[/?#]|$)",
    re.I,
)
_WEB_URL = re.compile(r"https?://\S+", re.I)
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'`]+", re.I)
_WRAPPERS = {("<", ">"), ('"', '"'), ("'", "'"), ("`", "`")}
_OUTPUT_VALUES = {
    **dict.fromkeys(("markdown", "md"), "markdown"),
    **dict.fromkeys(("text", "txt", "plain", "plaintext"), "text"),
    **dict.fromkeys(("raw", "html", "rawhtml", "source"), "raw"),
}
_NEXT_KEYS = frozenset({"next", "nextpage", "nextcall", "more"})


def _spelling(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value.casefold())


class _SpellingAliases(Mapping[str, str]):
    """Field aliases that match regardless of case, "_", "-", or spaces."""

    def __init__(self, fields: dict[str, tuple[str, ...]]) -> None:
        self._aliases = {
            _spelling(alias): field for field, names in fields.items() for alias in names
        }

    def __getitem__(self, key: str) -> str:
        return self._aliases[_spelling(key)]

    def __iter__(self) -> Iterator[str]:
        return iter(self._aliases)

    def __len__(self) -> int:
        return len(self._aliases)


# Names other fetch Tools and Agents use for the same fields.
FIELD_ALIASES = _SpellingAliases(
    {
        "url": (
            "uri",
            "link",
            "href",
            "address",
            "website",
            "webpage",
            "page_url",
            "target_url",
            "source_url",
            "site_url",
            "fetch_url",
        ),
        "urls": ("links", "uris", "hrefs", "url_list", "addresses"),
        "ref": ("reference", "page_ref", "ref_id", "cursor", "continuation", "saved_page"),
        "find": (
            "search",
            "search_text",
            "find_text",
            "search_for",
            "look_for",
            "keyword",
            "text_to_find",
        ),
        "prompt": ("question", "query", "instruction", "instructions", "task", "objective"),
        "max_chars": (
            "max_length",
            "length",
            "limit",
            "max_characters",
            "char_limit",
            "characters",
            "max_content_length",
            "context_max_characters",
        ),
        "output": (
            "format",
            "output_format",
            "extract_mode",
            "return_format",
            "content_format",
        ),
        "offset": ("start", "start_index", "start_char", "position"),
    }
)


def clean_url(value: Any) -> Any:
    """Return the address a copied URL clearly means, or the value unchanged."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    link = _MARKDOWN_LINK.fullmatch(text)
    if link is not None:
        text = link[1]
    while len(text) >= 2 and (text[0], text[-1]) in _WRAPPERS:
        text = text[1:-1].strip()
    if text.startswith("//"):
        return "https:" + text
    if _HAS_SCHEME.match(text):
        return text
    broken = _BROKEN_WEB_SCHEME.match(text)
    if broken is not None:
        return f"{broken[1].lower()}://{text[broken.end() :]}"
    if _HOST_START.match(text):
        return "https://" + text
    return text


def urls_in_text(text: str) -> list[str]:
    """Return the http(s) URLs a prose prompt names, in order."""
    found = []
    for match in _URL_IN_TEXT.finditer(text):
        url = match[0]
        while url and url[-1] in ".,;:!?":
            url = url[:-1]
        for opening, closing in (("(", ")"), ("[", "]")):
            while url.endswith(closing) and url.count(opening) < url.count(closing):
                url = url[:-1]
        if url:
            found.append(url)
    return list(dict.fromkeys(found))


def _clean_urls(value: Any) -> Any:
    if isinstance(value, list):
        return [clean_url(item) for item in value]
    return clean_url(value)


def _clean_ref(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    while len(text) >= 2 and (text[0], text[-1]) in _WRAPPERS:
        text = text[1:-1].strip()
    if _WEB_URL.fullmatch(text):
        return text
    found = REF_IN_TEXT.search(text)
    return found[0] if found is not None else text


def _output_value(value: Any) -> Any:
    return _OUTPUT_VALUES.get(_spelling(value), value) if isinstance(value, str) else value


def _lift_next(arguments: dict[str, Any]) -> dict[str, Any]:
    """Accept the continuation object of a result as the call's own fields."""
    keys = [key for key in arguments if _spelling(key) in _NEXT_KEYS]
    lifted = {key: value for key, value in arguments.items() if key not in keys}
    for key in keys:
        value = arguments[key]
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except ValueError:
                decoded = None
            if isinstance(decoded, dict):
                value = decoded
            else:
                found = REF_IN_TEXT.search(value)
                value = {"ref": found[0]} if found is not None else None
        if not isinstance(value, dict):
            raise ToolContractError(
                f'{key} must be the object a web_fetch result offers, such as {{"ref": '
                '"tmp_abcdefghjkmn.m.12000"}.'
            )
        for inner_key, inner in value.items():
            if inner_key in lifted and lifted[inner_key] != inner:
                raise ToolContractError(
                    f"Conflicting values for {inner_key}; provide one intended value."
                )
            lifted[inner_key] = inner
    return lifted


def _settle_targets(arguments: dict[str, Any]) -> None:
    """Resolve url, urls, a URL passed as ref, or URLs named in a prompt."""
    urls: list[Any] = []
    for key in ("url", "urls"):
        if key in arguments:
            value = arguments.pop(key)
            urls.extend(value if isinstance(value, list) else [value])
    ref = arguments.get("ref")
    if isinstance(ref, str) and _WEB_URL.fullmatch(ref):
        del arguments["ref"]
        urls.append(ref)
    if any(not isinstance(url, str) for url in urls):
        raise ToolContractError("url must be a URL string, or urls a list of URL strings.")
    urls = list(dict.fromkeys(url for url in urls if url.strip()))
    if not urls and "ref" not in arguments and isinstance(arguments.get("prompt"), str):
        urls = urls_in_text(arguments["prompt"])
    if len(urls) > MAX_URLS:
        raise ToolContractError(
            f"web_fetch reads at most {MAX_URLS} URLs per call; received {len(urls)}. "
            f"Split them across calls of up to {MAX_URLS} URLs each."
        )
    if len(urls) == 1:
        arguments["url"] = urls[0]
    elif urls:
        arguments["urls"] = urls


def _settle_output(arguments: dict[str, Any]) -> None:
    """Intersect the legacy raw/include_links switches with output."""
    raw_present = "raw" in arguments
    links_present = "include_links" in arguments
    raw = arguments.pop("raw", None)
    links = arguments.pop("include_links", None)
    if raw_present and not isinstance(raw, bool):
        raise ToolContractError(
            "raw must indicate true or false; use output to select markdown, text, or raw."
        )
    if links_present and not isinstance(links, bool):
        raise ToolContractError(
            "include_links must indicate true or false; use output markdown or text."
        )
    output = arguments.get("output")
    if "output" in arguments and output not in OUTPUTS:
        raise ToolContractError("output must be markdown, text, or raw.")
    # Raw HTML preserves links; it cannot also promise link-target removal.
    modes = set(OUTPUTS)
    if output is not None:
        modes.intersection_update({output})
    if raw_present:
        modes.intersection_update({"raw"} if raw else {"markdown", "text"})
    if links_present:
        modes.intersection_update({"markdown", "raw"} if links else {"text"})
    if not modes:
        raise ToolContractError(
            "raw, include_links, and output conflict; provide one consistent output choice."
        )
    if raw_present or links_present or output is not None:
        arguments["output"] = next(mode for mode in OUTPUTS if mode in modes)


def normalize_web_fetch_arguments(contract: ToolContract, arguments: Any) -> Any:
    """Repair one web_fetch call into its canonical fields."""
    if isinstance(arguments, str):
        # The shared repair reports an unreadable argument object.
        with contextlib.suppress(ValueError):
            arguments = json.loads(arguments)
    if isinstance(arguments, dict):
        arguments = _lift_next(arguments)
    repaired = normalize_call_arguments(
        contract,
        arguments,
        enum_fields=("scope",),
        field_aliases=FIELD_ALIASES,
        empty_as_omitted=("ref", "find", "prompt", "output", "scope"),
        field_normalizers={
            "url": _clean_urls,
            "urls": _clean_urls,
            "ref": _clean_ref,
            "output": _output_value,
        },
    )
    if not isinstance(repaired, dict):
        return repaired
    _settle_targets(repaired)
    _settle_output(repaired)
    return repaired
