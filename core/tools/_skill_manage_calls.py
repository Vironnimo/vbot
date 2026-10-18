"""Skill authoring call interpretation and complete-document preparation."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import yaml

from core.skills.skill_validator import (
    MAX_SKILL_NAME_LENGTH,
    SKILL_NAME_CHARSET_FRAGMENT,
    parse_skill_front_matter,
    split_skill_document,
)
from core.skills.skills import (
    RESOURCE_DIRECTORIES,
    SKILL_FILENAME,
)
from core.tools.availability import SKILL_MANAGE_TOOL_NAME
from core.tools.call_syntax import normalize_call_arguments, spelling
from core.tools.contracts import ToolContractError, compile_tool_contract
from core.tools.skill import (
    clean_skill_file_path,
)
from core.tools.tools import (
    JsonObject,
)

SKILL_MANAGE_UNKNOWN_SCOPE = (
    "skill_manage changes your own Skills and global Skills; Project and bundled Skills are "
    "read-only here, and nothing changed. Omit scope to change a Skill by its name."
)
# ``absorbed_into`` names the Skill that now holds a deleted Skill's instructions.
SKILL_MANAGE_ABSORBED_INTO_ACTION = (
    "absorbed_into is used only by delete; nothing changed. Omit absorbed_into for {action}."
)

_ACTIONS = ("create", "edit", "patch", "write_file", "remove_file", "delete", "publish")

SKILL_MANAGE_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_ACTIONS),
            "description": "Operation to perform.",
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_SKILL_NAME_LENGTH,
            "pattern": f"^{SKILL_NAME_CHARSET_FRAGMENT}$",
            "description": (
                "The Skill's name. A new name starts with a letter or digit and uses only "
                "letters, digits, '-' and '_'."
            ),
        },
        "content": {
            "type": "string",
            "description": (
                "The complete new text: SKILL.md for create and edit, the file for write_file."
            ),
        },
        "old_string": {
            "type": "string",
            "description": (
                "For patch: the exact current text to replace, unique in the file unless "
                "replace_all is true."
            ),
        },
        "new_string": {
            "type": "string",
            "description": "For patch: the replacement text; empty deletes old_string.",
        },
        "replace_all": {
            "type": "boolean",
            "description": "For patch: replace every occurrence of old_string.",
        },
        "file_path": {
            "type": "string",
            "minLength": 1,
            "description": (
                "A support file under scripts/, references/ or assets/. Required for "
                "write_file and remove_file; omit to patch SKILL.md."
            ),
        },
    },
    "required": ["action", "name"],
}
# Accepted without being offered: fields other harnesses send for the same operations.
_UNADVERTISED_PARAMETERS: JsonObject = {
    "description": {"type": "string"},
    "scope": {"type": "string"},
    "category": {"type": "string"},
    # Named in the Librarian and Reflection briefs: the Skill that absorbed a deleted one.
    "absorbed_into": {"type": "string"},
}
_FIELD_ALIASES = {
    "match": "old_string",
    "old_str": "old_string",
    "old_text": "old_string",
    "search": "old_string",
    "find": "old_string",
    "new_str": "new_string",
    "new_text": "new_string",
    "replacement": "new_string",
    "file_pth": "file_path",
    "path": "file_path",
    "file": "file_path",
    "skill": "name",
    "skill_name": "name",
}
_ACTION_SYNONYMS = {"strreplace": "patch", "deletefile": "remove_file"}
# The fields that carry an action's text; other harnesses send all of them, empty
# ones as placeholders.
_TEXT_FIELDS = ("content", "new_string", "file_content")
_OWN_SCOPES = frozenset({"own", "private", "agent", "mine", "personal", "self", "local"})
_GLOBAL_SCOPES = frozenset({"global", "user"})
_NAME_MARKS = "/$@"
_LISTED_NAME_LIMIT = 20

_SKILL_MANAGE_RUNTIME_CONTRACT = compile_tool_contract(
    name=SKILL_MANAGE_TOOL_NAME,
    input_schema={
        **SKILL_MANAGE_TOOL_PARAMETERS,
        "properties": {
            **SKILL_MANAGE_TOOL_PARAMETERS["properties"],
            **_UNADVERTISED_PARAMETERS,
            "file_content": {"type": "string"},
        },
    },
    require_closed_input=False,
)


class _RefusalError(Exception):
    """A call that is refused before any write; rendered as a Tool failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _Call:
    action: str
    name: str
    content: str | None = None
    old_string: str | None = None
    new_string: str | None = None
    replace_all: bool = False
    file_path: str | None = None
    description: str | None = None
    absorbed_into: str | None = None
    # ``global`` when the call names the global home explicitly, else ``None``.
    scope: str | None = None
    notes: list[str] = field(default_factory=list)


