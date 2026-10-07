"""Direct authoring tool for an Identity Agent's own Skills and the global Skills."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from core.runs import RunKind
from core.skills.authoring import (
    SkillAuthoringError,
    SkillAuthoringService,
    SkillProtectedError,
    SkillReference,
    SkillWriter,
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
from core.tools._skill_conventions import skill_manage_hints
from core.tools.availability import SKILL_MANAGE_TOOL_NAME
from core.tools.call_syntax import normalize_call_arguments, spelling
from core.tools.contracts import ToolContractError, compile_tool_contract
from core.tools.copy_match import copy_warnings, replace_copied
from core.tools.fuzzy_match import (
    AmbiguousFuzzyMatch,
    FuzzyReplacement,
    find_closest_candidates,
    preserve_typography,
    replace_fuzzy,
)
from core.tools.skill import (
    LIBRARIAN_SKILL_ACTOR,
    clean_skill_file_path,
    similar_skill_names,
    skill_writer,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
    run_tool_worker,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger
from core.utils.timestamps import parse_timestamp

SKILL_MANAGE_TOOL_DESCRIPTION = (
    'Create, change or delete one of your own Skills (listed under "Your own skills"), '
    "or write or remove one of its support files. create and edit take the complete "
    "SKILL.md: YAML front matter with name and description, then the instructions. "
    "patch replaces old_string with new_string in SKILL.md or in file_path. "
    'edit, patch, write_file and remove_file also change a Skill listed under "Your '
    'global skills", which other Agents use too. publish turns one of your own Skills '
    "into a global Skill."
)

# Results of a delete, which moves the Skill into the archive of its home.
SKILL_MANAGE_DELETED = (
    "Deleted Skill '{name}'. Its files are kept in the archive, where the user can restore it."
)
SKILL_MANAGE_ABSORBED = (
    "Deleted Skill '{name}'; its instructions now live in Skill '{target}'. Its files are "
    "kept in the archive, where the user can restore it."
)
# After a merge: the deleted Skill's shares and the automations that loaded it
# now use the Skill that absorbed it, so nobody has to change them by hand. One
# that could not be changed still names the deleted Skill and finds nothing, so
# the Agent passes it on to the user.
SKILL_MANAGE_FOLLOWED_NOTE = "These now use Skill '{target}' in place of '{name}': {items}."
SKILL_MANAGE_NOT_FOLLOWED_WARNING = (
    "These could not be changed and still name '{name}', which no longer exists: {items}. "
    "Name them in your reply so the user can change them to '{target}'."
)
# After a delete without absorbed_into: nothing holds the instructions, so what
# named the Skill now finds nothing, and the Agent passes it on to the user.
SKILL_MANAGE_ORPHANED_WARNING = (
    "These still name '{name}', which no longer exists: {items}. Name them in your reply so "
    "the user can change or remove them."
)
_REFERENCE_LABELS = {
    "shared": "the share with Agent '{name}'",
    "bootstrap": "Bootstrap job '{name}'",
    "cron": "Cron job '{name}'",
    "calendar": "Calendar event '{name}'",
}
# Refusals of a background writer: a Reflection, or the Librarian, also while
# the user talks with it in a Librarian Session. It never changes a Skill the
# user pinned. The reply is where it reports what it could not change; the
# briefs ask for the same.
_LEAVE_IT = "Leave it as it is and name the needed change in your reply."
SKILL_MANAGE_PINNED_REFUSAL = (
    f"Skill '{{name}}' is pinned by the user, so you cannot change it; nothing changed. {_LEAVE_IT}"
)
SKILL_MANAGE_HISTORY_UNREADABLE_REFUSAL = (
    "The history of Skill '{name}' cannot be read, so whether the user pinned it is unknown "
    f"and you cannot change it; nothing changed. {_LEAVE_IT}"
)
# A Librarian pass builds its changes from what it read; a Skill that someone else
# changed after the pass started is read again first.
SKILL_MANAGE_CHANGED_DURING_PASS_REFUSAL = (
    "Skill '{name}' was changed outside this pass after the pass started; nothing changed. "
    "Load '{name}' again with skill and build your change from that text, or leave it as "
    "it is."
)
# Global Skills (the user's global home) serve every Agent. A background writer
# never changes one, and only the user deletes one.
SKILL_MANAGE_GLOBAL_BACKGROUND_REFUSAL = (
    f"Skill '{{name}}' is a global Skill, which you cannot change here; nothing changed. "
    f"{_LEAVE_IT}"
)
SKILL_MANAGE_PUBLISH_BACKGROUND_REFUSAL = (
    "You cannot make a Skill global here; nothing changed. Leave Skill '{name}' as it is."
)
SKILL_MANAGE_PUBLISHED = (
    "Skill '{name}' is now a global Skill instead of one of your own. Other Agents can use it "
    "when their Skill selection allows it."
)
SKILL_MANAGE_PUBLISH_CONFLICT = (
    "A global Skill named '{name}' already exists; nothing changed. Tell the user, who can "
    "compare the two Skills in the Skill controls."
)
SKILL_MANAGE_GLOBAL_CREATE = (
    "create adds one of your own Skills; nothing changed. Omit scope to create it, and use "
    "action publish to make one of your own Skills global."
)
SKILL_MANAGE_UNKNOWN_SCOPE = (
    "skill_manage changes your own Skills and global Skills; Project and bundled Skills are "
    "read-only here, and nothing changed. Omit scope to change a Skill by its name."
)
# ``absorbed_into`` names the Skill that now holds a deleted Skill's instructions.
SKILL_MANAGE_ABSORBED_INTO_ACTION = (
    "absorbed_into is used only by delete; nothing changed. Omit absorbed_into for {action}."
)
SKILL_MANAGE_ABSORBED_INTO_SELF = (
    "absorbed_into names '{name}' itself; nothing changed. Name the other Skill that now holds "
    "its instructions."
)
SKILL_MANAGE_ABSORBED_INTO_UNKNOWN = (
    "absorbed_into names '{target}', which is not one of your own Skills; nothing changed. "
    "Name one of your own Skills that now holds the instructions of '{name}'."
)

_ACTIONS = ("create", "edit", "patch", "write_file", "remove_file", "delete", "publish")
# follow_merge(owner_id, name, target, delete) -> the references that could not move.
SkillMergeFollower = Callable[
    [str, str, str | None, Callable[[tuple[SkillReference, ...]], Awaitable[bool]]],
    Awaitable[tuple[SkillReference, ...]],
]
# Actions that may operate on a Skill shared into the caller (maintained in the
# owner's package), by every writer, and on a global Skill by an attended Agent.
# ``create`` and ``publish`` are own-home-only by definition; ``delete`` stays
# owner/human-only so an Agent cannot remove someone else's playbook.
_SHARED_TARGET_ACTIONS = frozenset({"edit", "patch", "write_file", "remove_file"})
_PROTECTED_MESSAGES = {
    "pinned": SKILL_MANAGE_PINNED_REFUSAL,
    "unknown": SKILL_MANAGE_HISTORY_UNREADABLE_REFUSAL,
}
_LOGGER = get_logger("tools.skill_manage")
# How many (Run, Skill) pairs remember the outside change they were told about.
_REPORTED_CHANGES_LIMIT = 1024
# Revisions read back to find an outside change; a pass writes far fewer per Skill.
_CHANGE_CHECK_REVISIONS = 100

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
_PREVIEW_LIMIT = 400
# Actions whose written text gets authoring hints, and those that replace a whole file.
_HINTED_ACTIONS = frozenset({"create", "edit", "patch", "write_file"})
_WHOLE_FILE_ACTIONS = frozenset({"create", "edit", "write_file"})
# Support files the hints read: the directories SKILL.md points into, bounded.
_HINT_DIRECTORIES = ("references", "scripts")
_HINT_FILE_LIMIT = 200
_HINT_TEXT_BYTES = 256 * 1024

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
    absorbed_into: str | None = None
    # ``global`` when the call names the global home explicitly, else ``None``.
    scope: str | None = None
    notes: list[str] = field(default_factory=list)


def make_skill_manage_handler(
    authoring: SkillAuthoringService,
    resolve_agent_skills_dir: Callable[[str], Path],
    invalidate_agent_skills: Callable[[str | None], None],
    resolve_shared_skills_dir: Callable[[str, str], Path | None] | None = None,
    resolve_external_skill_scope: (Callable[[str, str, str | None], str | None] | None) = None,
    *,
    on_changed: Callable[[], None] | None = None,
    resolve_global_skills_dir: Callable[[], Path] | None = None,
    publish_skill: Callable[[str, str, SkillWriter], SkillWriteResult] | None = None,
) -> Callable[[ToolContext, JsonObject, str | None, tuple[SkillReference, ...]], _Outcome]:
    """Return the direct Skill-management handler.

    A background writer (a Reflection or the Librarian) never changes a Skill
    the user pinned.

    The handler's third argument is when the calling Run started, given for a
    Librarian Run. Such a Run builds its changes from Skills it read during the Run, so a
    change to an existing Skill that someone else changed after the Run started
    is refused once per outside change; the Run reads the Skill again and
    retries, or leaves it. The fourth is what a delete with ``absorbed_into``
    moves to the absorbing Skill (``register_skill_manage_tool``); the archive
    revision records it.

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

    ``on_changed()`` optionally reports each successful mutation, after the
    affected scoped caches were invalidated, so open Skill views can refresh.

    ``resolve_global_skills_dir()`` returns the global home. With it, an attended
    Agent's edit, patch, write_file and remove_file reach a global Skill the
    resolver reports as ``global`` (or named with ``scope: global``); a
    background writer is refused. ``publish_skill(agent_id, name, writer)``
    moves an own Skill into that home (``SkillRuntime.publish_agent_skill``).
    The handler's outcome says whether the global home changed, so the caller
    reloads the global Skills.
    """

    # (Run id, Skill name) -> the newest outside revision the Run was told about.
    reported_changes: dict[tuple[str, str], int] = {}
    reported_lock = threading.Lock()

    def check_unchanged_since(root: Path, name: str, writer: SkillWriter, started: str) -> None:
        """Refuse once per outside change to ``name`` after the Run started."""
        try:
            since = parse_timestamp(started)
        except ValueError:
            return
        outside = [
            revision.id
            for revision in authoring.history(root, name, limit=_CHANGE_CHECK_REVISIONS)
            if revision.run_id != writer.run_id and _after(revision.at, since)
        ]
        if not outside:
            return
        key = (writer.run_id or "", name)
        with reported_lock:
            if reported_changes.get(key, 0) >= max(outside):
                return
            reported_changes[key] = max(outside)
            while len(reported_changes) > _REPORTED_CHANGES_LIMIT:
                del reported_changes[next(iter(reported_changes))]
        raise _RefusalError(
            "skill_changed", SKILL_MANAGE_CHANGED_DURING_PASS_REFUSAL.format(name=name)
        )

    def skill_manage_handler(
        context: ToolContext,
        arguments: JsonObject,
        run_started_at: str | None = None,
        followed: tuple[SkillReference, ...] = (),
    ) -> _Outcome:
        writer = skill_writer(context)
        # The Skills of the calling Agent, or of the Agent a Librarian Session maintains.
        owner_id = context.skill_subject_id
        try:
            call = _read_call(arguments)
            own_root = resolve_agent_skills_dir(owner_id)
            global_root = (
                resolve_global_skills_dir() if resolve_global_skills_dir is not None else None
            )
            target_root, target = own_root, "own"
            if call.scope == "global" and call.action != "publish":
                target_root, target = _global_target(call, writer, global_root), "global"
            elif call.action in _SHARED_TARGET_ACTIONS and (
                find_skill_package_dir(own_root, call.name) is None
            ):
                shared_root = (
                    resolve_shared_skills_dir(owner_id, call.name)
                    if resolve_shared_skills_dir is not None
                    else None
                )
                if shared_root is not None:
                    target_root, target = shared_root, "shared"
            # A missing target package on a mutate/delete action is not always an
            # unknown name: it may be a Skill the agent can see in another scope.
            # A global one an attended Agent changes; for the others report the
            # scope instead of a bare not-found. ``create`` is excluded — it
            # legitimately writes a private shadow over a shared-pool name, which
            # is the established override path.
            if call.action != "create" and find_skill_package_dir(target_root, call.name) is None:
                if target == "global":
                    raise _RefusalError(
                        "skill_not_found", _unknown_skill_message(call.name, target_root, own=False)
                    )
                scope = (
                    resolve_external_skill_scope(owner_id, call.name, context.skill_project_id)
                    if resolve_external_skill_scope is not None
                    else None
                )
                if scope == "global" and call.action in _SHARED_TARGET_ACTIONS:
                    target_root, target = _global_target(call, writer, global_root), "global"
                elif scope is not None:
                    return _Outcome(
                        tool_failure(
                            "skill_write_rejected",
                            _scope_rejection_message(call.name, scope, call.action),
                            retryable=False,
                        )
                    )
                else:
                    raise _RefusalError(
                        "skill_not_found", _unknown_skill_message(call.name, own_root)
                    )
            # Checks and write run under one Skill write lock, so a change that
            # lands after a check cannot be overwritten by this write.
            hints: list[str] = []
            with authoring.exclusive():
                if call.action == "publish":
                    result, summary = _publish(call, writer, owner_id, global_root, publish_skill)
                else:
                    if writer.background and call.action != "create":
                        authoring.check_writable(target_root, call.name, writer=writer)
                    if (
                        writer.actor == LIBRARIAN_SKILL_ACTOR
                        and run_started_at is not None
                        and call.action != "create"
                    ):
                        check_unchanged_since(target_root, call.name, writer, run_started_at)
                    if call.action == "delete":
                        _check_absorbed_into(call, own_root)
                    result, summary = _apply(authoring, target_root, call, writer, followed)
                    hints = _hints(authoring, target_root, call, result)
        except _RefusalError as refusal:
            return _Outcome(tool_failure(refusal.code, refusal.message, retryable=False))
        except SkillProtectedError as error:
            return _Outcome(
                tool_failure(
                    "skill_protected",
                    _PROTECTED_MESSAGES[error.reason].format(name=error.skill_name),
                    retryable=False,
                )
            )
        except SkillAuthoringError as error:
            return _Outcome(
                tool_failure(
                    "skill_write_rejected",
                    "; ".join(error.diagnostics),
                    retryable=False,
                )
            )
        except OSError as error:
            return _Outcome(tool_failure("skill_write_error", str(error)))

        global_changed = target == "global" or call.action == "publish"
        # An own-home invalidation also reaches its shared receivers. Receiver
        # and global edits conservatively invalidate all Agent scopes.
        invalidate_agent_skills(owner_id if target == "own" and not global_changed else None)
        if on_changed is not None:
            on_changed()
        _LOGGER.info(
            "Skill mutated (skill=%s scope=%s owner=%s action=%s actor_agent=%s)",
            result.name,
            target,
            target_root.parent.name if target == "shared" else owner_id,
            call.action,
            context.agent_id,
        )
        _record_display_details(context, result)
        # Deliberately identical for own and shared targets: a receiving Agent
        # must not be able to tell a shared Skill apart from its own.
        lines = [summary]
        lines.extend(f"Warning: {warning}" for warning in result.warnings)
        lines.extend(f"Note: {note}" for note in call.notes)
        lines.extend(f"Hint: {hint}" for hint in hints)
        return _Outcome(tool_success({"content": "\n".join(lines)}), global_changed)

    return skill_manage_handler


@dataclass(frozen=True)
class _Outcome:
    """A handler result, and whether it changed the global home."""

    result: JsonObject
    global_changed: bool = False


def _global_target(call: _Call, writer: SkillWriter, global_root: Path | None) -> Path:
    """The global home for a change of a global Skill, or the refusal of the call."""
    if call.action == "create":
        raise _RefusalError("invalid_arguments", SKILL_MANAGE_GLOBAL_CREATE)
    if call.action == "delete":
        raise _RefusalError("skill_write_rejected", _scope_rejection_message(call.name, "global"))
    if global_root is None:
        raise _RefusalError("skill_write_rejected", _scope_rejection_message(call.name, "external"))
    if writer.background:
        raise _RefusalError(
            "skill_protected", SKILL_MANAGE_GLOBAL_BACKGROUND_REFUSAL.format(name=call.name)
        )
    return global_root


def _publish(
    call: _Call,
    writer: SkillWriter,
    owner_id: str,
    global_root: Path | None,
    publish_skill: Callable[[str, str, SkillWriter], SkillWriteResult] | None,
) -> tuple[SkillWriteResult, str]:
    """Make the caller's own Skill ``call.name`` a global Skill."""
    if writer.background or publish_skill is None or global_root is None:
        raise _RefusalError(
            "skill_write_rejected", SKILL_MANAGE_PUBLISH_BACKGROUND_REFUSAL.format(name=call.name)
        )
    if (global_root / call.name).exists() or find_skill_package_dir(global_root, call.name):
        raise _RefusalError(
            "skill_write_rejected", SKILL_MANAGE_PUBLISH_CONFLICT.format(name=call.name)
        )
    result = publish_skill(owner_id, call.name, writer)
    return result, SKILL_MANAGE_PUBLISHED.format(name=call.name)


