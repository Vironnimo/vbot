"""CLI entrypoint and command routing with injectable management operations."""

from __future__ import annotations

import sys
from pathlib import Path

# Resolve this checkout before an installed package for python cli/main.py.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collections.abc import Callable, Sequence
from typing import Any

from cli._dispatch_agents import (
    _agent_changes_from_args,
    _project_add_fields_from_args,
    _project_set_changes_from_args,
    dispatch_agent_command,
    dispatch_project_command,
    dispatch_session_command,
    dispatch_session_store_command,
)
from cli._dispatch_connections import (
    _channel_changes_from_args,
    _model_filters_from_args,
    dispatch_channel_command,
    dispatch_extensions_command,
    dispatch_model_command,
    dispatch_provider_command,
    dispatch_task_model_command,
)
from cli._dispatch_lifecycle import (
    ServerCommandContext,
    _launch_desktop,
    dispatch_autostart_command,
    dispatch_desktop_command,
    dispatch_doctor_command,
    dispatch_server_command,
    dispatch_update_command,
)
from cli._dispatch_operations import (
    _statistics_report_adapter,
    dispatch_bootstrap_command,
    dispatch_config_command,
    dispatch_cron_command,
    dispatch_debug_command,
    dispatch_log_command,
    dispatch_memory_command,
    dispatch_prompt_command,
    dispatch_skill_command,
    dispatch_statistics_command,
    dispatch_tool_command,
)
from cli._output import (
    FAILURE_EXIT_CODE,
    SUCCESS_EXIT_CODE,
    exit_code_for,
    print_channel_command_result,
    print_command_result,
    print_config_command_result,
    print_management_command_result,
    print_server_command_start,
    print_update_command_result,
    print_update_command_start,
)
from cli.agent_management import (
    agent_create,
    agent_delete,
    agent_list,
    agent_rename,
    agent_show,
    agent_update,
)
from cli.autostart_management import DEFAULT_TASK_NAME
from cli.channel_management import (
    channel_access,
    channel_add,
    channel_disable,
    channel_enable,
    channel_grant_admin,
    channel_identity,
    channel_list,
    channel_remove,
    channel_revoke_admin,
    channel_set_token,
    channel_status,
    channel_update,
)
from cli.config_management import (
    config_describe,
    config_effective,
    config_get,
    config_list,
    config_patch,
    config_raw,
    config_set,
    config_unset,
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
from cli.model_management import model_list, model_refresh, model_show
from cli.parser import parse_args
from cli.prompt_management import prompt_list, prompt_preview, prompt_reset, prompt_update
from cli.provider_management import provider_list, provider_set_key, provider_status, provider_usage
from cli.server_management import (
    DEFAULT_SERVICE_NAME,
    CommandResult,
    ServerInstance,
    get_status,
    resolve_instance,
    start_server,
    stop_server,
)
from cli.skill_management import list_skills
from cli.tool_management import tool_list
from cli.uninstall_management import UninstallMode, UninstallResult, run_uninstall
from cli.update_management import read_checkout_version
from core.utils.config import VBOT_ROOT, Config


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
    channel_identity_fn: Callable[
        [ServerInstance, str, str | None], CommandResult
    ] = channel_identity,
    channel_access_fn: Callable[[ServerInstance, str, str], CommandResult] = channel_access,
    grant_channel_admin_fn: Callable[
        [ServerInstance, str, str, str], CommandResult
    ] = channel_grant_admin,
    revoke_channel_admin_fn: Callable[
        [ServerInstance, str, str, str], CommandResult
    ] = channel_revoke_admin,
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
    show_model_fn: Callable[[ServerInstance, str], CommandResult] = model_show,
    refresh_models_fn: Callable[[ServerInstance, str | None], CommandResult] = model_refresh,
    list_skills_fn: Callable[[ServerInstance], CommandResult] = list_skills,
    statistics_report_fn: Callable[
        [ServerInstance, str, str | None, str | None], CommandResult
    ] = _statistics_report_adapter,
    list_extensions_fn: Callable[[ServerInstance], CommandResult] = extensions_list,
    reload_extensions_fn: Callable[[ServerInstance], CommandResult] = extensions_reload,
    enable_extension_fn: Callable[[ServerInstance, str], CommandResult] = extensions_enable,
    disable_extension_fn: Callable[[ServerInstance, str], CommandResult] = extensions_disable,
    show_extension_fn: Callable[[ServerInstance, str], CommandResult] = extensions_show,
    set_extension_fn: Callable[[ServerInstance, str, str, str], CommandResult] = extensions_set,
    raw_config_fn: Callable[[ServerInstance], CommandResult] = config_raw,
    list_config_fn: Callable[[ServerInstance, str | None], CommandResult] = config_list,
    describe_config_fn: Callable[[ServerInstance, str], CommandResult] = config_describe,
    effective_config_fn: Callable[[ServerInstance], CommandResult] = config_effective,
    get_config_fn: Callable[[ServerInstance, str], CommandResult] = config_get,
    set_config_fn: Callable[[ServerInstance, str, Any], CommandResult] = config_set,
    unset_config_fn: Callable[[ServerInstance, str], CommandResult] = config_unset,
    patch_config_fn: Callable[[ServerInstance, list[dict[str, Any]]], CommandResult] = config_patch,
    doctor_settings_fn: Callable[[str | Path | None], CommandResult] = doctor_settings,
    doctor_config_fn: Callable[[str | Path | None], CommandResult] = doctor_config,
    launch_desktop_fn: Callable[[Sequence[str]], None] = _launch_desktop,
    uninstall_fn: Callable[..., UninstallResult] = run_uninstall,
) -> int:
    """Run the CLI and return an automation-safe process exit code."""

    args = parse_args(argv)
    if args.area == "home":
        config = Config(data_dir=Path(args.data_dir) if args.data_dir is not None else None)
        print(f"vbot_root: {VBOT_ROOT}")
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
        result = dispatch_server_command(context, announce=print_server_command_start)
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
        version_before = read_checkout_version()
        print_update_command_start(version_before)
        result = dispatch_update_command(args, resolve=resolve, stop=stop, start=start)
        print_update_command_result(
            result,
            version_before=version_before,
            version_after=read_checkout_version(),
        )
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

    if args.area == "session-store":
        result = dispatch_session_store_command(args, instance)
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
            channel_identity_fn=channel_identity_fn,
            channel_access_fn=channel_access_fn,
            grant_channel_admin_fn=grant_channel_admin_fn,
            revoke_channel_admin_fn=revoke_channel_admin_fn,
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
            show_model_fn=show_model_fn,
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

    if args.area == "memory":
        result = dispatch_memory_command(args, instance)
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
    if args.area == "bootstrap":
        result = dispatch_bootstrap_command(args, instance)
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
            raw_config_fn=raw_config_fn,
            list_config_fn=list_config_fn,
            describe_config_fn=describe_config_fn,
            effective_config_fn=effective_config_fn,
            get_config_fn=get_config_fn,
            set_config_fn=set_config_fn,
            unset_config_fn=unset_config_fn,
            patch_config_fn=patch_config_fn,
        )
        print_config_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "debug":
        result = dispatch_debug_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    raise ValueError(f"Unsupported command area: {args.area}")


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


__all__ = [
    "main",
    "run",
    "parse_args",
    "exit_code_for",
    "print_command_result",
    "print_management_command_result",
    "print_channel_command_result",
    "print_update_command_result",
    "print_update_command_start",
    "_agent_changes_from_args",
    "_channel_changes_from_args",
    "_model_filters_from_args",
    "_project_add_fields_from_args",
    "_project_set_changes_from_args",
]


if __name__ == "__main__":
    main()
