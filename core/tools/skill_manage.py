"""Direct authoring tool for an Identity Agent's writable vBot Skills."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from core.skills.authoring import (
    SkillAuthoringError,
    SkillAuthoringService,
    SkillWriteResult,
)
from core.skills.skill_validator import (
    MAX_SKILL_NAME_LENGTH,
    SKILL_NAME_CHARSET_FRAGMENT,
    parse_skill_front_matter,
    split_skill_document,
)
from core.skills.skills import (
    RESOURCE_DIRECTORIES,
    SKILL_FILENAME,
    find_skill_package_dir,
    scan_skill_names,
)
from core.tools._argument_repair import normalize_call_arguments
from core.tools.availability import SKILL_MANAGE_TOOL_NAME
from core.tools.contracts import ToolContractError, compile_tool_contract
from core.tools.fuzzy_match import (
    AmbiguousFuzzyMatch,
    FuzzyReplacement,
    find_closest_candidates,
    preserve_typography,
    replace_fuzzy,
)
from core.tools.skill import clean_skill_file_path, similar_skill_names
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
    offload_tool_handler,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

SKILL_MANAGE_TOOL_DESCRIPTION = (
    'Create, change or delete one of your own Skills (listed under "Your own skills"), '
    "or write or remove one of its support files. create and edit take the complete "
    "SKILL.md: YAML front matter with name and description, then the instructions. "
    "patch replaces old_string with new_string in SKILL.md or in file_path."
)

_ACTIONS = ("create", "edit", "patch", "write_file", "remove_file", "delete")
# Actions that may operate on a Skill shared into the caller (maintained in the
# owner's package). ``create`` is own-home-only by definition; ``delete`` stays
# owner/human-only so a receiver cannot remove someone else's playbook.
_SHARED_TARGET_ACTIONS = frozenset({"edit", "patch", "write_file", "remove_file"})
_LOGGER = get_logger("tools.skill_manage")

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
_NAME_MARKS = "/$@"
_LISTED_NAME_LIMIT = 20
_PREVIEW_LIMIT = 400

# Typographic glyphs a Model may type where a file has the plain form. Mirrors the
# patch folds of ``fuzzy_match`` so a folded match also folds its replacement.
_TYPOGRAPHY = {
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "–": "-",
    "—": "--",
    "−": "-",
    "…": "...",
    " ": " ",
}
_JSON_ESCAPE = re.compile(r'\\(["\\/nrt])')
_JSON_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "r": "\r", "t": "\t"}

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
    notes: list[str] = field(default_factory=list)


def make_skill_manage_handler(
    authoring: SkillAuthoringService,
    resolve_agent_skills_dir: Callable[[str], Path],
    invalidate_agent_skills: Callable[[str | None], None],
    resolve_shared_skills_dir: Callable[[str, str], Path | None] | None = None,
    resolve_external_skill_scope: (Callable[[str, str, str | None], str | None] | None) = None,
) -> Callable[[ToolContext, JsonObject], JsonObject]:
    """Return the direct Skill-management handler.

    ``resolve_shared_skills_dir(agent_id, name)`` optionally maps a name that is
    not one of the caller's own Skills to the owning home of the effective shared
    instance (first-found ordering, exactly what activation resolves). It is
    intentionally absent from the Tool contract and Agent-facing texts — a
    receiving Agent discovers editability through normal use.

    ``resolve_external_skill_scope(agent_id, name, project_id)`` optionally answers
    where a name that is missing from the resolved target root still lives in the
    agent's visible pool (``bundled``/``global``/``project``/``shared``), so the
    tool can fail with a scope refusal instead of a misleading not-found for a
    Skill the agent can see but not write. ``None`` keeps the plain not-found.
    """

    def skill_manage_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        try:
            call = _read_call(arguments)
            own_root = resolve_agent_skills_dir(context.agent_id)
            target_root = own_root
            shared_target = False
            if call.action in _SHARED_TARGET_ACTIONS and (
                find_skill_package_dir(own_root, call.name) is None
            ):
                shared_root = (
                    resolve_shared_skills_dir(context.agent_id, call.name)
                    if resolve_shared_skills_dir is not None
                    else None
                )
                if shared_root is not None:
                    target_root = shared_root
                    shared_target = True
            # A missing target package on a mutate/delete action is not always an
            # unknown name: it may be a Skill the agent can see in another scope but
            # cannot write. Report that scope instead of a bare not-found. ``create``
            # is excluded — it legitimately writes a private shadow over a shared-pool
            # name, which is the established override path.
            if call.action != "create" and find_skill_package_dir(target_root, call.name) is None:
                scope = (
                    resolve_external_skill_scope(
                        context.agent_id, call.name, context.skill_project_id
                    )
                    if resolve_external_skill_scope is not None
                    else None
                )
                if scope is not None:
                    return tool_failure(
                        "skill_write_rejected",
                        _scope_rejection_message(call.name, scope),
                        retryable=False,
                    )
                raise _RefusalError("skill_not_found", _unknown_skill_message(call.name, own_root))
            result, summary = _apply(authoring, target_root, call)
        except _RefusalError as refusal:
            return tool_failure(refusal.code, refusal.message, retryable=False)
        except SkillAuthoringError as error:
            return tool_failure(
                "skill_write_rejected",
                "; ".join(error.diagnostics),
                retryable=False,
            )
        except OSError as error:
            return tool_failure("skill_write_error", str(error))

        # An own-home invalidation also reaches its shared receivers. Receiver
        # edits conservatively invalidate all Agent scopes through the same owner.
        invalidate_agent_skills(None if shared_target else context.agent_id)
        _LOGGER.info(
            "Skill mutated (skill=%s scope=%s owner=%s action=%s actor_agent=%s)",
            result.name,
            "shared" if shared_target else "own",
            target_root.parent.name if shared_target else context.agent_id,
            call.action,
            context.agent_id,
        )
        # Deliberately identical for own and shared targets: a receiving Agent
        # must not be able to tell a shared Skill apart from its own.
        lines = [summary]
        lines.extend(f"Warning: {warning}" for warning in result.warnings)
        lines.extend(f"Note: {note}" for note in call.notes)
        return tool_success({"content": "\n".join(lines)})

    return skill_manage_handler


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
    )
    _check_scope(arguments.get("scope"))
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
    call.file_path = call.file_path or SKILL_FILENAME
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
}


def _text(arguments: JsonObject, key: str) -> str | None:
    value = arguments.get(key)
    return value if isinstance(value, str) else None


def _check_scope(scope: object) -> None:
    if scope is None:
        return
    if isinstance(scope, str) and _spelling(scope) in _OWN_SCOPES:
        return
    raise _RefusalError(
        "invalid_arguments",
        "skill_manage writes only your own Skills; global, Project and bundled Skills are "
        "read-only here. Omit scope to write one of your own Skills.",
    )


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
    front = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True).strip()
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


def _apply(
    authoring: SkillAuthoringService, target_root: Path, call: _Call
) -> tuple[SkillWriteResult, str]:
    name = call.name
    if call.action == "create":
        result = authoring.create(target_root, name, call.content or "", author="agent")
        return result, f"Created Skill '{name}'."
    if call.action == "edit":
        result = authoring.edit(target_root, name, call.content or "", author="agent")
        return result, f"Replaced SKILL.md of Skill '{name}'."
    if call.action == "patch":
        return _patch(authoring, target_root, call)
    file_path = call.file_path or ""
    if call.action == "write_file":
        result = authoring.write_file(target_root, name, file_path, call.content or "")
        return result, f"Wrote {file_path} of Skill '{name}'."
    if call.action == "remove_file":
        result = authoring.remove_file(target_root, name, file_path)
        return result, f"Removed {file_path} from Skill '{name}'."
    result = authoring.delete(target_root, name)
    return result, f"Deleted Skill '{name}' and its files."


def _patch(
    authoring: SkillAuthoringService, target_root: Path, call: _Call
) -> tuple[SkillWriteResult, str]:
    file_path = call.file_path or SKILL_FILENAME
    found: list[FuzzyReplacement] = []

    def edit(current: str) -> str:
        replacement, notes = _replace(current, call, file_path)
        found.append(replacement)
        call.notes.extend(notes)
        return replacement.new_content

    try:
        result = authoring.rewrite(target_root, call.name, file_path, edit, author="agent")
    except _RefusalError as refusal:
        if refusal.code == "text_not_found" and call.file_path == SKILL_FILENAME:
            hint = _support_file_hint(authoring, target_root, call)
            if hint:
                raise _RefusalError(refusal.code, f"{refusal.message}\n{hint}") from None
        raise
    replacement = found[0]
    where = f"{file_path} of Skill '{call.name}'"
    if replacement.replacements > 1:
        return result, f"Replaced {replacement.replacements} occurrences in {where}."
    return result, f"Patched {where} at line {replacement.first_changed_line}."


def _replace(current: str, call: _Call, file_path: str) -> tuple[FuzzyReplacement, list[str]]:
    old = _lf(call.old_string or "")
    new = _lf(call.new_string or "")
    notes: list[str] = []
    found = _find(current, old, new, call.replace_all)
    if found is None:
        decoded_old, decoded_new = _json_unescaped(old), _json_unescaped(new)
        if decoded_old != old:
            decoded = _find(current, decoded_old, decoded_new, call.replace_all)
            if decoded is not None:
                old, new, found = decoded_old, decoded_new, decoded
                notes.append(
                    "old_string and new_string arrived with an extra level of JSON escaping "
                    '(such as \\n or \\"); they were applied unescaped.'
                )
    if found is None:
        raise _RefusalError(
            "text_not_found", _not_found_message(current, old, call.name, file_path)
        )
    if isinstance(found, AmbiguousFuzzyMatch):
        lines = ", ".join(str(line) for line in dict.fromkeys(found.line_numbers))
        raise _RefusalError(
            "ambiguous_match",
            f"old_string matches {found.occurrences} places in {file_path} of Skill "
            f"'{call.name}' (lines {lines}); nothing changed. Include more surrounding text so "
            "it matches once, or set replace_all to true to change every occurrence.",
        )
    if found.strategy != "exact":
        adjusted = _matching_typography(current, found, old, new)
        if adjusted != new:
            refound = _find(current, old, adjusted, call.replace_all)
            if isinstance(refound, FuzzyReplacement):
                found = refound
    return found, notes


def _find(
    current: str, old: str, new: str, replace_all: bool
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    return replace_fuzzy(
        current, old, new, replace_all=replace_all, precise_only=True, typographic=True
    )


def _matching_typography(current: str, found: FuzzyReplacement, old: str, new: str) -> str:
    """Write the file's own quote and dash glyphs into a normalized match's replacement."""
    start, end = found.before_spans[0]
    region = current[start:end]
    for glyph, plain in _TYPOGRAPHY.items():
        if glyph in new and glyph in old and glyph not in region:
            new = new.replace(glyph, plain)
    region_lines, old_lines, new_lines = region.split("\n"), old.split("\n"), new.split("\n")
    if found.replacements == 1 and len(region_lines) == len(old_lines) == len(new_lines):
        new = "\n".join(
            preserve_typography(actual, locator, replacement)
            for actual, locator, replacement in zip(region_lines, old_lines, new_lines, strict=True)
        )
    return new


def _not_found_message(current: str, old: str, name: str, file_path: str) -> str:
    lines = [f"old_string was not found in {file_path} of Skill '{name}'; nothing changed."]
    candidates = find_closest_candidates(current, old)[:2]
    for candidate in candidates:
        text = candidate.text[:_PREVIEW_LIMIT]
        more = " ..." if candidate.truncated or len(candidate.text) > _PREVIEW_LIMIT else ""
        lines.append(f"Closest text at line {candidate.line_number}:\n{text}{more}")
    lines.append(
        f"Copy the exact current text into old_string. {_read_hint(name, file_path)}"
        if candidates
        else _read_hint(name, file_path)
    )
    return "\n".join(lines)


def _support_file_hint(authoring: SkillAuthoringService, target_root: Path, call: _Call) -> str:
    package = find_skill_package_dir(target_root, call.name)
    if package is None:
        return ""
    old = _lf(call.old_string or "")
    matches: list[str] = []
    for directory in RESOURCE_DIRECTORIES:
        for path in sorted((package / directory).rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(package).as_posix()
            try:
                text = authoring.read_text(target_root, call.name, relative)
            except (SkillAuthoringError, OSError):
                continue
            if _find(text, old, old, False) is not None:
                matches.append(relative)
    if len(matches) != 1:
        return ""
    return f'The text is in {matches[0]}; to patch it there, add "file_path": "{matches[0]}".'


def _unknown_skill_message(name: str, own_root: Path) -> str:
    names = sorted(scan_skill_names(own_root))
    lead = f"You have no Skill named '{name}'; nothing changed."
    suggestions = similar_skill_names(name, names)
    if suggestions:
        quoted = ", ".join(f"'{candidate}'" for candidate in suggestions)
        return f"{lead} Did you mean {quoted}?"
    if not names:
        return f"{lead} You have no Skills of your own yet; use action create to add one."
    if len(names) <= _LISTED_NAME_LIMIT:
        return f"{lead} Your own Skills: {', '.join(names)}."
    return f"{lead} List your own Skills with the skill Tool."


def _scope_rejection_message(name: str, scope: str) -> str:
    if scope == "shared":
        return (
            f"Skill '{name}' is shared with you — only its owner or the user can "
            f"delete it. Edits still go through skill_manage."
        )
    labels = {
        "bundled": "is a bundled Skill — read-only here.",
        "global": "is a global Skill — read-only here.",
        "project": "is a Project Skill — read-only here.",
    }
    lead = labels.get(scope)
    if lead is None:
        return f"Skill '{name}' not found."
    return (
        f"Skill '{name}' {lead} Non-private Skills are managed through the "
        f"user-facing Skill controls; do not edit the package with file or shell tools."
    )


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _json_unescaped(text: str) -> str:
    return _JSON_ESCAPE.sub(lambda match: _JSON_ESCAPES[match.group(1)], text)


def _spelling(value: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", value.casefold())


def _normalize_skill_manage_arguments(arguments: Any) -> Any:
    """Repair call syntax and resolve which field carries the action's text."""
    repaired = normalize_call_arguments(
        _SKILL_MANAGE_RUNTIME_CONTRACT,
        arguments,
        enum_fields=("action",),
        field_aliases=_FIELD_ALIASES,
        field_normalizers={
            "action": lambda value: (
                _ACTION_SYNONYMS.get(_spelling(value), value) if isinstance(value, str) else value
            )
        },
        empty_as_omitted=("name", "file_path", "old_string", "scope", "category", "description"),
    )
    if not isinstance(repaired, dict):
        return repaired
    _repair_name(repaired)
    _resolve_text(repaired)
    return repaired


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
        raise ToolContractError("Conflicting values for file_path; provide one intended value.")
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
        names = " and ".join(sorted(supplied))
        raise ToolContractError(f"Conflicting values for {target}: {names} differ; give one text.")
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


