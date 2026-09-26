"""apply_patch: plan requested file changes against current content and apply them.

``_patch_requests.py`` turns a call in any accepted shape into ordered
operations, ``_patch_hunks.py`` applies one hunk to current text, and
``_patch_report.py`` renders the result. This module owns the plan, the
coordinated filesystem mutation, and the Tool registration.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path

from core.tools._patch_entries import (
    _ABSENT,
    _entry_path,
    _observe_renamed,
    _rename_entry,
    _renames_entry,
    _resolve,
    _Snapshot,
    _snapshot,
)
from core.tools._patch_hunks import _apply_hunk, _clean_additions, _ending
from core.tools._patch_report import _FileReport, file_report, patch_result
from core.tools._patch_requests import (
    APPLY_PATCH_TOOL_NAME,
    PATCH_HIDDEN_PARAMETERS,
    normalize_patch_arguments,
    patch_operations,
)
from core.tools._patch_syntax import _HEADER, _Operation, _PatchError
from core.tools._path_suggestions import missing_file_message
from core.tools._read_text import add_line_numbers
from core.tools.arguments import split_text_lines
from core.tools.file_state import FileReadState, StaleReason, atomic_write_bytes
from core.tools.fuzzy_match import FuzzyReplacement, replace_fuzzy
from core.tools.model_names import model_tool_name
from core.tools.search import display_search_path
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolHandler,
    ToolRegistry,
    offload_tool_handler,
    tool_failure,
)

APPLY_PATCH_TOOL_DESCRIPTION = (
    "Edit, create, delete or move files with a patch. One call can change several places "
    "in several files; the changes apply in order, and changes that succeed stay applied "
    "if another one fails."
)
APPLY_PATCH_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "patch": {
            "type": "string",
            "description": (
                "Patch text, for example:\n"
                "*** Begin Patch\n*** Update File: src/app.py\n@@ def main():\n"
                "-    count = 1\n+    count = 2\n     run(count)\n"
                "*** Add File: notes.txt\n+first line of a new file\n"
                "*** Delete File: old.txt\n*** Move File: a.txt -> b.txt\n*** End Patch\n"
                "Under Update File, - lines are removed, + lines are added, and lines "
                "starting with a space are unchanged lines that locate the change; copy "
                "them exactly from the file. Every @@ block needs a - or + line. Text after "
                "@@ is optional and names an earlier line, such as the enclosing function. "
                "Start another @@ block for another place in the same file. A block of only "
                "+ lines goes after the @@ line, or at the end of the file after a bare @@. "
                "Add File creates a file or replaces all of its content. Paths are relative "
                "to the working directory or absolute."
            ),
        },
    },
    "required": ["patch"],
}

_BOM = b"\xef\xbb\xbf"
_WRITE_FAILED = "Could not change {path}: {reason}. Check this path before resending this change."
_DEPENDENCY_FAILED = (
    "{where} was not tried because an earlier change to {path} in this patch did not "
    "complete. Resend it together with that change."
)
_MOVE_SKIPPED = (
    "{path} was not moved to {destination} because a change to it above failed; it keeps "
    "its name. Resend the move together with the failed change."
)
_UNCONFIRMED = (
    "{path} was written, but reading it back failed: {reason}. Check it before resending "
    "this change."
)
_PRECISE_RECOVERY = (
    "After an earlier failure in this file, this change needs lines that match the file "
    "exactly (up to whitespace)."
)
_STALE_WARNING = (
    "{path} changed after this Session last read it; the change used its current content."
)
# A guard failure shows the whole current file when it is this small.
_GUARD_CONTENT_MAX_BYTES = 16 * 1024
_GUARD_CONTENT_MAX_LINES = 400
_CONTEXT_SEARCH_MAX_BYTES = 2 * 1024 * 1024


def _decode(payload: bytes, path: object) -> str:
    if b"\x00" in payload:
        raise _PatchError("binary_file", path=path)
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise _PatchError("unsupported_encoding", path=path) from error


def _text(snapshot: _Snapshot) -> str | None:
    """Return a snapshot's text, or ``None`` for an absent, link, or non-text entry."""
    if snapshot.payload is None or snapshot.link is not None:
        return None
    try:
        return _decode(snapshot.payload, "")
    except _PatchError:
        return None


