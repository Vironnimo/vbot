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
)
from cli.config_management import coerce_config_value, config_get, config_set, config_show
from cli.model_management import model_list, model_refresh
from cli.provider_management import provider_list
from cli.server_management import (
    CommandResult,
    ServerInstance,
    get_status,
    resolve_instance,
    start_server,
    stop_server,
)
from cli.skill_management import skill_list
from server.main import DEFAULT_HOST

SERVER_COMMANDS = ("start", "stop", "restart", "status")
CHANNEL_COMMANDS = ("add", "list", "remove", "enable", "disable", "status")
AGENT_COMMANDS = ("list", "show", "create", "update", "delete")
PROVIDER_COMMANDS = ("list",)
MODEL_COMMANDS = ("list", "refresh")
SKILL_COMMANDS = ("list",)
CONFIG_COMMANDS = ("get", "set")
THINKING_EFFORTS = ("", "none", "minimal", "low", "medium", "high", "xhigh", "max")
CHANNEL_PLATFORMS = ("telegram",)
CHANNEL_DM_SCOPES = (
    "per_conversation",
    "main",
    "per_peer",
    "per_account_channel_peer",
)
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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse vBot CLI arguments without prompting for input."""

    parser = argparse.ArgumentParser(description="Manage vBot from the command line")
    subparsers = parser.add_subparsers(dest="area", required=True)

    server_parser = subparsers.add_parser("server", description="Manage the local vBot server")
    server_subparsers = server_parser.add_subparsers(dest="command", required=True)
    for command in SERVER_COMMANDS:
        command_parser = server_subparsers.add_parser(command)
        _add_target_arguments(command_parser)

    agent_parser = subparsers.add_parser("agent", description="Manage vBot agents")
    agent_subparsers = agent_parser.add_subparsers(dest="command", required=True)

    agent_list_parser = agent_subparsers.add_parser("list")
    _add_target_arguments(agent_list_parser)

    agent_show_parser = agent_subparsers.add_parser("show")
    _add_target_arguments(agent_show_parser)
    agent_show_parser.add_argument("--id", required=True)

    agent_create_parser = agent_subparsers.add_parser("create")
    _add_target_arguments(agent_create_parser)
    agent_create_parser.add_argument("--id", required=True)
    agent_create_parser.add_argument("--name", required=True)
    _add_agent_change_arguments(agent_create_parser, include_name=False, include_session=False)

    agent_update_parser = agent_subparsers.add_parser("update")
    _add_target_arguments(agent_update_parser)
    agent_update_parser.add_argument("--id", required=True)
    _add_agent_change_arguments(agent_update_parser, include_name=True, include_session=True)

    agent_delete_parser = agent_subparsers.add_parser("delete")
    _add_target_arguments(agent_delete_parser)
    agent_delete_parser.add_argument("--id", required=True)

    channel_parser = subparsers.add_parser("channel", description="Manage vBot channels")
    channel_subparsers = channel_parser.add_subparsers(dest="command", required=True)

    add_parser = channel_subparsers.add_parser("add")
    _add_target_arguments(add_parser)
    add_parser.add_argument("--id", required=True)
    add_parser.add_argument("--platform", required=True, choices=CHANNEL_PLATFORMS)
    add_parser.add_argument("--agent", required=True)
    add_parser.add_argument("--token-env", required=True)
    add_parser.add_argument("--dm-scope", default="per_conversation", choices=CHANNEL_DM_SCOPES)
    add_parser.add_argument("--allow", type=int, nargs="*", default=[])

    list_parser = channel_subparsers.add_parser("list")
    _add_target_arguments(list_parser)

    remove_parser = channel_subparsers.add_parser("remove")
    _add_target_arguments(remove_parser)
    remove_parser.add_argument("--id", required=True)

    enable_parser = channel_subparsers.add_parser("enable")
    _add_target_arguments(enable_parser)
    enable_parser.add_argument("--id", required=True)

    disable_parser = channel_subparsers.add_parser("disable")
    _add_target_arguments(disable_parser)
    disable_parser.add_argument("--id", required=True)

    status_parser = channel_subparsers.add_parser("status")
    _add_target_arguments(status_parser)
    status_parser.add_argument("--id", required=True)

    provider_parser = subparsers.add_parser("provider", description="Manage vBot providers")
    provider_subparsers = provider_parser.add_subparsers(dest="command", required=True)
    for command in PROVIDER_COMMANDS:
        command_parser = provider_subparsers.add_parser(command)
        _add_target_arguments(command_parser)

    model_parser = subparsers.add_parser("model", description="Manage vBot models")
    model_subparsers = model_parser.add_subparsers(dest="command", required=True)
    model_list_parser = model_subparsers.add_parser(MODEL_COMMANDS[0])
    _add_target_arguments(model_list_parser)
    model_refresh_parser = model_subparsers.add_parser(MODEL_COMMANDS[1])
    _add_target_arguments(model_refresh_parser)
    model_refresh_parser.add_argument("--provider")

    skill_parser = subparsers.add_parser("skill", description="Manage vBot skills")
    skill_subparsers = skill_parser.add_subparsers(dest="command", required=True)
    for command in SKILL_COMMANDS:
        command_parser = skill_subparsers.add_parser(command)
        _add_target_arguments(command_parser)

    config_parser = subparsers.add_parser("config", description="Manage vBot configuration")
    config_subparsers = config_parser.add_subparsers(dest="command")
    _add_target_arguments(config_parser)

    config_get_parser = config_subparsers.add_parser(CONFIG_COMMANDS[0])
    _add_target_arguments(config_get_parser)
    config_get_parser.add_argument("key")

    config_set_parser = config_subparsers.add_parser(CONFIG_COMMANDS[1])
    _add_target_arguments(config_set_parser)
    config_set_parser.add_argument("key")
    config_set_parser.add_argument("value")

    return parser.parse_args(argv)


def _add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir")


def _add_agent_change_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_name: bool,
    include_session: bool,
) -> None:
    if include_name:
        parser.add_argument("--name")
    parser.add_argument("--model")
    parser.add_argument("--fallback-model")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--clear-temperature", action="store_true")
    parser.add_argument("--thinking-effort", choices=THINKING_EFFORTS)
    parser.add_argument("--clear-thinking-effort", action="store_true")
    parser.add_argument("--allowed-tools", nargs="*")
    parser.add_argument("--allowed-skills", nargs="*")
    if include_session:
        parser.add_argument("--current-session-id")


def run(
    argv: Sequence[str] | None = None,
    *,
    resolve: Callable[..., ServerInstance] = resolve_instance,
    start: Callable[[ServerInstance], CommandResult] = start_server,
    stop: Callable[[ServerInstance], CommandResult] = stop_server,
    status: Callable[[ServerInstance], CommandResult] = get_status,
    list_agents: Callable[[ServerInstance], CommandResult] = agent_list,
    show_agent: Callable[[ServerInstance, str], CommandResult] = agent_show,
    create_agent: Callable[[ServerInstance, str, str, dict[str, Any]], CommandResult] = agent_create,
    update_agent: Callable[[ServerInstance, str, dict[str, Any]], CommandResult] = agent_update,
    delete_agent: Callable[[ServerInstance, str], CommandResult] = agent_delete,
    add_channel: Callable[
        [ServerInstance, str, str, str, str, str, Sequence[int]], CommandResult
    ] = channel_add,
    list_channels: Callable[[ServerInstance], CommandResult] = channel_list,
    remove_channel: Callable[[ServerInstance, str], CommandResult] = channel_remove,
    enable_channel: Callable[[ServerInstance, str], CommandResult] = channel_enable,
    disable_channel: Callable[[ServerInstance, str], CommandResult] = channel_disable,
    channel_status_fn: Callable[[ServerInstance, str], CommandResult] = channel_status,
    list_providers: Callable[[ServerInstance], CommandResult] = provider_list,
    list_models_fn: Callable[[ServerInstance], CommandResult] = model_list,
    refresh_models_fn: Callable[[ServerInstance, str | None], CommandResult] = model_refresh,
    list_skills_fn: Callable[[ServerInstance], CommandResult] = skill_list,
    show_config_fn: Callable[[ServerInstance], CommandResult] = config_show,
    get_config_fn: Callable[[ServerInstance, str], CommandResult] = config_get,
    set_config_fn: Callable[[ServerInstance, str, Any], CommandResult] = config_set,
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

    if args.area == "agent":
        instance = resolve(host=args.host, port=args.port, data_dir=args.data_dir)
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

    if args.area == "channel":
        instance = resolve(host=args.host, port=args.port, data_dir=args.data_dir)
        result = dispatch_channel_command(
            args,
            instance,
            add_channel=add_channel,
            list_channels=list_channels,
            remove_channel=remove_channel,
            enable_channel=enable_channel,
            disable_channel=disable_channel,
            channel_status_fn=channel_status_fn,
        )
        print_channel_command_result(args.command, result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "provider":
        instance = resolve(host=args.host, port=args.port, data_dir=args.data_dir)
        result = dispatch_provider_command(args, instance, list_providers=list_providers)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "model":
        instance = resolve(host=args.host, port=args.port, data_dir=args.data_dir)
        result = dispatch_model_command(
            args,
            instance,
            list_models_fn=list_models_fn,
            refresh_models_fn=refresh_models_fn,
        )
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "skill":
        instance = resolve(host=args.host, port=args.port, data_dir=args.data_dir)
        result = dispatch_skill_command(args, instance, list_skills_fn=list_skills_fn)
        print_management_command_result(result)
        return SUCCESS_EXIT_CODE if result.ok else FAILURE_EXIT_CODE

    if args.area == "config":
        instance = resolve(host=args.host, port=args.port, data_dir=args.data_dir)
        result = dispatch_config_command(
            args,
            instance,
            show_config_fn=show_config_fn,
            get_config_fn=get_config_fn,
            set_config_fn=set_config_fn,
        )
        print_config_command_result(result)
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
    if getattr(args, "name", None) is not None and args.command == "update":
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
    if args.allowed_tools is not None:
        changes["allowed_tools"] = list(args.allowed_tools)
    if args.allowed_skills is not None:
        changes["allowed_skills"] = list(args.allowed_skills)
    if getattr(args, "current_session_id", None) is not None:
        changes["current_session_id"] = args.current_session_id
    return changes


def dispatch_channel_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    add_channel: Callable[[ServerInstance, str, str, str, str, str, Sequence[int]], CommandResult],
    list_channels: Callable[[ServerInstance], CommandResult],
    remove_channel: Callable[[ServerInstance, str], CommandResult],
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
    if args.command == "enable":
        return enable_channel(instance, args.id)
    if args.command == "disable":
        return disable_channel(instance, args.id)
    if args.command == "status":
        return channel_status_fn(instance, args.id)
    raise ValueError(f"Unsupported channel command: {args.command}")


def dispatch_provider_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_providers: Callable[[ServerInstance], CommandResult],
) -> CommandResult:
    """Dispatch one parsed provider command against the server RPC client."""

    if args.command == "list":
        return list_providers(instance)
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

    lines = [f"command: server {command}", f"result: {result.message}"]
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
        f"result: {result.message}",
        f"url: {result.instance.url}",
        f"data_dir: {result.instance.data_dir}",
    ]
    print("\n".join(lines))


def print_management_command_result(result: CommandResult) -> None:
    """Print plain-text output for non-channel RPC management command areas."""

    print(result.message)


def print_config_command_result(result: CommandResult) -> None:
    """Print deterministic plain-text config command output."""

    print(result.message)


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
