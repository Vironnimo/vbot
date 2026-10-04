"""apply_patch calls in other harnesses' shapes, turned into canonical fields and operations.

Models trained on other agent harnesses call apply_patch with an edit Tool's
fields: ``file_path``/``old_string``/``new_string`` (Claude Code Edit, Gemini
``replace``), ``edits`` (MultiEdit), or ``content`` (Write, ``write_file``).
Each shape that names one exact change runs as that change. Shapes that combine
different changes, or that leave the change open, fail with the call to send
instead.

Canonical arguments keep ``patch`` (the advertised field, empty when other
fields carry the change) plus the unadvertised fields in
``PATCH_HIDDEN_PARAMETERS``.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from core.tools._patch_syntax import _Hunk, _Operation, _parse, _PatchError, _Replacement
from core.tools.call_syntax import SpellingAliases, normalize_call_arguments, spelling
from core.tools.contracts import ToolContract, compile_tool_contract
from core.tools.tools import JsonObject

APPLY_PATCH_TOOL_NAME = "apply_patch"

_TEXT: dict[str, Any] = {"type": "string"}
_PATH: dict[str, Any] = {"type": "string", "minLength": 1}
_EDIT_FIELDS = ("old_string", "new_string", "replace_all", "path")

# Accepted but not advertised: the fields other harnesses' edit Tools use.
PATCH_HIDDEN_PARAMETERS: dict[str, Any] = {
    "path": _PATH,
    "old_string": _TEXT,
    "new_string": _TEXT,
    "replace_all": {"type": "boolean"},
    "edits": {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "object",
            "properties": {
                "old_string": _TEXT,
                "new_string": _TEXT,
                "replace_all": {"type": "boolean"},
                "path": _PATH,
            },
            "required": ["old_string", "new_string"],
            "additionalProperties": False,
        },
    },
    "content": _TEXT,
}

# Fields that exist only while a call is translated; none reaches the handler.
_TRANSLATED_FIELDS: dict[str, Any] = {"edits": {"type": "array"}}

_FIELD_ALIASES = SpellingAliases(
    {
        "patch": ("input", "patch_text", "diff", "unified_diff", "patch_content"),
        "path": (
            "file_path",
            "file",
            "filename",
            "target_file",
            "absolute_path",
            "relative_path",
            "relative_workspace_path",
        ),
        "old_string": ("old_str", "old_text", "old", "search", "find", "original"),
        "new_string": ("new_str", "new_text", "new", "replace", "replacement", "replace_with"),
        "replace_all": ("all", "replace_all_occurrences", "global"),
        "content": (
            "contents",
            "file_text",
            "file_content",
            "file_contents",
            "text",
            "code",
            "code_content",
            "new_content",
        ),
    }
)

# Remarks some harnesses attach to an edit; they request no effect.
_REMARKS = frozenset({"explanation", "instructions", "instruction", "description"})

_CHANGE_FIELDS = ("patch", "old_string", "new_string", "edits", "content")
_SINGLE_EDIT_FIELDS = ("old_string", "new_string", "replace_all")


@cache
def _repair_contract() -> ToolContract:
    from core.tools.apply_patch import APPLY_PATCH_TOOL_PARAMETERS

    schema = dict(APPLY_PATCH_TOOL_PARAMETERS)
    schema["properties"] = {
        **APPLY_PATCH_TOOL_PARAMETERS["properties"],
        **PATCH_HIDDEN_PARAMETERS,
        **_TRANSLATED_FIELDS,
    }
    return compile_tool_contract(
        name=APPLY_PATCH_TOOL_NAME, input_schema=schema, require_closed_input=False
    )


def normalize_patch_arguments(arguments: Any) -> Any:
    """Return apply_patch arguments with other harnesses' shapes translated."""
    normalized = normalize_call_arguments(
        _repair_contract(),
        arguments,
        field_aliases=_FIELD_ALIASES,
        empty_as_omitted=("path", "replace_all"),
        placeholder_as_omitted=("patch",),
        # Patch text is never an object, so one under a patch spelling holds call fields.
        wrapping_fields=("patch",),
    )
    if not isinstance(normalized, dict):
        return normalized
    result = {key: value for key, value in normalized.items() if spelling(key) not in _REMARKS}
    # The MCP filesystem server's edit_file asks for a preview with dryRun; off, it
    # asks for nothing. Turned on, it stays an unknown parameter.
    for key in [key for key in result if spelling(key) == "dryrun" and result[key] is False]:
        del result[key]
    if isinstance(result.get("edits"), list):
        result["edits"] = [
            _edit_item(item, number) for number, item in enumerate(result["edits"], 1)
        ]
    _check_change(result)
    _carry_empty_text(result)
    return result


def _edit_item(item: Any, number: int) -> Any:
    if not isinstance(item, dict):
        return item
    edit: dict[str, Any] = {}
    for key, value in item.items():
        field = key if key in _EDIT_FIELDS else _FIELD_ALIASES.get(key, key)
        if field == "replace_all" and value in (None, ""):
            continue
        if field in edit and edit[field] != value:
            raise ValueError(
                f"edits item {number} gives {field} twice with different values; send one."
            )
        edit[field] = value
    edit = _repair_contract().normalize_arguments(edit)
    if isinstance(edit, dict) and "old_string" in edit and "new_string" not in edit:
        raise ValueError(
            f"edits item {number} has old_string but no new_string. Send new_string, "
            '"" to delete the text.'
        )
    if isinstance(edit, dict) and "old_string" not in edit:
        raise ValueError(
            f"edits item {number} has no old_string, the current text to replace. Send "
            'old_string and new_string (old_string="" creates a new file).'
        )
    return edit


