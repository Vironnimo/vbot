"""edit and write: the replacement dialect of the file edit Tools.

``apply_patch`` (V4A patches) is the file edit capability the user configures.
``edit`` (exact old/new text replacement, several edits per call) and ``write``
(create a file or replace all of its content) follow it: they are active exactly
when ``apply_patch`` is, and a denial of ``apply_patch`` removes them.

Chat offers one dialect per prompt epoch. ``edit_dialect`` picks it from the
primary route's Model family: GPT families were trained on V4A and get
``apply_patch``; every other Model gets ``edit`` and ``write``. A call to a
sibling the route did not offer still runs under that Tool's own contract, so
Chat's dispatch allowlist carries ``edit_tool_siblings`` of an offered edit Tool.

All three run on the same change pipeline (``_file_changes.py``) and engine
(``_edit_engine.py``). An ``edit`` call is one atomic step: every edit applies,
in order, or nothing is written.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Collection, Sequence
from functools import cache
from typing import Any, Literal

from core.tools._file_changes import cancelled_paths, change_batch, file_reports, run_operations
from core.tools._patch_report import failure_text, patch_result
from core.tools._patch_requests import APPLY_PATCH_TOOL_NAME, content_operation
from core.tools._patch_syntax import _Hunk, _Operation, _Replacement, _same_path
from core.tools.availability import TOOL_ACTIVATION_FOLLOWS
from core.tools.call_syntax import SpellingAliases, normalize_call_arguments, spelling
from core.tools.contracts import ToolContract, ToolContractError, compile_tool_contract
from core.tools.file_state import FileReadState
from core.tools.model_names import model_tool_name
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

EDIT_TOOL_NAME = "edit"
WRITE_TOOL_NAME = "write"
# The file edit Tools of both dialects; ``apply_patch`` is the configurable one.
EDIT_TOOL_NAMES = (APPLY_PATCH_TOOL_NAME, EDIT_TOOL_NAME, WRITE_TOOL_NAME)
_REPLACE_TOOLS = frozenset({EDIT_TOOL_NAME, WRITE_TOOL_NAME})

EditDialect = Literal["patch", "replace"]

EDIT_TOOL_DESCRIPTION = (
    "Replace text in a file. Put all changes to one file in one call; for several files, "
    "call edit once per file in the same response."
)
EDIT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "File to change, relative to the working directory or absolute.",
        },
        "edits": {
            "type": "array",
            "description": "Changes, applied in order.",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "old_string": {
                        "type": "string",
                        "description": (
                            "Exact text from the file. It must occur only once; add surrounding "
                            "lines until it does."
                        ),
                    },
                    "new_string": {"type": "string", "description": "Replacement text."},
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every occurrence. Omit to replace exactly one.",
                    },
                },
                "required": ["old_string", "new_string"],
            },
        },
    },
    "required": ["path", "edits"],
}

WRITE_TOOL_DESCRIPTION = "Create a file or replace all of its content."
WRITE_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "File to write, relative to the working directory or absolute.",
        },
        "content": {"type": "string", "description": "Complete file content."},
    },
    "required": ["path", "content"],
}

_PATH_ALIASES = ("file_path", "filename")
_EDIT_ALIASES = SpellingAliases(
    {
        "path": _PATH_ALIASES,
        "old_string": ("old_str", "old_text"),
        "new_string": ("new_str", "new_text"),
    }
)
_WRITE_ALIASES = SpellingAliases(
    {"path": _PATH_ALIASES, "content": ("text", "contents", "file_text")}
)
_EDIT_FIELDS = ("old_string", "new_string", "replace_all")
_TEXT: JsonObject = {"type": "string"}

# Shared failures in the words of each Tool; see ``_patch_syntax._MESSAGES``.
_EDIT_TEMPLATES = {
    "file_exists": (
        "{where}: old_string is empty, which creates a file, but the file already has "
        "content. Put the current text to replace in old_string, or call write to replace "
        "the whole file."
    ),
    "binary_file": "{path} is a binary file, so edit cannot change its text.",
    "unsupported_encoding": "{path} is not UTF-8 text, so edit cannot change its text.",
    "file_changed": (
        "{path} changed on disk while this edit ran. Read it, then send the edits it still needs."
    ),
}
_WRITE_TEMPLATES = {
    "file_changed": (
        "{path} changed on disk while this write ran. Read it before writing it again."
    ),
}
_NO_PATH = "The call names no file. Add path, relative to the working directory or absolute."


def edit_dialect(family: str) -> EditDialect:
    """Return the edit dialect a Model family is offered.

    GPT families (``gpt...`` except ``gpt-oss``, and the ``o`` reasoning
    families) get ``patch``: ``apply_patch``. Every other family, and an
    unknown one, gets ``replace``: ``edit`` and ``write``.
    """
    family = family.strip().casefold()
    if family.startswith("gpt") and not family.startswith("gpt-oss"):
        return "patch"
    if family == "o" or family.startswith("o-"):
        return "patch"
    return "replace"


def known_edit_dialect(names: Collection[str]) -> EditDialect | None:
    """Return the dialect of the edit Tools among ``names``, ``None`` when there are none.

    A prompt epoch keeps the dialect its Model was first shown, also after a
    Model change.
    """
    if APPLY_PATCH_TOOL_NAME in names:
        return "patch"
    if any(name in _REPLACE_TOOLS for name in names):
        return "replace"
    return None


def offer_edit_dialect(definitions: list[JsonObject], dialect: EditDialect) -> list[JsonObject]:
    """Keep the edit Tool definitions of ``dialect`` and drop those of the other one.

    ``replace`` needs ``edit``; without it, as under a policy that denies ``edit``,
    ``apply_patch`` stays. Definitions without ``apply_patch`` stay as they are.
    """
    names = {definition.get("name") for definition in definitions}
    if dialect == "replace" and EDIT_TOOL_NAME not in names:
        dialect = "patch"
    if dialect == "patch" and APPLY_PATCH_TOOL_NAME not in names:
        return definitions
    dropped = _REPLACE_TOOLS if dialect == "patch" else {APPLY_PATCH_TOOL_NAME}
    return [definition for definition in definitions if definition.get("name") not in dropped]


def edit_tool_siblings(names: Collection[str]) -> tuple[str, ...]:
    """Return the edit Tools missing from ``names`` when ``names`` holds one of them."""
    if not any(name in EDIT_TOOL_NAMES for name in names):
        return ()
    return tuple(name for name in EDIT_TOOL_NAMES if name not in names)


def offered_edit_tool(context: ToolContext) -> str | None:
    """Return the Tool the Agent sees for changing part of a file, if any."""
    for name in (EDIT_TOOL_NAME, APPLY_PATCH_TOOL_NAME):
        if context.offers(name):
            return name
    return None


# --- Call shapes ------------------------------------------------------------------------


@cache
def _edit_repair_contract() -> ToolContract:
    schema = copy.deepcopy(EDIT_TOOL_PARAMETERS)
    schema["properties"].update(old_string=_TEXT, new_string=_TEXT, replace_all={"type": "boolean"})
    return compile_tool_contract(
        name=EDIT_TOOL_NAME, input_schema=schema, require_closed_input=False
    )


@cache
def _edit_item_contract() -> ToolContract:
    return compile_tool_contract(
        name=EDIT_TOOL_NAME,
        input_schema=EDIT_TOOL_PARAMETERS["properties"]["edits"]["items"],
        require_closed_input=False,
    )


@cache
def _write_repair_contract() -> ToolContract:
    return compile_tool_contract(
        name=WRITE_TOOL_NAME, input_schema=WRITE_TOOL_PARAMETERS, require_closed_input=False
    )


def normalize_edit_arguments(arguments: Any) -> Any:
    """Return edit arguments as ``path`` plus ``edits``, other spellings translated.

    A flat ``old_string``/``new_string`` call is one edit. Every edit belongs to
    the call's one file; a shape that leaves the change open, or changes another
    file, fails with the call to send instead.
    """
    normalized = normalize_call_arguments(
        _edit_repair_contract(),
        arguments,
        field_aliases=_EDIT_ALIASES,
        field_normalizers={"edits": _edits_value},
        empty_as_omitted=("path", "replace_all"),
    )
    if not isinstance(normalized, dict):
        return normalized
    flat = {field: normalized.pop(field) for field in _EDIT_FIELDS if field in normalized}
    if flat and "edits" in normalized:
        if set(flat) == {"replace_all"}:
            raise ValueError(
                "replace_all belongs to one edit. Put it into the edits item it applies to."
            )
        raise ValueError(
            "The call gives both edits and old_string/new_string. Put every change into edits."
        )
    if flat:
        _check_flat_edit(flat)
        normalized["edits"] = [flat]
    elif "content" in normalized and "edits" not in normalized:
        write = model_tool_name(WRITE_TOOL_NAME)
        raise ValueError(
            f"edit replaces text inside a file and takes no content. To replace the whole "
            f"file, call {write} with path and content."
        )
    edits = normalized.get("edits")
    if isinstance(edits, list):
        normalized["edits"] = [_edit_item(item, number) for number, item in enumerate(edits, 1)]
        _take_item_paths(normalized)
    if "path" not in normalized:
        raise ValueError(_NO_PATH)
    if not edits and set(normalized) <= {"path", "edits"}:
        raise ValueError(
            f"The call names {normalized['path']} but no change. Send edits, each with "
            "old_string, the current text, and new_string, its replacement."
        )
    return normalized


def _edits_value(value: Any) -> Any:
    """Return ``edits`` sent as JSON text as the array it encodes."""
    if isinstance(value, str) and value.lstrip().startswith("["):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _check_flat_edit(edit: dict[str, Any]) -> None:
    if "old_string" not in edit:
        raise ValueError(
            'new_string needs old_string, the current text it replaces (old_string "" creates '
            "a file)."
        )
    if "new_string" not in edit:
        raise ValueError(
            'old_string needs new_string, the text that replaces it (new_string "" deletes '
            "old_string)."
        )


def _edit_item(item: Any, number: int) -> Any:
    """Return one ``edits`` item under canonical field names."""
    if not isinstance(item, dict):
        return item
    edit: dict[str, Any] = {}
    for key, value in item.items():
        field = _EDIT_ALIASES.get(key, key)
        if field not in (*_EDIT_FIELDS, "path"):
            field = next((name for name in _EDIT_FIELDS if spelling(name) == spelling(key)), key)
        if field not in (*_EDIT_FIELDS, "path"):
            raise ValueError(
                f'edits item {number} has "{key}", which is not a field of an edit. Each edit '
                "takes old_string, new_string and replace_all."
            )
        if field in {"replace_all", "path"} and value in (None, ""):
            continue
        if field in edit and edit[field] != value:
            raise ValueError(
                f"edits item {number} gives {field} twice with different values; send one."
            )
        edit[field] = value
    path = edit.pop("path", None)
    edit = _edit_item_contract().normalize_arguments(edit)
    if "old_string" not in edit:
        raise ValueError(
            f"edits item {number} has no old_string, the current text to replace. Send "
            'old_string and new_string (old_string "" creates a file).'
        )
    if "new_string" not in edit:
        raise ValueError(
            f'edits item {number} has old_string but no new_string. Send new_string, "" to '
            "delete the text."
        )
    if path is not None:
        edit["path"] = path
    return edit


def _take_item_paths(arguments: dict[str, Any]) -> None:
    """Accept an item path that names the call's file; refuse one that names another."""
    path = arguments.get("path")
    for number, edit in enumerate(arguments["edits"], 1):
        if not isinstance(edit, dict) or "path" not in edit:
            continue
        named = edit.pop("path")
        if path is None:
            path = arguments["path"] = named
        elif not _same_path(named, path):
            raise ValueError(
                f"edits item {number} changes {named}, but this call changes {path}. Call edit "
                "once per file in the same response, each with the edits of its file."
            )


