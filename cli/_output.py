"""Deterministic CLI outcome rendering and exit-code policy."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import ParamSpec

from cli._progress import ProgressPrinter, Status, current_progress, status_line
from cli._recovery import format_command, recovery_guidance
from cli._update_types import UpdateResult
from cli.formatting import output_mode
from cli.parser import parse_args
from cli.server_management import CommandResult, ServerInstance
from cli.update_management import UNKNOWN_VBOT_VERSION
from core.utils.errors import ConfigError

SUCCESS_EXIT_CODE = 0


FAILURE_EXIT_CODE = 1

_P = ParamSpec("_P")
_arguments: ContextVar[argparse.Namespace] = ContextVar("cli_arguments")
_last_result: ContextVar[CommandResult | None] = ContextVar("cli_last_result", default=None)


def command_arguments() -> argparse.Namespace:
    return _arguments.get()


def with_command_output(function: Callable[_P, int]) -> Callable[_P, int]:
    """Own presentation for every command, including injected operations in tests."""

    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> int:
        argv = args[0] if args else kwargs.get("argv")
        parsed = parse_args(argv)  # type: ignore[arg-type]
        arguments_token = _arguments.set(parsed)
        mode_token = output_mode.set(getattr(parsed, "output", "auto"))
        result_token = _last_result.set(None)
        path = parsed._command_path
        try:
            with ProgressPrinter(stream=sys.stderr) as progress:
                progress_token = current_progress.set(progress if parsed.area != "update" else None)
                if output_mode.get() != "plain" and parsed.area != "update":
                    progress.track(f"Waiting for {path}")
                try:
                    code = function(*args, **kwargs)
                except (OSError, ValueError, ConfigError) as error:
                    sys.stdout.flush()
                    print(
                        status_line("error", f"{type(error).__name__}: {error}", stream=sys.stderr),
                        file=sys.stderr,
                    )
                    if isinstance(error, ConfigError):
                        command: tuple[str, ...] = ("vbot", "doctor", "settings")
                        if getattr(parsed, "data_dir", None):
                            command += ("--data-dir", parsed.data_dir)
                        print(f"Next: {format_command(command)}", file=sys.stderr)
                    code = FAILURE_EXIT_CODE
                except KeyboardInterrupt:
                    sys.stdout.flush()
                    print(
                        status_line(
                            "warning",
                            f"{path}: interrupted. Check the same target before repeating "
                            "a mutation; it may already have taken effect.",
                            stream=sys.stderr,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                    return 130
                finally:
                    current_progress.reset(progress_token)
            if output_mode.get() != "plain" and parsed.area not in {"server", "update", "doctor"}:
                result = _last_result.get()
                attention = result.attention if result else ()
                state: Status = "error" if code else "warning" if attention else "success"
                summary = (
                    "; ".join(attention)
                    if attention
                    else ("command completed" if not code else "command failed; see details")
                )
                sys.stdout.flush()
                print(
                    status_line(state, f"{path}: {summary}", stream=sys.stderr),
                    file=sys.stderr,
                    flush=True,
                )
            if code:
                result = _last_result.get()
                guidance = recovery_guidance(parsed, result)
                sys.stdout.flush()
                if result and result.failure and result.failure.request_state == "responded":
                    print(
                        f"rpc_method: {result.failure.method}\nserver: {result.instance.url}\n"
                        f"request_state: {result.failure.request_state}",
                        file=sys.stderr,
                    )
                if guidance.explanation:
                    print(guidance.explanation, file=sys.stderr)
                for command in guidance.commands:
                    print(f"Next: {format_command(command)}", file=sys.stderr)
            return code
        finally:
            _last_result.reset(result_token)
            output_mode.reset(mode_token)
            _arguments.reset(arguments_token)

    return wrapped


def print_server_command_start(command: str, instance: ServerInstance) -> None:
    """Announce a server lifecycle operation before it can block."""

    if output_mode.get() == "plain":
        return

    actions = {
        "start": "Starting",
        "stop": "Stopping",
        "restart": "Restarting",
        "status": "Checking the status of",
    }
    try:
        action = actions[command]
    except KeyError as exc:
        raise ValueError(f"Unsupported server command: {command}") from exc
    print(status_line("busy", f"{action} the vBot server at {instance.url}..."), flush=True)


def print_command_result(command: str, result: CommandResult) -> None:
    """Print deterministic plain-text server command output."""

    _last_result.set(result)
    lines = [f"command: server {command}", f"result: {_result_message(result)}"]
    if command in {"start", "restart"}:
        lines.extend(_start_like_output_lines(result))
    elif command == "stop":
        lines.extend(_stop_output_lines(result))
    elif command == "status":
        lines.extend(_status_output_lines(result))
    else:
        raise ValueError(f"Unsupported server command: {command}")

    state: Status = "success" if result.ok else "error"
    if result.ok and (
        result.forced
        or (command == "status" and _running_text(result) != "yes")
        or (command != "stop" and result.webui is not None and not result.webui.available)
    ):
        state = "warning"
    if output_mode.get() != "plain":
        lines.append(status_line(state, _server_completion_message(command, result)))
    print("\n".join(lines))


def print_channel_command_result(command: str, result: CommandResult) -> None:
    """Print deterministic plain-text channel command output."""

    _last_result.set(result)
    lines = [
        f"command: channel {command}",
        f"result: {_result_message(result)}",
        f"url: {result.instance.url}",
        f"local_data_dir: {result.instance.data_dir}",
    ]
    print("\n".join(lines))


def print_management_command_result(result: CommandResult) -> None:
    """Print plain-text output for non-channel RPC management command areas."""

    _last_result.set(result)
    print(_result_message(result))


def print_update_command_start(version: str) -> None:
    """Announce the self-update before its long-running work begins."""

    if output_mode.get() == "plain":
        return

    if version == UNKNOWN_VBOT_VERSION:
        print(
            status_line("busy", "Updating vBot. The current version could not be determined..."),
            flush=True,
        )
        return
    print(status_line("busy", f"Updating vBot from version {version}..."), flush=True)


def print_update_command_result(
    result: CommandResult,
    *,
    version_before: str,
    version_after: str,
    shown_messages: set[str] | None = None,
) -> None:
    """Print update details followed by one readable completion sentence."""

    _last_result.set(result)
    remaining = [
        line
        for line in _result_message(result).splitlines()
        if line not in (shown_messages or set())
    ]
    if remaining:
        print("\n".join(remaining))
    state: Status = "success" if result.ok else "error"
    if result.ok and (
        not isinstance(result, UpdateResult)
        or result.restart_state in {"pending", "skipped"}
        or result.forced
        or (result.webui is not None and not result.webui.available)
    ):
        state = "warning"
    print()
    print(status_line(state, _update_completion_message(result)))
    print(f"  Version: {version_before} -> {version_after}")
    if not isinstance(result, UpdateResult) or result.restart_state != "not_applicable":
        print(f"  Server: {result.instance.url}")
    if result.webui is not None:
        print(f"  WebUI: {_webui_text(result)}")
    if result.forced:
        print("  Attention: stopping the old server required forced termination.")


def print_config_command_result(result: CommandResult) -> None:
    """Print deterministic plain-text config command output."""

    print_management_command_result(result)


def _result_message(result: CommandResult) -> str:
    message = result.message.strip()
    if message:
        return result.message
    if result.ok:
        return "success: command completed without details"
    return "error: command failed without details"


def _server_completion_message(command: str, result: CommandResult) -> str:
    reason = _result_message(result).rstrip(".")
    url = result.instance.url
    if command == "status" and _is_non_vbot_conflict(result):
        return (
            f"The vBot server is not running at {url}; the port is occupied by a non-vBot process."
        )
    if not result.ok:
        actions = {
            "start": "start",
            "stop": "stop",
            "restart": "restart",
            "status": "determine the status of",
        }
        return f"Could not {actions[command]} the vBot server at {url}: {reason}."

    if command == "start":
        if result.message == "already running":
            return f"The vBot server is already running and healthy at {url}."
        return f"The vBot server started successfully and is healthy at {url}."
    if command == "stop":
        if result.message == "not running":
            return f"The vBot server is already stopped at {url}."
        if result.forced:
            return (
                f"The vBot server stopped at {url}, but required forced termination after "
                "the graceful shutdown timed out."
            )
        return f"The vBot server stopped successfully at {url}."
    if command == "restart":
        return f"The vBot server restarted successfully and is healthy at {url}."
    if command == "status":
        if _running_text(result) == "yes":
            return f"The vBot server is running and healthy at {url}."
        return f"The vBot server is not running at {url}."
    raise ValueError(f"Unsupported server command: {command}")


def _update_completion_message(result: CommandResult) -> str:
    if result.ok:
        if isinstance(result, UpdateResult):
            if result.restart_state == "pending":
                return (
                    "Update installed — server restart pending. "
                    "Availability has not yet been verified."
                )
            if result.restart_state == "skipped":
                return (
                    "Update installed — server was not restarted (--no-restart). "
                    "Restart it to use the update."
                )
            if result.restart_state == "not_applicable":
                return (
                    "Update completed — Desktop client is current; "
                    "no local server restart is needed."
                )
            if result.restart_state == "completed":
                if result.webui is not None and not result.webui.available:
                    return "Update completed — server is healthy, but the WebUI is unavailable."
                return "Update completed — server restarted and passed its health check."
        return "Update steps completed. Server readiness has not been verified."
    return (
        "Update stopped with an error. Earlier steps may already be applied; "
        "follow the recovery details above."
    )


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
