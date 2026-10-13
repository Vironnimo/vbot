"""The ``background_tasks`` Tool: list, check and stop the caller's background work.

Background work is what runs on after the call that started it: shell commands
that continue in the background (``cmd_`` ids, owned by ``CommandManager``) and
Sub-Agents (``sub_`` ids, owned by the ``SubAgentCoordinator``), each together
with those of the caller's Sub-Agents at every depth. The Tool shows the caller
its own Session's work and that of its descendant Sessions, never a parent's or
a sibling's (``SubAgentTree``). It never waits and never writes input or
messages: results and answers arrive as new messages.

The id's prefix selects the owner; a ``term_`` id belongs to ``terminal``, a
``ses_`` id is read as the Sub-Agent working in that Session, and any other id
names no task of the caller. The owners keep the behavior; this module owns the
schema, the texts and the result layout. Command results use the shell result's
fields (``_shell_results``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from core.sessions import SessionAddress
from core.tools._background_tasks_arguments import (
    ACTION_MISSING,
    BACKGROUND_TASK_ACTIONS,
    BACKGROUND_TASKS_TOOL_NAME,
    BACKGROUND_TASKS_UNADVERTISED_PARAMETERS,
    COMMAND_ID_PREFIX,
    INPUT_ACTION,
    MESSAGE_CONTENT,
    NO_EFFECT_FIELDS,
    NO_EFFECT_WAIT,
    SESSION_ID_PREFIX,
    SUBAGENT_ID_PREFIX,
    TERMINAL_ID_PREFIX,
    is_message_action,
    normalize_background_tasks_arguments,
)
from core.tools._shell_results import (
    TERMINAL_TOOL_NAME,
    ToolNames,
    duration,
    ended_result,
    named_tools,
    output_location,
    process_text,
    processes_text,
    running_output_note,
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
from core.tools.model_names import BASH_TOOL_NAME, SUBAGENT_TOOL_NAME
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

if TYPE_CHECKING:
    # Imported for annotations only: ``core.subagents`` imports the Tool package.
    from core.subagents import SubAgentState, SubAgentTree

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
# From this status of one running command on, its result says how often it was checked.
_CHECKS_NAMED = 3
# A list shows at most this many Sub-Agents of a tree.
_SUBAGENT_ROWS_SHOWN = 1_000
_STOPPED_BY_YOU = "you stopped it."
# Sub-Agent statuses whose answers are still to come.
_WORKING = frozenset({"working", "queued"})
_TAKEN_OVER = "taken over by the user"


@dataclass(frozen=True)
class _Tree:
    """What one call sees: the caller's Sub-Agents and the Sessions whose commands it sees.

    A takeover is a boundary: the caller keeps the row and the status of a Sub-Agent
    the user took over, but that Sub-Agent, what its Session runs and the Sub-Agents
    below it work for the user (``SubAgentState.within_takeover``), and no stop of
    the caller reaches them.
    """

    subagents: Sequence[SubAgentState]
    # Each Session with the id of the Sub-Agent working in it; the caller's own is None.
    sessions: Mapping[SessionAddress, str | None]
    # Every Sub-Agent below the caller, those within a takeover included.
    everyone: Sequence[SubAgentState]

    def subagent(self, subagent_id: str) -> SubAgentState | None:
        return next((item for item in self.subagents if item.id == subagent_id), None)

    def working_in(self, session_id: str) -> SubAgentState | None:
        """The one Sub-Agent below the caller working in *session_id*, within a takeover too."""
        matches = [item for item in self.everyone if item.session.session_id == session_id]
        return matches[0] if len(matches) == 1 else None


class BackgroundTasksTool:
    """Serves ``background_tasks`` calls from the ``CommandManager`` and the Sub-Agent tree.

    Without *subagents* the caller sees only its own Session's commands.
    """

    def __init__(self, commands: CommandManager, subagents: SubAgentTree | None = None) -> None:
        self._commands = commands
        self._subagents = subagents
        # How often status showed each command still running.
        self._checks: dict[str, int] = {}

    def _count_check(self, command_id: str) -> int:
        """Count a status of a running command; forget commands vBot no longer keeps."""
        for known in [item for item in self._checks if item != command_id]:
            if self._commands.status(known) != "running":
                del self._checks[known]
        self._checks[command_id] = self._checks.get(command_id, 0) + 1
        return self._checks[command_id]

    async def __call__(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        names = named_tools(context)
        caller = SessionAddress(
            project_id=context.project_id,
            agent_id=context.agent_id,
            session_id=context.session_id,
        )
        input_action = arguments.get(INPUT_ACTION)
        if isinstance(input_action, str):
            refusal = await self._input_refusal(context, caller, input_action, arguments, names)
            return tool_failure("invalid_arguments", refusal)
        tree = await self._tree(caller)
        action = arguments["action"]
        no_effect = arguments.get(NO_EFFECT_FIELDS) or {}
        if action == "list":
            data = self._list(caller, tree)
            ignored = _no_effect_notes(no_effect, shown="list")
            if ignored:
                data["notes"] = [*ignored, *data.get("notes", [])]
            return tool_success(data)
        task_id = arguments.get("id")
        if not isinstance(task_id, str) or not task_id:
            return tool_failure("invalid_arguments", self._missing_id(action, tree))
        if task_id.startswith(TERMINAL_ID_PREFIX):
            return tool_failure("not_a_background_task", _terminal_refusal(task_id, names))
        notes: list[str] = []
        if task_id.startswith(SESSION_ID_PREFIX):
            # A Session id from a result names the Sub-Agent working in it.
            working = tree.working_in(task_id)
            if working is not None:
                notes.append(f"session_id {task_id} was read as Sub-Agent {working.id}.")
                task_id = working.id
        taken_over = self._taken_over(task_id, action, tree)
        if taken_over is not None:
            return tool_failure("subagent_taken_over", taken_over)
        if task_id.startswith(SUBAGENT_ID_PREFIX):
            data = await self._subagent_task(context, caller, task_id, action, tree)
        else:
            data = await self._command_task(context, task_id, action, names, tree)
        if data.get("ok") is False:
            return data
        if arguments.get(ACTION_MISSING) is True:
            missing = "action is missing, so the status is shown."
            if self._taken_over(task_id, "stop", tree) is None:
                # The user's Sub-Agent is the user's to stop.
                missing += f" To stop it, call {stop_call(task_id)}."
            notes.append(missing)
        if no_effect:
            if task_id.startswith(SUBAGENT_ID_PREFIX):
                subagent = tree.subagent(task_id)
                activity = subagent.activity_file if subagent is not None else None
                notes.extend(_no_effect_notes(no_effect, shown="subagent", log=activity))
            else:
                log = self._log_file(task_id)
                notes.extend(_no_effect_notes(no_effect, shown="output", log=log))
        if notes:
            data["notes"] = [*notes, *data.get("notes", [])]
        return tool_success(data)

    async def _input_refusal(
        self,
        context: ToolContext,
        caller: SessionAddress,
        action: str,
        arguments: JsonObject,
        names: ToolNames,
    ) -> str:
        """The refusal of a call that asked to give a task input or a message."""
        target = arguments.get("id")
        if not is_message_action(action) or not isinstance(target, str):
            return _no_input(names)
        subagent_id: str | None = target
        if target.startswith(SESSION_ID_PREFIX):
            # A Session id names the Sub-Agent working in it, as for status and stop.
            working = (await self._tree(caller)).working_in(target)
            subagent_id = working.id if working is not None else None
        content = arguments.get(MESSAGE_CONTENT)
        return _message_refusal(
            subagent_id,
            content if isinstance(content, str) else None,
            can_send=context.can_call(SUBAGENT_TOOL_NAME),
        )

    async def _tree(self, caller: SessionAddress) -> _Tree:
        everyone = await self._subagents.subagents(caller) if self._subagents is not None else ()
        sessions: dict[SessionAddress, str | None] = {caller: None}
        sessions.update(
            (item.session, item.id) for item in everyone if item.within_takeover is None
        )
        return _Tree(
            subagents=[item for item in everyone if item.within_takeover in (None, item.id)],
            sessions=sessions,
            everyone=everyone,
        )

    async def _subagent_task(
        self,
        context: ToolContext,
        caller: SessionAddress,
        subagent_id: str,
        action: str,
        tree: _Tree,
    ) -> JsonObject:
        subagent = tree.subagent(subagent_id)
        if subagent is None or self._subagents is None:
            return tool_failure("task_not_found", self._not_found(subagent_id, action, tree))
        if action == "status":
            data = _subagent_fields(subagent, with_project=True)
            # What a taken-over Session runs works for the user; its ids would be refused.
            running = (
                await self._subagents.running_work(subagent)
                if subagent.within_takeover is None
                else ()
            )
            if running:
                data["running"] = list(running)
            data["next"] = _subagent_next(subagent, can_send=context.can_call(SUBAGENT_TOOL_NAME))
            return data
        outcome = await self._subagents.stop_subagent(context, subagent)
        if outcome == _TAKEN_OVER:
            # The user took it over, or a Sub-Agent above it, after this call read the tree.
            fresh = await self._tree(caller)
            refusal = self._taken_over(subagent_id, action, fresh)
            if refusal is None:
                return tool_failure("task_not_found", self._not_found(subagent_id, action, fresh))
            return tool_failure("subagent_taken_over", refusal)
        data = _subagent_identity(subagent)
        if outcome == "stopped":
            data["status"] = "stopped"
            note = (
                "Stopped: its current turn, its queued messages, its background commands and "
                "terminals, and its own Sub-Agents. Its Session keeps the history"
            )
            if not context.can_call(SUBAGENT_TOOL_NAME):
                # Without subagent, the caller cannot reach it with a message.
                note += "."
            else:
                # Only the Sub-Agent's own Parent can message it.
                sender = (
                    "send it a message"
                    if subagent.parent_id is None
                    else f"your Sub-Agent {subagent.parent_id} can send it a message"
                )
                note += f"; {sender} to continue."
        else:
            data["status"] = "idle"
            note = (
                f"Sub-Agent {subagent.id} and its own Sub-Agents were idle and ran no background "
                "commands or terminals, so nothing needed stopping."
            )
        notes = [note]
        kept = _taken_over_below(subagent, tree)
        if kept is not None:
            # The stop ends where the user took over; without this the Agent reports
            # the user's own work as stopped.
            notes.append(kept)
        data["notes"] = notes
        return data

    async def _command_task(
        self,
        context: ToolContext,
        task_id: str,
        action: str,
        names: ToolNames,
        tree: _Tree,
    ) -> JsonObject:
        report = self._visible(task_id, tree) if task_id.startswith(COMMAND_ID_PREFIX) else None
        if report is None:
            return tool_failure("task_not_found", self._not_found(task_id, action, tree))
        subagent = tree.sessions[report.owner]
        if action == "status":
            return self._status(context, report, subagent, names)
        return await self._stop(context, report, subagent, names, tree)

    # list

    def _list(self, caller: SessionAddress, tree: _Tree) -> JsonObject:
        reports = self._commands.commands(tree.sessions)
        rows = [_subagent_fields(item) for item in tree.subagents[:_SUBAGENT_ROWS_SHOWN]]
        rows.extend(_command_row(report, tree.sessions[report.owner]) for report in reports)
        data: JsonObject = {"tasks": rows}
        if not rows:
            data["notes"] = [
                "You have no Sub-Agents, and no background commands ran in this Session or its "
                "Sub-Agents in the last 30 minutes."
            ]
        elif any(
            item.parent_id is None and item.status in _WORKING for item in tree.subagents
        ) or any(report.owner == caller and not report.ended for report in reports):
            data["notes"] = [
                "vBot sends you each answer of a working Sub-Agent and the result of each of "
                "your running commands as a new message; calling list again does not make them "
                "finish faster."
            ]
        return data

    # status

    def _status(
        self,
        context: ToolContext,
        report: CommandReport,
        subagent: str | None,
        names: ToolNames,
    ) -> JsonObject:
        # The call names only the id: the user sees which command it is.
        context.add_display_results([_shown_task(_command_row(report, subagent))])
        if report.ended:
            self._checks.pop(report.command_id, None)
            if subagent is None:
                self._acknowledge_when_persisted(context, report.command_id)
            return _ended(report, subagent, names)
        data: JsonObject = {"status": "running", "command_id": report.command_id}
        if subagent is not None:
            data["subagent"] = subagent
        output = running_output_text(report)
        if output:
            data["output"] = output
        checks = self._count_check(report.command_id)
        if subagent is None and checks >= _CHECKS_NAMED:
            # Agents checked one command dozens of times, seconds apart.
            delivery = (
                f"You have checked it {checks} times while it runs; checking does not bring its "
                "result sooner. The result arrives as a new message when it exits: end your turn "
                "now unless other work is waiting."
            )
        elif subagent is None:
            delivery = (
                "Its result arrives as a new message when it exits; do not sleep or poll for "
                "that result."
            )
        else:
            delivery = f"Its result goes to the Sub-Agent {subagent}."
        data["next"] = (
            f"The command has run for {duration(report.elapsed_seconds)} and keeps running."
            f"{running_output_note(report)} {delivery} Stop it with "
            f"{stop_call(report.command_id)} if you no longer need it."
        )
        return data

    # stop

    async def _stop(
        self,
        context: ToolContext,
        before: CommandReport,
        subagent: str | None,
        names: ToolNames,
        tree: _Tree,
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
                f" These processes still ran when vBot stopped waiting: {survivors}."
                if survivors
                else " Whether its processes ended is unknown."
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

    def _visible(self, task_id: str, tree: _Tree) -> CommandReport | None:
        """The background command *task_id* when one of the *tree* Sessions owns it."""
        try:
            report = self._commands.report(task_id)
        except CommandNotFoundError:
            return None
        if not report.background or report.owner not in tree.sessions:
            return None
        return report

    def _taken_over(self, task_id: str, action: str, tree: _Tree) -> str | None:
        """The refusal of *task_id* when it works within a takeover, else None.

        The caller can still check the taken-over Sub-Agent itself, but not stop it:
        the user decided that a Session the user took over belongs to the user. The
        refusal names the state and the user as the way on; one that reads as a
        permission problem invites retries with other id spellings.
        """
        if task_id.startswith(SUBAGENT_ID_PREFIX):
            subagent = next((item for item in tree.everyone if item.id == task_id), None)
            if subagent is None or subagent.within_takeover is None:
                return None
            if subagent.within_takeover == task_id:
                if action != "stop":
                    return None
                state = f"{task_id} is a Sub-Agent the user took over and now works in."
            else:
                state = (
                    f"{task_id} works below Sub-Agent {subagent.within_takeover}, whose Session "
                    "the user took over and now works in."
                )
            outcome = "answer"
        elif task_id.startswith(COMMAND_ID_PREFIX):
            try:
                report = self._commands.report(task_id)
            except CommandNotFoundError:
                return None
            subagent = next((item for item in tree.everyone if item.session == report.owner), None)
            if not report.background or subagent is None or subagent.within_takeover is None:
                return None
            if subagent.within_takeover == subagent.id:
                state = (
                    f"{task_id} runs in the Session of Sub-Agent {subagent.id}, which the user "
                    "took over and now works in."
                )
            else:
                state = (
                    f"{task_id} runs in the Session of Sub-Agent {subagent.id}, which works below "
                    f"Sub-Agent {subagent.within_takeover}, whose Session the user took over and "
                    "now works in."
                )
            outcome = "result"
        else:
            return None
        if action == "stop":
            return f"Nothing was stopped: {state} If it needs to stop, tell the user."
        return f"{state} Ask the user if you need its {outcome}."

    def _yours(self, tree: _Tree) -> str:
        """Name the caller's working Sub-Agents and running commands, for an error."""
        running = [
            f"{item.id} ({item.title or item.agent_id})"
            for item in tree.subagents
            if item.status in _WORKING
        ]
        running.extend(
            f"{report.command_id} ({report.label})"
            for report in self._commands.commands(tree.sessions)
            if not report.ended or report.still_running
        )
        if not running:
            return "Nothing of yours is running now."
        entries = ", ".join(running[:_YOURS_SHOWN])
        more = len(running) - _YOURS_SHOWN
        return f"Running now: {entries}{f' and {more} more' if more > 0 else ''}."

    def _log_file(self, command_id: str) -> str | None:
        """The file with the complete output of *command_id*, as the Agent reads paths."""
        try:
            report = self._commands.report(command_id)
        except CommandNotFoundError:
            return None
        return model_path(report.log_path) if report.log_path is not None else None

    def _missing_id(self, action: str, tree: _Tree) -> str:
        if action == "stop":
            problem = (
                "stop needs id, the background command or Sub-Agent to stop; nothing was stopped."
            )
        else:
            problem = f"{action} needs id, the id of a background command or Sub-Agent."
        return f"{problem} {self._yours(tree)}"

    def _not_found(self, task_id: str, action: str, tree: _Tree) -> str:
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


