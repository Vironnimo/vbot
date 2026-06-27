"""Built-in write tool adapted for vBot tool envelopes."""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.tools.arguments import looks_like_line_numbered_content
from core.tools.file_state import FileReadState, stale_failure_text
from core.tools.syntax_check import warning_for_written_file
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolHandler,
    ToolRegistry,
    tool_failure,
    tool_success,
)

# UTF-8 BOM (byte form for detecting an existing file's marker, char form for
# re-adding it). A full-file write preserves a BOM the file already had so the
# round-trip with the BOM-stripping read tool does not silently drop it.
_UTF8_BOM_BYTES = b"\xef\xbb\xbf"
_UTF8_BOM = chr(0xFEFF)

WRITE_TOOL_NAME = "write"
WRITE_TOOL_DESCRIPTION = (
    "Write the full contents of a file. Creates the file if it does not "
    "exist, and replaces the entire file if it does. Not for partial "
    "edits or appending. Automatically creates parent directories. If the "
    "file already exists you must read it first; this tool fails if you did "
    "not, or if it changed on disk since you last read it."
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


def _file_starts_with_bom(path: Path) -> bool:
    """Return whether an existing file begins with a UTF-8 BOM (reads 3 bytes)."""
    try:
        with path.open("rb") as handle:
            return handle.read(len(_UTF8_BOM_BYTES)) == _UTF8_BOM_BYTES
    except OSError:
        return False


def write_handler(
    context: ToolContext, arguments: JsonObject, *, file_state: FileReadState | None = None
) -> JsonObject:
    """Handle a write tool call and return a stable vBot result envelope.

    When ``file_state`` is supplied the read-before-write guard is active: an
    overwrite of an existing file is refused unless that file was read in this
    session and has not changed on disk since. A non-existent target (a new file)
    is always allowed. A successful write restamps the file so the same session
    can write again without re-reading.
    """
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

    # A new file is never stale; the guard only gates overwriting an existing one.
    if file_state is not None and resolved.exists():
        reason = file_state.check_stale(context.session_id, resolved)
        if reason is not None:
            return tool_failure(*stale_failure_text(reason, resolved))

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        # Preserve a BOM the existing file already had, so a full-file rewrite of
        # content the model read BOM-free does not silently drop the marker.
        payload = content_argument
        if _file_starts_with_bom(resolved) and not payload.startswith(_UTF8_BOM):
            payload = _UTF8_BOM + payload
        encoded = payload.encode("utf-8")
        resolved.write_bytes(encoded)
    except OSError as error:
        return tool_failure("file_write_error", f"failed to write file: {resolved}: {error}")

    # The write is an implicit read: restamp so the same session can write again
    # without re-reading, and so the next stale check compares against this write.
    if file_state is not None:
        file_state.record_read(context.session_id, resolved)

    byte_count = len(encoded)
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


def make_write_handler(file_state: FileReadState) -> ToolHandler:
    """Create a write handler bound to the read-before-write guard registry."""

    async def write_handler_async(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await asyncio.to_thread(write_handler, context, arguments, file_state=file_state)

    return write_handler_async


def register_write_tool(registry: ToolRegistry, *, file_state: FileReadState) -> None:
    """Register the write tool with a vBot tool registry."""
    registry.register(
        WRITE_TOOL_NAME,
        WRITE_TOOL_DESCRIPTION,
        WRITE_TOOL_PARAMETERS,
        make_write_handler(file_state),
        display=ToolDisplay(summary_fields=("path",), hidden_argument_keys=("content",)),
    )


__all__ = [
    "WRITE_TOOL_DESCRIPTION",
    "WRITE_TOOL_NAME",
    "WRITE_TOOL_PARAMETERS",
    "make_write_handler",
    "register_write_tool",
    "write_handler",
]
