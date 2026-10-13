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
(``_edit_engine.py``). A canonical ``edit`` call applies its edits in order, each to the
text the edits before it left, and writes the file once: an edit that fails is
skipped and reported by its number, and the others are written. A problem of
the whole call, such as a missing file, writes nothing. The explicit per-item
path dialect runs each item in order under all of the call's file locks.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Collection, Mapping, Sequence
from functools import cache, partial
from pathlib import Path
from typing import Any, Literal

from core.tools._file_changes import cancelled_paths, change_batch, file_reports, run_operations
from core.tools._patch_entries import _resolve
from core.tools._patch_report import EQUAL_TEXT, failure_text, patch_result
from core.tools._patch_requests import APPLY_PATCH_TOOL_NAME, content_operation
from core.tools._patch_syntax import _Hunk, _Operation, _PatchError, _Replacement, _same_path
from core.tools._swarm_references import wiki_change_call
from core.tools.availability import TOOL_ACTIVATION_FOLLOWS
from core.tools.call_syntax import SpellingAliases, normalize_call_arguments, spelling
from core.tools.contracts import (
    ToolContract,
    ToolContractError,
    compile_tool_contract,
    decode_json_argument,
)
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
from core.utils.json_text import JsonPath, explain_json_break

EDIT_TOOL_NAME = "edit"
WRITE_TOOL_NAME = "write"
# The file edit Tools of both dialects; ``apply_patch`` is the configurable one.
EDIT_TOOL_NAMES = (APPLY_PATCH_TOOL_NAME, EDIT_TOOL_NAME, WRITE_TOOL_NAME)
_REPLACE_TOOLS = frozenset({EDIT_TOOL_NAME, WRITE_TOOL_NAME})

EditDialect = Literal["patch", "replace"]

EDIT_TOOL_DESCRIPTION = (
    "Replace text in a file. Calls that change different files never need each other "
    "finished first; send them in the same response."
)
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

WRITE_TOOL_DESCRIPTION = (
    "Create a file or replace all of its content. To replace an existing file, read it first."
)
WRITE_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
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
# Marks an edits item the call cannot apply, with the reason; never a field the Model sends.
_REFUSED = "\x00refused"
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
# Said by an edit not found after an earlier edit of the call failed (see ``apply_hunks``).
_PRECISE_AFTER_FAILURE = (
    "After an earlier edit of this call failed, old_string must match the file exactly "
    "(up to whitespace)."
)
_WRITE_TEMPLATES = {
    "file_changed": (
        "{path} changed on disk while this write ran. Read it before writing it again."
    ),
    "partial_change": f"call {model_tool_name(EDIT_TOOL_NAME)} to change only part of it",
}
_NO_PATH = "The call names no file. Add path, relative to the working directory or absolute."
# A call that names a Swarm Wiki page as page_id and no path; the handler refuses it.
_PAGE_ID = "page_id"


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
    """Return the Tool the Agent sees for changing part of a file, if any.

    Without a Model request (every callable Tool counts as shown) that is
    ``apply_patch``, the one the user configures.
    """
    for name in (APPLY_PATCH_TOOL_NAME, EDIT_TOOL_NAME):
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
        field_aliases=_EDIT_ALIASES,
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


def _edit_field(key: str) -> str:
    """Return the field an ``edits`` item key names, else the key itself."""
    field = _EDIT_ALIASES.get(key, key)
    if field not in (*_EDIT_FIELDS, "path"):
        field = next((name for name in _EDIT_FIELDS if spelling(name) == spelling(key)), key)
    return field


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
    edit: dict[str, Any] = {}
    for key, value in item.items():
        field = _edit_field(key)
        if field not in (*_EDIT_FIELDS, "path"):
            if not _edit_item_contract().normalize_arguments({key: value}):
                # An empty value under a name that is not a field requests nothing.
                continue
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


# --- Execution --------------------------------------------------------------------------


