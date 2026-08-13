"""Project entity, ``project.json`` schema, and field validation.

This is the public/main file of the ``core/projects`` deep module. A Project is
a first-class entity (see GLOSSARY → Project): a stable ``project_id`` slug, a
changeable ``display_name``, the repo ``cwd`` that tools resolve relative paths
against, optional project-default agent/model pointers, and an ordered
``auto_load`` file list. The minimal valid Project is just a cwd — team and
auto-load files are optional. ``AGENTS.md`` (the tool-neutral project-instruction
convention) is seeded as the first ``auto_load`` entry at creation, then a normal
removable entry — vBot does not special-case it at render time.

This module owns the persisted schema and enforces it once at load time before
constructing the entity. The on-disk anchor lifecycle and CRUD live in
``core/projects/store.py``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonObject,
    JsonValidationReport,
    add_error,
    child_path,
    error_diagnostic,
    load_validated_json_file,
    validate_json_file,
    validate_non_empty_string,
    validate_optional_allowed_string,
    validate_optional_string,
    validate_optional_string_list,
    validate_string,
    warn_unknown_keys,
)
from core.projects.paths import normalize_cwd
from core.settings import (
    DEFAULT_PROJECT_SOURCE_FORMAT,
    PROJECT_SOURCE_FORMATS,
    PROJECT_TOOL_ALLOWLIST_WILDCARD,
    SettingsValidationError,
    is_valid_project_id,
    validate_temperature,
    validate_thinking_effort,
)
from core.settings.validation import (
    validate_optional_compaction_policy,
    validate_temperature_diagnostic,
    validate_thinking_effort_diagnostic,
)

DEFAULT_DEFAULT_AGENT = ""
DEFAULT_DEFAULT_MODEL = ""
# A project may carry default reasoning/sampling knobs alongside its default
# model; unset (``None``) means "fall through the resolution chain" to the global
# agent default and finally the provider default, exactly like ``default_model``.
DEFAULT_DEFAULT_TEMPERATURE: float | None = None
DEFAULT_DEFAULT_THINKING_EFFORT: str | None = None

# The project Tool Whitelist ceiling a new project starts with and the fallback an
# old ``project.json`` missing the field loads at (decision 2 / decision 10). This
# is the SINGLE source for the creation seed, the missing-field fallback, and the
# UI "reset to defaults" — change the base list here and all three move together.
# The default-off-but-UI-toggleable Tools (``session_search``, ``image_generation``,
# ``text_to_speech``, ``cron``, ``channel_send``, the Home-Assistant tools) are
# deliberately absent. Automatic and Identity-only Tools are never directly
# configurable Project Tools; ``skill`` itself is directly configurable and default-on.
PROJECT_DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = (
    "read",
    "write",
    "edit",
    "glob",
    "grep",
    "bash",
    "process",
    "terminal",
    "web_fetch",
    "web_search",
    "status",
    "subagent",
    "skill",
)


def project_tool_configurability_reason(
    *, activation: str, constraints: Sequence[str]
) -> str | None:
    """Return why declarative Tool metadata excludes Project configuration."""
    if activation == "follows":
        return "follows_another_tool"
    if activation == "memory_mode":
        return "activated_by_memory_mode"
    if activation == "session_grant":
        return "requires_session_grant"
    if "identity_agent" in constraints:
        return "requires_identity_agent"
    return None


# The optional fields a per-agent override may carry. Each maps to the top tier of
# the matching config-agent resolver chain (model / temperature / thinking effort).
OVERRIDE_FIELDS: frozenset[str] = frozenset(
    {"model", "temperature", "thinking_effort", "compaction_policy", "tool_access"}
)

_PROJECT_CONFIG_FIELDS = frozenset(
    {
        "allowed_tools",
        "auto_load",
        "created_at",
        "cwd",
        "default_agent",
        "default_model",
        "default_temperature",
        "default_thinking_effort",
        "display_name",
        "overrides",
        "project_id",
        "skills_bundled_enabled",
        "skills_global_enabled",
        "skills_project_disabled",
        "source_format",
        "updated_at",
    }
)
# The tool-neutral project-instruction convention (the agents.md standard). Seeded
# as the first ``auto_load`` entry when a project is created
# (:func:`seed_default_auto_load`, used by ``ProjectStore.create``), then treated
# like any other list entry — removable, reorderable, rendered only through the
# list. CLAUDE.md and other tool-specific files are deliberately not seeded; the
# user adds those explicitly.
PROJECT_AGENTS_FILE = "AGENTS.md"


def seed_default_auto_load(auto_load: list[str] | None) -> list[str]:
    """Return the ``auto_load`` list a brand-new project starts with.

    Seeds :data:`PROJECT_AGENTS_FILE` as the first entry unless the caller already
    named it (case-insensitive — the file may live on a case-insensitive
    filesystem). **Creation-only:** editing a project must never re-seed, so a user
    who removes AGENTS.md keeps it removed; only ``ProjectStore.create`` calls this.
    """
    existing = list(auto_load or [])
    target = PROJECT_AGENTS_FILE.casefold()
    if any(entry.strip().casefold() == target for entry in existing):
        return existing
    return [PROJECT_AGENTS_FILE, *existing]


class ProjectError(ValueError):
    """Base error for expected project lifecycle failures."""


class ProjectAlreadyExistsError(ProjectError):
    """Raised when creating a project whose id (or cwd) already exists."""


class ProjectNotFoundError(ProjectError):
    """Raised when a project cannot be found."""


class InvalidProjectIdError(ProjectError):
    """Raised when a project id is unsafe for filesystem use."""


def validate_project_file(project_path: str | Path) -> JsonValidationReport:
    """Validate one persisted ``project.json`` without consuming it."""
    return validate_json_file(project_path, validate_project_data, missing_ok=False)


def load_validated_project_json(project_path: str | Path) -> JsonObject:
    """Load one schema-valid ``project.json`` mapping."""
    try:
        return cast(
            "JsonObject",
            load_validated_json_file(project_path, validate_project_data, missing_ok=False),
        )
    except JsonConfigValidationError as error:
        raise ProjectError(str(error)) from error


def validate_project_data(data: Any) -> list[JsonDiagnostic]:
    """Validate a decoded raw ``project.json`` mapping."""
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, dict):
        return [error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]

    warn_unknown_keys(diagnostics, "$", data, _PROJECT_CONFIG_FIELDS, "project field")
    _validate_project_config_id(diagnostics, data.get("project_id"))
    validate_optional_string(diagnostics, "$.display_name", data.get("display_name"))
    # A moved repository remains a valid re-point candidate, so the file rule
    # checks only that cwd is a non-empty path string, never that it exists.
    validate_non_empty_string(diagnostics, "$.cwd", data.get("cwd"), required=True)
    validate_optional_string(diagnostics, "$.default_agent", data.get("default_agent"))
    validate_optional_string(diagnostics, "$.default_model", data.get("default_model"))
    validate_temperature_diagnostic(
        diagnostics, "$.default_temperature", data.get("default_temperature"), allow_none=True
    )
    validate_thinking_effort_diagnostic(
        diagnostics,
        "$.default_thinking_effort",
        data.get("default_thinking_effort"),
        allow_none=True,
    )
    validate_optional_allowed_string(
        diagnostics,
        "$.source_format",
        data.get("source_format"),
        frozenset(PROJECT_SOURCE_FORMATS),
    )
    _validate_auto_load_list(diagnostics, "$.auto_load", data.get("auto_load"))
    validate_optional_string_list(diagnostics, "$.allowed_tools", data.get("allowed_tools"))
    if isinstance(data.get("allowed_tools"), list):
        for index, tool_name in enumerate(data["allowed_tools"]):
            if tool_name == PROJECT_TOOL_ALLOWLIST_WILDCARD:
                add_error(
                    diagnostics,
                    f"$.allowed_tools[{index}]",
                    "the all-tools wildcard '*' is not allowed in a Project Tool Whitelist",
                )
    validate_optional_string_list(
        diagnostics, "$.skills_bundled_enabled", data.get("skills_bundled_enabled")
    )
    validate_optional_string_list(
        diagnostics, "$.skills_global_enabled", data.get("skills_global_enabled")
    )
    validate_optional_string_list(
        diagnostics, "$.skills_project_disabled", data.get("skills_project_disabled")
    )
    _validate_override_schema(diagnostics, "$.overrides", data.get("overrides"))
    _validate_override_ceiling_diagnostics(
        diagnostics,
        data.get("overrides"),
        data.get("allowed_tools"),
    )
    validate_string(diagnostics, "$.created_at", data.get("created_at"), required=False)
    validate_string(diagnostics, "$.updated_at", data.get("updated_at"), required=False)
    return diagnostics


def _validate_project_config_id(diagnostics: list[JsonDiagnostic], value: Any) -> None:
    if not isinstance(value, str) or not value:
        add_error(diagnostics, "$.project_id", "must be a non-empty string")
    elif not is_valid_project_id(value):
        add_error(
            diagnostics,
            "$.project_id",
            "must be 1-64 characters using only letters, numbers, hyphen, or underscore",
        )


def _validate_auto_load_list(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        add_error(diagnostics, path, "must be a list of strings")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            add_error(diagnostics, f"{path}[{index}]", "must be a non-empty string")


def _validate_override_schema(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        add_error(diagnostics, path, "must be an object")
        return
    for agent_id, override in value.items():
        if not isinstance(agent_id, str) or not agent_id.strip():
            add_error(diagnostics, path, "keys must be non-empty agent id strings")
            continue
        _validate_one_override_schema(diagnostics, child_path(path, agent_id), override)


def _validate_one_override_schema(
    diagnostics: list[JsonDiagnostic], path: str, override: Any
) -> None:
    if not isinstance(override, Mapping):
        add_error(diagnostics, path, "must be an object")
        return
    for field_name in sorted(set(override) - OVERRIDE_FIELDS):
        add_error(
            diagnostics,
            child_path(path, field_name),
            f"unknown override field: {field_name}",
        )
    if not override:
        add_error(diagnostics, path, "must set at least one field")
    if "model" in override and (
        not isinstance(override["model"], str) or not override["model"].strip()
    ):
        add_error(diagnostics, child_path(path, "model"), "must be a non-empty string")
    if "temperature" in override:
        validate_temperature_diagnostic(
            diagnostics,
            child_path(path, "temperature"),
            override["temperature"],
            allow_none=False,
        )
    if "thinking_effort" in override:
        validate_thinking_effort_diagnostic(
            diagnostics,
            child_path(path, "thinking_effort"),
            override["thinking_effort"],
            allow_none=False,
        )
    if "compaction_policy" in override:
        validate_optional_compaction_policy(
            diagnostics,
            override["compaction_policy"],
            child_path(path, "compaction_policy"),
        )
    if "tool_access" in override:
        try:
            _normalize_tool_access_policy(override["tool_access"])
        except ValueError as error:
            add_error(diagnostics, child_path(path, "tool_access"), str(error))


def _validate_override_ceiling_diagnostics(
    diagnostics: list[JsonDiagnostic],
    overrides: Any,
    allowed_tools: Any,
) -> None:
    if not isinstance(overrides, Mapping):
        return
    ceiling = set(
        allowed_tools if isinstance(allowed_tools, list) else PROJECT_DEFAULT_ALLOWED_TOOLS
    )
    for agent_id, override in overrides.items():
        if not isinstance(agent_id, str) or not isinstance(override, Mapping):
            continue
        raw_policy = override.get("tool_access")
        try:
            policy = _normalize_tool_access_policy(raw_policy) if raw_policy is not None else None
        except ValueError:
            continue
        if policy is None:
            continue
        for tool_name in sorted(set(policy.allowed) - ceiling):
            add_error(
                diagnostics,
                child_path(child_path("$.overrides", agent_id), "tool_access.allowed"),
                f"Tool is outside the Project Tool Whitelist: {tool_name}",
            )


@dataclass(frozen=True)
class Project:
    """Persisted project configuration stored in ``project.json``.

    The anchor directory name is the ``project_id``; the ``cwd`` lives in the
    file (not the directory name) so the repo folder can move without breaking
    the key or its Sessions.
    """

    project_id: str
    display_name: str
    cwd: str
    created_at: str
    updated_at: str
    default_agent: str = DEFAULT_DEFAULT_AGENT
    default_model: str = DEFAULT_DEFAULT_MODEL
    default_temperature: float | None = DEFAULT_DEFAULT_TEMPERATURE
    default_thinking_effort: str | None = DEFAULT_DEFAULT_THINKING_EFFORT
    # The project's single source format (GLOSSARY → Source Format): which
    # coding-agent ecosystem its Team agents and project skills come from
    # (".opencode/" vs ".claude/"). Exactly one per project — every consumer
    # (scan, skills, autocomplete, prompt preview) sees only this format's set.
    source_format: str = DEFAULT_PROJECT_SOURCE_FORMAT
    auto_load: list[str] = field(default_factory=list)
    # The Project Tool Whitelist — the hard ceiling for this project's config
    # agents (GLOSSARY → Project Tool Whitelist). Defaults to the base list; an
    # explicit empty list is a real value (every tool off) and is preserved.
    allowed_tools: list[str] = field(default_factory=lambda: list(PROJECT_DEFAULT_ALLOWED_TOOLS))
    # The Project Skill Whitelist as a rule, not a resolved set (decision 3): which
    # bundled and global skills are opted in, and which project skills are exceptionally
    # off. All empty by default → only the project's own scanned skills are active.
    skills_bundled_enabled: list[str] = field(default_factory=list)
    skills_global_enabled: list[str] = field(default_factory=list)
    skills_project_disabled: list[str] = field(default_factory=list)
    # Per-agent overrides keyed by scanned ``agent_id`` → an override object with
    # optional ``model`` (user-facing ``<provider>/<model-id>[::connection]``),
    # ``temperature`` (number), and ``thinking_effort`` (effort string, ``""`` = force
    # provider default). The vBot-owned per-agent override layer (GLOSSARY → Model):
    # data-dir only (never the repo); the resolver applies each override field as the
    # **top** tier of the matching config-agent chain, so an override wins over the
    # repo-declared value. Empty by default. Set/cleared per field through the store
    # (``set_override`` / ``clear_override``), not the generic ``project.set`` field
    # surface.
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable mapping persisted to ``project.json``."""
        return {
            "project_id": self.project_id,
            "display_name": self.display_name,
            "cwd": self.cwd,
            "default_agent": self.default_agent,
            "default_model": self.default_model,
            "default_temperature": self.default_temperature,
            "default_thinking_effort": self.default_thinking_effort,
            "source_format": self.source_format,
            "auto_load": list(self.auto_load),
            "allowed_tools": list(self.allowed_tools),
            "skills_bundled_enabled": list(self.skills_bundled_enabled),
            "skills_global_enabled": list(self.skills_global_enabled),
            "skills_project_disabled": list(self.skills_project_disabled),
            "overrides": {
                agent_id: dict(override) for agent_id, override in self.overrides.items()
            },
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def build_project(
    project_id: str,
    display_name: str,
    cwd: str | os.PathLike[str],
    *,
    default_agent: str = DEFAULT_DEFAULT_AGENT,
    default_model: str = DEFAULT_DEFAULT_MODEL,
    default_temperature: float | None = DEFAULT_DEFAULT_TEMPERATURE,
    default_thinking_effort: str | None = DEFAULT_DEFAULT_THINKING_EFFORT,
    source_format: str = DEFAULT_PROJECT_SOURCE_FORMAT,
    auto_load: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    skills_bundled_enabled: list[str] | None = None,
    skills_global_enabled: list[str] | None = None,
    skills_project_disabled: list[str] | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> Project:
    """Validate fields and construct a :class:`Project` with normalized cwd.

    The ``cwd`` is resolved (symlinks, ``.``/``..``) and stored as an absolute
    path; case is preserved. ``allowed_tools=None`` falls back to the base list
    :data:`PROJECT_DEFAULT_ALLOWED_TOOLS` (an explicit ``[]`` is kept as "every
    tool off"); the skill lists default to empty. Timestamps default to now (UTC
    ISO 8601 with offset). Raises :class:`ProjectError` /
    :class:`InvalidProjectIdError` on bad input.
    """
    validated_id = _validate_project_id(project_id)
    validated_display_name = _normalize_project_display_name(validated_id, display_name)
    validated_cwd = str(_normalize_cwd(cwd))
    validated_default_agent = _validate_optional_string("default_agent", default_agent)
    validated_default_model = _validate_optional_string("default_model", default_model)
    validated_default_temperature = _validate_default_temperature(default_temperature)
    validated_default_thinking_effort = _validate_default_thinking_effort(default_thinking_effort)
    validated_source_format = _validate_source_format(source_format)
    validated_auto_load = _validate_auto_load(auto_load)
    validated_allowed_tools = _validate_allowed_tools(allowed_tools)
    validated_skills_bundled = _validate_string_list(
        "skills_bundled_enabled", skills_bundled_enabled
    )
    validated_skills_global = _validate_string_list("skills_global_enabled", skills_global_enabled)
    validated_skills_disabled = _validate_string_list(
        "skills_project_disabled", skills_project_disabled
    )
    validated_overrides = _validate_overrides(overrides)
    _validate_tool_access_override_ceilings(validated_overrides, validated_allowed_tools)
    now = _utc_now()
    return Project(
        project_id=validated_id,
        display_name=validated_display_name,
        cwd=validated_cwd,
        default_agent=validated_default_agent,
        default_model=validated_default_model,
        default_temperature=validated_default_temperature,
        default_thinking_effort=validated_default_thinking_effort,
        source_format=validated_source_format,
        auto_load=validated_auto_load,
        allowed_tools=validated_allowed_tools,
        skills_bundled_enabled=validated_skills_bundled,
        skills_global_enabled=validated_skills_global,
        skills_project_disabled=validated_skills_disabled,
        overrides=validated_overrides,
        created_at=created_at or now,
        updated_at=updated_at or now,
    )


def project_from_dict(data: dict[str, Any]) -> Project:
    """Build a Project from a mapping already validated by this domain.

    ``validate_project_data`` enforces the field rules at load
    time; this constructor only normalizes shapes (optional-field defaults,
    auto_load list copy), it does not re-validate.
    """
    project_id = cast("str", data["project_id"])
    timestamp_default = _utc_now()
    return Project(
        project_id=project_id,
        display_name=data.get("display_name") or project_id,
        cwd=data["cwd"],
        default_agent=data.get("default_agent") or DEFAULT_DEFAULT_AGENT,
        default_model=data.get("default_model") or DEFAULT_DEFAULT_MODEL,
        default_temperature=data.get("default_temperature", DEFAULT_DEFAULT_TEMPERATURE),
        default_thinking_effort=data.get(
            "default_thinking_effort", DEFAULT_DEFAULT_THINKING_EFFORT
        ),
        source_format=data.get("source_format") or DEFAULT_PROJECT_SOURCE_FORMAT,
        auto_load=list(cast("list[str]", data.get("auto_load") or [])),
        allowed_tools=_allowed_tools_from_data(data.get("allowed_tools")),
        skills_bundled_enabled=list(cast("list[str]", data.get("skills_bundled_enabled") or [])),
        skills_global_enabled=list(cast("list[str]", data.get("skills_global_enabled") or [])),
        skills_project_disabled=list(cast("list[str]", data.get("skills_project_disabled") or [])),
        overrides=_overrides_from_data(data.get("overrides")),
        created_at=data.get("created_at") or timestamp_default,
        updated_at=data.get("updated_at") or timestamp_default,
    )


def _normalize_project_display_name(project_id: str, value: Any) -> str:
    """Use the stable Project id when no display name is configured."""
    if value is None:
        return project_id
    if not isinstance(value, str):
        raise ProjectError("display_name must be a string or null")
    return value if value.strip() else project_id


def _allowed_tools_from_data(value: Any) -> list[str]:
    """Return the persisted Tool Whitelist, defaulting a missing field to the base list.

    An absent field (old ``project.json``) and any non-list value fall back to
    :data:`PROJECT_DEFAULT_ALLOWED_TOOLS` (decision 10), while an explicit empty
    list is preserved as "every tool off" — the ``isinstance`` check is what keeps
    ``[]`` distinct from absent (a plain ``or`` would collapse both to the base
    list). Validation runs before this, so a malformed value is already rejected;
    the defensive fallback only matters for a direct :func:`project_from_dict`.
    """
    if isinstance(value, list):
        return list(value)
    return list(PROJECT_DEFAULT_ALLOWED_TOOLS)


def _normalize_cwd(cwd: str | os.PathLike[str]) -> Any:
    try:
        return normalize_cwd(cwd)
    except ValueError as exc:
        raise ProjectError(f"cwd must be a non-empty path: {exc}") from exc


def _validate_project_id(project_id: Any) -> str:
    if not is_valid_project_id(project_id):
        raise InvalidProjectIdError(
            "Project id must be 1-64 characters using only letters, numbers, hyphen, or underscore"
        )
    return cast("str", project_id)


def _validate_non_empty_string(field_name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectError(f"{field_name} must be a non-empty string")
    return value


def _validate_optional_string(field_name: str, value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProjectError(f"{field_name} must be a string")
    return value


def _validate_default_temperature(value: Any) -> float | None:
    """Validate the optional project-default temperature via the canonical rule.

    Delegates to ``core.settings.validate_temperature`` (the single sampling-range
    authority) so the project default obeys the same ``[0, 2]`` bounds as an
    agent's; ``None`` means "no project default". The settings error is rewrapped
    as a :class:`ProjectError`, mirroring how the agent store wraps it.
    """
    try:
        return validate_temperature(value, label="default_temperature", allow_none=True)
    except SettingsValidationError as exc:
        raise ProjectError(str(exc)) from exc


def _validate_default_thinking_effort(value: Any) -> str | None:
    """Validate the optional project-default thinking effort via the canonical rule.

    Delegates to ``core.settings.validate_thinking_effort`` so the project default
    accepts exactly the same effort ladder as an agent's, including ``""`` as the
    explicit "provider default" value; ``None`` means "no project default".
    """
    try:
        return validate_thinking_effort(value, label="default_thinking_effort", allow_none=True)
    except SettingsValidationError as exc:
        raise ProjectError(str(exc)) from exc


def _validate_source_format(value: Any) -> str:
    """Validate the project source format against the canonical vocabulary."""
    if value not in PROJECT_SOURCE_FORMATS:
        choices = ", ".join(PROJECT_SOURCE_FORMATS)
        raise ProjectError(f"source_format must be one of: {choices}")
    return cast("str", value)


def _validate_auto_load(auto_load: list[str] | None) -> list[str]:
    if auto_load is None:
        return []
    if not isinstance(auto_load, list):
        raise ProjectError("auto_load must be a list of strings")
    for item in auto_load:
        if not isinstance(item, str) or not item.strip():
            raise ProjectError("auto_load entries must be non-empty strings")
    return list(auto_load)


def _validate_allowed_tools(allowed_tools: list[str] | None) -> list[str]:
    """Validate the Tool Whitelist; ``None`` falls back to the base list.

    An explicit empty list is a valid value (every tool off) and is kept as-is,
    so only ``None`` (caller said nothing) seeds the base ceiling.
    """
    if allowed_tools is None:
        return list(PROJECT_DEFAULT_ALLOWED_TOOLS)
    validated = _validate_string_list("allowed_tools", allowed_tools)
    if PROJECT_TOOL_ALLOWLIST_WILDCARD in validated:
        raise ProjectError("allowed_tools cannot contain the all-tools wildcard '*' for a Project")
    return validated


def _validate_string_list(field_name: str, values: list[str] | None) -> list[str]:
    """Validate an optional list-of-non-empty-strings field; ``None`` → ``[]``."""
    if values is None:
        return []
    if not isinstance(values, list):
        raise ProjectError(f"{field_name} must be a list of strings")
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise ProjectError(f"{field_name} entries must be non-empty strings")
    return list(values)


def _validate_overrides(value: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Validate the per-agent override map; ``None`` → ``{}``.

    Each key is a non-empty ``agent_id`` string; each value is an override object with
    any of the optional fields in :data:`OVERRIDE_FIELDS`. ``model`` is a non-empty
    model string (shape only, exactly like ``default_model`` — the model's
    *configured-ness* is the ``/model`` set-time gate, not a file-load concern, so a
    credential going away never makes an existing ``project.json`` fail to load).
    ``temperature`` and ``thinking_effort`` reuse the canonical agent field validators,
    so their ranges and effort ladder can never drift from an agent's;
    ``thinking_effort = ""`` is a real value meaning "force provider default". An empty
    override object (no fields) is rejected — an override with no field carries nothing.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProjectError("overrides must be an object")
    validated: dict[str, dict[str, Any]] = {}
    for agent_id, override in value.items():
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ProjectError("overrides keys must be non-empty agent id strings")
        validated[agent_id] = _validate_override(agent_id, override)
    return validated


def _validate_override(agent_id: str, override: Any) -> dict[str, Any]:
    """Validate one agent's override object, returning a normalized copy."""
    if not isinstance(override, dict):
        raise ProjectError(f"overrides[{agent_id!r}] must be an object")
    unknown = sorted(set(override) - OVERRIDE_FIELDS)
    if unknown:
        raise ProjectError(f"overrides[{agent_id!r}] has unknown fields: {', '.join(unknown)}")
    if not override:
        raise ProjectError(f"overrides[{agent_id!r}] must set at least one field")
    validated: dict[str, Any] = {}
    if "model" in override:
        model = override["model"]
        if not isinstance(model, str) or not model.strip():
            raise ProjectError(f"overrides[{agent_id!r}].model must be a non-empty model string")
        validated["model"] = model
    if "temperature" in override:
        validated["temperature"] = _validate_override_temperature(agent_id, override["temperature"])
    if "thinking_effort" in override:
        validated["thinking_effort"] = _validate_override_thinking_effort(
            agent_id, override["thinking_effort"]
        )
    if "compaction_policy" in override:
        from core.settings.normalizers import normalize_compaction_policy

        try:
            validated["compaction_policy"] = normalize_compaction_policy(
                override["compaction_policy"]
            )
        except Exception as exc:
            raise ProjectError(str(exc)) from exc
    if "tool_access" in override:
        try:
            policy = _normalize_tool_access_policy(override["tool_access"])
        except ValueError as exc:
            raise ProjectError(str(exc)) from exc
        validated["tool_access"] = policy.to_dict()
    return validated


def _validate_tool_access_override_ceilings(
    overrides: Mapping[str, Mapping[str, Any]],
    allowed_tools: list[str],
) -> None:
    """Reject selected Project Agent Tools outside the Project Tool Whitelist."""

    ceiling = set(allowed_tools)
    for agent_id, override in overrides.items():
        raw_policy = override.get("tool_access")
        if raw_policy is None:
            continue
        policy = _normalize_tool_access_policy(raw_policy)
        outside = sorted(set(policy.allowed) - ceiling)
        if outside:
            names = ", ".join(outside)
            raise ProjectError(
                f"overrides[{agent_id!r}].tool_access.allowed contains Tools outside "
                f"the Project Tool Whitelist: {names}"
            )


def _validate_override_temperature(agent_id: str, value: Any) -> float:
    """Validate an override ``temperature`` via the canonical rule (``None`` is not one)."""
    try:
        temperature = validate_temperature(
            value, label=f"overrides[{agent_id!r}].temperature", allow_none=False
        )
    except SettingsValidationError as exc:
        raise ProjectError(str(exc)) from exc
    return cast("float", temperature)


def _validate_override_thinking_effort(agent_id: str, value: Any) -> str:
    """Validate an override ``thinking_effort`` via the canonical rule (``""`` allowed)."""
    try:
        effort = validate_thinking_effort(
            value, label=f"overrides[{agent_id!r}].thinking_effort", allow_none=False
        )
    except SettingsValidationError as exc:
        raise ProjectError(str(exc)) from exc
    return cast("str", effort)


def _overrides_from_data(value: Any) -> dict[str, dict[str, Any]]:
    """Return the persisted override map from validated data, normalizing shapes.

    The Projects-owned schema validator runs before this, so a malformed value is
    already rejected; this only copies each override object. A missing field defaults
    to an empty map.
    """
    if not isinstance(value, dict):
        return {}
    return {
        agent_id: dict(cast("dict[str, Any]", override))
        for agent_id, override in value.items()
        if isinstance(override, dict)
    }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _normalize_tool_access_policy(value: Any) -> Any:
    """Import the Tools-owned policy validator lazily to avoid package cycles."""

    from core.tools.availability import normalize_tool_access

    return normalize_tool_access(value)
