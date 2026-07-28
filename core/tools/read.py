"""Built-in read tool: text files plus image/audio/video media handling."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.attachments import AttachmentError, sniff_media_type
from core.model_tasks import SpeechError
from core.tools.arguments import LINE_NUMBER_GUTTER_SEPARATOR, optional_int
from core.tools.file_state import FileReadState
from core.tools.read_extract import (
    ExtractionError,
    ExtractionLimitExceededError,
    detect_extractable_document,
    document_label,
    ensure_document_input_size,
    extract_document_text,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolHandler,
    ToolRegistry,
    read_media_artifact,
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
_FILE_PROBE_BYTES = 64 * 1024
_TEXT_STREAM_CHUNK_CHARACTERS = 64 * 1024
_LINE_BREAK_PATTERN = re.compile(r"\r\n|[\n\v\f\x1c-\x1e\x85\u2028\u2029\r]")

READ_TOOL_NAME = "read"
READ_TOOL_DESCRIPTION = (
    "Read a file. Text files return their contents with every line prefixed by "
    "its number as `N|` — a reference gutter only; never reproduce it when "
    "writing or editing. Output is truncated to 2000 lines or 50 KB; use "
    "offset/limit for large files. Image files are shown to the model directly "
    "when it supports vision; audio files are transcribed to text; "
    "PDF/Word/Excel/Jupyter files (.pdf/.docx/.xlsx/.ipynb) are extracted to "
    "readable text; video and other binary files return a short notice."
)
READ_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Path to the file to read (relative to the working directory, or absolute)."
            ),
        },
        "offset": {
            "oneOf": [
                {"type": "integer", "minimum": 1},
                {
                    "type": "string",
                    "pattern": r"^[1-9][0-9]*:[1-9][0-9]*$",
                },
            ],
            "description": "Line number to start reading from (1-indexed).",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum number of lines to read.",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class _ReadPosition:
    """A 1-indexed source position, optionally inside one physical line."""

    line: int
    character: int = 1


class _FileInputTooLargeError(Exception):
    """Raised when a bounded full-file consumer reaches its input ceiling."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        super().__init__(f"file exceeds input limit {max_bytes}")


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
    total_lines: int | None,
    *,
    byte_limited: bool,
    continuation_offset: str | None = None,
) -> str:
    message = f"[Showing lines {shown_start}-{shown_end}"
    if total_lines is not None:
        message += f" of {total_lines}"
    message += "."
    if byte_limited:
        message += " Output truncated at 50 KB."
    if continuation_offset is not None:
        message += f" Use offset={continuation_offset} to continue."
    elif total_lines is None or shown_end < total_lines:
        message += f" Use offset={shown_end + 1} to continue."
    return message + "]"


def _line_gutter(line: int, character: int = 1) -> str:
    """Return a display gutter for a full line or an in-line continuation."""
    if character == 1:
        return f"{line}{LINE_NUMBER_GUTTER_SEPARATOR}"
    return f"{line}:{character}{LINE_NUMBER_GUTTER_SEPARATOR}"


def _add_line_numbers(lines: list[str], start_line: int, start_character: int = 1) -> list[str]:
    """Prefix each line with a compact ``N|`` reference gutter.

    The gutter is deliberately unpadded: padding to a fixed width is pure token
    overhead on dense source, while dropping the numbers entirely makes the model
    hand-count lines and miss by one. Each input line keeps its trailing newline
    (``keepends``); the number goes in front, file-absolute from ``start_line``.
    """
    return [
        f"{_line_gutter(start_line + index, start_character if index == 0 else 1)}{line}"
        for index, line in enumerate(lines)
    ]


def render_text_file(raw: bytes, offset: object = None, limit: object = None) -> str:
    """Render file bytes as numbered text with offset/limit controls and truncation.

    Text attachments call this same renderer before entering a provider request, so
    accepting a file through a channel has exactly the same 50 KiB, 2,000-line,
    and continuation behavior as an explicit ``read`` call.
    """
    position = _parse_read_position(offset)
    max_lines = optional_int(limit, field_name="limit", minimum=1) or DEFAULT_LINE_LIMIT

    if raw.startswith(_UTF8_BOM_BYTES):
        raw = raw[len(_UTF8_BOM_BYTES) :]
    decoded = raw.decode("utf-8", errors="replace")
    return _render_text(
        decoded,
        position.line,
        max_lines,
        number=True,
        start_character=position.character,
    )


