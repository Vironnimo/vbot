"""V4A patch syntax, parsed operations, and structured parse failures."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.tools.tools import (
    JsonObject,
)

_MESSAGES = {
    "invalid_arguments": "Pass only a non-empty patch string.",
    "invalid_patch": "Invalid patch syntax at line {line}: {text}. Correct this line and retry.",
    "no_changes": "Patch contains no changes. Include an Add, Update, Delete, or Move operation.",
    "invalid_path": "Cannot use path {path}: {reason}.",
    "file_not_found": "File not found: {path}. Check the path and retry.",
    "not_a_file": "Path is not a regular file: {path}. Choose a file path.",
    "destination_exists": (
        "Destination already exists: {path}. Use Update to change it or choose another destination."
    ),
    "overlapping_paths": (
        "Patch paths overlap as file and parent directory: {path}. "
        "Split these operations into separate calls."
    ),
    "binary_file": (
        "Cannot update binary file: {path}. Updates require UTF-8 text without NUL bytes."
    ),
    "unsupported_encoding": (
        "Cannot update non-UTF-8 file: {path}. Convert it to UTF-8 before applying text changes."
    ),
    "ambiguous_match": (
        "Hunk {hunk} in {path} matches multiple locations. Add unchanged neighboring lines "
        "or a unique context hint and retry."
    ),
    "text_not_found": (
        "Hunk {hunk} in {path} was not found. Use any candidate excerpts below to update "
        "the hunk, or inspect the file for current context."
    ),
    "line_numbered_content": (
        "Hunk {hunk} in {path} contains incomplete line-number gutters. "
        "Supply complete raw lines without read-output prefixes."
    ),
    "file_changed": (
        "{path} changed unexpectedly during this patch. Inspect its current content and retry."
    ),
}


_HEADER = re.compile(r"^\*\*\*\s+(Add|Update|Delete|Move)\s+File:\s*(.*)$")


class _PatchError(Exception):
    def __init__(self, code: str, *, details: JsonObject | None = None, **values: object):
        super().__init__(_MESSAGES[code].format(**values))
        self.code = code
        self.details = details or {}


@dataclass
class _Hunk:
    hints: list[str] = field(default_factory=list)
    lines: list[tuple[str, str]] = field(default_factory=list)
    eof: bool = False
    no_newline: bool = False
    precise_only: bool = False


@dataclass
class _Operation:
    action: str
    path: str
    destination: str | None = None
    hunks: list[_Hunk] = field(default_factory=list)
    hunk_number: int = 1


def _parse(patch: str) -> list[_Operation]:
    # Strip only an enclosing Markdown fence, never quoted or prefixed content.
    lines = patch.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and re.fullmatch(r"```(?:diff|patch)?", lines[0].strip()):
        if not lines[-1].strip() == "```":
            raise _PatchError("invalid_patch", line=len(lines), text=lines[-1])
        lines = lines[1:-1]
    operations: list[_Operation] = []
    current: _Operation | None = None
    hunk: _Hunk | None = None
    ended = False
    for number, line in enumerate(lines, 1):
        if re.fullmatch(r"\*\*\*\s+Begin\s+Patch\s*", line) and not operations and current is None:
            continue
        if re.fullmatch(r"\*\*\*\s+End\s+Patch\s*", line):
            ended = True
            continue
        if ended:
            if not line.strip():
                continue
            raise _PatchError("invalid_patch", line=number, text=line[:200])
        header = _HEADER.fullmatch(line)
        if header:
            action, path = header.groups()
            path = path.strip()
            destination = None
            if action == "Move":
                path, separator, destination = path.partition(" -> ")
                if not separator or not destination.strip():
                    raise _PatchError("invalid_patch", line=number, text=line[:200])
                destination = destination.strip()
            if not path or "\x00" in path or (destination and "\x00" in destination):
                raise _PatchError("invalid_patch", line=number, text=line[:200])
            current = _Operation(action.lower(), path, destination)
            operations.append(current)
            hunk = None
            continue
        if line.startswith("*** Move to: ") and current and current.action == "update":
            if current.destination is not None or current.hunks or not line[13:].strip():
                raise _PatchError("invalid_patch", line=number, text=line[:200])
            current.destination = line[13:].strip()
            continue
        if line == "*** End of File" and hunk is not None:
            hunk.eof = True
            continue
        if line.startswith("@@") and current and current.action in {"update", "move"}:
            # Explicit hunks after Move File unambiguously mean update-and-move.
            current.action = "update"
            hint = line[2:].strip()
            if "@@" in hint:
                hint = hint.split("@@", 1)[0].strip()
            if re.fullmatch(r"-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?", hint):
                hint = ""  # Unified-diff line numbers are advisory, never authority.
            if hunk is None or hunk.lines:
                hunk = _Hunk()
                current.hunks.append(hunk)
            if hint:
                hunk.hints.append(hint)
            continue
        if line == "\\ No newline at end of file" and hunk and hunk.lines:
            if hunk.lines[-1][0] != "-":
                hunk.no_newline = True
            continue
        if line.startswith("***") or current is None or current.action in {"move", "delete"}:
            if not line.strip():
                continue
            raise _PatchError("invalid_patch", line=number, text=line[:200])
        if hunk is None:
            hunk = _Hunk()
            current.hunks.append(hunk)
        if hunk.eof or hunk.no_newline:
            raise _PatchError("invalid_patch", line=number, text=line[:200])
        prefix, text = (line[0], line[1:]) if line and line[0] in " +-" else (" ", line)
        if current.action == "add" and prefix != "+":
            raise _PatchError("invalid_patch", line=number, text=line[:200])
        hunk.lines.append((prefix, text))
    for operation in operations:
        if operation.action == "update" and not operation.hunks and not operation.destination:
            raise _PatchError("invalid_patch", line=len(lines), text=operation.path)
    if not operations or not any(
        op.action != "update"
        or op.destination
        or any(p in "+-" for h in op.hunks for p, _ in h.lines)
        for op in operations
    ):
        raise _PatchError("no_changes")
    return operations
