"""Built-in read tool: text files, directories, and image/audio/video media."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.attachments import AttachmentError, sniff_media_type
from core.model_tasks import SpeechError
from core.tools._path_suggestions import corrected_paths
from core.tools._read_arguments import READ_HIDDEN_PARAMETERS, normalize_read_arguments
from core.tools._read_text import (
    DEFAULT_LINE_LIMIT,
    MAX_FILE_BYTES,
    parse_read_position,
    render_directory_listing,
    render_matching_lines,
    render_text,
    render_text_file,
    render_text_path,
    without_bom,
)
from core.tools.arguments import optional_int, split_text_lines
from core.tools.contracts import ToolContractError
from core.tools.file_state import FileReadState
from core.tools.read_extract import (
    ExtractionError,
    ExtractionLimitExceededError,
    detect_extractable_document,
    document_label,
    ensure_document_input_size,
    extract_document_text,
)
from core.tools.search import display_search_path
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolHandler,
    ToolRegistry,
    run_tool_worker,
    tool_failure,
    tool_success,
)

# A NUL byte within this leading window marks a file as binary (the classic
# heuristic): text — even non-UTF-8 text shown with replacement chars — has none.
_BINARY_DETECTION_BYTES = 8192
_FILE_PROBE_BYTES = 64 * 1024


@dataclass(frozen=True)
class _PreparedAudio:
    resolved: Path
    raw: bytes
    media_type: str
    # Read stamp captured before the bytes; recorded after a successful transcription.
    stamp: tuple[float, int] | None = None


READ_TOOL_NAME = "read"
READ_TOOL_DESCRIPTION = (
    "Read a text file with line numbers, list a directory, view an image, transcribe "
    "an audio file, or extract the text of a PDF, Word, Excel or Jupyter file. Text "
    'lines start with "N| " (the line number, not file content). Use this instead '
    "of cat, head, tail, Get-Content or ls in the shell."
)
READ_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "minLength": 1,
            "description": "File or directory, relative to the working directory or absolute.",
        },
        "offset": {
            "oneOf": [
                {"type": "integer"},
                {"type": "string", "pattern": r"^[1-9][0-9]*:[1-9][0-9]*$"},
            ],
            "description": (
                "Line to start at, counting from 1. A negative number counts back from "
                "the end: -50 shows the last 50 lines."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum number of lines to show. Default 2000.",
        },
    },
    "required": ["path"],
}


class _FileInputTooLargeError(Exception):
    """Raised when a bounded full-file consumer reaches its input ceiling."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        super().__init__(f"file exceeds input limit {max_bytes}")


def _path_label(path: Path, cwd: Path) -> str:
    """Show a path relative to the working directory, absolute outside it."""
    return display_search_path(path, cwd=cwd)


def _call_cwd(context: ToolContext) -> Path:
    """Return the working directory in the resolved form that tool paths use."""
    try:
        return context.effective_cwd.resolve()
    except (OSError, RuntimeError):
        return context.effective_cwd


