"""apply_patch calls in other harnesses' shapes, turned into canonical fields and operations.

Models trained on other agent harnesses call apply_patch with an edit Tool's
fields: ``file_path``/``old_string``/``new_string`` (Claude Code Edit, Gemini
``replace``), ``edits`` (MultiEdit), ``content`` (Write, ``write_file``),
``command: "str_replace"`` (text-editor Tools), or ``diff`` with SEARCH/REPLACE
blocks or a unified diff. Each shape that names one exact change runs as that
change. Shapes that combine different changes, or that leave the change open,
fail with the call to send instead.

Canonical arguments keep ``patch`` (the advertised field, empty when other
fields carry the change) plus the unadvertised fields in
``PATCH_HIDDEN_PARAMETERS``.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from core.tools._argument_repair import normalize_call_arguments
from core.tools._call_vocabulary import SpellingAliases, spelling
from core.tools._patch_syntax import _Hunk, _Operation, _parse, _PatchError, _Replacement
from core.tools.contracts import ToolContract, compile_tool_contract
from core.tools.model_names import model_tool_name
from core.tools.tools import JsonObject

APPLY_PATCH_TOOL_NAME = "apply_patch"

_TEXT: dict[str, Any] = {"type": "string"}
_COUNT: dict[str, Any] = {"type": "integer", "minimum": 1}
_PATH: dict[str, Any] = {"type": "string", "minLength": 1}
_EDIT_FIELDS = ("old_string", "new_string", "replace_all", "expected_replacements", "path")

# Accepted but not advertised: the fields other harnesses' edit Tools use.
PATCH_HIDDEN_PARAMETERS: dict[str, Any] = {
    "path": _PATH,
    "old_string": _TEXT,
    "new_string": _TEXT,
    "replace_all": {"type": "boolean"},
    "expected_replacements": _COUNT,
    "edits": {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "object",
            "properties": {
                "old_string": _TEXT,
                "new_string": _TEXT,
                "replace_all": {"type": "boolean"},
                "expected_replacements": _COUNT,
                "path": _PATH,
            },
            "required": ["old_string", "new_string"],
            "additionalProperties": False,
        },
    },
    "content": _TEXT,
    "insert_line": {"type": "integer", "minimum": 0},
}

# Fields that exist only while a call is translated; none reaches the handler.
_TRANSLATED_FIELDS: dict[str, Any] = {"mode": _TEXT, "command": _TEXT, "edits": {"type": "array"}}

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
        "old_string": (
            "old_str",
            "old_text",
            "old",
            "search",
            "find",
            "target_content",
            "original",
        ),
        "new_string": (
            "new_str",
            "new_text",
            "new",
            "replace",
            "replacement",
            "replace_with",
            "replacement_content",
            "insert_text",
        ),
        "replace_all": ("all", "replace_all_occurrences", "allow_multiple", "global"),
        "expected_replacements": ("expected_occurrences", "occurrences"),
        "edits": ("replacement_chunks", "chunks"),
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
        "insert_line": ("insert_after_line", "insert_after"),
    }
)

# Remarks some harnesses attach to an edit; they request no effect. Roo's
# write_to_file adds line_count, a count of the content's lines.
_REMARKS = frozenset({"explanation", "instructions", "instruction", "description", "linecount"})
# Switches that ask for nothing extra while off: Windsurf's EmptyFile, Roo's
# use_regex and ignore_case, and dryRun from the MCP filesystem server. Turned
# on, each asks for an effect of its own and stays an unknown parameter, except
# EmptyFile without content, which is an empty file.
_OFF_SWITCHES = frozenset({"emptyfile", "useregex", "ignorecase", "dryrun"})

_CHANGE_FIELDS = {
    "patch": "patch",
    "old_string": "old_string/new_string",
    "new_string": "old_string/new_string",
    "edits": "edits",
    "content": "content",
}
_SINGLE_EDIT_FIELDS = ("old_string", "new_string", "replace_all", "expected_replacements")


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
        empty_as_omitted=("path", "replace_all", "expected_replacements", "insert_line"),
        placeholder_as_omitted=("patch",),
    )
    if not isinstance(normalized, dict):
        return normalized
    result = {key: value for key, value in normalized.items() if spelling(key) not in _REMARKS}
    _drop_off_switches(result)
    if "code_edit" in result:
        path = result.get("path")
        target = path if isinstance(path, str) else "<path>"
        raise ValueError(
            "code_edit cannot be applied: it marks unchanged code with placeholder "
            "comments, so the exact change is unknown. Send the exact lines instead: "
            f'patch="*** Begin Patch\\n*** Update File: {target}'
            '\\n@@\\n-old line\\n+new line\\n*** End Patch".'
        )
    _translate_command(result)
    if isinstance(result.get("edits"), list):
        result["edits"] = [
            _edit_item(item, number) for number, item in enumerate(result["edits"], 1)
        ]
    _check_change(result)
    _carry_empty_text(result)
    return result


def _drop_off_switches(arguments: dict[str, Any]) -> None:
    for key, value in list(arguments.items()):
        name = spelling(key)
        if name not in _OFF_SWITCHES:
            continue
        if value is True and name == "emptyfile" and arguments.get("content", "") == "":
            arguments["content"] = ""
            del arguments[key]
        elif value is False:
            del arguments[key]


def _translate_command(arguments: dict[str, Any]) -> None:
    """Check a harness's mode or command field against the fields it came with."""
    mode = arguments.pop("mode", None)
    if mode is not None:
        wanted = {"replace": "old_string", "patch": "patch"}.get(str(mode).strip().lower())
        if wanted is None or wanted not in arguments:
            raise ValueError(
                f'mode "{mode}" does not fit the other fields. Send patch="..." alone, or '
                "path, old_string and new_string."
            )
    command = arguments.pop("command", None)
    if command is None:
        return
    name = str(command).strip().lower()
    path = arguments.get("path", "<path>")
    if name == "view":
        raise ValueError(f'To view a file, call {model_tool_name("read")}(path="{path}").')
    if name == "undo_edit":
        raise ValueError(
            "An earlier edit cannot be undone by name. Send the reverse change as a patch."
        )
    required = {"str_replace": "old_string", "create": "content", "insert": "insert_line"}.get(name)
    if required is None or required not in arguments:
        raise ValueError(
            f'command "{command}" does not fit the other fields. Send patch="..." alone, '
            "or path with old_string and new_string."
        )


