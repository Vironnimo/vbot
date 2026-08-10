"""Internal skill activation tool."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from html import escape
from pathlib import Path, PurePosixPath
from typing import Any

from core.skills.requirements import environment_requirement_names
from core.skills.skill_validator import split_skill_document
from core.skills.skills import (
    RESOURCE_DIRECTORIES,
    SKILL_FILENAME,
    SkillRegistry,
    _scan_skill_resources,
    skill_origin_sort_key,
)
from core.tools.bash import format_bash_env_usage
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
SKILL_TOOL_DESCRIPTION = (
    "Activate one Skill by its required name, or read one UTF-8 file by Skill-relative "
    "path. Omit file_path to load SKILL.md instructions; provide file_path to read that "
    "package file without activation. Activation lists references and assets as "
    "Skill-relative paths and scripts as absolute paths for direct execution."
)
SKILL_LIST_TOOL_NAME = "skill_list"
SKILL_LIST_TOOL_DESCRIPTION = (
    "List the currently available Skills grouped by origin. Call it with no arguments "
    "before choosing a Skill to inspect."
)
SKILL_STATUS_LOADED = "loaded"
SKILL_STATUS_ALREADY_ACTIVE = "already_active"
SKILL_STATUS_FILE_LOADED = "file_loaded"
# OpenClaw-compatible marker skill authors may use in the body to reference bundled
# files (e.g. ``python {baseDir}/scripts/run.py``); replaced with the absolute skill
# directory at activation time.
SKILL_BASE_DIR_MARKER = "{baseDir}"
SKILL_PATH_RESOLUTION_NOTE = (
    "Scripts are listed with absolute paths for direct bash execution. Read relative "
    "references and assets with the skill tool using this skill name and file_path."
)
_SKILL_NAME_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "pattern": r"\S",
    "description": "Exact Skill name. Required for activate and read.",
}
_SKILL_FILE_PATH_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "pattern": r"^(SKILL\.md|(?:scripts|references|assets)/.+)$",
    "description": (
        "Path relative to the named Skill, such as 'references/api.md'. "
        "When present, returns that UTF-8 file without activating the Skill."
    ),
}
SKILL_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "description": (
        "name is required. Omit file_path to activate the Skill; provide file_path to "
        "read one package file without activation."
    ),
    "properties": {
        "name": _SKILL_NAME_PARAMETER,
        "file_path": _SKILL_FILE_PATH_PARAMETER,
    },
    "required": ["name"],
}
SKILL_LIST_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {},
    "required": [],
}


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
        unknown_arguments = set(arguments) - {"name", "file_path"}
        if unknown_arguments:
            names = ", ".join(sorted(unknown_arguments))
            return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

        # Identity runs only (``project_id is None``): a config agent's
        # project-local slug must not resolve a same-named identity agent's
        # private skill home (those skills bypass the project whitelist as
        # always-allowed for their owner).
        identity_agent_id = context.agent_id if context.project_id is None else None
        skill_registry = await run_tool_worker(
            resolve_registry,
            context.skill_project_id,
            identity_agent_id,
        )

        skill_name = arguments.get("name")
        file_path = arguments.get("file_path")
        if not isinstance(skill_name, str) or not skill_name.strip():
            return tool_failure("invalid_arguments", "name must be a non-empty string")
        if file_path is not None and (not isinstance(file_path, str) or not file_path.strip()):
            return tool_failure(
                "invalid_arguments",
                "file_path must be a non-empty string",
            )

        try:
            skill = skill_registry.get(skill_name)
        except KeyError:
            # A miss may just mean the skill was hand-dropped into a skill directory
            # after this run's registry was cached. Rescan disk once and re-resolve
            # so "drop it in, then activate it by name" works without a restart. The
            # session-pinned prompt catalog is deliberately left untouched (no
            # availability note) — only activation is made live.
            if inspect.iscoroutinefunction(refresh_skills):
                await refresh_skills()
            else:
                refresh_result = await run_tool_worker(refresh_skills)
                if inspect.isawaitable(refresh_result):
                    await refresh_result
            skill_registry = await run_tool_worker(
                resolve_registry,
                context.skill_project_id,
                identity_agent_id,
            )
            try:
                skill = skill_registry.get(skill_name)
            except KeyError:
                return tool_failure("skill_not_found", f"Skill not found: {skill_name}")

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
        if not isinstance(content, str) or not content:
            return tool_failure(
                "skill_read_error",
                f"Skill '{skill_name}' produced no loadable content.",
            )
        newly_activated = context.activate_skill(skill_name, content)
        if newly_activated is False:
            return _already_active_result(skill_name)
        return _loaded_skill_result(skill_name, content)

    return skill_handler


def make_skill_list_handler(resolve_registry: SkillRegistryResolver) -> Any:
    """Return the Skill catalog handler."""

    async def skill_list_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        if arguments:
            names = ", ".join(sorted(arguments))
            return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")
        identity_agent_id = context.agent_id if context.project_id is None else None
        skill_registry = await run_tool_worker(
            resolve_registry,
            context.skill_project_id,
            identity_agent_id,
        )
        return await run_tool_worker(
            _skill_list_result,
            skill_registry,
            context.allowed_skills,
        )

    return skill_list_handler


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
            )
        ),
        open_input_schema=True,
    )
    registry.register(
        SKILL_LIST_TOOL_NAME,
        SKILL_LIST_TOOL_DESCRIPTION,
        SKILL_LIST_TOOL_PARAMETERS,
        make_skill_list_handler(resolve_registry),
        result_schema={"type": "object"},
        session_scoped=True,
        open_input_schema=True,
        display=ToolDisplay(fact_builder=result_count_fact_builder("count")),
    )


def load_skill_content(
    skill_name: str,
    skill_file: Path,
    *,
    env_keys: Sequence[str] = (),
) -> JsonObject:
    """Load and wrap activation content for one skill file."""
    body = _read_skill_body(skill_file)
    skill_directory = skill_file.resolve().parent
    directory = skill_directory.as_posix()
    body = body.replace(SKILL_BASE_DIR_MARKER, directory)
    resources = _scan_skill_resources(skill_directory)
    return {
        "content": _wrap_skill_content(
            skill_name,
            body,
            resources,
            directory,
            env_keys=env_keys,
        ),
        "resources": resources,
        "directory": directory,
    }


def _load_skill_content_with_env(
    skill_name: str,
    skill_file: Path,
    env_keys: Sequence[str],
) -> JsonObject:
    return load_skill_content(skill_name, skill_file, env_keys=env_keys)


def load_skill_file(skill_name: str, skill_file: Path, file_path: str) -> JsonObject:
    """Read one UTF-8 package file by skill-relative path."""
    normalized = _normalized_skill_file_path(file_path)
    skill_directory = skill_file.resolve().parent
    candidate = skill_directory.joinpath(*PurePosixPath(normalized).parts).resolve()
    try:
        candidate.relative_to(skill_directory)
    except ValueError as error:
        raise ValueError(f"Illegal file path for skill '{skill_name}': {file_path}") from error
    if not candidate.is_file():
        raise ValueError(f"Skill '{skill_name}' file not found: {normalized}")
    try:
        content = candidate.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"Skill '{skill_name}' file is not UTF-8 text: {normalized}") from error
    return {"name": skill_name, "file_path": normalized, "content": content}


def _loaded_skill_result(skill_name: str, content: str) -> JsonObject:
    """Success envelope of a fresh activation — the tool result IS the content carrier.

    The full wrapped ``<skill_content>`` rides in ``data.content``, so it sits in
    the conversation exactly where the load happened and replays verbatim like
    any other tool result. The sessions domain parses this envelope shape
    (``skill_tool_activation``) for dedup, statistics, and the post-compaction
    re-injection — keep ``name``/``status``/``content`` stable.
    """
    return tool_success(
        {
            "name": skill_name,
            "status": SKILL_STATUS_LOADED,
            "content": content,
        }
    )


def _loaded_skill_file_result(
    skill_name: str,
    file_path: str,
    content: str,
) -> JsonObject:
    return tool_success(
        {
            "name": skill_name,
            "status": SKILL_STATUS_FILE_LOADED,
            "file_path": file_path,
            "content": content,
        }
    )


def _already_active_result(skill_name: str) -> JsonObject:
    return tool_success(
        {
            "name": skill_name,
            "status": SKILL_STATUS_ALREADY_ACTIVE,
            "message": (
                f"Skill '{skill_name}' is already active in this session; "
                "its instructions are already in context."
            ),
        }
    )


def _skill_list_result(
    skill_registry: SkillRegistry,
    allowed_skills: Sequence[str] | None,
) -> JsonObject:
    """Return the currently available Skills grouped by origin."""
    allowed = ["*"] if allowed_skills is None else list(allowed_skills)
    skills = skill_registry.filter_allowed(allowed)
    grouped: dict[str | None, list[JsonObject]] = {}
    for skill in skills:
        origin = getattr(skill, "origin", None)
        grouped.setdefault(origin, []).append(
            {"name": skill.name, "description": skill.description}
        )
    skill_groups = [
        {"origin": origin, "skills": grouped[origin]}
        for origin in sorted(grouped, key=skill_origin_sort_key)
    ]
    return tool_success({"skill_groups": skill_groups, "count": len(skills)})


def _allowed_skill_names(
    skill_registry: SkillRegistry,
    allowed_skills: Sequence[str] | None,
) -> set[str]:
    allowed = ["*"] if allowed_skills is None else list(allowed_skills)
    return {skill.name for skill in skill_registry.filter_allowed(allowed)}


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


def _normalized_skill_file_path(file_path: str) -> str:
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("file_path must be a non-empty string")
    raw = PurePosixPath(file_path.replace("\\", "/"))
    if raw.is_absolute() or any(part in {"", ".", ".."} for part in raw.parts):
        raise ValueError(f"Illegal skill file path: {file_path}")
    normalized = raw.as_posix()
    if normalized == SKILL_FILENAME:
        return normalized
    if len(raw.parts) < 2 or raw.parts[0] not in RESOURCE_DIRECTORIES:
        allowed = ", ".join(f"{name}/" for name in RESOURCE_DIRECTORIES)
        raise ValueError(f"Skill files must be {SKILL_FILENAME} or live under {allowed}")
    return normalized


def _wrap_skill_content(
    skill_name: str,
    body: str,
    resources: list[str],
    directory: str,
    *,
    env_keys: Sequence[str] = (),
) -> str:
    lines = [f'<skill_content name="{escape(skill_name, quote=True)}">']
    if env_keys:
        guidance = format_bash_env_usage(
            env_keys,
            intro=(
                "Loading this Skill makes these additional environment credentials "
                "available to Bash calls."
            ),
        )
        lines.extend(["<environment_access>", guidance, "</environment_access>"])
    lines.append(f"Skill directory: {escape(directory)}")
    lines.append(SKILL_PATH_RESOLUTION_NOTE)
    if resources:
        lines.append("<resources>")
        lines.extend(
            f"- {escape(_present_resource_path(resource, directory))}" for resource in resources
        )
        lines.append("</resources>")
    if body:
        lines.append(body)
    lines.append("</skill_content>")
    return "\n".join(lines)


def _present_resource_path(resource: str, directory: str) -> str:
    if PurePosixPath(resource).parts[0] == "scripts":
        return f"{directory}/{resource}"
    return resource


__all__ = [
    "SKILL_LIST_TOOL_DESCRIPTION",
    "SKILL_LIST_TOOL_NAME",
    "SKILL_LIST_TOOL_PARAMETERS",
    "SKILL_TOOL_DESCRIPTION",
    "SKILL_TOOL_NAME",
    "SKILL_TOOL_PARAMETERS",
    "load_skill_file",
    "make_skill_list_handler",
    "make_skill_handler",
    "load_skill_content",
    "register_skill_tool",
]