def _after(timestamp: str, moment: datetime) -> bool:
    """Whether ``timestamp`` is later than ``moment``; an unreadable one is not."""
    try:
        return parse_timestamp(timestamp) > moment
    except ValueError:
        return False


def _check_absorbed_into(call: _Call, own_root: Path) -> None:
    """A delete's ``absorbed_into``, when given, names another own Skill."""
    target = call.absorbed_into
    if target is None:
        return
    if target == call.name:
        raise _RefusalError(
            "invalid_arguments", SKILL_MANAGE_ABSORBED_INTO_SELF.format(name=call.name)
        )
    if find_skill_package_dir(own_root, target) is None:
        raise _RefusalError(
            "invalid_arguments",
            SKILL_MANAGE_ABSORBED_INTO_UNKNOWN.format(target=target, name=call.name),
        )


def _hints(
    authoring: SkillAuthoringService, target_root: Path, call: _Call, result: SkillWriteResult
) -> list[str]:
    """The authoring conventions the written text breaks; never fails the write."""
    if call.action not in _HINTED_ACTIONS or len(result.changes) != 1:
        return []
    change = result.changes[0]
    try:
        files = _package_texts(authoring, target_root, call.name)
        files[change.path] = change.after
        return skill_manage_hints(
            files, change.path, change.before, whole=call.action in _WHOLE_FILE_ACTIONS
        )
    except Exception:
        # The write succeeded; a defect in the advice must not report it as failed.
        _LOGGER.warning("Skill authoring hints failed (skill=%s)", call.name, exc_info=True)
        return []