def _is_empty(payload: bytes | None) -> bool:
    """Whether an existing file holds nothing a replacement could lose."""
    if payload is None:
        return False
    return not payload.removeprefix(_BOM).strip()


def _plan(
    operations: list[_Operation], paths: dict[str, Path], before: dict[Path, _Snapshot]
) -> tuple[dict[Path, _Snapshot], dict[Path, list[str]]]:
    pending = before.copy()
    warnings: dict[Path, list[str]] = {}
    for operation in operations:
        path = paths[operation.path]
        source = pending[path]
        payload = source.payload
        if operation.action == "add":
            if operation.only_if_empty and source.exists and not _is_empty(payload):
                raise _PatchError("file_exists", path=path)
            for index, hunk in enumerate(operation.hunks):
                operation.hunks[index], notes = _clean_additions(hunk, path)
                warnings.setdefault(path, []).extend(notes)
            content = "\n".join(t for h in operation.hunks for _, t in h.lines)
            if operation.hunks and not operation.hunks[-1].no_newline:
                content += "\n"
            if source.payload is not None:
                content = content.replace("\n", _ending(source.payload.decode("utf-8", "replace")))
                if source.payload.startswith(_BOM):
                    content = "﻿" + content.removeprefix("﻿")
            elif operation.newline != "\n":
                content = content.replace("\n", operation.newline)
            payload = content.encode("utf-8")
            if b"\x00" in payload:
                raise _PatchError("binary_file", path=path)
            pending[path] = _Snapshot(payload, source.mode)
            continue
        if not source.exists:
            raise _PatchError("file_not_found", path=path)
        target = paths[operation.destination] if operation.destination else path
        if target != path and pending[target].exists:
            raise _PatchError("destination_exists", path=target)
        if operation.action == "delete":
            pending[path] = _ABSENT
            continue
        if payload is None:
            # Content paths resolve through links; a link here appeared concurrently.
            raise _PatchError("file_changed", path=path)
        if operation.action == "update":
            content = _decode(payload, path)
            for hunk in operation.hunks:
                content, notes = _apply_hunk(content, hunk, path)
                warnings.setdefault(path, []).extend(notes)
            bom = _BOM if payload.startswith(_BOM) else b""
            payload = bom + content.encode("utf-8")
            if b"\x00" in payload:
                raise _PatchError("binary_file", path=path)
        if target != path:
            pending[path] = _ABSENT
            warnings.setdefault(target, []).extend(warnings.pop(path, []))
        pending[target] = _Snapshot(payload, source.mode)
    return pending, warnings


@dataclass
class _Batch:
    shown: Callable[[Path], str]
    cwd: Path
    # Actual observations and completed mutations only; never a speculative final plan.
    observed: dict[Path, _Snapshot] = field(default_factory=dict)
    before: dict[Path, _Snapshot] = field(default_factory=dict)
    after: dict[Path, _Snapshot] = field(default_factory=dict)
    warnings: dict[Path, list[str]] = field(default_factory=dict)
    blocked: set[Path] = field(default_factory=set)
    failed_text: set[Path] = field(default_factory=set)
    replaced: set[Path] = field(default_factory=set)
    # Case-only renames keep one path key: (original spelling, current spelling).
    respelled: dict[Path, tuple[Path, Path]] = field(default_factory=dict)
    # Completed moves by shown path: source -> destination.
    moves: dict[str, str] = field(default_factory=dict)
    results: list[JsonObject] = field(default_factory=list)


def _os_reason(error: OSError, batch: _Batch) -> str:
    """Describe a file-system error by its reason and shown path, never an absolute one."""
    reason = error.strerror or str(error)
    if error.filename:
        return f"{batch.shown(Path(error.filename))}: {reason}"
    return reason


