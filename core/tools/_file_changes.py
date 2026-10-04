"""Coordinated file changes: plan operations against current content and commit them.

``apply_patch``, ``edit`` and ``write`` turn their calls into ordered operations
(``_patch_syntax._Operation``). This module runs them: it resolves and locks the
paths, plans each step in memory (``_edit_engine.apply_hunks`` places text
changes), checks the read-before-write gate, writes atomically, verifies each
write, and collects what the Model and the user read about the call
(``file_reports``, the step outcomes in ``ChangeBatch.results``).

A step is the unit that succeeds or fails as a whole. ``apply_patch`` runs each
hunk, Add, Delete and Move as its own step, so a failed step leaves the others
applied. ``edit`` runs its whole call as one atomic step.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path

from core.sessions import SessionAddress
from core.tools._edit_engine import apply_hunks, clean_additions, line_ending
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
from core.tools._patch_report import _FileReport, file_report
from core.tools._patch_syntax import _Operation, _PatchError
from core.tools._path_suggestions import missing_file_message
from core.tools._read_text import add_line_numbers, plain_line_end
from core.tools.arguments import split_text_lines
from core.tools.file_state import (
    FileReadState,
    StaleReason,
    atomic_write_bytes,
    os_error_reason,
)
from core.tools.model_names import model_tool_name
from core.tools.search import display_search_path
from core.tools.tools import JsonObject, ToolContext

_BOM = b"\xef\xbb\xbf"
_WRITE_FAILED = "Could not change {path}: {reason}. Check this path before resending this change."
_WRITE_BUSY = (
    "Could not change {path}: {reason}. {path} is unchanged; send this change again after "
    "that program has finished, for example a running test or script."
)
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
_STALE_FAILURE = "{path} changed after this Session last read it; read it again before resending."
# Failures that mean the call's view of the file differs from its current content.
_MISMATCH_CODES = frozenset(
    {"text_not_found", "context_not_found", "ambiguous_match", "ambiguous_context"}
)
# A guard failure shows the whole current file when it is this small.
_GUARD_CONTENT_MAX_BYTES = 16 * 1024
_GUARD_CONTENT_MAX_LINES = 400


def decode_text(payload: bytes, path: object) -> str:
    """Decode file bytes as UTF-8 text, refusing binary and other encodings."""
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
        return decode_text(snapshot.payload, "")
    except _PatchError:
        return None


def _is_empty(payload: bytes | None) -> bool:
    """Whether an existing file holds nothing a replacement could lose."""
    if payload is None:
        return False
    return not payload.removeprefix(_BOM).strip()


def _plan(
    operations: list[_Operation],
    paths: dict[str, Path],
    before: dict[Path, _Snapshot],
    change_ends: dict[Path, int | None],
) -> tuple[dict[Path, _Snapshot], dict[Path, list[str]]]:
    """Plan the operations' effects; each Update's change end goes into ``change_ends``.

    See ``apply_hunks`` for what a change end orders.
    """
    pending = before.copy()
    warnings: dict[Path, list[str]] = {}
    for operation in operations:
        path = paths[operation.path]
        source = pending[path]
        payload = source.payload
        if operation.action == "add":
            if operation.only_if_empty and source.exists and not _is_empty(payload):
                raise _PatchError("file_exists", path=path, label=operation.label)
            if operation.minus_line and source.exists and not _is_empty(payload):
                # Replacing content, a - line may be a removal meant for Update File.
                line, text = operation.minus_line
                raise _PatchError(
                    "invalid_patch", template="add_minus_line", path=path, line=line, text=text
                )
            for index, hunk in enumerate(operation.hunks):
                operation.hunks[index], notes = clean_additions(hunk, path)
                warnings.setdefault(path, []).extend(notes)
            content = "\n".join(t for h in operation.hunks for _, t in h.lines)
            if operation.hunks and not operation.hunks[-1].no_newline:
                content += "\n"
            if source.payload is not None:
                content = content.replace(
                    "\n", line_ending(source.payload.decode("utf-8", "replace"))
                )
                if source.payload.startswith(_BOM):
                    content = "﻿" + content.removeprefix("﻿")
            elif operation.newline != "\n":
                content = content.replace("\n", operation.newline)
            payload = content.encode("utf-8")
            if b"\x00" in payload:
                raise _PatchError("binary_file", template="nul_text", path=path)
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
            content = decode_text(payload, path)
            content, notes, change_ends[path] = apply_hunks(
                content, operation.hunks, path, change_ends.get(path)
            )
            warnings.setdefault(path, []).extend(notes)
            bom = _BOM if payload.startswith(_BOM) else b""
            payload = bom + content.encode("utf-8")
            if b"\x00" in payload:
                raise _PatchError("binary_file", template="nul_text", path=path)
        if target != path:
            pending[path] = _ABSENT
            warnings.setdefault(target, []).extend(warnings.pop(path, []))
        pending[target] = _Snapshot(payload, source.mode)
    return pending, warnings


@dataclass
class ChangeBatch:
    """One call's file changes: what it observed, changed and reports.

    ``templates`` replaces the wording of shared failure codes for the Tool that
    runs the call, keyed by template name (see ``_patch_syntax._MESSAGES``).
    """

    shown: Callable[[Path], str]
    cwd: Path
    templates: Mapping[str, str] = field(default_factory=dict)
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
    # Where the line holding the current Update's last completed change starts,
    # by file; a later hunk takes the first of several occurrences after it.
    change_ends: dict[Path, int | None] = field(default_factory=dict)
    results: list[JsonObject] = field(default_factory=list)

    def error_text(self, error: _PatchError) -> str:
        """Render ``error`` with this call's path presentation and wording."""
        return error.text(self.shown, self.templates)


