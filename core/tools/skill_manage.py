"""Direct authoring tool for an Identity Agent's own Skills and the global Skills."""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from core.runs import RunKind
from core.skills.authoring import (
    SkillAuthoringError,
    SkillAuthoringService,
    SkillMergeAccessError,
    SkillProtectedError,
    SkillReference,
    SkillWriter,
    SkillWriteResult,
)
from core.skills.skills import (
    SKILL_FILENAME,
    find_skill_package_dir,
    scan_skill_names,
)
from core.tools._skill_conventions import skill_manage_hints
from core.tools._skill_manage_calls import (
    _LISTED_NAME_LIMIT,
    _UNADVERTISED_PARAMETERS,
    _Call,
    _normalize_skill_manage_arguments,
    _read_call,
    _RefusalError,
)
from core.tools._skill_manage_calls import (
    SKILL_MANAGE_TOOL_PARAMETERS as SKILL_MANAGE_TOOL_PARAMETERS,
)
from core.tools._skill_manage_patch import _apply
from core.tools.availability import SKILL_MANAGE_TOOL_NAME
from core.tools.skill import (
    LIBRARIAN_SKILL_ACTOR,
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
    "agent": "Agent '{name}'",
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
# Permission Rules that limit where the Agent can change files also keep it from
# changing Skills other Agents use: global Skills and Skills of other Agents.
_RULES_LIMIT_FILES = (
    "because they limit where you can change files. If the task needs this change, tell the user."
)
SKILL_MANAGE_NOT_OWN_REFUSAL = (
    f"Skill '{{name}}' is not one of your own Skills; nothing changed. The permission rules "
    f"let you change only your own Skills, {_RULES_LIMIT_FILES}"
)
SKILL_MANAGE_PUBLISH_LIMITED_REFUSAL = (
    f"Skill '{{name}}' stays one of your own Skills; nothing changed. The permission rules "
    f"keep you from making Skills global, {_RULES_LIMIT_FILES}"
)
SKILL_MANAGE_GLOBAL_CREATE = (
    "create adds one of your own Skills; nothing changed. Omit scope to create it, and use "
    "action publish to make one of your own Skills global."
)
SKILL_MANAGE_ABSORBED_INTO_SELF = (
    "absorbed_into names '{name}' itself; nothing changed. Name the other Skill that now holds "
    "its instructions."
)
SKILL_MANAGE_ABSORBED_INTO_UNKNOWN = (
    "absorbed_into names '{target}', which is not one of your own Skills; nothing changed. "
    "Name one of your own Skills that now holds the instructions of '{name}'."
)
SKILL_MANAGE_MERGE_ACCESS_REFUSAL = (
    "Nothing was changed. Skill '{target}' cannot replace '{name}' for {blocked}. "
    "Keep '{name}' and name the blocked merge in your reply so the user can resolve "
    "the Skill access."
)
_MERGE_ACCESS_REASONS = {
    "disabled": "the successor is disabled",
    "shadowed": "the name loads a different Skill",
    "unavailable": "the successor's requirements are not met",
}
# follow_merge(owner_id, name, target, delete) -> the references that could not move.
SkillMergeFollower = Callable[
    [str, str, str | None, Callable[[tuple[SkillReference, ...]], bool]],
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
# Actions whose written text gets authoring hints, and those that replace a whole file.
_HINTED_ACTIONS = frozenset({"create", "edit", "patch", "write_file"})
_WHOLE_FILE_ACTIONS = frozenset({"create", "edit", "write_file"})
# Support files the hints read: the directories SKILL.md points into, bounded.
_HINT_DIRECTORIES = ("references", "scripts")
_HINT_FILE_LIMIT = 200
_HINT_TEXT_BYTES = 256 * 1024


def make_skill_manage_handler(
    authoring: SkillAuthoringService,
    resolve_agent_skills_dir: Callable[[str], Path],
    invalidate_agent_skills: Callable[[str | None], None],
    resolve_shared_skills_dir: Callable[[str, str], Path | None] | None = None,
    resolve_external_skill_scope: (Callable[[str, str, str | None], str | None] | None) = None,
    *,
    on_changed: Callable[[], None] | None = None,
    resolve_subject_created_at: Callable[[str], str | None] | None = None,
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

    ``resolve_subject_created_at(agent_id)`` captures the Skill subject's current
    Agent identity for history attribution, including writes to another home.

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
        if resolve_subject_created_at is not None:
            writer = replace(writer, subject_created_at=resolve_subject_created_at(owner_id))
        try:
            call = _read_call(arguments)
            own_root = resolve_agent_skills_dir(owner_id)
            global_root = (
                resolve_global_skills_dir() if resolve_global_skills_dir is not None else None
            )
            target_root, target = own_root, "own"
            visible_scope = (
                resolve_external_skill_scope(owner_id, call.name, context.skill_project_id)
                if call.action != "create" and resolve_external_skill_scope is not None
                else None
            )
            if call.scope == "global" and call.action != "publish":
                target_root, target = _global_target(call, writer, global_root), "global"
            elif call.action in _SHARED_TARGET_ACTIONS and (
                find_skill_package_dir(own_root, call.name) is None
                or visible_scope not in (None, "own")
            ):
                scope = visible_scope if resolve_external_skill_scope is not None else "shared"
                shared_root = (
                    resolve_shared_skills_dir(owner_id, call.name)
                    if scope == "shared" and resolve_shared_skills_dir is not None
                    else None
                )
                if shared_root is not None:
                    target_root, target = shared_root, "shared"
            # A missing target package on a mutate/delete action is not always an
            # unknown name: it may be a Skill the agent can see in another scope.
            # A global one an attended Agent changes; for the others report the
            # scope instead of a bare not-found. ``create`` checks for a visible
            # name collision under the authoring lock below.
            if call.action != "create" and (
                find_skill_package_dir(target_root, call.name) is None
                or target == "own"
                and visible_scope not in (None, "own")
            ):
                if target == "global":
                    raise _RefusalError(
                        "skill_not_found", _unknown_skill_message(call.name, target_root, own=False)
                    )
                scope = visible_scope
                if scope == "global" and call.action in _SHARED_TARGET_ACTIONS:
                    target_root, target = _global_target(call, writer, global_root), "global"
                elif scope not in (None, "own"):
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
            if context.permissions.limits_writes and (target != "own" or call.action == "publish"):
                refusal = (
                    SKILL_MANAGE_PUBLISH_LIMITED_REFUSAL
                    if call.action == "publish"
                    else SKILL_MANAGE_NOT_OWN_REFUSAL
                )
                raise _RefusalError("permission_denied", refusal.format(name=call.name))
            # Checks and write run under one Skill write lock, so a change that
            # lands after a check cannot be overwritten by this write.
            hints: list[str] = []
            with authoring.exclusive():
                if call.action == "create" and resolve_external_skill_scope is not None:
                    scope = resolve_external_skill_scope(
                        owner_id, call.name, context.skill_project_id
                    )
                    if scope not in (None, "own"):
                        raise _RefusalError(
                            "skill_write_rejected",
                            _scope_rejection_message(call.name, scope, call.action),
                        )
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
    if action == "create":
        labels = {
            "shared": "a Skill shared with you",
            "global": "a global Skill",
            "bundled": "a bundled Skill",
            "project": "a Project Skill",
            "external": "a Skill from a configured folder or an Extension",
        }
        return (
            f"Nothing was changed. Skill '{name}' already exists as "
            f"{labels.get(scope, 'a Skill outside your own Skills')}. "
            "Use that Skill instead of creating a private copy."
        )
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
    resolve_subject_created_at: Callable[[str], str | None] | None = None,
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
        resolve_subject_created_at=resolve_subject_created_at,
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

        def delete(followed: tuple[SkillReference, ...]) -> bool:
            result = guarded_handler(context, arguments, started, followed, global_changes)
            results.append(result)
            planned.extend(followed)
            return result.get("ok") is True

        try:
            failed = await follow_merge(context.skill_subject_id, name, target, delete)
        except SkillMergeAccessError as error:
            blocked = "; ".join(
                f"Agent '{agent}'"
                + (f" in Project '{project}'" if project is not None else "")
                + f" ({_MERGE_ACCESS_REASONS[reason]})"
                for agent, project, reason in error.blocked
            )
            return tool_failure(
                "skill_merge_blocked",
                SKILL_MANAGE_MERGE_ACCESS_REFUSAL.format(name=name, target=target, blocked=blocked),
            )
        result = results[0]
        if result.get("ok") is True and target is None and failed:
            _note_orphaned(result, name, failed)
        elif result.get("ok") is True and (planned or failed):
            _note_followed(result, name, target or name, planned, failed)
        return result

    registry.register(
        SKILL_MANAGE_TOOL_NAME,
        SKILL_MANAGE_TOOL_DESCRIPTION,
        SKILL_MANAGE_TOOL_PARAMETERS,
        offloaded_handler,
        summary="Create, change or delete Skills.",
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