def _error_data(error: _PatchError | OSError, batch: _Batch) -> JsonObject:
    if not isinstance(error, _PatchError):
        return {"code": "file_read_error", "message": f"Could not read {_os_reason(error, batch)}."}
    data: JsonObject = {"code": error.code, "message": error.text(batch.shown), **error.details}
    path = error.values.get("path")
    if isinstance(path, Path):
        data["path_label"] = batch.shown(path)
        if error.code == "file_not_found":
            data["message"] = missing_file_message(path, batch.cwd)
    return data


def _commit(
    context: ToolContext,
    state: FileReadState,
    batch: _Batch,
    before: dict[Path, _Snapshot],
    pending: dict[Path, _Snapshot],
    warnings: dict[Path, list[str]],
) -> tuple[list[str], JsonObject | None]:
    completed: list[str] = []
    expected = before.copy()

    def check_expected() -> None:
        for checked, snapshot in expected.items():
            if _snapshot(checked) != snapshot:
                raise _PatchError("file_changed", path=checked)

    changed = [path for path in pending if pending[path] != before[path]]
    # A move first materializes its destination; a failure never silently loses its source.
    for path in sorted(changed, key=lambda p: not pending[p].exists):
        target = pending[path]
        try:
            check_expected()
            stale = state.check_stale(context.session_id, path) is StaleReason.MODIFIED
            if target.payload is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(
                    path, target.payload, mode=target.mode, before_replace=check_expected
                )
        except (OSError, _PatchError) as error:
            batch.blocked.update(before)
            failure: JsonObject = (
                _error_data(error, batch)
                if isinstance(error, _PatchError)
                else {
                    "code": "file_write_error",
                    "message": _WRITE_FAILED.format(
                        path=batch.shown(path), reason=error.strerror or str(error)
                    ),
                }
            )
            attempts = getattr(error, "attempts_made", None)
            if attempts is not None:
                failure.update(retryable=True, attempts_made=attempts)
            return completed, failure
        batch.before.setdefault(path, before[path])
        batch.after[path] = batch.observed[path] = target
        completed.append(batch.shown(path))
        notes = batch.warnings.setdefault(path, [])
        notes.extend(warnings.get(path, []))
        if stale:
            notes.append(_STALE_WARNING.format(path=batch.shown(path)))
        if context.change_tracker is not None and before[path].link is None:
            try:
                old = _decode(before[path].payload or b"", path)
                new = _decode(target.payload or b"", path)
            except _PatchError:
                pass
            else:
                context.change_tracker.record_write(context.session_id, path, before=old, after=new)
        # Record the completed write before observing it: an observation failure must
        # not report that nothing happened or invite a blind replay.
        try:
            actual = _snapshot(path)
        except OSError as error:
            batch.blocked.update(before)
            reason = error.strerror or str(error)
            return completed, {
                "code": "file_read_error",
                "message": _UNCONFIRMED.format(path=batch.shown(path), reason=reason),
            }
        if actual.payload != target.payload or (
            target.mode is not None and actual.mode != target.mode
        ):
            batch.blocked.update(before)
            return completed, _error_data(_PatchError("file_changed", path=path), batch)
        pending[path] = expected[path] = batch.after[path] = batch.observed[path] = actual
        if actual.payload is not None:
            state.record_read(context.session_id, path)
    for path in before:
        batch.observed[path] = pending[path]
        if path not in changed and pending[path].payload is not None:
            state.record_read(context.session_id, path)
    return completed, None


