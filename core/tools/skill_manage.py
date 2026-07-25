"""Package-oriented authoring tool for vBot Skills.

A vBot Skill is exactly one ``SKILL.md`` plus optional files below ``scripts/``,
``references/``, and ``assets/``. ``skill_manage`` edits that package through an
isolated draft and publishes it only after complete-package validation. Draft
operations never invalidate the live Skill registry; commit and recoverable archive
are the only live mutations.

The tool is identity-only and writes either the calling Agent's private Skill home
(``scope="own"``, the default) or the shared global pool when the user explicitly
requested a global Skill. Project Skills remain repository-owned and bundled Skills
remain read-only.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from core.skills.authoring import (
    SkillAuthoringError,
    SkillAuthoringService,
    SkillDraftMode,
    SkillPackageFile,
    SkillPackageInspection,
)
from core.tools.arguments import (
    ToolArgumentError,
    coerce_bool,
    optional_string,
    required_string,
)
from core.tools.availability import SKILL_MANAGE_TOOL_NAME
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

SKILL_MANAGE_TOOL_DESCRIPTION = (
    "Manage complete vBot Skill packages (SKILL.md plus optional scripts/, "
    "references/, and assets/). Inspect a published package; begin an isolated "
    "create/update draft; put, patch, or remove draft files; validate; then commit "
    "the whole package or abort it. put_file accepts UTF-8 content or copies a "
    "binary source_path from your Workspace/current Project. delete archives the "
    "published package for recovery. Defaults to your private Skill home; use "
    "scope='global' ONLY when the user explicitly requested a global Skill."
)

_OPERATIONS = (
    "inspect",
    "begin",
    "put_file",
    "patch",
    "remove_file",
    "validate",
    "commit",
    "abort",
    "delete",
)
_KNOWN_FIELDS = frozenset(
    {
        "operation",
        "scope",
        "name",
        "mode",
        "draft_id",
        "path",
        "content",
        "source_path",
        "executable",
        "old_string",
        "new_string",
        "source",
    }
)
_OWN_SCOPE = "own"
_GLOBAL_SCOPE = "global"
_SCOPES = (_OWN_SCOPE, _GLOBAL_SCOPE)
_DRAFT_MODES = ("create", "update")
_LOGGER = get_logger("tools.skill_manage")

SKILL_MANAGE_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": list(_OPERATIONS),
            "description": (
                "inspect a published package or draft; begin a create/update draft; "
                "put_file, patch, or remove_file inside that draft; validate; commit "
                "or abort the draft; delete archives a published Skill."
            ),
        },
        "scope": {
            "type": "string",
            "enum": list(_SCOPES),
            "description": (
                "'own' (default) is your private Skill home. 'global' is the shared "
                "global pool; use it only when the user explicitly requested global."
            ),
        },
        "name": {
            "type": "string",
            "description": (
                "Published Skill/directory name; required by inspect, begin, and delete."
            ),
        },
        "mode": {
            "type": "string",
            "enum": list(_DRAFT_MODES),
            "description": "begin: create a new package or update a copy of an existing package.",
        },
        "draft_id": {
            "type": "string",
            "description": "Draft id returned by begin; required for every draft operation.",
        },
        "path": {
            "type": "string",
            "description": (
                "Package-relative file path: SKILL.md or a file below scripts/, "
                "references/, or assets/. inspect may select one text file to return; "
                "patch defaults to SKILL.md."
            ),
        },
        "content": {
            "type": "string",
            "description": "put_file: UTF-8 text content (mutually exclusive with source_path).",
        },
        "source_path": {
            "type": "string",
            "description": (
                "put_file: copy a regular text or binary file from the current Project "
                "or Workspace byte-for-byte (mutually exclusive with content)."
            ),
        },
        "executable": {
            "type": "boolean",
            "description": "put_file: set executable mode; true is valid only below scripts/.",
        },
        "old_string": {
            "type": "string",
            "description": "patch: exact existing UTF-8 text to replace; it must be unique.",
        },
        "new_string": {
            "type": "string",
            "description": "patch: replacement text; it may be empty.",
        },
        "source": {
            "type": "string",
            "description": "begin: optional provenance label recorded in SKILL.md on commit.",
        },
    },
    "required": ["operation"],
    "additionalProperties": False,
}


def make_skill_manage_handler(
    authoring: SkillAuthoringService,
    resolve_agent_skills_dir: Callable[[str], Path],
    invalidate_agent_skills: Callable[[str], None],
    resolve_global_skills_dir: Callable[[], Path],
    reload_skills: Callable[[], None],
) -> Callable[[ToolContext, JsonObject], JsonObject]:
    """Return the vBot Skill package-management handler."""

    def skill_manage_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        unknown_arguments = set(arguments) - _KNOWN_FIELDS
        if unknown_arguments:
            names = ", ".join(sorted(unknown_arguments))
            return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

        operation = arguments.get("operation")
        if not isinstance(operation, str) or operation not in _OPERATIONS:
            allowed = ", ".join(_OPERATIONS)
            return tool_failure("invalid_arguments", f"operation must be one of: {allowed}")

        scope = arguments.get("scope", _OWN_SCOPE)
        if not isinstance(scope, str) or scope not in _SCOPES:
            allowed = ", ".join(_SCOPES)
            return tool_failure("invalid_arguments", f"scope must be one of: {allowed}")
        target_root = (
            resolve_global_skills_dir()
            if scope == _GLOBAL_SCOPE
            else resolve_agent_skills_dir(context.agent_id)
        )

        try:
            data, live_mutation = _apply_operation(
                authoring,
                target_root,
                operation,
                arguments,
                context=context,
                scope=scope,
            )
        except ToolArgumentError as error:
            return tool_failure("invalid_arguments", str(error))
        except SkillAuthoringError as error:
            return tool_failure("skill_write_rejected", "; ".join(error.diagnostics))
        except OSError as error:
            return tool_failure("skill_write_error", str(error))

        if live_mutation:
            if scope == _GLOBAL_SCOPE:
                reload_skills()
            else:
                invalidate_agent_skills(context.agent_id)
            _LOGGER.info(
                "Skill package mutated (skill=%s scope=%s operation=%s actor_agent=%s)",
                data.get("name"),
                scope,
                operation,
                context.agent_id,
            )
        return tool_success({**data, "scope": scope})

    return skill_manage_handler


def _apply_operation(
    authoring: SkillAuthoringService,
    target_root: Path,
    operation: str,
    arguments: JsonObject,
    *,
    context: ToolContext,
    scope: str,
) -> tuple[JsonObject, bool]:
    if operation == "inspect":
        draft_id = optional_string(arguments.get("draft_id"), field_name="draft_id")
        selected_path = optional_string(arguments.get("path"), field_name="path")
        if draft_id is not None:
            inspection = authoring.inspect_draft(
                target_root,
                draft_id,
                actor_id=context.agent_id,
                selected_path=selected_path,
            )
            return {
                "operation": operation,
                "draft_id": draft_id,
                **_inspection_data(inspection),
            }, False
        name = required_string(arguments.get("name"), field_name="name")
        inspection = authoring.inspect_published(
            target_root,
            name,
            selected_path=selected_path,
        )
        return {"operation": operation, **_inspection_data(inspection)}, False

    if operation == "begin":
        name = required_string(arguments.get("name"), field_name="name")
        mode = required_string(arguments.get("mode"), field_name="mode")
        if mode not in _DRAFT_MODES:
            raise ToolArgumentError(f"mode must be one of: {', '.join(_DRAFT_MODES)}")
        provenance_source = optional_string(arguments.get("source"), field_name="source")
        draft = authoring.begin_draft(
            target_root,
            name,
            mode=cast(SkillDraftMode, mode),
            actor_id=context.agent_id,
            author="agent",
            source=provenance_source,
        )
        return {
            "operation": operation,
            "name": draft.name,
            "mode": draft.mode,
            "draft_id": draft.id,
            "message": (
                f"Draft {draft.id} is isolated; publish it only with validate then commit."
            ),
        }, False

    if operation == "delete":
        name = required_string(arguments.get("name"), field_name="name")
        namespace = (_GLOBAL_SCOPE,) if scope == _GLOBAL_SCOPE else ("agents", context.agent_id)
        result = authoring.archive_skill(
            target_root,
            name,
            archive_namespace=namespace,
        )
        return {
            "operation": operation,
            "name": result.name,
            "archive_path": str(result.path),
            "message": f"Skill '{result.name}' was archived and can be recovered.",
        }, True

    draft_id = required_string(arguments.get("draft_id"), field_name="draft_id")
    if operation == "put_file":
        path = required_string(arguments.get("path"), field_name="path")
        has_content = "content" in arguments
        source_path = optional_string(arguments.get("source_path"), field_name="source_path")
        if has_content == (source_path is not None):
            raise ToolArgumentError("put_file requires exactly one of content or source_path")
        executable_argument = arguments.get("executable")
        if has_content:
            content = _exact_string(arguments.get("content"), field_name="content")
            text_executable = coerce_bool(
                executable_argument,
                field_name="executable",
                default=False,
            )
            manifest_file = authoring.put_draft_text(
                target_root,
                draft_id,
                path,
                content,
                actor_id=context.agent_id,
                executable=text_executable,
            )
        else:
            copied_executable = (
                None
                if "executable" not in arguments
                else coerce_bool(executable_argument, field_name="executable", default=False)
            )
            resolved_source = _resolve_safe_source(context, source_path or "")
            manifest_file = authoring.copy_draft_file(
                target_root,
                draft_id,
                path,
                resolved_source,
                actor_id=context.agent_id,
                executable=copied_executable,
            )
        return {
            "operation": operation,
            "draft_id": draft_id,
            "file": _manifest_file_data(manifest_file),
        }, False

    if operation == "patch":
        path = optional_string(arguments.get("path"), field_name="path") or "SKILL.md"
        old_string = required_string(
            arguments.get("old_string"),
            field_name="old_string",
            strip=False,
        )
        new_string = _exact_string(arguments.get("new_string"), field_name="new_string")
        manifest_file = authoring.patch_draft_text(
            target_root,
            draft_id,
            path,
            old_string,
            new_string,
            actor_id=context.agent_id,
        )
        return {
            "operation": operation,
            "draft_id": draft_id,
            "file": _manifest_file_data(manifest_file),
        }, False

    if operation == "remove_file":
        path = required_string(arguments.get("path"), field_name="path")
        authoring.remove_draft_file(
            target_root,
            draft_id,
            path,
            actor_id=context.agent_id,
        )
        return {
            "operation": operation,
            "draft_id": draft_id,
            "path": path.replace("\\", "/"),
        }, False

    if operation == "validate":
        inspection = authoring.validate_draft(
            target_root,
            draft_id,
            actor_id=context.agent_id,
        )
        return {
            "operation": operation,
            "draft_id": draft_id,
            "valid": True,
            **_inspection_data(inspection),
        }, False

    if operation == "commit":
        result = authoring.commit_draft(
            target_root,
            draft_id,
            actor_id=context.agent_id,
        )
        return {
            "operation": operation,
            "name": result.name,
            "draft_id": draft_id,
            "warnings": list(result.warnings),
            "message": f"Skill '{result.name}' package committed.",
        }, True

    draft = authoring.abort_draft(
        target_root,
        draft_id,
        actor_id=context.agent_id,
    )
    return {
        "operation": operation,
        "name": draft.name,
        "draft_id": draft.id,
        "message": f"Skill draft {draft.id} discarded; published state was unchanged.",
    }, False


def _inspection_data(inspection: SkillPackageInspection) -> JsonObject:
    data: JsonObject = {
        "name": inspection.name,
        "skill_md": inspection.skill_md,
        "files": [_manifest_file_data(item) for item in inspection.files],
        "diagnostics": list(inspection.diagnostics),
    }
    if inspection.selected_path is not None:
        data["selected_path"] = inspection.selected_path
        data["selected_content"] = inspection.selected_content
    return data


def _manifest_file_data(item: SkillPackageFile) -> JsonObject:
    return {
        "path": item.path,
        "kind": item.kind,
        "size": item.size,
        "sha256": item.sha256,
        "media_type": item.media_type,
        "binary": item.binary,
        "executable": item.executable,
    }


def _resolve_safe_source(context: ToolContext, value: str) -> Path:
    source = context.resolve_path(value)
    allowed_roots = _unique_paths((context.effective_cwd, context.workspace))
    if not any(_is_within(source, root) for root in allowed_roots):
        locations = ", ".join(str(root) for root in allowed_roots)
        raise SkillAuthoringError(
            f"source_path must stay inside the current Project or Workspace: {locations}"
        )
    if not source.is_file():
        raise SkillAuthoringError(f"source_path is not a regular file: {source}")
    return source


def _unique_paths(paths: Sequence[Path]) -> list[Path]:
    unique: list[Path] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _exact_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ToolArgumentError(f"{field_name} must be a string")
    return value


def register_skill_manage_tool(
    registry: ToolRegistry,
    authoring: SkillAuthoringService,
    resolve_agent_skills_dir: Callable[[str], Path],
    invalidate_agent_skills: Callable[[str], None],
    resolve_global_skills_dir: Callable[[], Path],
    reload_skills: Callable[[], None],
) -> None:
    """Register identity-only vBot Skill package management."""
    registry.register(
        SKILL_MANAGE_TOOL_NAME,
        SKILL_MANAGE_TOOL_DESCRIPTION,
        SKILL_MANAGE_TOOL_PARAMETERS,
        make_skill_manage_handler(
            authoring,
            resolve_agent_skills_dir,
            invalidate_agent_skills,
            resolve_global_skills_dir,
            reload_skills,
        ),
        display=ToolDisplay(summary_fields=("operation", "name", "draft_id", "scope")),
    )


__all__ = [
    "SKILL_MANAGE_TOOL_DESCRIPTION",
    "SKILL_MANAGE_TOOL_NAME",
    "SKILL_MANAGE_TOOL_PARAMETERS",
    "make_skill_manage_handler",
    "register_skill_manage_tool",
]
