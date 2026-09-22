"""V4A parsing, current-content planning, and coordinated file mutation."""

from __future__ import annotations

import json
import re
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path

from core.tools._argument_repair import normalize_call_arguments
from core.tools._change_preview import _change_preview
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
from core.tools._patch_hunks import _apply_hunk, _clean_additions, _ending, _hunk_text
from core.tools._patch_syntax import (
    _MESSAGES,
    _Operation,
    _parse,
    _PatchError,
)
from core.tools.arguments import split_text_lines
from core.tools.contracts import ToolContractError, compile_tool_contract
from core.tools.file_state import FileReadState, StaleReason, atomic_write_bytes, stale_failure_text
from core.tools.syntax_check import warning_for_edited_file, warning_for_written_file
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
    "Apply V4A patches with one or more edits across one or more files: replace, insert, "
    "or remove text; create, delete, or move files. Changes run in order; "
    "successful changes remain applied if another change fails."
)
APPLY_PATCH_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "patch": {
            "type": "string",
            "description": (
                "The complete V4A patch. Include changed lines and locating context together: "
                "+ for added lines, - for removed lines, a space for unchanged neighboring lines. "
                "Context-only calls do not select locations for later calls. "
                "Paths are relative to the working directory or absolute.\n"
                "Replacement and insertion across files:\n"
                "*** Begin Patch\n*** Update File: file_a.txt\n@@\n-old line\n+new line\n"
                "@@\n+inserted line\n existing line\n"
                "*** Update File: file_b.txt\n@@\n-old value\n+new value\n*** End Patch\n"
                "Repeat @@ blocks for separate locations and file headers for additional files, "
                "all inside the same Begin/End Patch. "
                "`*** Add File: path` followed by + lines creates or fully overwrites "
                "a file with that content. `*** Delete File: path` deletes a file. "
                "`*** Move File: source -> destination` moves a file.\n"
                "An Update block containing only + lines appends to the file. "
                "With `@@ existing full line`, those + lines are inserted after that line. "
                "End an Update block with `*** End of File` to restrict the edit to EOF."
            ),
        },
    },
    "required": ["patch"],
}

_PATCH_CONTRACT = compile_tool_contract(
    name=APPLY_PATCH_TOOL_NAME,
    input_schema=APPLY_PATCH_TOOL_PARAMETERS,
    require_closed_input=False,
)


def _normalize_patch_arguments(arguments: JsonObject) -> JsonObject:
    arguments = normalize_call_arguments(
        _PATCH_CONTRACT, arguments, field_aliases={"input": "patch"}
    )
    return arguments


_VALIDATION_FAILED = "Patch validation failed; no files were changed."
_WRITE_FAILED = (
    "Could not change {path}: {reason}. Inspect this path before retrying the failed entry."
)
_PARTIAL_GUIDANCE = (
    "Some entries are incomplete. Applied changes remain in place. Inspect entries marked "
    "partial before continuing; retry only failed or skipped entries using current content. "
    "Do not replay the whole patch."
)
_DEPENDENCY_FAILED = (
    "A previous operation left {path} unavailable or uncertain. Inspect this path and any "
    "move destination before retrying this entry."
)
_NONE_APPLIED = "No requested changes were applied. Correct the failed entries and retry."
_ALL_FAILED = "All {count} entries failed or were skipped. No files were changed."
_PRECISE_RECOVERY = (
    "After an earlier failure in this file, this hunk requires a unique exact or "
    "whitespace-normalized match. Inspect current content and retry this hunk "
    "with matching context."
)
_STALE_WARNING = (
    "{path} changed since this Session last read it. The patch was applied to current content."
)