def _parse_read_position(offset: object) -> _ReadPosition:
    """Parse a normal line offset or an in-line continuation address."""
    if isinstance(offset, str) and ":" in offset:
        parts = offset.split(":")
        if len(parts) != 2:
            raise ValueError("offset must be a line number or line:character address")
        if not parts[0].isdigit():
            raise ValueError("offset line must be an integer")
        if not parts[1].isdigit():
            raise ValueError("offset character must be an integer")
        line = int(parts[0])
        character = int(parts[1])
        if line < 1:
            raise ValueError("offset line must be >= 1")
        if character < 1:
            raise ValueError("offset character must be >= 1")
        return _ReadPosition(line, character)

    line = optional_int(offset, field_name="offset", minimum=1) or 1
    return _ReadPosition(line)


def _render_text(
    text: str,
    start_line: int,
    max_lines: int,
    *,
    number: bool,
    start_character: int = 1,
) -> str:
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
    source_line = all_lines[start_index]
    if start_character > len(source_line):
        return (
            f"[Character offset {start_character} is beyond end of line {start_line}. "
            "Nothing to show.]"
        )

    selected_lines = all_lines[start_index : start_index + max_lines]
    selected_lines[0] = selected_lines[0][start_character - 1 :]
    line_limited = start_index + len(selected_lines) < total_lines

    # Number before any byte fitting so the gutter counts against the 50 KB
    # budget and the model can cite/patch lines without hand-counting.
    rendered_lines = (
        _add_line_numbers(selected_lines, start_line, start_character) if number else selected_lines
    )
    output = "".join(rendered_lines)
    byte_limited = len(output.encode("utf-8")) > MAX_FILE_BYTES

    if not (line_limited or byte_limited):
        return output

    return _finalize_limited_text(
        rendered_lines,
        start_line=start_line,
        start_character=start_character,
        total_lines=total_lines,
        byte_limited=byte_limited,
        number=number,
    )


def _finalize_limited_text(
    rendered_lines: list[str],
    *,
    start_line: int,
    start_character: int,
    total_lines: int | None,
    byte_limited: bool,
    number: bool,
) -> str:
    """Fit rendered lines and append a continuation hint."""
    output = "".join(rendered_lines)

    shown_line_count = len(rendered_lines)
    continuation_offset: str | None = None
    if byte_limited:
        long_first_line = len(rendered_lines[0].encode("utf-8")) > MAX_FILE_BYTES
        provisional_count = max(1, min(len(rendered_lines), shown_line_count))
        while True:
            provisional_end = start_line + provisional_count - 1
            if total_lines is not None:
                provisional_end = min(total_lines, provisional_end)
            possible_continuation = (
                f"{start_line}:{start_character + MAX_FILE_BYTES}" if long_first_line else None
            )
            hint = _build_read_hint(
                start_line,
                provisional_end,
                total_lines,
                byte_limited=True,
                continuation_offset=possible_continuation,
            )
            reserved_bytes = len(hint.encode("utf-8")) + 2
            available_bytes = max(MAX_FILE_BYTES - reserved_bytes, 0)
            output, fitted_count = _fit_lines_within_byte_limit(rendered_lines, available_bytes)
            if fitted_count == provisional_count:
                shown_line_count = fitted_count
                first_line_was_cut = (
                    fitted_count == 1 and len(rendered_lines[0].encode("utf-8")) > available_bytes
                )
                if first_line_was_cut:
                    gutter = _line_gutter(start_line, start_character) if number else ""
                    shown_source = output[len(gutter) :]
                    continuation_offset = f"{start_line}:{start_character + len(shown_source)}"
                break
            provisional_count = max(1, fitted_count)

    if shown_line_count == 0 and output:
        shown_line_count = 1
    shown_start = start_line
    shown_end = shown_start + max(shown_line_count, 0) - 1
    if total_lines is not None:
        shown_end = min(total_lines, shown_end)
    hint = _build_read_hint(
        shown_start,
        shown_end,
        total_lines,
        byte_limited=byte_limited,
        continuation_offset=continuation_offset,
    )

    return output + ("\n\n" if output and not output.endswith("\n") else "") + hint