def _subagent_identity(subagent: SubAgentState) -> JsonObject:
    data: JsonObject = {
        "id": subagent.id,
        "type": "subagent",
        "agent_id": subagent.agent_id,
        "session_id": subagent.session.session_id,
    }
    if subagent.session.project_id is not None:
        data["project_id"] = subagent.session.project_id
    return data


def _taken_over_below(subagent: SubAgentState, tree: _Tree) -> str | None:
    """Name the Sub-Agents below *subagent* that the user took over, which its stop left running."""
    parents = {item.id: item.parent_id for item in tree.everyone}

    def below(item_id: str) -> bool:
        seen = {item_id}
        parent_id = parents.get(item_id)
        while parent_id is not None and parent_id not in seen:
            if parent_id == subagent.id:
                return True
            seen.add(parent_id)
            parent_id = parents.get(parent_id)
        return False

    kept = [item.id for item in tree.everyone if item.within_takeover == item.id and below(item.id)]
    if not kept:
        return None
    if len(kept) == 1:
        return (
            f"Not stopped: Sub-Agent {kept[0]}, which the user took over and now works in, "
            "with everything below it."
        )
    named = f"{', '.join(kept[:-1])} and {kept[-1]}"
    return (
        f"Not stopped: Sub-Agents {named}, which the user took over and now works in, "
        "with everything below them."
    )


