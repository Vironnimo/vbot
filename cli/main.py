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
    agent_show,
    agent_update,
)
from cli.channel_management import (
    channel_add,
    channel_disable,
    channel_enable,
    channel_list,
    channel_remove,
    channel_status,
    channel_update,
)
from cli.config_management import coerce_config_value, config_get, config_set, config_show
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
from cli.log_management import log_list, log_read
from cli.model_management import model_list, model_refresh
from cli.parser import parse_args
from cli.prompt_management import prompt_list, prompt_preview, prompt_reset, prompt_update
from cli.provider_management import (
    provider_connect,
    provider_connect_status,
    provider_disconnect,
    provider_list,
    provider_set_key,
    provider_status,
)
from cli.server_management import (
    CommandResult,
    ServerInstance,
    get_status,
    resolve_instance,
    start_server,
    stop_server,
)
from cli.session_management import session_create, session_link_channel, session_list
from cli.skill_management import skill_list
from cli.task_model_management import (
    task_model_clear,
    task_model_list,
    task_model_options,
    task_model_set,
    task_model_targets,
)
from cli.tool_management import tool_list

SUCCESS_EXIT_CODE = 0
FAILURE_EXIT_CODE = 1


@dataclass(frozen=True)
class ServerCommandContext:
    """Parsed server command target and service dispatch functions."""

    command: str
    host: str
    port: int | None
    data_dir: str | None
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
    delete_agent: Callable[[ServerInstance, str], CommandResult] = agent_delete,
    add_channel: Callable[
        [ServerInstance, str, str, str, str, str, Sequence[int]], CommandResult
    ] = channel_add,
    list_channels: Callable[[ServerInstance], CommandResult] = channel_list,
    remove_channel: Callable[[ServerInstance, str], CommandResult] = channel_remove,
    update_channel: Callable[[ServerInstance, str, dict[str, Any]], CommandResult] = channel_update,
    enable_channel: Callable[[ServerInstance, str], CommandResult] = channel_enable,
    disable_channel: Callable[[ServerInstance, str], CommandResult] = channel_disable,
    channel_status_fn: Callable[[ServerInstance, str], CommandResult] = channel_status,
    list_tools_fn: Callable[[ServerInstance], CommandResult] = tool_list,
    list_prompts_fn: Callable[[ServerInstance], CommandResult] = prompt_list,
    update_prompt_fn: Callable[[ServerInstance, str, str], CommandResult] = prompt_update,
    reset_prompt_fn: Callable[[ServerInstance, str], CommandResult] = prompt_reset,
    preview_prompt_fn: Callable[[ServerInstance, str], CommandResult] = prompt_preview,
    list_logs_fn: Callable[[ServerInstance], CommandResult] = log_list,
    read_log_fn: Callable[[ServerInstance, str], CommandResult] = log_read,
    list_providers: Callable[[ServerInstance], CommandResult] = provider_list,
    provider_status_fn: Callable[
        [ServerInstance, str, str | None], CommandResult
    ] = provider_status,
    set_provider_key: Callable[
        [ServerInstance, str, str, str | None, bool], CommandResult
    ] = provider_set_key,
    list_models_fn: Callable[[ServerInstance], CommandResult] = model_list,
    refresh_models_fn: Callable[[ServerInstance, str | None], CommandResult] = model_refresh,
    list_skills_fn: Callable[[ServerInstance], CommandResult] = skill_list,
    show_config_fn: Callable[[ServerInstance], CommandResult] = config_show,
    get_config_fn: Callable[[ServerInstance, str], CommandResult] = config_get,
    set_config_fn: Callable[[ServerInstance, str, Any], CommandResult] = config_set,
    doctor_settings_fn: Callable[[str | Path | None], CommandResult] = doctor_settings,
    doctor_config_fn: Callable[[str | Path | None], CommandResult] = doctor_config,
) -> int:
    """Run the CLI and return an automation-safe process exit code."""

    args = parse_args(argv)
    if args.area == "server":
        context = ServerCommandContext(
            command=args.command,
            host=args.host,
            port=args.port,
            data_dir=args.data_dir,
            resolve=resolve,
            start=start,
            stop=stop,
            status=status,
        )
        result = dispatch_server_command(context)
        print_command_result(context.command, result)
        return exit_code_for(context.command, result)

    if args.area == "doctor":
        result = dispatch_doctor_command(
            args,
            doctor_settings_fn=doctor_settings_fn,
            doctor_config_fn=doctor_config_fn,
        )
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
            delete_agent=delete_agent,
        )
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

    if args.area == "cron":
        result = dispatch_cron_command(args, instance)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "config":
        result = dispatch_config_command(
            args,
            instance,
            show_config_fn=show_config_fn,
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
    if args.command == "delete":
        return delete_agent(instance, args.id)
    raise ValueError(f"Unsupported agent command: {args.command}")


def _agent_changes_from_args(args: argparse.Namespace) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if args.command == "update" and getattr(args, "name", None) is not None:
        changes["name"] = args.name
    if args.model is not None:
        changes["model"] = args.model
    if args.fallback_model is not None:
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
    if getattr(args, "current_session_id", None) is not None:
        changes["current_session_id"] = args.current_session_id
    return changes


def dispatch_session_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_sessions_fn: Callable[[ServerInstance, str], CommandResult] = session_list,
    create_session_fn: Callable[
        [ServerInstance, str, str | None, bool], CommandResult
    ] = session_create,
    link_session_fn: Callable[
        [ServerInstance, str, str, str, str], CommandResult
    ] = session_link_channel,
) -> CommandResult:
    """Dispatch one parsed session command against the server RPC client."""

    if args.command == "list":
        return list_sessions_fn(instance, args.agent)
    if args.command == "create":
        return create_session_fn(instance, args.agent, args.id, args.make_current)
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
    list_prompts_fn: Callable[[ServerInstance], CommandResult],
    update_prompt_fn: Callable[[ServerInstance, str, str], CommandResult],
    reset_prompt_fn: Callable[[ServerInstance, str], CommandResult],
    preview_prompt_fn: Callable[[ServerInstance, str], CommandResult],
) -> CommandResult:
    """Dispatch one parsed prompt command against the server RPC client."""

    if args.command == "list":
        return list_prompts_fn(instance)
    if args.command == "update":
        try:
            content = _prompt_content_from_args(args)
        except (OSError, ValueError) as exc:
            return CommandResult(
                ok=False,
                message=f"cannot read prompt content file: {exc}",
                instance=instance,
            )
        return update_prompt_fn(instance, args.name, content)
    if args.command == "reset":
        return reset_prompt_fn(instance, args.name)
    if args.command == "preview":
        return preview_prompt_fn(instance, args.agent)
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