def _split_stream_fragments(
    text: str, *, final: bool = False
) -> tuple[list[tuple[str, bool]], str]:
    """Split a bounded decoded chunk into line fragments without retaining a long line."""
    held_carriage_return = ""
    if not final and text.endswith("\r"):
        text = text[:-1]
        held_carriage_return = "\r"

    fragments: list[tuple[str, bool]] = []
    start = 0
    for match in _LINE_BREAK_PATTERN.finditer(text):
        fragments.append((text[start : match.end()], True))
        start = match.end()
    if start < len(text):
        fragments.append((text[start:], False))
    return fragments, held_carriage_return


def _render_text_path(resolved: Path, arguments: JsonObject) -> str:
    """Render a local text file with bounded memory and early truncation."""
    position = _parse_read_position(arguments.get("offset"))
    max_lines = (
        optional_int(arguments.get("limit"), field_name="limit", minimum=1) or DEFAULT_LINE_LIMIT
    )
    rendered_lines: list[str] = []
    rendered_bytes = 0
    source_line = 1
    source_character = 1
    completed_source_lines = 0
    current_source_line_has_content = False
    target_line_seen = False
    target_character_reached = False
    character_offset_beyond_end = False
    current_selected_line_started = False
    selected_lines_completed = 0
    selected_window_complete = False
    line_limited = False
    byte_limited = False
    held_carriage_return = ""
    first_chunk = True

    def append_bounded(text: str) -> bool:
        nonlocal rendered_bytes
        if not text:
            return True
        remaining_bytes = MAX_FILE_BYTES + 1 - rendered_bytes
        if remaining_bytes <= 0:
            return False
        kept = _truncate_utf8(text, remaining_bytes)
        rendered_lines[-1] += kept
        rendered_bytes += len(kept.encode("utf-8"))
        return kept == text

    def process_fragment(fragment: str, *, ends_line: bool) -> bool:
        nonlocal byte_limited
        nonlocal character_offset_beyond_end
        nonlocal completed_source_lines
        nonlocal current_source_line_has_content
        nonlocal current_selected_line_started
        nonlocal line_limited
        nonlocal selected_lines_completed
        nonlocal selected_window_complete
        nonlocal source_character
        nonlocal source_line
        nonlocal target_character_reached
        nonlocal target_line_seen

        if not fragment:
            return True
        if selected_window_complete:
            line_limited = True
            return False

        current_source_line_has_content = True
        if source_line == position.line:
            target_line_seen = True

        selected_fragment = ""
        if source_line >= position.line:
            required_character = position.character if source_line == position.line else 1
            skip_characters = max(required_character - source_character, 0)
            if skip_characters < len(fragment):
                selected_fragment = fragment[skip_characters:]
                if source_line == position.line:
                    target_character_reached = True
                if not current_selected_line_started:
                    current_selected_line_started = True
                    rendered_lines.append(_line_gutter(source_line, required_character))
                if not append_bounded(selected_fragment) or rendered_bytes > MAX_FILE_BYTES:
                    byte_limited = True
                    return False

        source_character += len(fragment)
        if not ends_line:
            return True

        if source_line == position.line and not target_character_reached:
            character_offset_beyond_end = True
            return False
        if source_line >= position.line and current_selected_line_started:
            selected_lines_completed += 1
            if selected_lines_completed >= max_lines:
                selected_window_complete = True
        completed_source_lines += 1
        source_line += 1
        source_character = 1
        current_source_line_has_content = False
        current_selected_line_started = False
        return True

    with resolved.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        while not (line_limited or byte_limited or character_offset_beyond_end):
            chunk = handle.read(_TEXT_STREAM_CHUNK_CHARACTERS)
            if not chunk:
                break
            if first_chunk:
                first_chunk = False
                if chunk.startswith("\ufeff"):
                    chunk = chunk[1:]
                    if not chunk:
                        continue
            fragments, held_carriage_return = _split_stream_fragments(held_carriage_return + chunk)
            for fragment, ends_line in fragments:
                if not process_fragment(fragment, ends_line=ends_line):
                    break

        if (
            not (line_limited or byte_limited or character_offset_beyond_end)
            and held_carriage_return
        ):
            process_fragment(held_carriage_return, ends_line=True)

    if character_offset_beyond_end or (target_line_seen and not target_character_reached):
        return (
            f"[Character offset {position.character} is beyond end of line {position.line}. "
            "Nothing to show.]"
        )

    reached_eof = not (line_limited or byte_limited or character_offset_beyond_end)
    total_lines = (
        completed_source_lines + (1 if current_source_line_has_content else 0)
        if reached_eof
        else None
    )
    if total_lines == 0:
        return ""
    if not target_line_seen:
        return (
            f"[Offset {position.line} is beyond end of file ({total_lines or 0} lines). "
            "Nothing to show.]"
        )

    output = "".join(rendered_lines)
    if not (line_limited or byte_limited):
        return output
    return _finalize_limited_text(
        rendered_lines,
        start_line=position.line,
        start_character=position.character,
        total_lines=None,
        byte_limited=byte_limited,
        number=True,
    )