def _decode(payload: bytes, path: str) -> str:
    if b"\x00" in payload:
        raise _PatchError("binary_file", path=path)
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise _PatchError("unsupported_encoding", path=path) from error


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
            if source.payload is not None:
                content = content.replace("\n", _ending(source.payload.decode("utf-8", "replace")))
                if source.payload.startswith(b"\xef\xbb\xbf"):
                    content = "\ufeff" + content.removeprefix("\ufeff")
            payload = content.encode("utf-8")
            if b"\x00" in payload:
                raise _PatchError("binary_file", path=displayed)
            pending[path] = _Snapshot(payload, source.mode)
            continue
        if not source.exists:
            raise _PatchError("file_not_found", path=displayed)
        target = paths[operation.destination] if operation.destination else path
        if target != path and pending[target].exists:
            raise _PatchError("destination_exists", path=model_path(target))
        if operation.action == "delete":
            pending[path] = _ABSENT
            continue
        if payload is None:
            # Content paths resolve through links; a link here appeared concurrently.
            raise _PatchError("file_changed", path=displayed)
        if operation.action == "update":
            content = _decode(payload, displayed)
            for index, hunk in enumerate(operation.hunks, operation.hunk_number):
                content, notes = _apply_hunk(content, hunk, displayed, index)
                warnings.setdefault(path, []).extend(notes)
            bom = b"\xef\xbb\xbf" if payload.startswith(b"\xef\xbb\xbf") else b""
            payload = bom + content.encode("utf-8")
            if b"\x00" in payload:
                raise _PatchError("binary_file", path=displayed)
        if target != path:
            pending[path] = _ABSENT
            warnings.setdefault(target, []).extend(warnings.pop(path, []))
        pending[target] = _Snapshot(payload, source.mode)
    return pending, warnings


