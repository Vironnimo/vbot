"""Terminal observations, next actions and access-error results."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.tools._background_tasks_arguments import (
    BACKGROUND_TASKS_TOOL_NAME,
    COMMAND_ID_PREFIX,
    SUBAGENT_ID_PREFIX,
)
from core.tools._shell_arguments import resolve_timeout
from core.tools._terminal_arguments import (
    command_words,
)
from core.tools.arguments import (
    optional_number,
    optional_string,
)
from core.tools.contracts import ToolContractError
from core.tools.terminal_manager import (
    TERMINAL_STATUS_DEFAULT_LINES,
    TerminalCapacityError,
    TerminalInfo,
    TerminalManager,
    TerminalNotFoundError,
    TerminalOwner,
    screen_behind_note,
    unseen_lines_note,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
)

from ._terminal_arguments import TERMINAL_KEY_SUMMARY

if TYPE_CHECKING:
    pass

from core.utils.logging import get_logger

from ._terminal_definition import (
    _ARGUMENT_CHARS,
    _FINISHED_MINUTES,
    _LISTED_TERMINALS,
    _LOG_RETENTION_HOURS,
    _PATTERN_FLAGS,
    _PROGRAM_CHARS,
    _TERMINAL_LABEL_CHARS,
    TERMINAL_KEYS,
    TERMINAL_TOOL_NAME,
    TERMINAL_WAIT_MAX_SECONDS,
)

_LOGGER = get_logger("tools.terminal")


@dataclass(frozen=True, slots=True)
class _UnsentStartInput:
    """Input given to start, which launches the program and sends nothing."""

    text: str | None
    data: str | None
    key: str | None

    @classmethod
    def of(cls, arguments: JsonObject) -> _UnsentStartInput:
        text, data = arguments.get("text"), arguments.get("data")
        key = optional_string(arguments.get("key"), field_name="key") or None
        if key is not None and key not in TERMINAL_KEYS:
            raise ToolContractError(_unknown_key_message(key))
        text = text if isinstance(text, str) and text else None
        if key == "enter" and text is None:
            # Enter is the habitual companion of text, and requests nothing alone.
            key = None
        return cls(text, data if isinstance(data, str) and data else None, key)

    def note(self, terminal_id: str, *, running: bool) -> str | None:
        """What was not sent, and how to send it once the program is ready."""
        if self.text is not None or self.data is not None:
            what = "text was not typed" if self.text is not None else "data was not sent"
            if not running:
                return f"{what}: start only launches the program."
            if self.text is not None:
                yours, fields = "your text", f'your text as text and key "{self.key or "enter"}"'
            else:
                yours, fields = "your data", "your data as data"
            return (
                f"{what}: start only launches the program. Answer what its screen asks first, "
                f"if anything; once it is ready for {yours}, send it with terminal action "
                f'"input", terminal_id "{terminal_id}", {fields}.'
            )
        if self.key is None:
            return None
        unsent = f'key "{self.key}" was not pressed: start only launches the program.'
        if not running:
            return unsent
        press = json.dumps({"action": "input", "terminal_id": terminal_id, "key": self.key})
        return f"{unsent} Once the program is ready for it, call terminal {press}."


def _unread_screen_result(
    terminal_manager: TerminalManager, terminal_id: str, error: Exception, *, done: str
) -> JsonObject:
    """The result of a start or input whose effect happened, when the screen
    after it could not be read."""
    _LOGGER.warning(
        "Terminal screen could not be read after %s (terminal=%s)",
        done.lower(),
        terminal_id,
        exc_info=error,
    )
    info = _find(terminal_manager, terminal_id)
    data: JsonObject = {
        "terminal_id": terminal_id,
        "state": _agent_state(info) if info is not None else "running",
    }
    if info is not None:
        data.update(
            computer_id=info.computer_id,
            computer_name=info.computer_name,
            computer_online=info.computer_online,
        )
        if info.launch_pending:
            data["launch_pending"] = True
            done = "The launch remains unconfirmed"
    if info is not None and info.finished and info.exit_code is not None:
        data["exit_code"] = info.exit_code
    data["note"] = (
        f"{done}, but its screen could not be read ({error}). To try reading it again, call "
        f"terminal {_call('status', terminal_id)}."
    )
    return data


# What an attached interactive program sends on its own; Agents polled for it.
_DELIVERIES_TEXT = (
    "While it runs, its screen arrives as a new message each time it goes idle showing something "
    "new, and so does its exit, so do not poll with status. Continue with other work your task "
    "still needs. When no such work is left, end your turn: reply with a short status and no "
    "tool call. Your next turn starts when its next screen arrives."
)


# The output after input paused and the program declared no end of its work:
# a coding agent waiting for its model shows neither output nor CPU time
# (user decision D5).
_PAUSED_TEXT = (
    "It can still be working: unless its screen shows it waiting for input, its next screen "
    "arrives as a new message once it goes idle."
)


def _first_screen_pending_text(terminal_id: str) -> str:
    """What comes next for a running program that has shown nothing (user decision D4)."""
    return (
        "It has shown nothing yet. Its first screen arrives as a new message once it shows one; "
        f"to wait for it now, call terminal {_call('wait', terminal_id)}."
    )


def _reply_pending_text(terminal_id: str) -> str:
    """What to do when an interactive program still works as input's wait ended."""
    return (
        "It is still working: its screen arrives as a new message once it goes idle. Continue "
        "with other work your task still needs. When no such work is left, end your turn: "
        "reply with a short status and no tool call, and your next turn starts when its screen "
        f"arrives; to wait within this turn, call terminal {_call('wait', terminal_id)}."
    )


