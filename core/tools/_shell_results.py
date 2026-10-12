"""How shell commands are described to Agents: results, deliveries and their statuses.

The shell Tool and ``background_tasks`` describe a ``CommandReport`` the same
way: the ended result with its output, exit code and the facts the exit code
hides, a running command's newest output, why vBot stopped it, the processes it
left running and the exact call that stops them. A command that outlives its
call delivers its result as a new message (``format_command_delivery``);
``background_command_statuses`` folds stored results and deliveries into the
status of each background command.

Texts name another Tool only when *names* (``ToolNames``) says so; a delivery
outlives its call and gets a snapshot of the Tools named at the command's start.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Sequence
from typing import Any

from core.tools._background_tasks_arguments import BACKGROUND_TASKS_TOOL_NAME
from core.tools.command_manager import CommandReport, StopReason
from core.tools.model_names import BASH_TOOL_NAME
from core.tools.process_tree import RunningProcess
from core.tools.shell_hints import HINT_TOOL_NAMES, annotate_failure
from core.tools.tools import JsonObject, ToolContext
from core.utils.paths import model_path

SHELL_TOOL_NAME = BASH_TOOL_NAME
TERMINAL_TOOL_NAME = "terminal"
# Whether a text may name the Tool of a registry name.
ToolNames = Callable[[str], bool]
# Their exact calls work without loading them first, so they are named whenever
# the Agent can call them, also as On-demand Tools it has not loaded.
_CALLABLE_TOOLS = frozenset({BACKGROUND_TASKS_TOOL_NAME, TERMINAL_TOOL_NAME})
_WINDOWS = sys.platform == "win32"

# The output text of one result: head and tail around an omitted middle.
SHELL_OUTPUT_HEAD_CHARS = 4_000
SHELL_OUTPUT_TAIL_CHARS = 8_000
SHELL_OUTPUT_LINE_CHARS = 2_000
# Output over the budget by at most this much is shown whole: a marker for a few
# lines saves less than the read of the log file it invites.
SHELL_OUTPUT_OMIT_MIN_CHARS = 1_000
# A running command's result shows only its newest output; its log file has all of it.
SHELL_RUNNING_OUTPUT_LINES = 20
SHELL_RUNNING_OUTPUT_CHARS = 4_000
# A delivery shows the command up to this many characters.
_DELIVERY_COMMAND_CHARS = 1_000
# A pipeline stage that passes nothing on before its input ends: the last lines,
# a sort, a count or a group, or one string of everything. Output piped into it
# appears only once the command before the pipe has finished.
_HOLDING_STAGE = re.compile(
    r"(?<!\|)\|(?!\|)[ \t]*(?P<stage>"
    r"(?:Select-Object|select)(?=[ \t])[^|;\n]*?[ \t]-l(?:a(?:st?)?)?(?=[ \t]|$)[^|;\n]*"
    r"|(?:tail|sort|tac|wc|Sort-Object|Group-Object|group|Measure-Object|measure)"
    r"(?=[ \t);}]|$)[^|;\n]*"
    r"|Out-String(?=[ \t);}]|$)(?![^|;\n]*-Stream)[^|;\n]*)",
    re.IGNORECASE | re.MULTILINE,
)


# Tools a text may name


def named_tools(context: ToolContext) -> ToolNames:
    """The Tools a text of this call may name.

    ``background_tasks`` and ``terminal`` are named when the Agent can call
    them (``ToolContext.can_call``), every other Tool when it is offered.
    """

    def names(tool: str) -> bool:
        if tool in _CALLABLE_TOOLS:
            return context.can_call(tool)
        return context.offers(tool)

    return names


def delivery_names(names: ToolNames) -> ToolNames:
    """A snapshot of *names* for a delivery, which outlives the call."""
    return frozenset(
        tool for tool in (*HINT_TOOL_NAMES, BACKGROUND_TASKS_TOOL_NAME) if names(tool)
    ).__contains__


def _names_nothing(_tool: str) -> bool:
    return False


# Fragments


def duration(seconds: float) -> str:
    """A duration in words: ``45 seconds``, ``14 minutes 5 seconds``, ``1 hour 2 minutes``."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [
        f"{count} {unit}{'' if count == 1 else 's'}"
        for count, unit in ((hours, "hour"), (minutes, "minute"))
        if count
    ]
    if secs or not parts:
        parts.append(f"{secs} second{'' if secs == 1 else 's'}")
    return " ".join(parts)