def _subagent_fields(subagent: SubAgentState, *, with_project: bool = False) -> JsonObject:
    """One Sub-Agent in a list, or with its Project for its status."""
    data: JsonObject = {
        "id": subagent.id,
        "type": "subagent",
        "title": subagent.title,
        "agent_id": subagent.agent_id,
        "session_id": subagent.session.session_id,
    }
    if with_project and subagent.session.project_id is not None:
        data["project_id"] = subagent.session.project_id
    if subagent.parent_id is not None:
        data["parent_id"] = subagent.parent_id
    data["status"] = subagent.status
    if subagent.status == "working" and subagent.last_tool is not None:
        data["last_tool"] = subagent.last_tool
    if subagent.activity_file is not None:
        data["activity_file"] = subagent.activity_file
    return data


def _subagent_next(subagent: SubAgentState, *, can_send: bool) -> str:
    """What the caller can do about the Sub-Agent now; *can_send*: it can call ``subagent``."""
    if subagent.status == _TAKEN_OVER:
        return (
            "The user took over this Sub-Agent by writing in its Session; it works for the "
            "user, and vBot no longer sends you its answers."
        )
    parent_id = subagent.parent_id
    if subagent.status in _WORKING:
        stop = f"To stop it, call {stop_call(subagent.id)}."
        if parent_id is None:
            return (
                "The Sub-Agent is working; its answer arrives as a new message when its turn "
                f"ends, so do not poll for it. {stop}"
            )
        return (
            f"The Sub-Agent is working for your Sub-Agent {parent_id}, which receives its "
            f"answer. {stop}"
        )
    if parent_id is None:
        if not can_send:
            return "The Sub-Agent is idle."
        return (
            "The Sub-Agent is idle. To give it more work, send it a message with subagent "
            "action send."
        )
    return f"The Sub-Agent is idle; only your Sub-Agent {parent_id} can send it messages."