# Results


# Snapshot facts no Tool result shows: attention records repeat what state,
# wait_ended and the screen already say, revisions guard input the terminal
# tracks itself, timestamps and sizes change no next call, and the observation
# is the manager's record of the shown screen.
_HIDDEN_SNAPSHOT_FIELDS = frozenset(
    {
        "attention",
        "attention_revision",
        "screen_revision",
        "started_at",
        "finished_at",
        "pid",
        "observation",
        "columns",
        "rows",
        "alternate_screen",
        "stopped",
        "command",
        "arguments",
        "cursor",
        "seen_line",
        "idle_reason",
        "log_complete",
        "log_end_line",
        "screen_behind",
    }
)
_ALTERNATE_SCREEN_NOTE = (
    "The program shows a full-screen view (alternate screen), which keeps no terminal "
    "history; to see earlier content, use the program's own scrolling or paging."
)
# Launch facts: status shows them; other results do not repeat them, except
# that start names the log.
_LAUNCH_FIELDS = ("program", "title", "workdir", "log")
# Results that follow the program: their history holds only lines the Agent
# has not seen, and they offer no pages (user decision D1).
_FOLLOWING_VIEWS = frozenset({"start", "input", "wait", "resize"})


def _log_text(path: str, *, complete: bool, end_line: int | None = None) -> str:
    """What an interactive terminal's log file holds; a bare path did not say."""
    if not complete:
        lacks = "later output" if end_line is None else f"the output from line {end_line} on"
        return (
            f"The program's output was written to {path}, exactly as the terminal received "
            f"it, until writing it failed; {lacks} is missing from it."
        )
    return (
        f"Everything the program printed is written live to {path}, exactly as the terminal "
        "received it, with its control sequences."
    )


