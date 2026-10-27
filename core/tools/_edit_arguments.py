"""Replacement-dialect call interpretation and argument contracts."""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from functools import cache, partial
from typing import Any

from core.tools._patch_syntax import _same_path
from core.tools._replacement_requests import (
    REPLACEMENT_ALIASES,
    replacement_fields,
)
from core.tools._replacement_requests import (
    replacement_field as _edit_field,
)
from core.tools._swarm_references import wiki_change_call
from core.tools.call_syntax import SpellingAliases, normalize_call_arguments, spelling
from core.tools.contracts import (
    ToolContract,
    ToolContractError,
    compile_tool_contract,
    decode_json_argument,
)
from core.tools.model_names import model_tool_name
from core.tools.tools import (
    JsonObject,
    ToolContext,
    tool_failure,
)
from core.utils.json_text import JsonPath, explain_json_break

_PATH_ALIASES = ("file_path", "filename")

# Marks an edits item the call cannot apply, with the reason; never a field the Model sends.
_REFUSED = "\x00refused"

_TEXT: JsonObject = {"type": "string"}

_NO_PATH = "The call names no file. Add path, relative to the working directory or absolute."

_PAGE_ID = "page_id"


EDIT_TOOL_NAME = "edit"
WRITE_TOOL_NAME = "write"
EDIT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "edits": {
            "type": "array",
            "description": "All changes to this file.",
            "items": {
                "type": "object",
                "properties": {
                    "old_string": {
                        "type": "string",
                        "description": (
                            "Text to replace. Without replace_all, it must occur exactly once."
                        ),
                    },
                    "new_string": {"type": "string"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every occurrence. Omit to replace one.",
                    },
                },
                "required": ["old_string", "new_string"],
            },
        },
    },
    "required": ["path", "edits"],
}
WRITE_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string", "description": "Complete file content."},
    },
    "required": ["path", "content"],
}
_WRITE_ALIASES = SpellingAliases(
    {"path": _PATH_ALIASES, "content": ("text", "contents", "file_text")}
)
_EDIT_FIELDS = ("old_string", "new_string", "replace_all")


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

    A flat ``old_string``/``new_string`` call is one edit. Without a root path,
    explicit item paths can name several files. Contradictory targets refuse
    the call before effects. An item that leaves its edit open stays in place
    as a refused edit (``_REFUSED``), and the handler reports it by its number.
    """
    # Old or new text sent beside edits text that breaks: the call came apart.
    beside = [
        field
        for field in ("old_string", "new_string")
        if isinstance(arguments, dict) and any(_edit_field(key) == field for key in arguments)
    ]
    normalized = normalize_call_arguments(
        _edit_repair_contract(),
        arguments,
        field_aliases=REPLACEMENT_ALIASES,
        field_normalizers={"edits": partial(_edits_value, beside=beside)},
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
    refusals: list[str] = []
    if isinstance(edits, list):
        # An item with old text shows the call means a file that has text.
        held = any(_holds_old_text(item) for item in edits)
        items: list[Any] = []
        for number, item in enumerate(edits, 1):
            target = _edit_item_path(item, number)
            try:
                edit = _edit_item(item, number, held=held)
            except ValueError as error:
                refusals.append(str(error))
                # Kept in place, so the other edits keep their numbers.
                edit = {"old_string": "", "new_string": "", _REFUSED: str(error)}
                if target is not None:
                    edit["path"] = target
            items.append(edit)
        _take_item_paths(normalized, items)
        if refusals and len(refusals) == len(items):
            raise ValueError("\n".join(refusals))
        normalized["edits"] = items
    if "path" not in normalized:
        page = _wiki_page(normalized)
        if page is not None:
            # The path and edits only satisfy the contract: the handler refuses first.
            return {_PAGE_ID: page, "path": ".", "edits": []}
        raise ValueError("\n".join([_NO_PATH, *refusals]))
    if not edits and set(normalized) <= {"path", "edits"}:
        raise ValueError(
            f"The call names {normalized['path']} but no change. Send edits, each with "
            "old_string, the current text, and new_string, its replacement."
        )
    return normalized


@cache
def _edits_text_schemas() -> tuple[JsonObject, JsonObject]:
    """Return the schemas of ``edits`` sent as JSON text: the array, and one edit alone."""
    item = copy.deepcopy(EDIT_TOOL_PARAMETERS["properties"]["edits"]["items"])
    item["properties"]["path"] = _TEXT
    return {"type": "array", "items": item}, item


def _edits_value(value: Any, *, beside: Sequence[str] = ()) -> Any:
    """Return ``edits`` sent as JSON text as the array or object it encodes.

    Text that is JSON except for unescaped quotes or raw line breaks inside
    old_string or new_string is read with those escaped
    (``decode_json_argument``). ``beside`` names the call's other fields that
    belong in an edit, for the refusal of text that is not JSON.
    """
    if not isinstance(value, str):
        return value
    array_schema, item_schema = _edits_text_schemas()
    encoded = value.lstrip()[:1]
    try:
        decoded = decode_json_argument(value, item_schema if encoded == "{" else array_schema)
    except json.JSONDecodeError as error:
        if encoded not in ("[", "{"):
            decoded = None
        else:
            raise ValueError(_broken_edits_text(value, error, beside)) from None
    except ToolContractError:
        raise
    except ValueError, RecursionError:
        decoded = None
    if isinstance(decoded, list | dict):
        return decoded
    raise ValueError(
        "edits arrived as text, not as an array. Send edits as an array of objects, each with "
        "old_string and new_string."
    )


def _broken_edits_text(text: str, error: json.JSONDecodeError, beside: Sequence[str]) -> str:
    """Say where ``edits`` stops being JSON and how to send it.

    Text that ends early, or that has edit fields beside it, lost part of the
    call: no reading restores it, so the call is sent again.
    """
    found, explanation = explain_json_break(text, error, label=_edits_field)
    lines = [f"edits is not valid JSON. {explanation}"]
    if beside:
        lines.append(
            f"{' and '.join(beside)} arrived beside edits instead of inside an edits item. "
            "Send the complete call again, with each old_string and new_string inside its "
            "edits item."
        )
    elif found.kind == "incomplete":
        lines.append(
            "Send the complete call again; if it is cut off again, send one edit per call."
        )
    elif found.concerns_text:
        write = model_tool_name(WRITE_TOOL_NAME)
        lines.append(
            "If this text fails again, send one edit per call, or call "
            f"{write} with the whole file."
        )
    return "\n".join(lines)


def _edits_field(path: JsonPath) -> str | None:
    item = next((part for part in path if isinstance(part, int)), None)
    field = next((part for part in reversed(path) if isinstance(part, str)), None)
    where = f"edits item {item + 1}" if item is not None else None
    if field is not None and where is not None:
        return f"{field} in {where}"
    return field or where


def _json_value(text: str) -> Any:
    """Return the value JSON ``text`` encodes, ``None`` when it is not JSON."""
    try:
        return json.loads(text)
    except ValueError:
        return None


# How to send text without old_string, when the file has text and when that is unknown.
_INSERT = (
    "To insert text, put the line above it in old_string, and that line followed by the new "
    "text in new_string."
)
_INSERT_OR_CREATE = _INSERT[:-1] + '; old_string "" creates a file.'


def _check_flat_edit(edit: dict[str, Any]) -> None:
    if "old_string" not in edit:
        raise ValueError(
            f"new_string needs old_string, the current text it replaces. {_INSERT_OR_CREATE}"
        )
    if "new_string" not in edit:
        raise ValueError(
            'old_string needs new_string, the text that replaces it (new_string "" deletes '
            "old_string)."
        )


def _holds_old_text(item: Any) -> bool:
    """Say whether an ``edits`` item, in any spelling, has a nonempty old_string."""
    if isinstance(item, str):
        item = _json_value(item)
    return isinstance(item, dict) and any(
        _edit_field(key) == "old_string" and isinstance(value, str) and value != ""
        for key, value in item.items()
    )


def _edit_item(item: Any, number: int, *, held: bool) -> Any:
    """Return one ``edits`` item under canonical field names.

    ``held`` says another item of the call has old text, so the file has text.
    """
    if isinstance(item, str):
        item = _json_value(item)
    if not isinstance(item, dict):
        raise ValueError(
            f"edits item {number} is not an object. Send each edit as an object with old_string "
            "and new_string."
        )
    edit = replacement_fields(item, number, empty_as_omitted=("replace_all", "path"))
    for field, value in list(edit.items()):
        if field not in (*_EDIT_FIELDS, "path"):
            if not _edit_item_contract().normalize_arguments({field: value}):
                # An empty value under a name that is not a field requests nothing.
                del edit[field]
                continue
            raise ValueError(
                f'edits item {number} has "{field}", which is not a field of an edit. Each edit '
                "takes old_string, new_string and replace_all."
            )
    path = edit.pop("path", None)
    edit = _edit_item_contract().normalize_arguments(edit)
    if "old_string" not in edit:
        raise ValueError(
            f"edits item {number} has no old_string, the current text to replace. "
            + (_INSERT if held else _INSERT_OR_CREATE)
        )
    if "new_string" not in edit:
        raise ValueError(
            f'edits item {number} has old_string but no new_string. Send new_string, "" to '
            "delete the text."
        )
    if path is not None:
        edit["path"] = path
    return edit


def _edit_item_path(item: Any, number: int) -> str | None:
    """Keep an explicit target even when the item's content cannot apply."""
    if isinstance(item, str):
        item = _json_value(item)
    if not isinstance(item, dict):
        return None
    paths = [
        value
        for key, value in item.items()
        if _edit_field(key) == "path" and value not in (None, "")
    ]
    if not paths:
        return None
    if any(not isinstance(path, str) for path in paths):
        raise ValueError(
            f"edits item {number} needs path as text, relative to the working directory "
            "or absolute."
        )
    if any(path != paths[0] for path in paths[1:]):
        raise ValueError(f"edits item {number} gives path twice with different values; send one.")
    return str(paths[0])