def _edit_operations(
    context: ToolContext, path: str, edits: Sequence[JsonObject], *, combine: bool = True
) -> list[_Operation]:
    """Return creations and replacements, combining one-file calls into a single write.

    A refused edit has none; the replacements after it match precisely only,
    as after any failed edit (see ``apply_hunks``).
    """
    total = len(edits)
    operations: list[_Operation] = []
    refused: set[Path | str] = set()
    identities: dict[str, Path | str] = {}
    for number, edit in enumerate(edits, 1):
        target = edit.get("path", path)
        if target not in identities:
            try:
                identities[target] = _resolve(context, target)
            except _PatchError:
                # The pipeline reports an invalid path; it cannot alias a valid one.
                identities[target] = target
        identity = identities[target]
        label = f"edit {number} of {total}" if total > 1 else ""
        if _REFUSED in edit:
            refused.add(identity)
            continue
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
            precise_only=identity in refused,
        )
        if combine and operations and operations[-1].action == "update":
            operations[-1].hunks.append(hunk)
        else:
            operations.append(_Operation("update", target, hunks=[hunk]))
    return operations


def _edit_number(error: JsonObject, total: int) -> int | None:
    """Return the number of the edit a failure is about, ``None`` for the whole call."""
    label = error.get("label", "")
    return next((n for n in range(1, total + 1) if label == f"edit {n} of {total}"), None)


def _edit_numbers(numbers: Sequence[int]) -> str:
    """Name edits by number: ``edit 3``, ``edits 1 and 3``, ``edits 1-4 and 6``."""
    runs: list[list[int]] = []
    for number in sorted(numbers):
        if runs and number == runs[-1][-1] + 1:
            runs[-1].append(number)
        else:
            runs.append([number])
    parts: list[str] = []
    for run in runs:
        parts.extend([f"{run[0]}-{run[-1]}"] if len(run) > 2 else map(str, run))
    if len(numbers) == 1:
        return f"edit {parts[0]}"
    return "edits " + (", ".join(parts[:-1]) + f" and {parts[-1]}" if len(parts) > 1 else parts[0])


def _failed_edit(
    error: JsonObject, idle: Collection[int], total: int, *, written: bool
) -> JsonObject:
    """Prepare a failed edit's report: it saw the text the earlier applied edits left.

    ``idle`` holds the edits that left the text as it was: the failed ones and
    those whose new text equals the text they matched. ``written`` says the
    call's other edits were written, so an earlier edit that replaced this
    one's old text stays applied.
    """
    number = _edit_number(error, total)
    earlier = [n for n in range(1, number or 1) if n not in idle]
    if not earlier:
        return error

    def short(label: object) -> str:
        return str(label).split(" of ")[0]

    prepared = {
        **error,
        "after": _edit_numbers(earlier),
        "written": [{**item, "label": short(item["label"])} for item in error.get("written") or []],
    }
    if error.get("replaced_by"):
        prepared.update(replaced_by=short(error["replaced_by"]), replaced_kept=written)
    return prepared


def _edit_failure(
    context: ToolContext,
    errors: Sequence[JsonObject],
    total: int,
    in_place: Mapping[int, str] | None = None,
) -> JsonObject:
    """Say that no edit was applied, why each failed edit failed, and which to correct.

    ``in_place`` says, by edit number, which other edits found their new text
    already in the file: they need no other call.
    """
    in_place = in_place or {}
    numbers = [n for n in (_edit_number(error, total) for error in errors) if n is not None]
    failures = []
    for error in errors:
        context.add_display_notice("error", str(error["message"]))
        failures.append(
            failure_text(_failed_edit(error, {*numbers, *in_place}, total, written=False))
        )
    if total == 1:
        lines = ["No file was changed.", *failures]
    else:
        none, every = (
            ("Neither edit was", "both edits")
            if total == 2
            else (f"None of the {total} edits were", f"all {total} edits")
        )
        lines = [f"{none} applied; no file was changed.", *failures, *in_place.values()]
        if len(numbers) == total:
            lines.append(f"Send {every} again, each corrected or left out.")
        elif numbers and len(numbers) + len(in_place) == total:
            missed = _edit_numbers(numbers)
            each = "each " if len(numbers) > 1 else ""
            lines.append(f"Send only {missed} again, {each}corrected or left out.")
        elif numbers:
            lines.append(f"Send {every} again with {_edit_numbers(numbers)} corrected or left out.")
    first = errors[0]
    return tool_failure(
        str(first["code"]),
        "\n".join(lines),
        retryable=first.get("retryable"),
        attempts_made=first.get("attempts_made"),
    )