def _rename(
    context: ToolContext,
    state: FileReadState,
    batch: _Batch,
    source: Path,
    destination: Path,
    before: dict[Path, _Snapshot],
) -> tuple[list[str], JsonObject | None]:
    """Move a link itself, or respell a file name, as one directory-entry rename."""
    snapshot = before[source]
    try:
        _rename_entry(source, destination, before)
    except OSError as error:
        batch.blocked.update(before)
        return [], {
            "code": "file_write_error",
            "message": _WRITE_FAILED.format(
                path=batch.shown(source), reason=error.strerror or str(error)
            ),
        }
    completed = [batch.shown(destination)]
    batch.before.setdefault(source, snapshot)
    if destination == source:
        origin = batch.respelled.get(source, (source, destination))[0]
        batch.respelled[source] = (origin, destination)
    else:
        completed.append(batch.shown(source))
        batch.before.setdefault(destination, before[destination])
        batch.after[source] = batch.observed[source] = _ABSENT
    # Record the completed rename before observing it, like completed writes.
    batch.after[destination] = batch.observed[destination] = snapshot
    try:
        actual = _observe_renamed(destination, snapshot)
    except (OSError, _PatchError) as error:
        batch.blocked.update({source, destination})
        return completed, _error_data(error, batch)
    batch.after[destination] = batch.observed[destination] = actual
    if actual.payload is not None:
        state.record_read(context.session_id, destination)
    return completed, None


def _guard_failure(
    context: ToolContext,
    state: FileReadState,
    batch: _Batch,
    reason: StaleReason,
    path: Path,
    snapshot: _Snapshot,
) -> JsonObject:
    """Refuse to replace a file the Session has not seen, and show it when small.

    Showing the whole current content is the read the guard asks for, so the
    file is stamped and the same call succeeds when sent again.
    """
    label = batch.shown(path)
    never = reason is StaleReason.NEVER_READ
    code = "file_not_read" if never else "file_modified_since_read"
    why = "this Session has not read it" if never else "it changed after this Session last read it"
    lead = f"{label} already exists and {why}, so it was not replaced."
    text = _text(snapshot)
    payload = snapshot.payload or b""
    lines = split_text_lines(text or "", keepends=True)
    if (
        text is not None
        and len(payload) <= _GUARD_CONTENT_MAX_BYTES
        and len(lines) <= _GUARD_CONTENT_MAX_LINES
        and _snapshot(path) == snapshot
    ):
        state.record_read(context.session_id, path)
        shown = "".join(add_line_numbers(lines, 1)).rstrip("\n")
        return {
            "code": code,
            "message": (
                f"{lead} Its current content follows and now counts as read: send the same "
                "call again to replace it, or change only the parts that need it."
            ),
            "content": shown,
        }
    read = model_tool_name("read")
    return {
        "code": code,
        "message": f'{lead} Read it with {read}(path="{label}"), then send the call again.',
    }


def _run_step(
    context: ToolContext,
    state: FileReadState,
    batch: _Batch,
    operation: _Operation,
    paths: dict[str, Path],
    outcome: JsonObject,
) -> None:
    resolved = set(paths.values())
    source = paths[operation.path]
    if resolved & batch.blocked:
        outcome.update(
            status="skipped",
            error={
                "code": "previous_operation_failed",
                "message": _DEPENDENCY_FAILED.format(
                    where=outcome["where"], path=batch.shown(source)
                ),
            },
        )
        return
    try:
        before = {path: _snapshot(path) for path in resolved}
        for path, snapshot in before.items():
            if path in batch.observed and snapshot != batch.observed[path]:
                batch.blocked.update(resolved)
                raise _PatchError("file_changed", path=path)
        destination = paths[operation.destination] if operation.destination else None
        if destination is not None and _renames_entry(source, destination, before[source]):
            completed, failure = _rename(context, state, batch, source, destination, before)
        else:
            if source in batch.failed_text:
                operation = replace(
                    operation, hunks=[replace(h, precise_only=True) for h in operation.hunks]
                )
            pending, warnings = _plan([operation], paths, before)
            if (
                operation.action == "add"
                and before[source].payload is not None
                and pending[source] != before[source]
                and not _is_empty(before[source].payload)
            ):
                stale = state.check_stale(context.session_id, source)
                if stale is not None:
                    error = _guard_failure(context, state, batch, stale, source, before[source])
                    outcome.update(status="failed", error=error)
                    batch.blocked.update(resolved)
                    return
            for path in resolved:
                if _snapshot(path) != before[path]:
                    batch.blocked.update(resolved)
                    raise _PatchError("file_changed", path=path)
            completed, failure = _commit(context, state, batch, before, pending, warnings)
        if operation.action == "add" and completed:
            batch.replaced.add(source)
        elif operation.destination and source in batch.replaced:
            batch.replaced.add(paths[operation.destination])
        if operation.action == "move" and destination is not None and completed:
            batch.moves[batch.shown(source)] = batch.shown(destination)
        if failure:
            outcome.update(status="partial" if completed else "failed", error=failure)
            if completed:
                failure["completed_paths"] = completed
                failure["pending_paths"] = [
                    batch.shown(p) for p in resolved if batch.shown(p) not in completed
                ]
            return
        if completed:
            outcome["status"] = "applied"
        elif operation.action == "update" and all(h.is_identity() for h in operation.hunks):
            outcome["status"] = "unchanged"
        else:
            outcome["status"] = "already_applied"
    except (OSError, _PatchError) as error:
        outcome.update(status="failed", error=_error_data(error, batch))
        if (
            source in batch.failed_text
            and isinstance(error, _PatchError)
            and error.code == "text_not_found"
        ):
            outcome["error"]["message"] += " " + _PRECISE_RECOVERY
    if outcome["status"] == "failed":
        if operation.action in {"add", "move"} or operation.destination:
            batch.blocked.update(resolved)
        else:
            batch.failed_text.add(source)


