"""Numbered text windows for read: line offsets, truncation, and line counts.

Text attachments use the same renderer as the ``read`` Tool, so a file accepted
through a channel has exactly the same limits and continuation hints.
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from core.tools.arguments import (
    LINE_NUMBER_GUTTER_SEPARATOR,
    TEXT_LINE_BREAK,
    optional_int,
    split_text_lines,
)
from core.tools.tools import JsonObject

MAX_FILE_BYTES = 50 * 1024
DEFAULT_LINE_LIMIT = 2000
# UTF-8 BOM that some Windows editors prepend; stripped on read so the model sees
# clean content (apply_patch preserves it on the round-trip).
UTF8_BOM_BYTES = b"\xef\xbb\xbf"
_TEXT_STREAM_CHUNK_CHARACTERS = 64 * 1024
# A cut-off read reports the file's line count when counting stays this cheap.
_LINE_COUNT_MAX_BYTES = 256 * 1024 * 1024
_COUNT_CHUNK_BYTES = 1024 * 1024
# A matching line longer than this is cut, with the offset that continues it.
_MATCH_LINE_CHARACTERS = 2000


@dataclass(frozen=True)
class ReadPosition:
    """A 1-indexed source position, optionally inside one physical line.

    A negative line counts back from the end of the file: -1 is the last line.
    """

    line: int
    character: int = 1


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
        message += f' Use offset="{continuation_offset}" to continue.'
    elif total_lines is None or shown_end < total_lines:
        message += f" Use offset={shown_end + 1} to continue."
    return message + "]"


def _line_gutter(line: int, character: int = 1) -> str:
    """Return a display gutter for a full line or an in-line continuation."""
    if character == 1:
        return f"{line}{LINE_NUMBER_GUTTER_SEPARATOR} "
    return f"{line}:{character}{LINE_NUMBER_GUTTER_SEPARATOR} "


def add_line_numbers(lines: list[str], start_line: int, start_character: int = 1) -> list[str]:
    """Prefix each line with an unpadded ``N| `` reference gutter.

    The gutter is deliberately unpadded: padding to a fixed width is pure token
    overhead on dense source, while dropping the numbers entirely makes the model
    hand-count lines and miss by one. One separator space keeps the gutter visually
    distinct from source that begins with ``|`` or another punctuation character.
    Each input line keeps its trailing newline (``keepends``); the number and
    separator go in front, file-absolute from ``start_line``.
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
    position = parse_read_position(offset)
    max_lines = optional_int(limit, field_name="limit", minimum=1)

    if raw.startswith(UTF8_BOM_BYTES):
        raw = raw[len(UTF8_BOM_BYTES) :]
    decoded = raw.decode("utf-8", errors="replace")
    return render_text(
        decoded,
        position.line,
        max_lines,
        number=True,
        start_character=position.character,
    )


def read_position(arguments: JsonObject) -> ReadPosition:
    """Return where a read call starts: its ``offset`` line and optional ``character``."""
    position = parse_read_position(arguments.get("offset"))
    character = optional_int(arguments.get("character"), field_name="character", minimum=1)
    return ReadPosition(position.line, character) if character else position


def _past_line_end(position: ReadPosition, limit: int | None) -> tuple[str, int, int] | str:
    """Read "A:B" as lines A-B when line A has no character B, or say why nothing shows.

    Models write ``offset: "100:120"`` for a line range, where a continued long
    line would have a character 120. An explicit ``limit`` keeps its line count.
    """
    line, character = position.line, position.character
    missing = f'Line {line} has no character {character}, so offset "{line}:{character}"'
    if limit is None and character < line:
        return (
            f"[{missing} showed nothing. offset takes a line number: offset={line} reads "
            f"from line {line}.]"
        )
    count = limit if limit is not None else character - line + 1
    read_as = f"offset={line}" if limit is not None else f"offset={line}, limit={count}"
    return f"[{missing} was read as {read_as}.]\n", line, count


def parse_read_position(offset: object) -> ReadPosition:
    """Parse a line offset or an in-line continuation address such as 12:34."""
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
        return ReadPosition(line, character)

    number = optional_int(offset, field_name="offset")
    if number == 0:
        raise ValueError("offset 0 is not a line; lines count from 1")
    return ReadPosition(number or 1)


def plain_line_end(text: str) -> str:
    """Show a line break as a newline: the carriage return of CRLF is not content."""
    if text.endswith("\r\n"):
        return text[:-2] + "\n"
    if text.endswith("\r"):
        return text[:-1] + "\n"
    return text