def change_batch(context: ToolContext, templates: Mapping[str, str] | None = None) -> ChangeBatch:
    """Start the batch of one call, showing paths relative to its working directory."""
    cwd = _call_cwd(context)
    return ChangeBatch(
        shown=lambda path: display_search_path(path, cwd=cwd), cwd=cwd, templates=templates or {}
    )


def _os_reason(error: OSError, batch: ChangeBatch) -> str:
    """Describe a file-system error by its reason and shown path, never an absolute one."""
    reason = os_error_reason(error)
    if error.filename:
        return f"{batch.shown(Path(error.filename))}: {reason}"
    return reason


def _write_failure(path: str, error: OSError) -> JsonObject:
    """Describe a failed write, delete or rename of the shown ``path``."""
    # Windows sharing and lock violations: another program holds the file open.
    held_open = getattr(error, "winerror", None) in {32, 33}
    failure: JsonObject = {
        "code": "file_write_error",
        "message": (_WRITE_BUSY if held_open else _WRITE_FAILED).format(
            path=path, reason=os_error_reason(error)
        ),
    }
    attempts = getattr(error, "attempts_made", None)
    if attempts is not None:
        failure.update(retryable=True, attempts_made=attempts)
    return failure


def _error_data(error: _PatchError | OSError, batch: ChangeBatch) -> JsonObject:
    if not isinstance(error, _PatchError):
        return {"code": "file_read_error", "message": f"Could not read {_os_reason(error, batch)}."}
    data: JsonObject = {"code": error.code, "message": batch.error_text(error), **error.details}
    path = error.values.get("path")
    if isinstance(path, Path):
        data["path_label"] = batch.shown(path)
        if error.code == "file_not_found":
            data["message"] = missing_file_message(path, batch.cwd)
    label = error.values.get("label")
    if isinstance(label, str) and label:
        data["label"] = label
    return data


