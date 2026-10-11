"""The ``background_tasks`` Tool: list, check and stop the caller's background work.

Background work is what runs on after the call that started it: shell commands
that continue in the background (``cmd_`` ids, owned by ``CommandManager``),
those of the caller's Sub-Agents at every depth, and Sub-Agents themselves
(``sub_`` ids). The Tool shows the caller its own Session's work and that of its
descendant Sessions, never a parent's or a sibling's (``SubAgentTree``). It never
waits and never writes input or messages: results and answers arrive as new
messages.

The id's prefix selects the kind. Commands are handled here; a ``term_`` id
belongs to ``terminal``, and any other id names no task of the caller. This
module owns the schema, the texts and the result layout; command results use
the shell result's fields (``_shell_results``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from core.sessions import SessionAddress
from core.tools._background_tasks_arguments import (
    ACTION_MISSING,
    BACKGROUND_TASK_ACTIONS,
    BACKGROUND_TASKS_TOOL_NAME,
    BACKGROUND_TASKS_UNADVERTISED_PARAMETERS,
    COMMAND_ID_PREFIX,
    INPUT_ACTION,
    TERMINAL_ID_PREFIX,
    normalize_background_tasks_arguments,
)
from core.tools._shell_results import (
    TERMINAL_TOOL_NAME,
    ToolNames,
    duration,
    ended_result,
    log_sentence,
    named_tools,
    output_location,
    processes_text,
    running_output_text,
    stop_call,
)
from core.tools._tool_display import display_notice, display_results, display_text
from core.tools.availability import TOOL_ACTIVATION_FOLLOWS
from core.tools.command_manager import (
    CommandManager,
    CommandNotFoundError,
    CommandReport,
    CommandStopError,
    StopReason,
)
from core.tools.model_names import BASH_TOOL_NAME
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.paths import model_path

BACKGROUND_TASKS_TOOL_DESCRIPTION = "List, check or stop your background commands and Sub-Agents."
BACKGROUND_TASKS_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(BACKGROUND_TASK_ACTIONS),
            "description": (
                "status shows one task's state and, for a command, its newest output; stop "
                "stops a task and everything it started."
            ),
        },
        "id": {
            "type": "string",
            "description": (
                "Id of a background command or Sub-Agent, from a result or from list. Required "
                "for status and stop."
            ),
        },
    },
    "required": ["action"],
}

# The unknown-id error names at most this many of the caller's running tasks.
_YOURS_SHOWN = 5
_STOPPED_BY_YOU = "you stopped it."


class SubAgentTree(Protocol):
    """The caller's Sub-Agents, whose background work the caller sees."""

    async def descendant_sessions(self, caller: SessionAddress) -> Mapping[SessionAddress, str]:
        """The Sessions of *caller*'s Sub-Agents at every depth, each with its Sub-Agent id."""
        ...


