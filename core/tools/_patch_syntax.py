"""Patch text syntax, parsed operations, and structured patch failures.

``_parse`` reads the advertised V4A form, tolerating its common framing
mistakes, into ordered operations. Other patch dialects (unified diffs,
SEARCH/REPLACE blocks) fail as text before a file header. Parsing reads no
files; matching happens in ``_edit_engine.py``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from core.tools.tools import JsonObject
from core.utils.paths import model_path

_HEADERS_HELP = (
    "*** Update File: <path>, *** Add File: <path>, *** Delete File: <path> or "
    "*** Move File: <from> -> <to>"
)

_MESSAGES = {
    "invalid_patch": "Patch line {line} cannot be read: {text}",
    "before_header": (
        "Patch line {line} comes before any file header: {text}\n"
        "A patch names each file in a header before its changes, like this:\n"
        "*** Begin Patch\n*** Update File: <path>\n@@\n-old line\n+new line\n*** End Patch\n"
        f"The file headers are {_HEADERS_HELP}."
    ),
    "after_end": (
        "Patch line {line} follows *** End Patch: {text}\nMove it above *** End Patch or remove it."
    ),
    "unknown_marker": (
        "Patch line {line} is not a patch instruction: {text}\n"
        f"File headers are {_HEADERS_HELP}."
    ),
    "no_body": "Patch line {line} follows *** {action} File, which takes no lines: {text}",
    "after_eof": (
        "Patch line {line} follows the end-of-file marker of its @@ block: {text}\n"
        "Start a new @@ block for further changes."
    ),
    "move_syntax": (
        "Patch line {line} needs a source and a destination: *** Move File: <from> -> <to>."
    ),
    "open_fence": "The patch opens a ``` fence but does not close it.",
    "empty_update": (
        "*** Update File: {path} has no changes below it. Add @@ and the lines to change: "
        "- for removed lines, + for added lines, a leading space for unchanged lines."
    ),
    "no_changes": (
        f"The patch names no file changes. Start with a file header ({_HEADERS_HELP}) "
        "followed by the lines to change."
    ),
    "invalid_add_line": (
        "Patch line {line} in an *** Add File body starts with @@: {text}\n"
        "Add File takes the whole new file as + lines, without @@ blocks. To change part of "
        "an existing file, use *** Update File. To add a line that starts with @@, write it "
        "with a leading +."
    ),
    "add_minus_line": (
        "{where}: patch line {line} in the *** Add File body starts with - instead of +: "
        "{text}\nThe file exists, and Add File replaces all of its content. To change part of "
        "the file, use *** Update File. To replace the whole file, start every line of the "
        "new content with +."
    ),
    "path_mismatch": (
        "path is {path}, but the patch changes {other}. Send only the patch, or a path "
        "that matches it."
    ),
    "invalid_path": "Cannot use path {path}: {reason}.",
    "file_not_found": "File not found: {path}.",
    "not_a_file": "{path} is not a regular file.",
    "destination_exists": (
        "Cannot move to {path}: it already exists. Delete it earlier in the same patch or "
        "choose another destination."
    ),
    "overlapping_paths": (
        "{path} is used both as a file and as a folder of another path in this patch. "
        "Send these changes in separate calls."
    ),
    "binary_file": (
        "{path} is a binary file, so its text cannot be patched; Delete File and Move File "
        "still work on it."
    ),
    "nul_text": (
        "{path}: the new text contains a NUL character (U+0000), which only binary files "
        "hold. To produce that character in source code, write its escape sequence instead, "
        "such as \\x00."
    ),
    "unsupported_encoding": "{path} is not UTF-8 text, so its text cannot be patched.",
    "ambiguous_match": (
        "{where}: the lines to replace occur {occurrences} times ({lines}). Add unchanged "
        "lines around the change until it matches once, or start the block with an @@ line "
        "naming an earlier line, such as the enclosing function: the first occurrence after "
        "that line is changed."
    ),
    "ambiguous_patch_copy": (
        "{where}: the lines to replace do not match the file exactly and resemble "
        "{occurrences} places ({lines}). Copy the current lines of the one to change "
        "exactly, with enough unchanged lines around them to tell it apart."
    ),
    "ambiguous_replacement": (
        "{where}: old_string matches {occurrences} places ({lines}). Include more of the "
        "surrounding text so it matches once, or set replace_all to true to change every "
        "match."
    ),
    "ambiguous_copy": (
        "{where}: old_string does not match the file exactly and resembles {occurrences} "
        "places ({lines}). Copy the current text of the one to change into old_string, with "
        "enough surrounding text to tell it apart."
    ),
    "text_not_found": "{where}: the lines to replace were not found.",
    "old_text_not_found": "{where}: old_string was not found.",
    "eof_not_found": "{where}: the file does not end with the lines before *** End of File.",
    "not_after_hint": (
        '{where}: the lines to replace are not after the @@ line "{hint}"; they are at '
        "{lines}, above it. After @@, put a line above them, such as the first line of the "
        "enclosing function, or leave @@ empty."
    ),
    "line_numbered_content": (
        "{where}: the lines carry line-number prefixes (such as 12| ) from read output "
        "that do not fit the file. Send the file's lines without the prefixes."
    ),
    "context_not_found": (
        '{where}: the @@ line "{hint}" was not found. After @@, put one complete line from '
        "the file, such as the first line of the enclosing function, or leave @@ empty."
    ),
    "context_block_not_found": (
        "{where}: the lines of the @@ block above the lines to replace were not found "
        "together. That block has no - or + line, so it only locates the lines to replace. "
        "Copy its lines exactly from the file, or leave that block out."
    ),
    "ambiguous_context": (
        '{where}: the @@ line "{hint}" occurs {occurrences} times ({lines}). Put a line '
        "after @@ that occurs once, or add a second @@ line below it to narrow the place."
    ),
    "file_exists": (
        "{where}: the text to replace is empty, which creates a file, but the file already "
        "has content. Send the current text to replace, or Add File to replace the whole file."
    ),
    "no_newline_position": (
        "{where}: the no-newline marker applies only to the last line of a file."
    ),
    "conflicting_move": (
        "Patch line {line} moves the file to {second}, but an earlier line moves it to "
        "{first}. Keep one destination; use separate *** Move File operations for "
        "successive moves."
    ),
    "file_changed": (
        "{path} changed on disk while this patch ran. Read it and resend the unfinished changes."
    ),
}


class _PatchError(Exception):
    """A patch failure whose message names paths the way the caller shows them.

    ``values`` may hold ``Path`` objects; ``text(shown)`` renders them with the
    caller's path presentation. ``template`` selects a message other than the
    one named by ``code``; ``message`` is literal text used as-is.
    """

    def __init__(
        self,
        code: str,
        *,
        details: JsonObject | None = None,
        message: str | None = None,
        template: str | None = None,
        **values: object,
    ):
        self.code = code
        self.details = details or {}
        self.values = values
        self._message = message
        self._template = template or code
        super().__init__(self.text())

    def text(
        self, shown: Callable[[Path], str] = model_path, templates: Mapping[str, str] | None = None
    ) -> str:
        """Render the message; ``templates`` replaces the wording of some templates."""
        if self._message is not None:
            return self._message
        values = {k: shown(v) if isinstance(v, Path) else v for k, v in self.values.items()}
        if "path" in values:
            label = values.get("label")
            values.setdefault("where", f"{values['path']}, {label}" if label else values["path"])
        template = (templates or {}).get(self._template, _MESSAGES[self._template])
        return template.format(**values)


@dataclass
class _Replacement:
    """Replace text (``old_string``) where it occurs, also within lines."""

    old: str
    new: str
    replace_all: bool = False


@dataclass
class _Hunk:
    hints: list[str] = field(default_factory=list)
    lines: list[tuple[str, str]] = field(default_factory=list)
    eof: bool = False
    no_newline: bool = False
    precise_only: bool = False
    replacement: _Replacement | None = None
    label: str = ""

    def changes_text(self) -> bool:
        if self.replacement is not None:
            return self.replacement.old != self.replacement.new
        return any(prefix in "+-" for prefix, _ in self.lines)

    def is_identity(self) -> bool:
        """Whether applying the hunk leaves the text it locates as it is."""
        if self.replacement is not None:
            return self.replacement.old == self.replacement.new
        old = [text for prefix, text in self.lines if prefix in " -"]
        return old == [text for prefix, text in self.lines if prefix in " +"]


@dataclass
class _Operation:
    action: str
    path: str
    destination: str | None = None
    hunks: list[_Hunk] = field(default_factory=list)
    hunk_number: int = 1
    # Add only when the file is missing or empty (an empty old_string).
    only_if_empty: bool = False
    # Names the change in a failure of the whole operation, such as "edit 2".
    label: str = ""
    # Line ending for a file this Add creates; existing files keep their own.
    newline: str = "\n"
    # The first Add body line written with a leading - (patch line, text): content
    # of a new file, but a removal the Add cannot apply where it replaces a file.
    minus_line: tuple[int, str] | None = None
    # Content sent as whole text (write, edit creation): the final line break
    # follows the replaced file, and a file without content gets one; a patch
    # marks a missing final line break explicitly.
    keeps_final_break: bool = False


_ACTIONS = {
    "add": "add",
    "create": "add",
    "new": "add",
    "update": "update",
    "edit": "update",
    "modify": "update",
    "delete": "delete",
    "remove": "delete",
    "move": "move",
    "rename": "move",
}
_HEADER = re.compile(
    r"\*{3}\s*(add|create|new|update|edit|modify|delete|remove|move|rename)\s+file\s*:\s*(.*)",
    re.IGNORECASE,
)
_BEGIN = re.compile(r"\*{3}\s*begin\s+patch\s*", re.IGNORECASE)
_END = re.compile(r"\*{3}\s*end\s+patch\s*", re.IGNORECASE)
_MOVE_TO = re.compile(r"\*{3}\s*move\s+to\s*:\s*(.*)", re.IGNORECASE)
_END_OF_FILE = re.compile(r"\*{3}\s*end\s+of\s+file\s*", re.IGNORECASE)
_NO_NEWLINE = "\\ No newline at end of file"
_FENCE = re.compile(r"```[\w+.-]*\s*")


def _clip(text: str) -> str:
    return text[:200]


def _clean_path(text: str) -> str:
    """Strip whitespace and one pair of quotes or backticks around a header path."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "`'\"":
        text = text[1:-1].strip()
    return text


def _same_path(left: str, right: str) -> bool:
    def key(value: str) -> str:
        value = value.replace("\\", "/")
        while value.startswith("./"):
            value = value[2:]
        return value

    return key(left) == key(right)


def _patch_lines(patch: str) -> list[str]:
    """Split patch text into lines without surrounding blanks or one enclosing fence."""
    lines = patch.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    # Strip only an enclosing Markdown fence, never quoted or prefixed content.
    if lines and _FENCE.fullmatch(lines[0].strip()):
        if lines[-1].strip() != "```":
            raise _PatchError("invalid_patch", template="open_fence")
        lines = lines[1:-1]
    return lines


