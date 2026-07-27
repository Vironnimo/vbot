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
from core.skills.skill_validator import (
    MAX_SKILL_NAME_LENGTH,
    SKILL_NAME_CHARSET_FRAGMENT,
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
    "references/, and assets/). Call with exactly one top-level operation object, "
    'for example {"begin":{"name":"wiki-research","mode":"create"}}. Each operation '
    "exposes only its valid arguments and structurally requires everything it needs. "
    "Inspect a published package or owned draft; begin an isolated create/update "
    "draft; put, patch, or remove draft files; validate; then commit the whole "
    "package or abort it. put_file accepts UTF-8 content or copies a binary "
    "source_path from your Workspace/current Project. delete archives the published "
    "package for recovery. Omit scope for your private Skill home; use "
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
_OWN_SCOPE = "own"
_GLOBAL_SCOPE = "global"
_SCOPES = (_OWN_SCOPE, _GLOBAL_SCOPE)
_DRAFT_MODES = ("create", "update")
_LOGGER = get_logger("tools.skill_manage")

_SCOPE_PARAMETER: JsonObject = {
    "type": "string",
    "enum": list(_SCOPES),
    "default": _OWN_SCOPE,
    "description": (
        "Target Skill home. Omit for 'own', the calling Agent's private home. Use "
        "'global' only when the user explicitly requested a Skill shared by all Agents."
    ),
}
_NAME_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "maxLength": MAX_SKILL_NAME_LENGTH,
    "pattern": f"^{SKILL_NAME_CHARSET_FRAGMENT}$",
    "description": (
        "Published Skill directory and front-matter name. Use a short trigger-safe name "
        "that starts with a letter or digit and otherwise uses letters, digits, '-' or "
        "'_', for example 'wiki-research'."
    ),
}
_DRAFT_ID_PARAMETER: JsonObject = {
    "type": "string",
    "pattern": "^[a-f0-9]{32}$",
    "description": "Opaque draft_id returned by begin; copy it exactly.",
}
_PACKAGE_PATH_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "pattern": r"^(SKILL\.md|(?:scripts|references|assets)/.+)$",
    "description": (
        "Package-relative path: SKILL.md or a file below scripts/, references/, "
        "or assets/. No other top-level path is valid."
    ),
}


def _operation_parameters(
    description: str,
    properties: JsonObject,
    *,
    required: Sequence[str] = (),
    exactly_one_of: Sequence[str] = (),
) -> JsonObject:
    """Build one strict operation object for the provider-visible schema."""

    schema: JsonObject = {
        "type": "object",
        "description": description,
        "properties": {"scope": _SCOPE_PARAMETER, **properties},
        "required": list(required),
        "additionalProperties": False,
    }
    if exactly_one_of:
        schema["oneOf"] = [{"required": [name]} for name in exactly_one_of]
    return schema


SKILL_MANAGE_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "description": (
        "Choose exactly one operation property. Its value is the complete argument object "
        "for that operation; never send flat operation/name/draft_id fields."
    ),
    "properties": {
        "inspect": _operation_parameters(
            "Inspect one published Skill by name or one owned draft by draft_id. Provide "
            "exactly one of name or draft_id; path optionally returns one UTF-8 file.",
            {
                "name": _NAME_PARAMETER,
                "draft_id": _DRAFT_ID_PARAMETER,
                "path": _PACKAGE_PATH_PARAMETER,
            },
            exactly_one_of=("name", "draft_id"),
        ),
        "begin": _operation_parameters(
            "Create an isolated draft. mode='create' starts an absent Skill; mode='update' "
            "copies an existing published Skill. Save the returned draft_id for every "
            "following draft operation.",
            {
                "name": _NAME_PARAMETER,
                "mode": {
                    "type": "string",
                    "enum": list(_DRAFT_MODES),
                    "description": (
                        "'create' requires the published name to be absent; 'update' "
                        "requires it to exist and copies the complete package."
                    ),
                },
                "source": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Optional concise provenance label recorded under metadata.vbot "
                        "when the draft is committed."
                    ),
                },
            },
            required=("name", "mode"),
        ),
        "put_file": _operation_parameters(
            "Write one complete draft file. Provide exactly one of UTF-8 text content or "
            "source_path for a byte-preserving copy from the current Project/Workspace.",
            {
                "draft_id": _DRAFT_ID_PARAMETER,
                "path": _PACKAGE_PATH_PARAMETER,
                "content": {
                    "type": "string",
                    "description": (
                        "Complete UTF-8 file content. May be empty. Mutually exclusive "
                        "with source_path."
                    ),
                },
                "source_path": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Existing regular file inside the current Project or Workspace to "
                        "copy byte-for-byte. Mutually exclusive with content."
                    ),
                },
                "executable": {
                    "type": "boolean",
                    "description": (
                        "Whether the destination should be executable. true is valid only "
                        "for files below scripts/. Omit with source_path to preserve the "
                        "source file's executable mode; omission with content means false."
                    ),
                },
            },
            required=("draft_id", "path"),
            exactly_one_of=("content", "source_path"),
        ),
        "patch": _operation_parameters(
            "Replace one unique exact UTF-8 string inside a draft file. path defaults to "
            "SKILL.md; new_string may be empty to remove the match.",
            {
                "draft_id": _DRAFT_ID_PARAMETER,
                "path": _PACKAGE_PATH_PARAMETER,
                "old_string": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Exact existing text; it must occur exactly once.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Exact replacement text; an empty string deletes the match.",
                },
            },
            required=("draft_id", "old_string", "new_string"),
        ),
        "remove_file": _operation_parameters(
            "Remove one support file from an owned draft. SKILL.md cannot be removed.",
            {
                "draft_id": _DRAFT_ID_PARAMETER,
                "path": _PACKAGE_PATH_PARAMETER,
            },
            required=("draft_id", "path"),
        ),
        "validate": _operation_parameters(
            "Validate the complete isolated package before commit and return its manifest "
            "and diagnostics without changing the published Skill.",
            {"draft_id": _DRAFT_ID_PARAMETER},
            required=("draft_id",),
        ),
        "commit": _operation_parameters(
            "Validate and atomically publish the complete owned draft, then discard the "
            "draft. Call validate first so diagnostics can be handled deliberately.",
            {"draft_id": _DRAFT_ID_PARAMETER},
            required=("draft_id",),
        ),
        "abort": _operation_parameters(
            "Discard one owned draft without changing the published Skill.",
            {"draft_id": _DRAFT_ID_PARAMETER},
            required=("draft_id",),
        ),
        "delete": _operation_parameters(
            "Move one published Skill package into the recoverable archive.",
            {"name": _NAME_PARAMETER},
            required=("name",),
        ),
    },
    "minProperties": 1,
    "maxProperties": 1,
    "additionalProperties": False,
}

