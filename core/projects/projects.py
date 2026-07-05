"""Project entity, ``project.json`` schema, and field validation.

This is the public/main file of the ``core/projects`` deep module. A Project is
a first-class entity (see GLOSSARY → Project): a stable ``project_id`` slug, a
changeable ``display_name``, the repo ``cwd`` that tools resolve relative paths
against, optional project-default agent/model pointers, and an ordered
``auto_load`` file list. The minimal valid Project is just a cwd — team and
auto-load files are optional. ``AGENTS.md`` (the tool-neutral project-instruction
convention) is seeded as the first ``auto_load`` entry at creation, then a normal
removable entry — vBot does not special-case it at render time.

Field rules are enforced once by ``core.settings.validate_project_data`` at load
time (the central validator), the same way Agents validate through the settings
domain. This module owns the entity shape and the create-time field validation;
the on-disk anchor lifecycle and CRUD live in ``core/projects/store.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from core.projects.paths import normalize_cwd
from core.settings import (
    SettingsValidationError,
    is_valid_project_id,
    validate_temperature,
    validate_thinking_effort,
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
# The default-off-but-UI-toggleable tools (``session_search``, ``image_generation``,
# ``text_to_speech``, ``cron``, ``channel_send``, the Home-Assistant tools) are
# deliberately absent. ``memory`` (runtime-derived from memory mode) and the
# identity-only ``skill_manage`` (writes to an identity agent's private skill home) are
# never project tools at all; ``skill`` itself is an ordinary project tool, default-on.
PROJECT_DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = (
    "read",
    "write",
    "edit",
    "glob",
    "grep",
    "bash",
    "process",
    "web_fetch",
    "web_search",
    "status",
    "subagent",
    "skill",
)

# The optional fields a per-agent Pin may carry. Each maps to the top tier of the
# matching config-agent resolver chain (model / temperature / thinking effort).
PIN_FIELDS: frozenset[str] = frozenset({"model", "temperature", "thinking_effort"})

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
    # Per-agent Pins keyed by scanned ``agent_id`` → a pin object with optional
    # ``model`` (user-facing ``<provider>/<model-id>[::connection]``), ``temperature``
    # (number), and ``thinking_effort`` (effort string, ``""`` = force provider
    # default). The vBot-owned per-agent override layer (GLOSSARY → Model): data-dir
    # only (never the repo); the resolver applies each pinned field as the **top**
    # tier of the matching config-agent chain, so a pin wins over the repo-declared
    # value. Empty by default. Set/cleared per field through the store
    # (``set_pin`` / ``clear_pin``), not the generic ``project.set`` field surface.
    pins: dict[str, dict[str, Any]] = field(default_factory=dict)

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
            "auto_load": list(self.auto_load),
            "allowed_tools": list(self.allowed_tools),
            "skills_bundled_enabled": list(self.skills_bundled_enabled),
            "skills_global_enabled": list(self.skills_global_enabled),
            "skills_project_disabled": list(self.skills_project_disabled),
            "pins": {agent_id: dict(pin) for agent_id, pin in self.pins.items()},
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
    auto_load: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    skills_bundled_enabled: list[str] | None = None,
    skills_global_enabled: list[str] | None = None,
    skills_project_disabled: list[str] | None = None,
    pins: dict[str, dict[str, Any]] | None = None,
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
    validated_display_name = _validate_non_empty_string("display_name", display_name)
    validated_cwd = str(_normalize_cwd(cwd))
    validated_default_agent = _validate_optional_string("default_agent", default_agent)
    validated_default_model = _validate_optional_string("default_model", default_model)
    validated_default_temperature = _validate_default_temperature(default_temperature)
    validated_default_thinking_effort = _validate_default_thinking_effort(default_thinking_effort)
    validated_auto_load = _validate_auto_load(auto_load)
    validated_allowed_tools = _validate_allowed_tools(allowed_tools)
    validated_skills_bundled = _validate_string_list(
        "skills_bundled_enabled", skills_bundled_enabled
    )
    validated_skills_global = _validate_string_list("skills_global_enabled", skills_global_enabled)
    validated_skills_disabled = _validate_string_list(
        "skills_project_disabled", skills_project_disabled
    )
    validated_pins = _validate_pins(pins)
    now = _utc_now()
    return Project(
        project_id=validated_id,
        display_name=validated_display_name,
        cwd=validated_cwd,
        default_agent=validated_default_agent,
        default_model=validated_default_model,
        default_temperature=validated_default_temperature,
        default_thinking_effort=validated_default_thinking_effort,
        auto_load=validated_auto_load,
        allowed_tools=validated_allowed_tools,
        skills_bundled_enabled=validated_skills_bundled,
        skills_global_enabled=validated_skills_global,
        skills_project_disabled=validated_skills_disabled,
        pins=validated_pins,
        created_at=created_at or now,
        updated_at=updated_at or now,
    )


def project_from_dict(data: dict[str, Any]) -> Project:
    """Build a Project from a mapping already validated by the central validator.

    ``core.settings.validate_project_data`` enforces the field rules at load
    time; this constructor only normalizes shapes (optional-field defaults,
    auto_load list copy), it does not re-validate.
    """
    return Project(
        project_id=data["project_id"],
        display_name=data["display_name"],
        cwd=data["cwd"],
        default_agent=data.get("default_agent", DEFAULT_DEFAULT_AGENT),
        default_model=data.get("default_model", DEFAULT_DEFAULT_MODEL),
        default_temperature=data.get("default_temperature", DEFAULT_DEFAULT_TEMPERATURE),
        default_thinking_effort=data.get(
            "default_thinking_effort", DEFAULT_DEFAULT_THINKING_EFFORT
        ),
        auto_load=list(cast("list[str]", data.get("auto_load") or [])),
        allowed_tools=_allowed_tools_from_data(data.get("allowed_tools")),
        skills_bundled_enabled=list(cast("list[str]", data.get("skills_bundled_enabled") or [])),
        skills_global_enabled=list(cast("list[str]", data.get("skills_global_enabled") or [])),
        skills_project_disabled=list(cast("list[str]", data.get("skills_project_disabled") or [])),
        pins=_pins_from_data(data.get("pins")),
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )


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
    return _validate_string_list("allowed_tools", allowed_tools)


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


def _validate_pins(value: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Validate the per-agent Pin map; ``None`` → ``{}``.

    Each key is a non-empty ``agent_id`` string; each value is a pin object with any
    of the optional fields in :data:`PIN_FIELDS`. ``model`` is a non-empty model
    string (shape only, exactly like ``default_model`` — the model's *configured-ness*
    is the ``/model`` set-time gate, not a file-load concern, so a credential going
    away never makes an existing ``project.json`` fail to load). ``temperature`` and
    ``thinking_effort`` reuse the canonical agent field validators, so their ranges
    and effort ladder can never drift from an agent's; ``thinking_effort = ""`` is a
    real value meaning "force provider default". An empty pin object (no fields) is
    rejected — a pin with no field carries nothing.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProjectError("pins must be an object")
    validated: dict[str, dict[str, Any]] = {}
    for agent_id, pin in value.items():
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ProjectError("pins keys must be non-empty agent id strings")
        validated[agent_id] = _validate_pin(agent_id, pin)
    return validated


def _validate_pin(agent_id: str, pin: Any) -> dict[str, Any]:
    """Validate one agent's pin object, returning a normalized copy."""
    if not isinstance(pin, dict):
        raise ProjectError(f"pins[{agent_id!r}] must be an object")
    unknown = sorted(set(pin) - PIN_FIELDS)
    if unknown:
        raise ProjectError(f"pins[{agent_id!r}] has unknown fields: {', '.join(unknown)}")
    if not pin:
        raise ProjectError(f"pins[{agent_id!r}] must set at least one field")
    validated: dict[str, Any] = {}
    if "model" in pin:
        model = pin["model"]
        if not isinstance(model, str) or not model.strip():
            raise ProjectError(f"pins[{agent_id!r}].model must be a non-empty model string")
        validated["model"] = model
    if "temperature" in pin:
        validated["temperature"] = _validate_pin_temperature(agent_id, pin["temperature"])
    if "thinking_effort" in pin:
        validated["thinking_effort"] = _validate_pin_thinking_effort(
            agent_id, pin["thinking_effort"]
        )
    return validated