def _check_change(arguments: dict[str, Any]) -> None:
    """Require exactly one kind of change, complete, with the file it applies to."""
    patch = arguments.get("patch")
    # The old/new kind is named by the fields the call gives, such as old_string alone.
    replacing = "/".join(field for field in ("old_string", "new_string") if field in arguments)
    kinds = {
        replacing if field in ("old_string", "new_string") else field
        for field in _CHANGE_FIELDS
        if field in arguments and (field != "patch" or (isinstance(patch, str) and patch.strip()))
    }
    if len(kinds) > 1:
        first, second = sorted(kinds)
        raise ValueError(
            f"The call gives both {first} and {second}. Send one of them; for several changes, "
            "put them all in one patch."
        )
    if replacing:
        if "new_string" not in arguments:
            raise ValueError(
                'old_string needs new_string, the text that replaces it (new_string="" deletes '
                "old_string)."
            )
        if "old_string" not in arguments:
            raise ValueError(
                'new_string needs old_string, the current text it replaces (old_string="" '
                "creates a new file)."
            )
    if "replace_all" in arguments and "old_string" not in arguments:
        raise ValueError("replace_all applies only to old_string and new_string.")
    edits = arguments.get("edits")
    needs_path = (replacing or "content" in arguments) or (
        isinstance(edits, list) and any(isinstance(e, dict) and "path" not in e for e in edits)
    )
    if needs_path and "path" not in arguments:
        raise ValueError(
            "The call names no file. Add path, relative to the working directory or absolute."
        )
    if kinds - {"patch"}:
        # The change lives in other fields; the required patch field stays empty.
        arguments.setdefault("patch", "")
    elif (
        "path" in arguments
        and not kinds
        and patch in (None, "")
        # A field the Tool does not know may hold the change; validation names it.
        and set(arguments) <= {"patch", *PATCH_HIDDEN_PARAMETERS}
    ):
        raise ValueError(
            f"The call names {arguments['path']} but no change. Send the change as patch, "
            "or as old_string and new_string."
        )


def _carry_empty_text(arguments: dict[str, Any]) -> None:
    """Hold the change in fields where an empty text survives later argument cleanup.

    Empty unadvertised root fields count as omitted after this normalizer runs, yet
    old_string="" creates a file, new_string="" deletes text and content="" empties
    a file. A single edit therefore travels as an edits item, and empty content as
    the equivalent Add File patch.
    """
    if "old_string" in arguments:
        edit = {field: arguments.pop(field) for field in _SINGLE_EDIT_FIELDS if field in arguments}
        arguments["edits"] = [edit]
    if arguments.get("content") == "":
        del arguments["content"]
        arguments["patch"] = f"*** Add File: {arguments['path']}"


def patch_operations(arguments: JsonObject) -> list[_Operation]:
    """Return the ordered operations that canonical apply_patch arguments request."""
    path = arguments.get("path")
    patch = arguments.get("patch")
    if isinstance(patch, str) and patch.strip():
        return _parse(patch, path if isinstance(path, str) else None)
    edits = arguments.get("edits")
    if isinstance(edits, list) and edits:
        return _edit_operations(path if isinstance(path, str) else "", edits)
    if not isinstance(path, str) or "content" not in arguments:
        raise _PatchError(
            "invalid_arguments",
            message='The patch is empty. Send patch="*** Begin Patch\\n...\\n*** End Patch".',
        )
    return [content_operation(path, arguments["content"])]


def content_operation(
    path: str, content: str, *, only_if_empty: bool = False, label: str = ""
) -> _Operation:
    """Create or replace a file with ``content``; its final line break follows the file."""
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    newline = "\r\n" if "\r\n" in content else "\r" if "\r" in content else "\n"
    hunks = []
    if text:
        lines = text.split("\n")
        no_newline = lines[-1] != ""
        if not no_newline:
            lines.pop()
        hunks = [_Hunk(lines=[("+", line) for line in lines], no_newline=no_newline)]
    return _Operation(
        "add",
        path,
        hunks=hunks,
        only_if_empty=only_if_empty,
        newline=newline,
        label=label,
        keeps_final_break=True,
    )


def _edit_operations(path: str, edits: list[JsonObject]) -> list[_Operation]:
    operations: list[_Operation] = []
    for number, edit in enumerate(edits, 1):
        target = edit.get("path", path)
        label = f"edit {number}" if len(edits) > 1 else ""
        if edit["old_string"] == "":
            operations.append(
                content_operation(target, edit["new_string"], only_if_empty=True, label=label)
            )
            continue
        hunk = _Hunk(
            replacement=_Replacement(
                edit["old_string"], edit["new_string"], edit.get("replace_all", False)
            ),
            label=label,
        )
        last = operations[-1] if operations else None
        if last is not None and last.action == "update" and last.path == target:
            last.hunks.append(hunk)
        else:
            operations.append(_Operation("update", target, hunks=[hunk]))
    return operations


__all__ = [
    "APPLY_PATCH_TOOL_NAME",
    "PATCH_HIDDEN_PARAMETERS",
    "content_operation",
    "normalize_patch_arguments",
    "patch_operations",
]