def _command_row(report: CommandReport, subagent: str | None) -> JsonObject:
    """One background command in a list."""
    row: JsonObject = {"id": report.command_id, "type": "command", "command": report.label}
    if subagent is not None:
        row["subagent"] = subagent
    if not report.ended:
        row["status"] = "running"
    elif report.not_run is not None:
        # The shell ran nothing of it; status says why.
        row["status"] = "not_run"
    elif report.stop_reason is not None:
        # Its exit code is vBot's kill, not the command's.
        row["status"] = "stopped"
    else:
        row["status"] = "exited"
        if report.exit_code is not None:
            row["exit_code"] = report.exit_code
    if report.still_running:
        # The shell exited, but processes it started run on until stopped.
        row["still_running"] = [process_text(process) for process in report.still_running]
    row["runtime"] = duration(report.elapsed_seconds)
    if report.log_path is not None and report.not_run is None:
        row["log"] = model_path(report.log_path)
    return row


def _ended(report: CommandReport, subagent: str | None, names: ToolNames) -> JsonObject:
    """An ended command as the shell reports it, with its id and its complete output's file.

    For a Sub-Agent's command, ``stopped_because`` speaks to the caller above it.
    """
    shell = ended_result(report, names, subagent=subagent)
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


def _message_refusal(subagent_id: str | None, content: str | None, *, can_send: bool) -> str:
    """*subagent_id*: the Sub-Agent the message was for, None when no id names one."""
    refusal = f"{BACKGROUND_TASKS_TOOL_NAME} sends no messages; nothing was sent."
    if not can_send:
        return f"{refusal} Its actions are list, status and stop."
    if subagent_id is None:
        return (
            f"{refusal} To message a Sub-Agent, call {SUBAGENT_TOOL_NAME} with action send, the "
            "Sub-Agent's id and your message as content."
        )
    refusal = f"{refusal} To message Sub-Agent {subagent_id}, call {SUBAGENT_TOOL_NAME}"
    if content is None:
        return f"{refusal} with action send, this id and your message as content."
    call = {"action": "send", "id": subagent_id, "content": content}
    return f"{refusal} {json.dumps(call, ensure_ascii=False)}."


