"""Built-in process tool for managing background process sessions."""

from __future__ import annotations

from datetime import datetime

from core.tools.process_manager import (
    ProcessManager,
    SessionInputClosedError,
    SessionNotFoundError,
    SessionStillRunningError,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)

PROCESS_TOOL_NAME = "process"
PROCESS_TOOL_DESCRIPTION = (
    "Manage background process sessions started by shell-backed tools. Supports "
    "listing, polling, reading logs, writing stdin, submitting a line, killing, "
    "and clearing finished sessions."
)
PROCESS_ACTIONS = {"list", "poll", "log", "write", "submit", "kill", "clear"}
PROCESS_ALLOWED_ARGUMENTS = {
    "action",
    "session_id",
    "timeout_ms",
    "offset",
    "limit",
    "data",
    "eof",
}
MAX_POLL_TIMEOUT_MS = 30_000
DEFAULT_LOG_LIMIT = 200

PROCESS_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": sorted(PROCESS_ACTIONS),
            "description": "Action to perform on process sessions.",
        },
        "session_id": {
            "type": "string",
            "description": "Process session id for actions that target one session.",
        },
        "timeout_ms": {
            "type": "number",
            "description": "Poll wait timeout in milliseconds, capped at 30000.",
        },
        "offset": {
            "type": "number",
            "description": "Zero-based log line offset for the log action.",
        },
        "limit": {
            "type": "number",
            "description": "Maximum log lines to return for the log action. Defaults to 200.",
        },
        "data": {
            "type": "string",
            "description": "UTF-8 text to write to process stdin.",
        },
        "eof": {
            "type": "boolean",
            "description": "Close stdin after writing data.",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}


def make_process_handler(process_manager: ProcessManager):
    """Create a process tool handler bound to a ProcessManager instance."""

    async def process_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await _handle_process_tool(process_manager, context, arguments)

    return process_handler


async def _handle_process_tool(
    process_manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    unknown_arguments = set(arguments) - PROCESS_ALLOWED_ARGUMENTS
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

    action = arguments.get("action")
    if not isinstance(action, str) or action not in PROCESS_ACTIONS:
        return tool_failure(
            "invalid_arguments",
            "action must be one of: clear, kill, list, log, poll, submit, write",
        )

    try:
        if action == "list":
            return _handle_list(process_manager, context)
        if action == "poll":
            return await _handle_poll(process_manager, context, arguments)
        if action == "log":
            return await _handle_log(process_manager, context, arguments)
        if action == "write":
            return await _handle_write(process_manager, context, arguments)
        if action == "submit":
            return await _handle_submit(process_manager, context, arguments)
        if action == "kill":
            return await _handle_kill(process_manager, context, arguments)
        return await _handle_clear(process_manager, context, arguments)
    except SessionNotFoundError:
        return tool_failure("session_not_found", "Process session not found")
    except SessionStillRunningError:
        return tool_failure("session_still_running", "Process session is still running")
    except SessionInputClosedError as error:
        return tool_failure("session_input_closed", str(error))
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))


def _handle_list(process_manager: ProcessManager, context: ToolContext) -> JsonObject:
    sessions = [
        {
            "session_id": session.session_id,
            "status": session.status,
            "exit_code": session.exit_code,
            "started_at": _format_timestamp(session.started_at),
            "finished_at": _format_timestamp(session.finished_at),
        }
        for session in process_manager.list_sessions(context.agent_id)
    ]
    return tool_success({"sessions": sessions})


async def _handle_poll(
    process_manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    session_id = _required_session_id(arguments)
    timeout_ms = _coerce_non_negative_int(
        arguments.get("timeout_ms"),
        field_name="timeout_ms",
        default=0,
    )
    if timeout_ms > MAX_POLL_TIMEOUT_MS:
        timeout_ms = MAX_POLL_TIMEOUT_MS

    result = await process_manager.poll(session_id, context.agent_id, timeout_ms=timeout_ms)
    return tool_success(
        {
            "session_id": result["session_id"],
            "status": result["status"],
            "output": result["output"],
            "waiting_for_input": result["waiting_for_input"],
        }
    )


async def _handle_log(
    process_manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    session_id = _required_session_id(arguments)
    offset = _coerce_non_negative_int(arguments.get("offset"), field_name="offset", default=0)
    limit = _coerce_non_negative_int(
        arguments.get("limit"),
        field_name="limit",
        default=DEFAULT_LOG_LIMIT,
    )
    result = await process_manager.log(session_id, context.agent_id, offset=offset, limit=limit)
    return tool_success(
        {
            "session_id": result["session_id"],
            "output": result["output"],
            "total_lines": result["total_lines"],
        }
    )


async def _handle_write(
    process_manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    session_id = _required_session_id(arguments)
    data = arguments.get("data")
    if not isinstance(data, str):
        raise ValueError("data must be a string")
    eof = _coerce_bool(arguments.get("eof"), field_name="eof", default=False)

    await process_manager.write(session_id, context.agent_id, data, eof=eof)
    return tool_success({"session_id": session_id, "written": len(data)})


async def _handle_submit(
    process_manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    session_id = _required_session_id(arguments)
    await process_manager.submit(session_id, context.agent_id)
    return tool_success({"session_id": session_id})


async def _handle_kill(
    process_manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    session_id = _required_session_id(arguments)
    await process_manager.kill(session_id, context.agent_id)
    return tool_success({"session_id": session_id})


async def _handle_clear(
    process_manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    session_id = _required_session_id(arguments)
    await process_manager.clear(session_id, context.agent_id)
    return tool_success({"session_id": session_id})


def _required_session_id(arguments: JsonObject) -> str:
    session_id = arguments.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
    return session_id


def _coerce_non_negative_int(value: object, *, field_name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer >= 0")
    if isinstance(value, int):
        coerced = value
    elif isinstance(value, float) and value.is_integer():
        coerced = int(value)
    else:
        raise ValueError(f"{field_name} must be an integer >= 0")
    if coerced < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return coerced


def _coerce_bool(value: object, *, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def register_process_tool(registry: ToolRegistry, process_manager: ProcessManager) -> None:
    """Register the process tool with a vBot tool registry."""
    registry.register(
        PROCESS_TOOL_NAME,
        PROCESS_TOOL_DESCRIPTION,
        PROCESS_TOOL_PARAMETERS,
        make_process_handler(process_manager),
        display=ToolDisplay(summary_fields=("action", "session_id")),
    )


__all__ = [
    "DEFAULT_LOG_LIMIT",
    "MAX_POLL_TIMEOUT_MS",
    "PROCESS_ACTIONS",
    "PROCESS_TOOL_DESCRIPTION",
    "PROCESS_TOOL_NAME",
    "PROCESS_TOOL_PARAMETERS",
    "make_process_handler",
    "register_process_tool",
]