def log_sentence(report: CommandReport) -> str:
    """Where a running command's complete output is, as a sentence after a space."""
    if report.log_path is None:
        return ""
    # A running result shows only the newest output; Agents did not know what a
    # bare log_file field held.
    if report.transcript.processing_failed:
        return f" The output vBot processed is in {model_path(report.log_path)}."
    return f" The command's complete output is written live to {model_path(report.log_path)}."


def output_location(report: CommandReport, output: str) -> str | None:
    """The sentence naming the file with the complete output, unless *output*'s
    markers name it already; None without a log file."""
    if report.log_path is None:
        return None
    path = model_path(report.log_path)
    noun = _log_content(report)
    if f"the {noun} is in {path}" in output:
        return None
    return f"The {noun} is in {path}."


def stop_call(command_id: str) -> str:
    """The complete ``background_tasks`` call that stops a command."""
    arguments = {"action": "stop", "id": command_id}
    return f"{BACKGROUND_TASKS_TOOL_NAME} {json.dumps(arguments)}"


def stop_with(report: CommandReport, names: ToolNames) -> str:
    """``with <the call that stops the command>``, as the end of a sentence.

    Without ``background_tasks``, the host shell's own command stops the
    command's processes by its root process.
    """
    if names(BACKGROUND_TASKS_TOOL_NAME):
        return f"with {stop_call(report.command_id)}"
    command = f"taskkill /PID {report.pid} /T /F" if _WINDOWS else f"kill -- -{report.pid}"
    return f"with the shell command {command}"


def process_text(process: RunningProcess) -> str:
    return f"{process.name} (pid {process.pid})"


def processes_text(processes: Sequence[RunningProcess]) -> str:
    return ", ".join(process_text(process) for process in processes)


def leftovers_stop(report: CommandReport, names: ToolNames) -> str:
    """The sentence that stops the processes an ended command left running.

    Without ``background_tasks``, the host shell stops them by their ids:
    the shell that started them has exited.
    """
    if names(BACKGROUND_TASKS_TOOL_NAME):
        call = stop_call(report.command_id)
    else:
        pids = [str(process.pid) for process in report.still_running]
        call = (
            f"the shell command Stop-Process -Id {','.join(pids)}"
            if _WINDOWS
            else f"the shell command kill {' '.join(pids)}"
        )
    return f"Stop them with {call} when they are no longer needed."


_STOP_TEXTS: dict[StopReason, str] = {
    # Read by the Session that ran it; _STOP_TEXTS_ABOVE speaks to Agents above it.
    "agent": "you stopped it.",
    "parent": "your Parent Agent or an Agent above it stopped it.",
    "user": "the user stopped it.",
    "run_cancelled": "the turn that started it was cancelled.",
    "shutdown": "vBot shut down.",
    # Read by the Session that ran it and by Agents above it.
    "scope": "it was stopped together with all other work of the Session that ran it.",
}
# Read by an Agent above the Sub-Agent whose Session ran the command. The record
# keeps only "parent", not which of the Agents above that Sub-Agent stopped it.
_STOP_TEXTS_ABOVE: dict[StopReason, str] = {
    "agent": "Sub-Agent {subagent} stopped it.",
    "parent": "an Agent above Sub-Agent {subagent} stopped it.",
}


def stopped_because(
    report: CommandReport, *, delivery: bool = False, subagent: str | None = None
) -> str:
    """Why vBot stopped the command; a result says when the user stopped it.

    *subagent* is the Sub-Agent whose Session ran the command when an Agent above
    it reads the text; without it, the text speaks to the Session that ran it.
    """
    reason = report.stop_reason or "agent"
    if reason == "user" and not delivery:
        return f"the user stopped it after {duration(report.elapsed_seconds)}."
    if subagent is not None and reason in _STOP_TEXTS_ABOVE:
        return _STOP_TEXTS_ABOVE[reason].format(subagent=subagent)
    return _STOP_TEXTS[reason]


# Results


