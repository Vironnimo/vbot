"""Internal skill activation tool."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Collection, Iterable, Sequence
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import Any

from core.skills._packages import PackageError, excluded, package_path
from core.skills.requirements import environment_requirement_names
from core.skills.skill_validator import split_skill_document
from core.skills.skills import (
    SkillRegistry,
    _scan_skill_resources,
    format_skill_activation_context,
    format_skill_catalog_entries,
)
from core.tools._argument_repair import normalize_call_arguments
from core.tools.bash import format_bash_env_usage
from core.tools.contracts import compile_tool_contract
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayField,
    ToolRegistry,
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

SKILL_TOOL_NAME = "skill"
SKILL_TOOL_DESCRIPTION = "List available Skills, load one Skill, or read one file from it."
SKILL_STATUS_LOADED = "loaded"
SKILL_STATUS_ALREADY_ACTIVE = "already_active"
SKILL_STATUS_FILE_LOADED = "file_loaded"
# OpenClaw-compatible marker skill authors may use in the body to reference bundled
# files (e.g. ``python {baseDir}/scripts/run.py``); replaced with the absolute skill
# directory at activation time.
SKILL_BASE_DIR_MARKER = "{baseDir}"
SKILL_RESOURCE_FILES_GUIDANCE = (
    f"Files of this Skill. Run a scripts/ file by its absolute path with `{SHELL_MODEL_NAME}`; "
    "read another file with `skill` using this name and its relative file_path only "
    "when the instructions call for it."
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
_SKILL_PROPERTIES: JsonObject = {
    "name": _SKILL_NAME_PARAMETER,
    "file_path": _SKILL_FILE_PATH_PARAMETER,
}
SKILL_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": _SKILL_PROPERTIES,
    "required": [],
}


# Accepted but not offered: another harness passes a Skill's arguments this way.
_SKILL_UNADVERTISED_PARAMETERS: JsonObject = {"args": {"type": "string"}}
_SKILL_FIELD_ALIASES = {
    "skill": "name",
    "skill_name": "name",
    "command": "name",
    "file_pth": "file_path",
    "path": "file_path",
    "file": "file_path",
}
_SKILL_ARGS_NOTE = "Skills take no arguments; apply the loaded instructions to them yourself."
# Invocation marks a Model copies from a user's `/name` or `$name` Skill request.
_SKILL_NAME_MARKS = "/$@"
_SUGGESTION_LIMIT = 3
_LISTED_NAME_LIMIT = 20

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
    return repaired


def clean_skill_file_path(path: str) -> str:
    """Remove quoting, leading ``./`` and Windows separators from a package path."""
    text = path.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"', "`"):
        text = text[1:-1].strip()
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def skill_name_key(name: str) -> str:
    """Comparison key that ignores case, separators, quotes and invocation marks."""
    text = name.strip().strip("'\"`").lstrip(_SKILL_NAME_MARKS)
    return re.sub(r"[\s_-]+", "-", text.casefold()).strip("-")


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


def make_skill_handler(
    resolve_registry: SkillRegistryResolver, refresh_skills: SkillRefresh
) -> Any:
    """Return a skill handler that resolves its registry per call from the run.

    ``resolve_registry`` maps a run's effective skill project (``None`` for identity)
    and agent to the skill registry to activate against, so a project run loads
    project skills, an agent loads its own private skills, and an identity run loads
    global skills through the same handler. ``refresh_skills`` rescans skills from
    disk; the handler calls it once on a name miss and re-resolves, so a skill
    dropped into a skill directory after this run's registry was cached activates by
    name without a restart.
    """

    async def skill_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        # Identity runs only (``project_id is None``): a config agent's
        # project-local slug must not resolve a same-named identity agent's
        # private skill home (those skills bypass the project whitelist as
        # always-allowed for their owner).
        identity_agent_id = context.agent_id if context.project_id is None else None

        async def current_registry() -> SkillRegistry:
            registry: SkillRegistry = await run_tool_worker(
                resolve_registry,
                context.skill_project_id,
                identity_agent_id,
            )
            return registry

        skill_registry = await current_registry()
        requested = arguments.get("name")
        file_path = arguments.get("file_path")
        notes = [_SKILL_ARGS_NOTE] if arguments.get("args") else []
        if requested is None and file_path is None:
            return await run_tool_worker(
                _skill_catalog_result,
                skill_registry,
                context.allowed_skills,
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
            skill_registry = await current_registry()
            located = _locate_skill(skill_registry, requested, context.allowed_skills)
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
                f"Skill not found or not allowed for this agent: {skill_name}",
            )

        unavailable_message = _unavailable_skill_message(
            skill_registry,
            skill_name,
            context.allowed_skills,
        )
        if unavailable_message is not None:
            return tool_failure("skill_unavailable", unavailable_message)

        if isinstance(file_path, str):
            file_path = _package_relative_path(file_path, skill_name, skill.path.parent)
            try:
                data = await run_tool_worker(load_skill_file, skill_name, skill.path, file_path)
            except OSError as error:
                return tool_failure(
                    "skill_read_error",
                    f"Failed to read skill '{skill_name}' file '{file_path}': {error}",
                )
            except ValueError as error:
                return tool_failure("skill_read_error", str(error))
            return _loaded_skill_file_result(
                skill_name,
                str(data["file_path"]),
                str(data["content"]),
                notes,
            )

        try:
            data = await run_tool_worker(
                _load_skill_content_with_env,
                skill_name,
                skill.path,
                environment_requirement_names(skill.requirements),
            )
        except OSError as error:
            return tool_failure(
                "skill_read_error",
                f"Failed to read skill '{skill_name}': {error}",
            )
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
                f"Skill '{skill_name}' produced no loadable content.",
            )
        newly_activated = context.activate_skill(skill_name, activation_content)
        if newly_activated is False:
            return _already_active_result(skill_name, notes)
        return _loaded_skill_result(skill_name, data, notes)

    return skill_handler


def register_skill_tool(
    registry: ToolRegistry,
    resolve_registry: SkillRegistryResolver,
    refresh_skills: SkillRefresh,
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
        make_skill_handler(resolve_registry, refresh_skills),
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
        ),
        open_input_schema=True,
    )


def load_skill_content(
    skill_name: str,
    skill_file: Path,
    *,
    env_keys: Sequence[str] = (),
) -> JsonObject:
    """Load the instruction body and activation metadata for one Skill file."""
    body = _read_skill_body(skill_file)
    skill_directory = skill_file.resolve().parent
    directory = skill_directory.as_posix()
    body = body.replace(SKILL_BASE_DIR_MARKER, directory)
    resources = _scan_skill_resources(skill_directory)
    presented_resources = [_present_resource_path(resource, directory) for resource in resources]
    environment_access = ""
    if env_keys:
        environment_access = format_bash_env_usage(
            env_keys,
            intro=(
                "Loading this Skill makes these additional environment credentials "
                "available to shell commands."
            ),
        )
    activation_content = format_skill_activation_context(
        skill_name,
        body,
        resource_files=presented_resources,
        resource_guidance=SKILL_RESOURCE_FILES_GUIDANCE,
        environment_access=environment_access,
    )
    result: JsonObject = {
        "content": body,
        "activation_content": activation_content,
    }
    if presented_resources:
        result["resource_files"] = {
            "guidance": SKILL_RESOURCE_FILES_GUIDANCE,
            "files": presented_resources,
        }
    if environment_access:
        result["environment_access"] = environment_access
    return result


def _load_skill_content_with_env(
    skill_name: str,
    skill_file: Path,
    env_keys: Sequence[str],
) -> JsonObject:
    return load_skill_content(skill_name, skill_file, env_keys=env_keys)


def load_skill_file(skill_name: str, skill_file: Path, file_path: str) -> JsonObject:
    """Read one UTF-8 package file by skill-relative path."""
    try:
        normalized = package_path(file_path.replace("\\", "/"))
        if excluded(normalized):
            raise PackageError("Internal or generated package files are not Skill resources.")
    except PackageError as error:
        raise ValueError(f"Illegal file path for skill '{skill_name}': {file_path}") from error
    skill_directory = skill_file.resolve().parent
    candidate = skill_directory.joinpath(*PurePosixPath(normalized).parts).resolve()
    try:
        resolved_relative = candidate.relative_to(skill_directory).as_posix()
        if excluded(resolved_relative):
            raise ValueError("Internal or generated package files are not Skill resources.")
    except ValueError as error:
        raise ValueError(f"Illegal file path for skill '{skill_name}': {file_path}") from error
    if not candidate.is_file():
        raise ValueError(f"Skill '{skill_name}' file not found: {normalized}")
    try:
        content = candidate.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"Skill '{skill_name}' file is not UTF-8 text: {normalized}") from error
    return {"name": skill_name, "file_path": normalized, "content": content}


def _loaded_skill_result(skill_name: str, loaded: JsonObject, notes: list[str]) -> JsonObject:
    """Success envelope of a fresh activation — the tool result IS the content carrier.

    The raw SKILL.md instruction body rides in ``data.content`` while optional
    resource files and environment access stay in sibling fields. The sessions
    domain parses this envelope shape (``skill_tool_activation``) for dedup,
    statistics, and post-compaction re-injection — keep
    ``name``/``status``/``content`` stable.
    """
    data: JsonObject = {
        "name": skill_name,
        "status": SKILL_STATUS_LOADED,
        "content": loaded["content"],
    }
    resource_files = loaded.get("resource_files")
    if isinstance(resource_files, dict):
        data["resource_files"] = resource_files
    environment_access = loaded.get("environment_access")
    if isinstance(environment_access, str) and environment_access:
        data["environment_access"] = environment_access
    if notes:
        data["note"] = " ".join(notes)
    return tool_success(data)


def _loaded_skill_file_result(
    skill_name: str,
    file_path: str,
    content: str,
    notes: list[str],
) -> JsonObject:
    data: JsonObject = {
        "name": skill_name,
        "status": SKILL_STATUS_FILE_LOADED,
        "file_path": file_path,
    }
    if notes:
        data["note"] = " ".join(notes)
    data["content"] = content
    return tool_success(data)


def _already_active_result(skill_name: str, notes: list[str]) -> JsonObject:
    data: JsonObject = {
        "name": skill_name,
        "status": SKILL_STATUS_ALREADY_ACTIVE,
        "message": (
            f"Skill '{skill_name}' is already active in this session; "
            "its instructions are already in context."
        ),
    }
    if notes:
        data["note"] = " ".join(notes)
    return tool_success(data)


def _skill_catalog_result(
    skill_registry: SkillRegistry,
    allowed_skills: Sequence[str] | None,
) -> JsonObject:
    """Return the currently available Skills grouped by origin, like the catalog."""
    allowed = ["*"] if allowed_skills is None else list(allowed_skills)
    skills = skill_registry.filter_allowed(allowed)
    content = format_skill_catalog_entries(skills) if skills else "No Skills are available."
    return tool_success({"count": len(skills), "content": content})


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


def _package_relative_path(file_path: str, skill_name: str, skill_directory: Path) -> str:
    """Map an absolute path inside the package, or a ``name/...`` path, to its relative path."""
    directory = skill_directory.resolve()
    if PurePosixPath(file_path).is_absolute() or re.match(r"^[A-Za-z]:/", file_path):
        try:
            return Path(file_path).resolve().relative_to(directory).as_posix()
        except (OSError, ValueError):
            return file_path
    first, _, rest = file_path.partition("/")
    if (
        rest
        and skill_name_key(first) == skill_name_key(skill_name)
        and not (directory / file_path).is_file()
        and (directory / rest).is_file()
    ):
        return rest
    return file_path


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
    return f"Skill '{skill_name}' is unavailable: {detail}"


def _read_skill_body(skill_file: Path) -> str:
    content = skill_file.read_text(encoding="utf-8")
    _, body, _ = split_skill_document(content)
    return body.strip()


def _present_resource_path(resource: str, directory: str) -> str:
    if PurePosixPath(resource).parts[0] == "scripts":
        return f"{directory}/{resource}"
    return resource


__all__ = [
    "SKILL_RESOURCE_FILES_GUIDANCE",
    "SKILL_TOOL_DESCRIPTION",
    "SKILL_TOOL_NAME",
    "SKILL_TOOL_PARAMETERS",
    "load_skill_file",
    "make_skill_handler",
    "load_skill_content",
    "register_skill_tool",
]
