"""Project management RPC commands for the vBot CLI.

The CLI is an accessor, not a second control plane: these commands call the
``project.*`` server RPC and render its deterministic, agent-facing output. They
never read or mutate project files directly. The project rides the explicit
``project_id`` dimension (Option 1) — there is no ``--project`` flag and no
``agent@projekt`` parsing here; the ``agent@projekt`` outer spelling belongs to
the positional agent argument of session/cron commands, parsed server-side.

``project add`` and ``project show`` render the **scan preview** the server
returns: the Team (callable agents discovered in the repo) plus the report
(everything unclean under what exists). An empty folder yields an empty team and
a clean report — a valid Project, not an error.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance

PROJECT_SET_FLAGS = (
    "--cwd",
    "--name",
    "--default-agent",
    "--default-model",
    "--default-temperature",
    "--default-thinking-effort",
    "--auto-load",
)


def project_add(
    instance: ServerInstance,
    cwd: str,
    fields: Mapping[str, Any],
) -> CommandResult:
    """Create a project via ``project.add`` RPC and render the scan preview."""

    params: dict[str, Any] = {"cwd": cwd, **dict(fields)}
    payload = _rpc_call(instance, "project.add", params)
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=_format_project_added(payload.data),
        instance=instance,
    )


def project_list(instance: ServerInstance) -> CommandResult:
    """Return formatted project list output from ``project.list`` RPC."""

    payload = _rpc_call(instance, "project.list", {})
    if not payload.ok:
        return payload.to_command_result()
    projects = payload.data.get("projects")
    if not isinstance(projects, list):
        return CommandResult(
            ok=False, message="RPC result missing projects list", instance=instance
        )
    return CommandResult(ok=True, message=_format_project_rows(projects), instance=instance)


def project_show(instance: ServerInstance, project_id: str) -> CommandResult:
    """Return one project's config, Team, and report from ``project.show`` RPC."""

    payload = _rpc_call(instance, "project.show", {"project_id": project_id})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(ok=True, message=_format_project_detail(payload.data), instance=instance)


def project_set(
    instance: ServerInstance,
    project_id: str,
    changes: Mapping[str, Any],
) -> CommandResult:
    """Update a project via ``project.set`` RPC."""

    if not changes:
        return CommandResult(
            ok=False,
            message=f"no project fields provided; use one of: {', '.join(PROJECT_SET_FLAGS)}",
            instance=instance,
        )
    params = {"project_id": project_id, **dict(changes)}
    payload = _rpc_call(instance, "project.set", params)
    if not payload.ok:
        return payload.to_command_result()
    project = payload.data.get("project")
    updated_id = project_id
    if isinstance(project, dict):
        updated_id = _string_or_default(project.get("project_id"), project_id)
    return CommandResult(ok=True, message=f"updated project {updated_id}", instance=instance)


def project_remove(instance: ServerInstance, project_id: str) -> CommandResult:
    """Archive a project via ``project.rm`` RPC, or surface the block reason."""

    payload = _rpc_call(instance, "project.rm", {"project_id": project_id})
    if not payload.ok:
        return payload.to_command_result()
    removed_id = _string_or_default(payload.data.get("project_id"), project_id)
    archive_path = _string_or_default(payload.data.get("archive_path"), "-")
    return CommandResult(
        ok=True,
        message=f"removed project {removed_id} (archived to {archive_path})",
        instance=instance,
    )


def _format_project_added(data: Mapping[str, Any]) -> str:
    project = data.get("project")
    project_id = "?"
    if isinstance(project, dict):
        project_id = _string_or_default(project.get("project_id"), "?")
    lines = [f"added project {project_id}"]
    lines.extend(_project_config_lines(project))
    lines.extend(_scan_lines(data.get("scan")))
    return "\n".join(lines)


def _format_project_detail(data: Mapping[str, Any]) -> str:
    project = data.get("project")
    project_id = "?"
    if isinstance(project, dict):
        project_id = _string_or_default(project.get("project_id"), "?")
    lines = [f"project {project_id}:"]
    lines.extend(_project_config_lines(project))
    lines.extend(_scan_lines(data.get("scan")))
    return "\n".join(lines)