def ended_result(
    report: CommandReport, names: ToolNames, *, subagent: str | None = None
) -> JsonObject:
    """The result data of an ended command, fields in order and omitted when empty.

    ``status`` (``exited`` or ``stopped``), ``exit_code`` (of a command that
    exited by itself), ``stopped_because``, ``output``, ``failed_programs``,
    ``hint``, and for processes it left running ``command_id``,
    ``still_running`` and ``next``. *subagent* is the Sub-Agent whose Session
    ran the command when an Agent above it reads the result.
    """
    if report.stop_reason is not None:
        # The exit code of a stopped command is vBot's kill, not the program's.
        data: JsonObject = {
            "status": "stopped",
            "stopped_because": stopped_because(report, subagent=subagent),
        }
    else:
        data = {"status": "exited"}
        if report.exit_code is not None:
            data["exit_code"] = report.exit_code
    output = command_output_text(report)
    if output:
        data["output"] = output
    failed = failed_programs(report)
    if failed:
        data["failed_programs"] = list(failed)
    hint = failure_hint(report, output, names)
    if hint is not None:
        data["hint"] = hint
    if report.still_running:
        data["command_id"] = report.command_id
        data["still_running"] = [process_text(process) for process in report.still_running]
        data["next"] = (
            "The shell exited, but processes the command started still run: "
            f"{processes_text(report.still_running)}. {leftovers_stop(report, names)}"
        )
    return data


def failed_programs(report: CommandReport) -> tuple[str, ...]:
    """The failed programs that tell more than the exit code.

    A single failed program whose code is the exit code of a command that
    exited by itself repeats it; a stopped command shows no exit code. Each
    entry reads ``<name> exited with code <code>``, with the hex form after
    large codes.
    """
    exits = report.failed_programs
    if len(exits) == 1 and report.stop_reason is None:
        code = exits[0].rpartition(" exited with code ")[2].split(" ", 1)[0]
        if code == str(report.exit_code):
            return ()
    return exits


def failure_hint(report: CommandReport, output: str, names: ToolNames) -> str | None:
    """A hint for a command that failed on its own; a stop by vBot explains itself."""
    if report.stop_reason is not None:
        return None
    return annotate_failure(
        report.command, report.exit_code, output, workdir=report.workdir, offers=names
    )


# Output


def command_output_text(report: CommandReport) -> str:
    """The output for a result: head and tail within the character budget.

    A marker names the file with the complete output where anything is left out.
    """
    transcript = report.transcript
    where = _full_output(report)
    head = [_shortened(line, where) for line in transcript.head]
    tail = [_shortened(line, where) for line in transcript.tail]
    omitted = transcript.omitted_lines
    if not omitted:
        # One run of lines: where the transcript's head ends means nothing here.
        head, tail = [], head + tail
    head, tail, dropped = _head_and_tail(head, tail)
    omitted += dropped
    lines = list(head)
    if omitted:
        noun = "line" if omitted == 1 else "lines"
        lines.append(f"[... {omitted:,} {noun} omitted{where} ...]")
    lines.extend(tail)
    unprocessed = _unprocessed(report)
    if unprocessed is not None:
        lines.append(unprocessed)
    stage = _holding_stage(report.command) if report.stop_reason is not None else None
    if stage is not None:
        lines.append(
            f"[Output piped into {stage} appears only once the command before the pipe has "
            "finished; to see output as it arrives, run the command without that pipe.]"
        )
    return "\n".join(lines)


def running_output_text(report: CommandReport) -> str:
    """The newest output of a running command: whole lines within the running limits."""
    transcript = report.transcript
    lines = [*transcript.head, *transcript.tail]
    where = _full_output(report)
    newest = [_shortened(line, where) for line in lines[-SHELL_RUNNING_OUTPUT_LINES:]]
    kept, _ = _within(newest, SHELL_RUNNING_OUTPUT_CHARS, keep="end")
    omitted = transcript.total_lines - len(kept)
    if omitted:
        noun = "line" if omitted == 1 else "lines"
        kept.insert(0, f"[... {omitted:,} earlier {noun} omitted{where} ...]")
    unprocessed = _unprocessed(report)
    if unprocessed is not None:
        kept.append(unprocessed)
    stage = _holding_stage(report.command)
    if stage is not None:
        log = " and in the log file" if report.log_path is not None else ""
        kept.append(
            f"[Output piped into {stage} appears here{log} only once the command before the "
            "pipe has finished.]"
        )
    return "\n".join(kept)


