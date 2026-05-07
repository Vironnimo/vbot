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
        "offset": {"type": "integer"},
        "limit": {"type": "integer"},
        "description": {
            "type": "string",
            "description": "Brief description of what this tool call is doing",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}

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

        content = path.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
    except OSError as error:
        return tool_failure(READ_FAILED_CODE, f"Could not read file: {error}")

    return tool_success(
        {
            "content": _slice_lines(content, offset, limit),
        }
    )


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

    offset_result = _parse_non_negative_integer(arguments, "offset")
    if isinstance(offset_result, dict):
        return offset_result

    limit_result = _parse_non_negative_integer(arguments, "limit")
    if isinstance(limit_result, dict):
        return limit_result

    return path, offset_result, limit_result


def _parse_non_negative_integer(arguments: JsonObject, name: str) -> int | None | JsonObject:
    value: object = arguments.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return tool_failure(INVALID_ARGUMENTS_CODE, f"{name} must be an integer")
    if value < 0:
        return tool_failure(INVALID_ARGUMENTS_CODE, f"{name} must be non-negative")

    return value


def _resolve_path(workspace: Path, path_argument: str) -> Path:
    path = Path(path_argument)
    if not path.is_absolute():
        path = workspace / path

    return path.resolve(strict=False)


def _slice_lines(content: str, offset: int | None, limit: int | None) -> str:
    if offset is None and limit is None:
        return content

    lines = content.splitlines(keepends=True)
    start = offset if offset is not None else 0
    end = None if limit is None else start + limit
    return "".join(lines[start:end])


__all__ = [
    "READ_TOOL_DESCRIPTION",
    "READ_TOOL_NAME",
    "READ_TOOL_PARAMETERS",
    "read_handler",
    "register_builtin_tools",
]
