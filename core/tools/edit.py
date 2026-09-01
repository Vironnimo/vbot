"""Built-in edit tool adapted for vBot tool envelopes."""

from __future__ import annotations

import re
from bisect import bisect_right
from contextlib import nullcontext
from pathlib import Path

from core.tools.arguments import (
    LINE_NUMBER_GUTTER_SEPARATOR,
    ToolArgumentError,
    line_number_gutter_candidates,
    logical_line_count,
    looks_like_line_numbered_content,
    optional_bool,
    strip_line_number_gutters,
)
from core.tools.file_state import FileReadState, StaleReason, atomic_write_bytes
from core.tools.fuzzy_match import (
    AmbiguousFuzzyMatch,
    ClosestFuzzyCandidate,
    FuzzyReplacement,
    find_closest_candidates,
    replace_fuzzy,
)
from core.tools.syntax_check import warning_for_edited_file
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolHandler,
    ToolRegistry,
    offload_tool_handler,
    tool_failure,
    tool_success,
)
from core.utils.paths import model_path

EDIT_TOOL_NAME = "edit"
EDIT_TOOL_DESCRIPTION = (
    "Apply ordered text replacements across files in one call. Every edit is attempted "
    "even if another fails."
)
EDIT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "minLength": 1,
            "description": "Default file to edit; used by edits that omit their own path.",
        },
        "edits": {
            "type": "array",
            "minItems": 1,
            "description": "Replacements to attempt in order.",
            "items": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "File to edit, relative to the working directory or absolute. "
                            "Omit to use the top-level path."
                        ),
                    },
                    "old_string": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Text to replace. Include enough surrounding text to identify "
                            "one match unless replace_all is true."
                        ),
                    },
                    "new_string": {
                        "type": "string",
                        "description": (
                            "Replacement text. Use an empty string to delete old_string."
                        ),
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": ("Replace every match. Omit to require one unique match."),
                    },
                },
                "required": ["old_string", "new_string"],
            },
        },
    },
    "required": ["edits"],
}

AMBIGUOUS_CONTEXT_LIMIT = 3
AMBIGUOUS_CONTEXT_LINE_RADIUS = 1
AMBIGUOUS_CONTEXT_LINE_MAX_CHARS = 160
AMBIGUOUS_INLINE_CONTEXT_CHARS = 120
EDIT_PREVIEW_CONTEXT_LINES = 1
EDIT_PREVIEW_MAX_LINES_PER_SIDE = 8
EDIT_PREVIEW_MAX_LINE_CHARS = 240
EDIT_PREVIEW_MAX_REGIONS = 2
EDIT_LINE_BREAK_PATTERN = re.compile(r"\r\n|[\n\v\f\x1c-\x1e\x85\u2028\u2029\r]")
ALREADY_APPLIED_STRATEGIES = frozenset({"exact", "normalized"})
ALREADY_APPLIED_CONTEXT_MIN_CHARS = 4
NO_MATCH_DIFFERENCE_EXCERPT_MAX_CHARS = 80


def _bounded_context_line(line: str) -> str:
    if len(line) <= AMBIGUOUS_CONTEXT_LINE_MAX_CHARS:
        return line
    return line[: AMBIGUOUS_CONTEXT_LINE_MAX_CHARS - 3] + "..."


def _candidate_context(content: str, line_number: int) -> str:
    lines = content.splitlines()
    if not lines:
        return ""
    target_index = min(max(line_number - 1, 0), len(lines) - 1)
    start = max(0, target_index - AMBIGUOUS_CONTEXT_LINE_RADIUS)
    end = min(len(lines), target_index + AMBIGUOUS_CONTEXT_LINE_RADIUS + 1)
    return "\n".join(_bounded_context_line(line) for line in lines[start:end])


def _inline_candidate_context(content: str, line_number: int, character_number: int) -> str:
    lines = content.splitlines()
    if not lines:
        return ""
    line = lines[min(max(line_number - 1, 0), len(lines) - 1)]
    match_index = min(max(character_number - 1, 0), len(line))
    half_window = AMBIGUOUS_INLINE_CONTEXT_CHARS // 2
    start = max(0, match_index - half_window)
    end = min(len(line), start + AMBIGUOUS_INLINE_CONTEXT_CHARS)
    start = max(0, end - AMBIGUOUS_INLINE_CONTEXT_CHARS)
    prefix = "..." if start else ""
    suffix = "..." if end < len(line) else ""
    return prefix + line[start:end] + suffix