def _parse(patch: str, default_path: str | None = None) -> list[_Operation]:
    """Parse V4A patch text into ordered operations.

    ``default_path`` is the file a call names outside the patch text; a patch
    without file headers then changes that file, and headers must agree with it.
    """
    lines = _patch_lines(patch)
    headers = any(_HEADER.fullmatch(line) for line in lines)
    operations = _parse_v4a(lines, None if headers else default_path)
    if default_path is not None:
        for operation in operations:
            if not _same_path(operation.path, default_path):
                raise _PatchError(
                    "invalid_arguments",
                    template="path_mismatch",
                    path=default_path,
                    other=operation.path,
                )
    _check_operations(operations, len(lines))
    return operations


def _check_operations(operations: list[_Operation], line_count: int) -> None:
    for operation in operations:
        if operation.action == "update" and not operation.hunks and not operation.destination:
            raise _PatchError("invalid_patch", template="empty_update", path=operation.path)
    if not operations:
        raise _PatchError("no_changes")
    if not any(
        op.action != "update" or op.destination or any(h.changes_text() for h in op.hunks)
        for op in operations
    ):
        context = [
            (op.path, [text for _, text in hunk.lines])
            for op in operations
            for hunk in op.hunks
            if hunk.lines
        ]
        raise _PatchError(
            "no_changes",
            message=(
                "The patch changes nothing: it has no - or + line, so every line under @@ "
                "stays unchanged. To replace a line, write it as a - line; to insert above a "
                "line, write the + lines before it."
            ),
            details={"context_only": context},
        )


