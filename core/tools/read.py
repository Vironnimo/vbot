"""Built-in ``read`` tool for reading text files from disk."""

from __future__ import annotations

from pathlib import Path

from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure, tool_success

READ_TOOL_NAME = "read"
READ_TOOL_DESCRIPTION = "Read a text file from disk. Relative paths resolve from the workspace."
READ_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "offset": {
            "type": "number",
            "description": "1-indexed line number to start reading from.",
        },
        "limit": {"type": "number"},
        "description": {
            "type": "string",
            "description": "Brief description of what this tool call is doing",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}

DEFAULT_LINE_LIMIT = 2000
MAX_FILE_BYTES = 50 * 1024  # 50 KB

INVALID_ARGUMENTS_CODE = "invalid_arguments"
NOT_FOUND_CODE = "not_found"
NOT_FILE_CODE = "not_file"
READ_FAILED_CODE = "read_failed"
SUPPORTED_ARGUMENTS = frozenset(READ_TOOL_PARAMETERS["properties"])


def register_builtin_tools(registry: ToolRegistry) -> None:
    """Register all built-in tools with the provided registry."""
    registry.register(
        name=READ_TOOL_NAME,
        description=READ_TOOL_DESCRIPTION,
        parameters=READ_TOOL_PARAMETERS,
        handler=read_handler,
    )


def read_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    """Read a UTF-8 text file and return a stable tool result envelope."""
    parsed_arguments = _parse_arguments(arguments)
    if isinstance(parsed_arguments, dict):
        return parsed_arguments

    path_argument, offset, limit = parsed_arguments
    path = _resolve_path(context.workspace, path_argument)

    try:
        if not path.exists():
            return tool_failure(NOT_FOUND_CODE, "File not found")
        if not path.is_file():
            return tool_failure(NOT_FILE_CODE, "Path is not a file")

        raw = path.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
    except OSError as error:
        return tool_failure(READ_FAILED_CODE, f"Could not read file: {error}")

    all_lines = raw.splitlines(keepends=True)
    total_lines = len(all_lines)

    if total_lines == 0:
        return tool_success({"content": ""})

    start_line = offset if offset is not None else 1
    max_lines = limit if limit is not None else DEFAULT_LINE_LIMIT

    if start_line > total_lines:
        notice = (
            f"[Offset {start_line} is beyond end of file ({total_lines} lines). Nothing to show.]"
        )
        return tool_success({"content": notice})

    selected_lines = all_lines[start_line - 1 : start_line - 1 + max_lines]
    line_limited = (start_line - 1 + len(selected_lines)) < total_lines

    output, lines_kept, byte_limited = _fit_lines_within_byte_limit(selected_lines, MAX_FILE_BYTES)

    truncated = line_limited or byte_limited

    if not truncated:
        return tool_success({"content": output})

    shown_start = start_line
    shown_end = start_line - 1 + lines_kept
    byte_truncation_note = "Output truncated at 50 KB. " if byte_limited else ""

    if shown_end < total_lines:
        hint = (
            f"[Showing lines {shown_start}-{shown_end} of {total_lines}. "
            f"{byte_truncation_note}Use offset={shown_end + 1} to continue.]"
        )
    else:
        hint = f"[Showing lines {shown_start}-{shown_end} of {total_lines}. {byte_truncation_note}]"

    if output:
        separator = "\n" if output.endswith("\n") else "\n\n"
        content = output + separator + hint
    else:
        content = hint

    return tool_success({"content": content})


def _fit_lines_within_byte_limit(lines: list[str], max_bytes: int) -> tuple[str, int, bool]:
    """Return (output, lines_kept, was_truncated)."""
    if not lines or max_bytes <= 0:
        return "", 0, bool(lines)
    kept_lines: list[str] = []
    used_bytes = 0
    for line in lines:
        encoded_line = line.encode("utf-8")
        if kept_lines and used_bytes + len(encoded_line) > max_bytes:
            break
        if not kept_lines and len(encoded_line) > max_bytes:
            # single line exceeds budget — truncate it
            truncated = line.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
            return truncated, 1, True
        if used_bytes + len(encoded_line) > max_bytes:
            break
        kept_lines.append(line)
        used_bytes += len(encoded_line)
    was_truncated = len(kept_lines) < len(lines)
    return "".join(kept_lines), len(kept_lines), was_truncated


def _parse_arguments(arguments: JsonObject) -> tuple[str, int | None, int | None] | JsonObject:
    extra_arguments = sorted(set(arguments) - SUPPORTED_ARGUMENTS)
    if extra_arguments:
        return tool_failure(
            INVALID_ARGUMENTS_CODE,
            f"Unsupported argument(s): {', '.join(extra_arguments)}",
        )

    path = arguments.get("path")
    if not isinstance(path, str) or not path:
        return tool_failure(INVALID_ARGUMENTS_CODE, "path must be a non-empty string")

    offset_result = _coerce_positive_int(arguments.get("offset"), "offset")
    if isinstance(offset_result, dict):
        return offset_result

    limit_result = _coerce_positive_int(arguments.get("limit"), "limit")
    if isinstance(limit_result, dict):
        return limit_result

    return path, offset_result, limit_result


def _coerce_positive_int(value: object, field_name: str) -> int | None | JsonObject:
    if value is None:
        return None
    if isinstance(value, bool):
        return tool_failure(INVALID_ARGUMENTS_CODE, f"{field_name} must be a positive integer")
    if isinstance(value, float):
        if not value.is_integer():
            return tool_failure(INVALID_ARGUMENTS_CODE, f"{field_name} must be a positive integer")
        value = int(value)
    if not isinstance(value, int):
        return tool_failure(INVALID_ARGUMENTS_CODE, f"{field_name} must be a positive integer")
    if value < 1:
        return tool_failure(INVALID_ARGUMENTS_CODE, f"{field_name} must be a positive integer")
    return value


def _resolve_path(workspace: Path, path_argument: str) -> Path:
    path = Path(path_argument)
    if not path.is_absolute():
        path = workspace / path

    return path.resolve(strict=False)


__all__ = [
    "READ_TOOL_DESCRIPTION",
    "READ_TOOL_NAME",
    "READ_TOOL_PARAMETERS",
    "read_handler",
    "register_builtin_tools",
]