def _package_texts(
    authoring: SkillAuthoringService, target_root: Path, name: str
) -> dict[str, str | None]:
    """SKILL.md and the files under references/ and scripts/, by package path.

    A file that is large, not UTF-8 or unreadable maps to ``None``.
    """
    package = find_skill_package_dir(target_root, name)
    files: dict[str, str | None] = {}
    if package is None:
        return files
    paths = [package / SKILL_FILENAME]
    for directory in _HINT_DIRECTORIES:
        # Path.walk never enters a link, Windows junctions included.
        walk = (package / directory).walk()
        paths.extend(sorted(base / entry for base, _, names in walk for entry in names))
    for path in paths[:_HINT_FILE_LIMIT]:
        relative = path.relative_to(package).as_posix()
        try:
            if not path.is_file() or path.stat().st_size > _HINT_TEXT_BYTES:
                files[relative] = None
                continue
            files[relative] = authoring.read_text(target_root, name, relative)
        except SkillAuthoringError, OSError:
            files[relative] = None
    return files


def _record_display_details(context: ToolContext, result: SkillWriteResult) -> None:
    """Show the user the changed package files and the Skill's validation warnings."""
    for change in result.changes:
        if change.before != change.after:
            context.add_display_file_change(change.path, change.change, change.before, change.after)
    if result.changes and all(change.before == change.after for change in result.changes):
        context.add_display_notice("info", "Nothing changed; the file already had this text.")
    for warning in result.warnings:
        context.add_display_notice("warning", warning)


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