def _commit(
    context: ToolContext,
    state: FileReadState,
    batch: ChangeBatch,
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
            if isinstance(error, _PatchError):
                return completed, _error_data(error, batch)
            return completed, _write_failure(batch.shown(path), error)
        batch.before.setdefault(path, before[path])
        batch.after[path] = batch.observed[path] = target
        completed.append(batch.shown(path))
        notes = batch.warnings.setdefault(path, [])
        notes.extend(warnings.get(path, []))
        if stale:
            notes.append(_STALE_WARNING.format(path=batch.shown(path)))
        if context.change_tracker is not None and before[path].link is None:
            try:
                old = decode_text(before[path].payload or b"", path)
                new = decode_text(target.payload or b"", path)
            except _PatchError:
                pass
            else:
                context.change_tracker.record_write(
                    (
                        SessionAddress(context.project_id, context.agent_id, context.session_id),
                        context.run_id,
                    ),
                    path,
                    before=old,
                    after=new,
                )
        # Record the completed write before observing it: an observation failure must
        # not report that nothing happened or invite a blind replay.
        try:
            actual = _snapshot(path)
        except OSError as error:
            batch.blocked.update(before)
            reason = os_error_reason(error)
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
    batch: ChangeBatch,
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
        return [], _write_failure(batch.shown(source), error)
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
    batch: ChangeBatch,
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
        # A CRLF file shows plain line breaks, as read shows it.
        plain = [plain_line_end(line) for line in lines]
        shown = "".join(add_line_numbers(plain, 1)).rstrip("\n")
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
    batch: ChangeBatch,
    operations: list[_Operation],
    paths: dict[str, Path],
    outcome: JsonObject,
) -> None:
    """Plan and commit ``operations`` as one step: all of their changes, or none."""
    first = operations[0]
    resolved = set(paths.values())
    source = paths[first.path]
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
    adds = any(operation.action == "add" for operation in operations)
    moving = len(operations) == 1 and first.destination is not None
    try:
        before = {path: _snapshot(path) for path in resolved}
        for path, snapshot in before.items():
            if path in batch.observed and snapshot != batch.observed[path]:
                batch.blocked.update(resolved)
                raise _PatchError("file_changed", path=path)
        destination = paths[first.destination] if moving and first.destination else None
        if destination is not None and _renames_entry(source, destination, before[source]):
            completed, failure = _rename(context, state, batch, source, destination, before)
        else:
            if source in batch.failed_text:
                operations = [
                    replace(op, hunks=[replace(h, precise_only=True) for h in op.hunks])
                    for op in operations
                ]
            change_ends = dict(batch.change_ends)
            pending, warnings = _plan(operations, paths, before, change_ends)
            if (
                adds
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
            if not failure:
                batch.change_ends.update(change_ends)
        if adds and completed:
            batch.replaced.add(source)
        elif moving and first.destination and source in batch.replaced:
            batch.replaced.add(paths[first.destination])
        if first.action == "move" and destination is not None and completed:
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
        elif all(
            op.action == "update" and all(h.is_identity() for h in op.hunks) for op in operations
        ):
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
        if (
            isinstance(error, _PatchError)
            and error.code in _MISMATCH_CODES
            and state.check_stale(context.session_id, source) is StaleReason.MODIFIED
        ):
            outcome["error"]["message"] += " " + _STALE_FAILURE.format(path=batch.shown(source))
    if outcome["status"] == "failed":
        if any(op.action in {"add", "move"} or op.destination for op in operations):
            batch.blocked.update(resolved)
        else:
            batch.failed_text.add(source)
            # A failed hunk's target keeps its old lines, so the earlier change no
            # longer tells which occurrence a later hunk means.
            batch.change_ends.pop(source, None)


def _steps(operation: _Operation) -> list[_Operation]:
    """Split one patch operation into its steps: each hunk, then a move."""
    if operation.action != "update":
        return [operation]
    steps = [
        replace(operation, destination=None, hunks=[h], hunk_number=i)
        for i, h in enumerate(operation.hunks, 1)
    ]
    if operation.destination:
        steps.append(_Operation("move", operation.path, operation.destination))
    return steps


def run_operations(
    context: ToolContext,
    state: FileReadState,
    batch: ChangeBatch,
    operations: list[_Operation],
    *,
    atomic: bool = False,
) -> None:
    """Apply ``operations`` in order under their path locks; outcomes go to ``batch.results``.

    Each hunk, Add, Delete and Move is its own step, so the steps that succeed
    stay applied when another fails. With ``atomic``, all ``operations`` form one
    step that changes everything or nothing.
    """
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
    groups = [operations] if atomic else [[operation] for operation in operations]
    with ExitStack() as locks:
        for path in sorted(all_paths, key=str):
            locks.enter_context(state.lock_path(path))
        for number, group in enumerate(groups, 1):
            # Occurrences are ordered by earlier changes of the same Update only.
            batch.change_ends.clear()
            steps = [group] if atomic else [[step] for step in _steps(group[0])]
            operation_failed = False
            for step in steps:
                first = step[0]
                names = list(
                    dict.fromkeys(
                        n for op in step for n in (op.path, op.destination) if n is not None
                    )
                )
                paths: dict[str, Path] = {}
                for name in names:
                    target = (
                        entries.get((name, name == first.destination))
                        if first.action in {"delete", "move"}
                        else resolved.get(name)
                    )
                    if target is not None:
                        paths[name] = target
                label = batch.shown(paths[first.path]) if first.path in paths else first.path
                hunk_label = first.hunks[0].label if not atomic and first.action == "update" else ""
                outcome: JsonObject = {
                    "operation": number,
                    "action": step[-1].action,
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
                elif operation_failed and first.action == "move":
                    # The file keeps its name, so the failed change can be resent as it was.
                    batch.blocked.update(paths.values())
                    destination = paths.get(str(first.destination))
                    message = _MOVE_SKIPPED.format(
                        path=label,
                        destination=batch.shown(destination) if destination else first.destination,
                    )
                    outcome.update(
                        status="skipped",
                        error={"code": "previous_operation_failed", "message": message},
                    )
                else:
                    _run_step(context, state, batch, step, paths, outcome)
                operation_failed |= outcome["status"] in {"failed", "skipped", "partial"}


def _file_effects(batch: ChangeBatch) -> list[tuple[Path, _Snapshot, _Snapshot]]:
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


def file_reports(context: ToolContext, batch: ChangeBatch) -> list[_FileReport]:
    """Report each changed file's net effect, and record its diff for the user's display."""
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
        before_text, after_text = _text(before), _text(after)
        report = file_report(path, label, kind, before_text, after_text, destination=destination)
        change = context.add_display_file_change(
            label, kind, before_text, after_text, destination=destination
        )
        added += change["added"]
        removed += change["removed"]
        if after.exists:
            report.notes = batch.warnings.get(path, [])
        reports.append(report)
    if effects:
        context.add_display_line_changes(added=added, removed=removed)
        context.add_display_count(len(effects), "files")
    return reports


def cancelled_paths(batch: ChangeBatch) -> list[str]:
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
    except OSError, RuntimeError:
        return context.effective_cwd


__all__ = [
    "ChangeBatch",
    "cancelled_paths",
    "change_batch",
    "decode_text",
    "file_reports",
    "run_operations",
]
