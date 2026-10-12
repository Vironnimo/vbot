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
"""

from __future__ import annotations

import math
import re
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import cache
from heapq import heappush, heapreplace
from os.path import commonprefix

from core.tools.arguments import TEXT_LINE_BREAK, split_text_lines

_CANDIDATE_ANCHOR_COUNT = 3
_CANDIDATE_ANCHOR_POOL_SIZE = 20
# A pattern line the file holds more often than this places no block by itself.
_CANDIDATE_EXACT_MAX_OCCURRENCES = 3
_CANDIDATE_SCAN_LINE_LIMIT = 50_000
_CANDIDATE_SCORE_LINE_MAX_CHARS = 240
_CANDIDATE_SCORE_MAX_CHARS = 4_000
_CANDIDATE_MIN_SIMILARITY = 0.60
_CANDIDATE_RESULT_LIMIT = 3
_CANDIDATE_OUTPUT_MAX_LINES = 8
_CANDIDATE_OUTPUT_MAX_CHARS = 1_200
# A copied line this similar to a file line is a reworded copy of it.
_SIMILAR_LINE = 0.5
# File lines past a copy's length that ``line_differences`` still compares, so the
# passage of a copy that dropped a few lines ends where the copy ends.
_DIFFERENCE_SLACK_LINES = 3
# A differing block this large pairs its lines in order instead of by similarity.
_ALIGN_MAX_CELLS = 2_500
_ALIGN_LINE_CHARS = 500
# Pairing two unlike lines still beats leaving both unpaired, so a rewritten line
# pairs with the line it replaced.
_UNLIKE_PAIR = 0.01

# Visually-equivalent characters models emit in place of their ASCII forms, keyed
# by code point so the source stays pure ASCII and the entries are unambiguous.
# Only 1:1 mappings live here so every normalized character maps back to exactly
# one original character; length-changing expansions (em-dash -> "--", ellipsis
# -> "...") are deliberately omitted to keep span mapping unambiguous.
_UNICODE_NORMALIZATION = {
    "“": '"',  # left double quotation mark
    "”": '"',  # right double quotation mark
    "‘": "'",  # left single quotation mark
    "’": "'",  # right single quotation mark
    "\u00a0": " ",  # non-breaking space
    "–": "-",  # en dash
}

# Patch-only typography folds may expand a glyph. Default substring calls keep
# their established normalization and replacement semantics.
_TYPOGRAPHIC_NORMALIZATION = {
    **_UNICODE_NORMALIZATION,
    "\u2014": "--",
    "\u2026": "...",
    "\u2212": "-",
    **dict.fromkeys(
        "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000", " "
    ),
}

# The line endings file Tools number (``TEXT_LINE_BREAK``). Detection order
# matters: CRLF first, then LF before CR for mixed files. Other separators such
# as U+2028 or form feed are ordinary characters within a line.
_LINE_ENDINGS = ("\r\n", "\n", "\r")
_LINE_BREAK_RE = TEXT_LINE_BREAK
_HORIZONTAL_WHITESPACE_RE = re.compile(r"[ \t]+")
_UNICODE_TRANSLATION = str.maketrans(_UNICODE_NORMALIZATION)
_UNICODE_GLYPHS = "[" + "".join(re.escape(char) for char in _UNICODE_NORMALIZATION) + "]"
_TYPOGRAPHIC_GLYPHS = "[" + "".join(re.escape(char) for char in _TYPOGRAPHIC_NORMALIZATION) + "]"
_UNICODE_GLYPH_RE = re.compile(_UNICODE_GLYPHS)
_TYPOGRAPHIC_GLYPH_RE = re.compile(_TYPOGRAPHIC_GLYPHS)
# What ``_normalize_with_spans`` changes: a CRLF or CR line ending, or a mapped glyph.
_UNICODE_SPECIAL_RE = re.compile("\r\n?|" + _UNICODE_GLYPHS)
_TYPOGRAPHIC_SPECIAL_RE = re.compile("\r\n?|" + _TYPOGRAPHIC_GLYPHS)


@dataclass(frozen=True)
class FuzzyReplacement:
    """A successful fuzzy replacement applied to the original content."""

    new_content: str
    first_changed_line: int
    last_changed_line: int
    replacements: int
    strategy: str
    before_spans: tuple[tuple[int, int], ...]
    after_spans: tuple[tuple[int, int], ...]
    # Only ``copy_match`` sets these: replaced and kept lines that differed from the
    # caller's copy as (1-based line, original text or its part around the first
    # difference), and the caller's misspelled words its new text took the file's
    # spelling of, as (caller's word, file's word).
    differed: tuple[tuple[int, str], ...] = ()
    respelled: tuple[tuple[str, str], ...] = ()
    kept_differed: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class AmbiguousFuzzyMatch:
    """The winning strategy matched more than once without ``replace_all``.

    ``line_numbers`` holds the 1-based line each match starts on and ``spans``
    its (start, end) offsets in the searched text, both in text order.
    """

    occurrences: int
    line_numbers: list[int]
    spans: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class ClosestFuzzyCandidate:
    """A bounded raw excerpt similar to an unmatched edit locator."""

    line_number: int
    text: str
    truncated: bool


@dataclass(frozen=True)
class LineDifferences:
    """Every line where a copy differs from the passage it was compared with.

    The passage spans file lines ``first_line`` to ``last_line`` (1-based).
    ``differences`` holds the first few differing lines in passage order and
    ``count`` all of them. Each names the file ``line`` and the ``copy_line``
    with at most 240-character windows of both (``file``, ``copy``); a line
    only the file holds has no copy side, a line only the copy holds no file
    side (``None``).
    """

    first_line: int
    last_line: int
    differences: tuple[dict[str, int | str | bool | None], ...]
    count: int


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
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    """Find ``old_string`` in ``content`` via the strategy chain and replace it.

    Returns a :class:`FuzzyReplacement` on success, an :class:`AmbiguousFuzzyMatch`
    when the winning strategy matched more than once without ``replace_all`` or
    ``first``, or ``None`` when no strategy matched.

    Patch callers can require whole-line spans, anchor a hunk at EOF, or take the
    winning strategy's first match. ``rewrapped`` adds a last strategy for one
    target: ``old_string`` whose line breaks fall in other places than the file's,
    for an edit that changes more than whitespace.
    """
    replacement_text = _normalize_replacement_newlines(new_string)
    old_lf = _normalize_newlines(old_string)
    file_ending = dominant_line_ending(content)

    for name, matcher, reindent in _STRATEGIES:
        matches = (
            _match_normalized(content, old_string, typographic=True, whole_lines=whole_lines)
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


def _candidate_normalize_line(line: str) -> str:
    normalized = _HORIZONTAL_WHITESPACE_RE.sub(" ", _normalize_text(line)).strip()
    return normalized[:_CANDIDATE_SCORE_LINE_MAX_CHARS]


def _similarity_bound(counts: Counter[str], length: int, text: str) -> float:
    """Return an upper bound of ``SequenceMatcher.ratio`` against ``text``.

    Matching characters are at most the shared character multiset, so the bound
    rejects dissimilar text before the quadratic comparison runs.
    """
    total = length + len(text)
    if not total:
        return 1.0
    return 2.0 * sum((counts & Counter(text)).values()) / total


def _exact_line_starts(
    normalized_lines: list[str], line_count: int, pattern_lines: list[str], window_size: int
) -> Counter[int]:
    """Count, per block start, the distinctive pattern lines the file holds exactly there.

    The longest pattern lines anchor the similarity search, but they are often
    new text the file lacks, such as added lines copied without a "+" prefix.
    Lines found exactly still place the block.
    """
    wanted: dict[str, list[int]] = {}
    for index, line in enumerate(pattern_lines):
        if line:
            wanted.setdefault(line, []).append(index)
    found: dict[str, list[int]] = {}
    for content_index, normalized in enumerate(normalized_lines[:_CANDIDATE_SCAN_LINE_LIMIT]):
        if normalized in wanted:
            found.setdefault(normalized, []).append(content_index)
    starts: Counter[int] = Counter()
    for line, occurrences in found.items():
        if len(occurrences) > _CANDIDATE_EXACT_MAX_OCCURRENCES:
            continue
        for content_index in occurrences:
            for pattern_index in wanted[line]:
                start = content_index - pattern_index
                if start >= 0 and start + window_size <= line_count:
                    starts[start] += 1
    return starts


def _top_anchor_starts(
    normalized_lines: list[str], line_count: int, anchor_index: int, anchor: str, window_size: int
) -> list[int]:
    """Return a bounded pool of block starts whose aligned line resembles an anchor."""
    heap: list[tuple[float, int]] = []
    matcher = SequenceMatcher(None, anchor)
    anchor_counts = Counter(anchor)
    scan_limit = min(line_count, _CANDIDATE_SCAN_LINE_LIMIT)
    for content_index in range(scan_limit):
        start = content_index - anchor_index
        if start < 0 or start + window_size > line_count:
            continue
        candidate_line = normalized_lines[content_index]
        if not candidate_line:
            continue
        # Starts ascend, so a line scoring no better than the pool's weakest
        # entry can never displace it; skip its full comparison.
        if len(heap) >= _CANDIDATE_ANCHOR_POOL_SIZE and (
            _similarity_bound(anchor_counts, len(anchor), candidate_line) <= heap[0][0]
        ):
            continue
        matcher.set_seq2(candidate_line)
        item = (matcher.ratio(), -start)
        if len(heap) < _CANDIDATE_ANCHOR_POOL_SIZE:
            heappush(heap, item)
        elif item > heap[0]:
            heapreplace(heap, item)
    return [-negative_start for _, negative_start in heap]


def find_closest_candidates(content: str, pattern: str) -> list[ClosestFuzzyCandidate]:
    """Return bounded diagnostic candidates without authorizing replacement.

    Similarity is used only to rank raw excerpts for a failed Tool result. This
    function never returns replacement spans and is not part of ``replace_fuzzy``'s
    destructive strategy chain.
    """
    if not content or not pattern:
        return []

    pattern_lines = split_text_lines(pattern)
    while pattern_lines and not pattern_lines[0].strip():
        pattern_lines.pop(0)
    while pattern_lines and not pattern_lines[-1].strip():
        pattern_lines.pop()
    if not pattern_lines:
        return []

    content_lines = split_text_lines(content)
    window_size = len(pattern_lines)
    if not content_lines or window_size > len(content_lines):
        return []

    normalized_pattern_lines = [_candidate_normalize_line(line) for line in pattern_lines]
    anchors = sorted(
        (
            (len(line), -index, index, line)
            for index, line in enumerate(normalized_pattern_lines)
            if line
        ),
        reverse=True,
    )[:_CANDIDATE_ANCHOR_COUNT]
    if not anchors:
        return []

    # Every block starts before the scan limit, so no scored line lies beyond
    # one window past it.
    line_count = len(content_lines)
    normalized_lines = [
        _candidate_normalize_line(line)
        for line in content_lines[: _CANDIDATE_SCAN_LINE_LIMIT + window_size]
    ]
    possible_starts: set[int] = set()
    for _, _, anchor_index, anchor in anchors:
        possible_starts.update(
            _top_anchor_starts(normalized_lines, line_count, anchor_index, anchor, window_size)
        )
    placed = _exact_line_starts(normalized_lines, line_count, normalized_pattern_lines, window_size)
    possible_starts.update(placed)

    pattern_score_text = "\n".join(normalized_pattern_lines)[:_CANDIDATE_SCORE_MAX_CHARS]
    pattern_counts = Counter(pattern_score_text)
    scored: list[tuple[float, int]] = []
    for start in possible_starts:
        candidate_score_text = "\n".join(normalized_lines[start : start + window_size])[
            :_CANDIDATE_SCORE_MAX_CHARS
        ]
        # Two exact lines in place show the block even when new text lowers the similarity.
        if placed[start] < 2 and (
            _similarity_bound(pattern_counts, len(pattern_score_text), candidate_score_text)
            < _CANDIDATE_MIN_SIMILARITY
        ):
            continue
        similarity = SequenceMatcher(None, pattern_score_text, candidate_score_text).ratio()
        if similarity >= _CANDIDATE_MIN_SIMILARITY or placed[start] >= 2:
            scored.append((similarity, start))
    scored.sort(key=lambda item: (-item[0], item[1]))

    candidates: list[ClosestFuzzyCandidate] = []
    seen_text: set[str] = set()
    output_line_count = min(window_size, _CANDIDATE_OUTPUT_MAX_LINES)
    for _, start in scored:
        full_excerpt = "\n".join(content_lines[start : start + output_line_count])
        excerpt = full_excerpt[:_CANDIDATE_OUTPUT_MAX_CHARS]
        if not excerpt or excerpt in seen_text:
            continue
        seen_text.add(excerpt)
        candidates.append(
            ClosestFuzzyCandidate(
                line_number=start + 1,
                text=excerpt,
                truncated=(
                    output_line_count < window_size
                    or len(full_excerpt) > _CANDIDATE_OUTPUT_MAX_CHARS
                ),
            )
        )
        if len(candidates) >= _CANDIDATE_RESULT_LIMIT:
            break
    return candidates


def first_difference(
    content: str, copy: str, start: int, *, placed: bool = False, whole_lines: bool = False
) -> dict[str, int | str | bool] | None:
    """Locate where ``copy`` first differs from ``content`` near line ``start``.

    ``start`` is the 1-based line of the closest candidate for ``copy``;
    ``placed`` says it holds the copy's first word. Lines compare with runs of
    whitespace collapsed. The result names the file line and the copied line
    with 1-based character positions, and at most 240-character windows of
    both lines around the difference. ``None`` means every copied line matches
    the file from the aligned position on. ``whole_lines`` says the copy's
    first line is a whole line; otherwise only a copy that starts with a line
    break claims to start one.
    """
    file_lines = split_text_lines(content)
    wanted_lines = split_text_lines(copy)
    first = next((index for index, line in enumerate(wanted_lines) if line.strip()), 0)
    aligned = start if placed else _aligned_start(file_lines, start, wanted_lines, first)
    for position, wanted in enumerate(wanted_lines[first:], first):
        number = aligned + position - first
        if number > len(file_lines):
            return None
        actual = file_lines[number - 1]
        if _loose(actual) != _loose(wanted):
            file_position, copy_position = _difference_positions(actual, wanted)
            file_start, file_text = _difference_window(actual, file_position)
            copy_start, copy_text = _difference_window(wanted, copy_position)
            following = (
                file_lines[number] if number < len(file_lines) else None,
                wanted_lines[position + 1] if position + 1 < len(wanted_lines) else None,
            )
            return {
                "line": number,
                "character": file_position + 1,
                "copy_line": position + 1,
                "copy_character": copy_position + 1,
                "file_start": file_start,
                "copy_start": copy_start,
                "file": file_text,
                "copy": copy_text,
                "truncated": len(file_text) < len(actual) or len(copy_text) < len(wanted),
                **_break_difference(
                    actual,
                    wanted,
                    following,
                    starts=position == first and (whole_lines or first > 0),
                    part=position == first and not (whole_lines or first > 0),
                ),
            }
    return None


def line_differences(content: str, copy: str, start: int, *, limit: int) -> LineDifferences | None:
    """Compare ``copy`` with the passage of ``content`` it was taken from, line by line.

    ``start`` is the 1-based line of the closest candidate for ``copy``; the
    passage starts where ``first_difference`` aligns the copy. Lines compare
    with runs of whitespace collapsed, and lines the copy added or dropped are
    told apart from changed ones, so one missing line does not make every later
    line differ. File lines before the copy's first line and after its last are
    not part of the passage. ``None`` when the copy holds no text or the file no
    line at the aligned place.
    """
    file_lines = split_text_lines(content)
    copy_lines = split_text_lines(copy)
    marked = [index for index, line in enumerate(copy_lines) if line.strip()]
    if not marked:
        return None
    first, end = marked[0], marked[-1] + 1
    begin = _aligned_start(file_lines, start, copy_lines, first) - 1
    wanted = copy_lines[first:end]
    # A few more file lines than copied, so a dropped line does not cut the passage short.
    window = file_lines[begin : begin + len(wanted) + _DIFFERENCE_SLACK_LINES]
    if not window:
        return None
    matcher = SequenceMatcher(
        None, [_loose(line) for line in window], [_loose(line) for line in wanted], autojunk=False
    )
    pairs: list[tuple[int | None, int | None]] = []
    passage: list[int] = []
    for tag, file_start, file_end, copy_start, copy_end in matcher.get_opcodes():
        if copy_start == len(wanted):
            break  # Only file lines after the copy's last line remain.
        if tag == "equal":
            passage.extend(range(file_start, file_end))
            continue
        leading = not passage and copy_start == 0
        # The copy's first lines lead into the lines that follow, so they align from the end.
        block = _aligned_block(
            window[file_start:file_end], wanted[copy_start:copy_end], from_end=leading
        )
        if leading:
            # File lines before the copy's first line precede the passage.
            while block and block[0][1] is None:
                block.pop(0)
        if copy_end == len(wanted):
            # File lines after the copy's last line follow the passage.
            while block and block[-1][1] is None:
                block.pop()
        block = [
            (
                None if file_index is None else file_start + file_index,
                None if copy_index is None else copy_start + copy_index,
            )
            for file_index, copy_index in block
        ]
        pairs.extend(block)
        passage.extend(index for index, _ in block if index is not None)
    if not passage:
        return None
    passage.sort()
    shown = [
        _line_difference(
            None if file_index is None else (begin + file_index + 1, window[file_index]),
            None if copy_index is None else (first + copy_index + 1, wanted[copy_index]),
        )
        for file_index, copy_index in pairs[:limit]
    ]
    return LineDifferences(
        begin + passage[0] + 1, begin + passage[-1] + 1, tuple(shown), len(pairs)
    )


def _aligned_block(
    file_lines: list[str], copy_lines: list[str], *, from_end: bool
) -> list[tuple[int | None, int | None]]:
    """Pair the lines of one differing block in order, similar lines with each other.

    Each entry pairs a file line with a copied line, by their index in the block,
    or holds a line the other side lacks. A block too large to compare line by
    line pairs its lines in order, and its surplus lines stay unpaired.
    """
    if from_end:
        reversed_pairs = _aligned_block(file_lines[::-1], copy_lines[::-1], from_end=False)
        rows, cols = len(file_lines), len(copy_lines)
        return [
            (None if row is None else rows - 1 - row, None if col is None else cols - 1 - col)
            for row, col in reversed(reversed_pairs)
        ]
    rows, cols = len(file_lines), len(copy_lines)
    if rows * cols > _ALIGN_MAX_CELLS:
        paired = min(rows, cols)
        return [
            *((index, index) for index in range(paired)),
            *((index, None) for index in range(paired, rows)),
            *((None, index) for index in range(paired, cols)),
        ]
    similarity = [
        [
            ratio
            if (
                ratio := SequenceMatcher(
                    None,
                    _loose(file_line)[:_ALIGN_LINE_CHARS],
                    _loose(copy_line)[:_ALIGN_LINE_CHARS],
                    autojunk=False,
                ).ratio()
            )
            >= _SIMILAR_LINE
            else _UNLIKE_PAIR
            for copy_line in copy_lines
        ]
        for file_line in file_lines
    ]
    # best[row][col]: the highest total similarity of the lines from row and col on.
    best = [[0.0] * (cols + 1) for _ in range(rows + 1)]
    for row in range(rows - 1, -1, -1):
        for col in range(cols - 1, -1, -1):
            best[row][col] = max(
                similarity[row][col] + best[row + 1][col + 1],
                best[row + 1][col],
                best[row][col + 1],
            )
    pairs: list[tuple[int | None, int | None]] = []
    row = col = 0
    while row < rows and col < cols:
        if best[row][col] == similarity[row][col] + best[row + 1][col + 1]:
            pairs.append((row, col))
            row, col = row + 1, col + 1
        elif best[row][col] == best[row + 1][col]:
            pairs.append((row, None))
            row += 1
        else:
            pairs.append((None, col))
            col += 1
    pairs.extend((index, None) for index in range(row, rows))
    pairs.extend((None, index) for index in range(col, cols))
    return pairs


def _line_difference(
    file_line: tuple[int, str] | None, copy_line: tuple[int, str] | None
) -> dict[str, int | str | bool | None]:
    """Describe one differing line: numbered windows of both sides that hold it."""
    if file_line is not None and copy_line is not None:
        file_position, copy_position = _difference_positions(file_line[1], copy_line[1])
    else:
        file_position = copy_position = 0
    difference: dict[str, int | str | bool | None] = {
        "line": None,
        "copy_line": None,
        "file": None,
        "copy": None,
    }
    truncated = False
    for key, side, position in (
        ("line", file_line, file_position),
        ("copy_line", copy_line, copy_position),
    ):
        if side is None:
            continue
        number, text = side
        _start, window = _difference_window(text, position)
        difference[key] = number
        difference["file" if key == "line" else "copy"] = window
        truncated = truncated or len(window) < len(text)
    difference["truncated"] = truncated
    return difference


# A line break difference quotes at most this many characters on each side.
_BREAK_SIDE_CHARACTERS = 60
# A copy's first line is tried at most at this many places inside the file line.
_BREAK_PLACES = 20


def _break_difference(
    actual: str,
    wanted: str,
    following: tuple[str | None, str | None],
    *,
    starts: bool,
    part: bool = False,
) -> dict[str, str]:
    """Describe a first difference that is a line break in another place, if it is one.

    ``following`` holds the file's and the copy's next lines. ``break`` is
    ``file`` when the file line ends where the copy line goes on with what the
    next file line starts with, ``copy`` for the reverse, and ``start`` when the
    copy's first line, which ``starts`` a line, is the end of the file line.
    ``before`` is the text before the break, ``after`` the text the other side
    continues with. A copy's first line that is ``part`` of a line can start
    inside the file line.
    """
    if part:
        start = wanted.split()[:1]
        places = re.finditer(re.escape(start[0]), actual) if start else iter(())
        for _, found in zip(range(_BREAK_PLACES), places, strict=False):
            difference = _break_difference(actual[found.start() :], wanted, following, starts=False)
            if difference:
                return difference
        return {}
    held = list(re.finditer(r"\S+", actual))
    copied = list(re.finditer(r"\S+", wanted))
    words = [found[0] for found in held]
    copy_words = [found[0] for found in copied]

    def side(text: str, *, end: bool) -> str:
        text = text.strip()
        if len(text) <= _BREAK_SIDE_CHARACTERS:
            return text
        if end:
            return "..." + text[-_BREAK_SIDE_CHARACTERS:]
        return text[:_BREAK_SIDE_CHARACTERS] + "..."

    def first_word(line: str | None) -> str | None:
        found = (line or "").split()
        return found[0] if found else None

    def holds(held_word: str | None, copied_word: str | None) -> bool:
        # The copy can end inside a word.
        return (
            held_word is not None and copied_word is not None and held_word.startswith(copied_word)
        )

    count, copy_count = len(words), len(copy_words)
    if (
        0 < count < copy_count
        and copy_words[:count] == words
        and holds(first_word(following[0]), copy_words[count])
    ):
        return {
            "break": "file",
            "before": side(actual, end=True),
            "after": side(wanted[copied[count].start() :], end=False),
        }
    if (
        0 < copy_count < count
        and words[:copy_count] == copy_words
        and holds(words[copy_count], first_word(following[1]))
    ):
        return {
            "break": "copy",
            "before": side(wanted, end=True),
            "after": side(actual[held[copy_count].start() :], end=False),
        }
    if starts and 0 < copy_count < count and words[-copy_count:] == copy_words:
        return {
            "break": "start",
            "before": side(actual[: held[count - copy_count].start()], end=True),
            "after": side(wanted, end=False),
        }
    return {}


def _loose(text: str) -> str:
    return " ".join(text.split())


def _difference_positions(actual: str, wanted: str) -> tuple[int, int]:
    """Locate the first different token, ignoring earlier spacing-only differences."""
    actual_words = list(re.finditer(r"\S+", actual))
    wanted_words = list(re.finditer(r"\S+", wanted))
    for file_word, copy_word in zip(actual_words, wanted_words, strict=False):
        if file_word[0] != copy_word[0]:
            shared = len(commonprefix((file_word[0], copy_word[0])))
            return file_word.start() + shared, copy_word.start() + shared
    shared_words = min(len(actual_words), len(wanted_words))
    return (
        actual_words[shared_words].start() if shared_words < len(actual_words) else len(actual),
        wanted_words[shared_words].start() if shared_words < len(wanted_words) else len(wanted),
    )


def _difference_window(text: str, position: int) -> tuple[int, str]:
    """Keep the actual mismatch in a bounded window, including a missing suffix."""
    if len(text) <= 240:
        return 1, text
    start = max(0, position - 120)
    return start + 1, text[start : start + 240]


def _aligned_start(file_lines: list[str], start: int, wanted_lines: list[str], first: int) -> int:
    """Return the file line where patch line ``first`` belongs.

    A candidate window starts where its best-matching lines put it, so every line
    the copy added or dropped before them shifts it. The first patch line the
    file holds near the window fixes the alignment instead; the lines before it
    belong directly above it. Lines without a word, such as a closing quote or
    bracket, occur too often to fix it. When the file holds none of the patch
    lines, patch line ``first`` belongs at the nearby file line most like its
    start: a reworded copy resembles its line, and a copy that joins lines
    starts like the first of them. Words decide before characters: lines that
    differ in their names can share most of their signs (``_EQ_OPS = {...}`` and
    ``_RANGE_OPS = {...}``).
    """
    span = len(wanted_lines) - first
    if span == 1:
        # One copied line drops or adds no line above it; the candidate places it.
        return start
    for position in range(first, len(wanted_lines)):
        text = _loose(wanted_lines[position])
        if not re.search(r"\w", text):
            continue
        expected = start - 1 + position - first
        near = [
            index
            for index in range(max(0, expected - span), min(len(file_lines), expected + span + 1))
            if _loose(file_lines[index]) == text
        ]
        if near:
            index = min(near, key=lambda index: (abs(index - expected), index))
            return max(1, index + 1 - (position - first))
    text = _loose(wanted_lines[first])
    words = re.findall(r"\w+", text)
    expected = start - 1
    scores = []
    for index in range(max(0, expected - span), min(len(file_lines), expected + span + 1)):
        line = _loose(file_lines[index])
        held = re.findall(r"\w+", line)
        if len(line) < 4 or not held:
            continue
        # The copy's start against the whole line, by words and then by characters.
        shared = SequenceMatcher(None, words[: len(held)], held, autojunk=False).ratio()
        similarity = SequenceMatcher(None, text[: len(line)], line).ratio()
        if max(shared, similarity) >= _SIMILAR_LINE:
            scores.append((shared, similarity, -abs(index - expected), index))
    return max(scores)[3] + 1 if scores else start


def _normalize_newlines(text: str) -> str:
    """Normalize every recognized line ending to LF for tolerant matching."""
    normalized = text
    for ending in _LINE_ENDINGS:
        if ending != "\n":
            normalized = normalized.replace(ending, "\n")
    return normalized


def _normalize_replacement_newlines(text: str) -> str:
    """Normalize CRLF and CR to LF; other separators stay literal line content.

    Models normally author multiline replacement text with LF regardless of the
    target file's newline style, which the replacement then adopts.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def dominant_line_ending(content: str) -> str | None:
    """Return the line ending ``content`` uses most, or ``None`` when it has none.

    Text whose lines end in different ways keeps the style most of them have,
    so a stray CRLF or lone CR does not set the ending of every written line.
    A tie goes to CRLF, then LF.
    """
    crlf = content.count("\r\n")
    counts = {
        "\r\n": crlf,
        "\n": content.count("\n") - crlf,
        "\r": content.count("\r") - crlf,
    }
    ending = max(_LINE_ENDINGS, key=counts.__getitem__)
    return ending if counts[ending] else None