def _read_file_bytes_with_limit(resolved: Path, max_bytes: int) -> bytes:
    """Read at most one byte beyond a full-file consumer's input ceiling."""
    with resolved.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise _FileInputTooLargeError(max_bytes)
    return raw


def _attachment_input_limit(attachment_store: Any, file_size: int) -> int:
    """Run the AttachmentStore preflight and return a bounded read ceiling."""
    ensure_within_limit = getattr(attachment_store, "ensure_within_limit", None)
    if callable(ensure_within_limit):
        ensure_within_limit(file_size)
    configured_limit = getattr(attachment_store, "max_size_bytes", None)
    if (
        isinstance(configured_limit, int)
        and not isinstance(configured_limit, bool)
        and configured_limit > 0
    ):
        return configured_limit
    return max(file_size, 1)


def make_read_handler(
    attachment_store: Any,
    speech_service: Any,
    file_state: FileReadState,
    *,
    speech_max_size_bytes: int,
) -> ToolHandler:
    """Create a read handler bound to the attachment store and speech service.

    Closes over the services so the text path stays dependency-free while images
    are promoted to attachments and audio is transcribed via speech-to-text.
    Mirrors the image-generation tool's factory pattern. ``file_state`` records
    each read so the write/edit guard can detect unread or externally-changed
    files (see ``file_state.py``).
    """

    if (
        not isinstance(speech_max_size_bytes, int)
        or isinstance(speech_max_size_bytes, bool)
        or speech_max_size_bytes <= 0
    ):
        raise ValueError("speech_max_size_bytes must be a positive integer")

    async def read_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        path_argument = arguments.get("path")
        if not isinstance(path_argument, str) or not path_argument:
            return tool_failure("invalid_arguments", "path must be a non-empty string")

        unknown_arguments = set(arguments) - {"path", "offset", "limit"}
        if unknown_arguments:
            names = ", ".join(sorted(unknown_arguments))
            return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

        try:
            resolved = context.resolve_path(path_argument)
        except RuntimeError as error:
            return tool_failure("invalid_path", str(error))

        if not resolved.exists():
            return tool_failure("file_not_found", f"file not found: {resolved}")
        if not resolved.is_file():
            return tool_failure("not_a_file", f"path is not a file: {resolved}")

        # Stamp before reading bytes: if an external write lands in the tiny window
        # before the read, the stamp stays older than the new content, so the next
        # write/edit errs toward a (harmless) re-read rather than missing the change.
        file_state.record_read(context.session_id, resolved)

        try:
            file_size = resolved.stat().st_size
            with resolved.open("rb") as handle:
                probe = handle.read(_FILE_PROBE_BYTES)
        except OSError as error:
            return tool_failure("file_read_error", f"failed to read file: {resolved}: {error}")

        media_type = sniff_media_type(probe, resolved.name)
        if media_type.startswith("image/"):
            try:
                input_limit = _attachment_input_limit(attachment_store, file_size)
                raw = _read_file_bytes_with_limit(resolved, input_limit)
            except AttachmentError as error:
                return tool_failure("attachment_error", str(error))
            except _FileInputTooLargeError as error:
                return tool_failure(
                    "attachment_error",
                    f"Attachment size exceeds limit {error.max_bytes}",
                )
            except OSError as error:
                return tool_failure("file_read_error", f"failed to read file: {resolved}: {error}")
            return _read_image(attachment_store, resolved, raw, media_type)
        if media_type.startswith("audio/"):
            if file_size > speech_max_size_bytes:
                return tool_failure(
                    "audio_too_large",
                    f"Audio size {file_size} exceeds limit {speech_max_size_bytes}",
                )
            try:
                raw = _read_file_bytes_with_limit(resolved, speech_max_size_bytes)
            except _FileInputTooLargeError:
                return tool_failure(
                    "audio_too_large",
                    f"Audio size exceeds limit {speech_max_size_bytes}",
                )
            except OSError as error:
                return tool_failure("file_read_error", f"failed to read file: {resolved}: {error}")
            return await _read_audio(speech_service, resolved, raw, media_type)
        if media_type.startswith("video/"):
            return _read_video(resolved, media_type)
        # PDF/Office/notebook extraction runs before the binary check: pdf/docx/xlsx
        # are full of NUL bytes that would otherwise be dismissed as binary, and
        # ipynb is JSON that would dump as unreadable raw text.
        kind = detect_extractable_document(resolved.name, media_type)
        if kind is not None:
            try:
                input_limit = ensure_document_input_size(file_size)
                raw = _read_file_bytes_with_limit(resolved, input_limit)
            except (ExtractionLimitExceededError, _FileInputTooLargeError) as error:
                if isinstance(error, _FileInputTooLargeError):
                    try:
                        ensure_document_input_size(error.max_bytes + 1)
                    except ExtractionLimitExceededError as limit_error:
                        error = limit_error
                return tool_failure("document_too_large", str(error))
            except OSError as error:
                return tool_failure("file_read_error", f"failed to read file: {resolved}: {error}")
            extracted = _read_extracted_document(resolved.name, raw, kind, arguments)
            if extracted is not None:
                return extracted
            del raw
        if _looks_binary(probe):
            return _read_binary_notice(resolved)
        try:
            content = _render_text_path(resolved, arguments)
        except ValueError as error:
            return tool_failure("invalid_arguments", str(error))
        except OSError as error:
            return tool_failure("file_read_error", f"failed to read file: {resolved}: {error}")
        return tool_success({"content": content})

    return read_handler