def _change_details(
    path: Path, previous: _Snapshot, current: _Snapshot, *, replaced: bool = False
) -> tuple[JsonObject, int, int]:
    result: JsonObject = {
        "path": model_path(path),
        "action": ("add" if not previous.exists else "delete" if not current.exists else "update"),
    }
    if previous.link is not None or current.link is not None:
        return result, 0, 0  # A link entry has no text content to preview.
    before, after = previous.payload, current.payload
    try:
        new = _decode(after or b"", model_path(path))
        if replaced and after is not None:
            warning = warning_for_written_file(path, new)
            if warning:
                result["syntax_warning"] = warning
        old = _decode(before or b"", model_path(path))
    except _PatchError:
        return result, 0, 0
    old_lines = split_text_lines(old, keepends=True)
    new_lines = split_text_lines(new, keepends=True)
    old_starts, new_starts = [0], [0]
    for line in old_lines:
        old_starts.append(old_starts[-1] + len(line))
    for line in new_lines:
        new_starts.append(new_starts[-1] + len(line))
    before_spans, after_spans = [], []
    added = removed = 0
    for tag, i1, i2, j1, j2 in SequenceMatcher(
        None, old_lines, new_lines, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        added += j2 - j1
        removed += i2 - i1
        a, b, c, d = old_starts[i1], old_starts[i2], new_starts[j1], new_starts[j2]
        # Locate the changed characters inside a long line, not its first 240 characters.
        while a < b and c < d and old[a] == new[c]:
            a += 1
            c += 1
        while b > a and d > c and old[b - 1] == new[d - 1]:
            b -= 1
            d -= 1
        before_spans.append((a, b))
        after_spans.append((c, d))
    preview, omitted = _change_preview(old, new, tuple(before_spans), tuple(after_spans))
    result["preview"] = preview
    if omitted:
        result["preview_omitted_regions"] = omitted
    if after is not None and not replaced:
        warning = warning_for_edited_file(path, old, new)
        if warning:
            result["syntax_warning"] = warning
    return result, added, removed


@dataclass
class _Batch:
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
    results: list[JsonObject] = field(default_factory=list)


def _error_data(error: _PatchError | OSError) -> JsonObject:
    if isinstance(error, _PatchError):
        return {"code": error.code, "message": str(error), **error.details}
    return {"code": "file_read_error", "message": str(error)}


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
                raise _PatchError("file_changed", path=model_path(checked))

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
            failure: JsonObject = {
                "code": error.code if isinstance(error, _PatchError) else "file_write_error",
                "message": _WRITE_FAILED.format(path=model_path(path), reason=str(error)),
            }
            attempts = getattr(error, "attempts_made", None)
            if attempts is not None:
                failure.update(retryable=True, attempts_made=attempts)
            return completed, failure
        batch.before.setdefault(path, before[path])
        batch.after[path] = batch.observed[path] = target
        completed.append(model_path(path))
        notes = batch.warnings.setdefault(path, [])
        notes.extend(warnings.get(path, []))
        if stale:
            notes.append(_STALE_WARNING.format(path=model_path(path)))
        if context.change_tracker is not None and before[path].link is None:
            try:
                old = _decode(before[path].payload or b"", model_path(path))
                new = _decode(target.payload or b"", model_path(path))
            except _PatchError:
                pass
            else:
                context.change_tracker.record_write(context.session_id, path, before=old, after=new)
        # Record the completed write before observing it: an observation failure must
        # not report that nothing happened or invite a blind replay.
        try:
            actual = _snapshot(path)
            if actual.payload != target.payload or (
                target.mode is not None and actual.mode != target.mode
            ):
                raise _PatchError("file_changed", path=model_path(path))
        except (OSError, _PatchError) as error:
            batch.blocked.update(before)
            return completed, _error_data(error)
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
            "message": _WRITE_FAILED.format(path=model_path(source), reason=str(error)),
        }
    completed = [model_path(destination)]
    batch.before.setdefault(source, snapshot)
    if destination == source:
        origin = batch.respelled.get(source, (source, destination))[0]
        batch.respelled[source] = (origin, destination)
    else:
        completed.append(model_path(source))
        batch.before.setdefault(destination, before[destination])
        batch.after[source] = batch.observed[source] = _ABSENT
    # Record the completed rename before observing it, like completed writes.
    batch.after[destination] = batch.observed[destination] = snapshot
    try:
        actual = _observe_renamed(destination, snapshot)
    except (OSError, _PatchError) as error:
        batch.blocked.update({source, destination})
        return completed, _error_data(error)
    batch.after[destination] = batch.observed[destination] = actual
    if actual.payload is not None:
        state.record_read(context.session_id, destination)
    return completed, None


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
                "message": _DEPENDENCY_FAILED.format(path=model_path(source)),
            },
        )
        return
    try:
        before = {path: _snapshot(path) for path in resolved}
        for path, snapshot in before.items():
            if path in batch.observed and snapshot != batch.observed[path]:
                batch.blocked.update(resolved)
                raise _PatchError("file_changed", path=model_path(path))
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
            ):
                stale = state.check_stale(context.session_id, source)
                if stale is not None:
                    code, message = stale_failure_text(stale, source)
                    outcome.update(status="failed", error={"code": code, "message": message})
                    batch.blocked.update(resolved)
                    return
            for path in resolved:
                if _snapshot(path) != before[path]:
                    batch.blocked.update(resolved)
                    raise _PatchError("file_changed", path=model_path(path))
            completed, failure = _commit(context, state, batch, before, pending, warnings)
        if operation.action == "add" and completed:
            batch.replaced.add(source)
        elif operation.destination and source in batch.replaced:
            batch.replaced.add(paths[operation.destination])
        if failure:
            outcome.update(status="partial" if completed else "failed", error=failure)
            if completed:
                outcome["completed_paths"] = completed
                outcome["pending_paths"] = [
                    model_path(p) for p in resolved if model_path(p) not in completed
                ]
            return
        if completed:
            outcome["status"] = "applied"
        elif operation.action == "update" and all(
            _hunk_text(h, " -") == _hunk_text(h, " +") for h in operation.hunks
        ):
            outcome["status"] = "unchanged"
        else:
            outcome["status"] = "already_applied"
    except (OSError, _PatchError) as error:
        outcome.update(status="failed", error=_error_data(error))
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


