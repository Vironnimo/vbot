"""V4A parsing, current-content planning, and coordinated file mutation."""

from __future__ import annotations

import json
import re
import stat
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path

from core.tools.arguments import line_number_gutter_candidates, strip_line_number_gutters
from core.tools.file_state import FileReadState, StaleReason, atomic_write_bytes
from core.tools.fuzzy_match import (
    AmbiguousFuzzyMatch,
    FuzzyReplacement,
    find_closest_candidates,
    preserve_typography,
    replace_fuzzy,
)
from core.tools.syntax_check import warning_for_edited_file
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolHandler,
    ToolRegistry,
    offload_tool_handler,
    tool_failure,
    tool_success,
)
from core.utils.paths import model_path

APPLY_PATCH_TOOL_NAME = "apply_patch"
APPLY_PATCH_TOOL_DESCRIPTION = (
    "Apply a V4A patch to create, update, delete, or move files. Operations are ordered "
    "and validated together before any file is changed. A write failure can leave partial "
    "changes; the result lists the files actually changed."
)
APPLY_PATCH_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "patch": {
            "type": "string",
            "description": (
                "Patch text. Paths are relative to the working directory or absolute. "
                "Use `*** Begin Patch` and `*** End Patch` around file operations: "
                "`*** Add File: path` with `+` content lines; `*** Update File: path` "
                "with `@@` hunks containing space-prefixed context, `-` removals, and "
                "`+` additions; `*** Delete File: path`; or "
                "`*** Move File: source -> destination`. An Update may use "
                "`*** Move to: destination`. Use `@@ context` to locate a section and "
                "`*** End of File` to anchor the final hunk. Addition-only hunks insert "
                "after their context hint, or append when no hint is given. Include "
                "enough context to identify one location. Already-applied anchored hunks "
                "are skipped."
            ),
        },
    },
    "required": ["patch"],
}

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
        "Hunk {hunk} in {path} was not found. Compare the current file with the candidate "
        "excerpts and retry with current context."
    ),
    "line_numbered_content": (
        "Hunk {hunk} in {path} contains incomplete line-number gutters. "
        "Supply complete raw lines without read-output prefixes."
    ),
    "file_changed": (
        "{path} changed while the patch was being prepared. Inspect its current content and retry."
    ),
}
_VALIDATION_FAILED = "Patch validation failed; no files were changed."
_WRITE_FAILED = (
    "Writing stopped at {path}: {reason}. Earlier listed changes remain applied. "
    "Inspect them before retrying."
)
_GUTTER_WARNING = "Removed read-output line-number prefixes before applying the hunk."
_ESCAPE_WARNING = "Normalized escaped patch text after the literal text did not match."
_STALE_WARNING = (
    "{path} changed since this Session last read it. The patch was applied to current content."
)
_BREAK = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
_HEADER = re.compile(r"^\*\*\*\s+(Add|Update|Delete|Move)\s+File:\s*(.*)$")
_GUTTER = re.compile(r"^\s*[1-9][0-9]*(?::[1-9][0-9]*)?\|")
_ESCAPE = re.compile(r"\\(n|r|t|\\|\"|')")


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


@dataclass
class _Operation:
    action: str
    path: str
    destination: str | None = None
    hunks: list[_Hunk] = field(default_factory=list)


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
        if (
            re.fullmatch(r"\*\*\*\s+Begin\s+Patch\s*", line)
            and not operations
            and current is None
            and number == 1
        ):
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
        if line.startswith("@@") and current and current.action == "update":
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


