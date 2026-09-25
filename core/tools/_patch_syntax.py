"""Patch text syntax, parsed operations, and structured patch failures.

``_parse`` accepts the advertised V4A form and the other patch dialects Models
write: unified diffs (``diff --git`` and ``---``/``+++`` file pairs) and
SEARCH/REPLACE blocks, alone or inside a V4A Update. Every dialect becomes the
same ordered operations. Parsing reads no files; matching happens in
``_patch_hunks.py``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
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
        f"Start each file's changes with a header: {_HEADERS_HELP}."
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
        "Patch line {line} is in an *** Add File body but is not file content: {text}\n"
        "Add supplies the whole new file as + lines. Prefix a literal leading -, @@ or "
        "*** with +; use *** Update File to change part of an existing file."
    ),
    "unified_add_line": (
        "Patch line {line} changes a file the diff creates from /dev/null: {text}\n"
        "A new file's hunk contains only + lines."
    ),
    "binary_diff": "Patch line {line} is a binary diff, which cannot be applied as text: {text}",
    "block_syntax": (
        "Patch line {line} breaks a SEARCH/REPLACE block: {text}\n"
        "Each block is <<<<<<< SEARCH, the current lines, =======, the new lines, "
        ">>>>>>> REPLACE."
    ),
    "block_path": (
        "The SEARCH/REPLACE block at patch line {line} names no file. Put the file path "
        "on the line before the block, or send it as path."
    ),
    "block_text": (
        "Patch line {line} is outside the SEARCH/REPLACE blocks: {text}\n"
        "Only a file path may stand on the line before a block."
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
    "unsupported_encoding": "{path} is not UTF-8 text, so its text cannot be patched.",
    "ambiguous_match": (
        "{where}: the lines to replace occur {occurrences} times ({lines}). Add unchanged "
        "lines around the change, or an @@ line naming the enclosing function or class, "
        "so it matches once."
    ),
    "ambiguous_replacement": (
        "{where}: old_string occurs {occurrences} times ({lines}). Include more of the "
        "surrounding text so it matches once, or set replace_all to true to change every "
        "occurrence."
    ),
    "ambiguous_copy": (
        "{where}: old_string does not match the file exactly and resembles {occurrences} "
        "places ({lines}). Copy the current text of the one to change into old_string, with "
        "enough surrounding text to tell it apart."
    ),
    "replacement_count": (
        "{where}: old_string occurs {occurrences} times ({lines}), but expected_replacements "
        "is {expected}. Nothing was replaced."
    ),
    "text_not_found": "{where}: the lines to replace were not found.",
    "old_text_not_found": "{where}: old_string was not found.",
    "eof_not_found": "{where}: the file does not end with the lines before *** End of File.",
    "line_numbered_content": (
        "{where}: the lines carry line-number prefixes (such as 12| ) from read output "
        "that do not fit the file. Send the file's lines without the prefixes."
    ),
    "context_not_found": (
        '{where}: the @@ line "{hint}" was not found. After @@, put one complete line from '
        "the file, such as the first line of the enclosing function, or leave @@ empty."
    ),
    "ambiguous_context": (
        '{where}: the @@ line "{hint}" occurs {occurrences} times ({lines}). Put a line '
        "after @@ that occurs once, or add a second @@ line below it to narrow the place."
    ),
    "insert_past_end": "{where}: insert_line {line} is past the end of the file ({count} lines).",
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

    def text(self, shown: Callable[[Path], str] = model_path) -> str:
        if self._message is not None:
            return self._message
        values = {k: shown(v) if isinstance(v, Path) else v for k, v in self.values.items()}
        if "path" in values:
            label = values.get("label")
            values.setdefault("where", f"{values['path']}, {label}" if label else values["path"])
        return _MESSAGES[self._template].format(**values)


@dataclass
class _Replacement:
    """Replace exact text (``old_string``) wherever it occurs within lines."""

    old: str
    new: str
    replace_all: bool = False
    expected: int | None = None


@dataclass
class _Hunk:
    hints: list[str] = field(default_factory=list)
    lines: list[tuple[str, str]] = field(default_factory=list)
    eof: bool = False
    no_newline: bool = False
    precise_only: bool = False
    replacement: _Replacement | None = None
    insert_line: int | None = None
    label: str = ""

    def changes_text(self) -> bool:
        if self.replacement is not None:
            return self.replacement.old != self.replacement.new
        return any(prefix in "+-" for prefix, _ in self.lines)

    def is_identity(self) -> bool:
        """Whether applying the hunk leaves the text it locates as it is."""
        if self.replacement is not None:
            return self.replacement.old == self.replacement.new
        if self.insert_line is not None:
            return not self.lines
        old = [text for prefix, text in self.lines if prefix in " -"]
        return old == [text for prefix, text in self.lines if prefix in " +"]


@dataclass
class _Operation:
    action: str
    path: str
    destination: str | None = None
    hunks: list[_Hunk] = field(default_factory=list)
    hunk_number: int = 1
    # Add only when the file is missing or empty (an empty old_string or SEARCH).
    only_if_empty: bool = False
    # Line ending for a file this Add creates; existing files keep their own.
    newline: str = "\n"


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
_NUMBERED_HUNK = re.compile(r"-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?")
_UNIFIED_HUNK = re.compile(r"@@+\s*-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s*@@+.*")
_FENCE = re.compile(r"```[\w+.-]*\s*")
_SEARCH = re.compile(r"(?:<{5,}|-{5,})\s*SEARCH\s*", re.IGNORECASE)
_DIVIDER = re.compile(r"={5,}\s*")
_REPLACE = re.compile(r"(?:>{5,}|\+{5,})\s*REPLACE\s*", re.IGNORECASE)
_BLOCK_POSITION = re.compile(r":(?:start|end)_line:\s*\d+\s*")
_GIT_METADATA = (
    "index ",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "similarity index ",
    "dissimilarity index ",
)


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


def _is_unified(lines: list[str]) -> bool:
    """Whether a file pair or ``diff --git`` comes before the first hunk."""
    for index, line in enumerate(lines):
        if line.startswith("diff --git "):
            return True
        if line.startswith("@@"):
            return False
        if (
            line.startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            return True
    return False


def _parse(patch: str, default_path: str | None = None) -> list[_Operation]:
    """Parse patch text in any accepted dialect into ordered operations.

    ``default_path`` is the file a call names outside the patch text; a patch
    without file headers then changes that file, and headers must agree with it.
    """
    lines = _patch_lines(patch)
    if any(_HEADER.fullmatch(line) for line in lines):
        operations = _parse_v4a(lines, None)
    elif _is_unified(lines):
        operations = _parse_unified(lines)
    elif any(_SEARCH.fullmatch(line) for line in lines):
        operations = _parse_blocks(lines, default_path)
    else:
        operations = _parse_v4a(lines, default_path)
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
            (op.path, [text for h in op.hunks for _, text in h.lines])
            for op in operations
            if op.hunks
        ]
        raise _PatchError(
            "no_changes",
            message=(
                "The patch changes nothing: every line under @@ starts with a space, which "
                "marks an unchanged line. Mark lines to remove with - and lines to add with +, "
                "keeping unchanged lines around them to locate the change."
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
            # A unified-diff file pair pasted under an Update header repeats the path.
            if (
                action == "update"
                and index + 1 < len(lines)
                and lines[index].startswith("--- ")
                and lines[index + 1].startswith("+++ ")
            ):
                index += 2
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
        if _SEARCH.fullmatch(line) and current and current.action == "update":
            index, old, new = _read_block(lines, index, number)
            if not old:
                raise _PatchError(
                    "invalid_patch",
                    message=(
                        f"The SEARCH/REPLACE block at patch line {number} has an empty SEARCH "
                        "part inside *** Update File. Put the current lines to replace in "
                        "SEARCH, or use *** Add File to create a file."
                    ),
                )
            current.hunks.append(_block_hunk(old, new))
            hunk = None
            continue
        if line.startswith("@@") and current and current.action in {"update", "move"}:
            # Explicit hunks after Move File unambiguously mean update-and-move.
            current.action = "update"
            hint = line[2:].strip()
            if "@@" in hint:
                hint = hint.split("@@", 1)[0].strip()
            if _NUMBERED_HUNK.fullmatch(hint):
                hint = ""  # Unified-diff line numbers are advisory, never authority.
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
            if line.startswith(("-", "@@")):
                raise _PatchError(
                    "invalid_patch", template="invalid_add_line", line=number, text=_clip(line)
                )
            # Add has no context lines: a missing + means literal file content.
            # Keep all whitespace; interpreting one space as a diff prefix would
            # silently change indentation in otherwise recognizable creations.
            prefix, text = "+", line[1:] if line.startswith("+") else line
        else:
            prefix, text = (line[0], line[1:]) if line and line[0] in " +-" else (" ", line)
        hunk.lines.append((prefix, text))
    return operations


def _read_block(lines: list[str], index: int, number: int) -> tuple[int, list[str], list[str]]:
    """Read the SEARCH/REPLACE block whose SEARCH marker precedes ``lines[index]``.

    Returns the index after the block and its SEARCH and REPLACE lines.
    """
    old: list[str] = []
    new: list[str] = []
    # Roo Code places advisory line numbers and a dash separator before the text.
    while index < len(lines) and _BLOCK_POSITION.fullmatch(lines[index]):
        index += 1
        if index < len(lines) and re.fullmatch(r"-{3,}\s*", lines[index]):
            index += 1
    target = old
    while index < len(lines):
        line = lines[index]
        index += 1
        if _DIVIDER.fullmatch(line) and target is old:
            target = new
            continue
        if _REPLACE.fullmatch(line) and target is new:
            return index, old, new
        if _SEARCH.fullmatch(line) or _REPLACE.fullmatch(line):
            break
        target.append(line)
    raise _PatchError(
        "invalid_patch", template="block_syntax", line=number, text=_clip(lines[number - 1])
    )


def _block_hunk(old: list[str], new: list[str]) -> _Hunk:
    """Express a SEARCH/REPLACE block as a hunk: shared lines become context."""
    hunk = _Hunk()
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            hunk.lines.extend((" ", text) for text in old[i1:i2])
            continue
        hunk.lines.extend(("-", text) for text in old[i1:i2])
        hunk.lines.extend(("+", text) for text in new[j1:j2])
    return hunk


def _parse_blocks(lines: list[str], default_path: str | None) -> list[_Operation]:
    """Parse SEARCH/REPLACE blocks (Aider, Cline, Roo Code) into Update hunks."""
    operations: list[_Operation] = []
    candidate: tuple[int, str] | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        number = index + 1
        index += 1
        stripped = line.strip()
        if not stripped or _FENCE.fullmatch(stripped) or stripped == "```":
            continue
        if not _SEARCH.fullmatch(line):
            if candidate is not None:
                raise _PatchError(
                    "invalid_patch", template="block_text", line=candidate[0], text=candidate[1]
                )
            candidate = (number, line)
            continue
        path = default_path
        if candidate is not None:
            named = _clean_path(candidate[1])
            if default_path is None:
                path = named
            elif not _same_path(named, default_path):
                raise _PatchError(
                    "invalid_patch", template="block_text", line=candidate[0], text=candidate[1]
                )
        candidate = None
        if path is None:
            raise _PatchError("invalid_patch", template="block_path", line=number)
        index, old, new = _read_block(lines, index, number)
        if not old:
            # An empty SEARCH creates the file, as in Aider.
            hunks = [_Hunk(lines=[("+", text) for text in new])] if new else []
            operations.append(_Operation("add", path, hunks=hunks, only_if_empty=True))
            continue
        hunk = _block_hunk(old, new)
        if operations and operations[-1].action == "update" and operations[-1].path == path:
            operations[-1].hunks.append(hunk)
        else:
            operations.append(_Operation("update", path, hunks=[hunk]))
    if candidate is not None:
        raise _PatchError(
            "invalid_patch", template="block_text", line=candidate[0], text=candidate[1]
        )
    return operations


def _diff_path(text: str) -> str | None:
    """Return a unified-diff header path, or ``None`` for /dev/null."""
    path = text.split("\t", 1)[0].strip()
    if len(path) >= 2 and path[0] == path[-1] == '"':
        path = path[1:-1]
    return None if path == "/dev/null" else path


def _strip_prefixes(old: str | None, new: str | None, git: bool) -> tuple[str | None, str | None]:
    """Remove git's a/ and b/ prefixes when the diff shows git's convention."""
    if git or (old or "a/").startswith("a/") and (new or "b/").startswith("b/"):
        if old is not None and old.startswith("a/"):
            old = old[2:]
        if new is not None and new.startswith("b/"):
            new = new[2:]
    return old, new


def _parse_unified(lines: list[str]) -> list[_Operation]:
    """Parse a unified diff; hunk line numbers are advisory like V4A's."""
    operations: list[_Operation] = []
    current: _Operation | None = None
    hunk: _Hunk | None = None
    git = False
    rename: dict[str, str] = {}
    remaining = [0, 0]  # old and new lines a numbered hunk header still promises
    index = 0
    while index < len(lines):
        line = lines[index]
        number = index + 1
        index += 1
        if line.startswith("diff --git "):
            git = True
            if rename.keys() == {"from", "to"} and current is None:
                operations.append(_Operation("move", rename["from"], rename["to"]))
            current = hunk = None
            rename = {}
            continue
        if line.startswith("@@") and current is not None and current.action != "delete":
            numbers = _UNIFIED_HUNK.fullmatch(line)
            remaining = [0, 0]
            if numbers:
                remaining = [
                    int(numbers[2]) if numbers[2] is not None else 1,
                    int(numbers[4]) if numbers[4] is not None else 1,
                ]
            hunk = _Hunk()
            current.hunks.append(hunk)
            continue
        # Promised hunk lines are content even when they look like a file pair.
        inside = hunk is not None and (remaining[0] > 0 or remaining[1] > 0)
        if not inside:
            if _BEGIN.fullmatch(line) or _END.fullmatch(line):
                continue
            if line.startswith(_GIT_METADATA):
                continue
            if line.startswith(("rename from ", "rename to ")):
                rename[line.split()[1]] = line.split(" ", 2)[2].strip()
                continue
            if line.startswith("Binary files ") or line == "GIT binary patch":
                raise _PatchError(
                    "invalid_patch", template="binary_diff", line=number, text=_clip(line)
                )
            if line.startswith("--- ") and index < len(lines) and lines[index].startswith("+++ "):
                old, new = _strip_prefixes(_diff_path(line[4:]), _diff_path(lines[index][4:]), git)
                index += 1
                # A rename section without a file pair of its own ends here.
                renamed = (rename.get("from"), rename.get("to"))
                if rename.keys() == {"from", "to"} and current is None and (old, new) != renamed:
                    operations.append(_Operation("move", rename["from"], rename["to"]))
                rename = {}
                if old is None and new is None:
                    raise _PatchError("invalid_patch", line=number, text=_clip(line))
                if old is None:
                    current = _Operation("add", str(new))
                elif new is None:
                    current = _Operation("delete", old)
                else:
                    current = _Operation("update", old, None if old == new else new)
                operations.append(current)
                hunk = None
                continue
        if hunk is None or current is None:
            if current is not None and current.action == "delete" or not line.strip():
                continue
            raise _PatchError(
                "invalid_patch", template="before_header", line=number, text=_clip(line)
            )
        if line.startswith("\\"):
            if hunk.lines and hunk.lines[-1][0] != "-":
                hunk.no_newline = True
            continue
        prefix, text = (line[0], line[1:]) if line and line[0] in " +-" else (" ", line)
        if current.action == "add" and prefix != "+":
            raise _PatchError(
                "invalid_patch", template="unified_add_line", line=number, text=_clip(line)
            )
        if prefix in " -":
            remaining[0] -= 1
        if prefix in " +":
            remaining[1] -= 1
        hunk.lines.append((prefix, text))
    if rename.keys() == {"from", "to"} and current is None:
        operations.append(_Operation("move", rename["from"], rename["to"]))
    return operations