def _batch_result(context: ToolContext, batch: _Batch) -> JsonObject:
    files = []
    added = removed = 0
    for path, before, after in _file_effects(batch):
        if before == after:
            continue
        details, plus, minus = _change_details(path, before, after, replaced=path in batch.replaced)
        added += plus
        removed += minus
        notes = list(dict.fromkeys(batch.warnings.get(path, []))) if after.exists else []
        if notes:
            details["warnings"] = notes
        files.append(details)
    context.add_display_line_changes(added=added, removed=removed)
    context.add_display_count(len(files), "files")
    failed = sum(r["status"] in {"failed", "skipped", "partial"} for r in batch.results)
    succeeded = len(batch.results) - failed
    if failed and not succeeded and not batch.before:
        if len(batch.results) == 1:
            error = batch.results[0]["error"]
            message = _NONE_APPLIED + "\n" + error["message"]
            details = {k: v for k, v in error.items() if k not in {"code", "message"}}
            if details:
                message += "\n" + json.dumps(details, ensure_ascii=False)
            return tool_failure(
                error["code"],
                message,
                retryable=error.get("retryable"),
                attempts_made=error.get("attempts_made"),
            )
        return tool_failure(
            "all_changes_failed",
            _ALL_FAILED.format(count=failed) + "\n" + json.dumps(batch.results, ensure_ascii=False),
        )
    data: JsonObject = {
        "status": "partial" if failed else "success",
        "total": len(batch.results),
        "succeeded": succeeded,
        "failed": failed,
        "results": batch.results,
        "files": files,
    }
    if failed:
        data["guidance"] = _PARTIAL_GUIDANCE
    if not files:
        data["no_change"] = True
    if batch.results and all(r["status"] == "already_applied" for r in batch.results):
        data["already_applied"] = True
    return tool_success(data)


def _execute(context: ToolContext, arguments: JsonObject, state: FileReadState) -> JsonObject:
    try:
        arguments = _normalize_patch_arguments(arguments)
    except ToolContractError as error:
        return tool_failure("invalid_arguments", str(error))
    patch = arguments.get("patch")
    if set(arguments) != {"patch"} or not isinstance(patch, str) or not patch.strip():
        return tool_failure("invalid_arguments", _MESSAGES["invalid_arguments"])
    try:
        operations = _parse(patch)
    except _PatchError as error:
        return tool_failure(error.code, _VALIDATION_FAILED + "\n" + str(error))
    batch = _Batch()
    resolved: dict[str, Path] = {}
    resolution_errors: dict[str, JsonObject] = {}
    for operation in operations:
        for name in (operation.path, operation.destination):
            if name is not None and name not in resolved and name not in resolution_errors:
                try:
                    resolved[name] = _resolve(context, name)
                except _PatchError as error:
                    resolution_errors[name] = _error_data(error)
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
                        resolution_errors[name] = _error_data(error)
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
                outcome: JsonObject = {
                    "operation": number,
                    "action": step.action,
                    "path": model_path(paths[step.path]) if step.path in paths else step.path,
                }
                if step.action == "update":
                    outcome["hunk"] = step.hunk_number
                if step.destination:
                    outcome["destination"] = (
                        model_path(paths[step.destination])
                        if step.destination in paths
                        else step.destination
                    )
                batch.results.append(outcome)
                entry_error = next(
                    (resolution_errors[n] for n in names if n in resolution_errors), None
                )
                overlap = next((p for p in paths.values() if p in overlaps), None)
                if overlap is not None:
                    entry_error = _error_data(
                        _PatchError("overlapping_paths", path=model_path(overlap))
                    )
                if entry_error:
                    outcome.update(status="failed", error=entry_error)
                else:
                    if operation_failed and step.action == "move":
                        batch.blocked.update(paths.values())
                    _run_step(context, state, batch, step, paths, outcome)
                operation_failed |= outcome["status"] in {"failed", "skipped", "partial"}
    return _batch_result(context, batch)


def _display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    try:
        arguments = _normalize_patch_arguments(arguments)
    except ToolContractError:
        return ()
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
        argument_normalizer=_normalize_patch_arguments,
        result_schema={
            "type": "object",
            "required": ["status", "total", "succeeded", "failed", "results", "files"],
        },
        display=ToolDisplay(parts_builder=_display_parts, hidden_argument_keys=("patch", "input")),
    )