def _read_call(arguments: JsonObject) -> _Call:
    """Validate the action's fields and resolve what the call asks for."""
    action = arguments.get("action")
    name = arguments.get("name")
    if action not in _ACTIONS or not isinstance(name, str) or not name:
        raise _RefusalError("invalid_arguments", "action and name are required.")
    call = _Call(
        action=str(action),
        name=name,
        content=_text(arguments, "content"),
        old_string=_text(arguments, "old_string"),
        new_string=_text(arguments, "new_string"),
        replace_all=arguments.get("replace_all") is True,
        file_path=_text(arguments, "file_path"),
        description=_text(arguments, "description"),
        absorbed_into=_text(arguments, "absorbed_into"),
    )
    if call.absorbed_into is not None and call.action != "delete":
        raise _RefusalError(
            "invalid_arguments", SKILL_MANAGE_ABSORBED_INTO_ACTION.format(action=call.action)
        )
    call.scope = _check_scope(arguments.get("scope"))
    if arguments.get("category"):
        call.notes.append("category is not used; Skills have no categories.")
    if call.file_path is not None:
        call.file_path = _package_path(call.file_path, call.name)
    if call.description is not None and call.action not in ("create", "edit"):
        raise _RefusalError(
            "invalid_arguments",
            "description is used only by create and edit. To change an existing Skill's "
            "description, patch its description line in SKILL.md.",
        )
    return _ACTION_READERS[call.action](call)


def _read_create(call: _Call) -> _Call:
    if call.old_string is not None:
        raise _RefusalError(
            "invalid_arguments",
            "create writes a new Skill; old_string is only for patch. Omit old_string to "
            "create, or use action patch to change an existing Skill.",
        )
    _refuse_support_file(call, "write_file")
    if call.content is None:
        raise _RefusalError(
            "invalid_arguments", _document_needed(call.name, "create needs content")
        )
    call.content = _complete_document(call)
    return call


def _read_edit(call: _Call) -> _Call:
    _refuse_support_file(call, "write_file")
    if call.content is None:
        raise _RefusalError("invalid_arguments", _document_needed(call.name, "edit needs content"))
    if call.old_string is not None:
        if _has_front_matter(call.content):
            raise _RefusalError(
                "invalid_arguments",
                "edit replaces the complete SKILL.md and takes no old_string. Omit old_string "
                "to replace the whole file, or use action patch with old_string and "
                "new_string to change one passage.",
            )
        call.notes.append("edit with old_string changed only that text, as patch does.")
        call.action, call.new_string, call.content = "patch", call.content, None
        call.file_path = SKILL_FILENAME
        return _read_patch(call)
    call.content = _complete_document(call)
    call.file_path = SKILL_FILENAME
    return call


def _read_patch(call: _Call) -> _Call:
    if call.new_string is None and call.content is not None:
        call.new_string, call.content = call.content, None
    if call.old_string is None:
        if call.new_string is not None and _has_front_matter(call.new_string):
            raise _RefusalError(
                "invalid_arguments",
                "patch needs old_string, the exact current text to replace. To replace the "
                "complete SKILL.md, use action edit with the same text as content.",
            )
        raise _RefusalError(
            "invalid_arguments",
            "patch needs old_string, the exact current text to replace, and new_string. "
            f"{_read_hint(call.name, call.file_path or SKILL_FILENAME)}",
        )
    if call.old_string == "":
        raise _RefusalError(
            "invalid_arguments",
            "old_string is empty; give the exact current text to replace.",
        )
    if call.new_string is None:
        raise _RefusalError(
            "invalid_arguments",
            "patch needs new_string, the replacement text (empty to delete old_string).",
        )
    if call.old_string == call.new_string:
        raise _RefusalError(
            "invalid_arguments", "old_string and new_string are identical; nothing to change."
        )
    # An omitted file_path patches SKILL.md, or the one support file holding the text.
    return call


def _read_write_file(call: _Call) -> _Call:
    if call.file_path is None:
        raise _RefusalError(
            "invalid_arguments",
            "write_file needs file_path, a file under scripts/, references/ or assets/.",
        )
    if call.old_string is not None:
        raise _RefusalError(
            "invalid_arguments",
            "write_file replaces the whole file and takes no old_string. Use action patch "
            f'with file_path "{call.file_path}", old_string and new_string to change one '
            "passage, or omit old_string to write the complete file.",
        )
    if call.content is None:
        raise _RefusalError(
            "invalid_arguments", "write_file needs content, the complete file text."
        )
    if call.file_path == SKILL_FILENAME:
        call.action = "edit"
        call.content = _complete_document(call)
    return call


