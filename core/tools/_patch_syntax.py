"""V4A patch syntax, parsed operations, and structured parse failures."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.tools.tools import (
    JsonObject,
)

_MESSAGES = {
    "invalid_arguments": 'Pass {"patch": "..."} with non-empty patch text and no other fields.',
    "invalid_patch": "Invalid patch syntax at line {line}: {text}. Correct this line and retry.",
    "no_changes": "Patch contains no changes. Include an Add, Update, Delete, or Move operation.",
    "context_only_patch": (
        "This patch contains only unchanged context lines. No files were changed. "
        "Include the actual lines to insert with + or the lines to remove with -. "
        "Keep surrounding existing lines as context to locate the edit."
    ),
    "invalid_add_line": (
        "Invalid Add File body at patch line {line}: {text}. Add supplies the complete "
        "file content, not a deletion or an Update hunk. Prefix each content line with + "
        "(including a literal leading -, @@, or patch marker); use Update File for edits."
    ),
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
    "text_not_found": "Hunk {hunk} in {path} was not found. {guidance}",
    "line_numbered_content": (
        "Hunk {hunk} in {path} contains incomplete line-number gutters. "
        "Supply complete raw lines without read-output prefixes."
    ),
    "context_not_found": (
        "Context hint {hint!r} in {path} was not found. After @@, use a complete existing "
        "line, or use bare @@ with unchanged neighboring lines in the hunk."
    ),
    "ambiguous_context": (
        "Context hint {hint!r} in {path} matches multiple lines. Use a unique full line "
        "after @@, or use bare @@ with enough unchanged neighboring lines to identify "
        "one location."
    ),
    "conflicting_move": (
        "Conflicting move destinations at patch line {line}: {first!r} and {second!r}. "
        "Keep one destination for this operation; use separate Move File operations "
        "for successive moves."
    ),
    "file_changed": (
        "{path} changed unexpectedly during this patch. Inspect its current content and retry."
    ),
}


_NOT_FOUND_EXCERPTS = (
    "Use any candidate excerpts below to update the hunk, or inspect the file for current context."
)
_NOT_FOUND_WITHOUT_EXCERPTS = (
    "Read the file for its current content, then retry the hunk with matching context."
)


def _not_found_guidance(details: JsonObject | None) -> str:
    """Name the recovery the result supports: a candidate list can come back empty."""
    if details and details.get("candidates"):
        return _NOT_FOUND_EXCERPTS
    return _NOT_FOUND_WITHOUT_EXCERPTS


_HEADER = re.compile(r"^\*\*\*\s+(Add|Update|Delete|Move)\s+File:\s*(.*)$")


class _PatchError(Exception):
    def __init__(
        self,
        code: str,
        *,
        details: JsonObject | None = None,
        message: str | None = None,
        **values: object,
    ):
        if message is None and code == "text_not_found":
            values["guidance"] = _not_found_guidance(details)
        super().__init__(message if message is not None else _MESSAGES[code].format(**values))
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
        if re.fullmatch(r"\*\*\*\s+Begin\s+Patch\s*", line) and (current is None or ended):
            current = None
            hunk = None
            ended = False
            continue
        if re.fullmatch(r"\*\*\*\s+End\s+Patch\s*", line):
            ended = True
            continue
        header = _HEADER.fullmatch(line)
        if ended and not header:
            if not line.strip():
                continue
            raise _PatchError("invalid_patch", line=number, text=line[:200])
        if header:
            ended = False
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
            # A repeated header before any Update body adds no operation.
            if (
                current is not None
                and current.action == action.lower() == "update"
                and current.path == path
                and not current.hunks
                and current.destination is None
            ):
                continue
            current = _Operation(action.lower(), path, destination)
            operations.append(current)
            hunk = None
            continue
        if line.startswith("*** Move to: ") and current and current.action in {"update", "move"}:
            destination = line[13:].strip()
            if not destination or "\x00" in destination:
                raise _PatchError("invalid_patch", line=number, text=line[:200])
            if current.destination is not None and current.destination != destination:
                raise _PatchError(
                    "conflicting_move", line=number, first=current.destination, second=destination
                )
            # This is operation metadata, even when it follows a hunk. Repeating
            # the same destination adds no effect; conflicting targets never win.
            current.destination = destination
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
        if current.action == "add":
            if line.startswith(("-", "@@")):
                raise _PatchError(
                    "invalid_patch",
                    message=_MESSAGES["invalid_add_line"].format(line=number, text=line[:200]),
                )
            # Add has no context lines: a missing + means literal file content.
            # Keep all whitespace; interpreting one space as a diff prefix would
            # silently change indentation in otherwise recognizable creations.
            prefix, text = "+", line[1:] if line.startswith("+") else line
        else:
            prefix, text = (line[0], line[1:]) if line and line[0] in " +-" else (" ", line)
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
        if operations:
            raise _PatchError("no_changes", message=_MESSAGES["context_only_patch"])
        raise _PatchError("no_changes")
    return operations
