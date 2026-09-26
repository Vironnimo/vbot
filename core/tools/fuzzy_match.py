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

1. ``exact`` — literal substring match.
2. ``normalized`` — match after collapsing CR/CRLF to LF and mapping a few
   visually-equivalent Unicode characters (curly quotes, non-breaking space,
   en-dash) to ASCII, on both sides. Character-level, so it also matches a
   fragment within a line.
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
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from heapq import heappush, heapreplace

from core.tools.arguments import TEXT_LINE_BREAK, split_text_lines

_CANDIDATE_ANCHOR_COUNT = 3
_CANDIDATE_ANCHOR_POOL_SIZE = 20
_CANDIDATE_SCAN_LINE_LIMIT = 50_000
_CANDIDATE_SCORE_LINE_MAX_CHARS = 240
_CANDIDATE_SCORE_MAX_CHARS = 4_000
_CANDIDATE_MIN_SIMILARITY = 0.60
_CANDIDATE_RESULT_LIMIT = 3
_CANDIDATE_OUTPUT_MAX_LINES = 8
_CANDIDATE_OUTPUT_MAX_CHARS = 1_200

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
    """The winning strategy matched more than once without ``replace_all``."""

    occurrences: int
    line_numbers: list[int]
    character_numbers: list[int]


@dataclass(frozen=True)
class ClosestFuzzyCandidate:
    """A bounded raw excerpt similar to an unmatched edit locator."""

    line_number: int
    text: str
    truncated: bool