def normalize_write_arguments(arguments: Any) -> Any:
    """Return write arguments as ``path`` plus ``content``, other spellings translated."""
    normalized = normalize_call_arguments(
        _write_repair_contract(),
        arguments,
        field_aliases=_WRITE_ALIASES,
        empty_as_omitted=("path",),
    )
    if not isinstance(normalized, dict):
        return normalized
    replacing = [field for field in (*_EDIT_FIELDS, "edits") if field in normalized]
    if replacing and "content" not in normalized:
        edit = model_tool_name(EDIT_TOOL_NAME)
        raise ValueError(
            f"write replaces the whole file and has no {replacing[0]}. To replace text inside "
            f"a file, call {edit} with path and edits."
        )
    if "path" not in normalized:
        raise ValueError(_NO_PATH)
    if "content" not in normalized:
        raise ValueError(
            f"The call names {normalized['path']} but no content. Send content, the complete "
            'text of the file ("" empties it).'
        )
    return normalized


def _refusing(normalizer: Any) -> Any:
    """Turn a normalizer's refusal into a contract error that says no file changed."""

    def normalize(arguments: object) -> object:
        try:
            return normalizer(arguments)
        except ValueError as error:
            raise ToolContractError(f"{error}\nNo file was changed.") from error

    return normalize