def _apply(
    authoring: SkillAuthoringService,
    target_root: Path,
    call: _Call,
    writer: SkillWriter,
    followed: tuple[SkillReference, ...] = (),
) -> tuple[SkillWriteResult, str]:
    name = call.name
    if call.action == "create":
        result = authoring.create(target_root, name, call.content or "", writer=writer)
        return result, f"Created Skill '{name}'."
    if call.action == "edit":
        result = authoring.edit(target_root, name, call.content or "", writer=writer)
        return result, f"Replaced SKILL.md of Skill '{name}'."
    if call.action == "patch":
        return _patch(authoring, target_root, call, writer)
    file_path = call.file_path or ""
    if call.action == "write_file":
        result = authoring.write_file(
            target_root, name, file_path, call.content or "", writer=writer
        )
        return result, f"Wrote {file_path} of Skill '{name}'."
    if call.action == "remove_file":
        result = authoring.remove_file(target_root, name, file_path, writer=writer)
        return result, f"Removed {file_path} from Skill '{name}'."
    if call.absorbed_into is not None:
        result = authoring.delete(
            target_root, name, writer=writer, absorbed_into=call.absorbed_into, followed=followed
        )
        return result, SKILL_MANAGE_ABSORBED.format(name=name, target=call.absorbed_into)
    result = authoring.delete(target_root, name, writer=writer)
    return result, SKILL_MANAGE_DELETED.format(name=name)