def replace_fuzzy(
    content: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool,
    whole_lines: bool = False,
    at_eof: bool = False,
    typographic: bool = False,
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    """Find ``old_string`` in ``content`` via the strategy chain and replace it.

    Returns a :class:`FuzzyReplacement` on success, an :class:`AmbiguousFuzzyMatch`
    when the winning strategy matched more than once without ``replace_all``, or
    ``None`` when no strategy matched.

    Patch callers can require whole-line spans or anchor a hunk at EOF.
    """
    replacement_text = _normalize_replacement_newlines(new_string)
    old_lf = _normalize_newlines(old_string)
    file_ending = _detect_line_ending(content)

    for name, matcher, reindent in _STRATEGIES:
        matches = (
            _match_normalized(content, old_string, typographic=True, whole_lines=whole_lines)
            if typographic and name == "normalized"
            else matcher(content, old_string)
        )
        if whole_lines:
            matches = [
                (start, end)
                for start, end in matches
                if (start == 0 or content[start - 1] in "\n\r")
                and (end == len(content) or content[end] in "\n\r")
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
        if len(matches) > 1 and not replace_all:
            locations = [_line_and_character_at(content, start) for start, _ in matches]
            return AmbiguousFuzzyMatch(
                len(matches),
                [line for line, _ in locations],
                [character for _, character in locations],
            )

        selected = _leftmost_non_overlapping(matches) if replace_all else matches[:1]
        new_content, before_spans, after_spans = _apply_replacements(
            content,
            selected,
            replacement_text,
            reindent=reindent or (typographic and name == "normalized"),
            old_string_lf=old_lf,
            file_ending=file_ending,
        )
        first_line = _line_number_at(content, min(start for start, _ in selected))
        last_line = max(
            _line_number_at(new_content, max(start, end - 1)) for start, end in after_spans
        )
        return FuzzyReplacement(
            new_content,
            first_line,
            last_line,
            len(selected),
            name,
            before_spans,
            after_spans,
        )

    return None


def _candidate_normalize_line(line: str) -> str:
    normalized = _collapse_horizontal_whitespace_with_spans(line)[0].strip()
    return normalized[:_CANDIDATE_SCORE_LINE_MAX_CHARS]


def _top_anchor_starts(
    content_lines: list[str], anchor_index: int, anchor: str, window_size: int
) -> list[int]:
    """Return a bounded pool of block starts whose aligned line resembles an anchor."""
    heap: list[tuple[float, int]] = []
    scan_limit = min(len(content_lines), _CANDIDATE_SCAN_LINE_LIMIT)
    for content_index in range(scan_limit):
        start = content_index - anchor_index
        if start < 0 or start + window_size > len(content_lines):
            continue
        candidate_line = _candidate_normalize_line(content_lines[content_index])
        if not candidate_line:
            continue
        score = SequenceMatcher(None, anchor, candidate_line).ratio()
        item = (score, -start)
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

    possible_starts: set[int] = set()
    for _, _, anchor_index, anchor in anchors:
        possible_starts.update(_top_anchor_starts(content_lines, anchor_index, anchor, window_size))

    pattern_score_text = "\n".join(normalized_pattern_lines)[:_CANDIDATE_SCORE_MAX_CHARS]
    scored: list[tuple[float, int]] = []
    for start in possible_starts:
        candidate_score_text = "\n".join(
            _candidate_normalize_line(line) for line in content_lines[start : start + window_size]
        )[:_CANDIDATE_SCORE_MAX_CHARS]
        similarity = SequenceMatcher(None, pattern_score_text, candidate_score_text).ratio()
        if similarity >= _CANDIDATE_MIN_SIMILARITY:
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


def _detect_line_ending(content: str) -> str | None:
    """Return the line ending used in ``content``, or ``None`` when absent."""
    for ending in _LINE_ENDINGS:
        if ending in content:
            return ending
    return None


def _to_line_ending(text_lf: str, file_ending: str | None) -> str:
    """Convert LF-normalized text to the target line ending."""
    if file_ending is None or file_ending == "\n":
        return text_lf
    return text_lf.replace("\n", file_ending)


def _line_number_at(content: str, offset: int) -> int:
    """1-based line number at ``offset``, counting every read-rendered break."""
    return len(_LINE_BREAK_RE.findall(content[:offset])) + 1


def _line_and_character_at(content: str, offset: int) -> tuple[int, int]:
    """Return the 1-based read-style line and character at ``offset``."""
    breaks = list(_LINE_BREAK_RE.finditer(content[:offset]))
    line_start = breaks[-1].end() if breaks else 0
    return len(breaks) + 1, offset - line_start + 1


def _normalize_with_spans(
    text: str, *, typographic: bool = False
) -> tuple[str, list[tuple[int, int]]]:
    """Normalize newlines + Unicode and record each normalized char's origin span.

    ``spans[k]`` is the ``(start, end)`` range in ``text`` that produced the k-th
    normalized character, so a match found in the normalized string maps back to
    the exact original characters. Expanded glyphs share one origin span for
    every normalized character, so the character and span lists stay aligned.
    """
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    length = len(text)

    while index < length:
        char = text[index]
        if char == "\r" and index + 1 < length and text[index + 1] == "\n":
            chars.append("\n")
            spans.append((index, index + 2))
            index += 2
            continue
        if char == "\r":
            chars.append("\n")
            spans.append((index, index + 1))
            index += 1
            continue
        mapping = _TYPOGRAPHIC_NORMALIZATION if typographic else _UNICODE_NORMALIZATION
        folded = mapping.get(char, char)
        chars.extend(folded)
        spans.extend([(index, index + 1)] * len(folded))
        index += 1

    return "".join(chars), spans


def _normalize_text(text: str) -> str:
    return _normalize_with_spans(text)[0]


def _match_exact(content: str, pattern: str) -> list[tuple[int, int]]:
    return _find_all(content, pattern)


def _match_normalized(
    content: str, pattern: str, *, typographic: bool = False, whole_lines: bool = False
) -> list[tuple[int, int]]:
    normalized_pattern = _normalize_with_spans(pattern, typographic=typographic)[0]
    if not normalized_pattern:
        return []
    normalized_content, spans = _normalize_with_spans(content, typographic=typographic)

    matches: list[tuple[int, int]] = []
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

    for char, source_span in zip(normalized, source_spans, strict=True):
        if char in (" ", "\t"):
            if chars and chars[-1] == " ":
                spans[-1] = (spans[-1][0], source_span[1])
            else:
                chars.append(" ")
                spans.append(source_span)
            continue
        chars.append(char)
        spans.append(source_span)

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
) -> tuple[str, tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]:
    prepared: list[tuple[int, int, str]] = []
    before_spans: list[tuple[int, int]] = []
    after_spans: list[tuple[int, int]] = []
    offset_shift = 0
    file_unit = _indent_unit([_meaningful_indents(content)]) if reindent else None
    for start, end in sorted(matches):
        if reindent:
            replacement_lf = _reindent_replacement(
                content[start:end], old_string_lf, replacement_text, file_unit
            )
        else:
            replacement_lf = replacement_text
        replacement = _to_line_ending(replacement_lf, file_ending)
        after_start = start + offset_shift
        after_end = after_start + len(replacement)
        prepared.append((start, end, replacement))
        before_spans.append((start, end))
        after_spans.append((after_start, after_end))
        offset_shift += len(replacement) - (end - start)

    result = content
    # Splice from the end so earlier spans keep their offsets.
    for start, end, replacement in reversed(prepared):
        result = result[:start] + replacement + result[end:]
    return result, tuple(before_spans), tuple(after_spans)


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
    file_region: str, old_string_lf: str, replacement_text: str, file_unit: str | None
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
    intended.
    """
    if not replacement_text:
        return replacement_text

    old_indents = _meaningful_indents(old_string_lf)
    file_indents = _meaningful_indents(file_region)
    if not old_indents or not file_indents:
        return replacement_text
    pairs = (
        list(zip(old_indents, file_indents, strict=True))
        if len(old_indents) == len(file_indents)
        else [(old_indents[0], file_indents[0])]
    )
    seen: dict[str, str] = {}
    for model_indent, file_indent in pairs:
        seen.setdefault(model_indent, file_indent)
    if all(model_indent == file_indent for model_indent, file_indent in seen.items()):
        return replacement_text

    replacement_lines = _split_lines_preserving_endings(replacement_text)
    replacement_indents = [
        _leading_whitespace(line) for line, _ending in replacement_lines if line.strip()
    ]
    convert = _level_converter(pairs, (old_indents, replacement_indents), file_unit)
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
    return list(dict.fromkeys(candidates))


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
    "find_closest_candidates",
    "replace_fuzzy",
    "preserve_typography",
]
