"""Built-in read2 tool adapted for vBot tool envelopes."""

from __future__ import annotations

from pathlib import Path

from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure, tool_success

MAX_FILE_BYTES = 50 * 1024
DEFAULT_LINE_LIMIT = 2000

READ2_TOOL_NAME = "read2"
READ2_TOOL_DESCRIPTION = (
    "Read the contents of a file. Output is truncated to 2000 lines or "
    "50 KB (whichever is hit first). If offset is past EOF, returns an "
    "explicit end-of-file notice. Use offset/limit for large files."
)
READ2_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the file to read (relative to workspace, or absolute).",
        },
        "offset": {
            "type": "number",
            "description": "Line number to start reading from (1-indexed).",
        },
        "limit": {
            "type": "number",
            "description": "Maximum number of lines to read.",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}


def _coerce_positive_int(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None

    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")

    if isinstance(value, int):
        coerced = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{field_name} must be a positive integer")
        coerced = int(value)
    else:
        raise ValueError(f"{field_name} must be a positive integer")

    if coerced < 1:
        raise ValueError(f"{field_name} must be >= 1")

    return coerced


def _truncate_utf8(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def _fit_lines_within_byte_limit(lines: list[str], max_bytes: int) -> tuple[str, int]:
    if not lines or max_bytes <= 0:
        return "", 0

    kept_lines: list[str] = []
    used_bytes = 0

    for line in lines:
        encoded_line = line.encode("utf-8")
        if kept_lines and used_bytes + len(encoded_line) > max_bytes:
            break
        if not kept_lines and len(encoded_line) > max_bytes:
            return _truncate_utf8(line, max_bytes), 1
        if used_bytes + len(encoded_line) > max_bytes:
            break
        kept_lines.append(line)
        used_bytes += len(encoded_line)

    return "".join(kept_lines), len(kept_lines)


def _build_read_hint(
    shown_start: int,
    shown_end: int,
    total_lines: int,
    *,
    byte_limited: bool,
) -> str:
    message = f"[Showing lines {shown_start}-{shown_end} of {total_lines}."
    if byte_limited:
        message += " Output truncated at 50 KB."
    if shown_end < total_lines:
        message += f" Use offset={shown_end + 1} to continue."
    return message + "]"


def _resolve_read_path(context: ToolContext, path: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (context.workspace / candidate).resolve()


def _read_file_text(path: Path, offset: object = None, limit: object = None) -> str:
    """Read file content with offset/limit controls and truncation safeguards."""
    start_line = _coerce_positive_int(offset, field_name="offset") or 1
    max_lines = _coerce_positive_int(limit, field_name="limit") or DEFAULT_LINE_LIMIT

    raw = path.read_bytes()
    decoded = raw.decode("utf-8", errors="replace")
    all_lines = decoded.splitlines(keepends=True)
    total_lines = len(all_lines)

    if total_lines == 0:
        return ""

    start_index = start_line - 1
    if start_index >= total_lines:
        return (
            f"[Offset {start_line} is beyond end of file ({total_lines} lines). Nothing to show.]"
        )

    selected_lines = all_lines[start_index : start_index + max_lines]
    line_limited = start_index + len(selected_lines) < total_lines

    output = "".join(selected_lines)
    output_bytes = output.encode("utf-8")
    byte_limited = len(output_bytes) > MAX_FILE_BYTES

    if not (line_limited or byte_limited):
        return output

    shown_line_count = len(selected_lines)
    if byte_limited:
        provisional_count = max(1, min(len(selected_lines), shown_line_count))
        while True:
            provisional_end = min(total_lines, start_line + provisional_count - 1)
            hint = _build_read_hint(
                start_line,
                provisional_end,
                total_lines,
                byte_limited=True,
            )
            reserved_bytes = len(hint.encode("utf-8")) + 2
            available_bytes = max(MAX_FILE_BYTES - reserved_bytes, 0)
            output, fitted_count = _fit_lines_within_byte_limit(selected_lines, available_bytes)
            if fitted_count == provisional_count:
                shown_line_count = fitted_count
                break
            provisional_count = max(1, fitted_count)

    if shown_line_count == 0 and output:
        shown_line_count = 1
    shown_start = start_line
    shown_end = min(total_lines, shown_start + max(shown_line_count, 0) - 1)
    hint = _build_read_hint(
        shown_start,
        shown_end,
        total_lines,
        byte_limited=byte_limited,
    )

    return output + ("\n\n" if output and not output.endswith("\n") else "") + hint


def read2_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    """Handle a read2 tool call and return a stable vBot result envelope."""
    path_argument = arguments.get("path")
    if not isinstance(path_argument, str) or not path_argument:
        return tool_failure("invalid_arguments", "path must be a non-empty string")

    unknown_arguments = set(arguments) - {"path", "offset", "limit"}
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

    try:
        resolved = _resolve_read_path(context, path_argument)
    except RuntimeError as error:
        return tool_failure("invalid_path", str(error))

    if not resolved.exists():
        return tool_failure("file_not_found", f"file not found: {resolved}")
    if not resolved.is_file():
        return tool_failure("not_a_file", f"path is not a file: {resolved}")

    try:
        content = _read_file_text(
            resolved,
            offset=arguments.get("offset"),
            limit=arguments.get("limit"),
        )
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))
    except OSError as error:
        return tool_failure("file_read_error", f"failed to read file: {resolved}: {error}")

    data: JsonObject = {
        "path": str(resolved),
        "content": content,
    }
    return tool_success(data)


def register_read2_tool(registry: ToolRegistry) -> None:
    """Register the read2 tool with a vBot tool registry."""
    registry.register(
        READ2_TOOL_NAME,
        READ2_TOOL_DESCRIPTION,
        READ2_TOOL_PARAMETERS,
        read2_handler,
    )


__all__ = [
    "DEFAULT_LINE_LIMIT",
    "MAX_FILE_BYTES",
    "READ2_TOOL_DESCRIPTION",
    "READ2_TOOL_NAME",
    "READ2_TOOL_PARAMETERS",
    "read2_handler",
    "register_read2_tool",
]