def _validate_pin_temperature(agent_id: str, value: Any) -> float:
    """Validate a pin ``temperature`` via the canonical rule (``None`` is not a pin)."""
    try:
        temperature = validate_temperature(
            value, label=f"pins[{agent_id!r}].temperature", allow_none=False
        )
    except SettingsValidationError as exc:
        raise ProjectError(str(exc)) from exc
    return cast("float", temperature)


def _validate_pin_thinking_effort(agent_id: str, value: Any) -> str:
    """Validate a pin ``thinking_effort`` via the canonical rule (``""`` allowed)."""
    try:
        effort = validate_thinking_effort(
            value, label=f"pins[{agent_id!r}].thinking_effort", allow_none=False
        )
    except SettingsValidationError as exc:
        raise ProjectError(str(exc)) from exc
    return cast("str", effort)


def _pins_from_data(value: Any) -> dict[str, dict[str, Any]]:
    """Return the persisted pin map from validated data, normalizing shapes.

    Validation runs before this (central validator), so a malformed value is
    already rejected; this only copies each pin object. A missing field defaults to
    an empty map.
    """
    if not isinstance(value, dict):
        return {}
    return {
        agent_id: dict(cast("dict[str, Any]", pin))
        for agent_id, pin in value.items()
        if isinstance(pin, dict)
    }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