def _screen_result(
    snapshot: dict[str, Any],
    *,
    view: str,
    page_lines: int = TERMINAL_STATUS_DEFAULT_LINES,
    include_screen: bool = True,
    unseen_from: int | None = None,
    unseen_by_you: bool = True,
    requested_start: int | None = None,
) -> JsonObject:
    """Shape an interactive terminal's snapshot for one action's result.

    History text becomes its own multi-line ``history`` field; in status,
    ``scrollback`` keeps the line numbers and the requests for the adjacent
    pages, and appears only when there is history or another page to read.
    start, input and wait show history only from *unseen_from* on and no
    ``scrollback``. ``cursor`` follows a running program's screen that shows
    something. With *unseen_from* (the first
    history line the reader has not seen), a note says how many of those
    lines the page leaves out above it and where to read them;
    *unseen_by_you* says the reader is the attached Session, whose seen
    lines are known. *requested_start* is the start_line a status page was
    asked for; a note says when the page could not start there.
    """
    log = snapshot.get("log_file")
    log = log if isinstance(log, str) else None
    log_complete = snapshot.get("log_complete") is not False
    end = snapshot.get("log_end_line")
    log_end = end if isinstance(end, int) and not log_complete else None
    projected: JsonObject = {
        "terminal_id": snapshot["terminal_id"],
        "call_name": snapshot.get("call_name") if view in {"start", "status"} else None,
        "state": _agent_state_of(snapshot),
    }
    if not projected["call_name"]:
        del projected["call_name"]
    if projected["state"] != "running" and snapshot.get("exit_code") is not None:
        projected["exit_code"] = snapshot["exit_code"]
    for key, value in snapshot.items():
        if key in _HIDDEN_SNAPSHOT_FIELDS or key in projected or key in {"state", "call_name"}:
            continue
        if value is None or value == "" or key == "exit_code":
            continue
        if key == "log_file":
            projected["log"] = _log_text(str(value), complete=log_complete, end_line=log_end)
            continue
        projected[key] = value
    if view != "status":
        for key in _LAUNCH_FIELDS:
            if not (view == "start" and key == "log"):
                projected.pop(key, None)
    screen = projected.pop("screen", None)
    page = dict(projected.pop("scrollback", None) or {})
    history = page.pop("text", "")
    shown_from = page.get("start_line")
    following = view in _FOLLOWING_VIEWS
    if following and history and isinstance(shown_from, int) and unseen_from is not None:
        rows = history.split("\n")
        seen = max(0, min(len(rows), unseen_from - shown_from))
        history = "\n".join(rows[seen:])
        shown_from += seen
    scrollback: JsonObject = {
        key: page[key]
        for key in ("first_line", "start_line", "end_line", "total_lines", "screen_start_line")
        if key in page
    }
    adjacent = (("older_request", "previous_start_line"), ("newer_request", "next_start_line"))
    for name, key in adjacent:
        start = page.get(key)
        if isinstance(start, int):
            scrollback[name] = {
                "action": "status",
                "terminal_id": str(snapshot["terminal_id"]),
                "start_line": start,
                "lines": page_lines,
            }
    if history:
        projected["history"] = history
    if include_screen:
        projected["screen"] = screen if isinstance(screen, str) else ""
        cursor = snapshot.get("cursor")
        if (
            projected["state"] == "running"
            and projected["screen"]
            and isinstance(cursor, tuple | list)
            and cursor
        ):
            projected["cursor"] = f"row {cursor[0]}, column {cursor[1]}"
    if not following and (not include_screen or history or "older_request" in scrollback):
        projected["scrollback"] = scrollback
    notes: list[str] = []
    behind = snapshot.get("screen_behind")
    if include_screen and behind:
        notes.append(screen_behind_note(str(behind), log, complete=log_complete))
    if view == "status" and snapshot.get("alternate_screen"):
        notes.append(_ALTERNATE_SCREEN_NOTE)
    if requested_start is not None and isinstance(page.get("start_line"), int):
        moved = _moved_page_note(requested_start, page, log, complete=log_complete, log_end=log_end)
        if moved is not None:
            notes.append(moved)
    if unseen_from is not None and isinstance(shown_from, int):
        unseen = unseen_lines_note(
            str(snapshot["terminal_id"]),
            unseen_from=unseen_from,
            shown_from=shown_from,
            first_line=int(page.get("first_line") or 0),
            log=log,
            seen=unseen_by_you,
            log_complete=log_complete,
            log_end_line=log_end,
        )
        if unseen is not None:
            notes.append(unseen)
            if view == "start" and log is not None and log in unseen:
                # The note names the log where the Agent needs it.
                projected.pop("log", None)
    return _with_notes(projected, notes)


def _moved_page_note(
    requested: int,
    page: Mapping[str, Any],
    log: str | None,
    *,
    complete: bool,
    log_end: int | None = None,
) -> str | None:
    """A status page asked for lines the terminal does not have; *log_end* is
    the first line a log that failed lacks, when known."""
    start = int(page["start_line"])
    if requested < start:
        note = (
            f"Lines {requested} to {start - 1} are no longer kept, so this page starts at line "
            f"{start}"
        )
        if log is None:
            return f"{note}."
        if complete:
            return f"{note}; every line the program printed is in the log {log}."
        if log_end is not None and log_end >= start:
            return f"{note}; they are in the log {log}."
        if log_end is None:
            return f"{note}; the log {log} has the program's output until writing it failed."
        if log_end <= requested:
            return f"{note}, and writing the log {log} failed before them."
        return (
            f"{note}; the log {log} has lines {requested} to {log_end - 1}, and writing it "
            "failed before the later ones."
        )
    total = page.get("total_lines")
    if isinstance(total, int) and requested >= total:
        has = f"has lines up to {total - 1}" if total else "has no lines yet"
        return f"Line {requested} does not exist yet: the terminal {has}."
    return None