def _format_ambiguous_match_error(
    content: str,
    occurrence_count: int,
    line_numbers: list[int],
    character_numbers: list[int],
) -> str:
    unique_lines = list(dict.fromkeys(line_numbers))
    line_preview = ", ".join(str(line_number) for line_number in unique_lines[:3])
    if len(unique_lines) == 1:
        summary = f"Found {occurrence_count} occurrences on line {line_preview}. "
    elif line_preview:
        summary = f"Found {occurrence_count} occurrences on lines {line_preview}. "
    else:
        summary = f"Found {occurrence_count} occurrences. "

    same_line = len(unique_lines) == 1
    candidates = []
    for index, (line_number, character_number) in enumerate(
        zip(line_numbers, character_numbers, strict=True), start=1
    ):
        if index > AMBIGUOUS_CONTEXT_LIMIT:
            break
        if same_line:
            heading = f"Candidate {index} (line {line_number}, character {character_number}):"
            context = _inline_candidate_context(content, line_number, character_number)
        else:
            heading = f"Candidate {index} (around line {line_number}):"
            context = _candidate_context(content, line_number)
        candidates.append(f"{heading}\n{context}")
    candidate_text = ""
    if candidates:
        candidate_text = (
            "Raw candidate contexts (without read's line-number gutter):\n"
            + "\n".join(candidates)
            + "\n"
        )
        if occurrence_count > AMBIGUOUS_CONTEXT_LIMIT:
            candidate_text += (
                f"Showing the first {AMBIGUOUS_CONTEXT_LIMIT} of {occurrence_count} candidates.\n"
            )

    return (
        summary
        + candidate_text
        + "Provide enough raw surrounding text to make the target unique, or use "
        "replace_all=true only when every occurrence should change."
    )


def _line_and_column(text: str, offset: int) -> tuple[int, int]:
    prefix = text[:offset]
    return prefix.count("\n") + 1, offset - prefix.rfind("\n")


def _difference_excerpt(text: str, offset: int) -> str:
    if offset >= len(text):
        return "<end of text>"
    line_end = text.find("\n", offset)
    if line_end < 0:
        line_end = len(text)
    excerpt = text[offset:line_end][:NO_MATCH_DIFFERENCE_EXCERPT_MAX_CHARS]
    return repr(excerpt)


def _first_difference(candidate: ClosestFuzzyCandidate, pattern: str) -> str:
    """Describe the first concrete mismatch without authorizing a fuzzy write."""
    if candidate.truncated:
        return ""

    normalized_pattern = EDIT_LINE_BREAK_PATTERN.sub("\n", pattern)
    normalized_candidate = EDIT_LINE_BREAK_PATTERN.sub("\n", candidate.text)
    limit = min(len(normalized_pattern), len(normalized_candidate))
    offset = 0
    while offset < limit and normalized_pattern[offset] == normalized_candidate[offset]:
        offset += 1
    if offset == len(normalized_pattern) == len(normalized_candidate):
        return ""

    pattern_line, pattern_column = _line_and_column(normalized_pattern, offset)
    candidate_line, _ = _line_and_column(normalized_candidate, offset)
    file_line = candidate.line_number + candidate_line - 1
    return (
        f"\nFirst difference from old_string at line {pattern_line}, "
        f"column {pattern_column} (file line {file_line}):\n"
        f"old_string: {_difference_excerpt(normalized_pattern, offset)}\n"
        f"file: {_difference_excerpt(normalized_candidate, offset)}"
    )


def _format_no_match_candidates(candidates: list[ClosestFuzzyCandidate], pattern: str) -> str:
    if not candidates:
        return ""
    blocks = []
    for index, candidate in enumerate(candidates, start=1):
        excerpt_kind = "bounded raw prefix" if candidate.truncated else "raw text"
        blocks.append(
            f"Candidate {index} (starting line {candidate.line_number}; {excerpt_kind}):\n"
            f"{candidate.text}{_first_difference(candidate, pattern)}"
        )
    return (
        "\nClosest raw candidates (without read's line-number gutter):\n"
        + "\n\n".join(blocks)
        + "\nRetry with the intended candidate text as old_string and add raw surrounding "
        "context if it is not unique."
    )