def _start_line(line: int, total_lines: int) -> int:
    """Turn a line counted back from the end into a line counted from 1."""
    return max(1, total_lines + line + 1) if line < 0 else line


def render_text(
    text: str,
    start_line: int,
    limit: int | None,
    *,
    number: bool,
    start_character: int = 1,
) -> str:
    """Apply offset/limit, optional line numbering, and truncation safeguards.

    Shared by the literal-file path (``number=True`` adds the ``N| `` gutter) and
    the extracted-document path (``number=False`` — a rendering of an Office or
    notebook file is not editable source, so the gutter would only mislead).
    ``limit`` is the caller's line count; without one, the default bound applies.
    """
    max_lines = limit or DEFAULT_LINE_LIMIT
    all_lines = split_text_lines(text, keepends=True)
    total_lines = len(all_lines)

    if total_lines == 0:
        return ""

    start_line = _start_line(start_line, total_lines)
    start_index = start_line - 1
    if start_index >= total_lines:
        return (
            f"[Offset {start_line} is beyond end of file ({total_lines} lines). Nothing to show.]"
        )
    source_line = all_lines[start_index]
    if start_character > len(source_line):
        fallback = _past_line_end(ReadPosition(start_line, start_character), limit)
        if isinstance(fallback, str):
            return fallback
        note, line, count = fallback
        return note + render_text(text, line, count, number=number)

    selected_lines = [
        plain_line_end(line) for line in all_lines[start_index : start_index + max_lines]
    ]
    selected_lines[0] = selected_lines[0][start_character - 1 :]
    line_limited = start_index + len(selected_lines) < total_lines

    # Number before any byte fitting so the gutter counts against the 50 KB
    # budget and the model can cite/patch lines without hand-counting.
    rendered_lines = (
        add_line_numbers(selected_lines, start_line, start_character) if number else selected_lines
    )
    output = "".join(rendered_lines)
    byte_limited = len(output.encode("utf-8")) > MAX_FILE_BYTES

    if not (line_limited or byte_limited):
        return output

    return finalize_limited_text(
        rendered_lines,
        start_line=start_line,
        start_character=start_character,
        total_lines=total_lines,
        byte_limited=byte_limited,
        number=number,
    )