def _edit_item(item: Any, number: int) -> Any:
    if not isinstance(item, dict):
        return item
    edit: dict[str, Any] = {}
    for key, value in item.items():
        field = key if key in _EDIT_FIELDS else _FIELD_ALIASES.get(key, key)
        if field in {"replace_all", "expected_replacements"} and value in (None, ""):
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
    kinds = {
        label
        for field, label in _CHANGE_FIELDS.items()
        if field in arguments and (field != "patch" or (isinstance(patch, str) and patch.strip()))
    }
    if len(kinds) > 1:
        first, second = sorted(kinds)
        raise ValueError(
            f"The call gives both {first} and {second}. Send one of them; for several changes, "
            "put them all in one patch."
        )
    replacing = "old_string" in arguments or "new_string" in arguments
    insert = "insert_line" in arguments
    if insert and ("old_string" in arguments or "new_string" not in arguments):
        raise ValueError(
            "insert_line inserts new_string after that line; send insert_line and new_string "
            "without old_string."
        )
    if replacing and not insert:
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
    counting = [field for field in ("replace_all", "expected_replacements") if field in arguments]
    if counting and ("old_string" not in arguments or insert):
        raise ValueError(f"{counting[0]} applies only to old_string and new_string.")
    expected = arguments.get("expected_replacements")
    if arguments.get("replace_all") is False and isinstance(expected, int) and expected > 1:
        raise ValueError(
            f"replace_all is false but expected_replacements is {expected}. Send "
            "expected_replacements alone to replace that many occurrences."
        )
    edits = arguments.get("edits")
    needs_path = (replacing or insert or "content" in arguments) or (
        isinstance(edits, list) and any(isinstance(e, dict) and "path" not in e for e in edits)
    )
    if needs_path and "path" not in arguments:
        raise ValueError(
            "The call names no file. Add path, relative to the working directory or absolute."
        )
    if kinds - {"patch"}:
        # The change lives in other fields; the required patch field stays empty.
        arguments.setdefault("patch", "")
    elif "path" in arguments and not kinds and patch in (None, ""):
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
    if "old_string" in arguments and "insert_line" not in arguments:
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
    if not isinstance(path, str) or not ("content" in arguments or "insert_line" in arguments):
        raise _PatchError(
            "invalid_arguments",
            message='The patch is empty. Send patch="*** Begin Patch\\n...\\n*** End Patch".',
        )
    if "content" in arguments:
        return [_content_operation(path, arguments["content"])]
    lines = _text_lines(arguments["new_string"])
    hunk = _Hunk(lines=[("+", text) for text in lines], insert_line=arguments["insert_line"])
    return [_Operation("update", path, hunks=[hunk])]


def _text_lines(text: str) -> list[str]:
    """Split inserted text into lines; a final line break ends the last line."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()
    return lines


def _content_operation(path: str, content: str, *, only_if_empty: bool = False) -> _Operation:
    """Create or replace a file with exactly ``content``."""
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    newline = "\r\n" if "\r\n" in content else "\r" if "\r" in content else "\n"
    hunks = []
    if text:
        lines = text.split("\n")
        no_newline = lines[-1] != ""
        if not no_newline:
            lines.pop()
        hunks = [_Hunk(lines=[("+", line) for line in lines], no_newline=no_newline)]
    return _Operation("add", path, hunks=hunks, only_if_empty=only_if_empty, newline=newline)


def _edit_operations(path: str, edits: list[JsonObject]) -> list[_Operation]:
    operations: list[_Operation] = []
    for number, edit in enumerate(edits, 1):
        target = edit.get("path", path)
        if edit["old_string"] == "":
            operations.append(_content_operation(target, edit["new_string"], only_if_empty=True))
            continue
        hunk = _Hunk(
            replacement=_Replacement(
                edit["old_string"],
                edit["new_string"],
                edit.get("replace_all", False),
                edit.get("expected_replacements"),
            ),
            label=f"edit {number}" if len(edits) > 1 else "",
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
    "normalize_patch_arguments",
    "patch_operations",
]