def _unseen_from(snapshot: Mapping[str, Any], attached: bool = True) -> int:
    """The first history line the reader of *snapshot* has not seen: the attached
    Session has seen what its results and messages showed; another reader is
    known to have seen nothing."""
    seen = snapshot.get("seen_line")
    return seen if attached and isinstance(seen, int) else 0


def _agent_state(info: TerminalInfo) -> str:
    """The state an Agent sees: running, exited, stopped or failed."""
    return _state_text(info.state, stopped=info.stopped)


def _agent_state_of(snapshot: Mapping[str, Any]) -> str:
    return _state_text(str(snapshot.get("state")), stopped=bool(snapshot.get("stopped")))


def _state_text(state: str, *, stopped: bool) -> str:
    if state == "error":
        return "failed"
    if state == "exited":
        return "stopped" if stopped else "exited"
    return "running"


def _attachment(info: TerminalInfo, owner: TerminalOwner) -> str:
    """Who holds the terminal: ``here`` (this Session), ``other`` or ``no`` (nobody)."""
    if info.attachment == owner:
        return "here"
    return "no" if info.attachment is None else "other"


def _terminal_facts(info: TerminalInfo, owner: TerminalOwner) -> JsonObject:
    facts: JsonObject = {
        "terminal_id": info.terminal_id,
        "computer_id": info.computer_id,
        "computer_name": info.computer_name,
        "computer_online": info.computer_online,
        "call_name": info.call_name,
        "program": _short_program(info.program),
        "state": _agent_state(info),
        "exit_code": info.exit_code if info.finished else None,
        "attached": _attachment(info, owner),
    }
    return {key: value for key, value in facts.items() if value not in (None, "")}


def _short_program(program: str) -> str:
    lines = program.strip().splitlines()
    first = lines[0] if lines else program
    if len(lines) > 1 or len(first) > _PROGRAM_CHARS:
        return first[: _PROGRAM_CHARS - 3] + "..."
    return first


def _with_notes(data: JsonObject, notes: list[str]) -> JsonObject:
    if notes:
        existing = data.get("note")
        data["note"] = " ".join([*([existing] if isinstance(existing, str) else []), *notes])
    return data


def _capped_wait_note() -> str:
    """A capped wait timed out."""
    return (
        f"A wait lasts at most {TERMINAL_WAIT_MAX_SECONDS} seconds, so this one ended after "
        f"{TERMINAL_WAIT_MAX_SECONDS}; wait again to keep following the program."
    )


def _wait_seconds(
    arguments: JsonObject, *, default: float, zero: float | None = None
) -> tuple[float, list[str], bool]:
    """Return how long to wait in seconds, notes about reading the value, and whether
    the value was capped; *zero* is the wait a timeout of 0 means."""
    timeout = optional_number(arguments.get("timeout"), field_name="timeout", minimum=0)
    timeout_ms = optional_number(arguments.get("timeout_ms"), field_name="timeout_ms", minimum=0)
    seconds, note = resolve_timeout(timeout, timeout_ms, tool_name=TERMINAL_TOOL_NAME)
    notes = [note] if note else []
    if seconds is None:
        return default, notes, False
    if seconds == 0 and zero is not None:
        return zero, notes, False
    if seconds > TERMINAL_WAIT_MAX_SECONDS:
        return TERMINAL_WAIT_MAX_SECONDS, notes, True
    return seconds, notes, False


def _wait_pattern(arguments: JsonObject) -> tuple[re.Pattern[str] | None, list[str]]:
    """The pattern a wait looks for, case-insensitive and with ``^``/``$`` at every line;
    invalid syntax is literal text."""
    raw = arguments.get("pattern")
    if raw is None or raw == "":
        return None, []
    if not isinstance(raw, str):
        raise ToolContractError("terminal was not run: pattern must be text, a regular expression.")
    try:
        return re.compile(raw, _PATTERN_FLAGS), []
    except re.error as error:
        return re.compile(re.escape(raw), _PATTERN_FLAGS), [
            f"pattern is not a valid regular expression ({error}), so it was matched as "
            "literal text."
        ]


