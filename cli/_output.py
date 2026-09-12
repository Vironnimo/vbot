"""Deterministic CLI outcome rendering and exit-code policy."""

from __future__ import annotations

from pathlib import Path

from cli.server_management import CommandResult, ServerInstance
from cli.update_management import UNKNOWN_VBOT_VERSION

SUCCESS_EXIT_CODE = 0


FAILURE_EXIT_CODE = 1


def print_server_command_start(command: str, instance: ServerInstance) -> None:
    """Announce a server lifecycle operation before it can block."""

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
    print(f"{action} the vBot server at {instance.url}...", flush=True)


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

    lines.append(_server_completion_message(command, result))
    print("\n".join(lines))


def print_channel_command_result(command: str, result: CommandResult) -> None:
    """Print deterministic plain-text channel command output."""

    lines = [
        f"command: channel {command}",
        f"result: {_result_message(result)}",
        f"url: {result.instance.url}",
        f"local_data_dir: {result.instance.data_dir}",
    ]
    print("\n".join(lines))


def print_management_command_result(result: CommandResult) -> None:
    """Print plain-text output for non-channel RPC management command areas."""

    print(_result_message(result))


def print_update_command_start(version: str) -> None:
    """Announce the self-update before its long-running work begins."""

    if version == UNKNOWN_VBOT_VERSION:
        print("Updating vBot. The current version could not be determined...", flush=True)
        return
    print(f"Updating vBot from version {version}...", flush=True)


def print_update_command_result(
    result: CommandResult,
    *,
    version_before: str,
    version_after: str,
) -> None:
    """Print update details followed by one readable completion sentence."""

    print(_result_message(result))
    print(_update_completion_message(result, version_before, version_after))


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


def _update_completion_message(
    result: CommandResult, version_before: str, version_after: str
) -> str:
    versions = f"checkout version: {version_before} -> {version_after}"
    if result.ok:
        return (
            f"Update steps succeeded ({versions}). The server restart state is "
            "reported above; a scheduled restart still needs a health check."
        )
    return (
        f"Update stopped with an error ({versions}). Earlier steps may already "
        "be applied; use the recovery details above."
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