def _no_effect_notes(
    fields: Mapping[str, str],
    *,
    shown: Literal["list", "output", "subagent"],
    log: str | None = None,
) -> list[str]:
    """Notes for fields that asked to wait for a task or to filter its output.

    *fields* maps each key as the call sent it to its kind (``NO_EFFECT_FIELDS``);
    *shown* is what the result shows, *log* the file with a command's complete
    output or a Sub-Agent's activity, which the Agent can search instead.
    """
    notes: list[str] = []
    waits = [key for key, kind in fields.items() if kind == NO_EFFECT_WAIT]
    if waits:
        named = " and ".join([", ".join(waits[:-1]), waits[-1]] if len(waits) > 1 else waits)
        verb = "has" if len(waits) == 1 else "have"
        notes.append(f"{named} {verb} no effect: {BACKGROUND_TASKS_TOOL_NAME} never waits.")
    for key, kind in fields.items():
        if kind == NO_EFFECT_WAIT:
            continue
        if shown == "list":
            notes.append(f"{key} has no effect: list shows every task.")
        elif shown == "subagent":
            where = f" The Sub-Agent's activity log is {log}." if log is not None else ""
            notes.append(f"{key} has no effect.{where}")
        elif log is not None:
            notes.append(
                f"{key} has no effect: the output is shown unfiltered. vBot kept this "
                f"command's complete raw output in {log} for you to read."
            )
        else:
            notes.append(f"{key} has no effect: the output is shown unfiltered.")
    return notes


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