def _project_config_lines(project: object) -> list[str]:
    if not isinstance(project, dict):
        return ["  config: invalid project entry"]
    temperature = _number_or_default(project.get("default_temperature"), "-")
    thinking_effort = _thinking_effort_text(project.get("default_thinking_effort"))
    return [
        f"  display_name: {_string_or_default(project.get('display_name'), '-')}",
        f"  cwd: {_string_or_default(project.get('cwd'), '-')}",
        f"  cwd_exists: {_bool_text(project.get('cwd_exists'))}",
        f"  default_agent: {_string_or_default(project.get('default_agent'), '-')}",
        f"  default_model: {_string_or_default(project.get('default_model'), '-')}",
        f"  default_temperature: {temperature}",
        f"  default_thinking_effort: {thinking_effort}",
        f"  auto_load: {_format_string_list(project.get('auto_load'))}",
    ]


def _scan_lines(scan: object) -> list[str]:
    if not isinstance(scan, dict):
        return []
    lines: list[str] = []
    lines.extend(_team_lines(scan.get("team")))
    lines.extend(_report_lines(scan.get("report")))
    return lines


def _team_lines(team: object) -> list[str]:
    if not isinstance(team, list) or not team:
        return ["  team: (empty)"]
    lines = ["  team:"]
    for member in team:
        lines.append(_team_member_line(member))
    return lines


def _team_member_line(member: object) -> str:
    if not isinstance(member, dict):
        return "    - invalid team member entry"
    agent_id = _string_or_default(member.get("agent_id"), "?")
    model = _string_or_default(member.get("model"), "-")
    description = _string_or_default(member.get("description"), "-")
    return f"    - {agent_id} model={model} description={description}"


def _report_lines(report: object) -> list[str]:
    if not isinstance(report, dict):
        return []
    findings = report.get("findings")
    if report.get("clean") is True or not isinstance(findings, list) or not findings:
        return ["  report: clean"]
    lines = ["  report:"]
    for finding in findings:
        lines.append(_finding_line(finding))
    return lines


def _finding_line(finding: object) -> str:
    if not isinstance(finding, dict):
        return "    - invalid finding entry"
    finding_type = _string_or_default(finding.get("type"), "?")
    detail = _string_or_default(finding.get("detail"), "-")
    agent_id = finding.get("agent_id")
    line = f"    - [{finding_type}] {detail}"
    if isinstance(agent_id, str) and agent_id:
        line = f"{line} (agent {agent_id})"
    return line


def _format_project_rows(projects: Sequence[object]) -> str:
    if not projects:
        return "no projects configured"

    lines = ["projects:"]
    for project in projects:
        lines.append(_format_project_row(project))
    return "\n".join(lines)


def _format_project_row(project: object) -> str:
    if not isinstance(project, dict):
        return "- invalid project entry"

    project_id = _string_or_default(project.get("project_id"), "?")
    display_name = _string_or_default(project.get("display_name"), "?")
    cwd = _string_or_default(project.get("cwd"), "-")
    default_agent = _string_or_default(project.get("default_agent"), "-")
    return (
        f"- id={project_id}"
        f" name={display_name}"
        f" cwd={cwd}"
        f" cwd_exists={_bool_text(project.get('cwd_exists'))}"
        f" default_agent={default_agent}"
    )


def _format_string_list(value: object) -> str:
    if not isinstance(value, list):
        return "-"
    if not value:
        return "[]"
    return ",".join(str(item) for item in value)


def _bool_text(value: object) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def _number_or_default(value: object, default: str) -> str:
    # bool is an int subclass; never render a stray boolean as a temperature.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


def _thinking_effort_text(value: object) -> str:
    # null = no project default; "" = explicit provider default; else the level.
    if value is None:
        return "-"
    if value == "":
        return "(provider default)"
    if isinstance(value, str):
        return value
    return "-"


def _string_or_default(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default
