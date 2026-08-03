"""Project RPC handlers: ``project.add/list/show/set/rm`` plus the scan preview.

A Project is a first-class backend entity (see GLOSSARY → Project): a stable
``project_id`` slug, a changeable display name, a repo ``cwd``, optional
project-default agent/model pointers, an ``auto_load`` file list, and a Team
scanned live from the repo. These handlers are the agent-facing surface over the
:class:`core.projects.ProjectStore` anchor lifecycle and the
:class:`core.projects.AgentResolver` scan preview.

Addressing is Option 1 (plan requirement): the ``project_id`` is an explicit
param, never an ``agent@projekt`` string parsed here. The ``agent@projekt`` outer
spelling belongs to the session/chat RPC entry, not to this module.

**Scan preview.** ``project.add`` and ``project.show`` return a ``scan`` block —
the Team (callable agents discovered in the repo) plus the report (everything
unclean under what exists: bad/unconfigured model, slug collision, unslugifiable
name, or a persisted Tool Whitelist entry unavailable in the live registry).
``add`` returns it for the just-created project; ``show`` re-scans live (the repo
is the source of truth, no copy drift). An empty folder yields an empty team and
a clean report — that is a valid Project, not an error.

**Remove lock.** ``project.rm`` archives the anchor (never the repo) unless a
Project is in use: an atomic Run Admission Guard covers Project-anchored and
Rooted-Agent work (``RPC_ERROR_PROJECT_BUSY``), while the Agent-reference lock
covers cron references and rooted-Agent updates (``RPC_ERROR_PROJECT_IN_USE``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.automation.bootstrap import TERMINAL_BOOTSTRAP_STATUSES
from core.automation.cron import TERMINAL_CRON_JOB_STATUSES
from core.projects import (
    Project,
    cwd_exists,
    project_tool_configurability_reason,
    slugify_project_id,
)
from core.projects.projects import OVERRIDE_FIELDS
from core.projects.scan_report import FindingType, ScanFinding, ScanReport
from core.projects.scanners import detect_project_formats
from core.projects.scanners.base import ProjectFormatDetection, ScannedAgent, ScanResult
from core.runs import RunAdmissionBlockedError
from core.settings import (
    DEFAULT_PROJECT_SOURCE_FORMAT,
    PROJECT_SOURCE_FORMATS,
    PROJECT_TOOL_ALLOWLIST_WILDCARD,
    SettingsValidationError,
    validate_temperature,
    validate_thinking_effort,
)
from core.skills import SKILL_ORIGIN_GLOBAL
from core.utils.logging import get_logger
from server.events import RESOURCE_KIND_AGENTS, RESOURCE_KIND_PROJECTS
from server.rpc.agent_refs import _agent_reference_lock
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import (
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_PROJECT_BUSY,
    RPC_ERROR_PROJECT_IN_USE,
    RpcError,
)
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.runtime_access import _state_chat_runs
from server.rpc.validation import (
    _optional_bool,
    _optional_string,
    _reject_unsupported,
    _required_string,
)

JsonObject = dict[str, Any]
_LOGGER = get_logger("server.rpc.projects")
_MISSING = object()

# A bare cwd is a valid Project (GLOSSARY → Project; plan: "Minimal-Projekt = nur
# eine cwd"): the chosen format location's presence is surfaced in the scan
# preview's Team, never a hard add-time requirement, so add only validates that
# the folder exists and is not already claimed. ``source_format`` is optional —
# absent, it is auto-detected from the repo (see ``_auto_detect_source_format``).
_ADD_FIELDS = frozenset(
    {
        "cwd",
        "display_name",
        "default_agent",
        "default_model",
        "default_temperature",
        "default_thinking_effort",
        "source_format",
        "auto_load",
    }
)
_SET_MUTABLE_FIELDS = frozenset(
    {
        "cwd",
        "display_name",
        "default_agent",
        "default_model",
        "default_temperature",
        "default_thinking_effort",
        "source_format",
        "auto_load",
        "allowed_tools",
        "skills_bundled_enabled",
        "skills_global_enabled",
        "skills_project_disabled",
    }
)


def _projects(state: Any) -> Any:
    return state.runtime.projects


def _agent_resolver(state: Any) -> Any:
    return state.runtime.agent_resolver


def _invalidate_project_caches(state: Any, project_id: str) -> None:
    """Drop both per-project caches that hang off a project's cwd/repo.

    The resolver's Team-scan cache and the runtime's project-skill bundle are both
    keyed on a project's repo, so any operation that re-points or drops that repo
    must invalidate them **together** — a surviving half would resolve the
    project's agents against the old repo's Team or skills. The skill half is
    guarded with ``getattr`` so a minimal runtime without the skill seam degrades
    cleanly, mirroring ``_project_skill_pool``.
    """
    _agent_resolver(state).invalidate_team_cache(project_id)
    invalidate_project_skills = getattr(state.runtime, "invalidate_project_skills", None)
    if callable(invalidate_project_skills):
        invalidate_project_skills(project_id)


def _add_project(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, _ADD_FIELDS, "project.add")

    cwd = _required_string(params, "cwd")
    if not cwd_exists(cwd):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.cwd is not an existing directory: {cwd}",
        )

    display_name = _optional_string(params, "display_name")
    default_agent = _optional_string(params, "default_agent")
    default_model = _optional_string(params, "default_model")
    default_temperature = (
        _validate_default_temperature(params["default_temperature"])
        if "default_temperature" in params
        else None
    )
    default_thinking_effort = (
        _validate_default_thinking_effort(params["default_thinking_effort"])
        if "default_thinking_effort" in params
        else None
    )
    source_format = (
        _validated_source_format(params["source_format"])
        if "source_format" in params
        else _auto_detect_source_format(cwd)
    )
    auto_load = _optional_auto_load(params)
    resolved_display_name = display_name or _display_name_from_cwd(cwd)
    project_id = _slug_from_display_name(resolved_display_name)

    try:
        project = _projects(state).create(
            project_id,
            resolved_display_name,
            cwd,
            default_agent=default_agent or "",
            default_model=default_model or "",
            default_temperature=default_temperature,
            default_thinking_effort=default_thinking_effort,
            source_format=source_format,
            auto_load=auto_load,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    scan = _scan_preview(state, project)
    publish_resource_changed(state, RESOURCE_KIND_PROJECTS)
    _LOGGER.info(
        "Project added (project=%s source_format=%s)",
        project.project_id,
        project.source_format,
    )
    return {"project": _project_response(project), "scan": scan}


def _list_projects(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "project.list does not accept params")

    try:
        projects = _projects(state).list()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"projects": [_project_response(project) for project in projects]}


def _show_project(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"project_id"}, "project.show")

    project_id = _required_string(params, "project_id")
    try:
        project = _projects(state).get(project_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    # Open is a full per-project cache-refresh point. The Team is re-scanned on
    # every show, so drop both per-project caches first so the skill pool — and the
    # next resolve's effective skills — reflect the current repo together: a skill
    # newly added under .opencode/skills surfaces here just like a newly added repo
    # agent does. Runs never call project.show, so per-run caching is unaffected.
    # A show is also the moment to pick up hand-edited *global* skills: the global
    # registry is loaded once at startup with no filesystem watcher, so reload it
    # from disk here — otherwise a skill dropped into the global skills folder never
    # appears in the editor's opt-in pool. Guarded so a minimal runtime degrades.
    reload_skills = getattr(state.runtime, "reload_skills", None)
    if callable(reload_skills):
        reload_skills()
    _invalidate_project_caches(state, project_id)
    scan = _scan_preview(state, project)
    return {"project": _project_response(project), "scan": scan}


def _set_project(state: Any, params: JsonObject) -> JsonObject:
    project_id = _required_string(params, "project_id")
    unsupported_fields = sorted(set(params) - {"project_id"} - _SET_MUTABLE_FIELDS)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported project.set fields: {', '.join(unsupported_fields)}",
        )

    changes = _set_changes(params)
    if not changes:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST, "project.set requires at least one field to change"
        )

    if "cwd" in changes and not cwd_exists(changes["cwd"]):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.cwd is not an existing directory: {changes['cwd']}",
        )

    try:
        current_project = _projects(state).get(project_id)
        if "allowed_tools" in changes:
            _validate_project_tool_change(
                state,
                current_project,
                changes["allowed_tools"],
            )
        project = _projects(state).update(project_id, **changes)
        changed_fields = sorted(
            field
            for field in changes
            if getattr(current_project, field, None) != getattr(project, field, None)
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    # A cwd change re-points the repo and a source_format change re-points which
    # of its directories count, so the live Team and the project's own skills can
    # both change — drop the per-project caches so the returned report and every
    # later resolve see the new ground truth. Any other change (e.g. a whitelist
    # edit) deliberately does not invalidate: project.json is read fresh per
    # resolve and the skill cache holds only the file pool, not the whitelist rule.
    if "cwd" in changes or "source_format" in changes:
        _invalidate_project_caches(state, project_id)
    scan = _scan_preview(state, project)
    publish_resource_changed(state, RESOURCE_KIND_PROJECTS)
    if changed_fields:
        _LOGGER.info(
            "Project updated (project=%s fields=%s)",
            project_id,
            ",".join(changed_fields),
        )
    return {"project": _project_response(project), "scan": scan}


def _set_override(state: Any, params: JsonObject) -> JsonObject:
    """Override one field (``model`` / ``temperature`` / ``thinking_effort``) for an agent.

    Validates the field name and its value before the store write: ``model`` through
    the same usable-model check the ``/model`` command uses (configured here + any
    pinned ``::connection`` allowed), ``temperature`` / ``thinking_effort`` through
    the canonical agent field validators. Returns the refreshed project + scan
    (``clear_override`` returns the same shape). No cache invalidation is needed —
    ``project.json`` is read fresh on every resolve.
    """
    _reject_unsupported(
        params, {"project_id", "agent_id", "field", "value"}, "project.set_override"
    )

    project_id = _required_string(params, "project_id")
    agent_id = _required_string(params, "agent_id")
    field = _required_override_field(params)

    try:
        value = _validate_override_value(state, field, params.get("value"))
        current_project = _projects(state).get(project_id)
        previous_value = current_project.overrides.get(agent_id, {}).get(field, _MISSING)
        team = _agent_resolver(state).scan_project_report(current_project).team
        if agent_id not in {member.agent_id for member in team}:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"agent '{agent_id}' is not on project '{project_id}' team",
            )
        project = _projects(state).set_override(project_id, agent_id, field, value)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if previous_value is _MISSING or previous_value != value:
        _LOGGER.info(
            "Project Agent override set (project=%s agent=%s field=%s)",
            project_id,
            agent_id,
            field,
        )
    return _override_result(state, project)


def _clear_override(state: Any, params: JsonObject) -> JsonObject:
    """Clear one overridden field for an agent and return the refreshed project.

    Clearing an absent field (or the agent's last field, which removes the entry) is
    a no-op success. No cache invalidation is needed — ``project.json`` is read fresh
    on every resolve, so the dropped override takes effect on the next run.
    """
    _reject_unsupported(params, {"project_id", "agent_id", "field"}, "project.clear_override")

    project_id = _required_string(params, "project_id")
    agent_id = _required_string(params, "agent_id")
    field = _required_override_field(params)
    try:
        current_project = _projects(state).get(project_id)
        had_override = field in current_project.overrides.get(agent_id, {})
        project = _projects(state).clear_override(project_id, agent_id, field)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if had_override:
        _LOGGER.info(
            "Project Agent override cleared (project=%s agent=%s field=%s)",
            project_id,
            agent_id,
            field,
        )
    return _override_result(state, project)


def _override_result(state: Any, project: Project) -> JsonObject:
    """Return the standard override RPC result (refreshed project + live scan)."""
    scan = _scan_preview(state, project)
    return {"project": _project_response(project), "scan": scan}


def _required_override_field(params: JsonObject) -> str:
    """Return the override field name, rejecting an unknown one as ``invalid_request``."""
    field = _required_string(params, "field")
    if field not in OVERRIDE_FIELDS:
        allowed = ", ".join(sorted(OVERRIDE_FIELDS))
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.field must be one of: {allowed}",
        )
    return field


def _validate_override_value(state: Any, field: str, value: Any) -> Any:
    """Validate an override value against the field's rule; raise ``invalid_request`` on error.

    ``model`` reuses the ``/model`` usable-model gate (configured in this instance +
    any pinned connection allowed); ``temperature`` / ``thinking_effort`` reuse the
    canonical agent field validators (``""`` thinking effort forces the provider
    default). ``None`` is never a valid override value — clearing is
    ``project.clear_override``.
    """
    if field == "model":
        if not isinstance(value, str) or not value.strip():
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST, "params.value must be a non-empty model string"
            )
        state.runtime.agent_resolver.require_model_configured(value)
        return value
    if field == "temperature":
        try:
            return validate_temperature(value, label="params.value", allow_none=False)
        except SettingsValidationError as exc:
            raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    if field == "compaction_policy":
        try:
            from core.settings.normalizers import normalize_compaction_policy

            return normalize_compaction_policy(value)
        except Exception as exc:
            raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    try:
        return validate_thinking_effort(value, label="params.value", allow_none=False)
    except SettingsValidationError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc


async def _remove_project(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        {"project_id", "copy_rooted_agent_identity_files"},
        "project.rm",
    )

    project_id = _required_string(params, "project_id")
    copy_identity_files = _optional_bool(params, "copy_rooted_agent_identity_files", default=False)
    projects = _projects(state)
    affected_agents: list[str] = []
    copied_files: dict[str, list[str]] = {}
    backed_up_files: dict[str, list[str]] = {}
    try:
        # Serialize the check-then-archive against any concurrent remove using the
        # same lock the Agent delete lock uses, so a busy check cannot race the
        # archive.
        async with _agent_reference_lock(state):
            projects.get(project_id)
            try:
                async with _state_chat_runs(state).project_admission_guard(project_id):
                    _ensure_no_cron_reference(state, project_id)
                    rooted_agents = state.runtime.agents.agents_rooted_in(project_id)
                    completed_updates: list[tuple[Any, Any]] = []
                    try:
                        await state.runtime.terminal_manager.close_project_scope(project_id)
                        for agent in rooted_agents:
                            default_workspace = state.runtime.agents.default_workspace(agent.id)
                            changes: JsonObject = {"root_project_id": None}
                            workspace_changes = agent.workspace != default_workspace
                            if workspace_changes:
                                changes["workspace"] = default_workspace
                            result = state.runtime.agents.update_with_metadata(
                                agent.id,
                                copy_workspace_identity_files=(
                                    copy_identity_files and workspace_changes
                                ),
                                **changes,
                            )
                            completed_updates.append((agent, result))
                            affected_agents.append(agent.id)
                            copied_files[agent.id] = list(result.copied_files)
                            backed_up_files[agent.id] = list(result.backed_up_files)
                        archive_path = projects.delete(project_id)
                    except Exception:
                        for previous_agent, result in reversed(completed_updates):
                            state.runtime.agents.restore_update(previous_agent, result)
                        raise
            except RunAdmissionBlockedError as exc:
                raise RpcError(
                    RPC_ERROR_PROJECT_BUSY,
                    "cannot remove project with active or queued runs",
                ) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    # Removal drops this repo from resolution; clear both per-project caches so a
    # later project that reuses this slug against a different repo resolves fresh
    # instead of inheriting the removed project's stale Team or skills. Safe after
    # the lock: a deleted project can no longer repopulate either cache (every
    # load raises), so nothing can race a stale entry back in.
    _invalidate_project_caches(state, project_id)
    publish_resource_changed(state, RESOURCE_KIND_AGENTS)
    publish_resource_changed(state, RESOURCE_KIND_PROJECTS)
    _LOGGER.info(
        "Project archived (project=%s affected_agents=%s)",
        project_id,
        len(affected_agents),
    )
    return {
        "project_id": project_id,
        "archived": True,
        "archive_path": str(archive_path),
        "affected_agent_ids": affected_agents,
        "copied_files": copied_files,
        "backed_up_files": backed_up_files,
    }


def _ensure_no_cron_reference(state: Any, project_id: str) -> None:
    """Reject removal while an automation points at a Project agent.

    Mirrors the Agent ``agent_in_use`` cron guard, qualified to this project by a
    direct ``job.project_id == project_id`` match now that cron carries the
    project dimension. A job with ``project_id=None`` targets an identity agent,
    so it never blocks a project removal even when its bare ``agent_id`` happens
    to match a same-named Team member.
    """
    cron_service = getattr(state.runtime, "cron_service", None)
    if cron_service is None:
        return
    referencing = sorted(
        f"cron:{job.id}"
        for job in cron_service.list_jobs()
        if _cron_targets_project_agent(job, project_id)
    )
    if referencing:
        raise RpcError(
            RPC_ERROR_PROJECT_IN_USE,
            f"cannot remove project referenced by {', '.join(referencing)}",
        )
    bootstrap_service = getattr(state.runtime, "bootstrap_service", None)
    if bootstrap_service is None:
        return
    bootstrap_references = sorted(
        f"bootstrap:{job.id}"
        for job in bootstrap_service.list_jobs()
        if (
            job.project_id == project_id
            and getattr(job, "status", "active") not in TERMINAL_BOOTSTRAP_STATUSES
        )
    )
    if bootstrap_references:
        raise RpcError(
            RPC_ERROR_PROJECT_IN_USE,
            f"cannot remove project referenced by {', '.join(bootstrap_references)}",
        )


def _cron_targets_project_agent(job: Any, project_id: str) -> bool:
    """Return whether a cron job points at an agent of *this* project.

    Qualified match: a cron job targets a Project agent iff its ``project_id``
    equals this project's id. A bare job (``project_id=None``) targets an identity
    agent, never a Project agent, even when the ids collide by name.
    """
    return bool(
        job.project_id == project_id
        and getattr(job, "status", "active") not in TERMINAL_CRON_JOB_STATUSES
    )


def _scan_preview(state: Any, project: Project) -> JsonObject:
    """Scan one project into the agent-facing Team + report preview."""
    resolver = _agent_resolver(state)
    result = resolver.scan_project_report(project)
    result = ScanResult(
        team=result.team,
        report=result.report.with_tool_findings(_unavailable_project_tool_findings(state, project)),
    )
    response = _scan_response(resolver, result, project)
    response["skills"] = _project_skill_pool(state, project.project_id)
    return response


def _registered_project_tool_names(state: Any) -> frozenset[str]:
    """Return the live registry names eligible for a Project Tool Whitelist.

    This matches the Project editor's catalog boundary: registered normal tools,
    including not-ready tools, excluding Session-scoped tools and the two
    Agent-owned capabilities that a config/project agent cannot configure.
    """
    registered = state.runtime.tools.list_tools(include_session_scoped=False)
    return frozenset(
        tool.name for tool in registered if project_tool_configurability_reason(tool.name) is None
    )


def _validate_project_tool_change(
    state: Any,
    current_project: Project,
    requested_tools: list[str],
) -> None:
    """Reject wildcard or newly introduced unavailable Project tool names.

    Existing unavailable names may be carried forward or removed. That narrow
    exception preserves a disabled Extension's permission while still preventing
    any RPC caller from granting a name that is not currently a registered
    Project tool.
    """
    if PROJECT_TOOL_ALLOWLIST_WILDCARD in requested_tools:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.allowed_tools cannot contain the all-tools wildcard '*' for a Project",
        )

    registered = _registered_project_tool_names(state)
    unavailable = set(requested_tools) - registered
    newly_introduced = sorted(unavailable - set(current_project.allowed_tools))
    if newly_introduced:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.allowed_tools contains tools that are not currently registered "
            f"for Projects: {', '.join(newly_introduced)}",
        )


def _unavailable_project_tool_findings(state: Any, project: Project) -> list[ScanFinding]:
    """Build non-fatal findings for persisted names outside the live catalog."""
    registered = _registered_project_tool_names(state)
    return [
        ScanFinding(
            type=FindingType.UNAVAILABLE_TOOL,
            detail=(
                f"Tool Whitelist entry '{tool_name}' is not a currently registered "
                "Project tool. It remains stored but grants no access unless the "
                "tool becomes available again."
            ),
        )
        for tool_name in sorted(set(project.allowed_tools) - registered)
    ]


def _project_skill_pool(state: Any, project_id: str) -> JsonObject:
    """Return the project's skill pool for the whitelist editor.

    ``project`` is the project's own scanned skills (auto-on, off-exception list).
    ``global`` is the user's global-home skills and ``bundled`` is everything else
    shippable (bundled plus any configured extra dirs) — both opt-in lists, each with
    names a project skill shadows removed (project wins the collision). All sorted.

    Each pool entry is a ``{"name", "description"}`` object so the whitelist editor's
    chips can show the skill's description on hover, matching the tool pool. Names stay
    authoritative from ``project_skill_names`` (the set the whitelist math operates on);
    descriptions are best-effort — the project's own from ``project_own_skills``, the
    bundled/global ones from the loaded skills registry — defaulting to ``""``.

    Guarded with ``getattr`` so a minimal test runtime without the skill seams degrades
    to empty pools rather than raising.
    """
    runtime = state.runtime
    project_skill_names = getattr(runtime, "project_skill_names", None)
    project_names = sorted(project_skill_names(project_id)) if callable(project_skill_names) else []
    project_own = getattr(runtime, "project_own_skills", None)
    own_metadata = list(project_own(project_id)) if callable(project_own) else []
    project_descriptions = {skill.name: getattr(skill, "description", "") for skill in own_metadata}
    project_set = set(project_names)

    skills_registry = getattr(runtime, "skills", None)
    all_skills = list(skills_registry.list_all()) if skills_registry else []
    registry_descriptions = {skill.name: getattr(skill, "description", "") for skill in all_skills}

    global_names = sorted(
        skill.name
        for skill in all_skills
        if getattr(skill, "origin", None) == SKILL_ORIGIN_GLOBAL and skill.name not in project_set
    )
    global_set = set(global_names)
    bundled = sorted(
        skill.name
        for skill in all_skills
        if skill.name not in project_set and skill.name not in global_set
    )

    def _entries(names: list[str], descriptions: dict[str, str]) -> list[JsonObject]:
        return [{"name": name, "description": descriptions.get(name, "")} for name in names]

    return {
        "project": _entries(project_names, project_descriptions),
        "bundled": _entries(bundled, registry_descriptions),
        "global": _entries(global_names, registry_descriptions),
    }


def _set_changes(params: JsonObject) -> JsonObject:
    changes: JsonObject = {}
    if "cwd" in params:
        changes["cwd"] = _required_string(params, "cwd")
    if "display_name" in params:
        display_name = params["display_name"]
        if display_name is not None and not isinstance(display_name, str):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.display_name must be a string or null",
            )
        changes["display_name"] = display_name
    if "default_agent" in params:
        changes["default_agent"] = _optional_string(params, "default_agent") or ""
    if "default_model" in params:
        changes["default_model"] = _optional_string(params, "default_model") or ""
    # Not _optional_string: it rejects "", but "" is a real thinking value
    # ("provider default"). Presence decides change-vs-not; null clears the
    # default, "" forces the provider default, a level sets it.
    if "default_temperature" in params:
        changes["default_temperature"] = _validate_default_temperature(
            params["default_temperature"]
        )
    if "default_thinking_effort" in params:
        changes["default_thinking_effort"] = _validate_default_thinking_effort(
            params["default_thinking_effort"]
        )
    if "source_format" in params:
        changes["source_format"] = _validated_source_format(params["source_format"])
    if "auto_load" in params:
        changes["auto_load"] = _optional_auto_load(params)
    # The Tool/Skill Whitelist fields are lists of non-empty strings; an explicit
    # empty list is a real value (e.g. every tool off), so presence in params — not
    # truthiness — decides whether the field changes.
    for list_field in (
        "allowed_tools",
        "skills_bundled_enabled",
        "skills_global_enabled",
        "skills_project_disabled",
    ):
        if list_field in params:
            changes[list_field] = _string_list_field(params, list_field)
    return changes


def _optional_auto_load(params: JsonObject) -> list[str]:
    value = params.get("auto_load")
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.auto_load must be a list of non-empty strings",
        )
    return list(value)


def _string_list_field(params: JsonObject, key: str) -> list[str]:
    """Validate a list-of-non-empty-strings param (an empty list is allowed)."""
    value = params.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be a list of non-empty strings",
        )
    return list(value)


def _validate_default_temperature(value: Any) -> float | None:
    """Validate the optional project-default temperature (null allowed = no default).

    Delegates to the canonical ``core.settings`` rule (the single ``[0, 2]``
    authority), wrapping its error as ``invalid_request`` — exactly as the
    ``agent.*`` RPC validates the per-agent temperature (D5).
    """
    try:
        return validate_temperature(value, label="params.default_temperature", allow_none=True)
    except SettingsValidationError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc


def _validate_default_thinking_effort(value: Any) -> str | None:
    """Validate the optional project-default thinking effort (null = no default).

    Delegates to the canonical ``core.settings`` rule, which accepts ``""`` as the
    explicit "provider default" value; wraps its error as ``invalid_request``.
    """
    try:
        return validate_thinking_effort(
            value, label="params.default_thinking_effort", allow_none=True
        )
    except SettingsValidationError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc


def _validated_source_format(value: Any) -> str:
    """Validate an explicit ``source_format`` param against the canonical vocabulary."""
    if not isinstance(value, str) or value not in PROJECT_SOURCE_FORMATS:
        choices = ", ".join(PROJECT_SOURCE_FORMATS)
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.source_format must be one of: {choices}",
        )
    return value


def _auto_detect_source_format(cwd: str) -> str:
    """Pick the format for a creation without an explicit ``source_format``.

    Exactly one format present in the repo → that one (silent, no dialog noise);
    both or neither → the deterministic default ``opencode`` (decision 2 — a
    non-interactive creator gets a predictable outcome, and the format stays
    changeable in the project settings).
    """
    detection = detect_project_formats(Path(cwd))
    present = [
        format_key
        for format_key in PROJECT_SOURCE_FORMATS
        if format_key in detection.formats and detection.formats[format_key].present
    ]
    if len(present) == 1:
        return present[0]
    return DEFAULT_PROJECT_SOURCE_FORMAT


def _detect_project(state: Any, params: JsonObject) -> JsonObject:
    """``project.detect``: report per-format presence + context files for a cwd.

    The add dialog calls this while the user types a path, so a nonexistent or
    non-directory cwd is a **success** with ``cwd_exists: false`` and empty data,
    never an error. For an existing directory it returns per-format agent/skill
    counts and the context-file facts (``AGENTS.md`` present, ``CLAUDE.md``
    location or null) the dialog builds its format choice and CLAUDE.md
    suggestion from.
    """
    _reject_unsupported(params, {"cwd"}, "project.detect")

    cwd = _optional_string(params, "cwd")
    if not cwd or not cwd_exists(cwd):
        return {
            "cwd_exists": False,
            "formats": {},
            "context_files": {"agents_md": False, "claude_md": None},
        }

    detection = detect_project_formats(Path(cwd))
    return {
        "cwd_exists": True,
        "formats": _formats_response(detection),
        "context_files": {
            "agents_md": detection.agents_md,
            "claude_md": detection.claude_md,
        },
    }


def _formats_response(detection: ProjectFormatDetection) -> JsonObject:
    return {
        format_key: {"agents": presence.agents, "skills": presence.skills}
        for format_key, presence in detection.formats.items()
    }


def _display_name_from_cwd(cwd: str) -> str:
    """Derive a display name from the repo folder basename when none is given."""
    name = Path(cwd).name
    return name or cwd


def _slug_from_display_name(display_name: str) -> str:
    try:
        return slugify_project_id(display_name)
    except ValueError as exc:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"cannot derive a project id from {display_name!r}: "
            "provide a display_name with letters or digits",
        ) from exc


def _project_response(project: Project) -> JsonObject:
    return {
        "project_id": project.project_id,
        "display_name": project.display_name,
        "cwd": project.cwd,
        "cwd_exists": cwd_exists(project.cwd),
        "default_agent": project.default_agent,
        "default_model": project.default_model,
        "default_temperature": project.default_temperature,
        "default_thinking_effort": project.default_thinking_effort,
        "source_format": project.source_format,
        "auto_load": list(project.auto_load),
        "allowed_tools": list(project.allowed_tools),
        "skills_bundled_enabled": list(project.skills_bundled_enabled),
        "skills_global_enabled": list(project.skills_global_enabled),
        "skills_project_disabled": list(project.skills_project_disabled),
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def _scan_response(resolver: Any, result: ScanResult, project: Project) -> JsonObject:
    return {
        "team": [_team_member_response(resolver, member, project) for member in result.team],
        "report": _report_response(result.report),
    }


def _team_member_response(resolver: Any, member: ScannedAgent, project: Project) -> JsonObject:
    return {
        "agent_id": member.agent_id,
        "display_name": member.display_name,
        "description": member.description,
        "model": member.model,
        "temperature": member.temperature,
        "thinking_effort": member.thinking_effort,
        "source_format": member.source_format,
        "source_path": str(member.source_path),
        # The vBot tools this agent turns off via its OpenCode permissions, sorted.
        # The editor pairs this with the project Tool Whitelist (the ceiling) to show
        # that an individual agent may use less than the project maximum.
        "denied_tools": sorted(member.denied_tools),
        "tools": resolver.effective_tools_for_member(project, member),
        # The agent's override object (vBot-owned, the top tier of each chain), or
        # null when this agent has none — the Projects tab renders/clears it per row.
        "overrides": project.overrides.get(member.agent_id) or None,
        # Per run field, the effective value + winning tier (override / agent /
        # project_default / global_default / null), computed from the already-scanned
        # member so the team listing never re-scans per member.
        "effective": resolver.effective_config_for_member(project, member),
    }


def _report_response(report: ScanReport) -> JsonObject:
    return {
        "clean": report.is_clean,
        "findings": [_finding_response(finding) for finding in report.findings],
    }


def _finding_response(finding: ScanFinding) -> JsonObject:
    return {
        "type": finding.type.value,
        "detail": finding.detail,
        "agent_id": finding.agent_id,
        "source_path": str(finding.source_path) if finding.source_path is not None else None,
    }


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the project RPC handlers."""

    return {
        "project.add": _add_project,
        "project.list": _list_projects,
        "project.show": _show_project,
        "project.set": _set_project,
        "project.set_override": _set_override,
        "project.clear_override": _clear_override,
        "project.rm": _remove_project,
        "project.detect": _detect_project,
    }