def _text_not_found_failure(content: str, old_string: str) -> JsonObject:
    block_candidates = line_number_gutter_candidates(old_string)
    per_line = strip_line_number_gutters(old_string)

    if block_candidates:
        message = "old_string not found after removing read's line-number gutter."
        diagnostic_pattern = block_candidates[0]
    elif per_line is not None and per_line != old_string:
        message = "old_string not found after removing read's line-number gutter."
        diagnostic_pattern = per_line
    elif looks_like_line_numbered_content(old_string):
        message = (
            "old_string not found — it appears to contain an incomplete or damaged "
            "read line-number gutter that could not be recovered safely."
        )
        diagnostic_pattern = old_string
    else:
        message = "old_string not found in file. Compare it with the closest candidate below."
        diagnostic_pattern = old_string

    candidates = find_closest_candidates(content, diagnostic_pattern)
    return tool_failure(
        "text_not_found", message + _format_no_match_candidates(candidates, diagnostic_pattern)
    )


def _replace_with_gutter_fallback(
    content: str, old_string: str, new_string: str, *, replace_all: bool
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    """Match raw ``old_string`` first, then complete pasted read-gutter variants."""
    result = replace_fuzzy(content, old_string, new_string, replace_all=replace_all)
    if result is not None:
        return result

    for candidate in line_number_gutter_candidates(old_string):
        result = replace_fuzzy(content, candidate, new_string, replace_all=replace_all)
        if result is not None:
            return result

    # Per-line fallback: strip a gutter from each line that carries one, leaving
    # other lines unchanged. Handles single-line gutters and partial gutter
    # blocks that the strict block-level detector above rejects.
    per_line = strip_line_number_gutters(old_string)
    if per_line is not None and per_line != old_string:
        result = replace_fuzzy(content, per_line, new_string, replace_all=replace_all)
        if result is not None:
            return result
    return None


def _already_applied_match(
    content: str, old_string: str, new_string: str, *, replace_all: bool
) -> FuzzyReplacement | None:
    """Return a precise no-op match when the replacement is already present."""
    if not new_string or not _has_shared_replacement_context(old_string, new_string):
        return None
    result = replace_fuzzy(content, new_string, new_string, replace_all=replace_all)
    if not isinstance(result, FuzzyReplacement):
        return None
    if result.strategy not in ALREADY_APPLIED_STRATEGIES or result.new_content != content:
        return None
    return result


def _has_shared_replacement_context(old_string: str, new_string: str) -> bool:
    prefix_length = 0
    for old_character, new_character in zip(old_string, new_string, strict=False):
        if old_character != new_character:
            break
        prefix_length += 1

    remaining_old = len(old_string) - prefix_length
    remaining_new = len(new_string) - prefix_length
    suffix_length = 0
    for offset in range(1, min(remaining_old, remaining_new) + 1):
        if old_string[-offset] != new_string[-offset]:
            break
        suffix_length += 1

    shared = old_string[:prefix_length]
    if suffix_length:
        shared += old_string[-suffix_length:]
    meaningful_characters = sum(not character.isspace() for character in shared)
    return meaningful_characters >= ALREADY_APPLIED_CONTEXT_MIN_CHARS


def _validate_edit_arguments(
    arguments: JsonObject,
) -> tuple[str, str, str, bool, str | None] | JsonObject:
    unknown_arguments = set(arguments) - {"path", "old_string", "new_string", "replace_all"}
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

    path_argument = arguments.get("path")
    if not isinstance(path_argument, str) or not path_argument:
        return tool_failure("invalid_arguments", "path must be a non-empty string")

    old_string = arguments.get("old_string")
    if not isinstance(old_string, str):
        return tool_failure("invalid_arguments", "old_string must be a string")
    if old_string == "":
        return tool_failure("invalid_arguments", "old_string must not be empty")

    new_string = arguments.get("new_string")
    if not isinstance(new_string, str):
        return tool_failure("invalid_arguments", "new_string must be a string")

    complete_gutter_candidates = line_number_gutter_candidates(new_string)
    safe_gutter_candidates = line_number_gutter_candidates(new_string, allow_continuations=False)
    gutter_shaped_candidates = line_number_gutter_candidates(new_string, require_consecutive=False)
    normalization_warning = None
    if complete_gutter_candidates and not safe_gutter_candidates:
        return tool_failure(
            "line_numbered_content",
            "new_string contains read's N:C| continuation gutter. Use complete raw "
            "lines instead of a partial long-line excerpt.",
        )
    if safe_gutter_candidates:
        new_string = safe_gutter_candidates[0]
        normalization_warning = (
            "Removed read line-number gutters from new_string before applying the edit."
        )
    elif gutter_shaped_candidates or looks_like_line_numbered_content(new_string):
        return tool_failure(
            "line_numbered_content",
            "new_string looks like read's `N| ` line-number gutter pasted back in. "
            "Use the raw replacement text without the leading line numbers.",
        )

    try:
        replace_all = optional_bool(
            arguments.get("replace_all"), field_name="replace_all", default=False
        )
    except ToolArgumentError as error:
        return tool_failure("invalid_arguments", str(error))

    if old_string == new_string:
        return tool_failure(
            "invalid_arguments",
            "old_string and new_string are identical (no-op replacement)",
        )

    return path_argument, old_string, new_string, replace_all, normalization_warning


def _build_success_data(
    resolved: Path,
    first_line_number: int,
    last_line_number: int,
    replaced_count: int,
    preview: list[JsonObject],
    preview_omitted_regions: int,
    syntax_warning: str | None,
    stale_warning: str | None,
    normalization_warning: str | None,
) -> JsonObject:
    displayed_path = model_path(resolved)
    message = (
        f"OK: updated {displayed_path} (first changed line: {first_line_number}, "
        f"replacements: {replaced_count})"
    )
    data: JsonObject = {
        "message": message,
        "path": displayed_path,
        "first_changed_line": first_line_number,
        "last_changed_line": last_line_number,
        "replacements": replaced_count,
        "preview": preview,
    }
    if preview_omitted_regions:
        data["preview_omitted_regions"] = preview_omitted_regions
    if syntax_warning is not None:
        data["syntax_warning"] = syntax_warning
    if stale_warning is not None:
        data["stale_warning"] = stale_warning
    if normalization_warning is not None:
        data["normalization_warning"] = normalization_warning
    return data


def _bounded_preview_indices(start: int, end: int) -> tuple[list[int], int]:
    count = end - start
    if count <= EDIT_PREVIEW_MAX_LINES_PER_SIDE:
        return list(range(start, end)), 0
    leading = EDIT_PREVIEW_MAX_LINES_PER_SIDE // 2
    trailing = EDIT_PREVIEW_MAX_LINES_PER_SIDE - leading
    indices = list(range(start, start + leading)) + list(range(end - trailing, end))
    return indices, count - len(indices)


def _render_preview_line(line: str, line_number: int, focus_character: int | None) -> str:
    if len(line) <= EDIT_PREVIEW_MAX_LINE_CHARS:
        return f"{line_number}{LINE_NUMBER_GUTTER_SEPARATOR} {line}"
    focus_index = max((focus_character or 1) - 1, 0)
    start = max(0, focus_index - EDIT_PREVIEW_MAX_LINE_CHARS // 3)
    end = min(len(line), start + EDIT_PREVIEW_MAX_LINE_CHARS)
    start = max(0, end - EDIT_PREVIEW_MAX_LINE_CHARS)
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
    return [0, *(match.end() for match in EDIT_LINE_BREAK_PATTERN.finditer(text))]


def _preview_span(text: str, span: tuple[int, int]) -> tuple[list[str], int, int, int, int]:
    lines = text.splitlines()
    starts = _line_starts(text)
    span_start, span_end = span
    focus_line = bisect_right(starts, span_start) - 1
    last_offset = max(span_start, span_end - 1)
    last_line = bisect_right(starts, last_offset) - 1
    if lines:
        focus_line = min(focus_line, len(lines) - 1)
        last_line = min(last_line, len(lines) - 1)
    focus_character = span_start - starts[min(focus_line, len(starts) - 1)] + 1
    start = max(0, focus_line - EDIT_PREVIEW_CONTEXT_LINES)
    end = min(len(lines), last_line + EDIT_PREVIEW_CONTEXT_LINES + 1)
    return lines, start, end, focus_line, focus_character


def _change_preview(
    before: str,
    after: str,
    before_spans: tuple[tuple[int, int], ...],
    after_spans: tuple[tuple[int, int], ...],
) -> tuple[list[JsonObject], int]:
    span_pairs = list(zip(before_spans, after_spans, strict=True))
    selected_pairs = span_pairs
    if len(span_pairs) > EDIT_PREVIEW_MAX_REGIONS:
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


def _edit_one(
    context: ToolContext, arguments: JsonObject, *, file_state: FileReadState | None = None
) -> JsonObject:
    """Apply one replacement and return its stable result envelope.

    When ``file_state`` is supplied, mutations of the same path are serialized.
    A prior read is not required: the unique ``old_string`` match against current
    on-disk content is the edit's optimistic precondition. A successful edit
    restamps the file for later full-file writes.
    """
    validated_arguments = _validate_edit_arguments(arguments)
    if isinstance(validated_arguments, dict):
        return validated_arguments

    path_argument, old_string, new_string, replace_all, normalization_warning = validated_arguments

    try:
        resolved = context.resolve_path(path_argument)
    except RuntimeError as error:
        return tool_failure("invalid_path", str(error))
    displayed_path = model_path(resolved)

    mutation_lock = file_state.lock_path(resolved) if file_state is not None else nullcontext()
    with mutation_lock:
        try:
            if not resolved.exists():
                return tool_failure("file_not_found", f"file not found: {displayed_path}")
            if not resolved.is_file():
                return tool_failure("not_a_file", f"path is not a file: {displayed_path}")
            stale_reason = (
                file_state.check_stale(context.session_id, resolved)
                if file_state is not None
                else None
            )
            raw_content = resolved.read_bytes()
        except OSError as error:
            return tool_failure(
                "file_read_error", f"failed to read file: {displayed_path}: {error}"
            )

        if b"\x00" in raw_content:
            return tool_failure(
                "binary_file",
                f"Cannot edit binary file: {displayed_path}. "
                "The edit tool only supports UTF-8 text files.",
            )

        try:
            content = raw_content.decode("utf-8")
        except UnicodeDecodeError:
            return tool_failure(
                "unsupported_encoding",
                f"Cannot edit file because it is not valid UTF-8: {displayed_path}. "
                "Convert it to UTF-8 before using edit.",
            )

        # The matcher progresses from exact through deterministic newline, Unicode,
        # line-boundary, and horizontal-whitespace normalization, always splicing
        # the real original content.
        result = _replace_with_gutter_fallback(
            content, old_string, new_string, replace_all=replace_all
        )
        if result is None:
            already_applied = _already_applied_match(
                content, old_string, new_string, replace_all=replace_all
            )
            if already_applied is not None:
                if file_state is not None:
                    file_state.record_read(context.session_id, resolved)
                data: JsonObject = {
                    "message": f"OK: replacement already applied in {displayed_path}",
                    "path": displayed_path,
                    "replacements": 0,
                    "already_applied": True,
                    "added_lines": 0,
                    "removed_lines": 0,
                }
                if normalization_warning is not None:
                    data["normalization_warning"] = normalization_warning
                return tool_success(data)
            return _text_not_found_failure(content, old_string)
        if isinstance(result, AmbiguousFuzzyMatch):
            return tool_failure(
                "ambiguous_match",
                _format_ambiguous_match_error(
                    content,
                    result.occurrences,
                    result.line_numbers,
                    result.character_numbers,
                ),
            )

        if result.new_content == content:
            return tool_failure("no_changes", "replacement produced no changes")

        try:
            atomic_write_bytes(resolved, result.new_content.encode("utf-8"))
        except OSError as error:
            return tool_failure(
                "file_write_error", f"failed to write file: {displayed_path}: {error}"
            )

        # The edit is an implicit read of the new content: restamp so later writes
        # compare against this exact version.
        if file_state is not None:
            file_state.record_read(context.session_id, resolved)

        # Record the before/after content for git-style change statistics. The
        # baseline is the file's actual on-disk content immediately before this
        # edit, so external changes between tool calls (formatters, shell
        # commands) are never attributed to this run. Repeated edits of one
        # file in a run deduplicate inside the tracker against the first
        # pre-mutation state.
        if context.change_tracker is not None:
            context.change_tracker.record_write(
                context.session_id,
                resolved,
                before=content,
                after=result.new_content,
            )

        # Non-blocking: the edit is already written. The syntax warning reports
        # only a break this edit introduced, never a pre-existing one.
        syntax_warning = warning_for_edited_file(resolved, content, result.new_content)
        stale_warning = None
        if stale_reason is StaleReason.MODIFIED:
            stale_warning = (
                f"{displayed_path} changed since this Session last read it. "
                "The edit was applied to the current on-disk content and preserved "
                "all unmatched bytes."
            )
        added_lines = logical_line_count(new_string) * result.replacements
        removed_lines = logical_line_count(old_string) * result.replacements
        preview, preview_omitted_regions = _change_preview(
            content,
            result.new_content,
            result.before_spans,
            result.after_spans,
        )
        success_data = _build_success_data(
            resolved,
            result.first_changed_line,
            result.last_changed_line,
            result.replacements,
            preview,
            preview_omitted_regions,
            syntax_warning,
            stale_warning,
            normalization_warning,
        )
        success_data["added_lines"] = added_lines
        success_data["removed_lines"] = removed_lines
        return tool_success(success_data)


def _validate_batch_arguments(
    arguments: JsonObject,
) -> tuple[list[object], str | None] | JsonObject:
    unknown_arguments = set(arguments) - {"edits", "path"}
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")
    default_path = arguments.get("path")
    if default_path is not None and (not isinstance(default_path, str) or not default_path):
        return tool_failure("invalid_arguments", "path must be a non-empty string")
    edits = arguments.get("edits")
    if not isinstance(edits, list) or not edits:
        return tool_failure("invalid_arguments", "edits must be a non-empty array")
    return edits, default_path


def _edit_with_default_path(edit: object, default_path: str | None) -> object:
    """Apply the batch-level default path to an edit that omits its own.

    An edit's explicit non-empty path wins (per-item precedence); otherwise the
    batch-level ``path`` fills the slot. An edit with neither passes through so
    the per-item validator reports the missing path.
    """
    if not isinstance(edit, dict):
        return edit
    current = edit.get("path")
    if isinstance(current, str) and current:
        return edit
    if default_path is None:
        return edit
    effective = dict(edit)
    effective["path"] = default_path
    return effective


def _failed_item(index: int, edit: object, result: JsonObject) -> JsonObject:
    outcome: JsonObject = {"index": index, "ok": False, "error": result["error"]}
    path = edit.get("path") if isinstance(edit, dict) else None
    if isinstance(path, str) and path:
        outcome["path"] = path
    return outcome


def _successful_item(index: int, result: JsonObject) -> JsonObject:
    data = result.get("data")
    assert isinstance(data, dict)
    visible_data = {
        key: value
        for key, value in data.items()
        if key not in {"added_lines", "message", "removed_lines"}
    }
    return {"index": index, "ok": True, **visible_data}


def _all_failed_result(results: list[JsonObject]) -> JsonObject:
    if len(results) == 1:
        error = results[0]["error"]
        return tool_failure(str(error["code"]), str(error["message"]))
    details = []
    for result in results:
        error = result["error"]
        path = f" ({result['path']})" if "path" in result else ""
        details.append(f"Edit {result['index']}{path}: [{error['code']}] {error['message']}")
    return tool_failure(
        "all_edits_failed",
        f"All {len(results)} edits failed.\n" + "\n".join(details),
    )


def edit_handler(
    context: ToolContext, arguments: JsonObject, *, file_state: FileReadState | None = None
) -> JsonObject:
    """Apply an ordered batch while isolating failures to their individual edits."""
    # Retain direct-call compatibility for internal callers and historical tests.
    # The registered Agent-facing schema exposes only the batched shape.
    if "edits" not in arguments:
        result = _edit_one(context, arguments, file_state=file_state)
        data = result.get("data")
        if result.get("ok") is True and isinstance(data, dict):
            added_lines = int(data.get("added_lines", 0))
            removed_lines = int(data.get("removed_lines", 0))
            if added_lines or removed_lines:
                context.add_display_line_changes(added=added_lines, removed=removed_lines)
            data.pop("added_lines", None)
            data.pop("removed_lines", None)
        return result

    return _edit_batch(context, arguments, file_state=file_state)


def _edit_batch(
    context: ToolContext, arguments: JsonObject, *, file_state: FileReadState | None = None
) -> JsonObject:
    """Apply only the registered batched shape, with item-local validation."""

    validated_arguments = _validate_batch_arguments(arguments)
    if isinstance(validated_arguments, dict):
        return validated_arguments
    edits, default_path = validated_arguments

    outcomes: list[JsonObject] = []
    added_lines = 0
    removed_lines = 0
    for index, edit in enumerate(edits):
        effective_edit = _edit_with_default_path(edit, default_path)
        result = (
            _edit_one(context, effective_edit, file_state=file_state)
            if isinstance(effective_edit, dict)
            else tool_failure("invalid_arguments", "edit must be an object")
        )
        data = result.get("data")
        if result.get("ok") is True and isinstance(data, dict):
            outcomes.append(_successful_item(index, result))
            added_lines += int(data.get("added_lines", 0))
            removed_lines += int(data.get("removed_lines", 0))
        else:
            outcomes.append(_failed_item(index, effective_edit, result))

    succeeded = sum(outcome["ok"] is True for outcome in outcomes)
    failed = len(outcomes) - succeeded
    if succeeded == 0:
        return _all_failed_result(outcomes)

    if added_lines or removed_lines:
        context.add_display_line_changes(added=added_lines, removed=removed_lines)
    status = "partial" if failed else "success"
    if failed:
        message = f"Applied {succeeded} of {len(outcomes)} edits; {failed} failed."
    elif succeeded == 1:
        message = "Applied 1 edit."
    else:
        message = f"Applied {succeeded} edits."
    return tool_success(
        {
            "message": message,
            "status": status,
            "total": len(outcomes),
            "succeeded": succeeded,
            "failed": failed,
            "results": outcomes,
        }
    )


def _edit_display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    edits = arguments.get("edits")
    if not isinstance(edits, list) or not edits or not isinstance(edits[0], dict):
        return ()
    path = edits[0].get("path")
    if not isinstance(path, str) or not path:
        path = arguments.get("path")
    if not isinstance(path, str) or not path:
        return ()
    return (
        ToolDisplayPart(
            value=path,
            kind="path",
            truncate="start",
            tooltip="always",
            copyable=True,
        ),
    )


def _edit_display_facts(arguments: JsonObject, result: JsonObject | None) -> tuple[JsonObject, ...]:
    edits = arguments.get("edits")
    if not isinstance(edits, list) or len(edits) <= 1:
        return ()
    default_path = arguments.get("path")
    paths: set[str] = set()
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        path = edit.get("path")
        if isinstance(path, str) and path:
            paths.add(path)
        elif isinstance(default_path, str) and default_path:
            paths.add(default_path)
    facts: list[JsonObject] = [
        {"kind": "count", "value": len(edits), "unit": "edits", "at_least": False},
        {"kind": "count", "value": len(paths), "unit": "files", "at_least": False},
    ]
    if isinstance(result, dict) and result.get("ok") is True:
        data = result.get("data")
        failed = data.get("failed") if isinstance(data, dict) else None
        if isinstance(failed, int) and failed > 0:
            facts.append({"kind": "count", "value": failed, "unit": "failures", "at_least": False})
    return tuple(facts)


def make_edit_handler(file_state: FileReadState) -> ToolHandler:
    """Create an edit handler bound to the read-before-write guard registry."""

    def edit_handler_bound(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return _edit_batch(context, arguments, file_state=file_state)

    return edit_handler_bound


def register_edit_tool(registry: ToolRegistry, *, file_state: FileReadState) -> None:
    """Register the edit tool with a vBot tool registry."""
    registry.register(
        EDIT_TOOL_NAME,
        EDIT_TOOL_DESCRIPTION,
        EDIT_TOOL_PARAMETERS,
        offload_tool_handler(make_edit_handler(file_state)),
        family="files",
        open_input_schema=True,
        handler_validates_arguments=True,
        result_schema={
            "type": "object",
            "required": ["message", "status", "total", "succeeded", "failed", "results"],
        },
        display=ToolDisplay(
            parts_builder=_edit_display_parts,
            fact_builder=_edit_display_facts,
            hidden_argument_keys=("edits", "new_string", "old_string"),
        ),
    )


__all__ = [
    "EDIT_TOOL_DESCRIPTION",
    "EDIT_TOOL_NAME",
    "EDIT_TOOL_PARAMETERS",
    "edit_handler",
    "make_edit_handler",
    "register_edit_tool",
]
