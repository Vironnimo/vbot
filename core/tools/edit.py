"""Built-in edit tool adapted for vBot tool envelopes."""

from __future__ import annotations

from pathlib import Path

from core.tools.arguments import (
    ToolArgumentError,
    coerce_bool,
    looks_like_line_numbered_content,
    normalize_aliases,
)
from core.tools.file_state import FileReadState, stale_failure_text
from core.tools.fuzzy_match import AmbiguousFuzzyMatch, replace_fuzzy
from core.tools.syntax_check import warning_for_edited_file
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolHandler,
    ToolRegistry,
    tool_failure,
    tool_success,
)

EDIT_TOOL_NAME = "edit"
EDIT_TOOL_DESCRIPTION = (
    "Edit a file by replacing text. old_string is matched against the file, "
    "tolerating minor differences in whitespace/indentation, line endings, and "
    "quote style; it must still identify a unique location unless replace_all is "
    "true. Use this for precise, surgical edits. You must read the file first; "
    "this tool fails if you did not, or if it changed on disk since you last read it."
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


def _text_not_found_failure(old_string: str) -> JsonObject:
    if looks_like_line_numbered_content(old_string):
        return tool_failure(
            "text_not_found",
            "old_string not found — it carries read's `N|` line-number gutter. "
            "Match against the raw file text, without the leading line numbers.",
        )
    return tool_failure(
        "text_not_found",
        "old_string not found in file. Check whitespace, indentation, or line endings.",
    )


def _resolve_edit_path(context: ToolContext, path: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (context.effective_cwd / candidate).resolve()


_EDIT_ARGUMENT_ALIASES = {
    "oldString": "old_string",
    "newString": "new_string",
    "replaceAll": "replace_all",
}


def _validate_edit_arguments(arguments: JsonObject) -> tuple[str, str, str, bool] | JsonObject:
    # Accept camelCase variants some models emit instead of rejecting the whole
    # call as an unknown argument; the canonical key wins if both are present.
    arguments = normalize_aliases(arguments, _EDIT_ARGUMENT_ALIASES)

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

    if looks_like_line_numbered_content(new_string):
        return tool_failure(
            "line_numbered_content",
            "new_string looks like read's `N|` line-number gutter pasted back in. "
            "Use the raw replacement text without the leading line numbers.",
        )

    try:
        replace_all = coerce_bool(
            arguments.get("replace_all"), field_name="replace_all", default=False
        )
    except ToolArgumentError as error:
        return tool_failure("invalid_arguments", str(error))

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
    syntax_warning: str | None,
) -> JsonObject:
    message = (
        f"OK: updated {resolved} (first changed line: {first_line_number}, "
        f"replacements: {replaced_count})"
    )
    data: JsonObject = {
        "message": message,
        "path": str(resolved),
        "first_changed_line": first_line_number,
        "replacements": replaced_count,
    }
    if syntax_warning is not None:
        data["syntax_warning"] = syntax_warning
    return data


def edit_handler(
    context: ToolContext, arguments: JsonObject, *, file_state: FileReadState | None = None
) -> JsonObject:
    """Handle an edit tool call and return a stable vBot result envelope.

    When ``file_state`` is supplied the read-before-write guard is active: the
    edit is refused unless the file was read in this session and has not changed
    on disk since. A successful edit restamps the file so the same session can
    edit again without re-reading.
    """
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
        if file_state is not None:
            reason = file_state.check_stale(context.session_id, resolved)
            if reason is not None:
                return tool_failure(*stale_failure_text(reason, resolved))
        content = resolved.read_bytes().decode("utf-8", errors="replace")
    except OSError as error:
        return tool_failure("file_read_error", f"failed to read file: {resolved}: {error}")

    # The matcher tries exact, then newline/Unicode-normalized, then whitespace-
    # tolerant line matching, always splicing the real original bytes.
    result = replace_fuzzy(content, old_string, new_string, replace_all=replace_all)
    if result is None:
        return _text_not_found_failure(old_string)
    if isinstance(result, AmbiguousFuzzyMatch):
        return tool_failure(
            "ambiguous_match",
            _format_ambiguous_match_error(result.occurrences, result.line_numbers),
        )

    if result.new_content == content:
        return tool_failure("no_changes", "replacement produced no changes")

    try:
        resolved.write_bytes(result.new_content.encode("utf-8"))
    except OSError as error:
        return tool_failure("file_write_error", f"failed to write file: {resolved}: {error}")

    # The edit is an implicit read of the new content: restamp so the same session
    # can edit again without re-reading, and so the next stale check is accurate.
    if file_state is not None:
        file_state.record_read(context.session_id, resolved)

    # Non-blocking: the edit is already written. The warning reports only a syntax
    # break this edit introduced, never a pre-existing one (see syntax_check).
    syntax_warning = warning_for_edited_file(resolved, content, result.new_content)
    return tool_success(
        _build_success_data(
            resolved, result.first_changed_line, result.replacements, syntax_warning
        )
    )


def make_edit_handler(file_state: FileReadState) -> ToolHandler:
    """Create an edit handler bound to the read-before-write guard registry."""

    def edit_handler_bound(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return edit_handler(context, arguments, file_state=file_state)

    return edit_handler_bound


def register_edit_tool(registry: ToolRegistry, *, file_state: FileReadState) -> None:
    """Register the edit tool with a vBot tool registry."""
    registry.register(
        EDIT_TOOL_NAME,
        EDIT_TOOL_DESCRIPTION,
        EDIT_TOOL_PARAMETERS,
        make_edit_handler(file_state),
        display=ToolDisplay(
            summary_fields=("path",),
            hidden_argument_keys=("newString", "new_string", "oldString", "old_string"),
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
