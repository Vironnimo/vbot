"""Deterministic CLI outcome rendering and exit-code policy."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING

from cli._progress import ProgressPrinter, Status, current_progress, status_line
from cli._recovery import format_command, recovery_guidance
from cli.formatting import output_mode
from cli.parser import parse_args
from cli.server_management import CommandResult, ServerInstance
from core.utils.errors import ConfigError

if TYPE_CHECKING:
    from cli.application.state import Installation, Operation

SUCCESS_EXIT_CODE = 0


FAILURE_EXIT_CODE = 1

_arguments: ContextVar[argparse.Namespace] = ContextVar("cli_arguments")
_last_result: ContextVar[CommandResult | None] = ContextVar("cli_last_result", default=None)


def command_arguments() -> argparse.Namespace:
    return _arguments.get()


def with_command_output[**P](function: Callable[P, int]) -> Callable[P, int]:
    """Own presentation for every command, including injected operations in tests."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> int:
        argv = args[0] if args else kwargs.get("argv")
        parsed = parse_args(argv)  # type: ignore[arg-type]
        with (
            _arguments.set(parsed),
            output_mode.set(getattr(parsed, "output", "auto")),
            _last_result.set(None),
        ):
            path = parsed._command_path
            with (
                ProgressPrinter(stream=sys.stderr) as progress,
                current_progress.set(progress if parsed.area != "update" else None),
            ):
                # chat reports its own progress between streamed answer text.
                if output_mode.get() != "plain" and parsed.area not in {"update", "chat"}:
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
            if output_mode.get() != "plain" and parsed.area not in {
                "server",
                "update",
                "doctor",
                "chat",
            }:
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


def print_chat_command_result(result: CommandResult) -> None:
    """Report a chat outcome on stderr; the answer or JSON report is already on stdout.

    A failure is always reported; the success trailer (Session, Agent, Model and how
    to continue) replaces the generic completion line and is omitted in plain output.
    """

    _last_result.set(result)
    if result.ok and output_mode.get() == "plain":
        return
    sys.stdout.flush()
    state: Status = "success" if result.ok else "error"
    print(
        status_line(state, f"chat: {_result_message(result)}", stream=sys.stderr),
        file=sys.stderr,
        flush=True,
    )


def _operation_duration(operation: Operation) -> str | None:
    """Time from the update request to its saved outcome, e.g. ``2m 05s``."""
    try:
        elapsed = datetime.fromisoformat(operation.updated_at) - datetime.fromisoformat(
            operation.created_at
        )
    except TypeError, ValueError:
        return None
    seconds = round(elapsed.total_seconds())
    if seconds < 0:
        return None
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


def print_application_update_result(
    install: Installation, operation: Operation, *, handoff: bool = False
) -> None:
    from cli.application.operations import result_summary
    from cli.application.packages import version_label
    from cli.application.state import read_json

    def installed_label(version_id: str | None) -> str:
        if version_id is None:
            return "unknown"
        manifest = install.version(version_id) / "release.json"
        if not manifest.is_file():
            return "unknown"
        return version_label(read_json(manifest, limit=32 * 1024**2))

    unchanged = (
        operation.phase == "completed"
        and operation.candidate_version
        and operation.candidate_version == operation.previous_version
    )
    failed = operation.phase in {"failed", "rolled_back", "needs_attention"}
    state: Status = "error" if failed else "success" if operation.phase == "completed" else "info"
    print()
    print(status_line(state, result_summary(install, operation)))
    if operation.phase in {"completed", "prepared"}:
        candidate = operation.candidate_version
        if candidate:
            label = "Prepared version" if operation.phase == "prepared" else "Version"
            version_after = operation.target_label or installed_label(candidate)
            version_before = operation.previous_label or installed_label(operation.previous_version)
            print(
                f"  {label}: {version_after}"
                if operation.phase == "prepared" or unchanged
                else f"  Version: {version_before} -> {version_after}"
            )
        if install.owns_server and operation.phase == "completed":
            from cli.application.processes import target

            print(f"  Server: {target(install).url}")
        if (
            operation.phase == "completed"
            and not unchanged
            and install.install_shape
            in {
                "server-desktop",
                "desktop-client",
            }
        ):
            print("  Desktop: reopen any existing window to use the updated client.")
    if failed:
        if operation.message:
            print(f"  {operation.message}")
        if operation.error:
            print(f"  Reason: {operation.error}")
    duration = _operation_duration(operation) if operation.terminal else None
    if duration is not None:
        print(f"  Duration: {duration}")
    if operation.phase != "completed":
        print(f"  Status: vbot update status {operation.id}")
    if operation.phase == "prepared":
        print(f"  Activate: vbot update activate {operation.id}")
    if handoff and not operation.terminal:
        print(
            "End this Run after the Tool result is saved. The updater will arrange a continuation "
            "in this Session before restarting the server. Do not repeat the update or create "
            "another Bootstrap."
        )


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
