"""Built-in read tool — ported from vControl."""

from __future__ import annotations

from pathlib import Path

from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure, tool_success

READ_NEW_TOOL_NAME = "read_new"
READ_NEW_TOOL_DESCRIPTION = (
    "Read the contents of a file. Output is truncated to 2000 lines or "
    "50 KB (whichever is hit first). If offset is past EOF, returns an "
    "explicit end-of-file notice. Use offset/limit for large files."
)
READ_NEW_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the file to read (relative or absolute).",
        },
        "offset": {
            "type": "number",
            "description": "Line number to start reading from (1-indexed).",
        },
        "limit": {
            "type": "number",
            "description": "Maximum number of lines to read.",
        },
        "description": {
            "type": "string",
            "description": "Brief description of what this tool call is doing",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}

DEFAULT_LINE_LIMIT = 2000
MAX_FILE_BYTES = 50 * 1024

_KNOWN_ARGUMENTS = frozenset({"path", "offset", "limit", "description"})


def register_read_new_tool(registry: ToolRegistry) -> None:
    """Register the read_new tool with the provided registry."""
    registry.register(
        name=READ_NEW_TOOL_NAME,
        description=READ_NEW_TOOL_DESCRIPTION,
        parameters=READ_NEW_TOOL_PARAMETERS,
        handler=read_new_handler,
    )


def read_new_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    """Read file content with offset/limit controls and truncation safeguards."""
    extras = sorted(set(arguments) - _KNOWN_ARGUMENTS)
    if extras:
        return tool_failure("invalid_arguments", f"Unsupported argument(s): {', '.join(extras)}")

    path_arg = arguments.get("path")
    if not isinstance(path_arg, str) or not path_arg:
        return tool_failure("invalid_arguments", "path must be a non-empty string")

    p = Path(path_arg)
    if not p.is_absolute():
        p = context.workspace / p
    resolved = p.resolve(strict=False)

    try:
        offset_raw = arguments.get("offset")
        limit_raw = arguments.get("limit")
        start_line = _coerce_positive_int(offset_raw, field_name="offset") or 1
        max_lines = _coerce_positive_int(limit_raw, field_name="limit") or DEFAULT_LINE_LIMIT
    except ValueError as exc:
        return tool_failure("invalid_arguments", str(exc))

    if not resolved.exists():
        return tool_failure("not_found", f"file not found: {resolved}")
    if not resolved.is_file():
        return tool_failure("not_file", f"path is not a file: {resolved}")

    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        return tool_failure("read_failed", f"could not read file: {exc}")

    decoded = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    all_lines = decoded.splitlines(keepends=True)
    total_lines = len(all_lines)

    if total_lines == 0:
        return tool_success({"content": ""})

    start_index = start_line - 1
    if start_index >= total_lines:
        notice = (
            f"[Offset {start_line} is beyond end of file ({total_lines} lines). Nothing to show.]"
        )
        return tool_success({"content": notice})

    selected_lines = all_lines[start_index : start_index + max_lines]
    line_limited = start_index + len(selected_lines) < total_lines

    output = "".join(selected_lines)
    output_bytes = output.encode("utf-8")
    byte_limited = len(output_bytes) > MAX_FILE_BYTES

    if not (line_limited or byte_limited):
        return tool_success({"content": output})

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

    content = output + ("\n\n" if output and not output.endswith("\n") else "") + hint
    return tool_success({"content": content})


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


__all__ = [
    "READ_NEW_TOOL_DESCRIPTION",
    "READ_NEW_TOOL_NAME",
    "READ_NEW_TOOL_PARAMETERS",
    "read_new_handler",
    "register_read_new_tool",
]