def _file_effects(batch: _Batch) -> list[tuple[Path, _Snapshot, _Snapshot]]:
    """Return net per-entry effects; a case-only rename reads like any other move."""
    effects = []
    for path, before in batch.before.items():
        after = batch.after[path]
        origin, current = batch.respelled.get(path, (path, path))
        if origin.name == current.name:
            effects.append((path, before, after))
        else:
            effects += [(current, _ABSENT, after), (origin, before, _ABSENT)]
    return effects


def _move_pairs(moves: dict[str, str]) -> dict[str, str]:
    """Return each move chain's first source and final destination."""
    pairs = {}
    for source, destination in moves.items():
        seen = {source}
        while destination in moves and destination not in seen:
            seen.add(destination)
            destination = moves[destination]
        pairs[source] = destination
    return pairs


def _file_reports(context: ToolContext, batch: _Batch) -> list[_FileReport]:
    effects = [effect for effect in _file_effects(batch) if effect[1] != effect[2]]
    by_label = {batch.shown(path): (path, before, after) for path, before, after in effects}
    # A completed move reads as one entry: its source deletion plus destination addition.
    moved = {
        source: destination
        for source, destination in _move_pairs(batch.moves).items()
        if source in by_label
        and destination in by_label
        and not by_label[source][2].exists
        and by_label[destination][2].exists
    }
    sources = {destination: source for source, destination in moved.items()}
    reports: list[_FileReport] = []
    added = removed = 0
    reported: set[str] = set()
    for path, before, after in effects:
        label = batch.shown(path)
        if label in reported:
            continue
        destination = None
        source = label if label in moved else sources.get(label)
        if source is not None:
            destination = moved[source]
            before = by_label[source][1]
            path, _, after = by_label[destination]
            reported.add(destination)
            label, kind = source, "moved"
        elif not before.exists:
            kind = "created"
        elif not after.exists:
            kind = "deleted"
        elif path in batch.replaced:
            kind = "replaced"
        else:
            kind = "updated"
        reported.add(label)
        report, plus, minus = file_report(
            path, label, kind, _text(before), _text(after), destination=destination
        )
        added += plus
        removed += minus
        if after.exists:
            report.notes = batch.warnings.get(path, [])
        reports.append(report)
    context.add_display_line_changes(added=added, removed=removed)
    context.add_display_count(len(effects), "files")
    return reports


def _cancelled(batch: _Batch) -> list[str]:
    """Return written paths that ended as they began, outside any completed move."""
    moved = set(batch.moves) | set(batch.moves.values())
    return [
        label
        for path, before, after in _file_effects(batch)
        if before == after and (label := batch.shown(path)) not in moved
    ]


def _call_cwd(context: ToolContext) -> Path:
    try:
        return context.effective_cwd.resolve()
    except (OSError, RuntimeError):
        return context.effective_cwd