def dispatch_channel_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    add_channel: Callable[[ServerInstance, str, str, str, str, str, Sequence[int]], CommandResult],
    list_channels: Callable[[ServerInstance], CommandResult],
    remove_channel: Callable[[ServerInstance, str], CommandResult],
    update_channel: Callable[[ServerInstance, str, dict[str, Any]], CommandResult],
    enable_channel: Callable[[ServerInstance, str], CommandResult],
    disable_channel: Callable[[ServerInstance, str], CommandResult],
    channel_status_fn: Callable[[ServerInstance, str], CommandResult],
) -> CommandResult:
    """Dispatch one parsed channel command against the server RPC client."""

    if args.command == "add":
        return add_channel(
            instance,
            args.id,
            args.platform,
            args.agent,
            args.token_env,
            args.dm_scope,
            args.allow,
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
    return changes


def dispatch_provider_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_providers: Callable[[ServerInstance], CommandResult],
    provider_status_fn: Callable[[ServerInstance, str, str | None], CommandResult],
    set_provider_key: Callable[[ServerInstance, str, str, str | None, bool], CommandResult],
    connect_provider_fn: Callable[[ServerInstance, str, str], CommandResult] = provider_connect,
    disconnect_provider_fn: Callable[
        [ServerInstance, str, str], CommandResult
    ] = provider_disconnect,
    connect_status_fn: Callable[
        [ServerInstance, str, str], CommandResult
    ] = provider_connect_status,
) -> CommandResult:
    """Dispatch one parsed provider command against the server RPC client."""

    if args.command == "list":
        return list_providers(instance)
    if args.command == "status":
        return provider_status_fn(instance, args.provider, args.connection)
    if args.command == "set-key":
        return set_provider_key(
            instance,
            args.provider,
            args.value,
            args.connection,
            args.refresh_models,
        )
    if args.command == "connect":
        return connect_provider_fn(instance, args.provider, args.connection)
    if args.command == "disconnect":
        return disconnect_provider_fn(instance, args.provider, args.connection)
    if args.command == "connect-status":
        return connect_status_fn(instance, args.provider, args.connection)
    raise ValueError(f"Unsupported provider command: {args.command}")


def dispatch_model_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_models_fn: Callable[[ServerInstance], CommandResult],
    refresh_models_fn: Callable[[ServerInstance, str | None], CommandResult],
) -> CommandResult:
    """Dispatch one parsed model command against the server RPC client."""

    if args.command == "list":
        return list_models_fn(instance)
    if args.command == "refresh":
        return refresh_models_fn(instance, args.provider)
    raise ValueError(f"Unsupported model command: {args.command}")


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
) -> CommandResult:
    """Dispatch one parsed skill command against the server RPC client."""

    if args.command == "list":
        return list_skills_fn(instance)
    raise ValueError(f"Unsupported skill command: {args.command}")


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
    get_config_fn: Callable[[ServerInstance, str], CommandResult],
    set_config_fn: Callable[[ServerInstance, str, Any], CommandResult],
) -> CommandResult:
    """Dispatch one parsed config command against the server RPC client."""

    if args.command is None:
        return show_config_fn(instance)
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


def dispatch_server_command(context: ServerCommandContext) -> CommandResult:
    """Resolve the target and dispatch the requested server command."""

    instance = context.resolve(host=context.host, port=context.port, data_dir=context.data_dir)
    if context.command == "start":
        return context.start(instance)
    if context.command == "stop":
        return context.stop(instance)
    if context.command == "restart":
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

    sys.exit(run(argv))


if __name__ == "__main__":
    main()