def background_tasks_detail_blocks(
    arguments: JsonObject, result: JsonObject | None
) -> list[JsonObject]:
    """Show the user the listed tasks, a Sub-Agent's state or stop, or a command's output.

    ``subagent`` shows the calls it ran as ``background_tasks`` the same way.
    """
    data = result.get("data") if isinstance(result, dict) and result.get("ok") is True else None
    if not isinstance(data, dict):
        return []
    tasks = data.get("tasks")
    if isinstance(tasks, list):
        rows = [row for row in tasks if isinstance(row, dict)]
        if not rows:
            return [display_notice("info", "No background tasks.")]
        return [display_results([_shown_task(row) for row in rows])]
    if data.get("type") == "subagent":
        try:
            normalized = normalize_background_tasks_arguments(arguments)
        except ValueError:
            normalized = arguments
        if isinstance(normalized, dict) and normalized.get("action") == "stop":
            stopped = data.get("status") == "stopped"
            return [
                display_notice(
                    "info",
                    "The Sub-Agent was stopped."
                    if stopped
                    else "The Sub-Agent was idle; nothing needed stopping.",
                )
            ]
        return [display_results([_shown_task(data)])]
    blocks = [display_text("output", source="result", path=("data", "output"))]
    if data.get("status") == "running":
        blocks.append(display_notice("info", "The command keeps running in the background."))
    if data.get("status") == "not_run":
        blocks.append(display_notice("warning", "The command did not run."))
    exit_code = data.get("exit_code")
    if (
        data.get("status") == "exited"
        and isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
        and exit_code != 0
    ):
        blocks.append(display_notice("warning", f"The command exited with code {exit_code}."))
    return blocks


