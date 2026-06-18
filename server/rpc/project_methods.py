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
_ADD_FIELDS = frozenset({"cwd", "display_name", "default_agent", "default_model", "auto_load"})
_SET_MUTABLE_FIELDS = frozenset(
    {"cwd", "display_name", "default_agent", "default_model", "auto_load"}
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
    # cached scan and return a fresh report so the caller sees the new Team.
    if "cwd" in changes:
        _agent_resolver(state).invalidate_team_cache(project_id)
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

    Mirrors the Agent ``agent_in_use`` cron guard, qualified to this project: a
    cron job counts only when its target agent belongs to *this* project's Team.

    Cron qualification seam: the cron job schema is bare ``agent_id`` today (no
    ``project_id`` column — the project-aware cron task is a separate builder), so
    the match is "cron ``agent_id`` is a Team member of this project". Once cron
    carries ``project_id``, tighten this to ``job.project_id == project_id`` so a
    bare ``builder`` in another project no longer collides.
    """
    cron_service = getattr(state.runtime, "cron_service", None)
    if cron_service is None:
        return
    team_agent_ids = _project_agent_ids(state, project_id)
    referencing = sorted(
        f"cron:{job.id}"
        for job in cron_service.list_jobs()
        if _cron_targets_project_agent(job, project_id, team_agent_ids)
    )
    if referencing:
        raise RpcError(
            RPC_ERROR_PROJECT_IN_USE,
            f"cannot remove project referenced by {', '.join(referencing)}",
        )


def _cron_targets_project_agent(job: Any, project_id: str, team_agent_ids: set[str]) -> bool:
    """Return whether a cron job points at an agent of this project.

    Prefers an explicit ``project_id`` on the job when the cron-builder adds one
    (precise qualification); falls back to "the bare ``agent_id`` is a current
    Team member of this project" while cron stays project-unaware.
    """
    job_project_id = getattr(job, "project_id", None)
    if job_project_id is not None:
        return bool(job_project_id == project_id)
    return job.agent_id in team_agent_ids


def _project_agent_ids(state: Any, project_id: str) -> set[str]:
    """Return the agent ids that belong to this project (Team + session owners).

    Combines the live Team scan (callable Project agents) with the anchor's
    session-owning agents so a cron pointer at an agent that no longer scans but
    still owns sessions is still recognized as project-bound.
    """
    projects = _projects(state)
    try:
        project = projects.get(project_id)
        team = _agent_resolver(state).scan_project_report(project).team
        team_ids = {member.agent_id for member in team}
    except Exception:  # noqa: BLE001 - a bad repo must not break the busy check
        team_ids = set()
    return team_ids | set(projects.session_owning_agents(project_id))


def _scan_preview(state: Any, project: Project) -> JsonObject:
    """Scan one project into the agent-facing Team + report preview."""
    result = _agent_resolver(state).scan_project_report(project)
    return _scan_response(result)


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
    if "auto_load" in params:
        changes["auto_load"] = _optional_auto_load(params)
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
        "auto_load": list(project.auto_load),
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
        "source_format": member.source_format,
        "source_path": str(member.source_path),
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
