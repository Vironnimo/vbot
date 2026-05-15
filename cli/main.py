"""Command-line entrypoint for local vBot server management."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from cli.channel_management import (
    channel_add,
    channel_disable,
    channel_enable,
    channel_list,
    channel_remove,
    channel_status,
)
from cli.server_management import (
    CommandResult,
    ServerInstance,
    get_status,
    resolve_instance,
    start_server,
    stop_server,
)
from server.main import DEFAULT_HOST

SERVER_COMMANDS = ("start", "stop", "restart", "status")
CHANNEL_COMMANDS = ("add", "list", "remove", "enable", "disable", "status")
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

    return parser.parse_args(argv)


def _add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir")


def run(
    argv: Sequence[str] | None = None,
    *,
    resolve: Callable[..., ServerInstance] = resolve_instance,
    start: Callable[[ServerInstance], CommandResult] = start_server,
    stop: Callable[[ServerInstance], CommandResult] = stop_server,
    status: Callable[[ServerInstance], CommandResult] = get_status,
    add_channel: Callable[
        [ServerInstance, str, str, str, str, str, Sequence[int]], CommandResult
    ] = channel_add,
    list_channels: Callable[[ServerInstance], CommandResult] = channel_list,
    remove_channel: Callable[[ServerInstance, str], CommandResult] = channel_remove,
    enable_channel: Callable[[ServerInstance, str], CommandResult] = channel_enable,
    disable_channel: Callable[[ServerInstance, str], CommandResult] = channel_disable,
    channel_status_fn: Callable[[ServerInstance, str], CommandResult] = channel_status,
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

    raise ValueError(f"Unsupported command area: {args.area}")


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

    lines = [
        f"command: server {command}",
        f"result: {result.message}",
        f"running: {_running_text(result)}",
        f"url: {result.instance.url}",
        f"webui: {_webui_text(result)}",
        f"data_dir: {result.instance.data_dir}",
    ]
    log_path = result.log_path or result.instance.log_path
    if _should_print_log_path(command, result, log_path):
        lines.append(f"log_path: {log_path}")
    if result.process_id is not None:
        lines.append(f"process_id: {result.process_id}")
    if result.forced:
        lines.append("forced: true")
    if _is_non_vbot_conflict(result):
        lines.append("conflict: port occupied by non-vBot process")

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


def _should_print_log_path(command: str, result: CommandResult, log_path: Path) -> bool:
    return command in {"start", "restart", "status"} or result.log_path == log_path


def _is_non_vbot_conflict(result: CommandResult) -> bool:
    return result.message == "port occupied by non-vBot process"


def main(argv: Sequence[str] | None = None) -> None:
    """Process entrypoint."""

    sys.exit(run(argv))


if __name__ == "__main__":
    main()
