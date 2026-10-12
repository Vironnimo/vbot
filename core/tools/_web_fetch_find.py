"""Where web_fetch ``find`` text occurs in a saved page, as a reader sees the page.

Saved pages are Markdown renderings, so text a reader sees in one piece can be
split by syntax: ``**Iron** is a [chemical element](https://...)``, a table row
``| **Discovery date** | approx 3500BC |``, or a no-break space in ``968 kg``.
``find`` therefore matches the readable text of the page and maps each match back
to the Markdown passage the result shows. Presentation never counts: link and
image targets and brackets, emphasis and code markers, table separators and
rule rows, heading, quote and list markers, the Image placeholder syntax, kinds
and runs of whitespace, invisible characters, and typographic quotes, dashes and
ligatures (``core.tools.fuzzy_match.fold_for_search``). Every other character must
match, ignoring case.

A literal match in the Markdown also counts, so a query copied from a result
with its syntax finds that place exactly. Only when neither finds anything is
the query's own Markdown read as presentation too; a link address it names must
then still stand in the matched passage.
"""

from __future__ import annotations

import re
from functools import lru_cache

from core.tools.fuzzy_match import SearchText, fold_for_search

_FENCE = re.compile(r"[ \t]*(`{3,}|~{3,})")
_HORIZONTAL_RULE = re.compile(r"[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*")
_TABLE_RULE = re.compile(r"[ \t]*\|?(?:[ \t]*:?-+:?[ \t]*\|)+(?:[ \t]*:?-+:?[ \t]*)?")
_LINE_PREFIX = re.compile(r"[ \t]*(?:>[ \t]?)*(?:#{1,6}(?=[ \t]|$)[ \t]*|[-+*][ \t]+)?")
_INLINE = re.compile(
    r"(?P<escape>\\[*_|`])"
    r"|(?P<target>\]\((?P<address>(?:[^()\s]|\([^()\s]*\))+)(?:[ \t]+\"[^\"\n]*\")?\))"
    r"|(?P<autolink><(?:https?|mailto|tel):[^<>\s]+>)"
    r"|(?P<image>\[Image: )(?P<alt>[^\n]*?)(?P<omitted>; embedded image omitted\])"
    r"|(?P<spans>[ \t]\[(?:rowspan|colspan)=\d+(?:, (?:rowspan|colspan)=\d+)?\])"
    r"|(?P<marker>\*+|~~|`+)"
    r"|(?P<pipe>\|)"
)
_LINK_TEXT_LIMIT = 2_000
type _Replacement = tuple[int, int, str]


def _opening_bracket(text: str, close: int) -> int | None:
    """Return the ``[`` that the ``]`` at ``close`` ends, within its paragraph.

    Link text can span lines (an image and a label inside one link), but not a
    blank line, and a search stops after ``_LINK_TEXT_LIMIT`` characters.
    """
    depth = 0
    newline_seen = blank = False
    index = close - 1
    floor = max(0, close - _LINK_TEXT_LIMIT)
    while index >= floor:
        character = text[index]
        if character == "\n":
            if newline_seen and blank:
                return None
            newline_seen = blank = True
        elif not character.isspace():
            blank = False
            escaped = index > 0 and text[index - 1] == "\\"
            if character == "]" and not escaped:
                depth += 1
            elif character == "[" and not escaped:
                if depth == 0:
                    return index
                depth -= 1
        index -= 1
    return None


def _flanking(text: str, start: int, end: int, line_start: int, line_end: int) -> bool:
    """Whether a marker run opens or closes a span, as ``**`` around a word does."""
    before = text[start - 1] if start > line_start else " "
    after = text[end] if end < line_end else " "
    opens = not after.isspace() and (before.isspace() or not before.isalnum())
    closes = not before.isspace() and (after.isspace() or not after.isalnum())
    return opens or closes