def _locate_context(context: ToolContext, batch: _Batch, name: str, lines: list[str]) -> str:
    """Say where the lines of a context-only hunk are in the file, if they are there."""
    try:
        path = _resolve(context, name)
        if path.stat().st_size > _CONTEXT_SEARCH_MAX_BYTES:
            return ""
        content = _decode(path.read_bytes(), path)
    except (OSError, _PatchError):
        return ""
    text = "\n".join(lines)
    found = replace_fuzzy(
        content,
        text,
        text,
        replace_all=True,
        whole_lines=True,
        typographic=True,
    )
    if not isinstance(found, FuzzyReplacement):
        return ""
    ranges = []
    for start, end in found.before_spans[:3]:
        first = content.count("\n", 0, start) + 1
        last = content.count("\n", 0, max(start, end - 1)) + 1
        ranges.append(f"{first}" if first == last else f"{first}-{last}")
    where = ("line " if ranges == [str(ranges[0])] and "-" not in ranges[0] else "lines ") + (
        ", ".join(ranges)
    )
    return f" The unchanged lines match {batch.shown(path)} {where}."


def _request_failure(context: ToolContext, batch: _Batch, error: _PatchError) -> JsonObject:
    message = error.text(batch.shown)
    for name, lines in error.details.get("context_only", [])[:3]:
        message += _locate_context(context, batch, name, lines)
    return tool_failure(error.code, message + "\nNo file was changed.")


def _label_hunks(operations: list[_Operation]) -> None:
    for operation in operations:
        if operation.action == "update" and len(operation.hunks) > 1:
            for number, hunk in enumerate(operation.hunks, 1):
                hunk.label = hunk.label or f"hunk {number}"


def _execute(context: ToolContext, arguments: JsonObject, state: FileReadState) -> JsonObject:
    cwd = _call_cwd(context)
    batch = _Batch(shown=lambda path: display_search_path(path, cwd=cwd), cwd=cwd)
    try:
        arguments = normalize_patch_arguments(arguments)
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))
    try:
        operations = patch_operations(arguments)
    except _PatchError as error:
        return _request_failure(context, batch, error)
    _label_hunks(operations)
    resolved: dict[str, Path] = {}
    resolution_errors: dict[str, JsonObject] = {}
    for operation in operations:
        for name in (operation.path, operation.destination):
            if name is not None and name not in resolved and name not in resolution_errors:
                try:
                    resolved[name] = _resolve(context, name)
                except _PatchError as error:
                    resolution_errors[name] = _error_data(error, batch)
    # Delete and Move act on the named entry: a final link itself, not its target.
    entries: dict[tuple[str, bool], Path] = {}
    for operation in operations:
        if operation.action in {"delete", "move"} or operation.destination:
            roles = [(operation.path, False), (operation.destination, True)]
            for name, is_destination in roles:
                if name in resolved and (name, is_destination) not in entries:
                    try:
                        entries[name, is_destination] = _entry_path(
                            context, name, resolved[name], destination=is_destination
                        )
                    except _PatchError as error:
                        resolution_errors[name] = _error_data(error, batch)
    all_paths = set(resolved.values()) | set(entries.values())
    overlaps = {
        p for p in all_paths if any(p in q.parents or q in p.parents for q in all_paths if p != q)
    }
    with ExitStack() as locks:
        for path in sorted(all_paths, key=str):
            locks.enter_context(state.lock_path(path))
        for number, operation in enumerate(operations, 1):
            steps = [operation]
            if operation.action == "update":
                steps = [
                    replace(operation, destination=None, hunks=[h], hunk_number=i)
                    for i, h in enumerate(operation.hunks, 1)
                ]
                if operation.destination:
                    steps.append(_Operation("move", operation.path, operation.destination))
            operation_failed = False
            for step in steps:
                names = [n for n in (step.path, step.destination) if n is not None]
                paths: dict[str, Path] = {}
                for name in names:
                    target = (
                        entries.get((name, name == step.destination))
                        if step.action in {"delete", "move"}
                        else resolved.get(name)
                    )
                    if target is not None:
                        paths[name] = target
                label = batch.shown(paths[step.path]) if step.path in paths else step.path
                hunk_label = step.hunks[0].label if step.action == "update" else ""
                outcome: JsonObject = {
                    "operation": number,
                    "action": step.action,
                    "path": label,
                    "where": f"{label}, {hunk_label}" if hunk_label else label,
                }
                batch.results.append(outcome)
                entry_error = next(
                    (resolution_errors[n] for n in names if n in resolution_errors), None
                )
                overlap = next((p for p in paths.values() if p in overlaps), None)
                if overlap is not None:
                    entry_error = _error_data(_PatchError("overlapping_paths", path=overlap), batch)
                if entry_error:
                    outcome.update(status="failed", error=entry_error)
                elif operation_failed and step.action == "move":
                    # The file keeps its name, so the failed change can be resent as it was.
                    batch.blocked.update(paths.values())
                    destination = paths.get(str(step.destination))
                    message = _MOVE_SKIPPED.format(
                        path=label,
                        destination=batch.shown(destination) if destination else step.destination,
                    )
                    outcome.update(
                        status="skipped",
                        error={"code": "previous_operation_failed", "message": message},
                    )
                else:
                    _run_step(context, state, batch, step, paths, outcome)
                operation_failed |= outcome["status"] in {"failed", "skipped", "partial"}
    return patch_result(_file_reports(context, batch), batch.results, _cancelled(batch))


