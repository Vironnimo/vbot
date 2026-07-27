"""Built-in process tool for managing background process sessions."""

from __future__ import annotations

from datetime import datetime

from core.tools.arguments import optional_bool, optional_int, required_string
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
    extract_tool_operation,
    operation_envelope_schema,
    tool_failure,
    tool_success,
)

PROCESS_TOOL_NAME = "process"
PROCESS_TOOL_DESCRIPTION = (
    "Manage background process sessions started by shell-backed tools. Use it for "
    "immediate progress or control; a terminal poll or successful kill suppresses a "
    "pending automatic completion. Supports listing, polling, reading logs, writing "
    "stdin, submitting a line, killing, and clearing finished sessions. Set request.operation "
    "to list, poll, log, write, submit, kill, or clear."
)
PROCESS_ACTIONS = {"list", "poll", "log", "write", "submit", "kill", "clear"}
_PROCESS_ACTION_ARGUMENTS = {
    "list": frozenset(),
    "poll": frozenset({"session_id", "timeout_ms"}),
    "log": frozenset({"session_id", "offset", "limit"}),
    "write": frozenset({"session_id", "data", "eof"}),
    "submit": frozenset({"session_id"}),
    "kill": frozenset({"session_id"}),
    "clear": frozenset({"session_id"}),
}
MAX_POLL_TIMEOUT_MS = 30_000
DEFAULT_LOG_LIMIT = 200

_PROCESS_SESSION_ID_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": "Process session id returned by bash.",
}


def _process_operation(
    description: str,
    properties: JsonObject,
    *,
    required: tuple[str, ...] = (),
) -> JsonObject:
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


PROCESS_TOOL_PARAMETERS: JsonObject = operation_envelope_schema(
    {
        "list": _process_operation("List all owned process sessions.", {}),
        "poll": _process_operation(
            "Wait briefly for new output or return the current process status.",
            {
                "session_id": _PROCESS_SESSION_ID_PARAMETER,
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_POLL_TIMEOUT_MS,
                    "description": "Wait timeout in milliseconds; default 0, maximum 30000.",
                },
            },
            required=("session_id",),
        ),
        "log": _process_operation(
            "Read a window of process output lines.",
            {
                "session_id": _PROCESS_SESSION_ID_PARAMETER,
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Zero-based log line offset; default 0.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Maximum log lines to return; default 200.",
                },
            },
            required=("session_id",),
        ),
        "write": _process_operation(
            "Write UTF-8 text to process stdin without appending a newline.",
            {
                "session_id": _PROCESS_SESSION_ID_PARAMETER,
                "data": {
                    "type": "string",
                    "description": "Text to write. May be empty when only closing stdin.",
                },
                "eof": {
                    "type": "boolean",
                    "description": "Close stdin after writing data.",
                },
            },
            required=("session_id", "data"),
        ),
        "submit": _process_operation(
            "Submit the current stdin line by sending the platform line ending.",
            {"session_id": _PROCESS_SESSION_ID_PARAMETER},
            required=("session_id",),
        ),
        "kill": _process_operation(
            "Terminate a running process.",
            {"session_id": _PROCESS_SESSION_ID_PARAMETER},
            required=("session_id",),
        ),
        "clear": _process_operation(
            "Remove a finished process session.",
            {"session_id": _PROCESS_SESSION_ID_PARAMETER},
            required=("session_id",),
        ),
    },
    description=(
        "Set request.operation to list, poll, log, write, submit, kill, or clear and include "
        "that operation's arguments in the same request object."
    ),
)


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
    try:
        action, operation_arguments = extract_tool_operation(
            arguments,
            sorted(PROCESS_ACTIONS),
        )
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))

    unknown_arguments = set(operation_arguments) - _PROCESS_ACTION_ARGUMENTS[action]
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return tool_failure(
            "invalid_arguments",
            f"Unknown {action} argument(s): {names}",
        )

    try:
        if action == "list":
            return _handle_list(process_manager, context)
        if action == "poll":
            return await _handle_poll(process_manager, context, operation_arguments)
        if action == "log":
            return await _handle_log(process_manager, context, operation_arguments)
        if action == "write":
            return await _handle_write(process_manager, context, operation_arguments)
        if action == "submit":
            return await _handle_submit(process_manager, context, operation_arguments)
        if action == "kill":
            return await _handle_kill(process_manager, context, operation_arguments)
        return await _handle_clear(process_manager, context, operation_arguments)
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
            "log_file": str(session.log_file) if session.log_file is not None else None,
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
    timeout_ms = optional_int(
        arguments.get("timeout_ms"),
        field_name="timeout_ms",
        default=0,
        minimum=0,
    )
    if timeout_ms > MAX_POLL_TIMEOUT_MS:
        timeout_ms = MAX_POLL_TIMEOUT_MS

    result = await process_manager.poll(session_id, context.agent_id, timeout_ms=timeout_ms)
    if result["status"] != "running":
        _acknowledge_completion_after_persistence(process_manager, context, session_id)
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
    offset = optional_int(arguments.get("offset"), field_name="offset", default=0, minimum=0)
    limit = optional_int(
        arguments.get("limit"),
        field_name="limit",
        default=DEFAULT_LOG_LIMIT,
        minimum=0,
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
    eof = optional_bool(arguments.get("eof"), field_name="eof", default=False)

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
    _acknowledge_completion_after_persistence(process_manager, context, session_id)
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
    return required_string(arguments.get("session_id"), field_name="session_id")


def _acknowledge_completion_after_persistence(
    process_manager: ProcessManager,
    context: ToolContext,
    session_id: str,
) -> None:
    """Suppress automatic delivery only after this manual result is durable."""
    context.after_result_persisted(
        lambda: process_manager.acknowledge_completion(session_id, context.agent_id)
    )


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
        result_schema={"type": "object"},
        display=ToolDisplay(summary_builder=_process_display_summary),
    )


def _process_display_summary(arguments: JsonObject) -> str:
    try:
        action, operation_arguments = extract_tool_operation(arguments, sorted(PROCESS_ACTIONS))
    except ValueError:
        return ""
    session_id = operation_arguments.get("session_id")
    return f"{action} · {session_id}" if isinstance(session_id, str) and session_id else action


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