def _line_presentation(
    text: str, start: int, end: int, replacements: list[_Replacement], addresses: list[str]
) -> None:
    prefix = _LINE_PREFIX.match(text, start, end)
    if prefix and prefix.end() > start:
        replacements.append((start, prefix.end(), " "))
    for found in _INLINE.finditer(text, prefix.end() if prefix else start, end):
        if found["escape"]:
            replacements.append((found.start(), found.start() + 1, ""))
        elif found["target"]:
            opening = _opening_bracket(text, found.start())
            if opening is None:
                continue
            # An image's ``![`` goes with its brackets; its alt text stays readable.
            image = opening > 0 and text[opening - 1] == "!"
            replacements.append((opening - image, opening + 1, ""))
            replacements.append((found.start(), found.end(), ""))
            addresses.append(found["address"])
        elif found["autolink"]:
            replacements.append((found.start(), found.start() + 1, ""))
            replacements.append((found.end() - 1, found.end(), ""))
        elif found["image"]:
            replacements.append((found.start("image"), found.end("image"), ""))
            replacements.append((found.start("omitted"), found.end("omitted"), " "))
        elif found["spans"]:
            replacements.append((found.start(), found.end(), " "))
        elif found["marker"]:
            if _flanking(text, found.start(), found.end(), start, end):
                replacements.append((found.start(), found.end(), ""))
        elif found["pipe"]:
            before = text[found.start() - 1] if found.start() > start else " "
            after = text[found.end()] if found.end() < end else " "
            if before.isspace() and after.isspace():
                replacements.append((found.start(), found.end(), " "))


def _presentation(text: str) -> tuple[list[_Replacement], list[str]]:
    """Return the Markdown syntax of ``text`` to read differently, and its link addresses."""
    replacements: list[_Replacement] = []
    addresses: list[str] = []
    fence: str | None = None
    start = 0
    while start <= len(text):
        end = text.find("\n", start)
        end = len(text) if end == -1 else end
        opened = _FENCE.match(text, start, end)
        if fence is not None:
            if opened and opened[1][0] == fence[0] and len(opened[1]) >= len(fence):
                replacements.append((start, end, " "))
                fence = None
        elif opened:
            fence = opened[1]
            replacements.append((start, end, " "))
        elif end > start and (
            _HORIZONTAL_RULE.fullmatch(text, start, end) or _TABLE_RULE.fullmatch(text, start, end)
        ):
            replacements.append((start, end, " "))
        else:
            _line_presentation(text, start, end, replacements, addresses)
        start = end + 1
    replacements.sort()
    kept: list[_Replacement] = []
    for replacement in replacements:
        if replacement[0] < replacement[1] and (not kept or replacement[0] >= kept[-1][1]):
            kept.append(replacement)
    return kept, addresses


@lru_cache(maxsize=4)
def _readable(text: str, markdown: bool) -> SearchText:
    """The page as a reader sees it; repeated searches of one page share it."""
    return fold_for_search(text, _presentation(text)[0] if markdown else ())


def _search(readable: SearchText, query: str) -> list[tuple[int, int]]:
    """Return the matches of ``query`` in the readable text, as readable positions."""
    if not query:
        return []
    return [
        (found.start(), found.end())
        for found in re.finditer(re.escape(query), readable.text, re.IGNORECASE)
    ]


def find_spans(text: str, find: str, *, markdown: bool, offset: int = 0) -> list[tuple[int, int]]:
    """Return the ranges of ``text`` from ``offset`` on that ``find`` matches, in order.

    ``markdown`` says the text is a Markdown rendering whose syntax is presentation.
    Overlapping matches merge into one range.
    """
    readable = _readable(text, markdown)
    spans = [
        (offset + found.start(), offset + found.end())
        for found in re.finditer(re.escape(find), text[offset:], re.IGNORECASE)
    ]
    plain = fold_for_search(find).text.strip()
    spans += [readable.original(*found) for found in _search(readable, plain)]
    spans = [span for span in spans if span[0] >= offset]
    if not spans and markdown:
        replacements, addresses = _presentation(find)
        query = fold_for_search(find, replacements).text.strip()
        if query != plain:
            for found in _search(readable, query):
                start, end = readable.original(*found)
                # The syntax after the match, such as a link's target, ends where
                # the next readable character starts.
                passage = text[start : readable.resume(found[1])]
                if start >= offset and all(address in passage for address in addresses):
                    spans.append((start, end))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