class BackgroundTasksTool:
    """Serves ``background_tasks`` calls from the ``CommandManager``.

    *subagents* tells which Sessions descend from the caller; without it the
    caller sees only its own Session's commands.
    """

    def __init__(self, commands: CommandManager, subagents: SubAgentTree | None = None) -> None:
        self._commands = commands
        self._subagents = subagents

    async def __call__(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        names = named_tools(context)
        if isinstance(arguments.get(INPUT_ACTION), str):
            return tool_failure("invalid_arguments", _no_input(names))
        caller = SessionAddress(
            project_id=context.project_id,
            agent_id=context.agent_id,
            session_id=context.session_id,
        )
        # Each Session this call sees, with the id of the Sub-Agent it belongs to.
        tree: dict[SessionAddress, str | None] = {caller: None}
        if self._subagents is not None:
            descendants = await self._subagents.descendant_sessions(caller)
            tree.update(descendants)
        action = arguments["action"]
        if action == "list":
            return self._list(caller, tree)
        task_id = arguments.get("id")
        if not isinstance(task_id, str) or not task_id:
            return tool_failure("invalid_arguments", self._missing_id(action, tree))
        if task_id.startswith(TERMINAL_ID_PREFIX):
            return tool_failure("not_a_background_task", _terminal_refusal(task_id, names))
        report = self._visible(task_id, tree) if task_id.startswith(COMMAND_ID_PREFIX) else None
        if report is None:
            return tool_failure("task_not_found", self._not_found(task_id, action, tree))
        subagent = tree[report.owner]
        if action == "status":
            data = self._status(context, report, subagent, names)
        else:
            stopped = await self._stop(context, report, subagent, names, tree)
            if "error" in stopped:
                return stopped
            data = stopped
        if arguments.get(ACTION_MISSING) is True:
            note = (
                f"action is missing, so the status is shown. To stop it, call {stop_call(task_id)}."
            )
            data.setdefault("notes", []).insert(0, note)
        return tool_success(data)

    # list

    def _list(
        self, caller: SessionAddress, tree: Mapping[SessionAddress, str | None]
    ) -> JsonObject:
        reports = self._commands.commands(tree)
        rows = [_command_row(report, tree[report.owner]) for report in reports]
        data: JsonObject = {"tasks": rows}
        if not rows:
            data["notes"] = [
                "You have no Sub-Agents, and no background commands ran in this Session or its "
                "Sub-Agents in the last 30 minutes."
            ]
        elif any(report.owner == caller and not report.ended for report in reports):
            data["notes"] = [
                "vBot sends you each answer of a working Sub-Agent and the result of each of "
                "your running commands as a new message; calling list again does not make them "
                "finish faster."
            ]
        return tool_success(data)

    # status

    def _status(
        self,
        context: ToolContext,
        report: CommandReport,
        subagent: str | None,
        names: ToolNames,
    ) -> JsonObject:
        if report.ended:
            if subagent is None:
                self._acknowledge_when_persisted(context, report.command_id)
            return _ended(report, subagent, names)
        data: JsonObject = {"status": "running", "command_id": report.command_id}
        if subagent is not None:
            data["subagent"] = subagent
        output = running_output_text(report)
        if output:
            data["output"] = output
        if subagent is None:
            delivery = (
                "Its result arrives as a new message when it exits; do not sleep or poll for "
                "that result."
            )
        else:
            delivery = f"Its result goes to the Sub-Agent {subagent}."
        data["next"] = (
            f"The command has run for {duration(report.elapsed_seconds)} and keeps running."
            f"{log_sentence(report)} {delivery} Stop it with {stop_call(report.command_id)} if "
            "you no longer need it."
        )
        return data

    # stop

    async def _stop(
        self,
        context: ToolContext,
        before: CommandReport,
        subagent: str | None,
        names: ToolNames,
        tree: Mapping[SessionAddress, str | None],
    ) -> JsonObject:
        """Stop the command; an ancestor's stop is delivered to the command's own Session."""
        command_id = before.command_id
        # The Agent of the command's Session sees this result; an ancestor's
        # Sub-Agent did not see the stop and gets a message.
        reason: StopReason = "agent" if subagent is None else "parent"
        try:
            report = await self._commands.stop(command_id, reason)
        except CommandNotFoundError:
            return tool_failure("task_not_found", self._not_found(command_id, "stop", tree))
        except CommandStopError as error:
            survivors = processes_text(error.survivors)
            still = (
                f" Its processes may still run: {survivors}."
                if survivors
                else (" Its processes may still run.")
            )
            return tool_failure(
                "stop_failed",
                f"Stopping {command_id} failed: the operating system did not confirm that its "
                f"processes ended.{still} Call {stop_call(command_id)} again; if it fails the "
                "same way, tell the user.",
                retryable=True,
            )
        # Every stop result shows the command's end; its own Session needs no delivery.
        if subagent is None:
            self._acknowledge_when_persisted(context, command_id)
        data = _ended(report, subagent, names)
        notes: list[str] = []
        if before.ended or report.stop_reason is None:
            if before.still_running:
                notes.append(
                    "The shell had already exited; the processes it left running were stopped: "
                    f"{processes_text(before.still_running)}."
                )
                context.add_display_notice(
                    "info", f"Stopped the processes {report.label} left running."
                )
            else:
                notes.append("The command had already ended; nothing was stopped.")
                context.add_display_notice("info", f"{report.label} had already ended.")
        else:
            if report.stop_reason == reason:
                data["stopped_because"] = _STOPPED_BY_YOU
                if subagent is not None:
                    notes.append(
                        f"The Sub-Agent {subagent} receives a message that you stopped it."
                    )
            context.add_display_notice("info", f"Stopped {report.label}.")
        if notes:
            data["notes"] = notes
        return data

    def _acknowledge_when_persisted(self, context: ToolContext, command_id: str) -> None:
        """Once this result showing the end is stored, nothing more is delivered."""
        context.after_result_persisted(lambda: self._commands.acknowledge(command_id))

    # Lookups and errors

    def _visible(
        self, task_id: str, tree: Mapping[SessionAddress, str | None]
    ) -> CommandReport | None:
        """The background command *task_id* when one of the *tree* Sessions owns it."""
        try:
            report = self._commands.report(task_id)
        except CommandNotFoundError:
            return None
        if not report.background or report.owner not in tree:
            return None
        return report

    def _yours(self, tree: Mapping[SessionAddress, str | None]) -> str:
        running = [report for report in self._commands.commands(tree) if not report.ended]
        if not running:
            return "Nothing of yours is running now."
        entries = ", ".join(
            f"{report.command_id} ({report.label})" for report in running[:_YOURS_SHOWN]
        )
        more = len(running) - _YOURS_SHOWN
        return f"Running now: {entries}{f' and {more} more' if more > 0 else ''}."

    def _missing_id(self, action: str, tree: Mapping[SessionAddress, str | None]) -> str:
        if action == "stop":
            problem = (
                "stop needs id, the background command or Sub-Agent to stop; nothing was stopped."
            )
        else:
            problem = f"{action} needs id, the id of a background command or Sub-Agent."
        return f"{problem} {self._yours(tree)}"

    def _not_found(
        self, task_id: str, action: str, tree: Mapping[SessionAddress, str | None]
    ) -> str:
        message = (
            f"No background command or Sub-Agent {task_id} belongs to this Session or its "
            "Sub-Agents"
        )
        if task_id.startswith(COMMAND_ID_PREFIX):
            message += "; vBot keeps an ended command for 30 minutes"
        message += "."
        if action == "stop":
            message += " Nothing was stopped."
        return f"{message} {self._yours(tree)}"


# Results


def _command_row(report: CommandReport, subagent: str | None) -> JsonObject:
    """One background command in a list."""
    row: JsonObject = {"id": report.command_id, "type": "command", "command": report.label}
    if subagent is not None:
        row["subagent"] = subagent
    if not report.ended:
        row["status"] = "running"
    else:
        row["status"] = "stopped" if report.stop_reason is not None else "exited"
        if report.exit_code is not None:
            row["exit_code"] = report.exit_code
    row["runtime"] = duration(report.elapsed_seconds)
    if report.log_path is not None:
        row["log"] = model_path(report.log_path)
    return row


def _ended(report: CommandReport, subagent: str | None, names: ToolNames) -> JsonObject:
    """An ended command as the shell reports it, with its id and its complete output's file."""
    shell = ended_result(report, names)
    data: JsonObject = {"status": shell.pop("status"), "command_id": report.command_id}
    if subagent is not None:
        data["subagent"] = subagent
    shell.pop("command_id", None)
    data.update(shell)
    location = output_location(report, str(data.get("output", "")))
    if location is not None:
        data["next"] = f"{data['next']} {location}" if "next" in data else location
    return data


def _no_input(names: ToolNames) -> str:
    if names(TERMINAL_TOOL_NAME):
        return (
            "Nothing was sent: background commands get no input. Run a program you need to "
            f"answer with {TERMINAL_TOOL_NAME}."
        )
    return (
        "Nothing was sent: background commands get no input. Give the command its input "
        "through a file or a pipeline instead."
    )


def _terminal_refusal(terminal_id: str, names: ToolNames) -> str:
    refusal = f"{terminal_id} is a terminal, not a background task. Nothing was changed."
    if not names(TERMINAL_TOOL_NAME):
        return refusal
    call = f'{TERMINAL_TOOL_NAME} {{"action": "status", "terminal_id": "{terminal_id}"}}'
    return f"{refusal} Use {TERMINAL_TOOL_NAME} for it, such as {call}."


# Display


def _display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    # Persisted calls keep the Model's own spelling; label what the call meant.
    try:
        normalized = normalize_background_tasks_arguments(arguments)
    except ValueError:
        normalized = arguments
    if not isinstance(normalized, dict):
        return ()
    action = normalized.get(INPUT_ACTION) or normalized.get("action")
    if not isinstance(action, str) or not action:
        return ()
    parts = [ToolDisplayPart(action, truncate="never", tooltip="none")]
    task_id = normalized.get("id")
    if action != "list" and isinstance(task_id, str) and task_id:
        parts.append(ToolDisplayPart(task_id, kind="identifier", truncate="middle"))
    return tuple(parts)


def _detail_blocks(arguments: JsonObject, result: JsonObject | None) -> list[JsonObject]:
    """Show the user the listed tasks, or a command's output and outcome."""
    data = result.get("data") if isinstance(result, dict) and result.get("ok") is True else None
    if not isinstance(data, dict):
        return []
    tasks = data.get("tasks")
    if isinstance(tasks, list):
        rows = [row for row in tasks if isinstance(row, dict)]
        if not rows:
            return [display_notice("info", "No background tasks.")]
        return [
            display_results(
                [
                    {
                        "title": str(row.get("command") or row.get("title") or row.get("id")),
                        "meta": " · ".join(
                            str(row[key])
                            for key in ("id", "status", "runtime")
                            if row.get(key) is not None
                        ),
                    }
                    for row in rows
                ]
            )
        ]
    blocks = [display_text("output", source="result", path=("data", "output"))]
    if data.get("status") == "running":
        blocks.append(display_notice("info", "The command keeps running in the background."))
    exit_code = data.get("exit_code")
    if (
        data.get("status") == "exited"
        and isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
        and exit_code != 0
    ):
        blocks.append(display_notice("warning", f"The command exited with code {exit_code}."))
    return blocks


def register_background_tasks_tool(
    registry: ToolRegistry,
    command_manager: CommandManager,
    *,
    subagents: SubAgentTree | None = None,
) -> BackgroundTasksTool:
    """Register ``background_tasks``, active while the shell Tool is."""
    tool = BackgroundTasksTool(command_manager, subagents)
    registry.register(
        BACKGROUND_TASKS_TOOL_NAME,
        BACKGROUND_TASKS_TOOL_DESCRIPTION,
        BACKGROUND_TASKS_TOOL_PARAMETERS,
        tool,
        catalog_visible=False,
        family="execution",
        activation=TOOL_ACTIVATION_FOLLOWS,
        activation_source=BASH_TOOL_NAME,
        open_input_schema=True,
        unadvertised_parameters=BACKGROUND_TASKS_UNADVERTISED_PARAMETERS,
        argument_normalizer=normalize_background_tasks_arguments,
        result_schema={"type": "object"},
        display=ToolDisplay(parts_builder=_display_parts, detail_builder=_detail_blocks),
    )
    return tool


__all__ = [
    "BACKGROUND_TASKS_TOOL_DESCRIPTION",
    "BACKGROUND_TASKS_TOOL_NAME",
    "BACKGROUND_TASKS_TOOL_PARAMETERS",
    "BackgroundTasksTool",
    "SubAgentTree",
    "register_background_tasks_tool",
]