def _read_extracted_document(
    name: str, raw: bytes, kind: str, arguments: JsonObject
) -> JsonObject | None:
    """Return rendered text for a PDF/Office/notebook file, or ``None`` to fall through.

    On a malformed document the extractor raises ``ExtractionError``; returning
    ``None`` then lets the caller fall back to the binary-notice / text path. The
    rendered text is numbered-gutter-free (it is a rendering, not editable source)
    but still passes through the shared line/byte truncation. An empty extraction
    (e.g. a scanned PDF with no text layer) becomes an explicit note.
    """
    try:
        extracted = extract_document_text(raw, kind)
    except ExtractionLimitExceededError as error:
        return tool_failure("document_too_large", str(error))
    except ExtractionError:
        return None

    try:
        position = _parse_read_position(arguments.get("offset"))
        max_lines = (
            optional_int(arguments.get("limit"), field_name="limit", minimum=1)
            or DEFAULT_LINE_LIMIT
        )
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))

    header = f"[Extracted text from {name} ({document_label(kind)})]:"
    body = _render_text(
        extracted,
        position.line,
        max_lines,
        number=False,
        start_character=position.character,
    )
    if not body.strip():
        body = "(no extractable text)"
    return tool_success({"content": f"{header}\n{body}"})


def _read_image(
    attachment_store: Any,
    resolved: Path,
    raw: bytes,
    media_type: str,
) -> JsonObject:
    """Promote an image to an attachment-backed rich Tool Result."""
    try:
        record = attachment_store.store(resolved.name, raw)
    except AttachmentError as error:
        return tool_failure("attachment_error", str(error))

    return tool_success(
        {"content": (f"Loaded image {record.filename} ({record.media_type}).")},
        artifacts=[
            read_media_artifact(
                attachment_id=record.id,
                filename=record.filename,
                media_type=record.media_type,
            )
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
    file_state: FileReadState,
    speech_max_size_bytes: int,
) -> None:
    """Register the read tool with a vBot tool registry."""
    registry.register(
        READ_TOOL_NAME,
        READ_TOOL_DESCRIPTION,
        READ_TOOL_PARAMETERS,
        make_read_handler(
            attachment_store,
            speech_service,
            file_state,
            speech_max_size_bytes=speech_max_size_bytes,
        ),
        result_schema={"type": "object", "required": ["content"]},
        display=ToolDisplay(summary_fields=("path",)),
        parallel_safe=True,
    )


__all__ = [
    "DEFAULT_LINE_LIMIT",
    "MAX_FILE_BYTES",
    "render_text_file",
    "READ_TOOL_DESCRIPTION",
    "READ_TOOL_NAME",
    "READ_TOOL_PARAMETERS",
    "make_read_handler",
    "register_read_tool",
]