def finalize_limited_text(
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
    for match in TEXT_LINE_BREAK.finditer(text):
        fragments.append((text[start : match.end()], True))
        start = match.end()
    if start < len(text):
        fragments.append((text[start:], False))
    return fragments, held_carriage_return


def render_text_path(resolved: Path, arguments: JsonObject) -> str:
    """Render a local text file with bounded memory and early truncation."""
    position = read_position(arguments)
    limit = optional_int(arguments.get("limit"), field_name="limit", minimum=1)
    max_lines = limit or DEFAULT_LINE_LIMIT
    known_total: int | None = None
    if position.line < 0:
        known_total = count_lines(resolved)
        if not known_total:
            return ""
        position = ReadPosition(_start_line(position.line, known_total))
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
                if ends_line:
                    selected_fragment = plain_line_end(selected_fragment)
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
        fallback = _past_line_end(position, limit)
        if isinstance(fallback, str):
            return fallback
        note, line, count = fallback
        return note + render_text_path(resolved, {"offset": line, "limit": count})

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
    if known_total is None:
        known_total = count_lines(resolved, max_bytes=_LINE_COUNT_MAX_BYTES)
    return finalize_limited_text(
        rendered_lines,
        start_line=position.line,
        start_character=position.character,
        total_lines=known_total,
        byte_limited=byte_limited,
        number=True,
    )


def count_lines(resolved: Path, *, max_bytes: int | None = None) -> int | None:
    """Count lines the way the renderer splits them, or None past ``max_bytes``.

    Breaks are LF, CRLF and a lone CR. A final line without a break still counts.
    """
    count = 0
    size = 0
    first = b""
    last = b""
    with resolved.open("rb") as handle:
        while chunk := handle.read(_COUNT_CHUNK_BYTES):
            if not size:
                first = chunk[: len(UTF8_BOM_BYTES)]
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                return None
            count += chunk.count(b"\n") + chunk.count(b"\r") - chunk.count(b"\r\n")
            if last == b"\r" and chunk.startswith(b"\n"):
                count -= 1
            last = chunk[-1:]
    if size == len(UTF8_BOM_BYTES) and first == UTF8_BOM_BYTES:
        return 0
    if size and last not in (b"\n", b"\r"):
        count += 1
    return count


def without_bom(lines: Iterable[str]) -> Iterator[str]:
    """Yield text lines with a leading byte order mark removed."""
    iterator = iter(lines)
    first = next(iterator, None)
    if first is None:
        return
    yield first.removeprefix("\ufeff")
    yield from iterator


def render_matching_lines(lines: Iterable[str], arguments: JsonObject, label: str) -> str:
    """Show the lines that match ``pattern``, numbered like a read, with optional context.

    A call that sends search fields to read asks for this file's matching lines.
    The first line states the filter, so the result never reads as the whole file.
    """
    pattern = arguments["pattern"]
    flags = re.IGNORECASE if arguments.get("ignore_case") is True else 0
    note = ""
    try:
        regex = re.compile(pattern, flags)
    except re.error:
        regex = re.compile(re.escape(pattern), flags)
        note = " (as plain text: it is not a valid regex)"
    context = optional_int(arguments.get("context"), field_name="context", minimum=0) or 0
    position = parse_read_position(arguments.get("offset"))
    if position.line < 0:
        raise ValueError(
            "offset counts back from the end, which pattern cannot use; omit offset or "
            "give the line to start searching at"
        )
    max_lines = (
        optional_int(arguments.get("limit"), field_name="limit", minimum=1) or DEFAULT_LINE_LIMIT
    )

    shown: list[str] = []
    shown_lines = 0
    shown_bytes = 0
    last_shown = 0
    resume_at: int | None = None
    matches = 0
    before: deque[tuple[int, str]] = deque(maxlen=context)
    after = 0

    def show(number: int, text: str) -> None:
        nonlocal last_shown, resume_at, shown_bytes, shown_lines
        if resume_at is not None:
            return
        if len(text) > _MATCH_LINE_CHARACTERS:
            continuation = f"{number}:{_MATCH_LINE_CHARACTERS + 1}"
            text = (
                f"{text[:_MATCH_LINE_CHARACTERS]} [line continues; read "
                f'offset="{continuation}" for the rest]'
            )
        rendered = f"{_line_gutter(number)}{text}\n"
        if last_shown and number > last_shown + 1:
            rendered = "--\n" + rendered
        size = len(rendered.encode("utf-8"))
        if shown_lines >= max_lines or shown_bytes + size > MAX_FILE_BYTES:
            resume_at = number
            return
        shown.append(rendered)
        shown_lines += 1
        shown_bytes += size
        last_shown = number

    for number, line in enumerate(lines, start=1):
        if number < position.line:
            continue
        text = plain_line_end(line).removesuffix("\n")
        if regex.search(text):
            matches += 1
            for previous_number, previous in before:
                show(previous_number, previous)
            before.clear()
            show(number, text)
            after = context
        elif after:
            show(number, text)
            after -= 1
        else:
            before.append((number, text))

    scope = f" from line {position.line} on" if position.line > 1 else ""
    quoted = json.dumps(pattern, ensure_ascii=False)
    if not matches:
        return f"No line of {label}{scope} matches {quoted}{note}."
    noun = "line" if matches == 1 else "lines"
    header = f"{matches} {noun} of {label}{scope} match {quoted}{note}:\n"
    output = header + "".join(shown)
    if resume_at is not None:
        output += f"[Output stopped before line {resume_at}. Use offset={resume_at} to continue.]"
    return output


def render_directory_listing(names: list[str], arguments: JsonObject, label: str) -> str:
    """List directory entries one per line, paged like the lines of a file."""
    directory = label if label.endswith("/") else f"{label}/"
    total = len(names)
    if not total:
        return f"{directory} is an empty directory."
    position = read_position(arguments)
    if position.character != 1:
        raise ValueError(
            f"{directory} is a directory: offset counts its entries, so send "
            f"offset={position.line} without a character."
        )
    start = _start_line(position.line, total)
    if start > total:
        return f"[Offset {start} is beyond the {total} entries of {directory}. Nothing to show.]"
    max_entries = (
        optional_int(arguments.get("limit"), field_name="limit", minimum=1) or DEFAULT_LINE_LIMIT
    )
    shown: list[str] = []
    used = 0
    for name in names[start - 1 : start - 1 + max_entries]:
        size = len(name.encode("utf-8")) + 1
        if shown and used + size > MAX_FILE_BYTES:
            break
        shown.append(name)
        used += size
    end = start + len(shown) - 1
    noun = "entry" if total == 1 else "entries"
    output = f"Directory {directory} ({total} {noun}):\n" + "\n".join(shown)
    if end < total:
        output += f"\n[Showing entries {start}-{end} of {total}. Use offset={end + 1} to continue.]"
    return output
