"""Fuzzy text matching and replacement for file patches.

The exact text an edit targets is frequently *almost* right: the model sends
straight quotes where the file has curly ones, a bare ``\\n`` where the file uses
``\\r\\n``, or a different indentation than the file actually has. A literal match
then fails and the edit is rejected even though the intended target is
unambiguous. This module tries a short chain of increasingly tolerant — but never
*guessing* — strategies and always replaces the real original characters at the
matched span.

Strategies, in order; the first that finds any match wins (its own ambiguity is
terminal — it does not fall through to a looser strategy):

1. ``exact`` — literal substring match that does not split a CRLF pair.
2. ``normalized`` — match after collapsing CR/CRLF to LF and mapping a few
   visually-equivalent Unicode characters (curly quotes, non-breaking space,
   en-dash) to ASCII, on both sides. Character-level, so it also matches a
   fragment within a line. In a CRLF file it first searches the old text with
   CRLF line breaks, which finds the same spans without mapping every
   character when the normalized text holds no further match.
3. ``line_trimmed`` — match whole lines after stripping each line's leading and
   trailing whitespace (plus the same Unicode mapping). The replacement is
   re-indented in the file's own style (tabs or its level width), so a
   whitespace-only match never corrupts indentation.
4. ``whitespace_normalized`` — collapse horizontal space/tab runs while preserving
   line boundaries. The replacement is re-indented like a line-trimmed match.

All non-exact strategies search a normalized copy of the content and map the
match back to the original characters through a per-character span map, so CRLF line
endings and the exact original characters are always preserved. Old text copied
with other errors is ``copy_match``'s concern.

Searching text a reader sees (``fold_for_search``, used by web_fetch ``find``) is
looser than locating an edit, because a wrong guess only shows another passage:
every run of whitespace reads as one space, invisible characters as nothing, and
quote, dash and ligature glyphs as their plain forms (every dash as one hyphen).
The caller names further presentation to drop, such as Markdown syntax.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from difflib import SequenceMatcher

from core.tools._fuzzy_candidates import find_closest_candidates as find_closest_candidates
from core.tools._fuzzy_candidates import find_closest_passages as find_closest_passages
from core.tools._fuzzy_differences import line_differences as line_differences
from core.tools._fuzzy_text import (
    _HORIZONTAL_WHITESPACE_RE,
    _LINE_ENDINGS,
    _WORD_RE,
    _apply_replacements,
    _find_all,
    _folded,
    _found_with_crlf,
    _inside_crlf,
    _leftmost_non_overlapping,
    _line_number_at,
    _line_starts,
    _normalize_newlines,
    _normalize_replacement_newlines,
    _normalize_text,
    _normalize_with_spans,
    _on_line_boundaries,
)
from core.tools._fuzzy_text import SearchText as SearchText
from core.tools._fuzzy_text import dominant_line_ending as dominant_line_ending
from core.tools._fuzzy_text import fold_for_search as fold_for_search
from core.tools._fuzzy_types import AmbiguousFuzzyMatch as AmbiguousFuzzyMatch
from core.tools._fuzzy_types import ClosestFuzzyCandidate as ClosestFuzzyCandidate
from core.tools._fuzzy_types import FuzzyReplacement as FuzzyReplacement
from core.tools._fuzzy_types import LineDifferences as LineDifferences


def replace_fuzzy(
    content: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool,
    whole_lines: bool = False,
    at_eof: bool = False,
    typographic: bool = False,
    first: bool = False,
    rewrapped: bool = False,
    whitespace: bool = True,
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    """Find ``old_string`` in ``content`` via the strategy chain and replace it.

    Returns a :class:`FuzzyReplacement` on success, an :class:`AmbiguousFuzzyMatch`
    when the winning strategy matched more than once without ``replace_all`` or
    ``first``, or ``None`` when no strategy matched.

    Patch callers can require whole-line spans, anchor a hunk at EOF, or take the
    winning strategy's first match. ``rewrapped`` adds a last strategy for one
    target: ``old_string`` whose line breaks fall in other places than the file's,
    for an edit that changes more than whitespace.
    ``whitespace=False`` keeps spacing and indentation significant, including
    in the typographic step, for callers checking an edit's post-state.
    """
    replacement_text = _normalize_replacement_newlines(new_string)
    old_lf = _normalize_newlines(old_string)
    file_ending = dominant_line_ending(content)

    for name, matcher, reindent in _STRATEGIES:
        if not whitespace and name in {"line_trimmed", "whitespace_normalized"}:
            continue
        matches = (
            _match_normalized(
                content,
                old_string,
                typographic=True,
                whole_lines=whole_lines,
                trim_lines=whitespace,
            )
            if typographic and name == "normalized"
            else matcher(content, old_string)
        )
        # A span never starts or ends inside a CRLF pair: an old text starting
        # with LF would leave the CR behind. The normalized step takes the pair.
        matches = [
            (start, end)
            for start, end in matches
            if not _inside_crlf(content, start) and not _inside_crlf(content, end)
        ]
        if whole_lines:
            matches = [
                (start, end) for start, end in matches if _on_line_boundaries(content, start, end)
            ]
        if at_eof:
            matches = [
                (start, end) for start, end in matches if content[end:] in ("", *_LINE_ENDINGS)
            ]
        if not matches:
            continue
        # Matchers report overlapping occurrences too: a shadowed second
        # occurrence still makes a single-target request ambiguous.
        matches = sorted(set(matches))
        if len(matches) > 1 and not replace_all and not first:
            line_starts = _line_starts(content)
            return AmbiguousFuzzyMatch(
                len(matches),
                [bisect_right(line_starts, start) for start, _ in matches],
                tuple(matches),
            )

        selected = _leftmost_non_overlapping(matches) if replace_all else matches[:1]
        return _replaced(
            content,
            selected,
            replacement_text,
            name,
            reindent=reindent or (typographic and name == "normalized"),
            old_string_lf=old_lf,
            file_ending=file_ending,
        )

    # An edit of whitespace alone is about the line breaks that old_string
    # misstates: what the edit means is unclear.
    if (
        not rewrapped
        or not whitespace
        or first
        or whole_lines
        or at_eof
        or _unspaced(old_string) == _unspaced(new_string)
    ):
        return None
    matches = [
        (start, end)
        for start, end in _match_rewrapped(content, old_string)
        if not _inside_crlf(content, start) and not _inside_crlf(content, end)
    ]
    if len(matches) > 1 and not replace_all:
        line_starts = _line_starts(content)
        return AmbiguousFuzzyMatch(
            len(matches),
            [bisect_right(line_starts, start) for start, _ in matches],
            tuple(matches),
        )
    if not matches:
        return None
    return _replaced(
        content,
        _leftmost_non_overlapping(matches),
        replacement_text,
        "rewrapped",
        reindent=True,
        old_string_lf=old_lf,
        file_ending=file_ending,
        lines_correspond=False,
    )


def _replaced(
    content: str,
    selected: list[tuple[int, int]],
    replacement_text: str,
    strategy: str,
    *,
    reindent: bool,
    old_string_lf: str,
    file_ending: str | None,
    lines_correspond: bool = True,
) -> FuzzyReplacement:
    """Replace the ``selected`` spans and describe the result."""
    new_content, before_spans, after_spans = _apply_replacements(
        content,
        selected,
        replacement_text,
        reindent=reindent,
        old_string_lf=old_string_lf,
        file_ending=file_ending,
        lines_correspond=lines_correspond,
    )
    # The spans ascend without overlapping, so the last one reaches furthest.
    first_line = _line_number_at(content, selected[0][0])
    last_start, last_end = after_spans[-1]
    last_line = _line_number_at(new_content, max(last_start, last_end - 1))
    return FuzzyReplacement(
        new_content,
        first_line,
        last_line,
        len(selected),
        strategy,
        before_spans,
        after_spans,
    )


def _match_exact(content: str, pattern: str) -> list[tuple[int, int]]:
    return _find_all(content, pattern)


def _match_normalized(
    content: str,
    pattern: str,
    *,
    typographic: bool = False,
    whole_lines: bool = False,
    trim_lines: bool = True,
) -> list[tuple[int, int]]:
    normalized_pattern = _normalize_with_spans(pattern, typographic=typographic)[0]
    if not normalized_pattern:
        return []
    matches: list[tuple[int, int]] = [
        (start, end)
        for start, end in _found_with_crlf(content, pattern, typographic=typographic)
        if not whole_lines or _on_line_boundaries(content, start, end)
    ]
    if matches:
        return matches
    normalized_content, spans = _normalize_with_spans(content, typographic=typographic)

    matches = []
    pattern_length = len(normalized_pattern)
    start = 0
    while True:
        position = normalized_content.find(normalized_pattern, start)
        if position < 0:
            break
        end = position + pattern_length
        # A substring of an expanded glyph must not authorize replacing it.
        if (position == 0 or spans[position - 1] != spans[position]) and (
            end == len(spans) or spans[end - 1] != spans[end]
        ):
            left, right = spans[position][0], spans[end - 1][1]
            if not whole_lines or (
                (position == 0 or normalized_content[position - 1] == "\n")
                and (end == len(normalized_content) or normalized_content[end] == "\n")
            ):
                matches.append((left, right))
        start = position + 1
    if not matches and typographic and trim_lines:
        matches = [
            (spans[start][0], spans[end - 1][1])
            for start, end in _match_line_trimmed(normalized_content, normalized_pattern)
            if end > start
        ]
    return matches


def preserve_typography(actual: str, locator: str, replacement: str) -> str:
    """Retain original glyphs in unchanged portions of a normalized patch line.

    Only a precisely equivalent preimage proves the character correspondence.
    A changed glyph or a merely similar preimage never authorizes restoration.
    """
    actual_body, old_body, new_body = (
        text.lstrip(" \t") for text in (actual, locator, replacement)
    )
    actual_folded, actual_spans = _normalize_with_spans(actual_body, typographic=True)
    old_folded = _normalize_with_spans(old_body, typographic=True)[0]
    new_folded, new_spans = _normalize_with_spans(new_body, typographic=True)
    if actual_folded != old_folded:
        return replacement
    blocks = SequenceMatcher(None, old_folded, new_folded, autojunk=False).get_matching_blocks()
    replacements = []
    index = 0
    while index < len(actual_spans):
        start, end = actual_spans[index]
        limit = index + 1
        while limit < len(actual_spans) and actual_spans[limit] == (start, end):
            limit += 1
        original = actual_body[start:end]
        folded = actual_folded[index:limit]
        if original != folded:
            for block in blocks:
                if block.a <= index and limit <= block.a + block.size:
                    new_start = block.b + index - block.a
                    new_end = new_start + limit - index
                    left, right = new_spans[new_start][0], new_spans[new_end - 1][1]
                    if new_body[left:right] == folded:
                        replacements.append((left, right, original))
                    break
        index = limit
    for start, end, original in reversed(replacements):
        new_body = new_body[:start] + original + new_body[end:]
    return replacement[: len(replacement) - len(replacement.lstrip(" \t"))] + new_body


def _match_line_trimmed(content: str, pattern: str) -> list[tuple[int, int]]:
    # Work on the normalized content (LF, Unicode-folded) with its span map, so a
    # match maps back to the exact original characters and CRLF endings are preserved.
    normalized_content, spans = _normalize_with_spans(content)
    content_lines = normalized_content.split("\n")
    pattern_lines = _normalize_text(pattern).split("\n")

    trimmed_content = [line.strip() for line in content_lines]
    trimmed_pattern = [line.strip() for line in pattern_lines]
    window = len(trimmed_pattern)
    if window == 0:
        return []

    # Character offset (in the normalized string) where each line begins.
    line_offsets: list[int] = []
    cursor = 0
    for line in content_lines:
        line_offsets.append(cursor)
        cursor += len(line) + 1  # +1 for the splitting "\n"

    matches: list[tuple[int, int]] = []
    for index in range(len(trimmed_content) - window + 1):
        if trimmed_content[index : index + window] == trimmed_pattern:
            norm_start = line_offsets[index]
            last_line = index + window - 1
            norm_end = line_offsets[last_line] + len(content_lines[last_line])
            if norm_start < len(spans) and norm_end > norm_start:
                matches.append((spans[norm_start][0], spans[norm_end - 1][1]))
    return matches


def _collapse_horizontal_whitespace_with_spans(
    text: str,
) -> tuple[str, list[tuple[int, int]]]:
    """Normalize text and collapse each horizontal whitespace run to one space."""
    normalized, source_spans = _normalize_with_spans(text)
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    for run in _HORIZONTAL_WHITESPACE_RE.finditer(normalized):
        start, end = run.span()
        chars.append(normalized[cursor:start])
        spans.extend(source_spans[cursor:start])
        chars.append(" ")
        spans.append((source_spans[start][0], source_spans[end - 1][1]))
        cursor = end
    chars.append(normalized[cursor:])
    spans.extend(source_spans[cursor:])
    return "".join(chars), spans


def _match_whitespace_normalized(content: str, pattern: str) -> list[tuple[int, int]]:
    normalized_pattern = _collapse_horizontal_whitespace_with_spans(pattern)[0]
    if not normalized_pattern:
        return []
    normalized_content, spans = _collapse_horizontal_whitespace_with_spans(content)

    matches: list[tuple[int, int]] = []
    pattern_length = len(normalized_pattern)
    start = 0
    while True:
        position = normalized_content.find(normalized_pattern, start)
        if position < 0:
            break
        matches.append((spans[position][0], spans[position + pattern_length - 1][1]))
        start = position + 1
    return matches


# A copy whose line breaks fall in other places identifies its passage only
# with this many words: "return x" against "return\nx" is too short to tell.
_REWRAPPED_MIN_WORDS = 4
_SPACE_RUN_RE = re.compile(r"[ \t\n]+")
# Whitespace between words with at most one line break: any such run of the file.
_ONE_BREAK = r"(?:[ \t]+(?:\n[ \t]*)?|\n[ \t]*)"


def _space_pattern(run: str) -> str:
    """Match a whitespace run between two words of the copy in the file.

    A space, a line break or both stand for each other; a blank line, two or
    more line breaks, must be in the file as well.
    """
    breaks = run.count("\n")
    return _ONE_BREAK if breaks < 2 else r"[ \t]*" + r"\n[ \t]*" * breaks


def _unspaced(text: str) -> str:
    return "".join(text.split())


def _rewrapped_parts(old: str) -> tuple[str, str, str]:
    """Split ``old``, folded like the normalized strategy, into lead, words and trail."""
    text = _folded(old, typographic=True)
    core = text.strip(" \t\n")
    if not core:
        return text, "", ""
    start = text.index(core)
    return text[:start], core, text[start + len(core) :]


def _match_rewrapped(content: str, old: str) -> list[tuple[int, int]]:
    """Match ``old`` with its line breaks in other places than the file's.

    Every word (non-whitespace run) must be the file's, in order; a space and a
    line break with any indentation stand for each other between words. Leading
    and trailing whitespace keeps its line breaks: a copy that starts with a
    line break starts a line of the file, one that ends with a line break ends
    one, and one that starts with indentation alone starts a line or follows a
    space or tab inside one. At least one line break must fall elsewhere, else a precise strategy
    decides, and the copy needs ``_REWRAPPED_MIN_WORDS`` words.
    """
    lead, core, trail = _rewrapped_parts(old)
    if len(_WORD_RE.findall(core)) < _REWRAPPED_MIN_WORDS:
        return []
    words = _SPACE_RUN_RE.split(core)
    runs = _SPACE_RUN_RE.findall(core)
    body = re.escape(words[0]) + "".join(
        _space_pattern(run) + re.escape(word) for run, word in zip(runs, words[1:], strict=True)
    )
    lead_breaks = lead.count("\n")
    if lead_breaks:
        # Trailing spaces of the line before stay; the indentation is replaced.
        head = r"\n" + r"[ \t]*\n" * (lead_breaks - 1) + r"[ \t]*"
    else:
        # Indentation alone: the start of a line, or a space or tab inside one,
        # as where a file written on one line holds the passage.
        head = r"(?:^[ \t]*|(?<=[ \t]))" if lead else ""
    trail_breaks = trail.count("\n")
    if trail_breaks:
        tail = r"[ \t]*\n" * trail_breaks + (r"[ \t]*" if trail[-1] != "\n" else "")
    else:
        tail = r"[ \t]*$" if trail else ""
    pattern = re.compile(f"(?=({head}({body}){tail}))", re.MULTILINE)
    wanted = [run.count("\n") for run in runs]
    folded = _folded(content, typographic=True)
    found = [
        match.span(1)
        for match in pattern.finditer(folded)
        # A copy whose line breaks are all the file's is the precise strategies' concern.
        if [run.count("\n") for run in _SPACE_RUN_RE.findall(match[2])] != wanted
        # After indentation alone, the match from the start of the line holds the passage.
        and not (lead and not lead_breaks and _after_indentation(folded, match.start(1)))
    ]
    if not found:
        return []
    spans = _normalize_with_spans(content, typographic=True)[1]
    matches = []
    for start, end in found:
        # A part of an expanded glyph must not authorize replacing it.
        if (start and spans[start - 1] == spans[start]) or (
            end < len(spans) and spans[end - 1] == spans[end]
        ):
            continue
        matches.append((spans[start][0], spans[end - 1][1]))
    return matches


def _after_indentation(text: str, start: int) -> bool:
    """Whether only spaces and tabs, at least one, precede ``start`` on its line."""
    line_start = text.rfind("\n", 0, start) + 1
    return start > line_start and not text[line_start:start].strip(" \t")


def rewrapped_place(content: str, old: str) -> tuple[int, int] | None:
    """Return the span that holds every word of ``old`` in order, if exactly one place does.

    Any whitespace stands for any other between the words; leading and trailing
    whitespace counts for nothing, so the span runs from the first word to the
    last. A failure report shows this place: there ``old`` differs from the
    file only in its whitespace.
    """
    _lead, core, _trail = _rewrapped_parts(old)
    if not core:
        return None
    words = _SPACE_RUN_RE.split(core)
    pattern = re.compile("(?=(" + r"[ \t\n]+".join(map(re.escape, words)) + "))")
    found = [match.span(1) for match in pattern.finditer(_folded(content, typographic=True))]
    if len(found) != 1:
        return None
    start, end = found[0]
    spans = _normalize_with_spans(content, typographic=True)[1]
    return spans[start][0], spans[end - 1][1]


# (name, matcher, reindent-replacement) in increasing tolerance.
_STRATEGIES = (
    ("exact", _match_exact, False),
    ("normalized", _match_normalized, False),
    ("line_trimmed", _match_line_trimmed, True),
    ("whitespace_normalized", _match_whitespace_normalized, True),
)


__all__ = [
    "AmbiguousFuzzyMatch",
    "ClosestFuzzyCandidate",
    "FuzzyReplacement",
    "LineDifferences",
    "SearchText",
    "dominant_line_ending",
    "find_closest_candidates",
    "find_closest_passages",
    "fold_for_search",
    "line_differences",
    "replace_fuzzy",
    "preserve_typography",
]
