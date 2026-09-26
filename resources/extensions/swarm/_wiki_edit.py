"""Locate and apply one Wiki ``old_text``/``new_text`` edit.

The matching follows ``apply_patch``: precise matching first, up to newline,
Unicode, typography and whitespace differences; then the same edit without blank
boundary lines both texts share; then evidence that the page already holds the
change. Only after those, ``copy_match`` may apply an ``old_text`` copied with
errors: text the edit keeps stays as the page has it, so a peer's wording is never
overwritten, and the notes name each changed line that differed. Ambiguity is
terminal at the step that finds it. A miss returns bounded line hints for the
error, never a guess.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from core.tools.copy_match import copy_warnings, replace_copied
from core.tools.fuzzy_match import (
    AmbiguousFuzzyMatch,
    FuzzyReplacement,
    find_closest_candidates,
    replace_fuzzy,
)

from ._wiki_emphasis import matching_lines, preserve_kept_emphasis
from .wiki_text import WIKI_EMPHASIS_CLIPPED, WIKI_EMPHASIS_COPY

_LINE_BREAK = re.compile(r"\r\n|\n|\r")
_SNIPPET_LINE_CHARS = 240
_MAX_OCCURRENCES_SHOWN = 3
# A one-line hint needs a page line sharing at least half of old_text in one block.
_HINT_MIN_SHARE = 0.5
_HINT_SCAN_LINES = 5000
_HINT_LINE_CHARS = 4000


@dataclass(frozen=True)
class TextEdit:
    """The page content after the edit; ``applied`` is False when it already held it.

    ``notes`` tell the caller where its ``old_text`` differed from the page.
    """

    content: str
    line: int
    applied: bool
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Passage:
    line: int
    text: str
    truncated: bool


@dataclass(frozen=True)
class EditMiss:
    """No single target: ``occurrences`` > 1 when ambiguous, 0 when not found.

    ``lines`` names every distinct line an ambiguous ``old_text`` starts on;
    ``passages`` shows a bounded few of them, or the closest text on a miss.
    ``similar`` marks an ``old_text`` that matched nowhere exactly but resembles
    several passages.
    """

    occurrences: int
    passages: tuple[Passage, ...]
    lines: tuple[int, ...] = ()
    similar: bool = False


def apply_text_edit(content: str, old: str, new: str) -> TextEdit | EditMiss:
    """Replace the one passage ``old`` identifies, or explain why there is none."""

    attempts = [(old, new)]
    trimmed = _without_shared_blank_boundaries(old, new)
    if trimmed != (old, new) and trimmed[0].strip():
        attempts.append(trimmed)
    for locator, replacement in attempts:
        found = _replace(content, locator, replacement)
        if isinstance(found, AmbiguousFuzzyMatch):
            return _ambiguous(content, found)
        if found is not None:
            return TextEdit(found.new_content, found.first_changed_line, True)
    held = _already_applied(content, old, new)
    if held is not None:
        return TextEdit(content, held, False)
    emphasis = _emphasis_copy(content, old, new)
    if emphasis is not None:
        return emphasis
    if not new.strip() or _replace(content, new, new) is None:
        copied = replace_copied(content, old, new)
        if isinstance(copied, AmbiguousFuzzyMatch):
            return _ambiguous(content, copied, similar=True)
        if copied is not None:
            notes = tuple(copy_warnings(copied))
            return TextEdit(copied.new_content, copied.first_changed_line, True, notes)
    passages = tuple(
        Passage(candidate.line_number, candidate.text, candidate.truncated)
        for candidate in find_closest_candidates(content, old)
    )
    return EditMiss(0, passages or _fragment_hint(content, old))


def _emphasis_copy(content: str, old: str, new: str) -> TextEdit | EditMiss | None:
    matches = matching_lines(content, old)
    if len(matches) > 1:
        return EditMiss(
            len(matches),
            tuple(_emphasis_passage(line, value) for line, _, value, _ in matches[:3]),
            tuple(line for line, _, _, _ in matches),
            similar=True,
        )
    if not matches:
        return None
    line, offset, current, safe = matches[0]
    if not safe:
        return EditMiss(0, (_emphasis_passage(line, current),))
    replacement = preserve_kept_emphasis(old, current, new)
    if replacement is None:
        return EditMiss(0, (_emphasis_passage(line, current),))
    updated = content[:offset] + replacement + content[offset + len(current) :]
    start = max(0, len(_common_prefix(old, current)) - 40)
    if len(current) <= _SNIPPET_LINE_CHARS:
        start = 0
    snippet = current[start : start + _SNIPPET_LINE_CHARS]
    return TextEdit(
        updated,
        line,
        updated != content,
        (
            WIKI_EMPHASIS_COPY.format(
                line=line,
                clipped=WIKI_EMPHASIS_CLIPPED if len(snippet) < len(current) else "",
                content=snippet,
            ),
        ),
    )


def _emphasis_passage(line: int, value: str) -> Passage:
    return Passage(line, value[:_SNIPPET_LINE_CHARS], len(value) > _SNIPPET_LINE_CHARS)


def _fragment_hint(content: str, old: str) -> tuple[Passage, ...]:
    """Name the line holding most of a one-line ``old`` that whole-line ranking missed."""

    fragment = old.strip()
    if not fragment or _LINE_BREAK.search(fragment):
        return ()
    best: tuple[float, int, str] = (0.0, 0, "")
    for number, line in enumerate(_LINE_BREAK.split(content)[:_HINT_SCAN_LINES], 1):
        block = SequenceMatcher(
            None, line[:_HINT_LINE_CHARS], fragment, autojunk=False
        ).find_longest_match()
        score = block.size / len(fragment)
        if score > best[0]:
            best = (score, number, line)
    score, number, line = best
    if score < _HINT_MIN_SHARE:
        return ()
    return (Passage(number, line[:_SNIPPET_LINE_CHARS], len(line) > _SNIPPET_LINE_CHARS),)


def _replace(content: str, old: str, new: str) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    return replace_fuzzy(content, old, new, replace_all=False, typographic=True)


def _without_shared_blank_boundaries(old: str, new: str) -> tuple[str, str]:
    """Drop leading and trailing blank lines present in both texts."""

    old_lines, new_lines = _LINE_BREAK.split(old), _LINE_BREAK.split(new)
    while len(old_lines) > 1 and len(new_lines) > 1 and not old_lines[0].strip():
        if new_lines[0].strip():
            break
        old_lines.pop(0)
        new_lines.pop(0)
    while len(old_lines) > 1 and len(new_lines) > 1 and not old_lines[-1].strip():
        if new_lines[-1].strip():
            break
        old_lines.pop()
        new_lines.pop()
    return "\n".join(old_lines), "\n".join(new_lines)


def _already_applied(content: str, old: str, new: str) -> int | None:
    """Return the line of ``new`` when the page already holds the requested change.

    ``new`` must occur exactly once, and the text the edit keeps must be
    substantive enough to tie that occurrence to this edit rather than to an
    unrelated copy elsewhere.
    """

    if not new.strip() or old == new:
        return None
    found = _replace(content, new, new)
    if not isinstance(found, FuzzyReplacement):
        return None
    old_lines, new_lines = _LINE_BREAK.split(old), _LINE_BREAK.split(new)
    kept = "".join(
        line.strip()
        for tag, i1, i2, _, _ in _opcodes(old_lines, new_lines)
        if tag == "equal"
        for line in old_lines[i1:i2]
    )
    if len(kept) >= 4 or _inline_anchors_identify(content, old, new):
        return found.first_changed_line
    return None


def _inline_anchors_identify(content: str, old: str, new: str) -> bool:
    """Identify a one-line change by retained text before and after it."""

    if "\n" in old or "\n" in new:
        return False
    prefix = _common_prefix(old, new)
    suffix = _common_prefix(old[len(prefix) :][::-1], new[len(prefix) :][::-1])[::-1]
    if any(
        len(anchor.strip()) < 4 or not re.search(r"\w{3}", anchor) for anchor in (prefix, suffix)
    ):
        return False
    lines = [
        line
        for line in _LINE_BREAK.split(content)
        if line.startswith(prefix) and line.endswith(suffix)
    ]
    return lines == [new]


def _ambiguous(content: str, match: AmbiguousFuzzyMatch, *, similar: bool = False) -> EditMiss:
    lines = _LINE_BREAK.split(content)
    starts = tuple(dict.fromkeys(match.line_numbers))
    passages = []
    for number in starts[:_MAX_OCCURRENCES_SHOWN]:
        window = lines[number - 1 : number + 1]
        passages.append(
            Passage(
                number,
                "\n".join(line[:_SNIPPET_LINE_CHARS] for line in window),
                any(len(line) > _SNIPPET_LINE_CHARS for line in window),
            )
        )
    return EditMiss(match.occurrences, tuple(passages), starts, similar)


def _opcodes(
    old_lines: list[str], new_lines: list[str]
) -> Sequence[tuple[str, int, int, int, int]]:
    return SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()


def _common_prefix(first: str, second: str) -> str:
    size = 0
    for left, right in zip(first, second, strict=False):
        if left != right:
            break
        size += 1
    return first[:size]


__all__ = ["EditMiss", "Passage", "TextEdit", "apply_text_edit"]
