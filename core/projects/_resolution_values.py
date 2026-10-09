"""Project capability ceilings, scalar fallback and effective provenance."""

from __future__ import annotations

import copy
from dataclasses import replace
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any, Literal

from core.projects._runtime_agent import (
    ConfigAgent,
)
from core.projects.sources import (
    AgentProfile,
)
from core.settings import AgentDefaults
from core.skills import WILDCARD_ALLOWLIST

if TYPE_CHECKING:
    from typing import Any

    from core.projects.projects import Project
    from core.tools.availability import ToolAccess


def _project_agent_tool_access(project: Project, scanned: AgentProfile) -> ToolAccess:
    """Return the Project-scoped Tool policy for one Project Agent.

    A vBot override replaces the repository-scanned Tool policy. ``all`` is
    materialized against the Project Tool Whitelist so the shared runtime resolver
    never interprets it as every Tool registered in the whole vBot instance.
    """

    from core.tools.availability import ToolAccess, normalize_tool_access

    raw_override = project.overrides.get(scanned.agent_id, {}).get("tool_access")
    if raw_override is not None:
        override = normalize_tool_access(raw_override)
        if override.mode == "none":
            return override
        if override.mode == "all":
            return ToolAccess(
                mode="selected",
                allowed=tuple(project.allowed_tools),
                denied=override.denied,
                granted=override.granted,
            )
        return replace(
            override,
            allowed=tuple(name for name in override.allowed if name in project.allowed_tools),
        )

    if scanned.unavailable_reason:
        return ToolAccess(mode="none")
    return profile_tool_access(scanned, tuple(project.allowed_tools))


def profile_tool_access(scanned: AgentProfile, selection: tuple[str, ...]) -> ToolAccess:
    from core.tools.availability import ToolAccess

    if scanned.unavailable_reason:
        return ToolAccess(mode="none")
    allowed = set(selection)
    if scanned.allowed_tools is not None:
        allowed &= scanned.allowed_tools
    for tool in tuple(allowed):
        for rule in scanned.tool_rules:
            if fnmatchcase(tool, rule.pattern):
                if rule.allowed:
                    allowed.add(tool)
                else:
                    allowed.discard(tool)
    allowed -= scanned.denied_tools
    # A Profile selects the Agent's own Tools; Tools that follow them and the
    # Session's grants (``message_parent`` for a delegated Run) activate as usual.
    return ToolAccess(
        mode="selected",
        allowed=tuple(tool for tool in selection if tool in allowed),
        denied=tuple(sorted(scanned.denied_tools)),
    )


def effective_project_allowed_skills(
    project: Project, project_skill_names: frozenset[str]
) -> list[str]:
    """Return the effective names from the Project Skill Whitelist rule.

    ``(project skills ∪ skills_bundled_enabled ∪ skills_global_enabled) −
    (skills_project_disabled ∩ project skills)`` — the project's own scanned skills
    are active by default, plus any bundled or global skills explicitly opted in
    (decision 3). Two hardenings on top of the plain union:

    - **A disabled project skill name is off entirely.** The merged registry
      resolves a name collision to the project's own copy, so leaving the name
      allowed through a same-named bundled/global opt-in would silently serve the
      disabled project skill. A disabled name that is *not* a project skill stays
      inert (the opt-ins keep working).
    - **The literal wildcard is dropped.** This list is a resolved set of exact
      names; a repo-scanned skill *named* ``*`` (the lenient loader accepts that
      with a warning) must not smuggle the ``allowed_skills`` wildcard past the
      whitelist and expose the whole global pool to a project agent.

    This pool is Project-derived; Profile rules can narrow it per Agent.
    Config-Agent resolution and Identity Project Context both use
    this function so their interpretation cannot drift. The result is sorted for
    determinism; ``filter_allowed`` harmlessly ignores any name that no longer
    resolves to a loadable skill.
    """
    disabled = set(project.skills_project_disabled)
    enabled_bundled = set(project.skills_bundled_enabled)
    enabled_global = set(project.skills_global_enabled)
    allowed = set(project_skill_names) | enabled_bundled | enabled_global
    allowed -= disabled & project_skill_names
    allowed.discard(WILDCARD_ALLOWLIST)
    return sorted(allowed)