def _shown_task(row: JsonObject) -> JsonObject:
    """A command by its label or a Sub-Agent by its title, with id, status and what it runs."""
    return {
        "title": str(row.get("command") or row.get("title") or row.get("id")),
        "meta": " · ".join(
            str(row[key])
            for key in ("id", "status", "runtime", "last_tool")
            if row.get(key) is not None
        ),
    }


def register_background_tasks_tool(
    registry: ToolRegistry,
    command_manager: CommandManager,
    *,
    subagents: SubAgentTree | None = None,
) -> BackgroundTasksTool:
    """Register ``background_tasks``, active while the shell Tool or ``subagent`` is."""
    tool = BackgroundTasksTool(command_manager, subagents)
    registry.register(
        BACKGROUND_TASKS_TOOL_NAME,
        BACKGROUND_TASKS_TOOL_DESCRIPTION,
        BACKGROUND_TASKS_TOOL_PARAMETERS,
        tool,
        catalog_visible=False,
        family="execution",
        activation=TOOL_ACTIVATION_FOLLOWS,
        activation_source=(BASH_TOOL_NAME, SUBAGENT_TOOL_NAME),
        open_input_schema=True,
        unadvertised_parameters=BACKGROUND_TASKS_UNADVERTISED_PARAMETERS,
        argument_normalizer=normalize_background_tasks_arguments,
        result_schema={"type": "object"},
        display=ToolDisplay(
            parts_builder=_display_parts, detail_builder=background_tasks_detail_blocks
        ),
    )
    return tool


__all__ = [
    "BACKGROUND_TASKS_TOOL_DESCRIPTION",
    "BACKGROUND_TASKS_TOOL_NAME",
    "BACKGROUND_TASKS_TOOL_PARAMETERS",
    "BackgroundTasksTool",
    "background_tasks_detail_blocks",
    "register_background_tasks_tool",
]