def _line_parts(content: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    start = 0
    for match in _BREAK.finditer(content):
        parts.append((content[start : match.start()], match.group()))
        start = match.end()
    if start < len(content):
        parts.append((content[start:], ""))
    return parts


def _ending(content: str) -> str:
    for ending in ("\r\n", "\n", "\r"):
        if ending in content:
            return ending
    found = _BREAK.search(content)
    return found.group() if found else "\n"


def _hunk_text(hunk: _Hunk, prefixes: str) -> str:
    return "\n".join(text for prefix, text in hunk.lines if prefix in prefixes)


def _candidates(content: str, pattern: str, offset: int = 0) -> JsonObject:
    return {
        "candidates": [
            {
                "line": candidate.line_number + offset,
                "text": candidate.text,
                "truncated": candidate.truncated,
            }
            for candidate in find_closest_candidates(content, pattern)
        ]
    }


def _match(
    content: str, old: str, new: str, *, precise: bool = False, eof: bool = False
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    if old == "":
        positions = []
        offset = 0
        for line, (text, ending) in enumerate(_line_parts(content), 1):
            if text == "" and (not eof or offset + len(ending) == len(content)):
                positions.append((offset, line))
            offset += len(text) + len(ending)
        if not positions:
            return None
        if len(positions) > 1:
            return AmbiguousFuzzyMatch(
                len(positions), [line for _, line in positions], [1] * len(positions)
            )
        start, line = positions[0]
        return FuzzyReplacement(
            content[:start] + new + content[start:],
            line,
            line,
            1,
            "exact",
            ((start, start),),
            ((start, start + len(new)),),
        )
    return replace_fuzzy(
        content,
        old,
        new,
        replace_all=False,
        whole_lines=True,
        precise_only=precise,
        at_eof=eof,
        typographic=True,
    )


def _normalize_gutters(hunk: _Hunk) -> _Hunk | None:
    old_lines = [text for prefix, text in hunk.lines if prefix in " -"]
    old = "\n".join(old_lines)
    if strip_line_number_gutters(old) is None:
        return None
    # Locators may mix raw lines with copied gutters, as edit allows. Only a
    # unique whole-line match against the current file authorizes this recovery.
    lines = []
    for prefix, text in hunk.lines:
        stripped = strip_line_number_gutters(text)
        if stripped is not None and re.match(r"^\s*\d+:\d+\|", text):
            return None
        lines.append((prefix, text if stripped is None else stripped))
    return replace(hunk, lines=lines)


def _unescape(text: str, *, replacement_for: str | None = None) -> str:
    values = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'"}

    def decode(match: re.Match[str]) -> str:
        kind = match[1]
        if replacement_for is not None:
            if kind == "n" or match[0] not in replacement_for:
                return match[0]
            if kind in "tr" and not re.fullmatch(r"(?:[ \t]|\\[tr])*", text[: match.start()]):
                return match[0]
        return values[kind]

    return _ESCAPE.sub(decode, text)


def _clean_additions(hunk: _Hunk, path: str, index: int) -> tuple[_Hunk, list[str]]:
    # Existing literal gutter-shaped context is authoritative. New standalone
    # additions use the same complete-block recovery as edit's new_string.
    if any(_GUTTER.match(text) for prefix, text in hunk.lines if prefix in " -"):
        return hunk, []
    lines = list(hunk.lines)
    warnings = []
    start = 0
    while start < len(lines):
        if lines[start][0] != "+":
            start += 1
            continue
        end = start + 1
        while end < len(lines) and lines[end][0] == "+":
            end += 1
        texts = [text for _, text in lines[start:end]]
        if sum(bool(_GUTTER.match(text)) for text in texts) >= 2:
            candidates = line_number_gutter_candidates("\n".join(texts), allow_continuations=False)
            if not candidates:
                raise _PatchError("line_numbered_content", path=path, hunk=index)
            lines[start:end] = [("+", text) for text in candidates[0].split("\n")]
            warnings.append(_GUTTER_WARNING)
        start = end
    return replace(hunk, lines=lines), warnings


def _apply_hunk(content: str, hunk: _Hunk, path: str, index: int) -> tuple[str, list[str]]:
    if not any(prefix in "+-" for prefix, _ in hunk.lines):
        return content, []
    hunk, warnings = _clean_additions(hunk, path, index)
    offset = 0
    hint_start = 0
    for hint in hunk.hints:
        found = _match(content[offset:], hint, hint, precise=True)
        if isinstance(found, AmbiguousFuzzyMatch):
            raise _PatchError("ambiguous_match", path=path, hunk=index)
        if found is None:
            raise _PatchError(
                "text_not_found", path=path, hunk=index, details=_candidates(content, hint)
            )
        hint_start = offset + found.before_spans[0][0]
        offset += found.before_spans[0][1]
        ending = _BREAK.match(content, offset)
        if ending:
            offset = ending.end()
    window = content[offset:]
    old, new = _hunk_text(hunk, " -"), _hunk_text(hunk, " +")
    if not any(prefix in " -" for prefix, _ in hunk.lines):
        position = offset if hunk.hints else len(content)
        if hunk.no_newline and position != len(content):
            raise _PatchError("invalid_patch", line=index, text="\\ No newline at end of file")
        if hunk.eof and position != len(content):
            raise _PatchError(
                "text_not_found", path=path, hunk=index, details=_candidates(content, new)
            )
        inserted = new.replace("\n", _ending(content))
        if not hunk.no_newline:
            inserted += _ending(content)
        # A hint ties the post-state to an insertion point. A suffix alone
        # cannot distinguish a retry from an intentional repeated append.
        if hunk.hints and inserted.strip() and content[position:].startswith(inserted):
            return content, warnings
        separator = (
            _ending(content)
            if position and not _BREAK.search(content[position - 1 : position])
            else ""
        )
        return content[:position] + separator + inserted + content[position:], warnings
    if hunk.hints:
        # Models sometimes repeat the final hint as the first context/removal
        # line. Include that line in the search without weakening earlier hints.
        offset = hint_start
        window = content[offset:]
    found = _match(window, old, new, precise=True, eof=hunk.eof)
    normalized = _normalize_gutters(hunk)
    if found is None and normalized is not None:
        candidate_old, candidate_new = _hunk_text(normalized, " -"), _hunk_text(normalized, " +")
        candidate_match = _match(window, candidate_old, candidate_new, precise=True, eof=hunk.eof)
        hunk, old, new, found = normalized, candidate_old, candidate_new, candidate_match
        warnings.append(_GUTTER_WARNING)
    if found is None and any(_GUTTER.match(t) for _, t in hunk.lines):
        raise _PatchError(
            "line_numbered_content", path=path, hunk=index, details=_candidates(content, old)
        )
    if found is None and _unescape(old) != old:
        escaped = replace(
            hunk,
            lines=[
                (p, _unescape(t, replacement_for=old if p == "+" else None)) for p, t in hunk.lines
            ],
        )
        # Unescaping line separators changes the hunk's physical line structure.
        escaped.lines = [(p, line) for p, text in escaped.lines for line in text.split("\n")]
        candidate_old, candidate_new = _hunk_text(escaped, " -"), _hunk_text(escaped, " +")
        candidate_match = _match(window, candidate_old, candidate_new, precise=True, eof=hunk.eof)
        if candidate_match is not None:
            hunk, old, new, found = escaped, candidate_old, candidate_new, candidate_match
            warnings.append(_ESCAPE_WARNING)
    if found is None:
        # Ignore surplus blank context at a hunk boundary only after the full
        # locator misses. Removed blank lines remain part of the operation.
        lines = list(hunk.lines)
        while lines and lines[0][0] == " " and not lines[0][1].strip():
            lines.pop(0)
        while lines and lines[-1][0] == " " and not lines[-1][1].strip():
            lines.pop()
        if lines != hunk.lines and any(p in " -" for p, _ in lines):
            trimmed = replace(hunk, lines=lines)
            candidate_old, candidate_new = _hunk_text(trimmed, " -"), _hunk_text(trimmed, " +")
            candidate_match = _match(
                window, candidate_old, candidate_new, precise=True, eof=hunk.eof
            )
            if candidate_match is not None:
                hunk, old, new, found = trimmed, candidate_old, candidate_new, candidate_match
    if found is None:
        context = "".join(text.strip() for prefix, text in hunk.lines if prefix == " ")
        if (
            len(context) >= 4
            and new
            and isinstance(_match(window, new, new, precise=True, eof=hunk.eof), FuzzyReplacement)
        ):
            return content, warnings
        found = _match(window, old, new, eof=hunk.eof)
    if isinstance(found, AmbiguousFuzzyMatch):
        raise _PatchError(
            "ambiguous_match", path=path, hunk=index, details=_candidates(content, old)
        )
    if found is None:
        code = (
            "line_numbered_content"
            if any(_GUTTER.match(t) for _, t in hunk.lines)
            else "text_not_found"
        )
        raise _PatchError(code, path=path, hunk=index, details=_candidates(content, old))
    if [t for p, t in hunk.lines if p in " -"] == [t for p, t in hunk.lines if p in " +"]:
        return content, warnings
    start, end = found.before_spans[0]
    after_start, after_end = found.after_spans[0]
    actual = _line_parts(window[start:end])
    if found.strategy != "exact" and any(
        token in old and token in new and token not in window[start:end]
        for token in ('\\"', "\\'", "\\\\")
    ):
        raise _PatchError(
            "text_not_found", path=path, hunk=index, details=_candidates(content, old)
        )
    prepared = _line_parts(found.new_content[after_start:after_end])
    # splitlines omits the final empty line; the hunk still gives it a position.
    old_count = sum(p in " -" for p, _ in hunk.lines)
    new_count = sum(p in " +" for p, _ in hunk.lines)
    if len(actual) < old_count:
        actual.append(("", ""))
    if len(prepared) < new_count:
        prepared.append(("", ""))
    if len(actual) != old_count or len(prepared) != new_count:
        raise _PatchError(
            "text_not_found", path=path, hunk=index, details=_candidates(content, old)
        )
    trailing = _BREAK.match(window, end)
    final_ending = trailing.group() if trailing else ""
    if trailing:
        end = trailing.end()
    if hunk.no_newline and end != len(window):
        raise _PatchError("invalid_patch", line=index, text="\\ No newline at end of file")
    if actual:
        actual[-1] = (actual[-1][0], final_ending)
    output: list[tuple[str, str]] = []
    last_output_prefix = ""
    old_index = new_index = 0
    removed_lines: list[tuple[str, str]] = []
    for prefix, locator_line in hunk.lines:
        if prefix == " ":
            removed_lines.clear()
            output.append(actual[old_index])  # Preserve every context byte.
            last_output_prefix = prefix
        elif prefix == "+":
            replacement_line = prepared[new_index][0]
            if removed_lines:
                actual_line, old_line = removed_lines.pop(0)
                if found.strategy != "exact":
                    replacement_line = preserve_typography(actual_line, old_line, replacement_line)
            output.append((replacement_line, _ending(content)))
            last_output_prefix = prefix
        elif prefix == "-":
            removed_lines.append((actual[old_index][0], locator_line))
        if prefix in " -":
            old_index += 1
        if prefix in " +":
            new_index += 1
    if output:
        output = [(text, ending or _ending(content)) for text, ending in output[:-1]] + output[-1:]
        if last_output_prefix == "+" or hunk.no_newline:
            output[-1] = (output[-1][0], "" if hunk.no_newline else final_ending)
    replacement_text = "".join(text + ending for text, ending in output)
    return content[: offset + start] + replacement_text + window[end:], warnings


@dataclass(frozen=True)
class _Snapshot:
    payload: bytes | None
    mode: int | None = None


def _snapshot(path: Path) -> _Snapshot:
    try:
        info = path.stat()
    except FileNotFoundError:
        return _Snapshot(None)
    if not stat.S_ISREG(info.st_mode):
        raise _PatchError("not_a_file", path=model_path(path))
    return _Snapshot(path.read_bytes(), stat.S_IMODE(info.st_mode))


def _decode(payload: bytes, path: str) -> str:
    if b"\x00" in payload:
        raise _PatchError("binary_file", path=path)
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise _PatchError("unsupported_encoding", path=path) from error


def _resolve(context: ToolContext, path: str) -> Path:
    try:
        if "\x00" in path:
            raise ValueError(path)
        return context.resolve_path(path)
    except (ValueError, OSError, RuntimeError) as error:
        raise _PatchError("invalid_path", path=path, reason=str(error)) from error


def _plan(
    operations: list[_Operation], paths: dict[str, Path], before: dict[Path, _Snapshot]
) -> tuple[dict[Path, _Snapshot], dict[Path, list[str]]]:
    pending = before.copy()
    warnings: dict[Path, list[str]] = {}
    for operation in operations:
        path = paths[operation.path]
        displayed = model_path(path)
        source = pending[path]
        payload = source.payload
        if operation.action == "add":
            for index, hunk in enumerate(operation.hunks):
                operation.hunks[index], notes = _clean_additions(hunk, displayed, index + 1)
                warnings.setdefault(path, []).extend(notes)
            content = "\n".join(t for h in operation.hunks for _, t in h.lines)
            if operation.hunks and not operation.hunks[-1].no_newline:
                content += "\n"
            payload = content.encode("utf-8")
            if b"\x00" in payload:
                raise _PatchError("binary_file", path=displayed)
            if source.payload is not None and source.payload != payload:
                raise _PatchError("destination_exists", path=displayed)
            pending[path] = _Snapshot(payload, source.mode)
            continue
        if payload is None:
            raise _PatchError("file_not_found", path=displayed)
        target = paths[operation.destination] if operation.destination else path
        if target != path and pending[target].payload is not None:
            raise _PatchError("destination_exists", path=model_path(target))
        if operation.action == "delete":
            pending[path] = _Snapshot(None)
            continue
        if operation.action == "update":
            content = _decode(payload, displayed)
            for index, hunk in enumerate(operation.hunks, 1):
                content, notes = _apply_hunk(content, hunk, displayed, index)
                warnings.setdefault(path, []).extend(notes)
            bom = b"\xef\xbb\xbf" if payload.startswith(b"\xef\xbb\xbf") else b""
            payload = bom + content.encode("utf-8")
            if b"\x00" in payload:
                raise _PatchError("binary_file", path=displayed)
        if target != path:
            pending[path] = _Snapshot(None)
            warnings.setdefault(target, []).extend(warnings.pop(path, []))
        pending[target] = _Snapshot(payload, source.mode)
    return pending, warnings


def _change_details(
    path: Path, before: bytes | None, after: bytes | None
) -> tuple[JsonObject, int, int]:
    result: JsonObject = {
        "path": model_path(path),
        "action": "add" if before is None else "delete" if after is None else "update",
    }
    try:
        old = _decode(before or b"", model_path(path))
        new = _decode(after or b"", model_path(path))
    except _PatchError:
        return result, 0, 0
    old_lines, new_lines = old.splitlines(), new.splitlines()
    changes = [
        op
        for op in SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()
        if op[0] != "equal"
    ]
    added = sum(j2 - j1 for _, _, _, j1, j2 in changes)
    removed = sum(i2 - i1 for _, i1, i2, _, _ in changes)
    result["preview"] = [
        {
            "before_line": i1 + 1,
            "after_line": j1 + 1,
            "before": [line[:240] for line in old_lines[i1 : min(i2, i1 + 4)]],
            "after": [line[:240] for line in new_lines[j1 : min(j2, j1 + 4)]],
            "before_omitted_lines": max(0, i2 - i1 - 4),
            "after_omitted_lines": max(0, j2 - j1 - 4),
            "before_truncated_lines": [
                i + 1 for i in range(i1, min(i2, i1 + 4)) if len(old_lines[i]) > 240
            ],
            "after_truncated_lines": [
                j + 1 for j in range(j1, min(j2, j1 + 4)) if len(new_lines[j]) > 240
            ],
        }
        for _, i1, i2, j1, j2 in changes[:2]
    ]
    result["omitted_regions"] = max(0, len(changes) - 2)
    if after is not None:
        warning = warning_for_edited_file(path, old, new)
        if warning:
            result["syntax_warning"] = warning
    return result, added, removed


def _execute(context: ToolContext, arguments: JsonObject, state: FileReadState) -> JsonObject:
    patch = arguments.get("patch")
    if set(arguments) != {"patch"} or not isinstance(patch, str) or not patch.strip():
        return tool_failure("invalid_arguments", _MESSAGES["invalid_arguments"])
    try:
        operations = _parse(patch)
        paths = {
            name: _resolve(context, name)
            for op in operations
            for name in (op.path, op.destination)
            if name is not None
        }
        resolved = sorted(set(paths.values()), key=str)
        for path in resolved:
            if any(parent in resolved for parent in path.parents):
                raise _PatchError("overlapping_paths", path=model_path(path))
        with ExitStack() as locks:
            for path in resolved:
                locks.enter_context(state.lock_path(path))
            before = {path: _snapshot(path) for path in resolved}
            pending, warnings = _plan(operations, paths, before)
            changed = [path for path in resolved if pending[path] != before[path]]
            # Protect against an external writer during matching, before the first write.
            for path in resolved:
                if _snapshot(path) != before[path]:
                    raise _PatchError("file_changed", path=model_path(path))
            return _commit(context, state, before, pending, changed, warnings)
    except _PatchError as error:
        message = _VALIDATION_FAILED + "\n" + str(error)
        if error.details:
            message += "\n" + json.dumps(error.details, ensure_ascii=False)
        return tool_failure(error.code, message)
    except OSError as error:
        return tool_failure("file_read_error", _VALIDATION_FAILED + "\n" + str(error))


def _commit(
    context: ToolContext,
    state: FileReadState,
    before: dict[Path, _Snapshot],
    pending: dict[Path, _Snapshot],
    changed: list[Path],
    warnings: dict[Path, list[str]],
) -> JsonObject:
    results: list[JsonObject] = []
    added = removed = 0
    # Materialize destinations first, so a failed move never loses its source.
    ordered = sorted(changed, key=lambda path: pending[path].payload is None)
    failure: JsonObject | None = None
    for position, path in enumerate(ordered):
        target = pending[path]
        try:
            if _snapshot(path) != before[path]:
                raise _PatchError("file_changed", path=model_path(path))
            stale = state.check_stale(context.session_id, path) is StaleReason.MODIFIED
            if target.payload is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(path, target.payload, mode=target.mode)
        except (OSError, _PatchError) as error:
            failure = {
                "code": error.code if isinstance(error, _PatchError) else "file_write_error",
                "message": _WRITE_FAILED.format(path=model_path(path), reason=str(error)),
                "pending_paths": [model_path(p) for p in ordered[position:]],
            }
            break
        if target.payload is not None:
            state.record_read(context.session_id, path)
        details, plus, minus = _change_details(path, before[path].payload, target.payload)
        added += plus
        removed += minus
        notes = list(dict.fromkeys(warnings.get(path, [])))
        if stale:
            notes.append(_STALE_WARNING.format(path=model_path(path)))
        if notes:
            details["warnings"] = notes
        results.append(details)
        if context.change_tracker is not None:
            try:
                old = _decode(before[path].payload or b"", model_path(path))
                new = _decode(target.payload or b"", model_path(path))
            except _PatchError:
                pass  # Binary moves/deletions do not have a text delta.
            else:
                context.change_tracker.record_write(context.session_id, path, before=old, after=new)
    context.add_display_line_changes(added=added, removed=removed)
    context.add_display_count(len(results), "files")
    for path in before:
        if path not in changed and pending[path].payload is not None:
            state.record_read(context.session_id, path)
    if failure is not None and not results:
        failure["message"] += "\nNo files were changed."
        return tool_failure(failure["code"], failure["message"])
    data: JsonObject = {"status": "partial" if failure else "success", "files": results}
    if not changed:
        data["already_applied"] = True
    if failure:
        data["error"] = failure
    return tool_success(data)


def _display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    patch = arguments.get("patch")
    if not isinstance(patch, str):
        return ()
    match = re.search(r"(?m)^\*\*\* (?:Add|Update|Delete|Move) File: (.+)$", patch)
    if match is None:
        return ()
    return (
        ToolDisplayPart(
            value=match[1], kind="path", truncate="start", tooltip="always", copyable=True
        ),
    )


def make_apply_patch_handler(file_state: FileReadState) -> ToolHandler:
    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return _execute(context, arguments, file_state)

    return handler


def register_apply_patch_tool(registry: ToolRegistry, *, file_state: FileReadState) -> None:
    registry.register(
        APPLY_PATCH_TOOL_NAME,
        APPLY_PATCH_TOOL_DESCRIPTION,
        APPLY_PATCH_TOOL_PARAMETERS,
        offload_tool_handler(make_apply_patch_handler(file_state)),
        family="files",
        open_input_schema=True,
        result_schema={"type": "object", "required": ["status", "files"]},
        display=ToolDisplay(parts_builder=_display_parts, hidden_argument_keys=("patch",)),
    )