def _to_line_ending(text_lf: str, file_ending: str | None) -> str:
    """Convert LF-normalized text to the target line ending."""
    if file_ending is None or file_ending == "\n":
        return text_lf
    return text_lf.replace("\n", file_ending)


def _inside_crlf(content: str, offset: int) -> bool:
    """Whether ``offset`` falls between the CR and the LF of one line break."""
    return 0 < offset < len(content) and content[offset - 1 : offset + 1] == "\r\n"


def _on_line_boundaries(content: str, start: int, end: int) -> bool:
    """Whether ``content[start:end]`` starts at a line start and ends at a line end."""
    return (start == 0 or content[start - 1] in "\n\r") and (
        end == len(content) or content[end] in "\n\r"
    )


def _line_number_at(content: str, offset: int) -> int:
    """1-based line number at ``offset``, counting every read-rendered break."""
    return len(_LINE_BREAK_RE.findall(content[:offset])) + 1


def _line_starts(content: str) -> list[int]:
    """The offset of each read-rendered line, for many lookups with ``bisect_right``."""
    return [0, *(found.end() for found in _LINE_BREAK_RE.finditer(content))]


def _normalize_with_spans(
    text: str, *, typographic: bool = False
) -> tuple[str, list[tuple[int, int]]]:
    """Normalize newlines + Unicode and record each normalized char's origin span.

    ``spans[k]`` is the ``(start, end)`` range in ``text`` that produced the k-th
    normalized character, so a match found in the normalized string maps back to
    the exact original characters. Expanded glyphs share one origin span for
    every normalized character, so the character and span lists stay aligned.
    """
    # Only line endings and mapped glyphs change; the runs between them are
    # copied with one-character spans built in C, not per character in Python.
    mapping = _TYPOGRAPHIC_NORMALIZATION if typographic else _UNICODE_NORMALIZATION
    special = _TYPOGRAPHIC_SPECIAL_RE if typographic else _UNICODE_SPECIAL_RE
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    for found in special.finditer(text):
        start, end = found.span()
        if start > cursor:
            chars.append(text[cursor:start])
            spans.extend(zip(range(cursor, start), range(cursor + 1, start + 1), strict=True))
        glyph = found.group()
        folded = "\n" if glyph[0] == "\r" else mapping[glyph]
        chars.append(folded)
        spans.extend([(start, end)] * len(folded))
        cursor = end
    if cursor < len(text):
        chars.append(text[cursor:])
        spans.extend(zip(range(cursor, len(text)), range(cursor + 1, len(text) + 1), strict=True))
    return "".join(chars), spans