def _take_item_paths(arguments: dict[str, Any], edits: Sequence[JsonObject]) -> None:
    """Read every target before inferring a missing root path.

    One unambiguous file supplies a missing root. Several files keep explicit
    item paths; the inferred root only satisfies the advertised contract.
    """
    root = arguments.get("path")
    paths: list[str] = []
    for number, edit in enumerate(edits, 1):
        named = edit.get("path")
        if named is None:
            continue
        if isinstance(root, str) and not _same_path(named, root):
            raise ValueError(
                f"edits item {number} changes {named}, but this call changes {root}. "
                "Remove the root path to use each edit's own path, or make the paths agree."
            )
        if not any(_same_path(named, path) for path in paths):
            paths.append(named)
    if root is None and paths:
        if len(paths) > 1:
            for number, edit in enumerate(edits, 1):
                if "path" not in edit and _REFUSED not in edit:
                    raise ValueError(
                        f"edits item {number} has no path, but the call names several files. "
                        "Give each edit its own path."
                    )
        arguments["path"] = paths[0]
    if root is not None or len(paths) <= 1:
        for edit in edits:
            edit.pop("path", None)


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
        page = _wiki_page(normalized)
        if page is not None:
            # The path and content only satisfy the contract: the handler refuses first.
            return {_PAGE_ID: page, "path": ".", "content": ""}
        raise ValueError(_NO_PATH)
    if "content" not in normalized:
        raise ValueError(
            f"The call names {normalized['path']} but no content. Send content, the complete "
            'text of the file ("" empties it).'
        )
    return normalized


def _wiki_page(arguments: dict[str, Any]) -> str | None:
    """Return the page a call without path names as ``page_id``, if it names one."""
    pages = [
        value
        for key, value in arguments.items()
        if spelling(key) == spelling(_PAGE_ID) and isinstance(value, str) and value.strip()
    ]
    return pages[0] if len(pages) == 1 else None


def _page_id_refusal(context: ToolContext, tool: str, page: Any) -> JsonObject:
    """Refuse a change of ``page_id``, naming the swarm_wiki call when it is offered."""
    action = "changes the file" if tool == EDIT_TOOL_NAME else "writes the file"
    message = (
        f"No file was changed.\n{model_tool_name(tool)} has no page_id: it {action} given as path."
    )
    call = wiki_change_call(page, context.offers)
    return tool_failure("invalid_arguments", f"{message} {call}" if call else message)


def _refusing(normalizer: Any) -> Any:
    """Turn a normalizer's refusal into a contract error that says no file changed."""

    def normalize(arguments: object) -> object:
        try:
            return normalizer(arguments)
        except ValueError as error:
            raise ToolContractError(f"No file was changed.\n{error}") from error

    return normalize