def _missing_file_message(resolved: Path, cwd: Path) -> str:
    """Build a not-found error that names paths the next call can use."""
    label = _path_label(resolved, cwd)
    suggestions = corrected_paths(resolved, cwd)
    if suggestions:
        similar = ", ".join(_path_label(candidate, cwd) for candidate in suggestions)
        return f"File not found: {label} (similar: {similar})."
    parent = resolved.parent
    if parent.is_dir():
        return f"File not found: {label}. Read {_path_label(parent, cwd)} to list that directory."
    return f"File not found: {label}. Its directory {_path_label(parent, cwd)} does not exist."


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
    are passed in memory and audio is transcribed via speech-to-text.
    Mirrors the image-generation tool's factory pattern. ``file_state`` records
    each read so the write guard can detect unread or externally-changed
    files (see ``file_state.py``). Reads take no part in change statistics:
    the tracker diffs every mutation against actual on-disk content.
    """

    if (
        not isinstance(speech_max_size_bytes, int)
        or isinstance(speech_max_size_bytes, bool)
        or speech_max_size_bytes <= 0
    ):
        raise ValueError("speech_max_size_bytes must be a positive integer")

    def prepare_read(context: ToolContext, arguments: JsonObject) -> JsonObject | _PreparedAudio:
        path_argument = arguments.get("path")
        if not isinstance(path_argument, str) or not path_argument:
            return tool_failure("invalid_arguments", "path must be a non-empty string")

        try:
            resolved = context.resolve_path(path_argument)
        except RuntimeError as error:
            return tool_failure("invalid_path", str(error))
        cwd = _call_cwd(context)
        label = _path_label(resolved, cwd)

        if not resolved.exists():
            return tool_failure("file_not_found", _missing_file_message(resolved, cwd))
        if resolved.is_dir():
            if arguments.get("pattern") is not None:
                return tool_failure(
                    "invalid_arguments", _directory_search_message(arguments, label)
                )
            return _list_directory(resolved, arguments, label)
        if not resolved.is_file():
            return tool_failure("not_a_file", f"{label} is neither a file nor a directory.")

        # Capture the stamp before reading bytes, but record it only after a
        # successful read: an external write landing during the read leaves the
        # stamp older than the new content, so the next full-file write errs toward
        # a (harmless) re-read, while a failed read never counts as seen.
        stamp = file_state.stamp(resolved)
        result = read_resolved(context, arguments, resolved, label)
        if isinstance(result, _PreparedAudio):
            return replace(result, stamp=stamp)
        if result.get("ok") is True and stamp is not None:
            file_state.record_stamp(context.session_id, resolved, stamp)
        return result

    def read_resolved(
        context: ToolContext, arguments: JsonObject, resolved: Path, label: str
    ) -> JsonObject | _PreparedAudio:
        def read_error(error: OSError) -> JsonObject:
            return tool_failure("file_read_error", f"Failed to read {label}: {error}")

        try:
            file_size = resolved.stat().st_size
            with resolved.open("rb") as handle:
                probe = handle.read(_FILE_PROBE_BYTES)
        except OSError as error:
            return read_error(error)

        media_type = sniff_media_type(probe, resolved.name)
        kind = detect_extractable_document(resolved.name, media_type)
        filtering = arguments.get("pattern") is not None
        if (
            filtering
            and kind is None
            and (media_type.startswith(("image/", "audio/", "video/")) or _looks_binary(probe))
        ):
            return tool_failure(
                "invalid_arguments",
                f"pattern selects text lines, but {label} is not a text file ({media_type}). "
                f"Read it without pattern.",
            )
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
                return read_error(error)
            return _read_image(context, resolved, raw, media_type)
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
                return read_error(error)
            return _PreparedAudio(resolved=resolved, raw=raw, media_type=media_type)
        if media_type.startswith("video/"):
            return _read_video(label, media_type)
        # PDF/Office/notebook extraction runs before the binary check: pdf/docx/xlsx
        # are full of NUL bytes that would otherwise be dismissed as binary, and
        # ipynb is JSON that would dump as unreadable raw text.
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
                return read_error(error)
            extracted = _read_extracted_document(resolved.name, raw, kind, arguments, label)
            if extracted is not None:
                return extracted
            del raw
        if _looks_binary(probe):
            if filtering:
                return tool_failure(
                    "invalid_arguments",
                    f"pattern selects text lines, but {label} is a binary file. "
                    "Read it without pattern.",
                )
            return _read_binary_notice(label)
        try:
            if filtering:
                with resolved.open("r", encoding="utf-8", errors="replace", newline="") as text:
                    content = render_matching_lines(without_bom(text), arguments, label)
            else:
                content = render_text_path(resolved, arguments)
        except ValueError as error:
            return tool_failure("invalid_arguments", str(error))
        except OSError as error:
            return read_error(error)
        return tool_success({"content": content})

    async def read_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        prepared = await run_tool_worker(prepare_read, context, arguments)
        if isinstance(prepared, _PreparedAudio):
            result = await _read_audio(
                speech_service,
                prepared.resolved,
                prepared.raw,
                prepared.media_type,
            )
            if result.get("ok") is True and prepared.stamp is not None:
                file_state.record_stamp(context.session_id, prepared.resolved, prepared.stamp)
            return result
        return prepared

    return read_handler


def _read_extracted_document(
    name: str, raw: bytes, kind: str, arguments: JsonObject, label: str
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

    header = f"[Extracted text from {name} ({document_label(kind)})]:"
    try:
        if arguments.get("pattern") is not None:
            lines = split_text_lines(extracted, keepends=True)
            return tool_success(
                {"content": f"{header}\n{render_matching_lines(lines, arguments, label)}"}
            )
        position = parse_read_position(arguments.get("offset"))
        max_lines = (
            optional_int(arguments.get("limit"), field_name="limit", minimum=1)
            or DEFAULT_LINE_LIMIT
        )
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))

    body = render_text(
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
    context: ToolContext,
    resolved: Path,
    raw: bytes,
    media_type: str,
) -> JsonObject:
    """Pass loaded pixels to Chat without creating a persistent attachment."""
    context.presentation_images.append({"path": str(resolved), "filename": resolved.name})
    context.result_media.append(
        {
            "path": str(resolved),
            "filename": resolved.name,
            "media_type": media_type,
            "base64": base64.b64encode(raw).decode("ascii"),
        }
    )
    return tool_success({"content": f"Loaded image {resolved.name} ({media_type})."})


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


def _read_binary_notice(label: str) -> JsonObject:
    """Return a short notice for a binary file instead of decoding it to garbage."""
    return tool_success({"content": f"[{label} is a binary file; it is not shown as text.]"})


def _read_video(label: str, media_type: str) -> JsonObject:
    """Return a path note for video; no provider wire accepts raw video."""
    return tool_success(
        {"content": f"[{label} is a video ({media_type}); this model cannot view video.]"}
    )


def _list_directory(resolved: Path, arguments: JsonObject, label: str) -> JsonObject:
    """List a directory's entries; reading a directory never counts as reading a file."""
    names: list[str] = []
    try:
        with os.scandir(resolved) as entries:
            for entry in entries:
                try:
                    is_directory = entry.is_dir()
                except OSError:
                    is_directory = False
                names.append(entry.name + ("/" if is_directory else ""))
    except OSError as error:
        return tool_failure("file_read_error", f"Failed to list {label}: {error}")
    names.sort(key=lambda name: (name.casefold(), name))
    try:
        return tool_success({"content": render_directory_listing(names, arguments, label)})
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))


