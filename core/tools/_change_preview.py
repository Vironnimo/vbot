"""Bounded before/after previews for the apply_patch Tool."""

from __future__ import annotations

from bisect import bisect_right

from core.tools.arguments import LINE_NUMBER_GUTTER_SEPARATOR, TEXT_LINE_BREAK, split_text_lines
from core.tools.tools import JsonObject

_PREVIEW_CONTEXT_LINES = 1
_PREVIEW_MAX_LINES_PER_SIDE = 8
_PREVIEW_MAX_LINE_CHARS = 240
_PREVIEW_MAX_REGIONS = 2


def _bounded_preview_indices(start: int, end: int) -> tuple[list[int], int]:
    count = end - start
    if count <= _PREVIEW_MAX_LINES_PER_SIDE:
        return list(range(start, end)), 0
    leading = _PREVIEW_MAX_LINES_PER_SIDE // 2
    trailing = _PREVIEW_MAX_LINES_PER_SIDE - leading
    indices = list(range(start, start + leading)) + list(range(end - trailing, end))
    return indices, count - len(indices)


def _render_preview_line(line: str, line_number: int, focus_character: int | None) -> str:
    if len(line) <= _PREVIEW_MAX_LINE_CHARS:
        return f"{line_number}{LINE_NUMBER_GUTTER_SEPARATOR} {line}"
    focus_index = max((focus_character or 1) - 1, 0)
    start = max(0, focus_index - _PREVIEW_MAX_LINE_CHARS // 3)
    end = min(len(line), start + _PREVIEW_MAX_LINE_CHARS)
    start = max(0, end - _PREVIEW_MAX_LINE_CHARS)
    gutter = f"{line_number}"
    if start:
        gutter += f":{start + 1}"
    suffix = "..." if end < len(line) else ""
    return f"{gutter}{LINE_NUMBER_GUTTER_SEPARATOR} {line[start:end]}{suffix}"


def _render_preview_side(
    lines: list[str],
    start: int,
    end: int,
    focus_line: int,
    focus_character: int,
) -> tuple[list[str], int]:
    indices, omitted = _bounded_preview_indices(start, end)
    return [
        _render_preview_line(
            lines[index], index + 1, focus_character if index == focus_line else None
        )
        for index in indices
    ], omitted


def _line_starts(text: str) -> list[int]:
    return [0, *(match.end() for match in TEXT_LINE_BREAK.finditer(text))]


def _preview_span(text: str, span: tuple[int, int]) -> tuple[list[str], int, int, int, int]:
    lines = split_text_lines(text)
    starts = _line_starts(text)
    span_start, span_end = span
    focus_line = bisect_right(starts, span_start) - 1
    last_offset = max(span_start, span_end - 1)
    last_line = bisect_right(starts, last_offset) - 1
    if lines:
        focus_line = min(focus_line, len(lines) - 1)
        last_line = min(last_line, len(lines) - 1)
    focus_character = span_start - starts[min(focus_line, len(starts) - 1)] + 1
    start = max(0, focus_line - _PREVIEW_CONTEXT_LINES)
    end = min(len(lines), last_line + _PREVIEW_CONTEXT_LINES + 1)
    return lines, start, end, focus_line, focus_character


def _change_preview(
    before: str,
    after: str,
    before_spans: tuple[tuple[int, int], ...],
    after_spans: tuple[tuple[int, int], ...],
) -> tuple[list[JsonObject], int]:
    span_pairs = list(zip(before_spans, after_spans, strict=True))
    selected_pairs = span_pairs
    if len(span_pairs) > _PREVIEW_MAX_REGIONS:
        selected_pairs = [span_pairs[0], span_pairs[-1]]
    regions: list[JsonObject] = []
    for before_span, after_span in selected_pairs:
        before_lines, before_start, before_end, before_focus_line, before_focus_character = (
            _preview_span(before, before_span)
        )
        after_lines, after_start, after_end, after_focus_line, after_focus_character = (
            _preview_span(after, after_span)
        )
        before_preview, before_omitted = _render_preview_side(
            before_lines,
            before_start,
            before_end,
            before_focus_line,
            before_focus_character,
        )
        after_preview, after_omitted = _render_preview_side(
            after_lines,
            after_start,
            after_end,
            after_focus_line,
            after_focus_character,
        )
        region: JsonObject = {"before": before_preview, "after": after_preview}
        if before_omitted:
            region["before_omitted_lines"] = before_omitted
        if after_omitted:
            region["after_omitted_lines"] = after_omitted
        regions.append(region)
    return regions, len(span_pairs) - len(selected_pairs)