def _unprocessed(report: CommandReport) -> str | None:
    """The marker after the last line when vBot could not process the rest."""
    if not report.transcript.processing_failed:
        return None
    log = " and from the log file" if report.log_path is not None else ""
    return f"[vBot could not process the output after this point; it is missing here{log}.]"


def _head_and_tail(head: list[str], tail: list[str]) -> tuple[list[str], list[str], int]:
    """*head* and *tail* within the output budget, and the number of lines left out.

    *head* lines are kept from the start, *tail* lines from the end; with an
    empty *head*, both ends of *tail* are kept. Each end gets at least its own
    budget and the room the other end does not need. Output is left out only
    where that saves more than ``SHELL_OUTPUT_OMIT_MIN_CHARS``.
    """
    budget = SHELL_OUTPUT_HEAD_CHARS + SHELL_OUTPUT_TAIL_CHARS
    if _chars(head) + _chars(tail) <= budget + SHELL_OUTPUT_OMIT_MIN_CHARS:
        return head, tail, 0
    if not head:
        kept_head, _ = _within(tail, SHELL_OUTPUT_HEAD_CHARS, keep="start")
        rest = tail[len(kept_head) :]
        kept_tail, _ = _within(rest, budget - _chars(kept_head), keep="end")
        return kept_head, kept_tail, len(rest) - len(kept_tail)
    head_budget = max(SHELL_OUTPUT_HEAD_CHARS, budget - _chars(tail))
    kept_head, dropped_head = _within(head, head_budget, keep="start")
    kept_tail, dropped_tail = _within(tail, budget - _chars(kept_head), keep="end")
    return kept_head, kept_tail, dropped_head + dropped_tail


def _chars(lines: list[str]) -> int:
    """The characters *lines* take in the output, line breaks included."""
    return sum(len(line) + 1 for line in lines)


def _holding_stage(command: str) -> str | None:
    """The last pipeline stage of *command* that holds its input until it ends.

    Agents piped test runs into ``Select-Object -Last 25``: running in the
    background or stopped, the result showed no output, and they stopped
    commands that worked. The last such stage, since commands run their long
    step last, after quick preparations.
    """
    stages = [match["stage"] for match in _HOLDING_STAGE.finditer(command)]
    if not stages:
        return None
    stage = stages[-1].split(")", 1)[0].split("}", 1)[0].strip()
    return stage if len(stage) <= 60 else f"{stage[:57]}..."


def _full_output(report: CommandReport) -> str:
    """Where the omitted output is, as a clause of a marker."""
    path = report.log_path
    if path is None:
        return ""
    return f"; the {_log_content(report)} is in {model_path(path)}"


def _log_content(report: CommandReport) -> str:
    """What the log file holds: everything, or only what came before a cleaning failure."""
    return "output vBot processed" if report.transcript.processing_failed else "complete output"


def _shortened(line: str, where: str) -> str:
    """*line* cut to the line budget; *where* names the complete output."""
    if len(line) <= SHELL_OUTPUT_LINE_CHARS:
        return line
    rest = len(line) - SHELL_OUTPUT_LINE_CHARS
    return f"{line[:SHELL_OUTPUT_LINE_CHARS]}[... {rest:,} more characters{where}]"


def _within(lines: list[str], budget: int, *, keep: str) -> tuple[list[str], int]:
    """Keep whole lines from one end while they fit *budget* characters."""
    ordered = lines if keep == "start" else list(reversed(lines))
    kept: list[str] = []
    used = 0
    for line in ordered:
        if used + len(line) + 1 > budget and kept:
            break
        kept.append(line)
        used += len(line) + 1
    dropped = len(lines) - len(kept)
    return (kept if keep == "start" else list(reversed(kept))), dropped


# Delivery