# --- Execution --------------------------------------------------------------------------


def _edit_operations(path: str, edits: Sequence[JsonObject]) -> list[_Operation]:
    """Return one file's operations: a creation for an empty old_string, else replacements."""
    total = len(edits)
    operations: list[_Operation] = []
    for number, edit in enumerate(edits, 1):
        label = f"edit {number} of {total}" if total > 1 else ""
        if edit["old_string"] == "":
            operations.append(
                content_operation(path, edit["new_string"], only_if_empty=True, label=label)
            )
            continue
        hunk = _Hunk(
            replacement=_Replacement(
                edit["old_string"], edit["new_string"], edit.get("replace_all", False)
            ),
            label=label,
        )
        if operations and operations[-1].action == "update":
            operations[-1].hunks.append(hunk)
        else:
            operations.append(_Operation("update", path, hunks=[hunk]))
    return operations


def _edit_failure(
    context: ToolContext, error: JsonObject, edits: Sequence[JsonObject]
) -> JsonObject:
    """Say that no edit was applied, why, and which edit to correct."""
    context.add_display_notice("error", str(error["message"]))
    lines = [failure_text(error)]
    total = len(edits)
    if total == 1:
        lines.append("No file was changed.")
    else:
        label = str(error.get("label", ""))
        number = next((n for n in range(1, total + 1) if label == f"edit {n} of {total}"), None)
        if number is not None and number > 1 and error.get("candidates"):
            earlier = "edit 1" if number == 2 else f"edits 1-{number - 1}"
            lines.append(f"Line numbers count the text as {earlier} left it.")
        none, every = (
            ("Neither edit was", "both edits")
            if total == 2
            else (f"None of the {total} edits were", f"all {total} edits")
        )
        closing = f"{none} applied, so no file was changed."
        if number is not None:
            closing += f" Send {every} again with edit {number} corrected."
        lines.append(closing)
    return tool_failure(
        str(error["code"]),
        "\n".join(lines),
        retryable=error.get("retryable"),
        attempts_made=error.get("attempts_made"),
    )


