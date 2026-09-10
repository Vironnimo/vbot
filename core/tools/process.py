"""Agent-facing control for background processes created by the bash Tool."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from core.tools.arguments import optional_string, required_string
from core.tools.process_manager import (
    ProcessManager,
    ProcessNotFoundError,
    ProcessTerminationError,
    TrackedProcess,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
    result_count_fact_builder,
    tool_failure,
    tool_success,
)
from core.utils.paths import model_path

PROCESS_TOOL_NAME = "process"
PROCESS_TOOL_DESCRIPTION = "Inspect or stop a `bash` command that runs in the background."
PROCESS_ACTIONS = ("status", "kill")
PROCESS_OUTPUT_CAP_CHARS = 8_000
PROCESS_OUTPUT_MAX_LINES = 100
PROCESS_LIST_FILTERS = ("running", "finished", "all")
PROCESS_LIST_DEFAULT_LIMIT = 20
PROCESS_LIST_MAX_LIMIT = 100

_PROCESS_ACTION_ARGUMENTS = {
    "status": frozenset({"action", "process_id", "filter", "limit", "before"}),
    "kill": frozenset({"action", "process_id"}),
}

PROCESS_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(PROCESS_ACTIONS),
            "description": "Operation to perform.",
        },
        "process_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Id of the background `bash` command to act on. Required for kill; "
                "omit for status to list running commands, newest first."
            ),
        },
        "filter": {
            "type": "string",
            "enum": list(PROCESS_LIST_FILTERS),
            "description": (
                "Commands to list for status without process_id. Omit for running commands; "
                "finished includes completed, failed, and killed commands; all includes both."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": PROCESS_LIST_MAX_LIMIT,
            "default": PROCESS_LIST_DEFAULT_LIMIT,
            "description": "Maximum commands per list page. Omit for 20.",
        },
        "before": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Process id marking the exclusive page boundary in newest-first order. "
                "Omit for the first page; use the returned next_call to continue."
            ),
        },
    },
    "required": ["action"],
}


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
        return await _handle_kill(process_manager, context, arguments)
    except ProcessNotFoundError:
        return tool_failure(
            "process_not_found",
            "Process not found",
            retryable=False,
        )
    except ProcessTerminationError as error:
        return tool_failure("process_kill_failed", str(error), retryable=True)
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
    process_id = optional_string(arguments.get("process_id"), field_name="process_id")
    if process_id is None:
        return _list_processes(process_manager, context, arguments)
    if {"filter", "limit", "before"} & arguments.keys():
        raise ValueError("filter, limit, and before apply only to status without process_id.")

    snapshot = await process_manager.snapshot(
        process_id, context.agent_id, project_id=context.project_id
    )
    if snapshot["status"] != "running":
        _acknowledge_completion_after_persistence(process_manager, context, process_id)
    return tool_success(_status_snapshot_data(snapshot))


def _list_processes(
    process_manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    selection = arguments.get("filter", "running")
    if not isinstance(selection, str) or selection not in PROCESS_LIST_FILTERS:
        raise ValueError("filter must be one of: running, finished, all")
    limit = arguments.get("limit", PROCESS_LIST_DEFAULT_LIMIT)
    if type(limit) is not int or not 1 <= limit <= PROCESS_LIST_MAX_LIMIT:
        raise ValueError("limit must be an integer from 1 to 100")
    before = optional_string(arguments.get("before"), field_name="before")
    boundary = None
    if before is not None:
        try:
            anchor = process_manager.get_process(
                before, context.agent_id, project_id=context.project_id
            )
        except ProcessNotFoundError:
            return tool_failure(
                "process_not_found",
                "Page boundary process is no longer available. Omit before to restart the list.",
                retryable=False,
            )
        boundary = (anchor.started_at, anchor.process_id)
    owned = process_manager.list_processes(context.agent_id, project_id=context.project_id)
    running = sum(tracked.status == "running" for tracked in owned)
    finished = len(owned) - running
    matches = sorted(
        (
            tracked
            for tracked in owned
            if (selection == "all" or (tracked.status == "running") == (selection == "running"))
            and (boundary is None or (tracked.started_at, tracked.process_id) < boundary)
        ),
        key=lambda tracked: (tracked.started_at, tracked.process_id),
        reverse=True,
    )
    page = matches[:limit]
    data: JsonObject = {
        "processes": [_process_summary(tracked) for tracked in page],
        "filter": selection,
        "counts": {"running": running, "finished": finished},
        "next_call": (
            {"action": "status", "filter": selection, "limit": limit, "before": page[-1].process_id}
            if len(matches) > limit
            else None
        ),
    }
    if selection == "running" and finished:
        data["history_call"] = {"action": "status", "filter": "finished"}
    return tool_success(data)


async def _handle_kill(
    process_manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    process_id = required_string(arguments.get("process_id"), field_name="process_id")
    await process_manager.kill(process_id, context.agent_id, project_id=context.project_id)
    snapshot = await process_manager.snapshot(
        process_id, context.agent_id, project_id=context.project_id
    )
    _acknowledge_completion_after_persistence(process_manager, context, process_id)
    return tool_success({"process_id": process_id, "status": snapshot["status"]})


def _process_summary(tracked: TrackedProcess) -> JsonObject:
    return {
        "process_id": tracked.process_id,
        "status": tracked.status,
        "exit_code": tracked.exit_code,
        "started_at": _format_timestamp(tracked.started_at),
        "finished_at": _format_timestamp(tracked.finished_at),
        "log_file": model_path(tracked.log_file) if tracked.log_file is not None else None,
    }


def _status_snapshot_data(snapshot: JsonObject) -> JsonObject:
    raw_output = snapshot.get("output")
    output = raw_output if isinstance(raw_output, str) else ""
    raw_log_file = snapshot.get("log_file")
    log_file = model_path(raw_log_file) if isinstance(raw_log_file, Path) else None
    fields = shape_process_output(
        output, truncated=bool(snapshot.get("truncated")), log_file=log_file
    )
    return {
        "process_id": snapshot["process_id"],
        "status": snapshot["status"],
        "exit_code": snapshot["exit_code"],
        "started_at": _format_timestamp(snapshot.get("started_at")),
        "finished_at": _format_timestamp(snapshot.get("finished_at")),
        "output_tail": fields["output"],
        "output_truncated": fields["truncated"],
        "log_file": log_file,
    }


def shape_process_output(
    output: str,
    *,
    truncated: bool = False,
    log_file: str | None = None,
    max_lines: int = PROCESS_OUTPUT_MAX_LINES,
    max_chars: int = PROCESS_OUTPUT_CAP_CHARS,
) -> JsonObject:
    """Bound Model-facing Bash/Process output, retaining the newest text and log pointer."""
    lines = output.splitlines(keepends=True)
    truncated = truncated or len(lines) > max_lines or len(output) > max_chars
    output = "".join(lines[-max_lines:])
    if truncated:
        marker = _truncation_marker(log_file)
        # Keep unusually long paths in the separate field without using up the tail.
        if len(marker) >= max_chars:
            marker = _truncation_marker(None)
        budget = max_chars - len(marker)
        output = marker + (output[-budget:] if budget > 0 else "")
    fields: JsonObject = {"output": output, "truncated": truncated}
    if truncated and log_file is not None:
        fields["log_file"] = log_file
    return fields


def _truncation_marker(log_file: str | None) -> str:
    if log_file is None:
        return "[earlier output truncated]\n"
    return f"[earlier output truncated — complete output in {log_file}; grep/read it]\n"


def _acknowledge_completion_after_persistence(
    process_manager: ProcessManager,
    context: ToolContext,
    process_id: str,
) -> None:
    """Suppress automatic delivery only after this manual terminal result is durable."""
    context.after_result_persisted(
        lambda: process_manager.acknowledge_completion(
            process_id, context.agent_id, project_id=context.project_id
        )
    )


def _format_timestamp(value: object) -> str | None:
    if not isinstance(value, datetime):
        return None
    return value.isoformat()


def register_process_tool(registry: ToolRegistry, process_manager: ProcessManager) -> None:
    """Register Agent-facing control for background bash processes."""
    registry.register(
        PROCESS_TOOL_NAME,
        PROCESS_TOOL_DESCRIPTION,
        PROCESS_TOOL_PARAMETERS,
        make_process_handler(process_manager),
        family="execution",
        result_schema={"type": "object"},
        display=ToolDisplay(
            parts_builder=_process_display_parts,
            fact_builder=result_count_fact_builder(
                "processes", when_arguments={"action": "status"}
            ),
        ),
        open_input_schema=True,
    )


def _process_display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    action = arguments.get("action")
    if not isinstance(action, str) or action not in PROCESS_ACTIONS:
        return ()
    parts = [ToolDisplayPart(action, truncate="never", tooltip="none")]
    process_id = arguments.get("process_id")
    if isinstance(process_id, str) and process_id:
        parts.append(ToolDisplayPart(process_id, kind="identifier", truncate="middle"))
    return tuple(parts)


__all__ = [
    "PROCESS_ACTIONS",
    "shape_process_output",
    "PROCESS_TOOL_DESCRIPTION",
    "PROCESS_TOOL_NAME",
    "PROCESS_TOOL_PARAMETERS",
    "make_process_handler",
    "register_process_tool",
]