def _patch(
    authoring: SkillAuthoringService, target_root: Path, call: _Call, writer: SkillWriter
) -> tuple[SkillWriteResult, str]:
    file_path = call.file_path or SKILL_FILENAME
    found: list[FuzzyReplacement] = []
    unchanged: list[bool] = []

    def edit(current: str) -> str:
        replacement, notes = _replace(current, call, file_path)
        found.append(replacement)
        unchanged.append(replacement.new_content == current)
        call.notes.extend(notes)
        return replacement.new_content

    try:
        result = authoring.rewrite(target_root, call.name, file_path, edit, writer=writer)
    except _RefusalError as refusal:
        if refusal.code == "text_not_found" and call.file_path == SKILL_FILENAME:
            hint = _support_file_hint(authoring, target_root, call)
            if hint:
                raise _RefusalError(refusal.code, f"{refusal.message}\n{hint}") from None
        raise
    replacement = found[0]
    where = f"{file_path} of Skill '{call.name}'"
    if unchanged[0]:
        return result, (
            f"{where} already reads as new_string at line {replacement.first_changed_line}; "
            "nothing changed."
        )
    if replacement.replacements > 1:
        return result, f"Replaced {replacement.replacements} occurrences in {where}."
    return result, f"Patched {where} at line {replacement.first_changed_line}."


