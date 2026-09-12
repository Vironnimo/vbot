"""CLI argument translation for Agents, Projects, and Sessions."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Any

from cli.agent_management import agent_reorder
from cli.project_management import (
    project_add,
    project_clear_override,
    project_detect,
    project_list,
    project_remove,
    project_set,
    project_set_override,
    project_show,
)
from cli.server_management import CommandResult, ServerInstance
from cli.session_management import (
    session_create,
    session_delete,
    session_fork,
    session_link_channel,
    session_list,
    session_rename,
    session_set_compaction_policy,
)
from cli.session_store_management import (
    session_store_incident_acknowledge,
    session_store_snapshot_create,
    session_store_snapshot_list,
    session_store_snapshot_restore,
    session_store_snapshot_verify,
    session_store_status,
)


def dispatch_agent_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_agents: Callable[[ServerInstance], CommandResult],
    show_agent: Callable[[ServerInstance, str], CommandResult],
    create_agent: Callable[[ServerInstance, str, str, dict[str, Any]], CommandResult],
    update_agent: Callable[[ServerInstance, str, dict[str, Any]], CommandResult],
    rename_agent: Callable[[ServerInstance, str, str], CommandResult],
    reorder_agent: Callable[[ServerInstance, Sequence[str]], CommandResult] = agent_reorder,
    delete_agent: Callable[[ServerInstance, str], CommandResult],
) -> CommandResult:
    """Dispatch one parsed agent command against the server RPC client."""

    if args.command == "list":
        return list_agents(instance)
    if args.command == "show":
        return show_agent(instance, args.id)
    tool_access_error = _agent_tool_access_args_error(args)
    if tool_access_error is not None:
        return CommandResult(ok=False, message=tool_access_error, instance=instance)
    if args.command == "create":
        return create_agent(instance, args.id, args.name, _agent_changes_from_args(args))
    if args.command == "update":
        return update_agent(instance, args.id, _agent_changes_from_args(args))
    if args.command == "rename":
        return rename_agent(instance, args.id, args.new_id)
    if args.command == "reorder":
        return reorder_agent(instance, list(args.ids))
    if args.command == "delete":
        return delete_agent(instance, args.id)
    raise ValueError(f"Unsupported agent command: {args.command}")


def _agent_tool_access_args_error(args: argparse.Namespace) -> str | None:
    mode = getattr(args, "tool_access_mode", None)
    allowed = getattr(args, "tool_allow", None)
    denied = getattr(args, "tool_deny", None)
    if mode is None and (allowed is not None or denied is not None):
        return "--tool-allow and --tool-deny require --tool-access-mode"
    if allowed is not None and mode != "selected":
        return "--tool-allow is valid only with --tool-access-mode selected"
    return None


def _agent_changes_from_args(args: argparse.Namespace) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if args.command == "update" and getattr(args, "name", None) is not None:
        changes["name"] = args.name
    if getattr(args, "clear_model", False):
        changes["model"] = ""
    elif args.model is not None:
        changes["model"] = args.model
    if getattr(args, "clear_fallback_models", False):
        changes["fallback_models"] = []
    elif getattr(args, "fallback_models", None) is not None:
        changes["fallback_models"] = list(args.fallback_models)
    if args.clear_temperature:
        changes["temperature"] = None
    elif args.temperature is not None:
        changes["temperature"] = args.temperature
    if args.clear_thinking_effort:
        changes["thinking_effort"] = None
    elif args.thinking_effort is not None:
        changes["thinking_effort"] = args.thinking_effort
    if args.memory_prompt_mode is not None:
        changes["memory_prompt_mode"] = args.memory_prompt_mode
    if args.custom_system_prompt is not None:
        changes["custom_system_prompt_enabled"] = args.custom_system_prompt == "true"
    if (
        args.tool_access_mode is not None
        or args.tool_allow is not None
        or args.tool_deny is not None
    ):
        tool_access: dict[str, Any] = {}
        if args.tool_access_mode is not None:
            tool_access["mode"] = args.tool_access_mode
        if args.tool_access_mode == "selected":
            tool_access["allowed"] = list(args.tool_allow or [])
        elif args.tool_allow is not None:
            tool_access["allowed"] = list(args.tool_allow)
        if args.tool_deny is not None:
            tool_access["denied"] = list(args.tool_deny)
        changes["tool_access"] = tool_access
    if args.allowed_skills is not None:
        changes["allowed_skills"] = list(args.allowed_skills)
    if args.subagent_allow is not None:
        changes["tools"] = {"subagent": {"allowed_agents": list(args.subagent_allow)}}
    if getattr(args, "clear_compaction_policy", False):
        changes["compaction_policy"] = None
    elif args.compaction_policy is not None:
        changes["compaction_policy"] = args.compaction_policy
    if getattr(args, "default_workspace", False):
        changes["workspace"] = None
    elif getattr(args, "workspace", None) is not None:
        changes["workspace"] = args.workspace
    if getattr(args, "copy_workspace_files", False):
        changes["copy_workspace_identity_files"] = True
    if getattr(args, "clear_project", False):
        changes["root_project_id"] = None
    elif getattr(args, "project", None) is not None:
        changes["root_project_id"] = args.project
    if getattr(args, "current_session_id", None) is not None:
        changes["current_session_id"] = args.current_session_id
    return changes


def dispatch_project_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    add_project_fn: Callable[[ServerInstance, str, dict[str, Any]], CommandResult] = project_add,
    list_projects_fn: Callable[[ServerInstance], CommandResult] = project_list,
    show_project_fn: Callable[[ServerInstance, str], CommandResult] = project_show,
    set_project_fn: Callable[[ServerInstance, str, dict[str, Any]], CommandResult] = project_set,
    set_override_fn: Callable[
        [ServerInstance, str, str, str, str], CommandResult
    ] = project_set_override,
    clear_override_fn: Callable[
        [ServerInstance, str, str, str], CommandResult
    ] = project_clear_override,
    remove_project_fn: Callable[[ServerInstance, str, bool], CommandResult] = project_remove,
) -> CommandResult:
    """Dispatch one parsed project command against the server RPC client."""

    if args.command == "add":
        return add_project_fn(instance, args.cwd, _project_add_fields_from_args(args))
    if args.command == "list":
        return list_projects_fn(instance)
    if args.command == "show":
        return show_project_fn(instance, args.id)
    if args.command == "set":
        return set_project_fn(instance, args.id, _project_set_changes_from_args(args))
    if args.command == "set-override":
        return set_override_fn(instance, args.id, args.agent, args.field, args.value)
    if args.command == "clear-override":
        return clear_override_fn(instance, args.id, args.agent, args.field)
    if args.command == "rm":
        return remove_project_fn(instance, args.id, args.copy_rooted_agent_files)
    if args.command == "detect":
        return project_detect(instance, getattr(args, "cwd", None))
    raise ValueError(f"Unsupported project command: {args.command}")


def _project_add_fields_from_args(args: argparse.Namespace) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if args.name is not None:
        fields["display_name"] = args.name
    if args.default_agent is not None:
        fields["default_agent"] = args.default_agent
    if args.default_model is not None:
        fields["default_model"] = args.default_model
    _apply_project_default_knobs(args, fields)
    if args.format is not None:
        fields["source_format"] = args.format
    if args.auto_load is not None:
        fields["auto_load"] = list(args.auto_load)
    _apply_project_capability_fields(args, fields)
    return fields


def _project_set_changes_from_args(args: argparse.Namespace) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if args.cwd is not None:
        changes["cwd"] = args.cwd
    if args.name is not None:
        changes["display_name"] = args.name
    if args.clear_default_agent:
        changes["default_agent"] = None
    elif args.default_agent is not None:
        changes["default_agent"] = args.default_agent
    if args.clear_default_model:
        changes["default_model"] = None
    elif args.default_model is not None:
        changes["default_model"] = args.default_model
    _apply_project_default_knobs(args, changes)
    if args.format is not None:
        changes["source_format"] = args.format
    if args.auto_load is not None:
        changes["auto_load"] = list(args.auto_load)
    _apply_project_capability_fields(args, changes)
    return changes


def _apply_project_capability_fields(args: argparse.Namespace, target: dict[str, Any]) -> None:
    mappings = {
        "allowed_tools": "allowed_tools",
        "enabled_bundled_skills": "skills_bundled_enabled",
        "enabled_global_skills": "skills_global_enabled",
        "disabled_project_skills": "skills_project_disabled",
    }
    for argument, field in mappings.items():
        value = getattr(args, argument, None)
        if value is not None:
            target[field] = list(value)


def _apply_project_default_knobs(args: argparse.Namespace, target: dict[str, Any]) -> None:
    """Map the temperature/thinking flags into a project add/set payload.

    Mirrors ``_agent_changes_from_args``: a ``--clear-*`` flag wins and sends
    ``null`` (fall through to the global default); otherwise a provided value is
    sent. An empty ``--default-thinking-effort ""`` is a real value (provider
    default), so it passes the ``is not None`` gate and is sent verbatim.
    """
    if args.clear_default_temperature:
        target["default_temperature"] = None
    elif args.default_temperature is not None:
        target["default_temperature"] = args.default_temperature
    if args.clear_default_thinking_effort:
        target["default_thinking_effort"] = None
    elif args.default_thinking_effort is not None:
        target["default_thinking_effort"] = args.default_thinking_effort


def dispatch_session_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_sessions_fn: Callable[[ServerInstance, str], CommandResult] = session_list,
    create_session_fn: Callable[
        [ServerInstance, str, str | None, bool], CommandResult
    ] = session_create,
    delete_session_fn: Callable[[ServerInstance, str, str, bool], CommandResult] = session_delete,
    link_session_fn: Callable[
        [ServerInstance, str, str, str, str], CommandResult
    ] = session_link_channel,
    fork_session_fn: Callable[[ServerInstance, str, str, str | None], CommandResult] = session_fork,
    rename_session_fn: Callable[[ServerInstance, str, str, str], CommandResult] = session_rename,
    set_session_policy_fn: Callable[
        [ServerInstance, str, str, dict[str, object] | None], CommandResult
    ] = session_set_compaction_policy,
) -> CommandResult:
    """Dispatch one parsed session command against the server RPC client."""

    if args.command == "list":
        if args.limit != 100 or args.cursor is not None or args.all:
            return session_list(
                instance, args.agent, limit=args.limit, cursor=args.cursor, all_pages=args.all
            )
        return list_sessions_fn(instance, args.agent)
    if args.command == "create":
        return create_session_fn(instance, args.agent, args.id, args.make_current)
    if args.command == "delete":
        return delete_session_fn(instance, args.agent, args.session, args.yes)
    if args.command == "fork":
        return fork_session_fn(instance, args.agent, args.session, args.target_agent)
    if args.command == "rename":
        return rename_session_fn(instance, args.agent, args.session, args.title or "")
    if args.command == "set-compaction-policy":
        return set_session_policy_fn(instance, args.agent, args.session, args.policy)
    if args.command == "link-channel":
        return link_session_fn(instance, args.agent, args.session, args.channel, args.conversation)
    raise ValueError(f"Unsupported session command: {args.command}")


def dispatch_session_store_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    status_fn: Callable[[ServerInstance], CommandResult] = session_store_status,
    snapshot_list_fn: Callable[[ServerInstance], CommandResult] = session_store_snapshot_list,
    snapshot_create_fn: Callable[
        [ServerInstance, str], CommandResult
    ] = session_store_snapshot_create,
    snapshot_verify_fn: Callable[
        [ServerInstance, str], CommandResult
    ] = session_store_snapshot_verify,
    snapshot_restore_fn: Callable[
        [ServerInstance, str, bool], CommandResult
    ] = session_store_snapshot_restore,
    incident_acknowledge_fn: Callable[
        [ServerInstance, str], CommandResult
    ] = session_store_incident_acknowledge,
) -> CommandResult:
    """Dispatch operator controls for the current-format SQLite Session store."""

    if args.command == "status":
        return status_fn(instance)
    if args.command == "snapshot":
        if args.snapshot_command == "list":
            return snapshot_list_fn(instance)
        if args.snapshot_command == "create":
            return snapshot_create_fn(instance, args.reason)
        if args.snapshot_command == "verify":
            return snapshot_verify_fn(instance, args.snapshot_id)
        if args.snapshot_command == "restore":
            return snapshot_restore_fn(instance, args.snapshot_id, args.yes)
    if args.command == "incident" and args.incident_command == "acknowledge":
        return incident_acknowledge_fn(instance, args.incident_id)
    raise ValueError(f"Unsupported Session-store command: {args.command}")