def _display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    path = None
    try:
        normalized = normalize_patch_arguments(arguments)
        if isinstance(normalized, dict):
            path = normalized.get("path")
            if not isinstance(path, str):
                path = patch_operations(normalized)[0].path
    except (ValueError, _PatchError):
        patch = (
            arguments.get("patch", arguments.get("input")) if isinstance(arguments, dict) else None
        )
        header = (
            next(filter(None, map(_HEADER.fullmatch, patch.split("\n"))), None)
            if isinstance(patch, str)
            else None
        )
        path = header[2].strip() if header else None
    if not isinstance(path, str) or not path:
        return ()
    return (
        ToolDisplayPart(value=path, kind="path", truncate="start", tooltip="always", copyable=True),
    )


def patch_targets(arguments: JsonObject) -> list[str]:
    """Return every path an apply_patch call names, for callers that vet targets first."""
    operations = patch_operations(normalize_patch_arguments(arguments))
    return [
        name
        for operation in operations
        for name in (operation.path, operation.destination)
        if name is not None
    ]


def make_apply_patch_handler(file_state: FileReadState) -> ToolHandler:
    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return _execute(context, arguments, file_state)

    return handler


_HIDDEN_ARGUMENT_KEYS = (
    "patch",
    "input",
    "patchText",
    "diff",
    "old_string",
    "new_string",
    "oldString",
    "newString",
    "old_str",
    "new_str",
    "content",
    "file_text",
    "edits",
)


def register_apply_patch_tool(registry: ToolRegistry, *, file_state: FileReadState) -> None:
    registry.register(
        APPLY_PATCH_TOOL_NAME,
        APPLY_PATCH_TOOL_DESCRIPTION,
        APPLY_PATCH_TOOL_PARAMETERS,
        offload_tool_handler(make_apply_patch_handler(file_state)),
        family="files",
        open_input_schema=True,
        argument_normalizer=normalize_patch_arguments,
        unadvertised_parameters=PATCH_HIDDEN_PARAMETERS,
        result_schema={"type": "object", "required": ["status", "content"]},
        display=ToolDisplay(
            parts_builder=_display_parts, hidden_argument_keys=_HIDDEN_ARGUMENT_KEYS
        ),
    )


__all__ = [
    "APPLY_PATCH_TOOL_DESCRIPTION",
    "APPLY_PATCH_TOOL_NAME",
    "APPLY_PATCH_TOOL_PARAMETERS",
    "make_apply_patch_handler",
    "patch_targets",
    "register_apply_patch_tool",
]