def _normalize_text(text: str) -> str:
    return _normalize_newlines(text).translate(_UNICODE_TRANSLATION)


def _folded(text: str, *, typographic: bool) -> str:
    """Return the text ``_normalize_with_spans`` produces, without its span map."""
    mapping = _TYPOGRAPHIC_NORMALIZATION if typographic else _UNICODE_NORMALIZATION
    glyphs = _TYPOGRAPHIC_GLYPH_RE if typographic else _UNICODE_GLYPH_RE
    return glyphs.sub(lambda found: mapping[found.group()], _normalize_newlines(text))


def _found_with_crlf(content: str, pattern: str, *, typographic: bool) -> list[tuple[int, int]]:
    """Return the normalized matches of ``pattern`` as a literal search finds them.

    In a file with CRLF line breaks, the old text with CRLF line breaks finds
    its occurrences without mapping every character of the file. Each one is
    also a normalized match; when the normalized text holds no more matches
    than that, they are all of them. An empty result leaves the decision to
    the full normalized search.
    """
    if "\r\n" not in content:
        return []
    found = _find_all(content, _normalize_newlines(pattern).replace("\n", "\r\n"))
    if not found:
        return []
    folded = _find_all(
        _folded(content, typographic=typographic), _folded(pattern, typographic=typographic)
    )
    return found if len(folded) == len(found) else []


