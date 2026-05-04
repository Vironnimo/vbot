"""Command-line entrypoint for local vBot server management."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

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
        command_parser.add_argument("--host", default=DEFAULT_HOST)
        command_parser.add_argument("--port", type=int)
        command_parser.add_argument("--data-dir")
    return parser.parse_args(argv)


def run(
    argv: Sequence[str] | None = None,
    *,
    resolve: Callable[..., ServerInstance] = resolve_instance,
    start: Callable[[ServerInstance], CommandResult] = start_server,
    stop: Callable[[ServerInstance], CommandResult] = stop_server,
    status: Callable[[ServerInstance], CommandResult] = get_status,
) -> int:
    """Run the CLI and return an automation-safe process exit code."""

    args = parse_args(argv)
    if args.area != "server":
        raise ValueError(f"Unsupported command area: {args.area}")

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
