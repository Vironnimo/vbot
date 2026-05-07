"""Built-in edit tool adapted for vBot tool envelopes."""

from __future__ import annotations

from pathlib import Path

from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure, tool_success

EDIT_TOOL_NAME = "edit"
EDIT_TOOL_DESCRIPTION = (
    "Edit a file by replacing exact text. The old_string must match exactly "
    "(including whitespace) and must be unique in the file unless replace_all "
    "is true. Use this for precise, surgical edits."
)
EDIT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the file to edit (relative to workspace, or absolute).",
        },
        "old_string": {
            "type": "string",
            "description": (
                "Exact text to find and replace (must be unique unless replace_all is true)."
            ),
        },
        "new_string": {
            "type": "string",
            "description": "New text to replace the old text with.",
        },
        "replace_all": {
            "type": "boolean",
            "description": "Replace all occurrences instead of requiring uniqueness.",
            "default": False,
        },
    },
    "required": ["path", "old_string", "new_string"],
    "additionalProperties": False,
}


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalize_newlines_with_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    normalized_chars: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0

    while index < len(text):
        char = text[index]
        if char == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
            normalized_chars.append("\n")
            spans.append((index, index + 2))
            index += 2
            continue
        if char == "\r":
            normalized_chars.append("\n")
        else:
            normalized_chars.append(char)
        spans.append((index, index + 1))
        index += 1

    return "".join(normalized_chars), spans


def _find_all_occurrences(content: str, needle: str) -> list[int]:
    positions: list[int] = []
    start_index = 0

    while True:
        found_index = content.find(needle, start_index)
        if found_index < 0:
            return positions
        positions.append(found_index)
        start_index = found_index + len(needle)


def _detect_line_ending(content: str) -> str | None:
    if "\r\n" in content:
        return "\r\n"
    if "\n" in content or "\r" in content:
        return "\n"
    return None


def _adapt_new_string(new_string: str, *, file_line_ending: str | None) -> str:
    if file_line_ending is None:
        return new_string
    return _normalize_newlines(new_string).replace("\n", file_line_ending)


def _format_ambiguous_match_error(occurrence_count: int, line_numbers: list[int]) -> str:
    line_preview = ", ".join(str(line_number) for line_number in line_numbers[:3])
    if line_preview:
        return (
            f"Found {occurrence_count} occurrences on lines {line_preview}. "
            "Provide more context to make it unique, or use replace_all=true."
        )
    return (
        f"Found {occurrence_count} occurrences. Provide more context to make it "
        "unique, or use replace_all=true."
    )


