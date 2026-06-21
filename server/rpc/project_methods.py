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
name). ``add`` returns it for the just-created project; ``show`` re-scans live
(the repo is the source of truth, no copy drift). An empty folder yields an empty
team and a clean report — that is a valid Project, not an error.

**Remove lock.** ``project.rm`` archives the anchor (never the repo) unless a
Project agent is in use: an active or queued Run of a session-owning agent
(``RPC_ERROR_PROJECT_BUSY``), or a cron job pointing at a Project agent
(``RPC_ERROR_PROJECT_IN_USE``). This mirrors the Agent delete lock
(``agent_busy`` / ``agent_in_use``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.projects import (
    Project,
    cwd_exists,
    slugify_project_id,
)
from core.projects.scan_report import ScanFinding, ScanReport
from core.projects.scanners.base import ScannedAgent, ScanResult
from core.settings import (
    SettingsValidationError,
    validate_temperature,
    validate_thinking_effort,
)
from server.rpc.agent_refs import _agent_reference_lock
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import (
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_PROJECT_BUSY,
    RPC_ERROR_PROJECT_IN_USE,
    RpcError,
)
from server.rpc.runtime_access import _state_chat_runs
from server.rpc.validation import _optional_string, _required_string

JsonObject = dict[str, Any]

# A bare cwd is a valid Project (GLOSSARY → Project; plan: "Minimal-Projekt = nur
# eine cwd"): the OpenCode location's presence is surfaced in the scan preview's
# Team, never a hard add-time requirement, so add only validates that the folder
# exists and is not already claimed.
_ADD_FIELDS = frozenset(
    {
        "cwd",
        "display_name",
        "default_agent",
        "default_model",
        "default_temperature",
        "default_thinking_effort",
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
        "auto_load",
        "allowed_tools",
        "skills_bundled_enabled",
        "skills_project_disabled",
    }
)


def _projects(state: Any) -> Any:
    return state.runtime.projects


def _agent_resolver(state: Any) -> Any:
    return state.runtime.agent_resolver


def _add_project(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - _ADD_FIELDS)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported project.add fields: {', '.join(unsupported_fields)}",
        )

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
            auto_load=auto_load,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    scan = _scan_preview(state, project)
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
    unsupported_fields = sorted(set(params) - {"project_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported project.show fields: {', '.join(unsupported_fields)}",
        )

    project_id = _required_string(params, "project_id")
    try:
        project = _projects(state).get(project_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

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
        project = _projects(state).update(project_id, **changes)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    # A cwd change re-points the repo, so the live Team can change — drop the
    # cached scan and return a fresh report so the caller sees the new Team. The
    # project's cached skills (own ``.opencode/skills`` + bundled merge) hang off
    # the cwd too, so drop them on the same trigger.
    if "cwd" in changes:
        _agent_resolver(state).invalidate_team_cache(project_id)
        invalidate_project_skills = getattr(state.runtime, "invalidate_project_skills", None)
        if callable(invalidate_project_skills):
            invalidate_project_skills(project_id)
    scan = _scan_preview(state, project)
    return {"project": _project_response(project), "scan": scan}


async def _remove_project(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"project_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported project.rm fields: {', '.join(unsupported_fields)}",
        )

    project_id = _required_string(params, "project_id")
    projects = _projects(state)
    try:
        # Serialize the check-then-archive against any concurrent remove using the
        # same lock the Agent delete lock uses, so a busy check cannot race the
        # archive.
        async with _agent_reference_lock(state):
            projects.get(project_id)
            _ensure_not_busy(state, project_id)
            _ensure_no_cron_reference(state, project_id)
            archive_path = projects.delete(project_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"project_id": project_id, "archived": True, "archive_path": str(archive_path)}


def _ensure_not_busy(state: Any, project_id: str) -> None:
    """Reject removal while a session-owning Project agent has run activity.

    A Project agent owns its sessions under the anchor; if any of those agents
    has a running or queued Run, the project is in active use and removal is
    blocked (``project_busy``), mirroring the Agent ``agent_busy`` guard.
    """
    chat_runs = _state_chat_runs(state)
    for agent_id in _projects(state).session_owning_agents(project_id):
        if chat_runs.has_activity_for_agent(agent_id):
            raise RpcError(
                RPC_ERROR_PROJECT_BUSY,
                f"cannot remove project with active or queued runs: agent {agent_id}",
            )


def _ensure_no_cron_reference(state: Any, project_id: str) -> None:
    """Reject removal while a cron job points at a Project agent.

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


def _cron_targets_project_agent(job: Any, project_id: str) -> bool:
    """Return whether a cron job points at an agent of *this* project.

    Qualified match: a cron job targets a Project agent iff its ``project_id``
    equals this project's id. A bare job (``project_id=None``) targets an identity
    agent, never a Project agent, even when the ids collide by name.
    """
    return bool(job.project_id == project_id)


def _scan_preview(state: Any, project: Project) -> JsonObject:
    """Scan one project into the agent-facing Team + report preview."""
    result = _agent_resolver(state).scan_project_report(project)
    response = _scan_response(result)
    response["skills"] = _project_skill_pool(state, project.project_id)
    return response


def _project_skill_pool(state: Any, project_id: str) -> JsonObject:
    """Return the project's skill pool for the whitelist editor.

    ``project`` is the project's own scanned skills (auto-on, off-exception list);
    ``bundled`` is the global bundled pool minus any name a project skill shadows
    (project wins the collision), the opt-in list. Both sorted. Guarded with
    ``getattr`` so a minimal test runtime without the skill seams degrades to empty
    pools rather than raising.
    """
    runtime = state.runtime
    project_skill_names = getattr(runtime, "project_skill_names", None)
    project_skills = (
        sorted(project_skill_names(project_id)) if callable(project_skill_names) else []
    )
    skills_registry = getattr(runtime, "skills", None)
    bundled_all = (
        sorted(skill.name for skill in skills_registry.list_all()) if skills_registry else []
    )
    project_set = set(project_skills)
    bundled = [name for name in bundled_all if name not in project_set]
    return {"project": project_skills, "bundled": bundled}


def _set_changes(params: JsonObject) -> JsonObject:
    changes: JsonObject = {}
    if "cwd" in params:
        changes["cwd"] = _required_string(params, "cwd")
    if "display_name" in params:
        changes["display_name"] = _required_string(params, "display_name")
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
    if "auto_load" in params:
        changes["auto_load"] = _optional_auto_load(params)
    # The Tool/Skill Whitelist fields are lists of non-empty strings; an explicit
    # empty list is a real value (e.g. every tool off), so presence in params — not
    # truthiness — decides whether the field changes.
    for list_field in ("allowed_tools", "skills_bundled_enabled", "skills_project_disabled"):
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
        "auto_load": list(project.auto_load),
        "allowed_tools": list(project.allowed_tools),
        "skills_bundled_enabled": list(project.skills_bundled_enabled),
        "skills_project_disabled": list(project.skills_project_disabled),
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def _scan_response(result: ScanResult) -> JsonObject:
    return {
        "team": [_team_member_response(member) for member in result.team],
        "report": _report_response(result.report),
    }


def _team_member_response(member: ScannedAgent) -> JsonObject:
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
        "project.rm": _remove_project,
    }
