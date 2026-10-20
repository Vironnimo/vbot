"""Locate and apply one Wiki ``old_text``/``new_text`` edit.

The matching follows ``apply_patch``: precise matching first, up to newline,
Unicode, typography and whitespace differences; then the same edit without blank
boundary lines both texts share; then evidence that the page already holds the
change. Only after those, ``copy_match`` may apply an ``old_text`` copied with
errors: text the edit keeps stays as the page has it, so a peer's wording is never
overwritten, and the notes name each changed line that differed. Ambiguity is
terminal at the step that finds it. A miss returns bounded line hints for the
error, never a guess; the passage holding the words that set ``old_text`` apart,
such as a table row's ID, comes first. ``held_in_place`` tells, from a revision
that held ``old_text``, whether a later one already holds the edit there.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from difflib import SequenceMatcher

from core.tools.copy_match import copy_warnings, replace_copied
from core.tools.fuzzy_match import (
    AmbiguousFuzzyMatch,
    FuzzyReplacement,
    LineDifferences,
    find_closest_candidates,
    line_differences,
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
# A miss names this many lines where old_text differs from the closest passage.
_DIFFERENCES_SHOWN = 5
# A read shows at most this many characters (the swarm_wiki read maximum).
_READ_MAX_CHARS = 20000
_WORD = re.compile(r"\w+")
# A passage holding at least this share of old_text's word weight may be shown
# first, when no passage apart from it holds more than the rival share of its weight.
_DISTINCTIVE_MIN_SHARE = 0.5
_DISTINCTIVE_RIVAL = 0.8
# A closest passage shows at most this much, as fuzzy_match bounds its candidates.
_PASSAGE_LINES = 8
_PASSAGE_CHARS = 1200


@dataclass(frozen=True)
class TextEdit:
    """The page content after the edit; ``applied`` is False when it already held it.

    ``notes`` tell the caller where its ``old_text`` differed from the page. An
    applied edit names the passage it replaced, as (start, end) in the content
    before it (``before``), and the text it wrote in ``content`` (``after``).
    """

    content: str
    line: int
    applied: bool
    notes: tuple[str, ...] = ()
    before: tuple[int, int] | None = None
    after: tuple[int, int] | None = None


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
    several passages. On a miss, ``differences`` names the first few of the
    ``different_lines`` where ``old_text`` differs from the closest passage
    (``fuzzy_match.line_differences``), and ``span`` the character offset and
    length of the passage to read.
    """

    occurrences: int
    passages: tuple[Passage, ...]
    lines: tuple[int, ...] = ()
    similar: bool = False
    differences: tuple[dict[str, int | str | bool | None], ...] = ()
    different_lines: int = 0
    span: tuple[int, int] | None = None


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
            return _applied(found)
    held = _already_applied(content, old, new)
    if held is not None:
        return TextEdit(content, held, False)
    emphasis = _emphasis_copy(content, old, new)
    if isinstance(emphasis, EditMiss) and not emphasis.occurrences:
        line = emphasis.passages[0].line
        return replace(emphasis, span=_span(content, line, line))
    if emphasis is not None:
        return emphasis
    if not new.strip() or _replace(content, new, new) is None:
        copied = replace_copied(content, old, new)
        if isinstance(copied, AmbiguousFuzzyMatch):
            return _ambiguous(content, copied, similar=True)
        if copied is not None:
            return _applied(copied, tuple(copy_warnings(copied)))
    candidates = find_closest_candidates(content, old)
    passages = tuple(
        Passage(candidate.line_number, candidate.text, candidate.truncated)
        for candidate in candidates
    ) or _fragment_hint(content, old)
    distinctive = _distinctive_passage(content, old, passages[0].line if passages else None)
    if distinctive is not None:
        shown = set(range(distinctive.line, _last_line(distinctive) + 1))
        passages = (
            distinctive,
            *(
                passage
                for passage in passages
                if not set(range(passage.line, _last_line(passage) + 1)) & shown
            ),
        )
    if not passages:
        return EditMiss(0, ())
    if distinctive is not None:
        compared = _differences_within(content, old, distinctive.line)
    elif candidates:
        compared = line_differences(
            content, old, candidates[0].line_number, limit=_DIFFERENCES_SHOWN
        )
    else:
        compared = None
    if compared is None:
        first = passages[0]
        return EditMiss(0, passages, span=_span(content, first.line, _last_line(first)))
    return EditMiss(
        0,
        passages,
        differences=compared.differences,
        different_lines=compared.count,
        span=_span(content, compared.first_line, compared.last_line),
    )