def _match_exact(content: str, pattern: str) -> list[tuple[int, int]]:
    return _find_all(content, pattern)


def _match_normalized(
    content: str, pattern: str, *, typographic: bool = False, whole_lines: bool = False
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
    if not matches and typographic:
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
_WORD_RE = re.compile(r"\w+")
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
    one. At least one line break must fall elsewhere, else a precise strategy
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
        head = r"^[ \t]*" if lead else ""
    trail_breaks = trail.count("\n")
    if trail_breaks:
        tail = r"[ \t]*\n" * trail_breaks + (r"[ \t]*" if trail[-1] != "\n" else "")
    else:
        tail = r"[ \t]*$" if trail else ""
    pattern = re.compile(f"(?=({head}({body}){tail}))", re.MULTILINE)
    wanted = [run.count("\n") for run in runs]
    found = [
        match.span(1)
        for match in pattern.finditer(_folded(content, typographic=True))
        # A copy whose line breaks are all the file's is the precise strategies' concern.
        if [run.count("\n") for run in _SPACE_RUN_RE.findall(match[2])] != wanted
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


def rewrapped_place(content: str, old: str) -> tuple[int, int] | None:
    """Return the lines that hold every word of ``old`` in order, if exactly one place does.

    Any whitespace stands for any other between the words; leading and trailing
    whitespace counts for nothing. A failure report shows this place: there
    ``old`` differs from the file only in its whitespace.
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
    return (
        _line_number_at(content, spans[start][0]),
        _line_number_at(content, spans[end - 1][0]),
    )


def _find_all(haystack: str, needle: str) -> list[tuple[int, int]]:
    """Return every occurrence of ``needle``, including overlapping ones."""
    if not needle:
        return []
    matches: list[tuple[int, int]] = []
    position = haystack.find(needle)
    while position >= 0:
        matches.append((position, position + len(needle)))
        position = haystack.find(needle, position + 1)
    return matches


def _leftmost_non_overlapping(matches: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Select sorted spans left to right so ``replace_all`` cannot self-corrupt."""
    selected: list[tuple[int, int]] = []
    for start, end in matches:
        if not selected or start >= selected[-1][1]:
            selected.append((start, end))
    return selected


def _apply_replacements(
    content: str,
    matches: list[tuple[int, int]],
    replacement_text: str,
    *,
    reindent: bool,
    old_string_lf: str,
    file_ending: str | None,
    lines_correspond: bool = True,
) -> tuple[str, tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]:
    # The spans do not overlap: the result is built in one pass, so many
    # replacements cost linear time instead of one copy of the content each.
    parts: list[str] = []
    before_spans: list[tuple[int, int]] = []
    after_spans: list[tuple[int, int]] = []
    offset_shift = 0
    cursor = 0
    # Finding the file's indentation unit reads every line; only a replacement
    # whose indentation changes asks for it.
    file_unit = cache(lambda: _indent_unit([_meaningful_indents(content)]))
    for start, end in sorted(matches):
        if reindent:
            replacement_lf = _reindent_replacement(
                content[start:end], old_string_lf, replacement_text, file_unit, lines_correspond
            )
        else:
            replacement_lf = replacement_text
        replacement = _to_line_ending(replacement_lf, file_ending)
        after_start = start + offset_shift
        parts += (content[cursor:start], replacement)
        cursor = end
        before_spans.append((start, end))
        after_spans.append((after_start, after_start + len(replacement)))
        offset_shift += len(replacement) - (end - start)
    parts.append(content[cursor:])
    return "".join(parts), tuple(before_spans), tuple(after_spans)


def _leading_whitespace(line: str) -> str:
    index = 0
    while index < len(line) and line[index] in (" ", "\t"):
        index += 1
    return line[:index]


def _split_lines_preserving_endings(text: str) -> list[tuple[str, str]]:
    """Split read-visible lines without discarding their exact separators."""
    lines: list[tuple[str, str]] = []
    start = 0
    for match in _LINE_BREAK_RE.finditer(text):
        lines.append((text[start : match.start()], match.group(0)))
        start = match.end()
    lines.append((text[start:], ""))
    return lines


def _reindent_replacement(
    file_region: str,
    old_string_lf: str,
    replacement_text: str,
    file_unit: Callable[[], str | None],
    lines_correspond: bool = True,
) -> str:
    """Rewrite the replacement's indentation in the file's own indentation style.

    A whitespace-tolerant match succeeds when the model's indentation differs
    from the file's: 2 spaces against 4, spaces against tabs, or a dropped outer
    level. Writing the replacement verbatim would corrupt indentation, so each
    replacement line gets the file indent that the matched lines show for the
    same model indent. An indent the matched lines do not show, such as a new
    deeper line, is converted level by level between the model's and the file's
    indentation units; when no consistent conversion exists, the model's base
    indent is anchored onto the file's, keeping the relative nesting the model
    intended. ``file_unit`` gives the file's indentation unit when a conversion
    needs it. Without ``lines_correspond``, as when the line breaks fall in other
    places, only the first lines pair up.
    """
    if not replacement_text:
        return replacement_text

    old_indents = _meaningful_indents(old_string_lf)
    file_indents = _meaningful_indents(file_region)
    if not old_indents or not file_indents:
        return replacement_text
    pairs = (
        list(zip(old_indents, file_indents, strict=True))
        if lines_correspond and len(old_indents) == len(file_indents)
        else [(old_indents[0], file_indents[0])]
    )
    # Most indented lines as the file has them: the model wrote in the file's
    # indentation, and the other lines are typos, not a different style.
    indented = [(model, file) for model, file in pairs if model or file]
    if 2 * sum(model == file for model, file in indented) > len(indented):
        return replacement_text
    seen: dict[str, str] = {}
    for model_indent, file_indent in pairs:
        seen.setdefault(model_indent, file_indent)
    if all(model_indent == file_indent for model_indent, file_indent in seen.items()):
        return replacement_text

    replacement_lines = _split_lines_preserving_endings(replacement_text)
    replacement_indents = [
        _leading_whitespace(line) for line, _ending in replacement_lines if line.strip()
    ]
    convert = _level_converter(pairs, (old_indents, replacement_indents), file_unit())
    base_model, base_file = pairs[0]

    out_parts: list[str] = []
    for line, ending in replacement_lines:
        if not line.strip():
            out_parts.append(line + ending)
            continue
        indent = _leading_whitespace(line)
        body = line[len(indent) :]
        mapped = seen.get(indent)
        if mapped is None and convert is not None:
            mapped = convert(indent)
        if mapped is None:
            rest = indent[len(base_model) :] if indent.startswith(base_model) else ""
            mapped = base_file + rest
        out_parts.append(mapped + body + ending)
    return "".join(out_parts)


def _meaningful_indents(text: str) -> list[str]:
    return [_leading_whitespace(line) for line in _LINE_BREAK_RE.split(text) if line.strip()]


def _indent_unit(sequences: Iterable[list[str]]) -> str | None:
    """Return the indentation unit these indent sequences show: a tab or N spaces.

    Tabs win when most indented lines start with one. Otherwise the unit is the
    most common change between consecutive space indents, the smallest on ties.
    ``None`` when the text shows no indentation step.
    """
    tab_lines = space_lines = 0
    steps: Counter[int] = Counter()
    for indents in sequences:
        previous: int | None = None
        for indent in indents:
            if indent.startswith("\t"):
                tab_lines += 1
            elif indent:
                space_lines += 1
            if _only(indent, " "):
                if previous is not None and len(indent) != previous:
                    steps[abs(len(indent) - previous)] += 1
                previous = len(indent)
            else:
                previous = None
    if tab_lines > space_lines:
        return "\t"
    if not steps:
        return None
    return " " * max(sorted(steps), key=steps.__getitem__)


# Spaces per indentation level to try, most common first.
_LEVEL_WIDTHS = (4, 2, 8, 3)


def _level_converter(
    pairs: list[tuple[str, str]],
    model_sequences: tuple[list[str], ...],
    file_unit: str | None,
) -> Callable[[str], str | None] | None:
    """Convert model indents level by level into the file's indentation unit.

    An indent is whole levels of its unit plus trailing alignment spaces. The
    units are the ones each side shows; a side that shows no indentation step
    gets the candidate unit that explains the matched pairs, preferring the
    same level on both sides. The matched pairs that convert with unchanged
    alignment must agree on one constant level offset (a dropped outer level);
    pairs whose alignment cannot be told apart from levels are left out.
    ``None`` when no pair of units explains the matched lines.
    """
    model_unit = _indent_unit(model_sequences)
    model_units = _unit_candidates(model_unit, [model for model, _file in pairs])
    file_units = _unit_candidates(file_unit, [file for _model, file in pairs])
    best: tuple[tuple[int, bool, bool, int, int], str, str, int] | None = None
    for model_rank, model in enumerate(model_units):
        for file_rank, file in enumerate(file_units):
            fit = _level_offset(pairs, model, file)
            if fit is None:
                continue
            offset, explained = fit
            score = (
                -explained,
                model != model_unit,
                file != file_unit,
                abs(offset),
                model_rank + file_rank,
            )
            if best is None or score < best[0]:
                best = (score, model, file, offset)
    if best is None:
        return None
    _score, model, file, offset = best

    def convert(indent: str) -> str | None:
        split = _split_indent(indent, model)
        if split is None or split[0] + offset < 0:
            return None
        levels, alignment = split
        return file * (levels + offset) + " " * alignment

    return convert


def _unit_candidates(shown: str | None, indents: list[str]) -> list[str]:
    candidates = [shown] if shown else []
    if any("\t" in indent for indent in indents):
        candidates.append("\t")
    widths = [len(indent) for indent in indents if indent and _only(indent, " ")]
    if widths:
        candidates.append(" " * math.gcd(*widths))
    candidates.extend(" " * width for width in _LEVEL_WIDTHS)
    # One space is alignment, not a level: as a unit it multiplies every indent.
    return [unit for unit in dict.fromkeys(candidates) if unit != " "]


def _level_offset(
    pairs: list[tuple[str, str]], model_unit: str, file_unit: str
) -> tuple[int, int] | None:
    """Return the level offset the pairs agree on and how many pairs it explains."""
    offsets: set[int] = set()
    explained = 0
    for model_indent, file_indent in pairs:
        model_split = _split_indent(model_indent, model_unit)
        file_split = _split_indent(file_indent, file_unit)
        if model_split is None or file_split is None:
            return None
        if model_split[1] != file_split[1]:
            continue
        offsets.add(file_split[0] - model_split[0])
        explained += 1
    return (offsets.pop(), explained) if len(offsets) == 1 else None


def _split_indent(indent: str, unit: str) -> tuple[int, int] | None:
    """Split ``indent`` into whole ``unit`` levels and trailing alignment spaces."""
    if unit == "\t":
        levels = len(indent) - len(indent.lstrip("\t"))
        alignment = indent[levels:]
        return (levels, len(alignment)) if _only(alignment, " ") else None
    if not _only(indent, " "):
        return None
    return divmod(len(indent), len(unit))


def _only(indent: str, character: str) -> bool:
    """Whether ``indent`` is empty or consists of ``character`` alone."""
    return not indent.strip(character)


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
    "dominant_line_ending",
    "find_closest_candidates",
    "line_differences",
    "replace_fuzzy",
    "preserve_typography",
]