def _effective_allowed_agents(scanned: AgentProfile, team: list[AgentProfile]) -> list[str]:
    """Materialize additional targets against the Team; self is always implicit."""
    allowed: list[str] = []
    for member in team:
        if member.agent_id == scanned.agent_id or member.unavailable_reason:
            continue
        member_allowed = True
        for rule in scanned.agent_target_rules:
            if fnmatchcase(member.agent_id, rule.pattern):
                member_allowed = rule.allowed
        if member_allowed:
            allowed.append(member.agent_id)
    return sorted(allowed)


def _project_agent_tools(tool_access: ToolAccess, allowed_agents: list[str]) -> dict[str, Any]:
    """Project effective targets into the optional root Tool-settings block."""
    if (
        tool_access.mode == "none"
        or "subagent" not in tool_access.allowed
        or "subagent" in tool_access.denied
    ):
        return {}
    return {"subagent": {"allowed_agents": allowed_agents}}


def _build_config_agent(
    scanned: AgentProfile,
    resolved_model: str,
    resolved_temperature: float | None,
    resolved_thinking_effort: str | None,
    tool_access: ToolAccess,
    allowed_skills: list[str],
    tools: dict[str, Any],
    compaction_policy: Any,
    *,
    project_id: str | None = None,
    resolved_top_p: float | None = None,
    tool_loading: Any = None,
) -> ConfigAgent:
    return ConfigAgent(
        id=scanned.agent_id,
        project_id=project_id,
        name=scanned.display_name,
        model=resolved_model,
        temperature=resolved_temperature,
        top_p=resolved_top_p,
        thinking_effort=resolved_thinking_effort,
        body=scanned.body,
        source_path=scanned.source_path,
        source=scanned.source,
        preload_skills=scanned.preload_skills,
        model_inherit=scanned.model == "inherit",
        tool_access=tool_access,
        allowed_skills=allowed_skills,
        tools=tools,
        compaction_policy=(
            dict(compaction_policy) if isinstance(compaction_policy, dict) else None
        ),
        tool_loading=copy.deepcopy(tool_loading) if isinstance(tool_loading, dict) else None,
    )


SamplingField = Literal["temperature", "top_p"]


def _resolve_sampling(
    field: SamplingField,
    scanned: AgentProfile,
    project: Project,
    global_defaults: AgentDefaults,
) -> float | None:
    """Resolve ``temperature`` or ``top_p``: override → agent → project default → global default.

    The first tier that carries a number wins; ``0.0`` is a real value (the
    sampling floor) and stops the chain. Falling through every tier yields
    ``None`` → the field is dropped at the wire and the provider default applies.
    """
    value: float | None = _config_sampling_source(field, project, scanned, global_defaults)["value"]
    return value


def _resolve_thinking_effort(
    scanned: AgentProfile, project: Project, global_defaults: AgentDefaults
) -> str | None:
    """Resolve thinking effort: override → agent → project default → global default → None.

    The first tier that is not ``None`` wins. ``""`` is a real value meaning
    "provider default" and stops the chain, so an override (or project
    ``default_thinking_effort``) of ``""`` blocks the lower tiers (forces the
    provider default) while ``None`` lets them through. An override present (not
    ``None``, including ``""``) is the top tier. Falling through every tier yields ``None``.
    """
    candidates = (
        _overridden_thinking_effort(project, scanned.agent_id),
        scanned.thinking_effort,
        project.default_thinking_effort,
        global_defaults.thinking_effort,
    )
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return None


def _config_sampling_source(
    field: SamplingField,
    project: Project,
    scanned: AgentProfile,
    global_defaults: AgentDefaults,
) -> dict[str, Any]:
    """Return the effective ``temperature`` or ``top_p`` + source for a config agent.

    Same chain as :func:`_resolve_sampling` but reporting which tier won
    (``override`` / ``agent`` / ``project_default`` / ``global_default``).
    """
    tiers = (
        ("override", _number(project.overrides.get(scanned.agent_id, {}).get(field))),
        ("agent", _number(getattr(scanned, field))),
        ("project_default", _number(getattr(project, f"default_{field}"))),
        ("global_default", _number(getattr(global_defaults, field))),
    )
    for source, candidate in tiers:
        if candidate is not None:
            return {"value": candidate, "source": source}
    return {"value": None, "source": None}


