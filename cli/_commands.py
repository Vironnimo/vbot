"""CLI command routing with injectable management operations.

An area's dispatcher and management operations load only when that area runs, so
help, parse errors and local commands import no HTTP, database or Settings stack
(``cli.md`` -> Interfaces). An operation a caller does not inject is the real one.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cli._output import (
    FAILURE_EXIT_CODE,
    SUCCESS_EXIT_CODE,
    command_arguments,
    exit_code_for,
    print_channel_command_result,
    print_chat_command_result,
    print_command_result,
    print_config_command_result,
    print_management_command_result,
    print_server_command_start,
    with_command_output,
)
from cli.parser import parse_args
from core.utils.config import VBOT_ROOT, Config

if TYPE_CHECKING:
    from cli._server_target import CommandResult, ServerInstance

__all__ = [
    "run",
    "parse_args",
    "exit_code_for",
    "print_command_result",
    "print_management_command_result",
    "print_channel_command_result",
]


@with_command_output
def run(
    argv: Sequence[str] | None = None,
    *,
    resolve: Callable[..., ServerInstance] | None = None,
    start: Callable[[ServerInstance], CommandResult] | None = None,
    stop: Callable[[ServerInstance], CommandResult] | None = None,
    status: Callable[[ServerInstance], CommandResult] | None = None,
    list_agents: Callable[[ServerInstance], CommandResult] | None = None,
    show_agent: Callable[[ServerInstance, str], CommandResult] | None = None,
    create_agent: Callable[[ServerInstance, str, str, dict[str, Any]], CommandResult] | None = None,
    update_agent: Callable[[ServerInstance, str, dict[str, Any]], CommandResult] | None = None,
    rename_agent: Callable[[ServerInstance, str, str], CommandResult] | None = None,
    delete_agent: Callable[[ServerInstance, str, bool, bool], CommandResult] | None = None,
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
    ]
    | None = None,
    list_channels: Callable[[ServerInstance], CommandResult] | None = None,
    remove_channel: Callable[[ServerInstance, str], CommandResult] | None = None,
    update_channel: Callable[[ServerInstance, str, dict[str, Any]], CommandResult] | None = None,
    enable_channel: Callable[[ServerInstance, str], CommandResult] | None = None,
    disable_channel: Callable[[ServerInstance, str], CommandResult] | None = None,
    channel_status_fn: Callable[[ServerInstance, str], CommandResult] | None = None,
    set_channel_token: Callable[[ServerInstance, str, str], CommandResult] | None = None,
    channel_identity_fn: Callable[[ServerInstance, str, str | None], CommandResult] | None = None,
    channel_access_fn: Callable[[ServerInstance, str, str], CommandResult] | None = None,
    grant_channel_admin_fn: Callable[[ServerInstance, str, str, str], CommandResult] | None = None,
    revoke_channel_admin_fn: Callable[[ServerInstance, str, str, str], CommandResult] | None = None,
    list_tools_fn: Callable[[ServerInstance], CommandResult] | None = None,
    list_prompts_fn: Callable[[ServerInstance, str], CommandResult] | None = None,
    update_prompt_fn: Callable[[ServerInstance, str, str, str], CommandResult] | None = None,
    reset_prompt_fn: Callable[[ServerInstance, str, str], CommandResult] | None = None,
    preview_prompt_fn: Callable[[ServerInstance, str, str], CommandResult] | None = None,
    list_logs_fn: Callable[[ServerInstance], CommandResult] | None = None,
    read_log_fn: Callable[[ServerInstance, str], CommandResult] | None = None,
    list_providers: Callable[..., CommandResult] | None = None,
    provider_status_fn: Callable[[ServerInstance, str, str | None], CommandResult] | None = None,
    provider_usage_fn: Callable[[ServerInstance, Sequence[str] | None], CommandResult]
    | None = None,
    set_provider_key: Callable[
        [ServerInstance, str, str, str | None, bool, str | None], CommandResult
    ]
    | None = None,
    list_models_fn: Callable[[ServerInstance, dict[str, Any]], CommandResult] | None = None,
    show_model_fn: Callable[[ServerInstance, str], CommandResult] | None = None,
    refresh_models_fn: Callable[[ServerInstance, str | None], CommandResult] | None = None,
    list_skills_fn: Callable[[ServerInstance], CommandResult] | None = None,
    statistics_report_fn: Callable[[ServerInstance, str, str | None, str | None], CommandResult]
    | None = None,
    list_extensions_fn: Callable[[ServerInstance], CommandResult] | None = None,
    reload_extensions_fn: Callable[[ServerInstance], CommandResult] | None = None,
    enable_extension_fn: Callable[[ServerInstance, str], CommandResult] | None = None,
    disable_extension_fn: Callable[[ServerInstance, str], CommandResult] | None = None,
    show_extension_fn: Callable[[ServerInstance, str], CommandResult] | None = None,
    set_extension_fn: Callable[[ServerInstance, str, str, str], CommandResult] | None = None,
    raw_config_fn: Callable[[ServerInstance], CommandResult] | None = None,
    list_config_fn: Callable[[ServerInstance, str | None], CommandResult] | None = None,
    describe_config_fn: Callable[[ServerInstance, str], CommandResult] | None = None,
    effective_config_fn: Callable[[ServerInstance], CommandResult] | None = None,
    get_config_fn: Callable[[ServerInstance, str], CommandResult] | None = None,
    set_config_fn: Callable[[ServerInstance, str, Any], CommandResult] | None = None,
    unset_config_fn: Callable[[ServerInstance, str], CommandResult] | None = None,
    patch_config_fn: Callable[[ServerInstance, list[dict[str, Any]]], CommandResult] | None = None,
    doctor_settings_fn: Callable[[str | Path | None], CommandResult] | None = None,
    doctor_config_fn: Callable[[str | Path | None], CommandResult] | None = None,
    launch_desktop_fn: Callable[[Sequence[str]], None] | None = None,
) -> int:
    """Run the CLI and return an automation-safe process exit code."""

    args = command_arguments()
    from cli.application.command import dispatch as dispatch_application

    application_result = dispatch_application(args)
    if application_result is not None:
        return application_result
    if args.area == "home":
        config = Config(data_dir=Path(args.data_dir) if args.data_dir is not None else None)
        print(f"vbot_root: {VBOT_ROOT}")
        print(f"data_dir: {config.data_dir.expanduser().resolve()}")
        return SUCCESS_EXIT_CODE

    if args.area == "server":
        from cli import server_management as servers
        from cli._dispatch_lifecycle import ServerCommandContext, dispatch_server_command

        context = ServerCommandContext(
            command=args.command,
            host=args.host,
            port=args.port,
            data_dir=args.data_dir,
            resolve=resolve or servers.resolve_instance,
            start=start or servers.start_server,
            stop=stop or servers.stop_server,
            status=status or servers.get_status,
        )
        result = dispatch_server_command(context, announce=print_server_command_start)
        print_command_result(context.command, result)
        return exit_code_for(context.command, result)

    if args.area == "desktop":
        from cli._dispatch_lifecycle import _launch_desktop, dispatch_desktop_command

        return dispatch_desktop_command(
            args, launch_desktop_fn=launch_desktop_fn or _launch_desktop
        )

    if args.area == "doctor":
        from cli import doctor_management as doctor
        from cli._dispatch_lifecycle import dispatch_doctor_command

        result = dispatch_doctor_command(
            args,
            doctor_settings_fn=doctor_settings_fn or doctor.doctor_settings,
            doctor_config_fn=doctor_config_fn or doctor.doctor_config,
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    from cli._server_target import resolve_instance

    instance = (resolve or resolve_instance)(
        host=args.host, port=args.port, data_dir=args.data_dir, url=_server_url(args)
    )
    if args.area == "tui":
        from cli._dispatch_tui import dispatch_tui_command

        return dispatch_tui_command(args, instance)

    if args.area == "agent":
        from cli import agent_management as agents
        from cli._dispatch_agents import dispatch_agent_command

        result = dispatch_agent_command(
            args,
            instance,
            list_agents=list_agents or agents.agent_list,
            show_agent=show_agent or agents.agent_show,
            create_agent=create_agent or agents.agent_create,
            update_agent=update_agent or agents.agent_update,
            rename_agent=rename_agent or agents.agent_rename,
            delete_agent=delete_agent or agents.agent_delete,
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "project":
        from cli._dispatch_agents import dispatch_project_command

        result = dispatch_project_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "session":
        from cli._dispatch_agents import dispatch_session_command

        result = dispatch_session_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "archive":
        from cli._dispatch_agents import dispatch_archive_command

        result = dispatch_archive_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "chat":
        from cli._dispatch_agents import dispatch_chat_command

        result = dispatch_chat_command(args, instance)
        print_chat_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "data-store":
        from cli._dispatch_agents import dispatch_data_store_command

        result = dispatch_data_store_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "agent-secrets":
        from cli._dispatch_agents import dispatch_agent_secrets_command

        result = dispatch_agent_secrets_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "channel":
        from cli import channel_management as channels
        from cli._dispatch_connections import dispatch_channel_command

        result = dispatch_channel_command(
            args,
            instance,
            add_channel=add_channel or channels.channel_add,
            list_channels=list_channels or channels.channel_list,
            remove_channel=remove_channel or channels.channel_remove,
            update_channel=update_channel or channels.channel_update,
            enable_channel=enable_channel or channels.channel_enable,
            disable_channel=disable_channel or channels.channel_disable,
            channel_status_fn=channel_status_fn or channels.channel_status,
            set_channel_token=set_channel_token or channels.channel_set_token,
            channel_identity_fn=channel_identity_fn or channels.channel_identity,
            channel_access_fn=channel_access_fn or channels.channel_access,
            grant_channel_admin_fn=grant_channel_admin_fn or channels.channel_grant_admin,
            revoke_channel_admin_fn=revoke_channel_admin_fn or channels.channel_revoke_admin,
        )
        print_channel_command_result(args.command, result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "tool":
        from cli._dispatch_operations import dispatch_tool_command
        from cli.tool_management import tool_list

        result = dispatch_tool_command(args, instance, list_tools_fn=list_tools_fn or tool_list)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "prompt":
        from cli import prompt_management as prompts
        from cli._dispatch_operations import dispatch_prompt_command

        result = dispatch_prompt_command(
            args,
            instance,
            list_prompts_fn=list_prompts_fn or prompts.prompt_list,
            update_prompt_fn=update_prompt_fn or prompts.prompt_update,
            reset_prompt_fn=reset_prompt_fn or prompts.prompt_reset,
            preview_prompt_fn=preview_prompt_fn or prompts.prompt_preview,
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "log":
        from cli import log_management as logs
        from cli._dispatch_operations import dispatch_log_command

        result = dispatch_log_command(
            args,
            instance,
            list_logs_fn=list_logs_fn or logs.log_list,
            read_log_fn=read_log_fn or logs.log_read,
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "provider":
        from cli import provider_management as providers
        from cli._dispatch_connections import dispatch_provider_command

        result = dispatch_provider_command(
            args,
            instance,
            list_providers=list_providers or providers.provider_list,
            provider_status_fn=provider_status_fn or providers.provider_status,
            provider_usage_fn=provider_usage_fn or providers.provider_usage,
            set_provider_key=set_provider_key or providers.provider_set_key,
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "model":
        from cli import model_management as models
        from cli._dispatch_connections import dispatch_model_command

        result = dispatch_model_command(
            args,
            instance,
            list_models_fn=list_models_fn or models.model_list,
            show_model_fn=show_model_fn or models.model_show,
            refresh_models_fn=refresh_models_fn or models.model_refresh,
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "task-model":
        from cli._dispatch_connections import dispatch_task_model_command

        result = dispatch_task_model_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "skill":
        from cli._dispatch_operations import dispatch_skill_command
        from cli.skill_management import list_skills

        result = dispatch_skill_command(
            args, instance, list_skills_fn=list_skills_fn or list_skills
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "memory":
        from cli._dispatch_operations import dispatch_memory_command

        result = dispatch_memory_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "librarian":
        from cli._dispatch_operations import dispatch_librarian_command

        result = dispatch_librarian_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "extensions":
        from cli import extensions_management as extensions
        from cli._dispatch_connections import dispatch_extensions_command

        result = dispatch_extensions_command(
            args,
            instance,
            list_extensions_fn=list_extensions_fn or extensions.extensions_list,
            reload_extensions_fn=reload_extensions_fn or extensions.extensions_reload,
            enable_extension_fn=enable_extension_fn or extensions.extensions_enable,
            disable_extension_fn=disable_extension_fn or extensions.extensions_disable,
            show_extension_fn=show_extension_fn or extensions.extensions_show,
            set_extension_fn=set_extension_fn or extensions.extensions_set,
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "cron":
        from cli._dispatch_operations import dispatch_cron_command

        result = dispatch_cron_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE
    if args.area == "bootstrap":
        from cli._dispatch_operations import dispatch_bootstrap_command

        result = dispatch_bootstrap_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE
    if args.area == "devices":
        from cli._dispatch_operations import dispatch_devices_command

        result = dispatch_devices_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "statistics":
        from cli._dispatch_operations import (
            _statistics_report_adapter,
            dispatch_statistics_command,
        )

        result = dispatch_statistics_command(
            args, instance, statistics_report_fn=statistics_report_fn or _statistics_report_adapter
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "config":
        from cli import config_management as configs
        from cli._dispatch_operations import dispatch_config_command

        result = dispatch_config_command(
            args,
            instance,
            raw_config_fn=raw_config_fn or configs.config_raw,
            list_config_fn=list_config_fn or configs.config_list,
            describe_config_fn=describe_config_fn or configs.config_describe,
            effective_config_fn=effective_config_fn or configs.config_effective,
            get_config_fn=get_config_fn or configs.config_get,
            set_config_fn=set_config_fn or configs.config_set,
            unset_config_fn=unset_config_fn or configs.config_unset,
            patch_config_fn=patch_config_fn or configs.config_patch,
        )
        print_config_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "debug":
        from cli._dispatch_operations import dispatch_debug_command

        result = dispatch_debug_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "performance":
        from cli._dispatch_operations import dispatch_performance_command

        result = dispatch_performance_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    raise ValueError(f"Unsupported command area: {args.area}")


def _server_url(args: argparse.Namespace) -> str | None:
    """The normalized server address the command targets, after its ``Note:`` lines.

    ``--url`` (or ``devices pair <url>``) wins, then ``VBOT_SERVER_URL`` unless
    ``--host``/``--port`` or an Agent's Run select the target
    (``selected_server_url``). Raises ``ValueError`` for an unreadable
    ``VBOT_SERVER_URL``; argparse already checked ``--url``.
    """

    from cli._server_target import (
        SERVER_URL_ENVIRONMENT_VARIABLE,
        parse_server_url,
        selected_server_url,
    )

    selected = selected_server_url(args)
    if selected is None:
        return None
    try:
        address = parse_server_url(selected)
    except ValueError as exc:
        raise ValueError(f"{SERVER_URL_ENVIRONMENT_VARIABLE}: {exc}") from None
    for note in address.notes:
        print(f"Note: {note}", file=sys.stderr)
    return address.origin
