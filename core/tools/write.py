"""Built-in write tool adapted for vBot tool envelopes."""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.tools.arguments import looks_like_line_numbered_content
from core.tools.syntax_check import warning_for_written_file
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)

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
    return (context.effective_cwd / candidate).resolve()


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

    if looks_like_line_numbered_content(content_argument):
        return tool_failure(
            "line_numbered_content",
            "content looks like read's `N|` line-number gutter pasted back in. "
            "Write the raw file text without the leading line numbers.",
        )

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
    data: JsonObject = {
        "path": str(resolved),
        "bytes": byte_count,
        "message": message,
    }
    # Non-blocking: the file is already written. A syntax warning only tells the
    # model it just broke the file so it can fix it next turn.
    warning = warning_for_written_file(resolved, content_argument)
    if warning is not None:
        data["syntax_warning"] = warning
    return tool_success(data)


async def _write_handler_async(context: ToolContext, arguments: JsonObject) -> JsonObject:
    return await asyncio.to_thread(write_handler, context, arguments)


def register_write_tool(registry: ToolRegistry) -> None:
    """Register the write tool with a vBot tool registry."""
    registry.register(
        WRITE_TOOL_NAME,
        WRITE_TOOL_DESCRIPTION,
        WRITE_TOOL_PARAMETERS,
        _write_handler_async,
        display=ToolDisplay(summary_fields=("path",), hidden_argument_keys=("content",)),
    )


__all__ = [
    "WRITE_TOOL_DESCRIPTION",
    "WRITE_TOOL_NAME",
    "WRITE_TOOL_PARAMETERS",
    "register_write_tool",
    "write_handler",
]
