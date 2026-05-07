"""Built-in write tool adapted for vBot tool envelopes."""

from __future__ import annotations

from pathlib import Path

from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure, tool_success

WRITE_TOOL_NAME = "write"
WRITE_TOOL_DESCRIPTION = (
    "Write the full contents of a file. Creates the file if it does not "
    "exist, and replaces the entire file if it does. Not for partial "
    "edits or appending. Automatically creates parent directories."
)
WRITE_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the file to write (relative to workspace, or absolute).",
        },
        "content": {
            "type": "string",
            "description": "Content to write to the file.",
        },
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}


def _resolve_write_path(context: ToolContext, path: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (context.workspace / candidate).resolve()


def write_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    """Handle a write tool call and return a stable vBot result envelope."""
    unknown_arguments = set(arguments) - {"path", "content"}
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

    path_argument = arguments.get("path")
    if not isinstance(path_argument, str) or not path_argument:
        return tool_failure("invalid_arguments", "path must be a non-empty string")

    content_argument = arguments.get("content")
    if not isinstance(content_argument, str):
        return tool_failure("invalid_arguments", "content must be a string")

    try:
        resolved = _resolve_write_path(context, path_argument)
    except RuntimeError as error:
        return tool_failure("invalid_path", str(error))

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(content_argument.encode("utf-8"))
    except OSError as error:
        return tool_failure("file_write_error", f"failed to write file: {resolved}: {error}")

    byte_count = len(content_argument.encode("utf-8"))
    message = f"OK: written {byte_count} bytes to {resolved}"
    return tool_success(
        {
            "path": str(resolved),
            "bytes": byte_count,
            "message": message,
        }
    )


def register_write_tool(registry: ToolRegistry) -> None:
    """Register the write tool with a vBot tool registry."""
    registry.register(
        WRITE_TOOL_NAME,
        WRITE_TOOL_DESCRIPTION,
        WRITE_TOOL_PARAMETERS,
        write_handler,
    )


__all__ = [
    "WRITE_TOOL_DESCRIPTION",
    "WRITE_TOOL_NAME",
    "WRITE_TOOL_PARAMETERS",
    "register_write_tool",
    "write_handler",
]