def _read_remove_file(call: _Call) -> _Call:
    if call.file_path is None:
        raise _RefusalError("invalid_arguments", "remove_file needs file_path, the file to remove.")
    if call.file_path == SKILL_FILENAME:
        raise _RefusalError(
            "invalid_arguments",
            "SKILL.md cannot be removed on its own; action delete removes the whole Skill.",
        )
    if call.content or call.new_string or call.old_string:
        raise _RefusalError(
            "invalid_arguments",
            f"remove_file removes {call.file_path} and takes no text. Use write_file to "
            "replace the file or patch to change part of it.",
        )
    return call


def _read_publish(call: _Call) -> _Call:
    if call.file_path is not None or call.content or call.new_string or call.old_string:
        raise _RefusalError(
            "invalid_arguments",
            "publish makes the whole Skill global and takes no file_path or text. Omit them, "
            "and change the Skill with edit or patch first if it needs a change.",
        )
    return call


def _read_delete(call: _Call) -> _Call:
    if call.file_path is not None:
        raise _RefusalError(
            "invalid_arguments",
            "delete removes the whole Skill. To remove one file, use action remove_file "
            f'with file_path "{call.file_path}"; omit file_path to delete the Skill.',
        )
    if call.content or call.new_string or call.old_string:
        raise _RefusalError(
            "invalid_arguments",
            "delete removes the whole Skill and takes no text. Use edit or patch to "
            "change it instead.",
        )
    return call


_ACTION_READERS: dict[str, Callable[[_Call], _Call]] = {
    "create": _read_create,
    "edit": _read_edit,
    "patch": _read_patch,
    "write_file": _read_write_file,
    "remove_file": _read_remove_file,
    "delete": _read_delete,
    "publish": _read_publish,
}


def _text(arguments: JsonObject, key: str) -> str | None:
    value = arguments.get(key)
    return value if isinstance(value, str) else None


def _check_scope(scope: object) -> str | None:
    """``global`` for a call that names the global home, ``None`` for the own one."""
    if scope is None:
        return None
    if isinstance(scope, str) and spelling(scope) in _OWN_SCOPES:
        return None
    if isinstance(scope, str) and spelling(scope) in _GLOBAL_SCOPES:
        return "global"
    raise _RefusalError("invalid_arguments", SKILL_MANAGE_UNKNOWN_SCOPE)


def _refuse_support_file(call: _Call, suggestion: str) -> None:
    if call.file_path is not None and call.file_path != SKILL_FILENAME:
        raise _RefusalError(
            "invalid_arguments",
            f"{call.action} writes SKILL.md. To write {call.file_path}, use action "
            f"{suggestion} with that file_path; omit file_path to {call.action} SKILL.md.",
        )


def _package_path(path: str, name: str) -> str:
    """Remove quoting, a leading slash and a leading ``<name>/`` from a package path."""
    text = clean_skill_file_path(path).lstrip("/")
    first, _, rest = text.partition("/")
    if first == name and (rest == SKILL_FILENAME or rest.split("/", 1)[0] in RESOURCE_DIRECTORIES):
        return rest
    return text


def _has_front_matter(text: str) -> bool:
    return bool(split_skill_document(_unwrapped_document(text))[0].strip())


