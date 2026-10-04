"""apply_patch: the V4A patch Tool.

``_patch_requests.py`` turns a call in any accepted shape into ordered
operations, ``_file_changes.py`` plans and commits them (``_edit_engine.py``
places each text change), and ``_patch_report.py`` renders the result. This
module owns the Tool's definition, its request failures, display and
registration.
"""

from __future__ import annotations

from core.tools._edit_engine import file_excerpt
from core.tools._file_changes import (
    ChangeBatch,
    cancelled_paths,
    change_batch,
    decode_text,
    file_reports,
    run_operations,
)
from core.tools._patch_entries import _resolve
from core.tools._patch_report import _excerpts, patch_result
from core.tools._patch_requests import (
    APPLY_PATCH_TOOL_NAME,
    PATCH_HIDDEN_PARAMETERS,
    normalize_patch_arguments,
    patch_operations,
)
from core.tools._patch_syntax import _HEADER, _Operation, _PatchError
from core.tools.arguments import split_text_lines
from core.tools.contracts import ToolContractError
from core.tools.file_state import FileReadState
from core.tools.fuzzy_match import FuzzyReplacement, replace_fuzzy
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
                "Under Update File, the lines of an @@ block follow the file from top to "
                "bottom: lines starting with a space stay unchanged, - lines are removed, and "
                "+ lines are added at their position. Each is a whole line; copy - and "
                "unchanged lines exactly from the file. To replace a line, write it as a - "
                "line; to insert above a line, write the + lines before it. Every @@ block "
                "needs a - or + line. Text after @@ is optional and names an earlier line, "
                "such as the enclosing function. "
                "Start another @@ block for another place in the same file. A block of only "
                "+ lines goes after the @@ line, or at the end of the file after a bare @@. "
                "Add File creates a file or replaces all of its content. Paths are relative "
                "to the working directory or absolute."
            ),
        },
    },
    "required": ["patch"],
}

_CONTEXT_SEARCH_MAX_BYTES = 2 * 1024 * 1024
# A context-only patch shows this many file lines above and below where its lines are.
_CONTEXT_EXCERPT_LINES = 2


def _locate_context(
    context: ToolContext, batch: ChangeBatch, name: str, lines: list[str]
) -> list[str]:
    """Show where the lines of a context-only hunk are in the file, if they are there."""
    try:
        path = _resolve(context, name)
        if path.stat().st_size > _CONTEXT_SEARCH_MAX_BYTES:
            return []
        content = decode_text(path.read_bytes(), path)
    except OSError, _PatchError:
        return []
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
        return []
    file_lines = split_text_lines(content)
    spans = [
        (content.count("\n", 0, start) + 1, content.count("\n", 0, max(start, end - 1)) + 1)
        for start, end in found.before_spans
    ]
    around = _CONTEXT_EXCERPT_LINES
    excerpts = [
        file_excerpt(file_lines, max(1, first - around), min(len(file_lines), last + around))
        for first, last in spans[:3]
    ]
    label = batch.shown(path)
    if len(spans) == 1:
        first, last = spans[0]
        where = f"line {first}" if first == last else f"lines {first}-{last}"
        return [f"The unchanged lines match {label} {where}:", *_excerpts(excerpts, label)]
    shown = "" if len(spans) <= 3 else ", the first 3 of them"
    return [
        f"The unchanged lines occur {len(spans)} times in {label}{shown}:",
        *_excerpts(excerpts, label),
        "Add unchanged lines until they occur only at the place you mean.",
    ]


def _request_failure(context: ToolContext, batch: ChangeBatch, error: _PatchError) -> JsonObject:
    message = [batch.error_text(error)]
    context.add_display_notice("error", message[0])
    for name, lines in error.details.get("context_only", [])[:3]:
        message.extend(_locate_context(context, batch, name, lines))
    return tool_failure(error.code, "\n".join(message) + "\nNo file was changed.")


def _label_hunks(operations: list[_Operation]) -> None:
    for operation in operations:
        if operation.action == "update" and len(operation.hunks) > 1:
            for number, hunk in enumerate(operation.hunks, 1):
                hunk.label = hunk.label or f"hunk {number}"


def _execute(context: ToolContext, arguments: JsonObject, state: FileReadState) -> JsonObject:
    batch = change_batch(context)
    try:
        arguments = normalize_patch_arguments(arguments)
    except ValueError as error:
        return tool_failure("invalid_arguments", f"{error}\nNo file was changed.")
    try:
        operations = patch_operations(arguments)
    except _PatchError as error:
        return _request_failure(context, batch, error)
    _label_hunks(operations)
    run_operations(context, state, batch, operations)
    return patch_result(
        context, file_reports(context, batch), batch.results, cancelled_paths(batch)
    )


def _display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    path = None
    try:
        normalized = normalize_patch_arguments(arguments)
        if isinstance(normalized, dict):
            path = normalized.get("path")
            if not isinstance(path, str):
                path = patch_operations(normalized)[0].path
    except ValueError, _PatchError:
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


def _normalize_call(arguments: object) -> object:
    """Normalize a call before dispatch; a refused call also says no file changed."""
    try:
        return normalize_patch_arguments(arguments)
    except ValueError as error:
        raise ToolContractError(f"{error}\nNo file was changed.") from error


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
        argument_normalizer=_normalize_call,
        unadvertised_parameters=PATCH_HIDDEN_PARAMETERS,
        result_schema={"type": "object", "required": ["status", "content"]},
        display=ToolDisplay(
            parts_builder=_display_parts, hidden_argument_keys=_HIDDEN_ARGUMENT_KEYS, details=True
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