def _partial_lead(failed: Sequence[int], total: int, unchanged: Collection[int] = ()) -> str:
    """Name the applied edits and the failed ones, which alone need another call.

    Edits in ``unchanged`` matched but changed nothing; their own notes say so.
    """
    applied = [n for n in range(1, total + 1) if n not in failed and n not in unchanged]
    done, missed = _edit_numbers(applied), _edit_numbers(failed)
    return (
        f"{done[:1].upper()}{done[1:]} {'was' if len(applied) == 1 else 'were'} applied; "
        f"{missed} {'was' if len(failed) == 1 else 'were'} not. Send only {missed} again, "
        f"{'each ' if len(failed) > 1 else ''}corrected or left out."
    )


def _execute_edit(context: ToolContext, arguments: JsonObject, state: FileReadState) -> JsonObject:
    edits = arguments["edits"]
    if any("path" in edit for edit in edits):
        return _execute_multi_edit(context, arguments, state)
    total = len(edits)
    # Edits that leave the file as it was, such as a change and its reversal.
    result = "these edits would leave it" if total > 1 else "this edit would leave it"
    already = {"already_applied": f"{{where}} already reads as {result}."}
    batch = change_batch(context, {**_EDIT_TEMPLATES, **already})
    operations = _edit_operations(context, arguments["path"], edits)
    run_operations(context, state, batch, operations, one_write=True)
    [outcome] = batch.results
    refused: list[JsonObject] = [
        {"code": "invalid_arguments", "message": edit[_REFUSED], "label": f"edit {n} of {total}"}
        for n, edit in enumerate(edits, 1)
        if _REFUSED in edit
    ]
    skipped = outcome.get("failures") or []
    # Edits that changed nothing, each with why when its new text already stood there.
    unchanged: dict[int, str] = {}
    for label, reason in (outcome.get("unchanged") or {}).items():
        # The edit of a one-edit call has no label.
        number = 1 if total == 1 else _edit_number({"label": label}, total)
        if number is not None:
            unchanged[number] = reason
    applied = total - len(refused) - len(skipped) - len(unchanged)
    files = file_reports(context, batch, edits=applied)
    if outcome["status"] == "failed" and not skipped:
        # A problem of the whole call, such as a missing file.
        return _edit_failure(context, [outcome["error"], *refused], total)
    errors = sorted([*refused, *skipped], key=lambda error: _edit_number(error, total) or 0)

    def where(n: int) -> str:
        return str(outcome["where"]) if total == 1 else f"{outcome['where']}, edit {n} of {total}"

    in_place = {n: f"{where(n)}: {reason}" for n, reason in sorted(unchanged.items()) if reason}
    if outcome["status"] == "already_applied" and in_place and len(unchanged) == total:
        # Nothing to write: say why for each edit, naming where new text already stood.
        outcome["note"] = " ".join(
            in_place.get(n, f"{where(n)}: {EQUAL_TEXT}") for n in sorted(unchanged)
        )
    no_ops: list[JsonObject] = [
        {
            "status": "unchanged",
            "action": "update",
            "path": outcome["path"],
            "where": where(n),
            **({"note": in_place[n]} if n in in_place else {}),
        }
        for n in sorted(unchanged)
        if outcome["status"] == "applied"
    ]
    for error in errors[1:]:
        # Matched precisely only, so that no other text stands in for a failed edit's.
        if error["code"] == "text_not_found":
            error["message"] = f"{error['message']} {_PRECISE_AFTER_FAILURE}"
    if not errors:
        return patch_result(context, files, [*batch.results, *no_ops], cancelled_paths(batch))
    if outcome["status"] != "applied":
        # The edits that did not fail left the file as it was.
        return _edit_failure(context, errors, total, in_place)
    failed = [n for n in (_edit_number(error, total) for error in errors) if n is not None]
    entries = [
        outcome,
        *no_ops,
        *(
            {
                "status": "failed",
                "error": _failed_edit(error, {*failed, *unchanged}, total, written=True),
            }
            for error in errors
        ),
    ]
    lead = _partial_lead(failed, total, unchanged)
    return patch_result(context, files, entries, cancelled_paths(batch), lead=lead)