def _config_thinking_effort_source(
    project: Project, scanned: AgentProfile, global_defaults: AgentDefaults
) -> dict[str, Any]:
    """Return the effective thinking effort + source for a config agent.

    Same chain as :func:`_resolve_thinking_effort` (override → agent → project default →
    global default) but reporting which tier won; ``""`` is a real stopping value.
    """
    tiers = (
        ("override", _overridden_thinking_effort(project, scanned.agent_id)),
        ("agent", scanned.thinking_effort),
        ("project_default", project.default_thinking_effort),
        ("global_default", _global_default_thinking_effort(global_defaults)),
    )
    for source, candidate in tiers:
        if candidate is not None:
            return {"value": candidate, "source": source}
    return {"value": None, "source": None}


def _identity_string_source(own_value: str, default_value: Any) -> dict[str, Any]:
    """Return the identity effective value + source for a string field.

    Mirrors ``core.agents._config.apply_defaults``: the persisted own value wins unless it is
    ``""``, in which case the global default applies when present. Source is
    ``"agent"`` / ``"global_default"`` / ``None``.
    """
    if own_value != "":
        return {"value": own_value, "source": "agent"}
    if default_value is not None and isinstance(default_value, str):
        return {"value": default_value, "source": "global_default"}
    return {"value": None, "source": None}


def _identity_string_list_source(own_value: list[str], default_value: Any) -> dict[str, Any]:
    """Return the identity effective value + source for a string-list field.

    Mirrors ``core.agents._config.apply_defaults`` for ``fallback_models``: the persisted
    own value wins unless it is empty, in which case the global default applies
    when present. Source is ``"agent"`` / ``"global_default"`` / ``None``.
    """
    if own_value:
        return {"value": list(own_value), "source": "agent"}
    if (
        default_value is not None
        and isinstance(default_value, list)
        and all(isinstance(item, str) for item in default_value)
    ):
        return {"value": list(default_value), "source": "global_default"}
    return {"value": None, "source": None}


def _identity_optional_source(own_value: Any, default_value: Any) -> dict[str, Any]:
    """Return the identity effective value + source for a nullable field.

    Mirrors ``core.agents._config.apply_defaults``: the persisted own value wins unless it is
    ``None``, in which case the global default applies when present. Source is
    ``"agent"`` / ``"global_default"`` / ``None``. A present own value (including
    ``0.0`` for temperature or ``""`` for thinking effort) stops the chain.
    """
    if own_value is not None:
        return {"value": own_value, "source": "agent"}
    if default_value is not None:
        return {"value": default_value, "source": "global_default"}
    return {"value": None, "source": None}


def _number(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _global_default_thinking_effort(global_defaults: AgentDefaults) -> str | None:
    value = global_defaults.thinking_effort
    return value if isinstance(value, str) else None


def _config_tool_access_source(project: Project, scanned: AgentProfile) -> dict[str, Any]:
    """Return the editable Project Agent Tool policy and its winning source."""

    from core.tools.availability import normalize_tool_access

    raw_override = project.overrides.get(scanned.agent_id, {}).get("tool_access")
    if raw_override is not None:
        return {
            "value": normalize_tool_access(raw_override).to_dict(),
            "source": "override",
        }
    policy = _project_agent_tool_access(project, scanned)
    return {"value": policy.to_dict(), "source": "agent"}


def _overridden_model(project: Project, agent_id: str) -> str:
    """Return the agent's overridden model, or ``""`` when not overridden."""
    return str(project.overrides.get(agent_id, {}).get("model", "") or "")


def _overridden_thinking_effort(project: Project, agent_id: str) -> str | None:
    """Return the agent's overridden thinking effort (``""`` allowed), or ``None`` when not set."""
    value = project.overrides.get(agent_id, {}).get("thinking_effort")
    return value if isinstance(value, str) else None