def register_skill_manage_tool(
    registry: ToolRegistry,
    authoring: SkillAuthoringService,
    resolve_agent_skills_dir: Callable[[str], Path],
    invalidate_agent_skills: Callable[[str | None], None],
    resolve_shared_skills_dir: Callable[[str, str], Path | None] | None = None,
    resolve_external_skill_scope: (Callable[[str, str, str | None], str | None] | None) = None,
    *,
    lifecycle_guard: Callable[[], AbstractContextManager[object]] = nullcontext,
) -> None:
    """Register identity-only direct Skill management."""
    handler = make_skill_manage_handler(
        authoring,
        resolve_agent_skills_dir,
        invalidate_agent_skills,
        resolve_shared_skills_dir,
        resolve_external_skill_scope,
    )

    def guarded_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        with lifecycle_guard():
            return handler(context, arguments)

    registry.register(
        SKILL_MANAGE_TOOL_NAME,
        SKILL_MANAGE_TOOL_DESCRIPTION,
        SKILL_MANAGE_TOOL_PARAMETERS,
        offload_tool_handler(guarded_handler),
        family="skills",
        constraints=("identity_agent",),
        open_input_schema=True,
        unadvertised_parameters=_UNADVERTISED_PARAMETERS,
        argument_normalizer=_normalize_skill_manage_arguments,
        result_schema={"type": "object", "required": ["content"]},
        display=ToolDisplay(
            parts_builder=_skill_manage_display_parts,
            hidden_argument_keys=("content", "old_string", "new_string"),
        ),
    )


def _skill_manage_display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    action = arguments.get("action")
    if not isinstance(action, str) or not action.strip():
        return ()
    parts = [ToolDisplayPart(action.strip(), truncate="never", tooltip="none")]
    value = arguments.get("name")
    if isinstance(value, str) and value.strip():
        parts.append(ToolDisplayPart(value.strip()))
    return tuple(parts)


__all__ = [
    "SKILL_MANAGE_TOOL_DESCRIPTION",
    "SKILL_MANAGE_TOOL_NAME",
    "SKILL_MANAGE_TOOL_PARAMETERS",
    "make_skill_manage_handler",
    "register_skill_manage_tool",
]
