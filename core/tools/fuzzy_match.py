"""Fuzzy find-and-replace for the edit tool.

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
   re-indented to the file's actual indentation, so a whitespace-only match never
   corrupts indentation.
4. ``whitespace_normalized`` — collapse horizontal space/tab runs while preserving
   line boundaries. The replacement is re-indented like a line-trimmed match.

All non-exact strategies search a normalized copy of the content and map the
match back to the original characters through a per-character span map, so CRLF line
endings and the exact original characters are always preserved. Deliberately
excluded: similarity / anchor matching (replacing text that is merely *similar*).
For a destructive operation, failing so the model retries with a better target is
safer than silently editing the wrong block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from heapq import heappush, heapreplace

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

# Every line-ending flavor the read tool renders as a separate line (mirrors
# read.py's _LINE_BREAK_PATTERN). Detection order matters: CRLF first, then LF
# before CR (write.py's _detect_file_line_ending prefers the same way for
# mixed files), then the exotic flavors.
_LINE_ENDINGS = (
    "\r\n",
    "\n",
    "\r",
    "\v",
    "\f",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
)
_EXOTIC_LINE_ENDINGS = _LINE_ENDINGS[3:]
_LINE_BREAK_RE = re.compile(r"\r\n|[\n\v\f\x1c-\x1e\x85\u2028\u2029\r]")


@dataclass(frozen=True)
class FuzzyReplacement:
    """A successful fuzzy replacement applied to the original content."""

    new_content: str
    first_changed_line: int
    replacements: int
    strategy: str


@dataclass(frozen=True)
class AmbiguousFuzzyMatch:
    """The winning strategy matched more than once without ``replace_all``."""

    occurrences: int
    line_numbers: list[int]


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
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    """Find ``old_string`` in ``content`` via the strategy chain and replace it.

    Returns a :class:`FuzzyReplacement` on success, an :class:`AmbiguousFuzzyMatch`
    when the winning strategy matched more than once without ``replace_all``, or
    ``None`` when no strategy matched.
    """
    new_lf = _normalize_newlines(new_string)
    old_lf = _normalize_newlines(old_string)
    file_ending = _detect_line_ending(content)

    for name, matcher, reindent in _STRATEGIES:
        matches = matcher(content, old_string)
        if not matches:
            continue
        if len(matches) > 1 and not replace_all:
            line_numbers = [_line_number_at(content, start) for start, _ in matches]
            return AmbiguousFuzzyMatch(len(matches), line_numbers)

        selected = matches if replace_all else matches[:1]
        new_content = _apply_replacements(
            content,
            selected,
            new_lf,
            reindent=reindent,
            old_string_lf=old_lf,
            file_ending=file_ending,
        )
        first_line = _line_number_at(content, min(start for start, _ in selected))
        return FuzzyReplacement(new_content, first_line, len(selected), name)

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

    pattern_lines = pattern.splitlines()
    while pattern_lines and not pattern_lines[0].strip():
        pattern_lines.pop(0)
    while pattern_lines and not pattern_lines[-1].strip():
        pattern_lines.pop()
    if not pattern_lines:
        return []

    content_lines = content.splitlines()
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


def _detect_line_ending(content: str) -> str | None:
    """Return the line ending used in ``content``, or ``None`` when absent."""
    for ending in _LINE_ENDINGS:
        if ending in content:
            return ending
    return None


def _to_line_ending(text_lf: str, file_ending: str | None) -> str:
    """Convert LF-normalized text to the target line ending."""
    if file_ending in (None, "\n"):
        return text_lf
    return text_lf.replace("\n", file_ending)


def _line_number_at(content: str, offset: int) -> int:
    """1-based line number at ``offset``, counting every read-rendered break."""
    return len(_LINE_BREAK_RE.findall(content[:offset])) + 1


def _normalize_with_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Normalize newlines + Unicode and record each normalized char's origin span.

    ``spans[k]`` is the ``(start, end)`` range in ``text`` that produced the k-th
    normalized character, so a match found in the normalized string maps back to
    the exact original characters. Every mapping is K-original-chars -> 1-normalized
    (CRLF -> LF is 2->1; everything else is 1->1), so the lists stay aligned.
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
        if char in _EXOTIC_LINE_ENDINGS:
            chars.append("\n")
            spans.append((index, index + 1))
            index += 1
            continue
        chars.append(_UNICODE_NORMALIZATION.get(char, char))
        spans.append((index, index + 1))
        index += 1

    return "".join(chars), spans


def _normalize_text(text: str) -> str:
    return _normalize_with_spans(text)[0]


def _match_exact(content: str, pattern: str) -> list[tuple[int, int]]:
    return _find_non_overlapping(content, pattern)


def _match_normalized(content: str, pattern: str) -> list[tuple[int, int]]:
    normalized_pattern = _normalize_text(pattern)
    if not normalized_pattern:
        return []
    normalized_content, spans = _normalize_with_spans(content)

    matches: list[tuple[int, int]] = []
    pattern_length = len(normalized_pattern)
    start = 0
    while True:
        position = normalized_content.find(normalized_pattern, start)
        if position < 0:
            break
        matches.append((spans[position][0], spans[position + pattern_length - 1][1]))
        start = position + pattern_length
    return matches


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
    index = 0
    last_start = len(trimmed_content) - window
    while index <= last_start:
        if trimmed_content[index : index + window] == trimmed_pattern:
            norm_start = line_offsets[index]
            last_line = index + window - 1
            norm_end = line_offsets[last_line] + len(content_lines[last_line])
            if norm_start < len(spans) and norm_end > norm_start:
                matches.append((spans[norm_start][0], spans[norm_end - 1][1]))
                index += window  # non-overlapping, so replace_all cannot self-corrupt
                continue
        index += 1
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
        start = position + pattern_length
    return matches


def _find_non_overlapping(haystack: str, needle: str) -> list[tuple[int, int]]:
    if not needle:
        return []
    matches: list[tuple[int, int]] = []
    start = 0
    while True:
        position = haystack.find(needle, start)
        if position < 0:
            break
        matches.append((position, position + len(needle)))
        start = position + len(needle)
    return matches


def _apply_replacements(
    content: str,
    matches: list[tuple[int, int]],
    new_string_lf: str,
    *,
    reindent: bool,
    old_string_lf: str,
    file_ending: str | None,
) -> str:
    result = content
    # Splice from the end so earlier spans keep their offsets.
    for start, end in sorted(matches, reverse=True):
        if reindent:
            replacement_lf = _reindent_replacement(content[start:end], old_string_lf, new_string_lf)
        else:
            replacement_lf = new_string_lf
        result = result[:start] + _to_line_ending(replacement_lf, file_ending) + result[end:]
    return result


def _leading_whitespace(line: str) -> str:
    index = 0
    while index < len(line) and line[index] in (" ", "\t"):
        index += 1
    return line[:index]


def _first_meaningful_line(text: str) -> str | None:
    for line in text.split("\n"):
        if line.strip():
            return line
    return None


def _reindent_replacement(file_region: str, old_string_lf: str, new_string_lf: str) -> str:
    """Shift ``new_string`` so its base indent matches the file's actual indent.

    A line-trimmed match can succeed when the model's indentation differs from the
    file's (e.g. 2-space args vs a 4-space file). Writing the replacement verbatim
    would then corrupt indentation, so anchor the model's base indent onto the
    file's while preserving the relative nesting the model intended.
    """
    if not new_string_lf:
        return new_string_lf

    old_first = _first_meaningful_line(old_string_lf)
    file_first = _first_meaningful_line(file_region)
    if old_first is None or file_first is None:
        return new_string_lf

    old_indent = _leading_whitespace(old_first)
    file_indent = _leading_whitespace(file_first)
    if old_indent == file_indent:
        return new_string_lf

    out_lines: list[str] = []
    for line in new_string_lf.split("\n"):
        if not line.strip():
            out_lines.append(line)
            continue
        if _leading_whitespace(line).startswith(old_indent):
            out_lines.append(file_indent + line[len(old_indent) :])
        else:
            out_lines.append(file_indent + line.lstrip(" \t"))
    return "\n".join(out_lines)


# (name, matcher, reindent-replacement) in increasing tolerance. Only the
# line-level strategy needs re-indentation; the character-level ones match the
# file's real whitespace already.
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
]