def format_command_delivery(report: CommandReport, *, names: ToolNames = _names_nothing) -> str:
    """The message that delivers a background command's result when it ends.

    *names* tells which Tools the message can name.
    """
    ran = duration(report.elapsed_seconds)
    if report.stop_reason is not None:
        status = f"was stopped after {ran}: {stopped_because(report, delivery=True)}"
    elif report.exit_code is None:
        status = f"exited after {ran}; its exit code is unknown."
    else:
        status = f"exited with code {report.exit_code} after {ran}."
    lines = [f"{COMMAND_STATUS_NOTE_MARKER}{report.command_id} ({report.label}) {status}"]
    if report.description:
        # The label names the purpose; the Agent needs the exact command too.
        lines.append(f"Command: {_capped(report.command, _DELIVERY_COMMAND_CHARS)}")
    output = command_output_text(report)
    hint = failure_hint(report, output, names)
    if hint is not None:
        lines.append(f"Hint: {hint}")
    failed = failed_programs(report)
    if failed:
        lines.append(f"Failed programs: {'; '.join(failed)}.")
    if report.still_running:
        lines.append(
            f"Processes it started still run: {processes_text(report.still_running)}. "
            f"{leftovers_stop(report, names)}"
        )
    lines.append("Output:" if output else "Output: (none)")
    if output:
        lines.append(output)
    location = output_location(report, output)
    if location is not None:
        lines.append(location)
    return "\n".join(lines)


def _capped(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


# Background command statuses

# The durable records the status fold reads: results of these Tools, and the
# delivered results of background commands, which start with this text.
COMMAND_STATUS_TOOL_NAMES = (SHELL_TOOL_NAME, BACKGROUND_TASKS_TOOL_NAME)
COMMAND_STATUS_NOTE_MARKER = "The background command "
_DELIVERY_STATUS = re.compile(
    r"The background command (?P<command>cmd_[a-z0-9]+) \(.*?\) "
    r"(?:exited with code (?P<code>-?\d+) after [^.]*\."
    r"|exited after [^;]*; its exit code is unknown\."
    r"|(?P<stopped>was stopped) after [^:]*:)"
)


def background_command_statuses(records: Sequence[Any]) -> JsonObject:
    """Fold durable command results and deliveries into the statuses of background commands.

    Maps each command id to ``running``, ``completed`` (exit code 0),
    ``failed`` (another or an unknown exit code) or ``stopped``. Folding a later
    range of records onto an earlier fold equals folding both ranges at once.
    """
    statuses: JsonObject = {}
    for record in records:
        role = getattr(record, "role", None)
        content = getattr(record, "content", None)
        if not isinstance(content, str):
            continue
        if role == "tool" and getattr(record, "name", None) in COMMAND_STATUS_TOOL_NAMES:
            _fold_result(statuses, content)
        elif role == "note":
            for match in _DELIVERY_STATUS.finditer(content):
                statuses[match["command"]] = _delivered_status(match)
    return statuses


def _delivered_status(match: re.Match[str]) -> str:
    if match["stopped"]:
        return "stopped"
    return "completed" if match["code"] == "0" else "failed"


def _fold_result(statuses: JsonObject, content: str) -> None:
    """A result that names a background command: running, or the end it showed."""
    try:
        envelope = json.loads(content)
    except json.JSONDecodeError:
        return
    data = envelope.get("data") if isinstance(envelope, dict) and envelope.get("ok") else None
    if not isinstance(data, dict):
        return
    command_id = data.get("command_id")
    if not isinstance(command_id, str) or not command_id:
        return
    status = data.get("status")
    if status == "running":
        # A result taken before the end never overrides the end.
        statuses.setdefault(command_id, "running")
    elif status == "stopped":
        statuses[command_id] = "stopped"
    elif status == "exited":
        statuses[command_id] = "completed" if data.get("exit_code") == 0 else "failed"


__all__ = [
    "BACKGROUND_TASKS_TOOL_NAME",
    "COMMAND_STATUS_NOTE_MARKER",
    "COMMAND_STATUS_TOOL_NAMES",
    "SHELL_TOOL_NAME",
    "TERMINAL_TOOL_NAME",
    "ToolNames",
    "background_command_statuses",
    "command_output_text",
    "delivery_names",
    "duration",
    "ended_result",
    "failed_programs",
    "failure_hint",
    "format_command_delivery",
    "stop_call",
    "leftovers_stop",
    "log_sentence",
    "named_tools",
    "output_location",
    "process_text",
    "processes_text",
    "running_output_text",
    "stop_with",
    "stopped_because",
]