def _execute_edit(context: ToolContext, arguments: JsonObject, state: FileReadState) -> JsonObject:
    edits = arguments["edits"]
    batch = change_batch(context, _EDIT_TEMPLATES)
    run_operations(context, state, batch, _edit_operations(arguments["path"], edits), atomic=True)
    files = file_reports(context, batch)
    [outcome] = batch.results
    if outcome["status"] == "failed":
        return _edit_failure(context, outcome["error"], edits)
    return patch_result(context, files, batch.results, cancelled_paths(batch))


def _execute_write(context: ToolContext, arguments: JsonObject, state: FileReadState) -> JsonObject:
    batch = change_batch(context, _WRITE_TEMPLATES)
    operation = content_operation(arguments["path"], arguments["content"])
    run_operations(context, state, batch, [operation])
    return patch_result(
        context, file_reports(context, batch), batch.results, cancelled_paths(batch)
    )


def make_edit_handler(file_state: FileReadState) -> ToolHandler:
    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return _execute_edit(context, arguments, file_state)

    return handler


def make_write_handler(file_state: FileReadState) -> ToolHandler:
    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return _execute_write(context, arguments, file_state)

    return handler


def _path_parts(normalizer: Any, arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    try:
        normalized = normalizer(arguments)
    except ValueError:
        normalized = arguments
    path = normalized.get("path") if isinstance(normalized, dict) else None
    if not isinstance(path, str) or not path:
        return ()
    return (
        ToolDisplayPart(value=path, kind="path", truncate="start", tooltip="always", copyable=True),
    )


def _edit_display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    return _path_parts(normalize_edit_arguments, arguments)


def _write_display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    return _path_parts(normalize_write_arguments, arguments)


_EDIT_HIDDEN_ARGUMENT_KEYS = (
    "edits",
    "old_string",
    "new_string",
    "oldString",
    "newString",
    "old_str",
    "new_str",
    "oldText",
    "newText",
    "old_text",
    "new_text",
)
_WRITE_HIDDEN_ARGUMENT_KEYS = ("content", "contents", "text", "file_text")
_RESULT_SCHEMA: JsonObject = {"type": "object", "required": ["status", "content"]}


def register_edit_tools(registry: ToolRegistry, *, file_state: FileReadState) -> None:
    """Register ``edit`` and ``write``, which follow ``apply_patch``."""
    for name, description, parameters, handler, normalizer, parts, hidden in (
        (
            EDIT_TOOL_NAME,
            EDIT_TOOL_DESCRIPTION,
            EDIT_TOOL_PARAMETERS,
            make_edit_handler(file_state),
            normalize_edit_arguments,
            _edit_display_parts,
            _EDIT_HIDDEN_ARGUMENT_KEYS,
        ),
        (
            WRITE_TOOL_NAME,
            WRITE_TOOL_DESCRIPTION,
            WRITE_TOOL_PARAMETERS,
            make_write_handler(file_state),
            normalize_write_arguments,
            _write_display_parts,
            _WRITE_HIDDEN_ARGUMENT_KEYS,
        ),
    ):
        registry.register(
            name,
            description,
            parameters,
            offload_tool_handler(handler),
            catalog_visible=False,
            family="files",
            activation=TOOL_ACTIVATION_FOLLOWS,
            activation_source=APPLY_PATCH_TOOL_NAME,
            open_input_schema=True,
            argument_normalizer=_refusing(normalizer),
            result_schema=_RESULT_SCHEMA,
            display=ToolDisplay(parts_builder=parts, hidden_argument_keys=hidden, details=True),
        )


__all__ = [
    "EDIT_TOOL_DESCRIPTION",
    "EDIT_TOOL_NAME",
    "EDIT_TOOL_NAMES",
    "EDIT_TOOL_PARAMETERS",
    "WRITE_TOOL_DESCRIPTION",
    "WRITE_TOOL_NAME",
    "WRITE_TOOL_PARAMETERS",
    "EditDialect",
    "edit_dialect",
    "edit_tool_siblings",
    "known_edit_dialect",
    "make_edit_handler",
    "make_write_handler",
    "offer_edit_dialect",
    "offered_edit_tool",
    "register_edit_tools",
]