def _split_command_line(command: str, environment: Mapping[str, str]) -> list[str] | None:
    """Return the words of a command line sent as command, or None for one program.

    A command with spaces is one program only when it names one, such as a path
    with spaces; otherwise its first word must be a program.
    """
    if not any(character.isspace() for character in command):
        return None
    path = environment.get("PATH")
    if shutil.which(command, path=path) is not None or Path(command).is_file():
        return None
    try:
        words = command_words(command)
    except ValueError:
        return None
    if len(words) < 2 or shutil.which(words[0], path=path) is None:
        return None
    return words


# Errors


def _call(action: str, terminal_id: str) -> str:
    return json.dumps({"action": action, "terminal_id": terminal_id})


def _terminal_label(info: TerminalInfo) -> str:
    label = " ".join(info.program.split())
    if len(label) > _TERMINAL_LABEL_CHARS:
        label = label[: _TERMINAL_LABEL_CHARS - 3] + "..."
    return f"{info.call_name}, {label}" if info.call_name else label


def _attached_terminals(
    terminal_manager: TerminalManager, owner: TerminalOwner
) -> list[TerminalInfo]:
    """The terminals attached to this Session, running ones first, newest first."""
    return sorted(
        (info for info in reversed(terminal_manager.list_terminals()) if info.attachment == owner),
        key=lambda info: info.finished,
    )


def _terminals_text(
    terminal_manager: TerminalManager, owner: TerminalOwner, *, allowed: set[str] | None = None
) -> str:
    """Name the terminals attached to this Session, running ones first, newest first."""
    attached = _attached_terminals(terminal_manager, owner)
    if allowed is not None:
        attached = [info for info in attached if info.computer_id in allowed]
    if not attached:
        if terminal_manager.list_terminals():
            return (
                "No terminal is attached to this Session; to see every terminal, call terminal "
                '{"action": "list"}.'
            )
        return "No terminal is open."
    if len(attached) == 1:
        only = attached[0]
        return (
            f"The terminal attached to this Session is {only.terminal_id} "
            f"({_agent_state(only)}: {_terminal_label(only)}); repeat the call with "
            f'terminal_id "{only.terminal_id}".'
        )
    shown = "; ".join(
        f"{info.terminal_id} ({_agent_state(info)}: {_terminal_label(info)})"
        for info in attached[:_LISTED_TERMINALS]
    )
    more = (
        ' To see all of them, call terminal {"action": "list"}.'
        if len(attached) > _LISTED_TERMINALS
        else ""
    )
    return (
        f"Terminals attached to this Session: {shown}. Repeat the call with one of them as "
        f"terminal_id.{more}"
    )


def _not_found_message(
    terminal_manager: TerminalManager,
    owner: TerminalOwner,
    terminal_id: str,
    *,
    allowed: set[str] | None = None,
) -> str:
    attached = _attached_terminals(terminal_manager, owner)
    if allowed is not None:
        attached = [info for info in attached if info.computer_id in allowed]
    named = (
        " Terminals attached to this Session: "
        + "; ".join(
            f"{info.terminal_id} ({_agent_state(info)}: {_terminal_label(info)})"
            for info in attached[:_LISTED_TERMINALS]
        )
        + "."
        if attached
        else ""
    )
    return (
        f"No terminal with the id {terminal_id} is open: the id is wrong, or its program "
        f"ended more than {_FINISHED_MINUTES} minutes ago and the terminal was removed. Its "
        f"output log, if a result named one, is kept for {_LOG_RETENTION_HOURS} hours after "
        f'the program ended.{named} To see all terminals, call terminal {{"action": "list"}}.'
    )


def _not_owned_message(terminal_manager: TerminalManager, terminal_id: str, action: str) -> str:
    info = _find(terminal_manager, terminal_id)
    status = f"terminal {_call('status', terminal_id)}"
    if info is not None and info.attachment is not None:
        return (
            f"{terminal_id} is attached to another Session; only that Session can wait for it, "
            f"type into it, resize it or kill it. Nothing was done. To read its screen, call "
            f"{status}."
        )
    return (
        f"{terminal_id} is not attached to this Session, so {action} was not done. Attach it "
        f"with terminal {_call('attach', terminal_id)}, then repeat the call."
    )


def _not_attached_text(info: TerminalInfo, terminal_id: str) -> str:
    if info.attachment is None:
        return (
            f"To type into it or wait for it, attach it first with terminal "
            f"{_call('attach', terminal_id)}."
        )
    return "It is attached to another Session; only that Session can type into it or wait for it."


