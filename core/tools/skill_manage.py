"""Direct authoring tool for an Identity Agent's writable vBot Skills."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from core.skills.authoring import (
    SkillAuthoringError,
    SkillAuthoringService,
    SkillWriteResult,
)
from core.skills.skill_validator import (
    MAX_SKILL_NAME_LENGTH,
    SKILL_NAME_CHARSET_FRAGMENT,
)
from core.skills.skills import find_skill_package_dir
from core.tools.arguments import ToolArgumentError, optional_string, required_string
from core.tools.availability import SKILL_MANAGE_TOOL_NAME
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

SKILL_MANAGE_TOOL_DESCRIPTION = (
    "Create, edit, patch, or delete one private Skill, or write/remove one support "
    "file. Read an existing target with skill before changing it. Global, Project, "
    "and bundled Skills are read-only here."
)

_ACTIONS = ("create", "edit", "patch", "write_file", "remove_file", "delete")
# Actions that may operate on a Skill shared into the caller (maintained in the
# owner's package). ``create`` is own-home-only by definition; ``delete`` stays
# owner/human-only so a receiver cannot remove someone else's playbook.
_SHARED_TARGET_ACTIONS = frozenset({"edit", "patch", "write_file", "remove_file"})
_LOGGER = get_logger("tools.skill_manage")

_ACTION_FIELDS: dict[str, frozenset[str]] = {
    "create": frozenset({"action", "name", "content"}),
    "edit": frozenset({"action", "name", "content"}),
    "patch": frozenset({"action", "name", "file_path", "match", "content"}),
    "write_file": frozenset({"action", "name", "file_path", "content"}),
    "remove_file": frozenset({"action", "name", "file_path"}),
    "delete": frozenset({"action", "name"}),
}

_NAME_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "maxLength": MAX_SKILL_NAME_LENGTH,
    "pattern": f"^{SKILL_NAME_CHARSET_FRAGMENT}$",
    "description": (
        "Skill directory and front-matter name. It must start with a letter or digit "
        "and otherwise use only letters, digits, '-' or '_'."
    ),
}
_CONTENT_PARAMETER: JsonObject = {
    "type": "string",
    "description": (
        "Text to write. Complete SKILL.md for create/edit; replacement text for patch; "
        "complete file for write_file. May be empty for patch or write_file."
    ),
}
_FILE_PATH_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "pattern": r"^(SKILL\.md|(?:scripts|references|assets)/.+)$",
    "description": (
        "Path of the file inside the Skill package to target. Omit when patching "
        "SKILL.md; required for files under scripts, references, or assets."
    ),
}
_MATCH_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Exact non-empty text to replace once for patch. Use a larger unique passage when "
        "the short text occurs more than once."
    ),
}

SKILL_MANAGE_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_ACTIONS),
            "description": "Operation to perform.",
        },
        "name": _NAME_PARAMETER,
        "content": _CONTENT_PARAMETER,
        "file_path": _FILE_PATH_PARAMETER,
        "match": _MATCH_PARAMETER,
    },
    "required": ["action", "name"],
}


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
            action = required_string(arguments.get("action"), field_name="action")
            if action not in _ACTIONS:
                raise ToolArgumentError(f"action must be one of: {', '.join(_ACTIONS)}")
            unexpected = set(arguments) - _ACTION_FIELDS[action]
            if unexpected:
                names = ", ".join(sorted(unexpected))
                raise ToolArgumentError(f"Unknown {action} argument(s): {names}")

            name = required_string(arguments.get("name"), field_name="name")
            own_root = resolve_agent_skills_dir(context.agent_id)
            target_root = own_root
            shared_target = False
            if action in _SHARED_TARGET_ACTIONS and (
                find_skill_package_dir(own_root, name) is None
            ):
                shared_root = (
                    resolve_shared_skills_dir(context.agent_id, name)
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
            if action != "create" and find_skill_package_dir(target_root, name) is None:
                scope = (
                    resolve_external_skill_scope(context.agent_id, name, context.skill_project_id)
                    if resolve_external_skill_scope is not None
                    else None
                )
                if scope is not None:
                    return tool_failure(
                        "skill_write_rejected",
                        _scope_rejection_message(name, scope),
                        retryable=False,
                    )
            result, file_path = _apply_action(
                authoring,
                target_root,
                action,
                name,
                arguments,
            )
        except ToolArgumentError as error:
            return tool_failure("invalid_arguments", str(error), retryable=False)
        except SkillAuthoringError as error:
            return tool_failure(
                "skill_write_rejected",
                "; ".join(error.diagnostics),
                retryable=False,
            )
        except OSError as error:
            return tool_failure("skill_write_error", str(error))

        # A shared-package mutation changes every receiver's pool, so all
        # agent-aware registries rebuild; an own-home write touches one agent.
        invalidate_agent_skills(None if shared_target else context.agent_id)
        _LOGGER.info(
            "Skill mutated (skill=%s scope=%s owner=%s action=%s actor_agent=%s)",
            result.name,
            "shared" if shared_target else "own",
            target_root.parent.name if shared_target else context.agent_id,
            action,
            context.agent_id,
        )
        data: JsonObject = {
            "action": action,
            "name": result.name,
            # Deliberately identical for own and shared targets: a receiving Agent
            # must not be able to tell a shared Skill apart from its own.
            "scope": "own",
            "warnings": list(result.warnings),
            "message": _success_message(action, result.name),
        }
        if file_path is not None:
            data["file_path"] = file_path
        return tool_success(data)

    return skill_manage_handler


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


def _apply_action(
    authoring: SkillAuthoringService,
    target_root: Path,
    action: str,
    name: str,
    arguments: JsonObject,
) -> tuple[SkillWriteResult, str | None]:
    if action == "create":
        content = _exact_string(arguments.get("content"), field_name="content")
        return authoring.create(target_root, name, content, author="agent"), "SKILL.md"
    if action == "edit":
        content = _exact_string(arguments.get("content"), field_name="content")
        return authoring.edit(target_root, name, content, author="agent"), "SKILL.md"
    if action == "patch":
        file_path = (
            optional_string(arguments.get("file_path"), field_name="file_path") or "SKILL.md"
        )
        match = required_string(
            arguments.get("match"),
            field_name="match",
            strip=False,
        )
        content = _exact_string(arguments.get("content"), field_name="content")
        result = authoring.patch(
            target_root,
            name,
            match,
            content,
            author="agent",
            relative_path=file_path,
        )
        return result, file_path.replace("\\", "/")
    if action == "write_file":
        file_path = required_string(arguments.get("file_path"), field_name="file_path")
        content = _exact_string(arguments.get("content"), field_name="content")
        result = authoring.write_file(target_root, name, file_path, content)
        return result, file_path.replace("\\", "/")
    if action == "remove_file":
        file_path = required_string(arguments.get("file_path"), field_name="file_path")
        result = authoring.remove_file(target_root, name, file_path)
        return result, file_path.replace("\\", "/")
    return authoring.delete(target_root, name), None


def _exact_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ToolArgumentError(f"{field_name} must be a string")
    return value


def _success_message(action: str, name: str) -> str:
    verbs = {
        "create": "created",
        "edit": "updated",
        "patch": "patched",
        "write_file": "file written",
        "remove_file": "file removed",
        "delete": "deleted",
    }
    return f"Skill '{name}' {verbs[action]}."


def register_skill_manage_tool(
    registry: ToolRegistry,
    authoring: SkillAuthoringService,
    resolve_agent_skills_dir: Callable[[str], Path],
    invalidate_agent_skills: Callable[[str | None], None],
    resolve_shared_skills_dir: Callable[[str, str], Path | None] | None = None,
    resolve_external_skill_scope: (Callable[[str, str, str | None], str | None] | None) = None,
) -> None:
    """Register identity-only direct Skill management."""
    registry.register(
        SKILL_MANAGE_TOOL_NAME,
        SKILL_MANAGE_TOOL_DESCRIPTION,
        SKILL_MANAGE_TOOL_PARAMETERS,
        make_skill_manage_handler(
            authoring,
            resolve_agent_skills_dir,
            invalidate_agent_skills,
            resolve_shared_skills_dir,
            resolve_external_skill_scope,
        ),
        family="skills",
        constraints=("identity_agent",),
        open_input_schema=True,
        result_schema={"type": "object", "required": ["scope"]},
        display=ToolDisplay(parts_builder=_skill_manage_display_parts),
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