def _execute_multi_edit(
    context: ToolContext, arguments: JsonObject, state: FileReadState
) -> JsonObject:
    """Apply explicit per-item targets in input order under the shared path locks."""
    edits = arguments["edits"]
    total = len(edits)
    batch = change_batch(
        context,
        {
            **_EDIT_TEMPLATES,
            "already_applied": "{where} already reads as this edit would leave it.",
        },
    )
    operations = _edit_operations(context, arguments["path"], edits, combine=False)
    run_operations(context, state, batch, operations)
    outcomes = iter(zip(batch.results, operations, strict=True))
    entries: list[JsonObject] = []
    statuses: list[str] = []
    failed: list[int] = []
    unchanged: list[int] = []
    for number, edit in enumerate(edits, 1):
        label = f"edit {number} of {total}"
        entry: JsonObject
        operation = None
        if _REFUSED in edit:
            entry = {
                "status": "failed",
                "error": {"code": "invalid_arguments", "message": edit[_REFUSED]},
            }
        else:
            entry, operation = next(outcomes)
            entry["where"] = f"{entry['path']}, {label}"
        error = entry.get("error")
        if error is not None and not error.get("label"):
            error["message"] = f"{label[:1].upper()}{label[1:]}: {error['message']}"
            error["label"] = label
        if (
            error is not None
            and error["code"] == "text_not_found"
            and operation is not None
            and any(hunk.precise_only for hunk in operation.hunks)
        ):
            error["message"] += " " + _PRECISE_AFTER_FAILURE
        status = entry["status"]
        if status in {"failed", "skipped", "partial"}:
            failed.append(number)
            effect = "written but not confirmed" if status == "partial" else "not applied"
        elif status in {"unchanged", "already_applied"}:
            unchanged.append(number)
            effect = "nothing to change"
        else:
            effect = "applied"
        target = f" ({entry['path']})" if "path" in entry else ""
        statuses.append(f"Edit {number} of {total}{target}: {effect}.")
        entries.append(entry)
    applied = total - len(failed) - len(unchanged)
    files = file_reports(context, batch, edits=applied)
    lead = (
        _partial_lead(failed, total, unchanged)
        if failed and applied and not any(entry["status"] == "partial" for entry in entries)
        else None
    )
    if (
        failed
        and not applied
        and not files
        and all(entry["status"] != "partial" for entry in entries)
    ):
        in_place = {number: f"{entries[number - 1]['where']}: {EQUAL_TEXT}" for number in unchanged}
        result = _edit_failure(
            context, [entry["error"] for entry in entries if "error" in entry], total, in_place
        )
    else:
        result = patch_result(context, files, entries, cancelled_paths(batch), lead=lead)
    if result["ok"]:
        content = result["data"]["content"]
        # Keep the partial-result lead first, as in the canonical one-file dialect.
        if failed:
            first, _, rest = content.partition("\n")
            result["data"]["content"] = "\n".join([first, *statuses, rest]).rstrip()
        else:
            result["data"]["content"] = "\n".join([*statuses, content])
    else:
        first, _, rest = result["error"]["message"].partition("\n")
        result["error"]["message"] = "\n".join([first, *statuses, rest]).rstrip()
    return result


def _execute_write(context: ToolContext, arguments: JsonObject, state: FileReadState) -> JsonObject:
    batch = change_batch(context, _WRITE_TEMPLATES)
    operation = content_operation(arguments["path"], arguments["content"])
    run_operations(context, state, batch, [operation])
    return patch_result(
        context, file_reports(context, batch), batch.results, cancelled_paths(batch)
    )


def make_edit_handler(file_state: FileReadState) -> ToolHandler:
    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        if _PAGE_ID in arguments:
            return _page_id_refusal(context, EDIT_TOOL_NAME, arguments[_PAGE_ID])
        return _execute_edit(context, arguments, file_state)

    return handler


def make_write_handler(file_state: FileReadState) -> ToolHandler:
    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        if _PAGE_ID in arguments:
            return _page_id_refusal(context, WRITE_TOOL_NAME, arguments[_PAGE_ID])
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
    try:
        normalized = normalize_edit_arguments(arguments)
    except ValueError:
        normalized = None
    if isinstance(normalized, dict) and any("path" in edit for edit in normalized.get("edits", [])):
        paths = dict.fromkeys(
            edit["path"] for edit in normalized["edits"] if isinstance(edit.get("path"), str)
        )
        return tuple(
            ToolDisplayPart(
                value=path, kind="path", truncate="start", tooltip="always", copyable=True
            )
            for path in paths
        )
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
_RESULT_SCHEMA: JsonObject = {"type": "object", "required": ["content"]}


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
            unadvertised_parameters={_PAGE_ID: {}},
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