_OPERATION_FIELDS: dict[str, frozenset[str]] = {
    "inspect": frozenset({"scope", "name", "draft_id", "path"}),
    "begin": frozenset({"scope", "name", "mode", "source"}),
    "put_file": frozenset({"scope", "draft_id", "path", "content", "source_path", "executable"}),
    "patch": frozenset({"scope", "draft_id", "path", "old_string", "new_string"}),
    "remove_file": frozenset({"scope", "draft_id", "path"}),
    "validate": frozenset({"scope", "draft_id"}),
    "commit": frozenset({"scope", "draft_id"}),
    "abort": frozenset({"scope", "draft_id"}),
    "delete": frozenset({"scope", "name"}),
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
        try:
            operation, operation_arguments = _extract_operation(arguments)
            scope = operation_arguments.get("scope", _OWN_SCOPE)
            if not isinstance(scope, str) or scope not in _SCOPES:
                allowed = ", ".join(_SCOPES)
                raise ToolArgumentError(f"scope must be one of: {allowed}")
            target_root = (
                resolve_global_skills_dir()
                if scope == _GLOBAL_SCOPE
                else resolve_agent_skills_dir(context.agent_id)
            )
            data, live_mutation = _apply_operation(
                authoring,
                target_root,
                operation,
                operation_arguments,
                context=context,
                scope=scope,
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


def _extract_operation(arguments: JsonObject) -> tuple[str, JsonObject]:
    """Return the one selected operation and its strict argument object."""

    if len(arguments) != 1:
        allowed = ", ".join(_OPERATIONS)
        raise ToolArgumentError(
            f"skill_manage requires exactly one top-level operation object: {allowed}"
        )
    operation, raw_arguments = next(iter(arguments.items()))
    if operation not in _OPERATIONS:
        allowed = ", ".join(_OPERATIONS)
        raise ToolArgumentError(f"operation must be one of: {allowed}")
    if not isinstance(raw_arguments, dict):
        raise ToolArgumentError(f"{operation} must be an object")
    unknown_arguments = set(raw_arguments) - _OPERATION_FIELDS[operation]
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        raise ToolArgumentError(f"Unknown {operation} argument(s): {names}")
    return operation, cast(JsonObject, raw_arguments)


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
        name = optional_string(arguments.get("name"), field_name="name")
        selected_path = optional_string(arguments.get("path"), field_name="path")
        if (draft_id is None) == (name is None):
            raise ToolArgumentError("inspect requires exactly one of name or draft_id")
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
        inspection = authoring.inspect_published(
            target_root,
            cast(str, name),
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
        display=ToolDisplay(summary_builder=_skill_manage_display_summary),
    )


def _skill_manage_display_summary(arguments: JsonObject) -> str | None:
    """Summarize the nested operation call without exposing file content."""

    if len(arguments) != 1:
        return None
    operation, raw_arguments = next(iter(arguments.items()))
    if operation not in _OPERATIONS or not isinstance(raw_arguments, dict):
        return None
    identifier = raw_arguments.get("name") or raw_arguments.get("draft_id")
    scope = raw_arguments.get("scope")
    parts = [operation]
    if isinstance(identifier, str) and identifier.strip():
        parts.append(identifier)
    if isinstance(scope, str) and scope.strip():
        parts.append(scope)
    return " · ".join(parts)


__all__ = [
    "SKILL_MANAGE_TOOL_DESCRIPTION",
    "SKILL_MANAGE_TOOL_NAME",
    "SKILL_MANAGE_TOOL_PARAMETERS",
    "make_skill_manage_handler",
    "register_skill_manage_tool",
]