def _parse_v4a(lines: list[str], default_path: str | None) -> list[_Operation]:
    operations: list[_Operation] = []
    current: _Operation | None = None
    hunk: _Hunk | None = None
    ended = False
    # A header repeated before Begin Patch adds no operation of its own.
    duplicate: _Operation | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        number = index + 1
        index += 1
        if _BEGIN.fullmatch(line):
            if current is not None and not current.hunks and current.destination is None:
                duplicate = current
            current = None
            hunk = None
            ended = False
            continue
        if _END.fullmatch(line):
            ended = True
            continue
        header = _HEADER.fullmatch(line)
        if ended and not header:
            if not line.strip():
                continue
            raise _PatchError("invalid_patch", template="after_end", line=number, text=_clip(line))
        if header:
            ended = False
            action = _ACTIONS[header[1].lower()]
            path = _clean_path(header[2])
            destination = None
            if action == "move":
                path, separator, destination = path.partition(" -> ")
                path, destination = _clean_path(path), _clean_path(destination)
                if not separator or not destination:
                    raise _PatchError("invalid_patch", template="move_syntax", line=number)
            if not path or "\x00" in path or (destination and "\x00" in destination):
                raise _PatchError("invalid_patch", line=number, text=_clip(line))
            repeated = duplicate if duplicate is not None else current
            duplicate = None
            # A repeated header before any body adds no operation.
            if (
                repeated is not None
                and repeated.action == action
                and action in {"add", "update"}
                and repeated.path == path
                and not repeated.hunks
                and repeated.destination is None
            ):
                current = repeated
                hunk = None
                continue
            current = _Operation(action, path, destination)
            operations.append(current)
            hunk = None
            continue
        duplicate = None
        move_to = _MOVE_TO.fullmatch(line)
        if move_to and current and current.action in {"update", "move"}:
            destination = _clean_path(move_to[1])
            if not destination or "\x00" in destination:
                raise _PatchError("invalid_patch", line=number, text=_clip(line))
            if current.destination is not None and current.destination != destination:
                raise _PatchError(
                    "conflicting_move", line=number, first=current.destination, second=destination
                )
            # This is operation metadata, even when it follows a hunk. Repeating
            # the same destination adds no effect; conflicting targets never win.
            current.destination = destination
            continue
        if _END_OF_FILE.fullmatch(line) and hunk is not None:
            hunk.eof = True
            continue
        if current is None and default_path is not None and line.strip():
            current = _Operation("update", default_path)
            operations.append(current)
        if line.startswith("@@") and current and current.action in {"update", "move"}:
            # Explicit hunks after Move File unambiguously mean update-and-move.
            current.action = "update"
            hint = line[2:].strip()
            if "@@" in hint:
                hint = hint.split("@@", 1)[0].strip()
            if (
                hunk is not None
                and hunk.lines
                and all(prefix == " " for prefix, _ in hunk.lines)
                and not hunk.eof
                and not hunk.no_newline
            ):
                # A context-only block before another @@ is a locator for that
                # edit, not a successful mutation that may be silently ignored.
                anchor = "\n".join(text for _, text in hunk.lines)
                if anchor.strip():
                    hunk.hints.append(anchor)
                hunk.lines = []
            if hunk is None or hunk.lines:
                hunk = _Hunk()
                current.hunks.append(hunk)
            if hint:
                hunk.hints.append(hint)
            continue
        if line == "@@" and current and current.action == "add" and hunk is None:
            # A bare opening hunk delimiter adds no constraint to a whole-file Add.
            continue
        if line == _NO_NEWLINE and hunk and hunk.lines:
            if hunk.lines[-1][0] != "-":
                hunk.no_newline = True
            continue
        if current is None:
            if not line.strip():
                continue
            raise _PatchError(
                "invalid_patch",
                template="unknown_marker" if line.startswith("***") else "before_header",
                line=number,
                text=_clip(line),
            )
        if current.action in {"move", "delete"}:
            if not line.strip():
                continue
            raise _PatchError(
                "invalid_patch",
                template="no_body",
                line=number,
                action=current.action.capitalize(),
                text=_clip(line),
            )
        if line.startswith("***"):
            raise _PatchError(
                "invalid_patch", template="unknown_marker", line=number, text=_clip(line)
            )
        if hunk is None:
            hunk = _Hunk()
            current.hunks.append(hunk)
        if hunk.eof or hunk.no_newline:
            raise _PatchError("invalid_patch", template="after_eof", line=number, text=_clip(line))
        if current.action == "add":
            if line.startswith("@@"):
                raise _PatchError(
                    "invalid_patch", template="invalid_add_line", line=number, text=_clip(line)
                )
            if line.startswith("-") and current.minus_line is None:
                current.minus_line = (number, _clip(line))
            # Add has no context lines: a missing + means literal file content.
            # Keep all whitespace; interpreting one space as a diff prefix would
            # silently change indentation in otherwise recognizable creations.
            prefix, text = "+", line[1:] if line.startswith("+") else line
        else:
            prefix, text = (line[0], line[1:]) if line and line[0] in " +-" else (" ", line)
        hunk.lines.append((prefix, text))
    return operations
