"""Agent-facing control for background Process Sessions created by the bash Tool."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from core.tools.arguments import optional_bool, optional_string, required_string
from core.tools.contracts import action_schema
from core.tools.process_manager import (
    ProcessManager,
    ProcessSession,
    SessionInputClosedError,
    SessionNotFoundError,
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
    "Inspect or control your own background Process Sessions created by the `bash` Tool. "
    "Use the session_id returned when a bash call continues in the background; this Tool "
    "cannot access arbitrary operating-system processes. Bash output is only a capped "
    "snapshot; when log_file is present, it receives the complete combined stdout/stderr "
    "stream live through exit. Completion is delivered automatically, so use status only "
    "for an immediate snapshot, input to send stdin, and kill to stop a Process Session."
)
PROCESS_ACTIONS = ("status", "input", "kill")
PROCESS_STATUS_OUTPUT_CAP_CHARS = 30_000

_PROCESS_ACTION_ARGUMENTS = {
    "status": frozenset({"action", "session_id"}),
    "input": frozenset({"action", "session_id", "text", "newline", "eof"}),
    "kill": frozenset({"action", "session_id"}),
}

PROCESS_TOOL_PARAMETERS: JsonObject = action_schema(
    {
        "status": {
            "type": "object",
            "description": (
                "List tracked background Process Sessions without session_id, or inspect "
                "one immediately by session_id."
            ),
            "properties": {
                "session_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Process Session id returned by the bash Tool. Omit to list all "
                        "currently tracked Process Sessions."
                    ),
                },
            },
            "required": [],
        },
        "input": {
            "type": "object",
            "description": "Send stdin to one running Process Session.",
            "properties": {
                "session_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Process Session id returned by the bash Tool.",
                },
                "text": {
                    "type": "string",
                    "description": (
                        "UTF-8 text to send. It may be empty to send only a newline or EOF."
                    ),
                },
                "newline": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Append the platform line ending after text; default true. Set false "
                        "for raw text or when closing stdin without sending a line."
                    ),
                },
                "eof": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Close stdin after sending text and the optional newline; default false."
                    ),
                },
            },
            "required": ["session_id", "text"],
        },
        "kill": {
            "type": "object",
            "description": "Stop one Process Session.",
            "properties": {
                "session_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Process Session id returned by the bash Tool.",
                },
            },
            "required": ["session_id"],
        },
    },
    description=(
        "Use status without session_id to list tracked background Process Sessions, "
        "or provide session_id to inspect one. input and kill require session_id."
    ),
    action_description=(
        "status lists or inspects Process Sessions without waiting, input sends stdin "
        "to a running Process Session, and kill stops one."
    ),
)


def make_process_handler(process_manager: ProcessManager):
    """Create a process Tool handler bound to a ProcessManager instance."""

    async def process_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await _handle_process_tool(process_manager, context, arguments)

    return process_handler


async def _handle_process_tool(
    process_manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    action = arguments.get("action")
    if not isinstance(action, str) or action not in PROCESS_ACTIONS:
        return tool_failure(
            "invalid_arguments",
            f"action must be one of: {', '.join(PROCESS_ACTIONS)}",
            retryable=False,
        )

    unsupported_arguments = sorted(set(arguments) - _PROCESS_ACTION_ARGUMENTS[action])
    if unsupported_arguments:
        return tool_failure(
            "invalid_arguments",
            f"Action '{action}' does not accept: {', '.join(unsupported_arguments)}",
            retryable=False,
        )

    try:
        if action == "status":
            return await _handle_status(process_manager, context, arguments)
        if action == "input":
            return await _handle_input(process_manager, context, arguments)
        return await _handle_kill(process_manager, context, arguments)
    except SessionNotFoundError:
        return tool_failure(
            "session_not_found",
            "Process Session not found",
            retryable=False,
        )
    except SessionInputClosedError as error:
        return tool_failure(
            "session_input_closed",
            str(error),
            retryable=False,
        )
    except ValueError as error:
        return tool_failure(
            "invalid_arguments",
            str(error),
            retryable=False,
        )


async def _handle_status(
    process_manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    session_id = optional_string(arguments.get("session_id"), field_name="session_id")
    if session_id is None:
        return tool_success(
            {
                "sessions": [
                    _session_summary(session)
                    for session in process_manager.list_sessions(context.agent_id)
                ]
            }
        )

    snapshot = await process_manager.snapshot(session_id, context.agent_id)
    if snapshot["status"] != "running":
        _acknowledge_completion_after_persistence(process_manager, context, session_id)
    return tool_success(_status_snapshot_data(snapshot))


async def _handle_input(
    process_manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    session_id = required_string(arguments.get("session_id"), field_name="session_id")
    text = arguments.get("text")
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    newline = optional_bool(arguments.get("newline"), field_name="newline", default=True)
    eof = optional_bool(arguments.get("eof"), field_name="eof", default=False)
    if not text and not newline and not eof:
        raise ValueError("input must send text, append a newline, or close stdin with eof")

    await process_manager.send_input(
        session_id,
        context.agent_id,
        text,
        newline=newline,
        eof=eof,
    )
    return tool_success(
        {
            "session_id": session_id,
            "characters_sent": len(text),
            "newline": newline,
            "eof": eof,
        }
    )


async def _handle_kill(
    process_manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    session_id = required_string(arguments.get("session_id"), field_name="session_id")
    await process_manager.kill(session_id, context.agent_id)
    snapshot = await process_manager.snapshot(session_id, context.agent_id)
    _acknowledge_completion_after_persistence(process_manager, context, session_id)
    return tool_success({"session_id": session_id, "status": snapshot["status"]})


def _session_summary(session: ProcessSession) -> JsonObject:
    return {
        "session_id": session.session_id,
        "status": session.status,
        "exit_code": session.exit_code,
        "started_at": _format_timestamp(session.started_at),
        "finished_at": _format_timestamp(session.finished_at),
        "log_file": str(session.log_file) if session.log_file is not None else None,
    }


def _status_snapshot_data(snapshot: JsonObject) -> JsonObject:
    raw_output = snapshot.get("output")
    output = raw_output if isinstance(raw_output, str) else ""
    capped = len(output) > PROCESS_STATUS_OUTPUT_CAP_CHARS
    if capped:
        output = output[-PROCESS_STATUS_OUTPUT_CAP_CHARS:]
    truncated = bool(snapshot.get("truncated")) or capped

    raw_log_file = snapshot.get("log_file")
    log_file = str(raw_log_file) if isinstance(raw_log_file, Path) else None
    return {
        "session_id": snapshot["session_id"],
        "status": snapshot["status"],
        "exit_code": snapshot["exit_code"],
        "started_at": _format_timestamp(snapshot.get("started_at")),
        "finished_at": _format_timestamp(snapshot.get("finished_at")),
        "waiting_for_input": snapshot["waiting_for_input"],
        "output_tail": output,
        "output_truncated": truncated,
        "log_file": log_file,
    }


def _acknowledge_completion_after_persistence(
    process_manager: ProcessManager,
    context: ToolContext,
    session_id: str,
) -> None:
    """Suppress automatic delivery only after this manual terminal result is durable."""
    context.after_result_persisted(
        lambda: process_manager.acknowledge_completion(session_id, context.agent_id)
    )


def _format_timestamp(value: object) -> str | None:
    if not isinstance(value, datetime):
        return None
    return value.isoformat()


def register_process_tool(registry: ToolRegistry, process_manager: ProcessManager) -> None:
    """Register Agent-facing control for background bash Process Sessions."""
    registry.register(
        PROCESS_TOOL_NAME,
        PROCESS_TOOL_DESCRIPTION,
        PROCESS_TOOL_PARAMETERS,
        make_process_handler(process_manager),
        result_schema={"type": "object"},
        display=ToolDisplay(summary_builder=_process_display_summary),
    )


def _process_display_summary(arguments: JsonObject) -> str:
    action = arguments.get("action")
    if not isinstance(action, str) or action not in PROCESS_ACTIONS:
        return ""
    session_id = arguments.get("session_id")
    return f"{action} · {session_id}" if isinstance(session_id, str) and session_id else action


__all__ = [
    "PROCESS_ACTIONS",
    "PROCESS_STATUS_OUTPUT_CAP_CHARS",
    "PROCESS_TOOL_DESCRIPTION",
    "PROCESS_TOOL_NAME",
    "PROCESS_TOOL_PARAMETERS",
    "make_process_handler",
    "register_process_tool",
]
