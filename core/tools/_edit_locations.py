"""Shared edit locations, poststate checks, and line geometry."""

from __future__ import annotations

import re
from dataclasses import replace
from os.path import commonprefix

from core.tools._patch_syntax import _Hunk
from core.tools.arguments import (
    TEXT_LINE_BREAK,
    split_text_lines,
    strip_line_number_gutters,
)
from core.tools.fuzzy_match import (
    AmbiguousFuzzyMatch,
    FuzzyReplacement,
    dominant_line_ending,
    replace_fuzzy,
)
from core.tools.tools import JsonObject

type _Found = FuzzyReplacement | AmbiguousFuzzyMatch | None


class DeviationNote(str):
    """A note that the change landed on text other than the call's copy.

    It names lines that read differently, respelled words or the occurrence a
    repeated text chose. A result that carries one shows the changed region as
    it now reads, since the written text is not what the call sent.
    """


class AlreadyApplied(str):
    """Why a text replacement changed nothing: its new text already stands in the file."""


def _deviations(notes: list[str]) -> list[str]:
    return [DeviationNote(note) for note in notes]


_GUTTER_WARNING = "Removed read-output line-number prefixes before applying the change."
_BREAK = TEXT_LINE_BREAK
_GUTTER = re.compile(r"^\s*[1-9][0-9]*(?::[1-9][0-9]*)?\|")


def line_ending(content: str) -> str:
    """Return the line ending most lines of ``content`` use, else LF."""
    return dominant_line_ending(content) or "\n"


def file_excerpt(lines: list[str], first: int, last: int) -> JsonObject:
    """Return file lines ``first``-``last`` (1-based) as a report candidate.

    Each line is cut at 240 characters; a cut line names where ``read`` continues it.
    """
    shown = lines[first - 1 : last]
    return {
        "line": first,
        "text": "\n".join(line[:240] for line in shown),
        "truncated": any(len(line) > 240 for line in shown),
        "continuations": [
            {"offset": f"{number}:241", "limit": 1}
            for number, line in enumerate(shown, first)
            if len(line) > 240
        ],
    }


# --- Matching helpers ------------------------------------------------------------------


def _poststate(
    content: str, new: str, *, whole_lines: bool = False, eof: bool = False, first: bool = False
) -> _Found:
    """Recognize written text without treating requested whitespace as a copy error."""
    found = replace_fuzzy(
        content,
        new,
        new,
        replace_all=False,
        whole_lines=whole_lines,
        at_eof=eof,
        first=first,
        typographic=True,
        whitespace=False,
    )
    # A substring starting inside indentation does not prove the requested
    # indentation: dedenting a first line must not count as already applied.
    if isinstance(found, FuzzyReplacement):
        start = found.before_spans[0][0]
        lead = content[
            max(content.rfind("\n", 0, start), content.rfind("\r", 0, start)) + 1 : start
        ]
        if lead and not lead.strip():
            return None
    return found


def _match(
    content: str,
    old: str,
    new: str,
    *,
    eof: bool = False,
    first: bool = False,
) -> _Found:
    """Match ``old`` as whole lines precisely; an empty ``old`` is one blank line."""
    if old == "":
        positions = []
        offset = 0
        for line, (text, ending) in enumerate(_line_parts(content), 1):
            if text == "" and (not eof or offset + len(ending) == len(content)):
                positions.append((offset, line))
            offset += len(text) + len(ending)
        if not positions:
            return None
        if len(positions) > 1 and not first:
            return AmbiguousFuzzyMatch(
                len(positions),
                [line for _, line in positions],
                tuple((start, start) for start, _ in positions),
            )
        start, line = positions[0]
        return FuzzyReplacement(
            content[:start] + new + content[start:],
            line,
            line,
            1,
            "exact",
            ((start, start),),
            ((start, start + len(new)),),
        )
    return replace_fuzzy(
        content,
        old,
        new,
        replace_all=False,
        whole_lines=True,
        at_eof=eof,
        typographic=True,
        first=first,
    )