def _closed_message(terminal_manager: TerminalManager, terminal_id: str, action: str) -> str:
    info = _find(terminal_manager, terminal_id)
    ended = "has ended"
    if info is not None and info.exit_code is not None:
        state = _state_text("error" if info.state == "error" else "exited", stopped=info.stopped)
        ended = f"has ended ({state}, exit code {info.exit_code})"
    done = "the input was not sent" if action == "input" else f"{action} was not done"
    message = (
        f"The program in {terminal_id} {ended}, so {done}. To read its last screen, call "
        f"terminal {_call('status', terminal_id)}."
    )
    if info is not None:
        message += ' To run the program again, call terminal with action "start".'
    return message


def _background_task_refusal(context: ToolContext, task_id: str, action: str) -> str | None:
    """Refuse the id of a background command or Sub-Agent, which ``background_tasks`` serves.

    A ``kill`` gets the call that stops the task; every other action the one that shows it.
    """
    if task_id.startswith(COMMAND_ID_PREFIX):
        task = "a background command"
    elif task_id.startswith(SUBAGENT_ID_PREFIX):
        task = "a Sub-Agent"
    else:
        return None
    refusal = f"{task_id} is {task}, not a terminal. Nothing was changed."
    # Its exact call works without loading it first, so it is named whenever the
    # Agent can call it, as the shell results name it (``named_tools``).
    if not context.can_call(BACKGROUND_TASKS_TOOL_NAME):
        return refusal
    call = json.dumps({"action": "stop" if action == "kill" else "status", "id": task_id})
    return (
        f"{refusal} Use {BACKGROUND_TASKS_TOOL_NAME} for it, such as "
        f"{BACKGROUND_TASKS_TOOL_NAME} {call}."
    )


def _capacity_message(error: TerminalCapacityError) -> str:
    terminals = error.terminals
    named = "; ".join(f"{info.terminal_id} ({_terminal_label(info)})" for info in terminals)
    stop = (
        f" Your running terminals: {named}. Stop one you no longer need with terminal action "
        '"kill" and its terminal_id from this list, then start again.'
        if terminals
        else ""
    )
    if error.scope == "session":
        return (
            f"This Session already runs {error.limit} terminals, the most it can run at once, "
            f"so no terminal was started.{stop}"
        )
    if error.scope == "global":
        rest = stop or " Ask the user to close terminals they no longer need, then start again."
        return (
            f"{error.limit} terminals already run, the most vBot runs at once, so no terminal "
            f"was started.{rest}"
        )
    return str(error)


def _unpassable_argument_message(program: str, value: str) -> str:
    """start's batch program cannot receive the argument *value* through cmd.exe.

    The argument is named by its text: the command can have been split into
    words before args, so a position would point at the wrong one.
    """
    breaks = "\n" in value or "\r" in value
    nul = "\0" in value
    if len(value) > _ARGUMENT_CHARS:
        value = value[: _ARGUMENT_CHARS - 3] + "..."
    what = " and ".join(
        part for part, present in (("a line break", breaks), ("a NUL character", nul)) if present
    )
    plural = " and ".join(
        part for part, present in (("line breaks", breaks), ("NUL characters", nul)) if present
    )
    return (
        f"No terminal was started: {program} is a batch program (.cmd or .bat), which runs "
        f"through cmd.exe, and cmd.exe cannot pass the argument "
        f"{json.dumps(value, ensure_ascii=False)} to it because it contains {what}. Pass that "
        f"text without {plural}, or leave it out and send it with the input action once the "
        "program is ready."
    )


def _not_found_program_message(error: FileNotFoundError, arguments: JsonObject) -> str:
    program = error.filename or arguments.get("command") or "the requested program"
    return (
        f"Program {program} was not found, so no terminal was started. Pass the program's full "
        "path as command, or omit command to start the user's default shell and run it there."
    )


# Access


def _find(terminal_manager: TerminalManager, terminal_id: str) -> TerminalInfo | None:
    return next(
        (info for info in terminal_manager.list_terminals() if info.terminal_id == terminal_id),
        None,
    )


def _terminal_info(terminal_manager: TerminalManager, terminal_id: str) -> TerminalInfo:
    info = _find(terminal_manager, terminal_id)
    if info is None:
        raise TerminalNotFoundError(f"Terminal Session not found: {terminal_id}")
    return info


def _unknown_key_message(key: str) -> str:
    return (
        f'terminal was not run: key "{key}" is not a named key. Named keys: '
        f"{TERMINAL_KEY_SUMMARY}. Type other characters as text, or send exact sequences as data."
    )
