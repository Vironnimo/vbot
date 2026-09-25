"""Agent-facing control for background processes created by the shell Tool."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from core.tools._argument_repair import normalize_call_arguments
from core.tools._call_vocabulary import SpellingAliases, spelling
from core.tools._shell_arguments import resolve_timeout
from core.tools.arguments import optional_number, optional_string, required_string
from core.tools.contracts import compile_tool_contract
from core.tools.model_names import SHELL_MODEL_NAME
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
PROCESS_TOOL_DESCRIPTION = (
    f"Check on, wait for, or stop a `{SHELL_MODEL_NAME}` command that runs in the background."
)
PROCESS_ACTIONS = ("status", "wait", "kill")
PROCESS_OUTPUT_CAP_CHARS = 8_000
PROCESS_OUTPUT_MAX_LINES = 100
PROCESS_LIST_FILTERS = ("running", "finished", "all")
PROCESS_LIST_DEFAULT_LIMIT = 20
PROCESS_LIST_MAX_LIMIT = 100
PROCESS_WAIT_DEFAULT_SECONDS = 60
PROCESS_WAIT_MAX_SECONDS = 600
# A still-running wait shows recent context only; the log file holds the rest.
PROCESS_WAIT_OUTPUT_MAX_LINES = 20
PROCESS_WAIT_OUTPUT_CAP_CHARS = 4_000
# Commands named in listings and not-found errors.
_LISTED_COMMANDS = 3
_COMMAND_LABEL_CHARS = 60
_MATCHED_LINE_CHARS = 500

PROCESS_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(PROCESS_ACTIONS),
            "description": (
                "status: one command's state and recent output, or a list without process_id. "
                "wait: block until the command exits, prints a line matching pattern, or "
                "timeout passes. kill: stop the command."
            ),
        },
        "process_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                f"Id from the `{SHELL_MODEL_NAME}` result. Required for wait and kill; omit "
                "for status to list your commands, newest first."
            ),
        },
        "timeout": {
            "type": "number",
            "minimum": 0,
            "description": (
                f"wait: seconds to wait. Omit for {PROCESS_WAIT_DEFAULT_SECONDS}; at most "
                f"{PROCESS_WAIT_MAX_SECONDS}."
            ),
        },
        "pattern": {
            "type": "string",
            "minLength": 1,
            "description": (
                "wait: return as soon as an output line matches this regular expression "
                "(case-insensitive), such as a server's ready line; earlier output counts."
            ),
        },
        "filter": {
            "type": "string",
            "enum": list(PROCESS_LIST_FILTERS),
            "description": (
                "List for status without process_id. Omit for running commands; finished "
                "includes completed, failed, and killed ones."
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
            "description": "List page boundary; use the returned next_call to continue.",
        },
    },
    "required": ["action"],
}
PROCESS_UNADVERTISED_PARAMETERS: JsonObject = {"timeout_ms": {"type": "number", "minimum": 0}}

_LIST_FIELDS = ("filter", "limit", "before")
_WAIT_FIELDS = ("timeout", "timeout_ms", "pattern")
_FIELD_ALIASES = SpellingAliases(
    {
        "process_id": (
            "session_id",
            "process",
            "proc_id",
            "bash_id",
            "shell_id",
            "task_id",
            "id",
        ),
        "timeout": ("timeout_seconds", "timeout_secs", "timeout_sec", "timeout_s", "seconds"),
        "timeout_ms": ("timeout_millis", "timeout_milliseconds"),
        "pattern": ("until", "match", "regex", "wait_for", "expect", "expected"),
    }
)
_ACTION_VALUES = {
    **dict.fromkeys(
        (
            "status",
            "list",
            "ls",
            "poll",
            "log",
            "logs",
            "output",
            "read",
            "check",
            "show",
            "get",
            "info",
            "inspect",
        ),
        "status",
    ),
    **dict.fromkeys(("wait", "await", "block", "join", "waitfor"), "wait"),
    **dict.fromkeys(
        ("kill", "stop", "terminate", "cancel", "abort", "end", "close", "killsession"), "kill"
    ),
}
_INPUT_ACTIONS = frozenset({"write", "submit", "send", "input", "stdin", "sendkeys", "type"})
# Output-window requests from other harnesses; the result already carries a tail.
_OUTPUT_WINDOW_FIELDS = frozenset({"offset", "lines", "tail", "maxoutputtokens"})


def _action_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    word = spelling(value)
    if word in _INPUT_ACTIONS:
        raise ValueError(
            f"{PROCESS_TOOL_NAME} was not run: background commands take no input; their input "
            "is closed when they start. Run interactive programs with the terminal Tool if you "
            "have it, or give the command its input through a file or a pipeline."
        )
    return _ACTION_VALUES.get(word, value)


def _process_contract():
    schema = {
        **PROCESS_TOOL_PARAMETERS,
        "properties": {
            **PROCESS_TOOL_PARAMETERS["properties"],
            **PROCESS_UNADVERTISED_PARAMETERS,
        },
    }
    return compile_tool_contract(
        name=PROCESS_TOOL_NAME, input_schema=schema, require_closed_input=False
    )


_PROCESS_CONTRACT = _process_contract()


def normalize_process_arguments(arguments: Any) -> Any:
    """Map other harnesses' process calls onto action, process_id, and the action's fields.

    Hermes and Codex name the id session_id, Claude Code bash_id or task_id; a
    poll with a timeout waits. Placeholder values and fields another action owns
    request nothing and are dropped, so they cannot fail an otherwise exact call.
    """
    normalized = normalize_call_arguments(
        _PROCESS_CONTRACT,
        arguments,
        field_aliases=_FIELD_ALIASES,
        enum_fields=("action",),
        field_normalizers={"action": _action_value},
        empty_as_omitted=("process_id", "pattern", "filter", "before"),
    )
    if not isinstance(normalized, dict):
        return normalized
    for key in list(normalized):
        value = normalized[key]
        if spelling(key) in _OUTPUT_WINDOW_FIELDS or (
            key in {*_LIST_FIELDS, "process_id", "pattern"}
            and (value in (None, 0) or (isinstance(value, str) and not value.strip()))
        ):
            del normalized[key]
    action = normalized.get("action")
    has_id = "process_id" in normalized
    waits = "pattern" in normalized or any(
        isinstance(normalized.get(key), (int, float)) and normalized[key] > 0
        for key in ("timeout", "timeout_ms")
    )
    if action == "status" and has_id and waits:
        action = normalized["action"] = "wait"
    inapplicable: dict[object, tuple[str, ...]] = {
        "status": (*_LIST_FIELDS, *_WAIT_FIELDS) if has_id else _WAIT_FIELDS,
        "wait": _LIST_FIELDS,
        "kill": (*_LIST_FIELDS, *_WAIT_FIELDS),
    }
    for key in inapplicable.get(action, ()):
        normalized.pop(key, None)
    return normalized


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

    try:
        if action == "status":
            return await _handle_status(process_manager, context, arguments)
        if action == "wait":
            return await _handle_wait(process_manager, context, arguments)
        return await _handle_kill(process_manager, context, arguments)
    except ProcessNotFoundError:
        return tool_failure(
            "process_not_found",
            _not_found_message(process_manager, context, arguments.get("process_id")),
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

    snapshot = await process_manager.snapshot(
        process_id, context.agent_id, project_id=context.project_id
    )
    if snapshot["status"] != "running":
        _acknowledge_completion_after_persistence(process_manager, context, process_id)
    return tool_success(_status_snapshot_data(snapshot))


async def _handle_wait(
    process_manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    process_id = optional_string(arguments.get("process_id"), field_name="process_id")
    if not process_id:
        raise ValueError(
            "wait needs the process_id of the command to wait for. "
            + _owned_commands_text(process_manager, context)
        )
    timeout, timeout_note = resolve_timeout(
        optional_number(arguments.get("timeout"), field_name="timeout", minimum=0),
        optional_number(arguments.get("timeout_ms"), field_name="timeout_ms", minimum=0),
        tool_name=PROCESS_TOOL_NAME,
    )
    notes = [timeout_note] if timeout_note else []
    if timeout is None:
        timeout = PROCESS_WAIT_DEFAULT_SECONDS
    elif timeout > PROCESS_WAIT_MAX_SECONDS:
        notes.append(
            f"wait waits at most {PROCESS_WAIT_MAX_SECONDS} s per call; wait again if the "
            "command is still running."
        )
        timeout = PROCESS_WAIT_MAX_SECONDS
    pattern, pattern_note = _wait_pattern(arguments.get("pattern"))
    if pattern_note:
        notes.append(pattern_note)

    # The same id must exist before the wait registers any control.
    process_manager.get_process(process_id, context.agent_id, project_id=context.project_id)
    ended_by_user = False

    def end_wait() -> bool:
        nonlocal ended_by_user
        if context.is_cancelled() or context.was_cancelled_by_user():
            return False
        ended_by_user = True
        return True

    if context.nesting_depth == 0 and context.background_registration_hook is not None:
        context.background_registration_hook(end_wait)

    outcome, line = await process_manager.wait(
        process_id,
        context.agent_id,
        project_id=context.project_id,
        timeout_seconds=timeout,
        pattern=pattern,
        interrupted=lambda: ended_by_user or context.was_cancelled_by_user(),
    )
    snapshot = await process_manager.snapshot(
        process_id, context.agent_id, project_id=context.project_id
    )
    if snapshot["status"] != "running":
        _acknowledge_completion_after_persistence(process_manager, context, process_id)
        data = _status_snapshot_data(snapshot)
    elif context.was_cancelled_by_user() and not ended_by_user:
        return tool_failure(
            "cancelled_by_user",
            "The user cancelled this wait. The command is still running.",
            retryable=False,
        )
    else:
        data = _running_wait_data(snapshot)
        if outcome == "interrupted":
            notes.append(
                "The user ended this wait. The command is still running, and vBot delivers "
                "its result when it exits."
            )
        elif outcome == "timed_out":
            notes.append(
                f"Still running after {timeout:g} s. If your next step depends on it, wait "
                "again; otherwise continue, and vBot delivers the result when it exits."
            )
        else:
            notes.append("The command is still running; vBot delivers its result when it exits.")
    if line is not None:
        data["matched"] = _shown_line(line)
    if notes:
        data["note"] = " ".join(notes)
    return tool_success(data)


def _wait_pattern(value: object) -> tuple[re.Pattern[str] | None, str | None]:
    text = optional_string(value, field_name="pattern")
    if not text:
        return None, None
    try:
        return re.compile(text, re.IGNORECASE), None
    except re.error:
        return re.compile(re.escape(text), re.IGNORECASE), (
            "pattern is not a valid regular expression, so it was matched as plain text."
        )


def _shown_line(line: str) -> str:
    shown = _last_segment(line.rstrip("\r\n")).strip()
    if len(shown) > _MATCHED_LINE_CHARS:
        shown = shown[: _MATCHED_LINE_CHARS - 3] + "..."
    return shown


def _running_wait_data(snapshot: JsonObject) -> JsonObject:
    raw_output = snapshot.get("output")
    raw_log_file = snapshot.get("log_file")
    log_file = model_path(raw_log_file) if isinstance(raw_log_file, Path) else None
    fields = shape_process_output(
        raw_output if isinstance(raw_output, str) else "",
        truncated=bool(snapshot.get("truncated")),
        log_file=log_file,
        max_lines=PROCESS_WAIT_OUTPUT_MAX_LINES,
        max_chars=PROCESS_WAIT_OUTPUT_CAP_CHARS,
    )
    return {"process_id": snapshot["process_id"], "status": snapshot["status"], **fields}


def _not_found_message(
    process_manager: ProcessManager, context: ToolContext, process_id: object
) -> str:
    if isinstance(process_id, str) and process_id.startswith("term_"):
        return (
            f"{process_id} is a terminal, not a background command. Use the terminal Tool "
            "with this terminal_id."
        )
    shown = process_id if isinstance(process_id, str) and process_id else "that id"
    return f"No background command has the id {shown}. " + _owned_commands_text(
        process_manager, context
    )


def _owned_commands_text(process_manager: ProcessManager, context: ToolContext) -> str:
    owned = sorted(
        process_manager.list_processes(context.agent_id, project_id=context.project_id),
        key=lambda tracked: (tracked.status != "running", -tracked.started_at.timestamp()),
    )
    if not owned:
        return (
            f"You have no background commands; a `{SHELL_MODEL_NAME}` command that runs in the "
            "background returns its process_id."
        )
    rows = []
    for tracked in owned[:_LISTED_COMMANDS]:
        label = _command_label(tracked.command)
        rows.append(f"{tracked.process_id} ({tracked.status}{': ' + label if label else ''})")
    text = "Your commands: " + "; ".join(rows) + "."
    if len(owned) > _LISTED_COMMANDS:
        text += ' List them all with {"action": "status", "filter": "all"}.'
    return text


def _command_label(command: str | None) -> str | None:
    if not command:
        return None
    label = " ".join(command.split())
    if len(label) > _COMMAND_LABEL_CHARS:
        label = label[: _COMMAND_LABEL_CHARS - 3] + "..."
    return label


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
        "command": _command_label(tracked.command),
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
        "output": fields["output"],
        "truncated": fields["truncated"],
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
    output = _final_line_text(output)
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


def _final_line_text(output: str) -> str:
    """Show output as a terminal leaves it: plain line ends, redrawn lines in final form.

    Windows programs end lines with CRLF, and progress bars redraw one line after
    a bare carriage return; the log file keeps the raw text.
    """
    if "\r" not in output:
        return output
    output = output.replace("\r\n", "\n")
    if "\r" not in output:
        return output
    return "\n".join(_last_segment(line) for line in output.split("\n"))


def _last_segment(line: str) -> str:
    segments = [segment for segment in line.split("\r") if segment]
    return segments[-1] if segments else ""


def _truncation_marker(log_file: str | None) -> str:
    if log_file is None:
        return "[earlier output truncated]\n"
    return f"[earlier output truncated — complete output in {log_file}]\n"


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
    """Register Agent-facing control for background shell commands."""
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
        unadvertised_parameters=PROCESS_UNADVERTISED_PARAMETERS,
        argument_normalizer=normalize_process_arguments,
    )


def _process_display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    try:
        normalized = normalize_process_arguments(arguments)
    except ValueError:
        normalized = arguments
    if not isinstance(normalized, dict):
        return ()
    action = normalized.get("action")
    if not isinstance(action, str) or action not in PROCESS_ACTIONS:
        return ()
    parts = [ToolDisplayPart(action, truncate="never", tooltip="none")]
    process_id = normalized.get("process_id")
    if isinstance(process_id, str) and process_id:
        parts.append(ToolDisplayPart(process_id, kind="identifier", truncate="middle"))
    pattern = normalized.get("pattern")
    if action == "wait" and isinstance(pattern, str) and pattern.strip():
        parts.append(ToolDisplayPart(pattern, kind="query"))
    return tuple(parts)


__all__ = [
    "PROCESS_ACTIONS",
    "PROCESS_WAIT_DEFAULT_SECONDS",
    "PROCESS_WAIT_MAX_SECONDS",
    "normalize_process_arguments",
    "shape_process_output",
    "PROCESS_TOOL_DESCRIPTION",
    "PROCESS_TOOL_NAME",
    "PROCESS_TOOL_PARAMETERS",
    "make_process_handler",
    "register_process_tool",
]
