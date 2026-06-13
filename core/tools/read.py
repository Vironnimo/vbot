"""Built-in read tool: text files plus image/audio/video media handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.attachments import AttachmentError, sniff_media_type
from core.model_tasks import SpeechError
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolHandler,
    ToolRegistry,
    tool_failure,
    tool_success,
)

MAX_FILE_BYTES = 50 * 1024
DEFAULT_LINE_LIMIT = 2000

READ_TOOL_NAME = "read"
READ_TOOL_DESCRIPTION = (
    "Read a file. Text files return their contents, truncated to 2000 lines or "
    "50 KB (whichever is hit first); use offset/limit for large files, and an "
    "offset past EOF returns an explicit end-of-file notice. Image files are "
    "shown to the model directly when it supports vision; audio files are "
    "transcribed to text; video files return a path note only."
)
READ_TOOL_PARAMETERS: JsonObject = {
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


def _read_file_text(raw: bytes, offset: object = None, limit: object = None) -> str:
    """Render file bytes as text with offset/limit controls and truncation safeguards."""
    start_line = _coerce_positive_int(offset, field_name="offset") or 1
    max_lines = _coerce_positive_int(limit, field_name="limit") or DEFAULT_LINE_LIMIT

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


def make_read_handler(attachment_store: Any, speech_service: Any) -> ToolHandler:
    """Create a read handler bound to the attachment store and speech service.

    Closes over the services so the text path stays dependency-free while images
    are promoted to attachments and audio is transcribed via speech-to-text.
    Mirrors the image-generation tool's factory pattern.
    """

    async def read_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
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
            raw = resolved.read_bytes()
        except OSError as error:
            return tool_failure("file_read_error", f"failed to read file: {resolved}: {error}")

        media_type = sniff_media_type(raw, resolved.name)
        if media_type.startswith("image/"):
            return _read_image(attachment_store, resolved, raw, media_type)
        if media_type.startswith("audio/"):
            return await _read_audio(speech_service, resolved, raw, media_type)
        if media_type.startswith("video/"):
            return _read_video(resolved, media_type)
        return _read_text(raw, arguments)

    return read_handler


def _read_text(raw: bytes, arguments: JsonObject) -> JsonObject:
    """Return the text-rendering envelope for non-media (text/unknown) files."""
    try:
        content = _read_file_text(
            raw,
            offset=arguments.get("offset"),
            limit=arguments.get("limit"),
        )
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))

    return tool_success({"content": content})


def _read_image(
    attachment_store: Any,
    resolved: Path,
    raw: bytes,
    media_type: str,
) -> JsonObject:
    """Promote an image to an attachment and signal it for current-turn injection.

    The blob goes through the attachment store (reusing its size limit and
    allowlist); a ``read_media`` artifact tells the chat loop to inject the image
    as a synthetic current-turn user message so a vision model actually sees it.
    """
    try:
        record = attachment_store.store(resolved.name, raw)
    except AttachmentError as error:
        return tool_failure("attachment_error", str(error))

    return tool_success(
        {
            "content": (
                f"Loaded image {record.filename} ({record.media_type}) — "
                "shown to you in the following message."
            )
        },
        artifacts=[
            {
                "kind": "read_media",
                "attachment_id": record.id,
                "filename": record.filename,
                "media_type": record.media_type,
            }
        ],
    )


async def _read_audio(
    speech_service: Any,
    resolved: Path,
    raw: bytes,
    media_type: str,
) -> JsonObject:
    """Transcribe an audio file to text via speech-to-text.

    Transcription is plain text, which is a legal tool result on every provider,
    so no message injection is needed. STT failures and empty transcriptions
    surface as a failure envelope rather than aborting the run.
    """
    try:
        result = await speech_service.transcribe(raw, filename=resolved.name, media_type=media_type)
    except SpeechError as error:
        return tool_failure("transcription_failed", str(error))

    text = getattr(result, "text", None)
    if not isinstance(text, str) or not text.strip():
        return tool_failure(
            "transcription_failed",
            f"transcription produced no text for {resolved.name}",
        )

    return tool_success({"content": f"[Transcription of {resolved.name} ({media_type})]:\n{text}"})


def _read_video(resolved: Path, media_type: str) -> JsonObject:
    """Return a path note for video; no provider wire accepts raw video."""
    return tool_success(
        {
            "content": (
                f"[Video: {resolved.name} ({media_type}) — Path: {resolved}]. "
                "This model cannot view video directly."
            )
        }
    )


def register_read_tool(
    registry: ToolRegistry,
    *,
    attachment_store: Any,
    speech_service: Any,
) -> None:
    """Register the read tool with a vBot tool registry."""
    registry.register(
        READ_TOOL_NAME,
        READ_TOOL_DESCRIPTION,
        READ_TOOL_PARAMETERS,
        make_read_handler(attachment_store, speech_service),
        display=ToolDisplay(summary_fields=("path",)),
    )


__all__ = [
    "DEFAULT_LINE_LIMIT",
    "MAX_FILE_BYTES",
    "READ_TOOL_DESCRIPTION",
    "READ_TOOL_NAME",
    "READ_TOOL_PARAMETERS",
    "make_read_handler",
    "register_read_tool",
]