def _replace(current: str, call: _Call, file_path: str) -> tuple[FuzzyReplacement, list[str]]:
    old = _lf(call.old_string or "")
    new = _lf(call.new_string or "")
    notes: list[str] = []
    copied = False
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
    if found is None and not call.replace_all:
        # Old text copied with errors; not when new text shows the edit already made.
        present = new.strip() and new != old and _find(current, new, new, True) is not None
        found = None if present else replace_copied(current, old, new)
        if isinstance(found, FuzzyReplacement):
            notes.extend(copy_warnings(found))
            return found, notes
        copied = True
    if found is None:
        raise _RefusalError(
            "text_not_found", _not_found_message(current, old, call.name, file_path)
        )
    if isinstance(found, AmbiguousFuzzyMatch):
        lines = ", ".join(str(line) for line in dict.fromkeys(found.line_numbers))
        where = f"{file_path} of Skill '{call.name}' (lines {lines}); nothing changed."
        raise _RefusalError(
            "ambiguous_match",
            f"old_string does not match exactly and resembles {found.occurrences} places in "
            f"{where} Copy the current text of the one to change into old_string, with enough "
            "surrounding text to tell it apart."
            if copied
            else f"old_string matches {found.occurrences} places in {where} Include more "
            "surrounding text so it matches once, or set replace_all to true to change every "
            "occurrence.",
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
    return replace_fuzzy(current, old, new, replace_all=replace_all, typographic=True)


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
        # Path.walk never enters a link, Windows junctions included.
        walk = (package / directory).walk()
        for path in sorted(base / name for base, _, names in walk for name in names):
            if not path.is_file():
                continue
            relative = path.relative_to(package).as_posix()
            try:
                text = authoring.read_text(target_root, call.name, relative)
            except SkillAuthoringError, OSError:
                continue
            if _find(text, old, old, False) is not None:
                matches.append(relative)
    if len(matches) != 1:
        return ""
    return f'The text is in {matches[0]}; to patch it there, add "file_path": "{matches[0]}".'


def _unknown_skill_message(name: str, root: Path, *, own: bool = True) -> str:
    names = sorted(scan_skill_names(root))
    if not own:
        lead = f"There is no global Skill named '{name}'; nothing changed."
        suggestions = similar_skill_names(name, names)
        if suggestions:
            quoted = ", ".join(f"'{candidate}'" for candidate in suggestions)
            return f"{lead} Did you mean {quoted}?"
        return lead
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


def _scope_rejection_message(name: str, scope: str, action: str = "delete") -> str:
    if scope == "shared":
        if action == "publish":
            return f"Skill '{name}' is shared with you; only its owner can make it global."
        return (
            f"Skill '{name}' is shared with you — only its owner or the user can "
            f"delete it. Edits still go through skill_manage."
        )
    if scope == "global":
        if action == "publish":
            return f"Skill '{name}' is already a global Skill; nothing changed."
        return (
            f"Skill '{name}' is a global Skill, which only the user can delete in the Skill "
            "controls; nothing changed."
        )
    if scope == "external":
        return (
            f"Skill '{name}' comes from a skill folder or an Extension and is read-only; "
            "nothing changed. Tell the user which change it needs; do not edit its files "
            "with file or shell Tools."
        )
    labels = {
        "bundled": "is a bundled Skill — read-only here.",
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
    on_changed: Callable[[], None] | None = None,
    run_started_at: Callable[[str], str | None] | None = None,
    follow_merge: SkillMergeFollower | None = None,
    resolve_global_skills_dir: Callable[[], Path] | None = None,
    publish_skill: Callable[[str, str, SkillWriter], SkillWriteResult] | None = None,
    refresh_global_skills: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Register identity-only direct Skill management.

    ``refresh_global_skills()`` reloads the global Skills on the Event Loop
    after a call changed the global home (a global edit or a publish).

    ``run_started_at(run_id)`` returns when a Run started (``None`` when
    unknown). It reads Event Loop state, so it runs on the Loop, before the
    write moves to a worker.

    ``follow_merge(owner_id, name, target, delete)`` runs every delete. With
    ``absorbed_into`` as ``target`` it calls ``delete(followed)`` with what
    names the Skill (its shares, the automations that trigger it), and once that
    delete succeeded moves each of them to ``target``, returning those it could
    not move. Without, ``target`` is ``None``: nothing moves and it returns what
    still names the Skill. The result then notes them.
    """
    handler = make_skill_manage_handler(
        authoring,
        resolve_agent_skills_dir,
        invalidate_agent_skills,
        resolve_shared_skills_dir,
        resolve_external_skill_scope,
        on_changed=on_changed,
        resolve_global_skills_dir=resolve_global_skills_dir,
        publish_skill=publish_skill,
    )

    def guarded_handler(
        context: ToolContext,
        arguments: JsonObject,
        started: str | None,
        followed: tuple[SkillReference, ...],
        global_changes: list[bool],
    ) -> JsonObject:
        with lifecycle_guard():
            outcome = handler(context, arguments, started, followed)
        global_changes.append(outcome.global_changed)
        return outcome.result

    async def offloaded_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        global_changes: list[bool] = []
        result = await dispatch(context, arguments, global_changes)
        if any(global_changes) and refresh_global_skills is not None:
            await refresh_global_skills()
        return result

    async def dispatch(
        context: ToolContext, arguments: JsonObject, global_changes: list[bool]
    ) -> JsonObject:
        started = None
        if run_started_at is not None and context.run_kind is RunKind.LIBRARIAN:
            started = run_started_at(context.run_id)
        merge = _merge(arguments)
        if follow_merge is None or merge is None:
            return await run_tool_worker(
                guarded_handler, context, arguments, started, (), global_changes
            )
        name, target = merge
        results: list[JsonObject] = []
        planned: list[SkillReference] = []

        async def delete(followed: tuple[SkillReference, ...]) -> bool:
            result = await run_tool_worker(
                guarded_handler, context, arguments, started, followed, global_changes
            )
            results.append(result)
            planned.extend(followed)
            return result.get("ok") is True

        failed = await follow_merge(context.skill_subject_id, name, target, delete)
        result = results[0]
        if result.get("ok") is True and target is None and failed:
            _note_orphaned(result, name, failed)
        elif result.get("ok") is True and planned:
            _note_followed(result, name, target or name, planned, failed)
        return result

    registry.register(
        SKILL_MANAGE_TOOL_NAME,
        SKILL_MANAGE_TOOL_DESCRIPTION,
        SKILL_MANAGE_TOOL_PARAMETERS,
        offloaded_handler,
        family="skills",
        constraints=("identity_agent",),
        open_input_schema=True,
        unadvertised_parameters=_UNADVERTISED_PARAMETERS,
        argument_normalizer=_normalize_skill_manage_arguments,
        result_schema={"type": "object", "required": ["content"]},
        display=ToolDisplay(
            parts_builder=_skill_manage_display_parts,
            hidden_argument_keys=("content", "old_string", "new_string"),
            details=True,
        ),
    )


def _merge(arguments: JsonObject) -> tuple[str, str | None] | None:
    """The Skill name and the absorbing Skill (``None`` without one) of a delete.

    ``None`` for another action, and for a delete the handler refuses anyway
    because ``absorbed_into`` names the Skill itself.
    """
    name, target = arguments.get("name"), arguments.get("absorbed_into")
    if arguments.get("action") != "delete" or not isinstance(name, str):
        return None
    if not isinstance(target, str) or not target:
        return name, None
    if target == name:
        return None
    return name, target


def _note_orphaned(result: JsonObject, name: str, references: tuple[SkillReference, ...]) -> None:
    """Add what still names the deleted Skill ``name`` (a warning)."""
    items = "; ".join(_reference_label(reference) for reference in references)
    warning = SKILL_MANAGE_ORPHANED_WARNING.format(name=name, items=items)
    result["data"]["content"] = "\n".join((result["data"]["content"], f"Warning: {warning}"))


def _note_followed(
    result: JsonObject,
    name: str,
    target: str,
    planned: list[SkillReference],
    failed: tuple[SkillReference, ...],
) -> None:
    """Add what now uses ``target`` (a note), and what still names ``name`` (a warning)."""
    lines = [result["data"]["content"]]
    moved = [reference for reference in planned if reference not in failed]
    if moved:
        items = "; ".join(_reference_label(reference) for reference in moved)
        note = SKILL_MANAGE_FOLLOWED_NOTE.format(target=target, name=name, items=items)
        lines.append(f"Note: {note}")
    if failed:
        items = "; ".join(_reference_label(reference) for reference in failed)
        warning = SKILL_MANAGE_NOT_FOLLOWED_WARNING.format(name=name, target=target, items=items)
        lines.append(f"Warning: {warning}")
    result["data"]["content"] = "\n".join(lines)


def _reference_label(reference: SkillReference) -> str:
    return _REFERENCE_LABELS[reference.kind].format(name=reference.name)


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
