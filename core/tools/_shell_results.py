"""How shell commands are described to Agents: results, deliveries and their statuses.

The shell Tool and ``background_tasks`` describe a ``CommandReport`` the same
way: the ended result with its output, exit code and the facts the exit code
hides, a running command's newest output, why vBot stopped it, why the shell ran
nothing of it, the processes it left running and the exact call that stops them.
A command that outlives its call delivers its result as a new message
(``format_command_delivery``); ``background_command_statuses`` folds stored
results and deliveries into the status of each background command.

Texts name another Tool only when *names* (``ToolNames``) says so; a delivery
outlives its call and gets a snapshot of the Tools named at the command's start.
"""

from __future__ import annotations

import errno
import json
import re
import sys
from collections.abc import Callable, Sequence
from typing import Any

from core.tools._background_tasks_arguments import BACKGROUND_TASKS_TOOL_NAME
from core.tools.command_manager import (
    COMMAND_HEAD_CHARS,
    COMMAND_TAIL_CHARS,
    CommandCapacityError,
    CommandLaunchError,
    CommandNotStartedError,
    CommandReport,
    CommandTranscript,
    StopReason,
)
from core.tools.file_state import os_error_reason
from core.tools.model_names import BASH_TOOL_NAME, SHELL_MODEL_NAME
from core.tools.process_tree import RunningProcess
from core.tools.shell_hints import HINT_TOOL_NAMES, annotate_failure
from core.tools.tools import (
    JsonObject,
    ToolContext,
    tool_failure,
)
from core.utils.file_status import is_dir_strict
from core.utils.logging import get_logger
from core.utils.paths import model_path

from ._shell_arguments import _missing_workdir, _ShellCall

_LOGGER = get_logger("tools.shell")

SHELL_TOOL_NAME = BASH_TOOL_NAME
TERMINAL_TOOL_NAME = "terminal"
# Whether a text may name the Tool of a registry name.
ToolNames = Callable[[str], bool]
# Their exact calls work without loading them first, so they are named whenever
# the Agent can call them, also as On-demand Tools it has not loaded.
_CALLABLE_TOOLS = frozenset({BACKGROUND_TASKS_TOOL_NAME, TERMINAL_TOOL_NAME})
_WINDOWS = sys.platform == "win32"

# An ended command's output is shown whole up to SHELL_OUTPUT_CHARS characters.
# Longer output shows its first lines within SHELL_OUTPUT_HEAD_CHARS and its last
# lines within the rest, around a marker; the transcript keeps what this needs.
SHELL_OUTPUT_CHARS = COMMAND_TAIL_CHARS
SHELL_OUTPUT_HEAD_CHARS = COMMAND_HEAD_CHARS
# A running command's result shows only its newest output; its log file has all of it.
SHELL_RUNNING_OUTPUT_LINES = 20
SHELL_RUNNING_OUTPUT_CHARS = 4_000
# A delivery shows the command up to this many characters.
_DELIVERY_COMMAND_CHARS = 1_000
RUN_AGAIN = "Run the command again; if it fails the same way, tell the user."
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


# A running command silent this long says so: a command stuck after its last
# line read like one making progress, and Agents checked it again and again
# (Swarm run 2026-10-20). Builds and test runs print at least every few
# minutes; a server or watcher is quiet by nature, which the fact alone leaves
# to the Agent's judgment.
COMMAND_SILENCE_NOTED_SECONDS = 300.0


