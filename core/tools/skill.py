"""Internal skill activation tool."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from difflib import SequenceMatcher
from functools import partial
from typing import Any

from core.projects import ProjectNotFoundError
from core.runs import RunKind, is_unattended_run_kind
from core.skills.authoring import ArchivedSkill, SkillActor, SkillWriter
from core.skills.requirements import environment_requirement_names
from core.skills.skills import (
    SKILL_ORIGIN_AGENT,
    SkillCatalogEntry,
    SkillRegistry,
    format_skill_catalog_entries,
)
from core.tools._read_text import ReadPosition
from core.tools._skill_reading import (
    _ARCHIVE_REASONS,
    _SKILL_NAME_MARKS,
    SKILL_ABSORBED_FILE_MESSAGE,
    SKILL_ABSORBED_NOTE,
    SKILL_ARCHIVED_MESSAGE,
    SKILL_READ_ONLY_MARKS,
    SKILL_READ_ONLY_NOTE,
    SKILL_STATUS_ALREADY_ACTIVE,
    SKILL_STATUS_CONTINUED,
    SKILL_STATUS_FILE_LOADED,
    SKILL_STATUS_LOADED,
    _already_active_result,
    _continued_skill_result,
    _load_skill_content_with_env,
    _loaded_skill_file_result,
    _loaded_skill_result,
    _skill_read_failure,
)
from core.tools._skill_reading import (
    SKILL_BASE_DIR_MARKER as SKILL_BASE_DIR_MARKER,
)
from core.tools._skill_reading import (
    SKILL_PARTIAL_INSTRUCTIONS_NOTE as SKILL_PARTIAL_INSTRUCTIONS_NOTE,
)
from core.tools._skill_reading import SKILL_RESOURCE_FILES_GUIDANCE as SKILL_RESOURCE_FILES_GUIDANCE
from core.tools._skill_reading import SKILL_TOOL_DESCRIPTION as SKILL_TOOL_DESCRIPTION
from core.tools._skill_reading import SKILL_TOOL_NAME as SKILL_TOOL_NAME
from core.tools._skill_reading import clean_skill_file_path as clean_skill_file_path
from core.tools._skill_reading import load_skill_content as load_skill_content
from core.tools._skill_reading import load_skill_file as load_skill_file
from core.tools._skill_reading import load_skill_instructions_page as load_skill_instructions_page
from core.tools._skill_reading import skill_name_key as skill_name_key
from core.tools.arguments import optional_int
from core.tools.call_syntax import normalize_call_arguments
from core.tools.contracts import compile_tool_contract
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayField,
    ToolRegistry,
    display_notice,
    display_text,
    result_count_fact_builder,
    run_tool_worker,
    tool_failure,
    tool_success,
)

# Resolves the skill registry a call should use from its run's effective skill
# project (``None`` → the global/identity registry) and its identity agent
# (``None`` for a config-agent run — private skills are identity-only). The
# runtime wires this to ``Runtime.skills_for`` so the ``skill`` tool activates
# project skills in a project run, an identity agent's own private skills for
# their owner, and global skills everywhere else, without re-registering per run.
SkillRegistryResolver = Callable[[str | None, str | None], SkillRegistry]

# Rescans skills from disk and drops the cached per-run registries so the next
# resolve rebuilds against the fresh pool (the runtime wires this to
# ``Runtime.reload_skills``). Invoked once on a name miss so a skill hand-dropped
# into a skill directory after this run's registry was cached is picked up without
# a restart — see the rescan-on-miss retry in the handler below.
SkillRefresh = Callable[[], None | Awaitable[None]]

# Finds the newest archived Skill of a name in the writable homes a call sees: the
# identity Agent's own home (``None`` for other runs) and the global home. The
# runtime wires this to ``Runtime.archived_skill``.
ArchivedSkillResolver = Callable[[str | None, str], ArchivedSkill | None]

# Answers why a background Run of an Agent cannot change each of the named Skills
# it sees as its own (reason ``pinned`` or ``unknown``); the runtime wires this to
# ``Runtime.background_skill_protection``.
BackgroundProtectionResolver = Callable[[str, list[str]], Mapping[str, str]]

# The Skill history actors of background writers: the Librarian, and a Reflection
# in every other Run without the user.
LIBRARIAN_SKILL_ACTOR: SkillActor = "librarian"
REFLECTION_SKILL_ACTOR: SkillActor = "reflection"


def skill_writer(context: ToolContext) -> SkillWriter:
    """Name who changes Skills in this call; its history revisions record it.

    The Librarian writes in its passes and in every Session bound to the Agent whose
    Skills it maintains, also while the user talks with it there; a Reflection in
    every other Run without the user; the calling Agent otherwise. The first two
    are background writers, which never change a pinned Skill.
    """
    kind = context.run_kind
    actor: SkillActor = "agent"
    if kind is RunKind.LIBRARIAN or context.skill_agent_id is not None:
        actor = LIBRARIAN_SKILL_ACTOR
    elif is_unattended_run_kind(kind):
        actor = REFLECTION_SKILL_ACTOR
    return SkillWriter(
        actor=actor,
        session_id=context.session_id or None,
        run_id=context.run_id or None,
        run_kind=None if kind is None else kind.value,
    )


_SKILL_NAME_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Skill to load or read. Omit to list available Skills; required when file_path is provided."
    ),
}
_SKILL_FILE_PATH_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Relative path of a UTF-8 file inside the Skill package, including 'SKILL.md'. "
        "Omit to load the named Skill."
    ),
}
_SKILL_OFFSET_PARAMETER: JsonObject = {
    "type": "integer",
    "minimum": 1,
    "description": (
        "1-based line to start at in the instructions or file_path. Omit to start at the beginning."
    ),
}
_SKILL_PROPERTIES: JsonObject = {
    "name": _SKILL_NAME_PARAMETER,
    "file_path": _SKILL_FILE_PATH_PARAMETER,
    "offset": _SKILL_OFFSET_PARAMETER,
}
SKILL_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": _SKILL_PROPERTIES,
    "required": [],
}


# Accepted but not offered: another harness passes a Skill's arguments this way,
# and ``character`` carries the second half of an ``offset`` such as "12:241",
# which continues a long line where a page cut it.
_SKILL_UNADVERTISED_PARAMETERS: JsonObject = {
    "args": {"type": "string"},
    "character": {"type": "integer", "minimum": 1},
}
_SKILL_FIELD_ALIASES = {
    "skill": "name",
    "skill_name": "name",
    "command": "name",
    "file_pth": "file_path",
    "path": "file_path",
    "file": "file_path",
}
_SKILL_ARGS_NOTE = "Skills take no arguments; apply the loaded instructions to them yourself."
_SUGGESTION_LIMIT = 3
_LISTED_NAME_LIMIT = 20
_POSITION = re.compile(r"\s*(\d+)(?::(\d+))?\s*")

_SKILL_RUNTIME_CONTRACT = compile_tool_contract(
    name=SKILL_TOOL_NAME,
    input_schema={
        **SKILL_TOOL_PARAMETERS,
        "properties": {**_SKILL_PROPERTIES, **_SKILL_UNADVERTISED_PARAMETERS},
    },
    require_closed_input=False,
)


def _normalize_skill_arguments(arguments: Any) -> Any:
    """Repair Skill addressing before the shared schema checks."""
    repaired = normalize_call_arguments(
        _SKILL_RUNTIME_CONTRACT,
        arguments,
        field_aliases=_SKILL_FIELD_ALIASES,
        empty_as_omitted=("name", "file_path", "args"),
    )
    if not isinstance(repaired, dict):
        return repaired
    if isinstance(repaired.get("name"), str):
        repaired["name"] = repaired["name"].strip()
        if not repaired["name"]:
            repaired.pop("name")
    path = repaired.get("file_path")
    if isinstance(path, str):
        repaired["file_path"] = clean_skill_file_path(path)
    offset = repaired.get("offset")
    if isinstance(offset, str) and (position := _POSITION.fullmatch(offset)) is not None:
        # A page continues a long line at ``offset="line:character"``.
        offset = repaired["offset"] = int(position[1])
        if position[2] is not None and "character" not in repaired:
            repaired["character"] = int(position[2])
    if offset == 0 and "character" not in repaired:
        # Line 0 can only mean the start.
        del repaired["offset"]
    return repaired


def similar_skill_names(name: str, names: Iterable[str]) -> list[str]:
    """Return up to three names the requested one probably means, best first.

    Suggestions only feed error messages; they never select a target.
    """
    key = skill_name_key(name)
    if not key:
        return []
    scored: list[tuple[float, str]] = []
    for candidate in names:
        other = skill_name_key(candidate)
        if other == key:
            score = 1.0
        elif len(key) == len(other) + 1 and other in (key[1:], key[:-1]):
            score = 0.99  # one stray character before or after the name
        elif min(len(key), len(other)) >= 3 and (key in other or other in key):
            score = 0.9
        else:
            score = SequenceMatcher(None, key, other).ratio()
            if score < 0.8:
                continue
        scored.append((-score, candidate))
    return [candidate for _, candidate in sorted(scored)[:_SUGGESTION_LIMIT]]


def _resolve_skill_address(requested: str, names: Collection[str]) -> tuple[str, str | None] | None:
    """Resolve a name that differs only in formatting, or a path to a Skill file.

    Accepts case and separator differences, invocation marks (``/name``) and
    package paths such as ``name/references/guide.md`` or an absolute
    ``.../name/SKILL.md``. Returns the Skill name and the file path named
    inside it, if any; ``None`` when no single Skill matches exactly.
    """
    if requested in names:
        return requested, None
    by_key: dict[str, list[str]] = {}
    for candidate in names:
        by_key.setdefault(skill_name_key(candidate), []).append(candidate)

    def exact(segment: str) -> str | None:
        if segment in names:
            return segment
        matches = by_key.get(skill_name_key(segment), [])
        return matches[0] if len(matches) == 1 else None

    text = clean_skill_file_path(requested).lstrip(_SKILL_NAME_MARKS)
    segments = [segment for segment in text.split("/") if segment]
    for index, segment in enumerate(segments):
        matched = exact(segment)
        if matched is None:
            continue
        rest = "/".join(segments[index + 1 :])
        return matched, (rest if rest and rest != "SKILL.md" else None)
    return None


def _missing_project_failure(project_id: str | None) -> JsonObject:
    """Refuse a call whose Session takes its Skills from a Project that is gone."""
    return tool_failure(
        "project_not_found",
        f"skill was not run: the Project {json.dumps(project_id)} that this Session's Skills "
        "come from does not exist. Tell the user that this Project is missing.",
        retryable=False,
    )


def make_skill_handler(
    resolve_registry: SkillRegistryResolver,
    refresh_skills: SkillRefresh,
    resolve_archived: ArchivedSkillResolver | None = None,
    resolve_protection: BackgroundProtectionResolver | None = None,
) -> Any:
    """Return a skill handler that resolves its registry per call from the run.

    ``resolve_registry`` maps a run's effective skill project (``None`` for identity)
    and agent to the skill registry to activate against, so a project run loads
    project skills, an agent loads its own private skills, and an identity run loads
    global skills through the same handler. ``refresh_skills`` rescans skills from
    disk; the handler calls it once on a name miss and re-resolves, so a skill
    dropped into a skill directory after this run's registry was cached activates by
    name without a restart.

    ``resolve_archived`` answers a remaining miss of a deleted (archived) name: a
    Skill merged into one this call may load loads that one with a note; any other
    archived name fails with when and why it was archived.

    ``resolve_protection`` marks, in the list a background writer (``skill_writer``)
    gets, each own Skill it cannot change, so it can plan before writing; loading
    any Skill such a writer cannot change opens with a read-only note.

    A call works on the Skills of ``ToolContext.skill_subject_id``: the calling
    Agent, or in a Librarian Session the Agent whose Skills it maintains.
    """

    async def skill_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        # Identity runs only (``project_id is None``): a config agent's
        # project-local slug must not resolve a same-named identity agent's
        # private skill home (those skills bypass the project whitelist as
        # always-allowed for their owner).
        identity_agent_id = context.skill_subject_id if context.project_id is None else None

        async def current_registry() -> SkillRegistry:
            registry: SkillRegistry = await run_tool_worker(
                resolve_registry,
                context.skill_project_id,
                identity_agent_id,
            )
            return registry

        try:
            skill_registry = await current_registry()
        except ProjectNotFoundError:
            return _missing_project_failure(context.skill_project_id)
        requested = arguments.get("name")
        file_path = arguments.get("file_path")
        notes = [_SKILL_ARGS_NOTE] if arguments.get("args") else []
        try:
            position = ReadPosition(
                optional_int(arguments.get("offset"), field_name="offset", minimum=1) or 1,
                optional_int(arguments.get("character"), field_name="character", minimum=1) or 1,
            )
        except ValueError as error:
            return tool_failure("invalid_arguments", str(error))
        protect = (
            partial(resolve_protection, identity_agent_id)
            if resolve_protection is not None
            and identity_agent_id is not None
            and skill_writer(context).background
            else None
        )
        if requested is None and file_path is None:
            return await run_tool_worker(
                _skill_catalog_result, skill_registry, context.allowed_skills, protect
            )
        if requested is None:
            # A package path such as ``name/references/guide.md`` names its Skill.
            requested, file_path = file_path, None
            if not isinstance(requested, str) or not _names_package_file(
                requested, skill_registry, context.allowed_skills
            ):
                return tool_failure(
                    "invalid_arguments",
                    "file_path needs the Skill's name: call skill with name and file_path.",
                )
        if not isinstance(requested, str) or not requested.strip():
            return tool_failure("invalid_arguments", "name must be a non-empty string")
        if file_path is not None and (not isinstance(file_path, str) or not file_path.strip()):
            return tool_failure("invalid_arguments", "file_path must be a non-empty string")

        located = _locate_skill(skill_registry, requested, context.allowed_skills)
        if located is None:
            # A miss may just mean the skill was hand-dropped into a skill directory
            # after this run's registry was cached. Rescan disk once and re-resolve
            # so "drop it in, then activate it by name" works without a restart. The
            # session-pinned prompt catalog is deliberately left untouched (no
            # availability note) - only activation is made live.
            if inspect.iscoroutinefunction(refresh_skills):
                await refresh_skills()
            else:
                refresh_result = await run_tool_worker(refresh_skills)
                if inspect.isawaitable(refresh_result):
                    await refresh_result
            try:
                skill_registry = await current_registry()
            except ProjectNotFoundError:
                return _missing_project_failure(context.skill_project_id)
            located = _locate_skill(skill_registry, requested, context.allowed_skills)
        if located is None and resolve_archived is not None:
            archived = await run_tool_worker(
                resolve_archived, identity_agent_id, requested.strip().lstrip(_SKILL_NAME_MARKS)
            )
            if archived is not None:
                # The Skill that holds the instructions now, after any later merges.
                target = archived.holder
                merged = (archived.holder_since or archived.archived_at)[:10]
                loadable = target is not None and _is_loadable(
                    skill_registry, target, context.allowed_skills
                )
                if target is not None and loadable and file_path is not None:
                    return tool_failure(
                        "skill_not_found",
                        SKILL_ABSORBED_FILE_MESSAGE.format(
                            name=archived.name, target=target, date=merged
                        ),
                    )
                if target is not None and loadable:
                    notes.append(
                        SKILL_ABSORBED_NOTE.format(name=archived.name, target=target, date=merged)
                    )
                    located = (target, None)
                elif archived.available:
                    return tool_failure(
                        "skill_not_found",
                        SKILL_ARCHIVED_MESSAGE.format(
                            name=archived.name,
                            reason=_archive_reason(archived),
                            date=archived.archived_at[:10],
                        ),
                    )
        if located is None:
            return tool_failure(
                "skill_not_found",
                _not_found_message(requested, skill_registry, context.allowed_skills),
            )
        skill_name, named_file = located
        if named_file is not None and file_path is not None and named_file != file_path:
            return tool_failure(
                "invalid_arguments",
                f"name points to file '{named_file}' but file_path is '{file_path}'; "
                f'call skill with name "{skill_name}" and the one file_path you mean.',
            )
        file_path = file_path or named_file
        skill = skill_registry.get(skill_name)

        if not _is_skill_allowed(skill_registry, skill_name, context.allowed_skills):
            return tool_failure(
                "skill_not_found",
                f"Skill '{skill_name}' is not allowed for this Agent. "
                "Call skill without arguments to list the available Skills.",
            )

        unavailable_message = _unavailable_skill_message(
            skill_registry,
            skill_name,
            context.allowed_skills,
        )
        if unavailable_message is not None:
            return tool_failure("skill_unavailable", unavailable_message)
        if protect is not None:
            mark = await run_tool_worker(_read_only_mark, skill, protect)
            if mark is not None:
                notes.insert(0, SKILL_READ_ONLY_NOTE.format(name=skill_name, mark=mark))

        if isinstance(file_path, str):
            try:
                data = await run_tool_worker(
                    load_skill_file, skill_name, skill.path, file_path, position
                )
            except OSError as error:
                return _skill_read_failure(skill_name, file_path, error)
            except ValueError as error:
                return tool_failure("skill_read_error", str(error))
            return _loaded_skill_file_result(
                skill_name,
                str(data["file_path"]),
                str(data["content"]),
                notes,
            )

        if position != ReadPosition(1):
            try:
                page = await run_tool_worker(
                    load_skill_instructions_page, skill_name, skill.path, position
                )
            except OSError as error:
                return _skill_read_failure(skill_name, "SKILL.md", error)
            except ValueError as error:
                return tool_failure("skill_read_error", str(error))
            return _continued_skill_result(skill_name, page, notes)

        try:
            data = await run_tool_worker(
                _load_skill_content_with_env,
                skill_name,
                skill.path,
                environment_requirement_names(skill.requirements),
            )
        except OSError as error:
            return _skill_read_failure(skill_name, "SKILL.md", error)
        except ValueError as error:
            return tool_failure("skill_read_error", str(error))

        content = data.get("content")
        activation_content = data.get("activation_content")
        if (
            not isinstance(content, str)
            or not content
            or not isinstance(activation_content, str)
            or not activation_content
        ):
            return tool_failure(
                "skill_read_error",
                f"Skill '{skill_name}' has no instructions. "
                "Add instructions to SKILL.md before loading it.",
            )
        newly_activated = context.activate_skill(skill_name, activation_content, skill.package_ref)
        if newly_activated is False:
            return _already_active_result(skill_name, notes)
        return _loaded_skill_result(skill_name, data, notes)

    return skill_handler


def register_skill_tool(
    registry: ToolRegistry,
    resolve_registry: SkillRegistryResolver,
    refresh_skills: SkillRefresh,
    resolve_archived: ArchivedSkillResolver | None = None,
    resolve_protection: BackgroundProtectionResolver | None = None,
) -> None:
    """Register the skill activation tool with a per-project registry resolver.

    A normal allow-list tool: an agent offers it only when ``skill`` is in its allowed
    tools, so it can be toggled per agent like any other tool. It is **not** gated on the
    agent currently having a loadable skill — a skill can be authored or activated
    mid-session, so the loader stays available whenever the tool itself is allowed.
    ``refresh_skills`` rescans skills from disk on a name miss so a hand-dropped skill
    is activatable by name without a restart.
    """
    registry.register(
        SKILL_TOOL_NAME,
        SKILL_TOOL_DESCRIPTION,
        SKILL_TOOL_PARAMETERS,
        make_skill_handler(resolve_registry, refresh_skills, resolve_archived, resolve_protection),
        summary="Load a Skill or read its files.",
        argument_normalizer=_normalize_skill_arguments,
        unadvertised_parameters=_SKILL_UNADVERTISED_PARAMETERS,
        family="skills",
        result_schema={"type": "object"},
        display=ToolDisplay(
            primary_candidates=(
                ToolDisplayField("name"),
                ToolDisplayField(
                    "file_path",
                    kind="path",
                    truncate="start",
                    tooltip="always",
                    copyable=True,
                ),
            ),
            fact_builder=result_count_fact_builder("count"),
            detail_builder=_skill_detail_blocks,
        ),
        open_input_schema=True,
    )


def _skill_detail_blocks(arguments: JsonObject, result: JsonObject | None) -> list[JsonObject]:
    """Show the user the Skill's instructions, the file read, or the catalog.

    Resource listings, activation guidance and notes are for the Agent and stay
    in the raw result.
    """
    data = result.get("data") if isinstance(result, dict) and result.get("ok") is True else None
    if not isinstance(data, dict):
        return []
    status = data.get("status")
    if status == SKILL_STATUS_ALREADY_ACTIVE:
        return [display_notice("info", "The Skill was already active; it was not loaded again.")]
    label = (
        "content"
        if status in {SKILL_STATUS_LOADED, SKILL_STATUS_FILE_LOADED, SKILL_STATUS_CONTINUED}
        else "results"
    )
    blocks = [display_text(label, source="result", path=("data", "content"))]
    if data.get("environment_access"):
        blocks.append(
            display_notice(
                "info",
                "This Skill makes additional environment credentials available to shell commands.",
            )
        )
    return blocks


def _skill_catalog_result(
    skill_registry: SkillRegistry,
    allowed_skills: Sequence[str] | None,
    resolve_protection: Callable[[list[str]], Mapping[str, str]] | None = None,
) -> JsonObject:
    """Return the currently available Skills grouped by origin, like the catalog.

    With ``resolve_protection``, own Skills the Run cannot change are marked.
    """
    allowed = ["*"] if allowed_skills is None else list(allowed_skills)
    skills = skill_registry.filter_allowed(allowed)
    marks: dict[str, str] = {}
    if resolve_protection is not None:
        own = [skill.name for skill in skills if skill.origin == SKILL_ORIGIN_AGENT]
        marks = {
            name: SKILL_READ_ONLY_MARKS[reason]
            for name, reason in resolve_protection(own).items()
            if reason in SKILL_READ_ONLY_MARKS
        }
    content = (
        format_skill_catalog_entries(skills, marks=marks) if skills else "No Skills are available."
    )
    return tool_success({"count": len(skills), "content": content})


def _read_only_mark(
    skill: SkillCatalogEntry, resolve_protection: Callable[[list[str]], Mapping[str, str]]
) -> str | None:
    """Return why a background Run cannot change *skill*, or ``None`` when it can."""
    if skill.origin != SKILL_ORIGIN_AGENT:
        return "read-only here"
    return SKILL_READ_ONLY_MARKS.get(resolve_protection([skill.name]).get(skill.name, ""))


def _allowed_skill_names(
    skill_registry: SkillRegistry,
    allowed_skills: Sequence[str] | None,
) -> set[str]:
    allowed = ["*"] if allowed_skills is None else list(allowed_skills)
    return {skill.name for skill in skill_registry.filter_allowed(allowed)}


def _addressable_names(
    skill_registry: SkillRegistry, allowed_skills: Sequence[str] | None
) -> list[str]:
    """Allowed Skill names, including Skills whose requirements are unmet."""
    return [
        skill.name
        for skill in skill_registry.list_all()
        if _is_skill_allowed(skill_registry, skill.name, allowed_skills)
    ]


def _locate_skill(
    skill_registry: SkillRegistry, requested: str, allowed_skills: Sequence[str] | None
) -> tuple[str, str | None] | None:
    try:
        skill_registry.get(requested)
    except KeyError:
        return _resolve_skill_address(requested, _addressable_names(skill_registry, allowed_skills))
    return requested, None


def _is_loadable(
    skill_registry: SkillRegistry, name: str, allowed_skills: Sequence[str] | None
) -> bool:
    try:
        skill_registry.get(name)
    except KeyError:
        return False
    return _is_skill_allowed(skill_registry, name, allowed_skills)


def _archive_reason(archived: ArchivedSkill) -> str:
    if archived.reason == "absorbed" and archived.absorbed_into:
        return f"merged into Skill '{archived.absorbed_into}'"
    return _ARCHIVE_REASONS.get(archived.reason or "", "deleted")


def _names_package_file(
    path: str, skill_registry: SkillRegistry, allowed_skills: Sequence[str] | None
) -> bool:
    located = _resolve_skill_address(path, _addressable_names(skill_registry, allowed_skills))
    return located is not None and located[1] is not None


def _not_found_message(
    requested: str, skill_registry: SkillRegistry, allowed_skills: Sequence[str] | None
) -> str:
    names = _addressable_names(skill_registry, allowed_skills)
    lead = f"Skill not found: {requested}."
    suggestions = similar_skill_names(requested, names)
    if len(suggestions) == 1:
        return f'{lead} Did you mean "{suggestions[0]}"? Load it with name "{suggestions[0]}".'
    if suggestions:
        quoted = ", ".join(f'"{name}"' for name in suggestions)
        return f"{lead} Did you mean one of: {quoted}?"
    if not names:
        return f"{lead} No Skills are available to you."
    if len(names) <= _LISTED_NAME_LIMIT:
        return f"{lead} Available Skills: {', '.join(names)}."
    return f"{lead} Call skill without arguments to list the available Skills."


def _is_skill_allowed(
    skill_registry: SkillRegistry,
    skill_name: str,
    allowed_skills: Sequence[str] | None,
) -> bool:
    is_allowed = getattr(skill_registry, "is_allowed", None)
    if callable(is_allowed):
        return bool(is_allowed(skill_name, allowed_skills))
    return skill_name in _allowed_skill_names(skill_registry, allowed_skills)


def _unavailable_skill_message(
    skill_registry: SkillRegistry,
    skill_name: str,
    allowed_skills: Sequence[str] | None,
) -> str | None:
    availability_for = getattr(skill_registry, "availability_for", None)
    if not callable(availability_for):
        return None

    availability = availability_for(skill_name, allowed_skills)
    if getattr(availability, "state", "available") == "available":
        return None
    missing = list(getattr(availability, "missing", ()))
    detail = "; ".join(missing) if missing else str(getattr(availability, "state", "unavailable"))
    return (
        f"Skill '{skill_name}' is unavailable: {detail}. "
        "Resolve these requirements before loading it again, or call skill without "
        "arguments to choose an available Skill."
    )


__all__ = [
    "SKILL_RESOURCE_FILES_GUIDANCE",
    "SKILL_TOOL_DESCRIPTION",
    "SKILL_TOOL_NAME",
    "SKILL_TOOL_PARAMETERS",
    "load_skill_file",
    "load_skill_instructions_page",
    "make_skill_handler",
    "load_skill_content",
    "register_skill_tool",
]