def _resolve_edit_path(context: ToolContext, path: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (context.workspace / candidate).resolve()


def _validate_edit_arguments(arguments: JsonObject) -> tuple[str, str, str, bool] | JsonObject:
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

    replace_all = arguments.get("replace_all", False)
    if not isinstance(replace_all, bool):
        return tool_failure("invalid_arguments", "replace_all must be a boolean")

    if old_string == new_string:
        return tool_failure(
            "invalid_arguments",
            "old_string and new_string are identical (no-op replacement)",
        )

    return path_argument, old_string, new_string, replace_all


def _build_success_data(
    resolved: Path,
    first_line_number: int,
    replaced_count: int,
) -> JsonObject:
    message = (
        f"OK: updated {resolved} (first changed line: {first_line_number}, "
        f"replacements: {replaced_count})"
    )
    return {
        "message": message,
        "path": str(resolved),
        "first_changed_line": first_line_number,
        "replacements": replaced_count,
    }


def _replace_exact_matches(
    content: str,
    old_string: str,
    replacement_text: str,
    replace_all: bool,
) -> tuple[str, int, int] | JsonObject | None:
    exact_occurrences = _find_all_occurrences(content, old_string)
    if not exact_occurrences:
        return None

    exact_line_numbers = [content.count("\n", 0, index) + 1 for index in exact_occurrences]
    if len(exact_occurrences) > 1 and not replace_all:
        return tool_failure(
            "ambiguous_match",
            _format_ambiguous_match_error(len(exact_occurrences), exact_line_numbers),
        )

    if replace_all:
        updated_content = content.replace(old_string, replacement_text)
        replaced_count = len(exact_occurrences)
    else:
        updated_content = content.replace(old_string, replacement_text, 1)
        replaced_count = 1

    return updated_content, exact_line_numbers[0], replaced_count


def _replace_normalized_matches(
    content: str,
    old_string: str,
    replacement_text: str,
    replace_all: bool,
) -> tuple[str, int, int] | JsonObject:
    normalized_content, normalized_spans = _normalize_newlines_with_spans(content)
    normalized_old_string = _normalize_newlines(old_string)
    normalized_occurrences = _find_all_occurrences(normalized_content, normalized_old_string)

    if not normalized_occurrences:
        return tool_failure(
            "text_not_found",
            "old_string not found in file. Check whitespace, indentation, or line endings.",
        )

    normalized_line_numbers = [
        normalized_content.count("\n", 0, index) + 1 for index in normalized_occurrences
    ]
    if len(normalized_occurrences) > 1 and not replace_all:
        return tool_failure(
            "ambiguous_match",
            _format_ambiguous_match_error(len(normalized_occurrences), normalized_line_numbers),
        )

    replaced_count = len(normalized_occurrences) if replace_all else 1
    selected_occurrences = normalized_occurrences if replace_all else normalized_occurrences[:1]
    updated_content = content

    for occurrence_index in reversed(selected_occurrences):
        original_start = normalized_spans[occurrence_index][0]
        original_end = normalized_spans[occurrence_index + len(normalized_old_string) - 1][1]
        updated_content = (
            updated_content[:original_start] + replacement_text + updated_content[original_end:]
        )

    return updated_content, normalized_line_numbers[0], replaced_count


def edit_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    """Handle an edit tool call and return a stable vBot result envelope."""
    validated_arguments = _validate_edit_arguments(arguments)
    if isinstance(validated_arguments, dict):
        return validated_arguments

    path_argument, old_string, new_string, replace_all = validated_arguments

    try:
        resolved = _resolve_edit_path(context, path_argument)
    except RuntimeError as error:
        return tool_failure("invalid_path", str(error))

    try:
        if not resolved.exists():
            return tool_failure("file_not_found", f"file not found: {resolved}")
        if not resolved.is_file():
            return tool_failure("not_a_file", f"path is not a file: {resolved}")
        content = resolved.read_bytes().decode("utf-8", errors="replace")
    except OSError as error:
        return tool_failure("file_read_error", f"failed to read file: {resolved}: {error}")

    file_line_ending = _detect_line_ending(content)
    replacement_text = _adapt_new_string(new_string, file_line_ending=file_line_ending)
    replacement_result = _replace_exact_matches(
        content,
        old_string,
        replacement_text,
        replace_all,
    )
    if replacement_result is None:
        replacement_result = _replace_normalized_matches(
            content,
            old_string,
            replacement_text,
            replace_all,
        )
    if isinstance(replacement_result, dict):
        return replacement_result

    updated_content, first_line_number, replaced_count = replacement_result
    if updated_content == content:
        return tool_failure("no_changes", "replacement produced no changes")

    try:
        resolved.write_bytes(updated_content.encode("utf-8"))
    except OSError as error:
        return tool_failure("file_write_error", f"failed to write file: {resolved}: {error}")

    return tool_success(_build_success_data(resolved, first_line_number, replaced_count))


def register_edit_tool(registry: ToolRegistry) -> None:
    """Register the edit tool with a vBot tool registry."""
    registry.register(
        EDIT_TOOL_NAME,
        EDIT_TOOL_DESCRIPTION,
        EDIT_TOOL_PARAMETERS,
        edit_handler,
    )


__all__ = [
    "EDIT_TOOL_DESCRIPTION",
    "EDIT_TOOL_NAME",
    "EDIT_TOOL_PARAMETERS",
    "edit_handler",
    "register_edit_tool",
]
