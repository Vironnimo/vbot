"""Bash completion results, handoff guidance, and durable status projection."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from core.tools.bash_hints import annotate_failure
from core.tools.process import shape_process_output
from core.tools.process_manager import (
    ProcessManager,
    ProcessNotFoundError,
    TrackedProcess,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    tool_failure,
    tool_success,
)
from core.utils.paths import model_path

BASH_TOOL_NAME = "bash"
BASH_COMPLETION_STATUS_PREFIX = "### Bash process — "
BASH_COMPLETION_PROCESS_ID_PREFIX = "Process ID: "
BASH_HANDOFF_OUTPUT_CAP_CHARS = 4_000
BASH_HANDOFF_OUTPUT_MAX_LINES = 20
USER_CANCELLED_FAILURE_CODE = "cancelled_by_user"
USER_CANCELLED_FAILURE_MESSAGE = "Command aborted by the user"
BACKGROUND_USER_CANCELLED_MESSAGE = "Background process was aborted by the user."


def _format_elapsed_duration(seconds: float) -> str:
    """Render an elapsed time compactly: ``45s``, ``14m 5s``, ``1h 2m 3s``."""
    total_seconds = max(0, int(round(seconds)))
    if total_seconds == 0:
        return "<1s"
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if hours or minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _user_cancelled_failure_message(
    process_manager: ProcessManager,
    context: ToolContext,
    process_id: str,
) -> str:
    """User-abort message extended by how long the command ran before the abort."""
    try:
        tracked = process_manager.get_process(
            process_id, context.agent_id, project_id=context.project_id
        )
    except ProcessNotFoundError:
        return USER_CANCELLED_FAILURE_MESSAGE
    elapsed_seconds = (datetime.now(UTC) - tracked.started_at).total_seconds()
    return f"{USER_CANCELLED_FAILURE_MESSAGE} after {_format_elapsed_duration(elapsed_seconds)}"


def _background_user_cancelled_message(tracked: TrackedProcess) -> str:
    """Background user-abort note extended by the process runtime before the abort."""
    if tracked.finished_at is None:
        return BACKGROUND_USER_CANCELLED_MESSAGE
    elapsed_seconds = (tracked.finished_at - tracked.started_at).total_seconds()
    return (
        f"Background process was aborted by the user "
        f"after {_format_elapsed_duration(elapsed_seconds)}."
    )


async def _background_result(
    process_manager: ProcessManager,
    context: ToolContext,
    process_id: str,
    *,
    mode: str,
    handoff_after: float | None,
    requested_by_user: bool = False,
) -> JsonObject:
    process_manager.mark_backgrounded(process_id, context.agent_id, project_id=context.project_id)
    tracked = process_manager.get_process(
        process_id, context.agent_id, project_id=context.project_id
    )
    output = await _combined_output(process_manager, context, process_id)
    fields = _shape_output_fields(tracked, output, handoff=True)
    if tracked.log_file is not None:
        # A background process keeps writing after this result; always hand the
        # model the log path so it can grep progress without polling.
        fields["log_file"] = model_path(tracked.log_file)
    result: JsonObject = {
        "status": "running",
        "process_id": process_id,
        **fields,
    }
    result["delivery"] = "automatic"
    result["handoff_note"] = _handoff_note(mode, handoff_after, requested_by_user=requested_by_user)
    return tool_success(result)


def background_bash_statuses(messages: Sequence[Any]) -> JsonObject:
    """Fold durable Bash and Process results into background-process statuses.

    Background Bash results are immutable handoff records, so their terminal
    state arrives later in either an automatic completion note or a manually
    persisted Process Tool Result. The WebUI history response uses this folded
    projection without exposing internal notes themselves.
    """
    statuses: JsonObject = {}
    for message in messages:
        role = getattr(message, "role", None)
        content = getattr(message, "content", None)
        if role == "tool" and isinstance(content, str):
            _fold_background_tool_status(
                statuses,
                getattr(message, "name", None),
                content,
            )
        elif role == "note" and isinstance(content, str):
            _fold_background_completion_note(statuses, content)
    return statuses


def _fold_background_tool_status(statuses: JsonObject, tool_name: Any, content: str) -> None:
    try:
        envelope = json.loads(content)
    except json.JSONDecodeError:
        return
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        return
    data = envelope.get("data")
    if not isinstance(data, dict):
        return

    if tool_name == BASH_TOOL_NAME:
        if data.get("delivery") != "automatic":
            return
        _record_background_status(statuses, data)
        return
    if tool_name != "process":
        return

    _record_background_status(statuses, data)
    processes = data.get("processes")
    if isinstance(processes, list):
        for tracked in processes:
            if isinstance(tracked, dict):
                _record_background_status(statuses, tracked)


def _record_background_status(statuses: JsonObject, data: JsonObject) -> None:
    process_id = data.get("process_id")
    status = data.get("status")
    if (
        isinstance(process_id, str)
        and process_id
        and status
        in {
            "running",
            "completed",
            "failed",
            "killed",
            "cancelled",
        }
    ):
        statuses[process_id] = status


def _fold_background_completion_note(statuses: JsonObject, content: str) -> None:
    pending_status: str | None = None
    for line in content.splitlines():
        if line.startswith(BASH_COMPLETION_STATUS_PREFIX):
            pending_status = _completion_process_status(
                line.removeprefix(BASH_COMPLETION_STATUS_PREFIX)
            )
            continue
        if pending_status is None or not line.startswith(BASH_COMPLETION_PROCESS_ID_PREFIX):
            continue
        process_id = line.removeprefix(BASH_COMPLETION_PROCESS_ID_PREFIX).strip()
        if process_id:
            statuses[process_id] = pending_status
        pending_status = None


def _completion_process_status(status: str) -> str | None:
    normalized = status.strip().lower()
    if normalized == "aborted by user":
        return "cancelled"
    if normalized in {"completed", "failed", "killed"}:
        return normalized
    return None


async def _completion_result(
    process_manager: ProcessManager,
    context: ToolContext,
    process_id: str,
    *,
    command: str,
) -> JsonObject:
    tracked = process_manager.get_process(
        process_id, context.agent_id, project_id=context.project_id
    )
    if tracked.cancelled_by_user:
        return tool_failure(
            USER_CANCELLED_FAILURE_CODE,
            _user_cancelled_failure_message(process_manager, context, process_id),
        )
    output = await _combined_output(process_manager, context, process_id)
    result: JsonObject = {
        "status": "completed",
        "exit_code": tracked.exit_code,
        **_shape_output_fields(tracked, output),
    }
    hint = annotate_failure(command, tracked.exit_code, output)
    if hint:
        result["hint"] = hint
    return tool_success(result)


def _handoff_note(
    mode: str, handoff_after: float | None, *, requested_by_user: bool = False
) -> str:
    if requested_by_user and handoff_after is not None:
        transition = (
            "The user moved this command to the background after "
            f"{_format_elapsed_duration(handoff_after)}. The command is still running."
        )
    elif mode == "auto" and handoff_after is not None:
        transition = (
            "The command is still running and has been handed off to vBot after "
            f"{handoff_after:g} seconds."
        )
    else:
        transition = "The command is still running and has been handed off to vBot immediately."
    note = (
        f"{transition} vBot will monitor it and deliver its terminal result automatically "
        "in one coalesced follow-up Run. You may continue work that does not depend on "
        "this result, or finish the current Run now."
    )
    if requested_by_user:
        return note
    return (
        f"{note} Do not poll merely to wait, and do "
        "not start another copy of the command. If your next action depends on the "
        "result, inspect the process explicitly or use foreground mode next time."
    )


def _shape_output_fields(
    tracked: TrackedProcess, output: str, *, handoff: bool = False
) -> JsonObject:
    """Apply the shared Process output policy, with a smaller snapshot at handoff."""
    limits = (
        {"max_lines": BASH_HANDOFF_OUTPUT_MAX_LINES, "max_chars": BASH_HANDOFF_OUTPUT_CAP_CHARS}
        if handoff
        else {}
    )
    return shape_process_output(
        output,
        truncated=tracked.truncated,
        log_file=model_path(tracked.log_file) if tracked.log_file is not None else None,
        **limits,
    )


async def _failure_output_suffix(
    process_manager: ProcessManager,
    context: ToolContext,
    process_id: str,
) -> str:
    """Build the output tail + log pointer appended to timeout-style failures.

    Without it a killed command fails with only the timing fact and every byte
    of diagnostics the process printed is lost to the model.
    """
    output = await _combined_output(process_manager, context, process_id)
    tracked = process_manager.get_process(
        process_id, context.agent_id, project_id=context.project_id
    )

    parts: list[str] = []
    if output:
        fields = _shape_output_fields(tracked, output)
        label = "Output tail" if fields["truncated"] else "Output"
        parts.append(f"\n{label}:\n{fields['output']}")
    if tracked.log_file is not None:
        parts.append(f"\nComplete output: {model_path(tracked.log_file)}")
    return "".join(parts)


def _spawn_failure_message(argv: list[str], error: Exception) -> str:
    message = f"failed to start process: {error}"
    if not isinstance(error, FileNotFoundError):
        return message

    shell = argv[0] if argv else "the shell"
    message += f". The shell '{shell}' was not found on this host"
    if shell == "pwsh":
        message += (
            " — the bash tool requires PowerShell 7 (pwsh) on Windows; install it or add it to PATH"
        )
    return message


async def _combined_output(
    process_manager: ProcessManager,
    context: ToolContext,
    process_id: str,
) -> str:
    log_result = await process_manager.log(
        process_id,
        context.agent_id,
        offset=0,
        limit=None,
        project_id=context.project_id,
    )
    output = log_result.get("output", "")
    return output if isinstance(output, str) else ""