def silence_note(report: CommandReport) -> str:
    """How long a running command has printed nothing, as a sentence after a
    space, from ``COMMAND_SILENCE_NOTED_SECONDS`` on; empty before, after the end
    and behind a stage that holds the output, which explains the silence."""
    silent = report.silent_seconds
    if (
        report.ended
        or silent < COMMAND_SILENCE_NOTED_SECONDS
        or holding_stage(report.command) is not None
    ):
        return ""
    minutes = int(silent // 60)
    if report.transcript.total_lines:
        return f" It has printed no new output for {minutes} minutes."
    return f" It has printed nothing for {minutes} minutes."


def running_output_note(report: CommandReport) -> str:
    """Where a running command's complete output is, and when output piped into a
    stage that holds it appears, as sentences after a space."""
    note = ""
    stage = holding_stage(report.command)
    lost = lost_output(report)
    if lost is not None:
        note = (
            f" {lost}. The command is still running. Stop and repeat it only if "
            "interrupting it and repeating its effects are safe."
        )
    elif report.log_path is not None:
        # A running result shows only the newest output; Agents did not know what a
        # bare log_file field held. A stage's output reaches the file only at its end.
        path = model_path(report.log_path)
        note = (
            f" The command's raw output is written to {path}."
            if stage is not None
            else f" The command's complete raw output is written live to {path}."
        )
    if stage is not None:
        note += (
            f" Output piped into {stage} appears only once the command before the pipe has "
            "finished."
        )
    return note


def output_location(report: CommandReport, output: str) -> str | None:
    """The sentence naming the file with the complete output, or saying how much
    of it the file holds, unless *output*'s markers say it already; None without
    a log file."""
    kept = _kept_output(report)
    if kept is None or kept in output:
        return None
    return f"{kept}."


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


_RESTART_OUTCOME = "The command's outcome is unknown; inspect its effects before repeating it."
_STOP_TEXTS: dict[StopReason, str] = {
    # Read by the Session that ran it; _STOP_TEXTS_ABOVE speaks to Agents above it.
    "agent": "you stopped it.",
    "parent": "your Parent Agent or an Agent above it stopped it.",
    "user": "the user stopped it.",
    "run_cancelled": "the turn that started it was cancelled.",
    "shutdown": "vBot shut down.",
    "server_restart": f"vBot stopped unexpectedly. {_RESTART_OUTCOME}",
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
    if reason == "server_restart" and not delivery:
        ran = _restart_minimum(report)
        if ran is not None:
            # The output alone does not show how long the command ran before the stop.
            return (
                f"vBot stopped unexpectedly at least {ran} after the command started. "
                f"{_RESTART_OUTCOME}"
            )
    if subagent is not None and reason in _STOP_TEXTS_ABOVE:
        return _STOP_TEXTS_ABOVE[reason].format(subagent=subagent)
    return _STOP_TEXTS[reason]


def _restart_minimum(report: CommandReport) -> str | None:
    """How long a command that vBot's unexpected stop ended ran at least, in words;
    None below a second, when nothing showed it running longer.

    Restart recovery measures it from the command's start to the last write of
    its log file or its last saved state, whichever is later.
    """
    if report.elapsed_seconds < 1:
        return None
    return duration(report.elapsed_seconds)


def not_run_problem(report: CommandReport) -> tuple[str, bool] | None:
    """Why the shell ran nothing of an ended command and what to do, and whether a
    retry can help; None when the command ran.

    Only the shell's own report counts (``CommandReport.not_run``): an exit code
    alone, such as a reserved one, can be the command's.
    """
    if report.not_run == "request_unreadable":
        return f"vBot could not pass the command to the shell. {RUN_AGAIN}", True
    if report.not_run == "language_mode_unavailable":
        return (
            "PowerShell's language mode prevents vBot from starting the command. Tell the user.",
            False,
        )
    if report.not_run != "workdir_unavailable":
        return None
    path = model_path(report.workdir)
    try:
        directory = is_dir_strict(report.workdir)
    except OSError:
        directory = True
    if not directory:
        # The directory was removed after the call was checked.
        return (
            f"the workdir {path} is no longer an existing directory. Pass an existing "
            "directory as workdir.",
            False,
        )
    return (
        f"vBot could not enter the workdir {path}. If the command can run in another "
        "directory, pass that directory as workdir; otherwise tell the user.",
        False,
    )


# Results


def ended_result(
    report: CommandReport, names: ToolNames, *, subagent: str | None = None
) -> JsonObject:
    """The result data of an ended command, fields in order and omitted when empty.

    ``status`` (``exited``, ``stopped`` or ``not_run``), ``exit_code`` (of a
    command that exited by itself), its optional ``exit_code_hex``,
    ``stopped_because``, ``not_run_because``,
    ``output``, ``failed_programs``, ``hint``, and for processes it left running
    ``command_id``, ``still_running`` and ``next``. *subagent* is the Sub-Agent
    whose Session ran the command when an Agent above it reads the result.
    """
    not_run = not_run_problem(report)
    if not_run is not None:
        # The shell's exit code is its own and nothing of the command ran.
        return {"status": "not_run", "not_run_because": not_run[0]}
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
            if hexadecimal := exit_code_hex(report.exit_code):
                data["exit_code_hex"] = hexadecimal
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
        location = output_location(report, output)
        if location is not None:
            data["next"] += f" {location}"
    return data


def exit_code_hex(code: int) -> str | None:
    """The diagnostic spelling of a high-bit Windows process exit value."""
    return f"0x{code:08X}" if 0x80000000 <= code <= 0xFFFFFFFF else None


def exit_code_text(code: int) -> str:
    hexadecimal = exit_code_hex(code)
    return f"{code} ({hexadecimal})" if hexadecimal else str(code)


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
    """A hint for a command that failed or did not run as written.

    A stop by vBot explains itself; its hint only says why output piped into
    a stage that holds it is missing. The line count is the command's own,
    since a long *output* keeps only its first and last lines.
    """
    if report.stop_reason is not None:
        stage = holding_stage(report.command)
        if stage is None:
            return None
        return (
            f"{stage} held the output piped into it until the command before the pipe "
            "finished, so the stop lost that output; to see output as it arrives, run the "
            "command without that pipe."
        )
    hint = annotate_failure(
        report.command,
        report.exit_code,
        output,
        workdir=report.workdir,
        offers=names,
        failed_programs=report.failed_programs,
        output_lines=report.transcript.total_lines,
    )
    if hint is None and report.exit_code == 0 and report.failed_programs:
        # Agents read exit code 0 as success although a test script before
        # the last statement had failed.
        hint = (
            "Exit code 0 comes from the last statement only: the failed programs exited with "
            "an error before it, and the command went on after them."
        )
    return hint


# Output


def command_output_text(report: CommandReport) -> str:
    """The output for a result: whole up to ``SHELL_OUTPUT_CHARS`` characters.

    Longer output keeps its first lines within ``SHELL_OUTPUT_HEAD_CHARS`` and
    its last lines within the rest, with one marker line between them that says
    what was left out and names the file with all of it. A line longer than its
    part's room is cut inside it: the head keeps its start, the tail its end.
    """
    transcript = report.transcript
    whole = _whole_lines(transcript)
    if whole is not None and _text_chars(whole) <= SHELL_OUTPUT_CHARS:
        return "\n".join(whole)
    head = list(transcript.head)
    # The marker stands between head and tail, so no line break counts there.
    room = SHELL_OUTPUT_CHARS - _text_chars(head)
    tail, tail_cut = _last_lines(transcript.tail, transcript.tail_cut, room)
    if transcript.head_cut and transcript.total_lines == 1:
        # One line: the head has its start, the tail its end.
        omitted, cut = 0, tail_cut - len(head[0])
    else:
        omitted = transcript.total_lines - len(head) - len(tail)
        cut = transcript.head_cut + tail_cut
    marker = f"[... {_omission(omitted, cut)} omitted{_full_output(report)} ...]"
    return "\n".join([*head, marker, *tail])


def running_output_text(report: CommandReport) -> str:
    """The newest output of a running command: its last lines within the running
    limits, after a marker for what came before.

    The marker only counts: the result's ``next`` names the log file
    (``running_output_note``), and a marker naming it too said it twice.
    """
    transcript = report.transcript
    whole = _whole_lines(transcript)
    lines = whole if whole is not None else list(transcript.tail)
    first_cut = 0 if whole is not None else transcript.tail_cut
    if len(lines) > SHELL_RUNNING_OUTPUT_LINES:
        lines, first_cut = lines[-SHELL_RUNNING_OUTPUT_LINES:], 0
    kept, cut = _last_lines(lines, first_cut, SHELL_RUNNING_OUTPUT_CHARS)
    earlier = transcript.total_lines - len(kept)
    if earlier or cut:
        kept.insert(0, f"[... {_omission(earlier, cut, earlier=True)} omitted ...]")
    return "\n".join(kept)


def _whole_lines(transcript: CommandTranscript) -> list[str] | None:
    """Every line of the output, when the transcript holds all of them whole."""
    if transcript.tail_cut:
        return None
    # A head cut short holds the start of a line the tail can hold whole.
    lines = [*transcript.tail] if transcript.head_cut else [*transcript.head, *transcript.tail]
    return lines if len(lines) == transcript.total_lines else None


def _last_lines(lines: Sequence[str], first_cut: int, room: int) -> tuple[list[str], int]:
    """The last whole *lines* within *room* characters, and the characters cut.

    When the last line alone is longer, its end. The first of *lines* is
    *first_cut* characters short already.
    """
    if not lines:
        return [], 0
    start = len(lines)
    used = -1
    while start > 0 and not (start == 1 and first_cut):
        used += len(lines[start - 1]) + 1
        if used > room:
            break
        start -= 1
    if start < len(lines):
        return list(lines[start:]), 0
    last = lines[-1]
    end = last[len(last) - room :] if len(last) > room else last
    full = len(last) + (first_cut if len(lines) == 1 else 0)
    return [end], full - len(end)


def _text_chars(lines: Sequence[str]) -> int:
    """The characters *lines* take as text, joined by line breaks."""
    return sum(len(line) for line in lines) + max(0, len(lines) - 1)


def _omission(lines: int, chars: int, *, earlier: bool = False) -> str:
    """What a marker stands for: ``3 lines``, ``3 lines and 2,000 characters``."""
    parts = []
    if lines:
        parts.append(f"{lines:,} {'line' if lines == 1 else 'lines'}")
    if chars:
        parts.append(f"{chars:,} {'character' if chars == 1 else 'characters'}")
    if earlier:
        parts[0] = parts[0].replace(" ", " earlier ", 1)
    return " and ".join(parts)


def holding_stage(command: str) -> str | None:
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
    kept = _kept_output(report)
    if kept is None:
        return ""
    if report.log_loss is not None:
        # A lost copy does not undo the command's effects.
        return (
            f"; {kept}. The command has already run. "
            "Repeat it only if repeating its effects is safe."
        )
    return f"; {kept}"


def _kept_output(report: CommandReport) -> str | None:
    """The clause naming the log file as vBot's copy for the Agent; None without one.

    Agents whose task forbade reading outside their working directory took the
    file for someone else's and ran the whole command again instead of reading
    it. The file holds every byte the command wrote, escape sequences included.
    """
    lost = lost_output(report)
    if lost is not None:
        return lost
    path = report.log_path
    if path is None or report.not_run is not None:
        return None
    if report.stop_reason == "server_restart" or (
        report.stop_reason is not None and holding_stage(report.command) is not None
    ):
        # The lines a stage held never reached vBot, and a vBot that stopped
        # unexpectedly wrote nothing after it; a file of nothing is no copy.
        if not report.transcript.total_lines:
            return None
        return f"vBot kept the raw output that reached it in {model_path(path)} for you to read"
    return f"vBot kept this command's complete raw output in {model_path(path)} for you to read"


def lost_output(report: CommandReport, *, name_file: bool = True) -> str | None:
    """The clause saying how much of the raw output the log file holds and why
    not all of it, naming the file unless *name_file* is False; None while it
    holds every byte.

    A file that writing stopped short of holds only the bytes written before
    the failure. Its end is not the command's, and the command's effects remain.
    """
    loss = report.log_loss
    if loss is None or report.not_run is not None:
        return None
    if isinstance(loss.error, OSError):
        reason = os_error_reason(loss.error)
    else:
        reason = "an unexpected error occurred"
    path = report.log_path
    if path is None:
        return f"vBot could not keep this command's raw output; writing it failed: {reason}"
    # Raw output has no single line grammar or encoding: CR-only progress and
    # UTF-16 both make a count of LF bytes misleading. The written extent is exact.
    count = loss.kept_bytes
    kept = f"the first {count:,} {'byte' if count == 1 else 'bytes'}"
    where = f", in {model_path(path)}" if name_file else ""
    return (
        f"vBot kept only {kept} of this command's raw output{where}; writing more failed: {reason}"
    )


# Delivery


def format_command_delivery(report: CommandReport, *, names: ToolNames = _names_nothing) -> str:
    """The message that delivers a background command's result when it ends.

    *names* tells which Tools the message can name. A command the shell ran
    nothing of gets one line that says why.
    """
    not_run = not_run_problem(report)
    if not_run is not None:
        return (
            f"{COMMAND_STATUS_NOTE_MARKER}{report.command_id} ({report.label}) did not run: "
            f"{not_run[0]}"
        )
    ran = duration(report.elapsed_seconds)
    if report.stop_reason == "server_restart":
        minimum = _restart_minimum(report)
        ran = "an unknown duration" if minimum is None else f"at least {minimum}"
        status = f"was stopped after {ran}: {stopped_because(report, delivery=True)}"
    elif report.stop_reason is not None:
        status = f"was stopped after {ran}: {stopped_because(report, delivery=True)}"
    elif report.exit_code is None:
        status = f"exited after {ran}; its exit code is unknown."
    else:
        status = f"exited with code {report.exit_code} after {ran}."
    lines = [f"{COMMAND_STATUS_NOTE_MARKER}{report.command_id} ({report.label}) {status}"]
    if (
        report.stop_reason is None
        and report.exit_code is not None
        and (hexadecimal := exit_code_hex(report.exit_code))
    ):
        # Keep the durable status line readable by earlier versions.
        lines.append(f"Exit code (hex): {hexadecimal}")
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
    r"|did not run:"
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
    elif status == "not_run":
        statuses[command_id] = "failed"
    elif status == "exited":
        statuses[command_id] = "completed" if data.get("exit_code") == 0 else "failed"


__all__ = [
    "BACKGROUND_TASKS_TOOL_NAME",
    "COMMAND_STATUS_NOTE_MARKER",
    "COMMAND_STATUS_TOOL_NAMES",
    "SHELL_TOOL_NAME",
    "TERMINAL_TOOL_NAME",
    "RUN_AGAIN",
    "ToolNames",
    "background_command_statuses",
    "command_output_text",
    "delivery_names",
    "duration",
    "ended_result",
    "exit_code_hex",
    "exit_code_text",
    "failed_programs",
    "failure_hint",
    "format_command_delivery",
    "holding_stage",
    "stop_call",
    "leftovers_stop",
    "lost_output",
    "named_tools",
    "not_run_problem",
    "output_location",
    "process_text",
    "processes_text",
    "running_output_note",
    "running_output_text",
    "silence_note",
    "stop_with",
    "stopped_because",
]


class _NotStartedError(Exception):
    """The shell could not be started for a reason other than the call; nothing ran."""

    def __init__(self, result: JsonObject) -> None:
        super().__init__(result["error"]["message"])
        self.result = result


def _cannot_start(problem: str) -> _NotStartedError:
    """The shell cannot be started on this host for this command; a retry fails the same way."""
    return _NotStartedError(
        tool_failure(
            "command_not_started", f"{SHELL_MODEL_NAME} was not run: {problem}", retryable=False
        )
    )


def _start_failure(
    error: Exception, call: _ShellCall, context: ToolContext, names: ToolNames
) -> JsonObject:
    """The failure for a command the manager did not start."""
    if isinstance(error, CommandCapacityError):
        message = (
            f"{SHELL_MODEL_NAME} was not run: vBot already runs {error.limit} commands, the "
            "most it runs at once."
        )
        if names(BACKGROUND_TASKS_TOOL_NAME):
            message += (
                f' Find your background commands with {BACKGROUND_TASKS_TOOL_NAME} {{"action": '
                '"list"}, stop the ones you no longer need with action "stop", then run the '
                "command again."
            )
        else:
            message += " Run the command again once one of your background commands has exited."
        return tool_failure("command_limit", message, retryable=True)
    if isinstance(error, CommandNotStartedError):
        return tool_failure(
            "command_not_started",
            f"{SHELL_MODEL_NAME} was not run: this turn was cancelled, or vBot is shutting down.",
            retryable=False,
        )
    try:
        directory = is_dir_strict(call.workdir)
    except OSError:
        directory = True
    if not directory:
        # The directory was removed after the call was checked.
        given = call.workdir != context.effective_cwd
        return tool_failure(
            "command_not_started",
            f"{SHELL_MODEL_NAME} was not run: "
            + _missing_workdir(
                call.workdir,
                given=given,
                cwd=context.effective_cwd,
                permissions=context.permissions,
            ),
            retryable=False,
        )
    _LOGGER.warning("Starting a shell command failed", exc_info=error)
    problem, retryable = _launch_problem(error, call, names)
    return tool_failure(
        "command_not_started", f"{SHELL_MODEL_NAME} was not run: {problem}", retryable=retryable
    )


# Operating system errors a start can meet, by errno and by Windows error code.
_NO_RESOURCES_ERRNOS = frozenset({errno.ENOMEM, errno.EAGAIN, errno.EMFILE, errno.ENFILE})


_NO_RESOURCES_WINERRORS = frozenset({4, 8, 14, 1450, 1455})


_DISK_FULL_ERRNOS = frozenset({errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)})


_DISK_FULL_WINERRORS = frozenset({39, 112})


def _launch_problem(error: Exception, call: _ShellCall, names: ToolNames) -> tuple[str, bool]:
    """What kept the command from starting and what to do, and whether a retry can help.

    The operating system's own message is in the host's language and names no
    next step: the text names the state it reports instead.
    """
    os_error = _os_error(error)
    # A launch error is the shell process's; any other is the output file's.
    process = isinstance(error, CommandLaunchError)
    if os_error is None:
        return f"vBot could not start the shell. {RUN_AGAIN} vBot's log has the details.", True
    winerror = getattr(os_error, "winerror", None)
    if os_error.errno in _NO_RESOURCES_ERRNOS or winerror in _NO_RESOURCES_WINERRORS:
        problem = "the system is out of memory or process resources, so the shell could not start."
        if names(BACKGROUND_TASKS_TOOL_NAME):
            return (
                f"{problem} Stop the background commands you no longer need (find them with "
                f'{BACKGROUND_TASKS_TOOL_NAME} {{"action": "list"}}), then run the command again; '
                "if it fails the same way, tell the user.",
                True,
            )
        return f"{problem} {RUN_AGAIN}", True
    if os_error.errno in _DISK_FULL_ERRNOS or winerror in _DISK_FULL_WINERRORS:
        return (
            "the disk is full, so vBot could not write the command's output. Tell the user that "
            "the disk is full.",
            False,
        )
    if isinstance(os_error, PermissionError):
        if process:
            return (
                f"access was denied when vBot started the shell in {model_path(call.workdir)}. "
                "If the command can run in another directory, pass that directory as workdir; "
                "otherwise tell the user.",
                False,
            )
        return (
            "access was denied to vBot's data directory, where the command's output is written. "
            "Tell the user.",
            False,
        )
    code = winerror if winerror is not None else os_error.errno
    detail = f" (operating system error {code})" if code is not None else ""
    if process:
        problem = f"the operating system could not start the shell{detail}."
    else:
        problem = f"vBot could not create the file for the command's output{detail}."
    return f"{problem} {RUN_AGAIN}", True


def _os_error(error: BaseException) -> OSError | None:
    """The operating system error *error* is or was caused by, if any."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, OSError):
            return current
        seen.add(id(current))
        current = current.__cause__
    return None
