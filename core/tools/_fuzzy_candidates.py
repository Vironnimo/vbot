"""Bounded candidate search for unmatched edit locators."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter
from difflib import SequenceMatcher
from heapq import heappush, heapreplace

from core.tools._fuzzy_text import (
    _HORIZONTAL_WHITESPACE_RE,
    _WORD_RE,
    _folded,
    _normalize_text,
    _normalize_with_spans,
)
from core.tools._fuzzy_types import ClosestFuzzyCandidate
from core.tools.arguments import split_text_lines

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


# A passage needs this many words of the pattern to be told from text alike by chance.
_PASSAGE_MIN_WORDS = 3
# Passages are compared with at most this many characters of the pattern.
_PASSAGE_MAX_CHARS = 400
# The pattern's rarest words place passages, each at most this many times.
_PASSAGE_ANCHORS = 4
_PASSAGE_ANCHOR_PLACES = 20
# At most this many places are compared with the pattern.
_PASSAGE_COMPARISONS = 40
# Passages scoring this much less than the closest one are not shown beside it.
_PASSAGE_MARGIN = 0.1
# Matching blocks shorter than this are chance; they do not widen a passage.
_PASSAGE_BLOCK_CHARS = 4
_PASSAGE_WORD_RE = re.compile(r"\w{3,}")


def find_closest_passages(content: str, pattern: str) -> list[tuple[int, int]]:
    """Return the spans of ``content`` most like ``pattern``, wherever its lines break.

    For a pattern whole lines do not resemble: part of a longer line, or text
    whose line breaks fall in other places than the file's. Both sides compare
    with every whitespace run as one space. Like ``find_closest_candidates``,
    the spans only rank diagnostic excerpts and never authorize a replacement.
    Each span starts where the pattern's first word would be; the closest
    comes first.
    """
    wanted = " ".join(_folded(pattern, typographic=True).split())[:_PASSAGE_MAX_CHARS]
    if len(_WORD_RE.findall(wanted)) < _PASSAGE_MIN_WORDS:
        return []
    folded, spans = _normalize_with_spans(content, typographic=True)
    tokens = [found.span() for found in re.finditer(r"\S+", folded)]
    if not tokens:
        return []
    text = " ".join(folded[start:end] for start, end in tokens)
    # Where each token starts in ``text``.
    starts = [0]
    for start, end in tokens[:-1]:
        starts.append(starts[-1] + end - start + 1)

    places = _passage_places(text, wanted)
    counts = Counter(wanted)
    slack = len(wanted) // 4 + 10
    scored: list[tuple[float, int, int, bool]] = []
    for place in places[:_PASSAGE_COMPARISONS]:
        begin = max(0, place - slack)
        region = text[begin : place + len(wanted) + slack]
        # Matched characters within any span are at most those both sides hold.
        common = sum((counts & Counter(region)).values())
        if 2 * common / (len(wanted) + common) < _CANDIDATE_MIN_SIMILARITY:
            continue
        found = _passage_in(wanted, region)
        if found is not None:
            score, start, end, exact = found
            scored.append((score, begin + start, begin + end, exact))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen: list[tuple[int, int, bool]] = []
    for score, start, end, exact in scored:
        if score < max(_CANDIDATE_MIN_SIMILARITY, scored[0][0] - _PASSAGE_MARGIN):
            break
        if all(end <= other_start or start >= other_end for other_start, other_end, _ in chosen):
            chosen.append((start, end, exact))
            if len(chosen) == _CANDIDATE_RESULT_LIMIT:
                break

    def original(index: int, *, end: bool) -> int:
        token = bisect_right(starts, index) - 1
        token_start, token_end = tokens[token]
        position = min(token_start + index - starts[token], token_end - 1)
        return spans[position][1] if end else spans[position][0]

    result = []
    for start, end, exact in chosen:
        token = bisect_right(starts, start) - 1
        # A start the pattern's first characters do not show moves to the nearer word start.
        if not exact and start > starts[token]:
            length = tokens[token][1] - tokens[token][0]
            nearer = token + 1 if start - starts[token] > length // 2 else token
            start = starts[min(nearer, len(starts) - 1)]
        result.append((original(start, end=False), original(max(start, end - 1), end=True)))
    return result


def _passage_places(text: str, wanted: str) -> list[int]:
    """Return where ``wanted`` would start in ``text``, placed by its rarest words first."""
    words = {word: text.count(word) for word in set(_PASSAGE_WORD_RE.findall(wanted))}
    anchors = sorted(
        (word for word, count in words.items() if count), key=lambda word: (words[word], -len(word))
    )[:_PASSAGE_ANCHORS]
    places: list[int] = []
    for word in anchors:
        offsets = [found.start() for found in re.finditer(re.escape(word), wanted)][:3]
        position = text.find(word)
        for _ in range(_PASSAGE_ANCHOR_PLACES):
            if position < 0:
                break
            for offset in offsets:
                place = max(0, position - offset)
                # Places close together compare one region.
                if all(abs(place - other) > len(wanted) // 8 for other in places):
                    places.append(place)
            position = text.find(word, position + 1)
    return places


def _passage_in(wanted: str, region: str) -> tuple[float, int, int, bool] | None:
    """Score the part of ``region`` most like ``wanted``: (similarity, start, end, exact).

    ``start`` is where the first character of ``wanted`` would be; ``exact``
    says that character is there.
    """
    blocks = [
        block
        for block in SequenceMatcher(None, wanted, region, autojunk=False).get_matching_blocks()
        if block.size
    ]
    marked = [block for block in blocks if block.size >= _PASSAGE_BLOCK_CHARS]
    if not marked:
        return None
    first, last = marked[0], marked[-1]
    left, right = first.b, last.b + last.size
    matched = sum(
        block.size for block in blocks if left <= block.b and block.b + block.size <= right
    )
    score = 2 * matched / (len(wanted) + right - left)
    start = max(0, first.b - first.a)
    end = min(len(region), right + len(wanted) - last.a - last.size)
    return score, start, end, first.a == 0