def _applied(found: FuzzyReplacement, notes: tuple[str, ...] = ()) -> TextEdit:
    """Describe a replacement of one passage as an applied edit."""

    return TextEdit(
        found.new_content,
        found.first_changed_line,
        True,
        notes,
        found.before_spans[0],
        found.after_spans[0],
    )


def _last_line(passage: Passage) -> int:
    return passage.line + passage.text.count("\n")


def _span(content: str, first_line: int, last_line: int) -> tuple[int, int]:
    """Return the offset and length of page lines ``first_line`` to ``last_line``.

    The offset counts characters as a read does; the length ends with the last
    line's text and is bounded by what one read shows.
    """

    starts = [0, *(match.end() for match in _LINE_BREAK.finditer(content))]
    first = min(max(first_line, 1), len(starts)) - 1
    last = min(max(last_line, first_line), len(starts))
    end = len(content[: starts[last]].rstrip("\r\n")) if last < len(starts) else len(content)
    return starts[first], max(1, min(end - starts[first], _READ_MAX_CHARS))


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
    applied = updated != content
    return TextEdit(
        updated,
        line,
        applied,
        (
            WIKI_EMPHASIS_COPY.format(
                line=line,
                clipped=WIKI_EMPHASIS_CLIPPED if len(snippet) < len(current) else "",
                content=snippet,
            ),
        ),
        (offset, offset + len(current)) if applied else None,
        (offset, offset + len(replacement)) if applied else None,
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


def _distinctive_passage(content: str, old: str, first: int | None) -> Passage | None:
    """Return the passage holding the words that set ``old`` apart, when it stands out.

    Character similarity ranks lines sharing a layout, such as the rows of a
    table, by that layout; the words that tell them apart, such as a row ID,
    decide here. Each word of ``old`` weighs more the fewer page lines hold it.
    The passage of ``old``'s line count holding the most weight is returned when
    it holds at least half of all, no passage apart from it comes close, and it
    outweighs the passage at line ``first``, which character similarity ranked
    first.
    """

    words = _words(old)
    lines = _LINE_BREAK.split(content)[:_HINT_SCAN_LINES]
    size = len(_text_lines(old))
    if not words or not size or size > len(lines):
        return None
    held = [_words(line) & words for line in lines]
    frequency = Counter(word for line in held for word in line)
    weight = {word: math.log((len(lines) + 1) / (frequency[word] + 0.5)) for word in words}
    total = sum(weight.values())
    scores = _window_weights(held, size, weight)
    best = max(range(len(scores)), key=lambda index: (scores[index], -index))
    rivals = [
        score for index, score in enumerate(scores) if index <= best - size or index >= best + size
    ]
    ranked = first - 1 if first is not None and 0 < first <= len(scores) else None
    if (
        scores[best] < _DISTINCTIVE_MIN_SHARE * total
        or max(rivals, default=0.0) > _DISTINCTIVE_RIVAL * scores[best]
        or (ranked is not None and scores[best] <= scores[ranked] + 1e-9)
    ):
        return None
    shown = lines[best : best + min(size, _PASSAGE_LINES)]
    text = "\n".join(shown)
    return Passage(best + 1, text[:_PASSAGE_CHARS], size > len(shown) or len(text) > _PASSAGE_CHARS)


def _differences_within(content: str, old: str, line: int) -> LineDifferences | None:
    """Compare ``old`` with the page passage of its line count that starts at ``line``.

    Only that passage is compared, so a neighboring line of the same layout is
    never paired with ``old`` instead of the passage its words identify.
    """

    size = len(_text_lines(old))
    passage = "\n".join(_LINE_BREAK.split(content)[line - 1 : line - 1 + size])
    compared = line_differences(passage, old, 1, limit=_DIFFERENCES_SHOWN)
    if compared is None:
        return None
    shift = line - 1
    return LineDifferences(
        compared.first_line + shift,
        compared.last_line + shift,
        tuple(
            {**item, "line": None if item["line"] is None else int(item["line"]) + shift}
            for item in compared.differences
        ),
        compared.count,
    )


def _text_lines(text: str) -> list[str]:
    """Return the lines of ``text`` without leading and trailing blank lines."""

    lines = _LINE_BREAK.split(text)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _words(text: str) -> set[str]:
    return {word.casefold() for word in _WORD.findall(text)}


def _window_weights(held: list[set[str]], size: int, weight: dict[str, float]) -> list[float]:
    """Return, for each run of ``size`` lines, the weight of the distinct words it holds."""

    counts: Counter[str] = Counter()
    total = 0.0
    weights: list[float] = []
    for index, words in enumerate(held):
        for word in words:
            if not counts[word]:
                total += weight[word]
            counts[word] += 1
        if index >= size:
            for word in held[index - size]:
                counts[word] -= 1
                if not counts[word]:
                    total -= weight[word]
        if index >= size - 1:
            weights.append(total)
    return weights


def holds_passage(content: str, old: str) -> bool:
    """Whether precise matching finds ``old`` in ``content``, once or more."""

    return _replace(content, old, old) is not None


def held_in_place(before: str, after: str, old: str, new: str) -> int | None:
    """Return the line where ``after`` holds this edit in place of ``old`` in ``before``.

    The edit is applied to ``before``. The lines it wrote must be new to
    ``before``, occur exactly once in ``after``, and still border a line that
    bordered them there, so ``after`` holds them where ``old`` was.
    """

    edit = apply_text_edit(before, old, new)
    if not isinstance(edit, TextEdit) or not edit.applied:
        return None
    before_lines, edited = _LINE_BREAK.split(before), _LINE_BREAK.split(edit.content)
    changed = [block for block in _opcodes(before_lines, edited) if block[0] != "equal"]
    if not changed:
        return None
    start, end = changed[0][3], changed[-1][4]
    written = edited[start:end]
    if not "".join(written).strip() or _block_starts(before_lines, written):
        return None
    after_lines = _LINE_BREAK.split(after)
    found = _block_starts(after_lines, written)
    if len(found) != 1:
        return None
    place = found[0]
    above = start > 0 and place > 0 and after_lines[place - 1] == edited[start - 1]
    below = end < len(edited) and place + len(written) < len(after_lines)
    below = below and after_lines[place + len(written)] == edited[end]
    if not (above or below or (start == 0 and end == len(edited))):
        return None
    return place + 1


def _block_starts(lines: list[str], block: list[str]) -> list[int]:
    size = len(block)
    return [index for index in range(len(lines) - size + 1) if lines[index : index + size] == block]


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
    """Identify a one-line change by retained text before and after it.

    Both anchors must hold text and one of them words; the other may be bare,
    such as the empty cells ending a table row, since only the line with both
    anchors may hold ``new``.
    """

    if "\n" in old or "\n" in new:
        return False
    prefix = _common_prefix(old, new)
    suffix = _common_prefix(old[len(prefix) :][::-1], new[len(prefix) :][::-1])[::-1]
    if not (prefix.strip() and suffix.strip()) or not any(
        len(anchor.strip()) >= 4 and re.search(r"\w{3}", anchor) for anchor in (prefix, suffix)
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


__all__ = ["EditMiss", "Passage", "TextEdit", "apply_text_edit", "held_in_place", "holds_passage"]
