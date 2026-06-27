"""Built-in read tool: text files plus image/audio/video media handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.attachments import AttachmentError, sniff_media_type
from core.model_tasks import SpeechError
from core.tools.arguments import LINE_NUMBER_GUTTER_SEPARATOR, optional_int
from core.tools.read_extract import (
    ExtractionError,
    document_label,
    extract_document_text,
    is_extractable_document,
)
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
# UTF-8 BOM that some Windows editors prepend; stripped on read so the model sees
# clean content (the write tool preserves it on the round-trip).
_UTF8_BOM_BYTES = b"\xef\xbb\xbf"
# A NUL byte within this leading window marks a file as binary (the classic
# heuristic): text — even non-UTF-8 text shown with replacement chars — has none.
_BINARY_DETECTION_BYTES = 8192

READ_TOOL_NAME = "read"
READ_TOOL_DESCRIPTION = (
    "Read a file. Text files return their contents with every line prefixed by "
    "its number as `N|` — a reference gutter only; never reproduce it when "
    "writing or editing. Output is truncated to 2000 lines or 50 KB (whichever "
    "is hit first); use offset/limit for large files, and an offset past EOF "
    "returns an explicit end-of-file notice. Image files are shown to the model "
    "directly when it supports vision; audio files are transcribed to text; "
    "Word/Excel/Jupyter files (.docx/.xlsx/.ipynb) are extracted to readable "
    "text; video files return a path note only; binary files return a short "
    "notice instead of garbled text."
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


def _add_line_numbers(lines: list[str], start_line: int) -> list[str]:
    """Prefix each line with a compact ``N|`` reference gutter.

    The gutter is deliberately unpadded: padding to a fixed width is pure token
    overhead on dense source, while dropping the numbers entirely makes the model
    hand-count lines and miss by one. Each input line keeps its trailing newline
    (``keepends``); the number goes in front, file-absolute from ``start_line``.
    """
    return [
        f"{start_line + index}{LINE_NUMBER_GUTTER_SEPARATOR}{line}"
        for index, line in enumerate(lines)
    ]


def _resolve_read_path(context: ToolContext, path: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (context.effective_cwd / candidate).resolve()


def _read_file_text(raw: bytes, offset: object = None, limit: object = None) -> str:
    """Render file bytes as numbered text with offset/limit controls and truncation."""
    start_line = optional_int(offset, field_name="offset", minimum=1) or 1
    max_lines = optional_int(limit, field_name="limit", minimum=1) or DEFAULT_LINE_LIMIT

    if raw.startswith(_UTF8_BOM_BYTES):
        raw = raw[len(_UTF8_BOM_BYTES) :]
    decoded = raw.decode("utf-8", errors="replace")
    return _render_text(decoded, start_line, max_lines, number=True)


def _render_text(text: str, start_line: int, max_lines: int, *, number: bool) -> str:
    """Apply offset/limit, optional line numbering, and truncation safeguards.

    Shared by the literal-file path (``number=True`` adds the ``N|`` gutter) and
    the extracted-document path (``number=False`` — a rendering of an Office or
    notebook file is not editable source, so the gutter would only mislead).
    """
    all_lines = text.splitlines(keepends=True)
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

    # Number before any byte fitting so the gutter counts against the 50 KB
    # budget and the model can cite/patch lines without hand-counting.
    rendered_lines = _add_line_numbers(selected_lines, start_line) if number else selected_lines
    output = "".join(rendered_lines)
    output_bytes = output.encode("utf-8")
    byte_limited = len(output_bytes) > MAX_FILE_BYTES

    if not (line_limited or byte_limited):
        return output

    shown_line_count = len(rendered_lines)
    if byte_limited:
        provisional_count = max(1, min(len(rendered_lines), shown_line_count))
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
            output, fitted_count = _fit_lines_within_byte_limit(rendered_lines, available_bytes)
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
        # Office/notebook extraction runs before the binary check: docx/xlsx are
        # zip archives full of NUL bytes that would otherwise be dismissed as
        # binary, and ipynb is JSON that would dump as unreadable raw text.
        if is_extractable_document(resolved.name):
            extracted = _read_extracted_document(resolved, arguments)
            if extracted is not None:
                return extracted
        if _looks_binary(raw):
            return _read_binary_notice(resolved)
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


def _read_extracted_document(resolved: Path, arguments: JsonObject) -> JsonObject | None:
    """Return rendered text for an Office/notebook file, or ``None`` to fall through.

    On a malformed document the extractor raises ``ExtractionError``; returning
    ``None`` then lets the caller fall back to the binary-notice / text path. The
    rendered text is numbered-gutter-free (it is a rendering, not editable source)
    but still passes through the shared line/byte truncation.
    """
    try:
        extracted = extract_document_text(resolved)
    except ExtractionError:
        return None

    try:
        start_line = optional_int(arguments.get("offset"), field_name="offset", minimum=1) or 1
        max_lines = (
            optional_int(arguments.get("limit"), field_name="limit", minimum=1)
            or DEFAULT_LINE_LIMIT
        )
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))

    header = f"[Extracted text from {resolved.name} ({document_label(resolved.name)})]:"
    body = _render_text(extracted, start_line, max_lines, number=False)
    if not body.strip():
        body = "(no extractable text)"
    return tool_success({"content": f"{header}\n{body}"})


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


def _looks_binary(raw: bytes) -> bool:
    """Return whether the leading bytes contain a NUL, marking the file binary.

    Checked only after media routing, so image/audio/video files (which contain
    NUL bytes) are still handled by their own branches. A NUL is the reliable
    text/binary signal: text has none, binaries almost always do — including
    files that decode as valid UTF-8 but are really data.
    """
    return b"\x00" in raw[:_BINARY_DETECTION_BYTES]


def _read_binary_notice(resolved: Path) -> JsonObject:
    """Return a short notice for a binary file instead of decoding it to garbage."""
    return tool_success(
        {
            "content": (
                f"[Binary file: {resolved.name} — Path: {resolved}]. "
                "It contains non-text (binary) data and is not shown as text."
            )
        }
    )


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