def _unwrapped_document(text: str) -> str:
    """Drop blank lines before front matter and a code fence around the whole document."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```") and stripped.count("```") == 2:
        inner = stripped[3:-3]
        inner = inner.split("\n", 1)[1] if "\n" in inner else ""
        if inner.lstrip().startswith("---"):
            return inner.lstrip()
    if text.lstrip().startswith("---"):
        return text.lstrip()
    return text


def _complete_document(call: _Call) -> str:
    """Return the SKILL.md to write, or refuse when name or description is unresolved."""
    content = call.content or ""
    document = _unwrapped_document(content)
    front_matter, body, _ = split_skill_document(document)
    if not front_matter.strip():
        if call.action == "create" and call.description and call.description.strip():
            fields: dict[str, Any] = {"name": call.name}
            return _assemble(fields, call.description.strip(), content)
        raise _RefusalError(
            "invalid_arguments",
            _document_needed(call.name, "SKILL.md must start with YAML front matter"),
        )
    parsed, _ = parse_skill_front_matter(front_matter)
    fields = dict(parsed) if isinstance(parsed, dict) else {}
    declared_name = fields.get("name")
    if declared_name is not None and str(declared_name).strip() != call.name:
        raise _RefusalError(
            "invalid_arguments",
            f"The front matter names '{declared_name}' but name is '{call.name}'; use the "
            "same name in both.",
        )
    declared = fields.get("description")
    declared_text = declared.strip() if isinstance(declared, str) else ""
    wanted = (call.description or "").strip()
    if wanted and declared_text and wanted != declared_text:
        raise _RefusalError(
            "invalid_arguments",
            "description differs from the description in the front matter; give it once.",
        )
    if not declared_text and not wanted:
        raise _RefusalError(
            "invalid_arguments",
            _document_needed(call.name, "The front matter needs a description"),
        )
    if declared_name is None or not declared_text:
        fields.setdefault("name", call.name)
        return _assemble(fields, wanted or declared_text, body)
    return document


def _assemble(fields: dict[str, Any], description: str, body: str) -> str:
    ordered = {"name": fields.pop("name"), "description": description}
    fields.pop("description", None)
    ordered.update(fields)
    # One line per scalar keeps the author's lines, so a patch copied from them matches.
    front = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, width=float("inf")).strip()
    return f"---\n{front}\n---\n\n{body.strip(chr(10))}\n"


def _document_needed(name: str, lead: str) -> str:
    return (
        f"{lead}. SKILL.md starts with front matter holding name and a description of "
        f"when to load the Skill, then the instructions:\n---\nname: {name}\n"
        "description: <what it covers and when to load it>\n---\n\n<instructions>"
    )


def _read_hint(name: str, file_path: str) -> str:
    call = json.dumps({"name": name, "file_path": file_path})
    return f"Read the current text with skill {call}."


def _normalize_skill_manage_arguments(arguments: Any) -> Any:
    """Repair call syntax and resolve which field carries the action's text."""
    repaired = normalize_call_arguments(
        _SKILL_MANAGE_RUNTIME_CONTRACT,
        arguments,
        enum_fields=("action",),
        field_aliases=_FIELD_ALIASES,
        field_normalizers={
            "action": lambda value: (
                _ACTION_SYNONYMS.get(spelling(value), value) if isinstance(value, str) else value
            )
        },
        empty_as_omitted=(
            "name",
            "file_path",
            "old_string",
            "scope",
            "category",
            "description",
            "absorbed_into",
        ),
    )
    if not isinstance(repaired, dict):
        return repaired
    _repair_name(repaired)
    _repair_file_path(repaired)
    _resolve_text(repaired)
    return repaired


def _repair_file_path(arguments: dict[str, Any]) -> None:
    """Record the package-relative path the call writes, as the handler resolves it."""
    path, name = arguments.get("file_path"), arguments.get("name")
    if isinstance(path, str) and isinstance(name, str):
        cleaned = _package_path(path, name)
        if cleaned:
            arguments["file_path"] = cleaned


def _repair_name(arguments: dict[str, Any]) -> None:
    """Strip invocation marks and split a package path given as the name."""
    name = arguments.get("name")
    if not isinstance(name, str):
        return
    text = clean_skill_file_path(name).lstrip(_NAME_MARKS)
    segments = [segment for segment in text.split("/") if segment]
    path: str | None = None
    for index, segment in enumerate(segments[1:], start=1):
        if segment == SKILL_FILENAME or (
            segment in RESOURCE_DIRECTORIES and index < len(segments) - 1
        ):
            text, path = segments[index - 1], "/".join(segments[index:])
            break
    arguments["name"] = text
    if path is None or path == SKILL_FILENAME:
        return
    current = arguments.get("file_path")
    if isinstance(current, str) and _package_path(current, text) != path:
        raise ToolContractError.conflicting_values(
            "file_path", ("file_path", current), ("name", name), tool_name=SKILL_MANAGE_TOOL_NAME
        )
    arguments["file_path"] = path


def _resolve_text(arguments: dict[str, Any]) -> None:
    """Keep the one text an action uses; empty duplicates are placeholders."""
    action = arguments.get("action")
    target = {"patch": "new_string", "create": "content", "edit": "content"}.get(
        action if isinstance(action, str) else "", "content"
    )
    supplied = {key: arguments.pop(key) for key in _TEXT_FIELDS if key in arguments}
    if not supplied:
        return
    texts = [value for value in supplied.values() if isinstance(value, str)]
    distinct = sorted({value for value in texts if value})
    if len(distinct) > 1:
        first = next((key, value) for key, value in supplied.items() if value == distinct[0])
        second = next((key, value) for key, value in supplied.items() if value == distinct[1])
        raise ToolContractError.conflicting_values(
            target, first, second, tool_name=SKILL_MANAGE_TOOL_NAME
        )
    if action not in ("patch", "create", "edit", "write_file"):
        # Other actions take no text; the handler refuses a nonempty one.
        if distinct:
            arguments["content"] = distinct[0]
        return
    if distinct:
        arguments[target] = distinct[0]
    elif texts:
        arguments[target] = ""
    else:
        arguments[target] = next(iter(supplied.values()))