def _match_hunk(window: str, hunk: _Hunk, *, first: bool = False) -> _Found:
    return _match(window, _hunk_text(hunk, " -"), _hunk_text(hunk, " +"), eof=hunk.eof, first=first)


def _normalize_gutters(hunk: _Hunk) -> _Hunk | None:
    """Return the hunk with read gutters stripped, or ``None`` when they do not fit.

    Locators may mix raw lines with copied gutters. Only a unique whole-line
    match against the current file authorizes this recovery.
    """
    old = "\n".join(text for prefix, text in hunk.lines if prefix in " -")
    if strip_line_number_gutters(old) is None:
        return None
    lines = []
    for prefix, text in hunk.lines:
        stripped = strip_line_number_gutters(text)
        if stripped is not None and re.match(r"^\s*\d+:\d+\|", text):
            return None
        lines.append((prefix, text if stripped is None else stripped))
    return replace(hunk, lines=lines)


def _located_by_gutters(hunk: _Hunk) -> bool:
    """Whether unchanged or removed lines carry read gutters; added lines never locate."""
    return any(_GUTTER.match(text) for prefix, text in hunk.lines if prefix in " -")


def _inline_context_identifies(content: str, old: str, new: str) -> bool:
    """Identify a single-line replacement by two retained, unique text anchors."""
    if "\n" in old or "\n" in new:
        return False
    prefix = commonprefix((old, new))
    suffix = commonprefix((old[len(prefix) :][::-1], new[len(prefix) :][::-1]))[::-1]
    # Syntax/indentation alone cannot identify a target. Both sides must retain
    # substantive text, and together they must select exactly one current line.
    if any(
        len(anchor.strip()) < 4 or not re.search(r"\w{3}", anchor) for anchor in (prefix, suffix)
    ):
        return False
    candidates = [
        line
        for line in split_text_lines(content)
        if line.startswith(prefix) and line.endswith(suffix)
    ]
    return candidates == [new]


def _beside_additions(lines: list[tuple[str, str]], index: int) -> bool:
    """Tell whether the unchanged line at ``index`` can be an added line missing its +.

    It can when it is between + lines, or when a + line is right next to it.
    """
    return _between_additions(lines, index) or any(
        0 <= i < len(lines) and lines[i][0] == "+" for i in (index - 1, index + 1)
    )


def _between_additions(lines: list[tuple[str, str]], index: int) -> bool:
    """Whether unchanged lines alone separate the line at ``index`` from + lines on both sides."""
    before = index - 1
    while before >= 0 and lines[before][0] == " ":
        before -= 1
    after = index + 1
    while after < len(lines) and lines[after][0] == " ":
        after += 1
    return before >= 0 and after < len(lines) and lines[before][0] == lines[after][0] == "+"


def _change_end_line(before: str, after: str) -> int:
    """Return where the line holding the end of the change from ``before`` starts."""
    prefix = len(commonprefix((before, after)))
    limit = min(len(before), len(after)) - prefix
    suffix = len(commonprefix((before[::-1][:limit], after[::-1][:limit])))
    end = max(prefix, len(after) - suffix - 1)
    return max(after.rfind("\n", 0, end), after.rfind("\r", 0, end)) + 1


def _line_parts(content: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    start = 0
    for match in _BREAK.finditer(content):
        parts.append((content[start : match.start()], match.group()))
        start = match.end()
    if start < len(content):
        parts.append((content[start:], ""))
    return parts


def _hunk_text(hunk: _Hunk, prefixes: str) -> str:
    return "\n".join(text for prefix, text in hunk.lines if prefix in prefixes)


def _hint_text(hint: str) -> str:
    """Show a context hint in one line; a multi-line context block by its first line."""
    first, _, rest = hint.partition("\n")
    return first[:120] + (" ..." if rest or len(first) > 120 else "")


def _loose(text: str) -> str:
    return " ".join(text.split())