def _directory_search_message(arguments: JsonObject, label: str) -> str:
    """Name the search_files call that finds matching lines across a directory."""
    call: dict[str, Any] = {"pattern": arguments["pattern"], "path": label}
    if arguments.get("ignore_case") is True:
        call["args"] = ["-i"]
    if arguments.get("context"):
        call["context"] = arguments["context"]
    rendered = ", ".join(
        f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in call.items()
    )
    return (
        f"{label} is a directory, and read shows the lines of one file. To find matching "
        f"lines in its files, call search_files({rendered})."
    )


def _normalized_for_display(arguments: Any) -> JsonObject:
    """Return the arguments the handler would see, or the raw call if they are invalid."""
    try:
        normalized = normalize_read_arguments(arguments)
    except (ToolContractError, ValueError):
        normalized = arguments
    return normalized if isinstance(normalized, dict) else {}


def _display_parts(arguments: JsonObject) -> list[ToolDisplayPart]:
    path = _normalized_for_display(arguments).get("path")
    if not isinstance(path, str) or not path.strip():
        return []
    return [ToolDisplayPart(path, kind="path", truncate="start", tooltip="always", copyable=True)]


def _read_line_range_facts(
    arguments: JsonObject, _result: JsonObject | None
) -> tuple[JsonObject, ...]:
    """Describe an explicitly bounded read without coupling the UI to read arguments."""
    arguments = _normalized_for_display(arguments)
    if "offset" not in arguments and "limit" not in arguments:
        return ()

    raw_offset = arguments.get("offset", 1)
    if isinstance(raw_offset, str) and ":" in raw_offset:
        raw_offset = raw_offset.partition(":")[0]
    start = _display_line_number(raw_offset)
    limit = _display_line_number(arguments.get("limit", DEFAULT_LINE_LIMIT))
    if start is None or limit is None:
        return ()
    return ({"kind": "line_range", "start": start, "end": start + limit - 1},)


def _display_line_number(value: object) -> int | None:
    """Accept canonical integers and their pre-normalization decimal spelling."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed >= 1 else None
    return None


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
        family="files",
        result_schema={"type": "object", "required": ["content"]},
        display=ToolDisplay(parts_builder=_display_parts, fact_builder=_read_line_range_facts),
        parallel_safe=True,
        open_input_schema=True,
        unadvertised_parameters=READ_HIDDEN_PARAMETERS,
        argument_normalizer=normalize_read_arguments,
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
