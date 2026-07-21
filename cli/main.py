"""Command-line entrypoint for local vBot server management."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cli.agent_management import (
    agent_create,
    agent_delete,
    agent_list,
    agent_rename,
    agent_show,
    agent_update,
)
from cli.autostart_management import (
    DEFAULT_TASK_NAME,
    autostart_status,
    disable_autostart,
    enable_autostart,
)
from cli.channel_management import (
    channel_add,
    channel_disable,
    channel_enable,
    channel_list,
    channel_remove,
    channel_set_token,
    channel_status,
    channel_update,
)
from cli.config_management import (
    coerce_config_value,
    config_effective,
    config_get,
    config_set,
    config_show,
)
from cli.cron_management import (
    cron_create,
    cron_delete,
    cron_disable,
    cron_enable,
    cron_list,
    cron_update,
)
from cli.debug_management import (
    debug_model_probe,
    debug_status,
    debug_trace_clear,
    debug_trace_list,
    debug_trace_show,
)
from cli.doctor_management import doctor_config, doctor_settings
from cli.extensions_management import (
    extensions_disable,
    extensions_enable,
    extensions_list,
    extensions_reload,
    extensions_set,
    extensions_show,
)
from cli.log_management import log_list, log_read
from cli.model_management import model_list, model_refresh
from cli.parser import parse_args
from cli.project_management import (
    project_add,
    project_clear_override,
    project_list,
    project_remove,
    project_set,
    project_set_override,
    project_show,
)
from cli.prompt_management import (
    prompt_create,
    prompt_list,
    prompt_preview,
    prompt_remove,
    prompt_reset,
    prompt_reset_layout,
    prompt_set_layout,
    prompt_update,
)
from cli.provider_management import (
    provider_connect,
    provider_connect_status,
    provider_disconnect,
    provider_list,
    provider_set_enabled,
    provider_set_key,
    provider_status,
    provider_unset_key,
    provider_usage,
)
from cli.server_management import (
    DEFAULT_SERVICE_NAME,
    CommandResult,
    ServerInstance,
    get_status,
    resolve_instance,
    restart_via_systemd_if_managed,
    start_server,
    stop_server,
)
from cli.session_management import (
    session_create,
    session_delete,
    session_fork,
    session_link_channel,
    session_list,
    session_rename,
    session_set_compaction_policy,
)
from cli.skill_management import (
    skill_create,
    skill_delete,
    skill_list,
    skill_read,
    skill_remove_file,
    skill_update,
    skill_write_file,
)
from cli.statistics_management import statistics_report
from cli.task_model_management import (
    task_model_clear,
    task_model_list,
    task_model_options,
    task_model_set,
    task_model_targets,
)
from cli.tool_management import tool_list
from cli.uninstall_management import UninstallMode, UninstallResult, run_uninstall
from cli.update_management import run_update
from core.utils.config import APP_DIR, Config

SUCCESS_EXIT_CODE = 0
FAILURE_EXIT_CODE = 1


def _statistics_report_adapter(
    instance: ServerInstance, section: str, since: str | None, until: str | None
) -> CommandResult:
    """Adapt the keyword-only statistics report call to a positional signature.

    ``statistics_report`` takes ``since``/``until`` as keyword-only options; the
    dispatcher and its injected test double use a uniform positional signature,
    so this thin adapter bridges the two.
    """

    return statistics_report(instance, section, since=since, until=until)


def _launch_desktop(argv: Sequence[str]) -> None:
    """Open the Desktop window via its stable entrypoint.

    The Desktop launcher is imported lazily so the default CLI path never
    requires the optional ``[desktop]`` group (pywebview). A missing pywebview
    surfaces through the launcher's own ``load_webview`` message, which is
    raised as ``RuntimeError`` and turned into a failure exit by the caller.
    """

    from desktop.main import main as desktop_main

    desktop_main(list(argv))


@dataclass(frozen=True)
class ServerCommandContext:
    """Parsed server command target and service dispatch functions."""

    command: str
    host: str
    port: int | None
    data_dir: str | None
    service_name: str
    resolve: Callable[..., ServerInstance]
    start: Callable[[ServerInstance], CommandResult]
    stop: Callable[[ServerInstance], CommandResult]
    status: Callable[[ServerInstance], CommandResult]


def run(
    argv: Sequence[str] | None = None,
    *,
    resolve: Callable[..., ServerInstance] = resolve_instance,
    start: Callable[[ServerInstance], CommandResult] = start_server,
    stop: Callable[[ServerInstance], CommandResult] = stop_server,
    status: Callable[[ServerInstance], CommandResult] = get_status,
    list_agents: Callable[[ServerInstance], CommandResult] = agent_list,
    show_agent: Callable[[ServerInstance, str], CommandResult] = agent_show,
    create_agent: Callable[
        [ServerInstance, str, str, dict[str, Any]], CommandResult
    ] = agent_create,
    update_agent: Callable[[ServerInstance, str, dict[str, Any]], CommandResult] = agent_update,
    rename_agent: Callable[[ServerInstance, str, str], CommandResult] = agent_rename,
    delete_agent: Callable[[ServerInstance, str], CommandResult] = agent_delete,
    add_channel: Callable[
        [
            ServerInstance,
            str,
            str,
            str,
            str,
            str,
            Sequence[str],
            str,
            Sequence[str],
            Sequence[str],
            bool,
            str | None,
        ],
        CommandResult,
    ] = channel_add,
    list_channels: Callable[[ServerInstance], CommandResult] = channel_list,
    remove_channel: Callable[[ServerInstance, str], CommandResult] = channel_remove,
    update_channel: Callable[[ServerInstance, str, dict[str, Any]], CommandResult] = channel_update,
    enable_channel: Callable[[ServerInstance, str], CommandResult] = channel_enable,
    disable_channel: Callable[[ServerInstance, str], CommandResult] = channel_disable,
    channel_status_fn: Callable[[ServerInstance, str], CommandResult] = channel_status,
    set_channel_token: Callable[[ServerInstance, str, str], CommandResult] = channel_set_token,
    list_tools_fn: Callable[[ServerInstance], CommandResult] = tool_list,
    list_prompts_fn: Callable[[ServerInstance, str], CommandResult] = prompt_list,
    update_prompt_fn: Callable[[ServerInstance, str, str, str], CommandResult] = prompt_update,
    reset_prompt_fn: Callable[[ServerInstance, str, str], CommandResult] = prompt_reset,
    preview_prompt_fn: Callable[[ServerInstance, str, str], CommandResult] = prompt_preview,
    list_logs_fn: Callable[[ServerInstance], CommandResult] = log_list,
    read_log_fn: Callable[[ServerInstance, str], CommandResult] = log_read,
    list_providers: Callable[[ServerInstance], CommandResult] = provider_list,
    provider_status_fn: Callable[
        [ServerInstance, str, str | None], CommandResult
    ] = provider_status,
    provider_usage_fn: Callable[
        [ServerInstance, Sequence[str] | None], CommandResult
    ] = provider_usage,
    set_provider_key: Callable[
        [ServerInstance, str, str, str | None, bool, str | None], CommandResult
    ] = provider_set_key,
    list_models_fn: Callable[[ServerInstance, dict[str, Any]], CommandResult] = model_list,
    refresh_models_fn: Callable[[ServerInstance, str | None], CommandResult] = model_refresh,
    list_skills_fn: Callable[[ServerInstance], CommandResult] = skill_list,
    statistics_report_fn: Callable[
        [ServerInstance, str, str | None, str | None], CommandResult
    ] = _statistics_report_adapter,
    list_extensions_fn: Callable[[ServerInstance], CommandResult] = extensions_list,
    reload_extensions_fn: Callable[[ServerInstance], CommandResult] = extensions_reload,
    enable_extension_fn: Callable[[ServerInstance, str], CommandResult] = extensions_enable,
    disable_extension_fn: Callable[[ServerInstance, str], CommandResult] = extensions_disable,
    show_extension_fn: Callable[[ServerInstance, str], CommandResult] = extensions_show,
    set_extension_fn: Callable[[ServerInstance, str, str, str], CommandResult] = extensions_set,
    show_config_fn: Callable[[ServerInstance], CommandResult] = config_show,
    effective_config_fn: Callable[[ServerInstance], CommandResult] = config_effective,
    get_config_fn: Callable[[ServerInstance, str], CommandResult] = config_get,
    set_config_fn: Callable[[ServerInstance, str, Any], CommandResult] = config_set,
    doctor_settings_fn: Callable[[str | Path | None], CommandResult] = doctor_settings,
    doctor_config_fn: Callable[[str | Path | None], CommandResult] = doctor_config,
    launch_desktop_fn: Callable[[Sequence[str]], None] = _launch_desktop,
    uninstall_fn: Callable[..., UninstallResult] = run_uninstall,
) -> int:
    """Run the CLI and return an automation-safe process exit code."""

    args = parse_args(argv)
    if args.area == "home":
        config = Config(data_dir=Path(args.data_dir) if args.data_dir is not None else None)
        print(f"app_dir: {APP_DIR}")
        print(f"data_dir: {config.data_dir.expanduser().resolve()}")
        return SUCCESS_EXIT_CODE

    if args.area == "server":
        context = ServerCommandContext(
            command=args.command,
            host=args.host,
            port=args.port,
            data_dir=args.data_dir,
            service_name=getattr(args, "service_name", None) or DEFAULT_SERVICE_NAME,
            resolve=resolve,
            start=start,
            stop=stop,
            status=status,
        )
        result = dispatch_server_command(context)
        print_command_result(context.command, result)
        return exit_code_for(context.command, result)

    if args.area == "desktop":
        return dispatch_desktop_command(args, launch_desktop_fn=launch_desktop_fn)

    if args.area == "doctor":
        result = dispatch_doctor_command(
            args,
            doctor_settings_fn=doctor_settings_fn,
            doctor_config_fn=doctor_config_fn,
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "update":
        result = dispatch_update_command(args, resolve=resolve, stop=stop, start=start)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "uninstall":
        uninstall_result = uninstall_fn(
            mode=UninstallMode(args.uninstall_mode) if args.uninstall_mode else None,
            assume_yes=args.yes,
            host=args.host,
            port=args.port,
            data_dir=args.data_dir,
            task_name=args.task_name or DEFAULT_TASK_NAME,
            service_name=args.service_name or DEFAULT_SERVICE_NAME,
            resolve=resolve,
            stop=stop,
            start=start,
        )
        print(uninstall_result.message)
        return SUCCESS_EXIT_CODE if uninstall_result.ok else FAILURE_EXIT_CODE

    if args.area == "autostart":
        result = dispatch_autostart_command(args, resolve=resolve, start=start)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    instance = resolve(host=args.host, port=args.port, data_dir=args.data_dir)
    if args.area == "agent":
        result = dispatch_agent_command(
            args,
            instance,
            list_agents=list_agents,
            show_agent=show_agent,
            create_agent=create_agent,
            update_agent=update_agent,
            rename_agent=rename_agent,
            delete_agent=delete_agent,
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "project":
        result = dispatch_project_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "session":
        result = dispatch_session_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "channel":
        result = dispatch_channel_command(
            args,
            instance,
            add_channel=add_channel,
            list_channels=list_channels,
            remove_channel=remove_channel,
            update_channel=update_channel,
            enable_channel=enable_channel,
            disable_channel=disable_channel,
            channel_status_fn=channel_status_fn,
            set_channel_token=set_channel_token,
        )
        print_channel_command_result(args.command, result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "tool":
        result = dispatch_tool_command(args, instance, list_tools_fn=list_tools_fn)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "prompt":
        result = dispatch_prompt_command(
            args,
            instance,
            list_prompts_fn=list_prompts_fn,
            update_prompt_fn=update_prompt_fn,
            reset_prompt_fn=reset_prompt_fn,
            preview_prompt_fn=preview_prompt_fn,
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "log":
        result = dispatch_log_command(
            args,
            instance,
            list_logs_fn=list_logs_fn,
            read_log_fn=read_log_fn,
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "provider":
        result = dispatch_provider_command(
            args,
            instance,
            list_providers=list_providers,
            provider_status_fn=provider_status_fn,
            provider_usage_fn=provider_usage_fn,
            set_provider_key=set_provider_key,
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "model":
        result = dispatch_model_command(
            args,
            instance,
            list_models_fn=list_models_fn,
            refresh_models_fn=refresh_models_fn,
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "task-model":
        result = dispatch_task_model_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "skill":
        result = dispatch_skill_command(args, instance, list_skills_fn=list_skills_fn)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "extensions":
        result = dispatch_extensions_command(
            args,
            instance,
            list_extensions_fn=list_extensions_fn,
            reload_extensions_fn=reload_extensions_fn,
            enable_extension_fn=enable_extension_fn,
            disable_extension_fn=disable_extension_fn,
            show_extension_fn=show_extension_fn,
            set_extension_fn=set_extension_fn,
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "cron":
        result = dispatch_cron_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "statistics":
        result = dispatch_statistics_command(
            args, instance, statistics_report_fn=statistics_report_fn
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "config":
        result = dispatch_config_command(
            args,
            instance,
            show_config_fn=show_config_fn,
            effective_config_fn=effective_config_fn,
            get_config_fn=get_config_fn,
            set_config_fn=set_config_fn,
        )
        print_config_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "debug":
        result = dispatch_debug_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    raise ValueError(f"Unsupported command area: {args.area}")


def dispatch_agent_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_agents: Callable[[ServerInstance], CommandResult],
    show_agent: Callable[[ServerInstance, str], CommandResult],
    create_agent: Callable[[ServerInstance, str, str, dict[str, Any]], CommandResult],
    update_agent: Callable[[ServerInstance, str, dict[str, Any]], CommandResult],
    rename_agent: Callable[[ServerInstance, str, str], CommandResult],
    delete_agent: Callable[[ServerInstance, str], CommandResult],
) -> CommandResult:
    """Dispatch one parsed agent command against the server RPC client."""

    if args.command == "list":
        return list_agents(instance)
    if args.command == "show":
        return show_agent(instance, args.id)
    if args.command == "create":
        return create_agent(instance, args.id, args.name, _agent_changes_from_args(args))
    if args.command == "update":
        return update_agent(instance, args.id, _agent_changes_from_args(args))
    if args.command == "rename":
        return rename_agent(instance, args.id, args.new_id)
    if args.command == "delete":
        return delete_agent(instance, args.id)
    raise ValueError(f"Unsupported agent command: {args.command}")


def _agent_changes_from_args(args: argparse.Namespace) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if args.command == "update" and getattr(args, "name", None) is not None:
        changes["name"] = args.name
    if getattr(args, "clear_model", False):
        changes["model"] = ""
    elif args.model is not None:
        changes["model"] = args.model
    if getattr(args, "clear_fallback_model", False):
        changes["fallback_model"] = ""
    elif args.fallback_model is not None:
        changes["fallback_model"] = args.fallback_model
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
    if args.allowed_tools is not None:
        changes["allowed_tools"] = list(args.allowed_tools)
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


def dispatch_tool_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_tools_fn: Callable[[ServerInstance], CommandResult],
) -> CommandResult:
    """Dispatch one parsed tool command against the server RPC client."""

    if args.command == "list":
        return list_tools_fn(instance)
    raise ValueError(f"Unsupported tool command: {args.command}")


def dispatch_prompt_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_prompts_fn: Callable[[ServerInstance, str], CommandResult],
    update_prompt_fn: Callable[[ServerInstance, str, str, str], CommandResult],
    reset_prompt_fn: Callable[[ServerInstance, str, str], CommandResult],
    preview_prompt_fn: Callable[[ServerInstance, str, str], CommandResult],
    create_prompt_fn: Callable[
        [ServerInstance, str, str | None, int | None, str], CommandResult
    ] = prompt_create,
    remove_prompt_fn: Callable[[ServerInstance, str, str], CommandResult] = prompt_remove,
    set_prompt_layout_fn: Callable[
        [ServerInstance, list[object], str], CommandResult
    ] = prompt_set_layout,
    reset_prompt_layout_fn: Callable[[ServerInstance, str], CommandResult] = prompt_reset_layout,
) -> CommandResult:
    """Dispatch one parsed prompt command against the server RPC client."""

    if args.command == "list":
        return list_prompts_fn(instance, args.scope)
    if args.command == "update":
        try:
            content = _prompt_content_from_args(args)
        except (OSError, ValueError) as exc:
            return CommandResult(
                ok=False,
                message=f"cannot read prompt content file: {exc}",
                instance=instance,
            )
        return update_prompt_fn(instance, args.block_id, content, args.scope)
    if args.command == "reset":
        return reset_prompt_fn(instance, args.block_id, args.scope)
    if args.command == "create":
        try:
            create_content = _optional_content_from_args(args)
        except OSError as exc:
            return CommandResult(
                ok=False,
                message=f"cannot read prompt content file: {exc}",
                instance=instance,
            )
        return create_prompt_fn(instance, args.slug, create_content, args.position, args.scope)
    if args.command == "remove":
        return remove_prompt_fn(instance, args.block_id, args.scope)
    if args.command == "set-layout":
        return set_prompt_layout_fn(instance, args.layout_json, args.scope)
    if args.command == "reset-layout":
        return reset_prompt_layout_fn(instance, args.scope)
    if args.command == "preview":
        return preview_prompt_fn(instance, args.agent, args.scope)
    raise ValueError(f"Unsupported prompt command: {args.command}")


def dispatch_log_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_logs_fn: Callable[[ServerInstance], CommandResult],
    read_log_fn: Callable[[ServerInstance, str], CommandResult],
) -> CommandResult:
    """Dispatch one parsed log command against the server RPC client."""

    if args.command == "list":
        return list_logs_fn(instance)
    if args.command == "read":
        return read_log_fn(instance, args.file)
    raise ValueError(f"Unsupported log command: {args.command}")


def _prompt_content_from_args(args: argparse.Namespace) -> str:
    content = args.content
    if isinstance(content, str):
        return content
    content_file = args.content_file
    if not isinstance(content_file, str):
        raise ValueError("missing prompt content file")
    return Path(content_file).read_text(encoding="utf-8")


def _optional_content_from_args(args: argparse.Namespace) -> str | None:
    content = getattr(args, "content", None)
    if isinstance(content, str):
        return content
    content_file = getattr(args, "content_file", None)
    if isinstance(content_file, str):
        return Path(content_file).read_text(encoding="utf-8")
    return None


def dispatch_channel_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    add_channel: Callable[
        [
            ServerInstance,
            str,
            str,
            str,
            str,
            str,
            Sequence[str],
            str,
            Sequence[str],
            Sequence[str],
            bool,
            str | None,
        ],
        CommandResult,
    ],
    list_channels: Callable[[ServerInstance], CommandResult],
    remove_channel: Callable[[ServerInstance, str], CommandResult],
    update_channel: Callable[[ServerInstance, str, dict[str, Any]], CommandResult],
    enable_channel: Callable[[ServerInstance, str], CommandResult],
    disable_channel: Callable[[ServerInstance, str], CommandResult],
    channel_status_fn: Callable[[ServerInstance, str], CommandResult],
    set_channel_token: Callable[[ServerInstance, str, str], CommandResult],
) -> CommandResult:
    """Dispatch one parsed channel command against the server RPC client."""

    if args.command == "add":
        token: str | None = None
        if args.token_stdin:
            try:
                token = _read_stdin_utf8()
            except (OSError, UnicodeError) as exc:
                return CommandResult(
                    ok=False,
                    message=f"cannot read --token-stdin value as UTF-8: {exc}",
                    instance=instance,
                )
        return add_channel(
            instance,
            args.id,
            args.platform,
            args.agent,
            args.token_env,
            args.dm_scope,
            args.allow,
            args.response_mode,
            args.mention_patterns,
            args.owner_user_ids,
            args.observe_unaddressed == "true",
            token,
        )
    if args.command == "list":
        return list_channels(instance)
    if args.command == "remove":
        return remove_channel(instance, args.id)
    if args.command == "update":
        return update_channel(instance, args.id, _channel_changes_from_args(args))
    if args.command == "enable":
        return enable_channel(instance, args.id)
    if args.command == "disable":
        return disable_channel(instance, args.id)
    if args.command == "status":
        return channel_status_fn(instance, args.id)
    if args.command == "set-token":
        try:
            token = _read_stdin_utf8()
        except (OSError, UnicodeError) as exc:
            return CommandResult(
                ok=False,
                message=f"cannot read --stdin token as UTF-8: {exc}",
                instance=instance,
            )
        return set_channel_token(instance, args.id, token)
    raise ValueError(f"Unsupported channel command: {args.command}")


def _channel_changes_from_args(args: argparse.Namespace) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if args.platform is not None:
        changes["platform"] = args.platform
    if args.agent is not None:
        changes["agent_id"] = args.agent
    if args.token_env is not None:
        changes["token_env_var"] = args.token_env
    if args.dm_scope is not None:
        changes["dm_scope"] = args.dm_scope
    if args.allow is not None:
        changes["allowed_chat_ids"] = list(args.allow)
    if args.enabled is not None:
        changes["enabled"] = args.enabled == "true"
    if args.response_mode is not None:
        changes["response_mode"] = args.response_mode
    if args.mention_patterns is not None:
        changes["mention_patterns"] = list(args.mention_patterns)
    if args.owner_user_ids is not None:
        changes["owner_user_ids"] = list(args.owner_user_ids)
    if args.observe_unaddressed is not None:
        changes["observe_unaddressed"] = args.observe_unaddressed == "true"
    return changes


def dispatch_provider_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_providers: Callable[[ServerInstance], CommandResult],
    provider_status_fn: Callable[[ServerInstance, str, str | None], CommandResult],
    provider_usage_fn: Callable[[ServerInstance, Sequence[str] | None], CommandResult],
    set_provider_key: Callable[
        [ServerInstance, str, str, str | None, bool, str | None], CommandResult
    ],
    unset_provider_key_fn: Callable[
        [ServerInstance, str, str | None, str | None], CommandResult
    ] = provider_unset_key,
    connect_provider_fn: Callable[
        [ServerInstance, str, str, str | None], CommandResult
    ] = provider_connect,
    disconnect_provider_fn: Callable[
        [ServerInstance, str, str, str | None], CommandResult
    ] = provider_disconnect,
    connect_status_fn: Callable[
        [ServerInstance, str, str, str | None], CommandResult
    ] = provider_connect_status,
    set_enabled_fn: Callable[
        [ServerInstance, str, bool, str | None], CommandResult
    ] = provider_set_enabled,
) -> CommandResult:
    """Dispatch one parsed provider command against the server RPC client."""

    if args.command == "list":
        return list_providers(instance)
    if args.command == "status":
        return provider_status_fn(instance, args.provider, args.connection)
    if args.command == "usage":
        return provider_usage_fn(instance, args.connection)
    if args.command in ("enable", "disable"):
        return set_enabled_fn(instance, args.provider, args.command == "enable", args.connection)
    if args.command == "set-key":
        return set_provider_key(
            instance,
            args.provider,
            args.value,
            args.connection,
            args.refresh_models,
            args.account,
        )
    if args.command == "unset-key":
        return unset_provider_key_fn(instance, args.provider, args.connection, args.account)
    if args.command == "connect":
        return connect_provider_fn(instance, args.provider, args.connection, args.account)
    if args.command == "disconnect":
        return disconnect_provider_fn(instance, args.provider, args.connection, args.account)
    if args.command == "connect-status":
        return connect_status_fn(instance, args.provider, args.connection, args.account)
    raise ValueError(f"Unsupported provider command: {args.command}")


def dispatch_model_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_models_fn: Callable[[ServerInstance, dict[str, Any]], CommandResult],
    refresh_models_fn: Callable[[ServerInstance, str | None], CommandResult],
) -> CommandResult:
    """Dispatch one parsed model command against the server RPC client."""

    if args.command == "list":
        return list_models_fn(instance, _model_filters_from_args(args))
    if args.command == "refresh":
        return refresh_models_fn(instance, args.provider)
    raise ValueError(f"Unsupported model command: {args.command}")


def _model_filters_from_args(args: argparse.Namespace) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    for argument, rpc_field in (
        ("provider_id", "provider_id"),
        ("capability", "capabilities"),
        ("task", "tasks"),
        ("input_modality", "input_modalities"),
        ("output_modality", "output_modalities"),
        ("min_context_window", "min_context_window"),
    ):
        value = getattr(args, argument, None)
        if value is not None:
            filters[rpc_field] = value
    return filters


def dispatch_task_model_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_bindings_fn: Callable[[ServerInstance], CommandResult] = task_model_list,
    list_targets_fn: Callable[[ServerInstance, str], CommandResult] = task_model_targets,
    show_options_fn: Callable[[ServerInstance, str, str], CommandResult] = task_model_options,
    set_binding_fn: Callable[
        [ServerInstance, str, str, str | None], CommandResult
    ] = task_model_set,
    clear_binding_fn: Callable[[ServerInstance, str], CommandResult] = task_model_clear,
) -> CommandResult:
    """Dispatch one parsed task-model command against the server RPC client."""

    if args.command == "list":
        return list_bindings_fn(instance)
    if args.command == "targets":
        return list_targets_fn(instance, args.task_type)
    if args.command == "options":
        return show_options_fn(instance, args.task_type, args.target)
    if args.command == "set":
        return set_binding_fn(instance, args.task_type, args.target, args.options_json)
    if args.command == "clear":
        return clear_binding_fn(instance, args.task_type)
    raise ValueError(f"Unsupported task-model command: {args.command}")


def dispatch_skill_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_skills_fn: Callable[[ServerInstance], CommandResult],
    read_skills_fn: Callable[[ServerInstance, str], CommandResult] = skill_read,
    create_skill_fn: Callable[
        [ServerInstance, str, str, str, str | None], CommandResult
    ] = skill_create,
    update_skill_fn: Callable[
        [ServerInstance, str, str, str, str | None], CommandResult
    ] = skill_update,
    delete_skill_fn: Callable[[ServerInstance, str, str, bool], CommandResult] = skill_delete,
    write_skill_file_fn: Callable[
        [ServerInstance, str, str, str, str], CommandResult
    ] = skill_write_file,
    remove_skill_file_fn: Callable[
        [ServerInstance, str, str, str, bool], CommandResult
    ] = skill_remove_file,
) -> CommandResult:
    """Dispatch one parsed skill command against the server RPC client."""

    if args.command == "list":
        return list_skills_fn(instance)
    if args.command == "read":
        return read_skills_fn(instance, args.scope)
    if args.command in {"create", "update", "write-file"}:
        try:
            content = _prompt_content_from_args(args)
        except (OSError, ValueError) as exc:
            return CommandResult(
                ok=False,
                message=f"cannot read skill content file: {exc}",
                instance=instance,
            )
        if args.command == "create":
            return create_skill_fn(instance, args.scope, args.name, content, args.source)
        if args.command == "update":
            return update_skill_fn(instance, args.scope, args.name, content, args.source)
        return write_skill_file_fn(instance, args.scope, args.name, args.path, content)
    if args.command == "delete":
        return delete_skill_fn(instance, args.scope, args.name, args.yes)
    if args.command == "remove-file":
        return remove_skill_file_fn(instance, args.scope, args.name, args.path, args.yes)
    raise ValueError(f"Unsupported skill command: {args.command}")


def dispatch_extensions_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_extensions_fn: Callable[[ServerInstance], CommandResult],
    reload_extensions_fn: Callable[[ServerInstance], CommandResult],
    enable_extension_fn: Callable[[ServerInstance, str], CommandResult],
    disable_extension_fn: Callable[[ServerInstance, str], CommandResult],
    show_extension_fn: Callable[[ServerInstance, str], CommandResult],
    set_extension_fn: Callable[[ServerInstance, str, str, str], CommandResult],
) -> CommandResult:
    """Route one name-first extensions command against the server RPC client.

    Grammar: ``list`` | ``reload`` | ``enable|disable <name>`` | ``<name>`` (show
    settings) | ``<name> set <field> <value>`` (write one setting). The selector is
    either a reserved verb or an extension name; a name is inspected or configured.
    """

    selector = args.selector
    rest = list(args.rest)

    if selector == "list":
        if rest:
            return _extensions_usage(instance, "extensions list takes no arguments")
        return list_extensions_fn(instance)

    if selector == "reload":
        if rest:
            return _extensions_usage(instance, "extensions reload takes no arguments")
        return reload_extensions_fn(instance)

    if selector in ("enable", "disable"):
        if len(rest) != 1:
            return _extensions_usage(instance, f"usage: extensions {selector} <name>")
        toggle = enable_extension_fn if selector == "enable" else disable_extension_fn
        return toggle(instance, rest[0])

    # Otherwise the selector is an extension name: inspect or configure it.
    name = selector
    if not rest:
        return show_extension_fn(instance, name)
    if rest[0] == "set":
        return _dispatch_extensions_set(args, instance, name, rest, set_extension_fn)
    return _extensions_usage(
        instance,
        f"unknown command 'extensions {name} {rest[0]}'; "
        f"use 'extensions {name}' or 'extensions {name} set <field> <value>'",
    )


def _dispatch_extensions_set(
    args: argparse.Namespace,
    instance: ServerInstance,
    name: str,
    rest: list[str],
    set_extension_fn: Callable[[ServerInstance, str, str, str], CommandResult],
) -> CommandResult:
    """Parse ``<name> set <field> <value>`` (or ``--stdin``) and delegate."""

    if args.stdin:
        if len(rest) != 2:
            return _extensions_usage(instance, f"usage: extensions {name} set <field> --stdin")
        field = rest[1]
        try:
            value = _read_stdin_utf8()
        except (OSError, UnicodeError) as exc:
            return CommandResult(
                ok=False,
                message=f"cannot read --stdin value as UTF-8: {exc}",
                instance=instance,
            )
    else:
        if len(rest) != 3:
            return _extensions_usage(
                instance, f"usage: extensions {name} set <field> <value>  (or --stdin)"
            )
        field = rest[1]
        value = rest[2]
    return set_extension_fn(instance, name, field, value)


def _read_stdin_utf8() -> str:
    """Read a piped secret or setting value through an explicit UTF-8 contract."""

    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        return sys.stdin.read().rstrip("\r\n")
    raw_value = buffer.read()
    if isinstance(raw_value, bytes):
        return raw_value.decode("utf-8-sig").rstrip("\r\n")
    return str(raw_value).rstrip("\r\n")


def _extensions_usage(instance: ServerInstance, message: str) -> CommandResult:
    return CommandResult(ok=False, message=message, instance=instance)


def dispatch_cron_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    create_cron_fn: Callable[[ServerInstance, dict[str, Any]], CommandResult] = cron_create,
    list_cron_fn: Callable[[ServerInstance], CommandResult] = cron_list,
    update_cron_fn: Callable[[ServerInstance, str, dict[str, Any]], CommandResult] = cron_update,
    delete_cron_fn: Callable[[ServerInstance, str], CommandResult] = cron_delete,
    enable_cron_fn: Callable[[ServerInstance, str], CommandResult] = cron_enable,
    disable_cron_fn: Callable[[ServerInstance, str], CommandResult] = cron_disable,
) -> CommandResult:
    """Dispatch one parsed cron command against the server RPC client."""

    if args.command == "list":
        return list_cron_fn(instance)
    if args.command == "create":
        return create_cron_fn(instance, _cron_create_fields_from_args(args))
    if args.command == "update":
        return update_cron_fn(instance, args.id, _cron_changes_from_args(args))
    if args.command == "delete":
        return delete_cron_fn(instance, args.id)
    if args.command == "enable":
        return enable_cron_fn(instance, args.id)
    if args.command == "disable":
        return disable_cron_fn(instance, args.id)
    raise ValueError(f"Unsupported cron command: {args.command}")


def _cron_create_fields_from_args(args: argparse.Namespace) -> dict[str, Any]:
    fields: dict[str, Any] = {"agent_id": args.agent, "prompt": args.prompt}
    if args.cron is not None:
        fields["schedule_type"] = "cron"
        fields["cron_expression"] = args.cron
    else:
        fields["schedule_type"] = "once"
        fields["run_at"] = args.at
    if args.timezone is not None:
        fields["timezone"] = args.timezone
    if args.session is not None:
        fields["session_id"] = args.session
    return fields


def _cron_changes_from_args(args: argparse.Namespace) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if args.agent is not None:
        changes["agent_id"] = args.agent
    if args.prompt is not None:
        changes["prompt"] = args.prompt
    if args.cron is not None:
        changes["schedule_type"] = "cron"
        changes["cron_expression"] = args.cron
    elif args.at is not None:
        changes["schedule_type"] = "once"
        changes["run_at"] = args.at
    if args.timezone is not None:
        changes["timezone"] = args.timezone
    if args.session is not None:
        changes["session_id"] = args.session
    if args.status is not None:
        changes["status"] = args.status
    return changes


def dispatch_statistics_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    statistics_report_fn: Callable[
        [ServerInstance, str, str | None, str | None], CommandResult
    ] = _statistics_report_adapter,
) -> CommandResult:
    """Dispatch one parsed statistics command against the server RPC client.

    The subcommand name is the report section; ``--since``/``--until`` pass
    through verbatim (``None`` when the flag was omitted) so the server owns
    their validation.
    """

    return statistics_report_fn(instance, args.command, args.since, args.until)


def dispatch_debug_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    debug_status_fn: Callable[[ServerInstance], CommandResult] = debug_status,
    trace_list_fn: Callable[[ServerInstance], CommandResult] = debug_trace_list,
    trace_show_fn: Callable[[ServerInstance, str], CommandResult] = debug_trace_show,
    trace_clear_fn: Callable[[ServerInstance], CommandResult] = debug_trace_clear,
    model_probe_fn: Callable[[ServerInstance, str, str], CommandResult] = debug_model_probe,
) -> CommandResult:
    """Dispatch one parsed debug command against the server RPC client."""

    if args.command == "status":
        return debug_status_fn(instance)
    if args.command == "traces":
        return trace_list_fn(instance)
    if args.command == "trace":
        return trace_show_fn(instance, args.trace_id)
    if args.command == "clear":
        return trace_clear_fn(instance)
    if args.command == "probe":
        return model_probe_fn(instance, args.provider, args.connection)
    raise ValueError(f"Unsupported debug command: {args.command}")


def dispatch_config_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    show_config_fn: Callable[[ServerInstance], CommandResult],
    effective_config_fn: Callable[[ServerInstance], CommandResult],
    get_config_fn: Callable[[ServerInstance, str], CommandResult],
    set_config_fn: Callable[[ServerInstance, str, Any], CommandResult],
) -> CommandResult:
    """Dispatch one parsed config command against the server RPC client."""

    if args.command is None:
        return show_config_fn(instance)
    if args.command == "effective":
        return effective_config_fn(instance)
    if args.command == "get":
        return get_config_fn(instance, args.key)
    if args.command == "set":
        coerced = coerce_config_value(args.value)
        return set_config_fn(instance, args.key, coerced)
    raise ValueError(f"Unsupported config command: {args.command}")


def dispatch_doctor_command(
    args: argparse.Namespace,
    *,
    doctor_settings_fn: Callable[[str | Path | None], CommandResult],
    doctor_config_fn: Callable[[str | Path | None], CommandResult],
) -> CommandResult:
    """Dispatch one parsed local doctor command."""

    if args.command == "settings":
        return doctor_settings_fn(args.data_dir)
    if args.command == "config":
        return doctor_config_fn(args.data_dir)
    raise ValueError(f"Unsupported doctor command: {args.command}")


def dispatch_autostart_command(
    args: argparse.Namespace,
    *,
    resolve: Callable[..., ServerInstance],
    start: Callable[[ServerInstance], CommandResult],
    enable_fn: Callable[..., CommandResult] = enable_autostart,
    disable_fn: Callable[..., CommandResult] = disable_autostart,
    status_fn: Callable[..., CommandResult] = autostart_status,
) -> CommandResult:
    """Dispatch one parsed autostart command against the local OS."""

    instance = resolve(host=args.host, port=args.port, data_dir=args.data_dir)
    if args.command == "enable":
        return enable_fn(
            instance, start=start, task_name=args.task_name, service_name=args.service_name
        )
    if args.command == "disable":
        return disable_fn(instance, task_name=args.task_name, service_name=args.service_name)
    if args.command == "status":
        return status_fn(instance, task_name=args.task_name, service_name=args.service_name)
    raise ValueError(f"Unsupported autostart command: {args.command}")


def dispatch_update_command(
    args: argparse.Namespace,
    *,
    resolve: Callable[..., ServerInstance],
    stop: Callable[[ServerInstance], CommandResult],
    start: Callable[[ServerInstance], CommandResult],
    run_update_fn: Callable[..., CommandResult] = run_update,
) -> CommandResult:
    """Run the local self-update against the resolved server target."""

    instance = resolve(host=args.host, port=args.port, data_dir=args.data_dir)
    return run_update_fn(
        instance,
        discard=args.discard,
        stash=args.stash,
        restart=not args.no_restart,
        stop=stop,
        start=start,
        service_name=getattr(args, "service_name", None) or DEFAULT_SERVICE_NAME,
    )


def dispatch_desktop_command(
    args: argparse.Namespace,
    *,
    launch_desktop_fn: Callable[[Sequence[str]], None],
) -> int:
    """Launch the Desktop window locally and return a stable exit code.

    This is a local GUI-launch action, not an RPC management command: it
    branches before the shared ``resolve(...)`` and never builds a
    ``ServerInstance``. Only the flags the user actually supplied are forwarded,
    so a bare ``vbot desktop`` reaches the launcher's last-used auto-connect path
    instead of a silent localhost target. The call blocks until the window
    closes; a missing ``[desktop]`` group raises ``RuntimeError`` (the launcher's
    own install hint), which maps to a failure exit.
    """

    launch_argv = _desktop_launch_argv(args)
    try:
        launch_desktop_fn(launch_argv)
    except RuntimeError as exc:
        print(f"error: {exc}")
        return FAILURE_EXIT_CODE
    print("desktop window closed")
    return SUCCESS_EXIT_CODE


def _desktop_launch_argv(args: argparse.Namespace) -> list[str]:
    """Build the Desktop launcher argv from only the supplied target flags."""

    launch_argv: list[str] = []
    if args.host is not None:
        launch_argv.extend(["--host", args.host])
    if args.port is not None:
        launch_argv.extend(["--port", str(args.port)])
    return launch_argv


def dispatch_server_command(context: ServerCommandContext) -> CommandResult:
    """Resolve the target and dispatch the requested server command."""

    instance = context.resolve(host=context.host, port=context.port, data_dir=context.data_dir)
    if context.command == "start":
        return context.start(instance)
    if context.command == "stop":
        return context.stop(instance)
    if context.command == "restart":
        via_systemd = restart_via_systemd_if_managed(instance, service_name=context.service_name)
        if via_systemd is not None:
            return via_systemd
        stop_result = context.stop(instance)
        if not stop_result.ok:
            return stop_result
        restarted_instance = context.resolve(
            host=context.host,
            port=context.port,
            data_dir=context.data_dir,
        )
        return context.start(restarted_instance)
    if context.command == "status":
        return context.status(instance)
    raise ValueError(f"Unsupported server command: {context.command}")


def print_command_result(command: str, result: CommandResult) -> None:
    """Print deterministic plain-text server command output."""

    lines = [f"command: server {command}", f"result: {_result_message(result)}"]
    if command in {"start", "restart"}:
        lines.extend(_start_like_output_lines(result))
    elif command == "stop":
        lines.extend(_stop_output_lines(result))
    elif command == "status":
        lines.extend(_status_output_lines(result))
    else:
        raise ValueError(f"Unsupported server command: {command}")

    print("\n".join(lines))


def print_channel_command_result(command: str, result: CommandResult) -> None:
    """Print deterministic plain-text channel command output."""

    lines = [
        f"command: channel {command}",
        f"result: {_result_message(result)}",
        f"url: {result.instance.url}",
        f"data_dir: {result.instance.data_dir}",
    ]
    print("\n".join(lines))


def print_management_command_result(result: CommandResult) -> None:
    """Print plain-text output for non-channel RPC management command areas."""

    print(_result_message(result))


def print_config_command_result(result: CommandResult) -> None:
    """Print deterministic plain-text config command output."""

    print(_result_message(result))


def _result_message(result: CommandResult) -> str:
    message = result.message.strip()
    if message:
        return result.message
    if result.ok:
        return "success: command completed without details"
    return "error: command failed without details"


def exit_code_for(command: str, result: CommandResult) -> int:
    """Map service outcomes to stable CLI exit codes."""

    if result.ok:
        return SUCCESS_EXIT_CODE
    if command == "status" and _is_non_vbot_conflict(result):
        return SUCCESS_EXIT_CODE
    return FAILURE_EXIT_CODE


def _running_text(result: CommandResult) -> str:
    if result.health and result.health.is_vbot:
        return "yes"
    if result.message in {"already running", "running", "started"}:
        return "yes"
    return "no"


def _webui_text(result: CommandResult) -> str:
    if result.webui is None:
        return "unknown"
    if result.webui.available:
        return "available"
    return "unavailable"


def _start_like_output_lines(result: CommandResult) -> list[str]:
    lines = [
        f"running: {_running_text(result)}",
        f"url: {result.instance.url}",
    ]
    if result.webui is not None:
        lines.append(f"webui: {_webui_text(result)}")
    lines.append(f"data_dir: {result.instance.data_dir}")
    lines.append(f"log_path: {_log_path_text(result)}")
    if result.process_id is not None:
        lines.append(f"process_id: {result.process_id}")
    if _is_non_vbot_conflict(result):
        lines.append("conflict: port occupied by non-vBot process")
    return lines


def _stop_output_lines(result: CommandResult) -> list[str]:
    lines = [
        f"url: {result.instance.url}",
        f"data_dir: {result.instance.data_dir}",
    ]
    if result.process_id is not None:
        lines.append(f"process_id: {result.process_id}")
    if result.forced:
        lines.append("forced: true")
    if _is_non_vbot_conflict(result):
        lines.append("conflict: port occupied by non-vBot process")
    return lines


def _status_output_lines(result: CommandResult) -> list[str]:
    lines = [
        f"running: {_running_text(result)}",
        f"url: {result.instance.url}",
        f"webui: {_webui_text(result)}",
        f"data_dir: {result.instance.data_dir}",
        f"log_path: {_log_path_text(result)}",
    ]
    if _is_non_vbot_conflict(result):
        lines.append("conflict: port occupied by non-vBot process")
    return lines


def _log_path_text(result: CommandResult) -> Path:
    return result.log_path or result.instance.log_path


def _is_non_vbot_conflict(result: CommandResult) -> bool:
    return result.message == "port occupied by non-vBot process"


def main(argv: Sequence[str] | None = None) -> None:
    """Process entrypoint."""

    _configure_console_output()
    sys.exit(run(argv))


def _configure_console_output() -> None:
    """Emit deterministic UTF-8 without crashing on legacy Windows code pages."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            # Imported/test streams may expose ``reconfigure`` while refusing an
            # encoding change. CLI output still uses the stream's own contract.
            continue


if __name__ == "__main__":
    main()
