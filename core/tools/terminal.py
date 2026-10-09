"""Agent-facing live terminals backed by PTY/ConPTY.

The ``terminal`` Tool starts interactive programs that an Agent drives by
typing, and operates the commands the shell Tool left running. For a command
terminal, ``status``, ``wait`` and ``kill`` return the shell result
(``command_terminal_result``), so following a command up reads like the
shell call that started it.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from collections.abc import Awaitable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from core.projects import ProjectStore
from core.tools._shell_arguments import resolve_timeout
from core.tools._terminal_arguments import (
    TERMINAL_KEY_SUMMARY,
    command_words,
    normalize_terminal_arguments,
)
from core.tools._workdir import ProjectWorkdirError, is_project_workdir, project_workdir
from core.tools.arguments import (
    optional_int,
    optional_number,
    optional_string,
    required_string,
)
from core.tools.contracts import ToolContractError
from core.tools.model_names import BASH_TOOL_NAME, model_tool_name
from core.tools.shell_environment import terminal_environment
from core.tools.terminal_backend import default_terminal_argv
from core.tools.terminal_manager import (
    TERMINAL_DEFAULT_COLUMNS,
    TERMINAL_DEFAULT_ROWS,
    TERMINAL_FINISHED_TTL,
    TERMINAL_GROUP_NAME_MAX_CHARS,
    TERMINAL_INPUT_KEY_SEQUENCES,
    TERMINAL_INPUT_MAX_CHARS,
    TERMINAL_MAX_COLUMNS,
    TERMINAL_MAX_ROWS,
    TERMINAL_MIN_COLUMNS,
    TERMINAL_MIN_ROWS,
    TERMINAL_START_WAIT_SECONDS,
    TERMINAL_STATUS_DEFAULT_LINES,
    TERMINAL_STATUS_MAX_LINES,
    TerminalAlreadyAttachedError,
    TerminalCapacityError,
    TerminalClosedError,
    TerminalInfo,
    TerminalIsCommandError,
    TerminalLaunchError,
    TerminalManager,
    TerminalManagerError,
    TerminalNotAttachedError,
    TerminalNotFoundError,
    TerminalNotOwnedError,
    TerminalOwner,
    TerminalStaleScreenError,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
    display_notice,
    display_results,
    display_text,
    result_count_fact_builder,
    tool_failure,
    tool_success,
)
from core.utils.paths import model_path

TERMINAL_TOOL_NAME = "terminal"
TERMINAL_ACTIONS = (
    "start",
    "list",
    "status",
    "wait",
    "input",
    "kill",
    "attach",
    "detach",
)
TERMINAL_WAIT_DEFAULT_SECONDS = 60
TERMINAL_WAIT_MAX_SECONDS = 600
# input waits this long for the reply to settle, as start does for the first screen.
TERMINAL_REPLY_SECONDS = TERMINAL_START_WAIT_SECONDS
# Case-insensitive, with ^ and $ at every line of the output.
_PATTERN_FLAGS = re.IGNORECASE | re.MULTILINE
TERMINAL_KEYS = tuple(TERMINAL_INPUT_KEY_SEQUENCES)
# Terminals named in errors, and the label length for each.
_LISTED_TERMINALS = 3
# A matched line longer than this is cut.
_MATCHED_LINE_CHARS = 500
_TERMINAL_LABEL_CHARS = 40
# A list row shows this much of the program's first line.
_PROGRAM_CHARS = 80
_FINISHED_MINUTES = int(TERMINAL_FINISHED_TTL.total_seconds() // 60)
_SHELL_NAME = model_tool_name(BASH_TOOL_NAME)


def _terminal_description(*, shell: str | None) -> str:
    """The description for one request: the shell Tool's name when offered."""
    sentences = [
        "Start and operate interactive programs you drive by typing, such as REPLs, TUIs and "
        "coding-agent CLIs."
    ]
    if shell:
        sentences.append(f"Run all other commands, servers included, with {shell}.")
    sentences.append("A program keeps running after your turn ends, until it exits or is stopped.")
    return " ".join(sentences)


def _terminal_id_description(shell: str | None) -> str:
    sources = f"start, list, or a {shell} result" if shell else "start or list"
    return f"The terminal's id from {sources}. Required except for start and list."


TERMINAL_TOOL_DESCRIPTION = _terminal_description(shell=_SHELL_NAME)

TERMINAL_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(TERMINAL_ACTIONS),
            "description": (
                "start launches a program attached to this Session and returns its first "
                "screen; list shows terminals; status shows a terminal's screen, or a "
                "command's output; wait waits until the program exits, prints output "
                "matching pattern, or the timeout passes; without pattern, it also ends when "
                "an interactive program's new output settles; input types text and keys; kill "
                "stops the program and everything it started; attach takes over an unattached "
                "terminal; detach releases it for another Session."
            ),
        },
        "terminal_id": {
            "type": "string",
            "description": _terminal_id_description(_SHELL_NAME),
        },
        "command": {
            "type": "string",
            "description": (
                "Program to start, without shell expansion. Omit to start the user's default shell."
            ),
        },
        "args": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Arguments for command. Omit for none.",
        },
        "workdir": {
            "type": "string",
            "description": (
                "Directory to start in, absolute or relative to the working directory. Omit to "
                "use the working directory."
            ),
        },
        "name": {
            "type": "string",
            "maxLength": 80,
            "description": (
                "Label the user sees for the terminal, such as its task. Omit to show the "
                "program instead."
            ),
        },
        "group": {
            "type": "string",
            "maxLength": TERMINAL_GROUP_NAME_MAX_CHARS,
            "description": (
                "Group to show the terminal in; joins or creates the group of that name. Omit "
                "for automatic grouping."
            ),
        },
        "text": {
            "type": "string",
            "maxLength": TERMINAL_INPUT_MAX_CHARS,
            "description": (
                'For input, text to type; add key "enter" to submit it. For start, text to '
                "submit once the program is ready. Multiline text is pasted as one block when "
                "the program supports it."
            ),
        },
        "key": {
            "type": "string",
            "description": (
                f"Named key for input, pressed after text: {TERMINAL_KEY_SUMMARY}. Omit to "
                "type text without submitting it."
            ),
        },
        "data": {
            "type": "string",
            "maxLength": TERMINAL_INPUT_MAX_CHARS,
            "description": (
                "Exact characters for input, including control sequences, only for what text "
                "and key cannot express."
            ),
        },
        "lines": {
            "type": "integer",
            "minimum": 1,
            "maximum": TERMINAL_STATUS_MAX_LINES,
            "default": TERMINAL_STATUS_DEFAULT_LINES,
            "description": "History lines status shows above the screen.",
        },
        "pattern": {
            "type": "string",
            "description": (
                "For wait: a case-insensitive regular expression (Python syntax), matched line "
                "by line; the wait ends when the output matches it, such as a server's ready "
                "line. Output printed before the call counts, except output before your last "
                "input and the echo of that input. Omit to wait for the exit, or for an "
                "interactive program's new output to settle."
            ),
        },
        "timeout": {
            "type": "number",
            "minimum": 0,
            "default": TERMINAL_WAIT_DEFAULT_SECONDS,
            "description": (
                f"For wait: the longest wait in seconds, at most {TERMINAL_WAIT_MAX_SECONDS}; "
                f"0 for {TERMINAL_WAIT_MAX_SECONDS}. The program keeps running when the wait "
                "ends."
            ),
        },
    },
    "required": ["action"],
}
# Accepted and validated, never advertised: milliseconds from other harnesses,
# history paging the results address themselves, the screen guard and wait
# baseline the terminal tracks itself, and a start size (the size follows the
# user's view of the terminal).
TERMINAL_UNADVERTISED_PARAMETERS: JsonObject = {
    "timeout_ms": {"type": "number", "minimum": 0},
    "start_line": {"type": "integer", "minimum": 0},
    "expected_screen_revision": {"type": "integer", "minimum": 0},
    "after_revision": {"type": "integer", "minimum": 0},
    "columns": {
        "type": "integer",
        "minimum": TERMINAL_MIN_COLUMNS,
        "maximum": TERMINAL_MAX_COLUMNS,
    },
    "rows": {"type": "integer", "minimum": TERMINAL_MIN_ROWS, "maximum": TERMINAL_MAX_ROWS},
}


def project_terminal_tool_definitions(definitions: list[JsonObject]) -> list[JsonObject]:
    """Fit the terminal definition to one request: the Tools offered with it.

    The shell Tool is named by its Model name, and only when it is offered.
    """
    offered = frozenset(str(definition.get("name")) for definition in definitions)
    if TERMINAL_TOOL_NAME not in offered or BASH_TOOL_NAME in offered:
        return definitions
    description = _terminal_description(shell=None)
    projected: list[JsonObject] = []
    for definition in definitions:
        if definition.get("name") != TERMINAL_TOOL_NAME:
            projected.append(definition)
            continue
        fitted = deepcopy(definition)
        fitted["description"] = description
        properties = fitted.get("parameters", {}).get("properties", {})
        if "terminal_id" in properties:
            properties["terminal_id"]["description"] = _terminal_id_description(None)
        projected.append(fitted)
    return projected


def make_terminal_handler(terminal_manager: TerminalManager, projects: ProjectStore):
    """Create a terminal Tool handler bound to one Terminal Manager."""

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await _handle_terminal(terminal_manager, projects, context, arguments)

    return handler


async def _handle_terminal(
    terminal_manager: TerminalManager,
    projects: ProjectStore,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    action = arguments["action"]
    terminal_id = arguments.get("terminal_id")
    owner = _owner(context)
    if action not in {"start", "list"} and (
        not isinstance(terminal_id, str) or not terminal_id.strip()
    ):
        return tool_failure(
            "invalid_arguments",
            f"terminal was not run: {action} needs terminal_id. "
            f"{_terminals_text(terminal_manager, owner)}",
            retryable=False,
        )

    try:
        if action == "start":
            return await _handle_start(terminal_manager, projects, context, arguments)
        if action == "list":
            return _handle_list(terminal_manager, context)
        if action == "attach":
            return _handle_attach(terminal_manager, context, arguments)
        if action == "detach":
            return _handle_detach(terminal_manager, context, arguments)
        if action == "status":
            return await _handle_status(terminal_manager, context, arguments)
        if action == "wait":
            return await _handle_wait(terminal_manager, context, arguments)
        if action == "input":
            return await _handle_input(terminal_manager, context, arguments)
        return await _handle_kill(terminal_manager, context, arguments)
    except TerminalNotFoundError:
        return tool_failure(
            "terminal_not_found",
            _not_found_message(terminal_manager, context, str(terminal_id)),
            retryable=False,
        )
    except TerminalIsCommandError:
        # Only detach reaches here: another Session's commands are not visible, and
        # attaching one's own command changes nothing.
        return tool_failure(
            "command_terminal", _command_refusal(context, str(terminal_id)), retryable=False
        )
    except TerminalAlreadyAttachedError:
        return tool_failure(
            "terminal_already_attached",
            f"{terminal_id} is attached to another Session, and a terminal is attached to one "
            "Session at a time. Nothing was changed. To read its screen without attaching it, "
            f"call terminal {_call('status', str(terminal_id))}.",
            retryable=False,
        )
    except TerminalNotAttachedError:
        return tool_failure(
            "terminal_not_attached",
            f"{terminal_id} is not attached to this Session, so there is nothing to detach. "
            "Nothing was changed.",
            retryable=False,
        )
    except TerminalNotOwnedError:
        return tool_failure(
            "terminal_not_owned",
            _not_owned_message(terminal_manager, str(terminal_id), action),
            retryable=False,
        )
    except TerminalClosedError:
        return tool_failure(
            "terminal_closed",
            _closed_message(terminal_manager, context, str(terminal_id), action),
            retryable=False,
        )
    except TerminalCapacityError as error:
        return tool_failure("terminal_capacity", _capacity_message(error), retryable=True)
    except TerminalStaleScreenError:
        return tool_failure(
            "stale_screen",
            f"The screen of {terminal_id} changed after the revision you passed, so the input "
            f"was not sent. Read the current screen with terminal "
            f"{_call('status', str(terminal_id))}, then send the input again if it still fits.",
            retryable=True,
        )
    except ProjectWorkdirError as error:
        code = "invalid_arguments" if error.code == "invalid_workdir" else error.code
        return tool_failure(code, f"No terminal was started: {error}", retryable=False)
    except TerminalLaunchError as error:
        cause = error.__cause__
        if isinstance(cause, FileNotFoundError):
            return tool_failure(
                "terminal_command_not_found",
                _not_found_program_message(cause, arguments),
                retryable=False,
            )
        detail = cause if cause is not None else error
        return tool_failure(
            "terminal_launch_failed",
            f"No terminal was started: the program could not start ({detail}). Check command, "
            "args and workdir, then start again.",
            retryable=False,
        )
    except FileNotFoundError as error:
        return tool_failure(
            "terminal_command_not_found",
            _not_found_program_message(error, arguments),
            retryable=False,
        )
    except ToolContractError as error:
        return tool_failure("invalid_arguments", str(error), retryable=False)
    except TerminalManagerError as error:
        message = str(error).rstrip(". ") + "."
        if isinstance(terminal_id, str) and terminal_id:
            if action == "kill":
                message += f" To try again, call terminal {_call('kill', terminal_id)}."
            message += (
                f" To see whether {terminal_id} still runs, call terminal "
                f"{_call('status', terminal_id)}."
            )
        return tool_failure("terminal_failed", message, retryable=True)


async def _handle_start(
    terminal_manager: TerminalManager,
    projects: ProjectStore,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    notes: list[str] = []
    requested_id = arguments.get("terminal_id")
    if isinstance(requested_id, str) and requested_id.strip():
        # A live terminal can be what the Agent means to use; a finished one cannot be.
        if any(
            item.terminal_id == requested_id and not item.finished
            for item in terminal_manager.list_terminals()
        ):
            raise ToolContractError(
                f"terminal was not run: start opens a new terminal. To type into "
                f'{requested_id}, call input with terminal_id "{requested_id}"; to start '
                "another terminal, omit terminal_id."
            )
    else:
        requested_id = None
    raw_command = arguments.get("command")
    args = _optional_string_array(arguments.get("args"), field_name="args")
    environment = await asyncio.to_thread(terminal_environment)
    if raw_command in (None, ""):
        if args:
            raise ToolContractError(
                "terminal was not run: args needs command, the program to start with them."
            )
        argv = default_terminal_argv(environment)
    else:
        command = required_string(raw_command, field_name="command")
        argv = [command, *args]
        words = _split_command_line(command, environment)
        if words is not None:
            argv = [*words, *args]
            notes.append(
                f'command "{command}" is a whole command line, so it started as command '
                f'"{words[0]}" with args {json.dumps(words[1:], ensure_ascii=False)}.'
            )
    columns = optional_int(
        arguments.get("columns"),
        field_name="columns",
        default=TERMINAL_DEFAULT_COLUMNS,
        minimum=TERMINAL_MIN_COLUMNS,
        maximum=TERMINAL_MAX_COLUMNS,
    )
    rows = optional_int(
        arguments.get("rows"),
        field_name="rows",
        default=TERMINAL_DEFAULT_ROWS,
        minimum=TERMINAL_MIN_ROWS,
        maximum=TERMINAL_MAX_ROWS,
    )
    text = arguments.get("text")
    if text == "":
        text = None
    if text is not None and (not isinstance(text, str) or not text.strip()):
        raise ToolContractError("terminal was not run: text must be non-empty text when given.")
    raw_workdir = arguments.get("workdir")
    workdir_value = optional_string(raw_workdir, field_name="workdir")
    if raw_workdir == "":
        workdir_value = None
    workdir = _resolve_workdir(projects, context, workdir_value)
    if not workdir.is_dir():
        # Checked here, before a named group is created for the terminal.
        raise ToolContractError(
            f"No terminal was started: workdir {model_path(workdir)} is not a directory. Pass "
            "an existing directory, or omit workdir to use the working directory."
        )
    raw_name = arguments.get("name")
    name = optional_string(raw_name, field_name="name")
    if raw_name == "":
        name = None
    if name is not None:
        name = name.strip()
        if not name:
            raise ToolContractError("terminal was not run: name must not be blank; omit it.")
    owner = _owner(context)
    group_id = None
    raw_group = arguments.get("group")
    if raw_group == "":
        raw_group = None
    if raw_group is not None:
        if not isinstance(raw_group, str) or not raw_group.strip():
            raise ToolContractError("terminal was not run: group must be a non-empty name.")
        group_id = terminal_manager.resolve_or_create_agent_group(raw_group.strip()).group_id
    started = await terminal_manager.spawn(
        owner,
        argv,
        cwd=workdir,
        env=None,
        columns=columns,
        rows=rows,
        origin_run_id=context.run_id,
        execution_owner=context.execution_owner,
        name=name,
        initial_text=text if isinstance(text, str) else None,
        group_id=group_id,
    )
    terminal_id = started.terminal_id
    await terminal_manager.wait_for_startup(terminal_id)
    snapshot = await terminal_manager.snapshot(terminal_id, owner)
    _acknowledge_after_persistence(terminal_manager, context, owner, snapshot)
    data = _screen_result(snapshot, view="start")
    if snapshot.get("initial_input_pending"):
        notes.append(
            "text is not typed yet: it is typed and submitted once the program's screen stops "
            "changing."
        )
    elif snapshot.get("initial_output_pending"):
        notes.append(
            "the text was submitted; its output is not on this screen yet. To see it, call "
            f"terminal {_call('wait', terminal_id)}; do not send it again."
        )
    if requested_id is not None:
        notes.append(f"start assigns the terminal_id: use {terminal_id}, not {requested_id}.")
    data = _with_notes(data, notes)
    if data["state"] == "running":
        data["next"] = _DELIVERIES_TEXT
    return tool_success(data)


def _handle_list(terminal_manager: TerminalManager, context: ToolContext) -> JsonObject:
    owner = _owner(context)
    rows: list[JsonObject] = []
    for info in terminal_manager.list_terminals():
        if not _visible(info, owner):
            continue
        row: JsonObject = {
            "terminal_id": info.terminal_id,
            "program": _short_program(info.program),
            "name": info.name,
            "state": _agent_state(info),
            "exit_code": info.exit_code if info.finished else None,
            "attached": _attachment(info, owner),
        }
        rows.append({key: value for key, value in row.items() if value not in (None, "")})
    return tool_success({"terminals": rows})


def _handle_attach(
    terminal_manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    terminal_id = required_string(arguments.get("terminal_id"), field_name="terminal_id")
    owner = _owner(context)
    _visible_terminal(terminal_manager, owner, terminal_id)
    info, changed = terminal_manager.attach(
        terminal_id,
        owner,
        origin_run_id=context.run_id,
        execution_owner=context.execution_owner,
    )
    data = _terminal_facts(info, owner)
    if info.kind == "command":
        data["note"] = "A command stays attached to the Session that ran it; nothing changed."
    elif not changed:
        data["note"] = "The terminal was already attached to this Session; nothing changed."
    elif data["state"] == "running":
        data["next"] = _DELIVERIES_TEXT
    return tool_success(data)


def _handle_detach(
    terminal_manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    terminal_id = required_string(arguments.get("terminal_id"), field_name="terminal_id")
    owner = _owner(context)
    _visible_terminal(terminal_manager, owner, terminal_id)
    info = terminal_manager.detach(terminal_id, owner)
    return tool_success(_terminal_facts(info, owner))


async def _handle_status(
    terminal_manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    terminal_id = required_string(arguments.get("terminal_id"), field_name="terminal_id")
    lines = optional_int(
        arguments.get("lines"),
        field_name="lines",
        default=TERMINAL_STATUS_DEFAULT_LINES,
        minimum=1,
        maximum=TERMINAL_STATUS_MAX_LINES,
    )
    assert lines is not None
    start_line = optional_int(
        arguments.get("start_line"),
        field_name="start_line",
        default=None,
        minimum=0,
    )
    owner = _owner(context)
    info = _visible_terminal(terminal_manager, owner, terminal_id)
    if info.kind == "command":
        data = await _command_result(terminal_manager, context, terminal_id)
        if start_line is not None or "lines" in arguments:
            data["note"] = (
                "lines and start_line page an interactive terminal's screen; a command's result "
                "shows its output and names the file with its complete output when it shows "
                "only part."
            )
        return tool_success(data)
    snapshot = await terminal_manager.read(terminal_id, lines=lines, start_line=start_line)
    attached_here = info.attachment == owner
    if attached_here and start_line is None:
        _acknowledge_after_persistence(terminal_manager, context, owner, snapshot)
    data = _screen_result(
        snapshot, view="status", page_lines=lines, include_screen=start_line is None
    )
    if not attached_here:
        data["attached"] = _attachment(info, owner)
        data["next"] = _not_attached_text(info, terminal_id)
    return tool_success(data)


async def _handle_wait(
    terminal_manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    terminal_id = required_string(arguments.get("terminal_id"), field_name="terminal_id")
    owner = _owner(context)
    info = _visible_terminal(terminal_manager, owner, terminal_id)
    seconds, notes, capped = _wait_seconds(
        arguments, default=TERMINAL_WAIT_DEFAULT_SECONDS, zero=TERMINAL_WAIT_MAX_SECONDS
    )
    pattern, pattern_notes = _wait_pattern(arguments)
    notes.extend(pattern_notes)
    after_revision = optional_int(
        arguments.get("after_revision"),
        field_name="after_revision",
        default=None,
        minimum=0,
    )
    matched: list[str] = []
    ended = await _wait_unless_released(
        context,
        terminal_manager.wait(
            terminal_id,
            owner,
            seconds=seconds,
            pattern=pattern,
            after_revision=after_revision,
            on_match=matched.append,
        ),
    )
    command = info.kind == "command"
    if ended == "user":
        notes.append("The user ended this wait; the program keeps running.")
    elif ended == "timeout" and pattern is not None:
        notes.append(f"No output matched pattern within {seconds:g} seconds.")
    elif ended == "timeout" and command:
        notes.append(f"The command did not exit within {seconds:g} seconds.")
    if capped and ended == "timeout":
        notes.append(_capped_wait_note(command=command))
    if command:
        data = await _command_result(terminal_manager, context, terminal_id, wait_ended=ended)
    else:
        snapshot = await terminal_manager.snapshot(terminal_id, owner, include_name=False)
        _acknowledge_after_persistence(terminal_manager, context, owner, snapshot)
        data = _screen_result(snapshot, view="wait")
        data["wait_ended"] = ended
    if matched:
        data["matched"] = _matched_line(matched[0])
    return tool_success(_with_notes(data, notes))


async def _wait_unless_released(context: ToolContext, waiting: Awaitable[str]) -> str:
    """How *waiting* ended, or ``user`` when the user ended the wait first."""
    released = asyncio.Event()

    def release() -> bool:
        if released.is_set():
            return False
        released.set()
        return True

    if context.background_registration_hook is not None:
        context.background_registration_hook(release)
    wait = asyncio.ensure_future(waiting)
    user = asyncio.ensure_future(released.wait())
    try:
        await asyncio.wait({wait, user}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (wait, user):
            task.cancel()
        await asyncio.gather(wait, user, return_exceptions=True)
    if wait.done() and not wait.cancelled():
        return wait.result()
    return "user"


def _matched_line(line: str) -> str:
    """The line a pattern matched, as a result shows it."""
    line = line.strip()
    if len(line) <= _MATCHED_LINE_CHARS:
        return line
    return line[: _MATCHED_LINE_CHARS - 3] + "..."


async def _handle_input(
    terminal_manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    terminal_id = required_string(arguments.get("terminal_id"), field_name="terminal_id")
    raw_data = arguments.get("data")
    if raw_data is not None and not isinstance(raw_data, str):
        raise ToolContractError("terminal was not run: data must be text.")
    if raw_data == "":
        raw_data = None
    text = arguments.get("text")
    if text is not None and not isinstance(text, str):
        raise ToolContractError("terminal was not run: text must be text.")
    if text == "":
        text = None
    key = optional_string(arguments.get("key"), field_name="key")
    if key == "":
        key = None
    if raw_data is not None and (text is not None or key is not None):
        raise ToolContractError(
            "terminal was not run: data is sent exactly as given, so it cannot be combined "
            'with text or key; put the whole sequence in data (end it with "\\r" to press '
            "Enter), or send text and key without data."
        )
    if key is not None and key not in TERMINAL_KEYS:
        raise ToolContractError(
            f'terminal was not run: key "{key}" is not a named key. Named keys: '
            f"{TERMINAL_KEY_SUMMARY}. Type other characters as text, or send exact sequences "
            "as data."
        )
    # A pattern or a longer wait after input is accepted for other harnesses'
    # habits, not advertised; a pattern makes input wait as wait does.
    pattern, pattern_notes = _wait_pattern(arguments)
    seconds, notes, capped = _wait_seconds(
        arguments,
        default=TERMINAL_WAIT_DEFAULT_SECONDS if pattern is not None else TERMINAL_REPLY_SECONDS,
    )
    notes.extend(pattern_notes)
    expected_revision = optional_int(
        arguments.get("expected_screen_revision"),
        field_name="expected_screen_revision",
        default=None,
        minimum=0,
    )
    owner = _owner(context)
    _visible_terminal(terminal_manager, owner, terminal_id)
    info = terminal_manager.terminal(terminal_id, owner)
    prior_attention_revision = info.attention_revision if info.attention is not None else None
    revision_before_input = info.attention_revision
    sent = await terminal_manager.send_input(
        terminal_id,
        owner,
        data=raw_data if isinstance(raw_data, str) else None,
        text=text if isinstance(text, str) else None,
        key=key,
        expected_screen_revision=expected_revision,
        origin_run_id=context.run_id,
        execution_owner=context.execution_owner,
    )
    if not sent["characters_sent"]:
        return tool_success(
            {
                "terminal_id": terminal_id,
                "note": "nothing was typed, because text, key and data were empty. To see what "
                f"the terminal shows, call terminal {_call('status', terminal_id)}.",
            }
        )
    if prior_attention_revision is not None:
        context.after_result_persisted(
            lambda: terminal_manager.acknowledge_attention(
                terminal_id, owner, prior_attention_revision
            )
        )
    typed: JsonObject = {}
    if sent.get("key"):
        typed["key"] = sent["key"]
    if isinstance(text, str) and ("\n" in text or "\r" in text):
        # Whether several lines arrived as one pasted block or line by line.
        typed["bracketed_paste"] = bool(sent.get("bracketed_paste"))
    if isinstance(text, str) and key is None and raw_data is None:
        notes.insert(
            0,
            "text was typed but not submitted. To submit it, call terminal "
            + json.dumps({"action": "input", "terminal_id": terminal_id, "key": "enter"})
            + ".",
        )
    matched: list[str] = []
    if pattern is not None:
        ended = await terminal_manager.wait(
            terminal_id,
            owner,
            seconds=seconds,
            pattern=pattern,
            after_revision=revision_before_input,
            on_match=matched.append,
        )
    else:
        # The reply: the output settles after the input, as start's first screen does.
        ended = await terminal_manager.wait_for_reply(
            terminal_id, owner, seconds=seconds, after_quiet=int(sent["quiet_boundaries"])
        )
    if capped and ended == "timeout":
        notes.append(_capped_wait_note(command=info.kind == "command"))
    if info.kind == "command":
        data = await _command_result(
            terminal_manager,
            context,
            terminal_id,
            wait_ended=ended if pattern is not None else None,
        )
    else:
        snapshot = await terminal_manager.snapshot(terminal_id, owner, include_name=False)
        _acknowledge_after_persistence(terminal_manager, context, owner, snapshot)
        data = _screen_result(snapshot, view="input")
        if pattern is not None:
            data["wait_ended"] = ended
        elif ended == "timeout":
            data["next"] = _reply_pending_text(terminal_id)
    if matched:
        data["matched"] = _matched_line(matched[0])
    result = _with_notes({**data, **typed}, notes)
    if "next" in result:
        # What to do next closes the result, after what was typed.
        result["next"] = result.pop("next")
    return tool_success(result)


async def _handle_kill(
    terminal_manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    terminal_id = required_string(arguments.get("terminal_id"), field_name="terminal_id")
    owner = _owner(context)
    before = _visible_terminal(terminal_manager, owner, terminal_id)
    report = terminal_manager.command_report(terminal_id) if before.kind == "command" else None
    after = await terminal_manager.kill(terminal_id, owner)
    context.after_result_persisted(lambda: terminal_manager.acknowledge_exit(terminal_id, owner))
    if after.kind == "command":
        result = await _command_result(terminal_manager, context, terminal_id)
        if report is not None and report.exited:
            if report.still_running and not before.finished:
                names = ", ".join(
                    f"{process.name} (pid {process.pid})" for process in report.still_running
                )
                result["note"] = (
                    f"the shell had already exited; stopped the processes it left running: {names}."
                )
            else:
                result["note"] = "the command had already ended; nothing was stopped."
        return tool_success(result)
    data: JsonObject = {"terminal_id": terminal_id, "state": _agent_state(after)}
    if after.exit_code is not None:
        data["exit_code"] = after.exit_code
    if before.finished:
        snapshot = await terminal_manager.read(terminal_id)
        data["screen"] = snapshot.get("screen") or ""
        data["note"] = "The program had already ended; nothing was stopped."
    return tool_success(data)


# Command terminals


async def _command_result(
    terminal_manager: TerminalManager,
    context: ToolContext,
    terminal_id: str,
    *,
    wait_ended: str | None = None,
) -> JsonObject:
    """The shell result of a command terminal; an end it shows is not delivered again."""
    # Imported here: the shell Tool owns the result of a command and imports the manager.
    from core.tools.shell import command_terminal_result

    data = await command_terminal_result(
        terminal_manager, context, terminal_id, wait_ended=wait_ended
    )
    if data.get("status") in {"exited", "stopped"}:
        owner = _owner(context)
        context.after_result_persisted(
            lambda: terminal_manager.acknowledge_exit(terminal_id, owner)
        )
    return data


# What an attached interactive program sends on its own; Agents polled for it.
_DELIVERIES_TEXT = (
    "While it runs, its screen arrives as a new message each time its output settles after "
    "activity, and so does its exit. Continue with your next step instead of polling with status."
)


def _reply_pending_text(terminal_id: str) -> str:
    """What to do when an interactive program still printed output as input's wait ended."""
    return (
        "Its screen arrives as a new message when its output settles; continue other work or "
        f"end your turn. To wait for it now instead, call terminal {_call('wait', terminal_id)}."
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
        "kind",
        "command",
        "arguments",
        "initial_input_pending",
        "initial_output_pending",
    }
)
_ALTERNATE_SCREEN_NOTE = (
    "The program shows a full-screen view (alternate screen), which keeps no terminal "
    "history; to see earlier content, use the program's own scrolling or paging."
)
# Launch facts: status shows them; other results do not repeat them.
_LAUNCH_FIELDS = ("program", "title", "workdir", "log", "name")


def _log_text(path: str) -> str:
    """What an interactive terminal's log file holds; a bare path did not say."""
    return (
        f"Everything the program printed is written live to {path}, as raw text with its "
        "terminal control sequences."
    )


def _screen_result(
    snapshot: dict[str, Any],
    *,
    view: str,
    page_lines: int = TERMINAL_STATUS_DEFAULT_LINES,
    include_screen: bool = True,
) -> JsonObject:
    """Shape an interactive terminal's snapshot for one action's result.

    History text becomes its own multi-line ``history`` field; ``scrollback``
    keeps the line numbers and the requests for the adjacent pages, and
    appears only when there is history or another page to read.
    """
    projected: JsonObject = {
        "terminal_id": snapshot["terminal_id"],
        "state": _agent_state_of(snapshot),
    }
    if projected["state"] != "running" and snapshot.get("exit_code") is not None:
        projected["exit_code"] = snapshot["exit_code"]
    for key, value in snapshot.items():
        if key in _HIDDEN_SNAPSHOT_FIELDS or key in projected or key == "state":
            continue
        if value is None or value == "" or key == "exit_code":
            continue
        if key == "log_file":
            projected["log"] = _log_text(str(value))
            continue
        projected[key] = value
    if view != "status":
        for key in _LAUNCH_FIELDS:
            projected.pop(key, None)
    screen = projected.pop("screen", None)
    page = dict(projected.pop("scrollback", None) or {})
    history = page.pop("text", "")
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
    if not include_screen or history or "older_request" in scrollback:
        projected["scrollback"] = scrollback
    if view == "status" and snapshot.get("alternate_screen"):
        projected["note"] = _ALTERNATE_SCREEN_NOTE
    return projected


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
    if info.attachment == owner:
        return "here"
    return "no" if info.attachment is None else "other"


def _terminal_facts(info: TerminalInfo, owner: TerminalOwner) -> JsonObject:
    facts: JsonObject = {
        "terminal_id": info.terminal_id,
        "program": _short_program(info.program),
        "name": info.name,
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


def _capped_wait_note(*, command: bool) -> str:
    """A capped wait timed out; a command's result arrives without another wait."""
    note = (
        f"A wait lasts at most {TERMINAL_WAIT_MAX_SECONDS} seconds, so this one ended after "
        f"{TERMINAL_WAIT_MAX_SECONDS}"
    )
    return f"{note}." if command else f"{note}; wait again to keep following the program."


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
    label = " ".join((info.name or info.program).split())
    if len(label) > _TERMINAL_LABEL_CHARS:
        return label[: _TERMINAL_LABEL_CHARS - 3] + "..."
    return label


def _attached_terminals(
    terminal_manager: TerminalManager, owner: TerminalOwner
) -> list[TerminalInfo]:
    """The terminals attached to this Session, running ones first, newest first."""
    return sorted(
        (
            info
            for info in reversed(terminal_manager.list_terminals())
            if _visible(info, owner) and info.attachment == owner
        ),
        key=lambda info: info.finished,
    )


def _terminals_text(terminal_manager: TerminalManager, owner: TerminalOwner) -> str:
    """Name the terminals attached to this Session, running ones first, newest first."""
    attached = _attached_terminals(terminal_manager, owner)
    if not attached:
        if any(_visible(info, owner) for info in terminal_manager.list_terminals()):
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
    terminal_manager: TerminalManager, context: ToolContext, terminal_id: str
) -> str:
    owner = _owner(context)
    attached = _attached_terminals(terminal_manager, owner)
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
        f"No terminal with the id {terminal_id} is open for this Session: the id is wrong or "
        f"belongs to another Session, or its program ended more than {_FINISHED_MINUTES} "
        f"minutes ago and the terminal was removed, with its output.{named} To see all "
        'terminals, call terminal {"action": "list"}.'
    )


def _not_owned_message(terminal_manager: TerminalManager, terminal_id: str, action: str) -> str:
    info = next(
        (item for item in terminal_manager.list_terminals() if item.terminal_id == terminal_id),
        None,
    )
    status = f"terminal {_call('status', terminal_id)}"
    if info is not None and info.attachment is not None:
        return (
            f"{terminal_id} is attached to another Session; only that Session can wait for it, "
            f"type into it or kill it. Nothing was done. To read its screen, call {status}."
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


def _closed_message(
    terminal_manager: TerminalManager, context: ToolContext, terminal_id: str, action: str
) -> str:
    info = next(
        (item for item in terminal_manager.list_terminals() if item.terminal_id == terminal_id),
        None,
    )
    ended = "has ended"
    if info is not None and info.exit_code is not None:
        # A command's shell can end before processes it started, which keep its terminal live.
        state = _state_text("error" if info.state == "error" else "exited", stopped=info.stopped)
        ended = f"has ended ({state}, exit code {info.exit_code})"
    done = "the input was not sent" if action == "input" else f"{action} was not done"
    message = (
        f"The program in {terminal_id} {ended}, so {done}. To read its last screen or output, "
        f"call terminal {_call('status', terminal_id)}."
    )
    if info is not None and info.kind == "command" and context.offers(BASH_TOOL_NAME):
        message += f" To run the command again, use {_SHELL_NAME}."
    elif info is not None and info.kind == "terminal":
        message += ' To run the program again, call terminal with action "start".'
    return message


def _command_refusal(context: ToolContext, terminal_id: str) -> str:
    origin = f" that {_SHELL_NAME} left running" if context.offers(BASH_TOOL_NAME) else ""
    return (
        f"{terminal_id} runs a command{origin}, and a command stays attached to the Session "
        "that ran it so its result arrives there. Nothing was changed. To stop it, call "
        f"terminal {_call('kill', terminal_id)}."
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


def _not_found_program_message(error: FileNotFoundError, arguments: JsonObject) -> str:
    program = error.filename or arguments.get("command") or "the requested program"
    return (
        f"Program {program} was not found, so no terminal was started. Pass the program's full "
        "path as command, or omit command to start the user's default shell and run it there."
    )


# Access


def _visible(info: TerminalInfo, owner: TerminalOwner) -> bool:
    """Interactive terminals are shared; a command belongs to the Session that ran it."""
    return info.kind != "command" or owner in (info.attachment, info.lifecycle_owner)


def _visible_terminal(
    terminal_manager: TerminalManager, owner: TerminalOwner, terminal_id: str
) -> TerminalInfo:
    for info in terminal_manager.list_terminals():
        if info.terminal_id == terminal_id and _visible(info, owner):
            return info
    raise TerminalNotFoundError(f"Terminal Session not found: {terminal_id}")


def _acknowledge_after_persistence(
    terminal_manager: TerminalManager,
    context: ToolContext,
    owner: TerminalOwner,
    snapshot: dict[str, Any],
) -> None:
    """Once the result is durable, its screen and the attention it showed are delivered."""
    attention = snapshot.get("attention")
    terminal_id = str(snapshot["terminal_id"])
    observation = snapshot["observation"]
    ended = snapshot.get("state") in {"exited", "error"}

    def acknowledge() -> None:
        if not terminal_manager.acknowledge_screen(terminal_id, owner, observation):
            return
        if isinstance(attention, dict) and isinstance(attention.get("revision"), int):
            terminal_manager.acknowledge_attention(terminal_id, owner, attention["revision"])
        if ended:
            terminal_manager.acknowledge_exit(terminal_id, owner)

    context.after_result_persisted(acknowledge)


def _optional_string_array(value: object, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ToolContractError(f"terminal was not run: {field_name} must be an array of strings.")
    if any("\x00" in item for item in value):
        raise ToolContractError(
            f"terminal was not run: {field_name} must not contain NUL characters."
        )
    return list(value)


def _resolve_workdir(
    projects: ProjectStore,
    context: ToolContext,
    workdir_value: str | None,
) -> Path:
    if workdir_value is None:
        return context.effective_cwd.resolve()
    if is_project_workdir(workdir_value):
        return project_workdir(projects, workdir_value)
    return context.resolve_path(workdir_value)


def _owner(context: ToolContext) -> TerminalOwner:
    return TerminalOwner(context.project_id, context.agent_id, context.session_id)


def register_terminal_tool(
    registry: ToolRegistry,
    terminal_manager: TerminalManager,
    projects: ProjectStore,
) -> None:
    """Register the Agent-facing terminal Tool."""
    registry.register(
        TERMINAL_TOOL_NAME,
        TERMINAL_TOOL_DESCRIPTION,
        TERMINAL_TOOL_PARAMETERS,
        make_terminal_handler(terminal_manager, projects),
        summary="Drive interactive programs such as REPLs and coding-agent CLIs.",
        family="execution",
        open_input_schema=True,
        unadvertised_parameters=TERMINAL_UNADVERTISED_PARAMETERS,
        argument_normalizer=normalize_terminal_arguments,
        result_schema={"type": "object"},
        display=ToolDisplay(
            parts_builder=_terminal_display_parts,
            fact_builder=result_count_fact_builder("terminals", when_arguments={"action": "list"}),
            detail_builder=_terminal_detail_blocks,
        ),
    )


# Display

_ATTACHMENT_TEXT = {
    "here": "attached here",
    "other": "attached to another conversation",
    "no": "not attached",
}


def _visible_input(data: str) -> str:
    """Spell out control characters, such as Escape, that exact input data can hold."""
    return "".join(
        char
        if char in "\n\t" or (char >= " " and char != "\x7f")
        else char.encode("unicode_escape").decode("ascii")
        for char in data
    )


def _typed_input(arguments: JsonObject) -> str:
    """Return what a start or input call types, with a named key shown as ``<key>``."""
    data = arguments.get("data")
    if isinstance(data, str) and data:
        return _visible_input(data)
    text = arguments.get("text")
    key = arguments.get("key")
    typed = text if isinstance(text, str) else ""
    if isinstance(key, str) and key:
        typed = f"{typed} <{key}>" if typed else f"<{key}>"
    return typed


def _terminal_state_text(item: Mapping[str, Any]) -> str:
    state = item.get("state") or item.get("status")
    exit_code = item.get("exit_code")
    if state == "exited" and isinstance(exit_code, int) and not isinstance(exit_code, bool):
        return f"exited with code {exit_code}"
    return str(state or "")


def _terminal_detail_blocks(arguments: JsonObject, result: JsonObject | None) -> list[JsonObject]:
    """Show the user what the Agent typed, the terminal's screen and how its program ended.

    Paging requests and notes are for the Agent and stay in the raw result.
    """
    try:
        normalized = normalize_terminal_arguments(arguments)
    except ValueError:
        normalized = arguments
    call = normalized if isinstance(normalized, dict) else {}
    action = call.get("action")
    blocks: list[JsonObject] = []
    typed = _typed_input(call) if action in {"start", "input"} else ""
    if typed.strip():
        blocks.append(display_text("input", text=typed))
    data = result.get("data") if isinstance(result, dict) and result.get("ok") is True else None
    if not isinstance(data, dict):
        return blocks
    terminals = data.get("terminals")
    if isinstance(terminals, list):
        listed = [item for item in terminals if isinstance(item, dict)]
        if not listed:
            return [*blocks, display_notice("info", "No terminal is open.")]
        items = [
            {
                "title": item.get("name") or item.get("program") or item.get("terminal_id"),
                "meta": " · ".join(
                    part
                    for part in (
                        _terminal_state_text(item),
                        _ATTACHMENT_TEXT.get(str(item.get("attached")), ""),
                    )
                    if part
                ),
            }
            for item in listed
        ]
        return [*blocks, display_results(items)]
    if "history" in data:
        blocks.append(display_text("scrollback", source="result", path=("data", "history")))
    if "screen" in data:
        blocks.append(display_text("screen", source="result", path=("data", "screen")))
    if "output" in data:
        blocks.append(display_text("output", source="result", path=("data", "output")))
    state = data.get("state") or data.get("status")
    exit_code = data.get("exit_code")
    if state == "stopped":
        blocks.append(display_notice("info", "The program was stopped."))
    elif state == "failed":
        blocks.append(display_notice("warning", "The terminal stopped with an error."))
    elif state == "exited" and isinstance(exit_code, int) and exit_code != 0:
        blocks.append(display_notice("warning", f"The program exited with code {exit_code}."))
    elif state == "exited":
        blocks.append(display_notice("info", "The program exited."))
    elif data.get("wait_ended") == "timeout":
        blocks.append(display_notice("info", "The program was still running when the wait ended."))
    return blocks


def _terminal_display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    try:
        normalized = normalize_terminal_arguments(arguments)
    except ValueError:
        normalized = arguments
    if not isinstance(normalized, dict):
        return ()
    arguments = normalized
    action = arguments.get("action")
    if not isinstance(action, str) or action not in TERMINAL_ACTIONS:
        return ()
    parts = [ToolDisplayPart(action, truncate="never", tooltip="none")]
    terminal_id = arguments.get("terminal_id")
    if isinstance(terminal_id, str) and terminal_id:
        parts.append(ToolDisplayPart(terminal_id, kind="identifier", truncate="middle"))
        return tuple(parts)
    command = arguments.get("command")
    if action == "start":
        if isinstance(command, str) and command:
            parts.append(ToolDisplayPart(command, kind="command", copyable=True))
        else:
            parts.append(ToolDisplayPart("default shell", kind="command"))
    return tuple(parts)


__all__ = [
    "TERMINAL_ACTIONS",
    "TERMINAL_KEYS",
    "TERMINAL_TOOL_DESCRIPTION",
    "TERMINAL_TOOL_NAME",
    "TERMINAL_TOOL_PARAMETERS",
    "TERMINAL_UNADVERTISED_PARAMETERS",
    "TERMINAL_WAIT_DEFAULT_SECONDS",
    "TERMINAL_WAIT_MAX_SECONDS",
    "make_terminal_handler",
    "project_terminal_tool_definitions",
    "register_terminal_tool",
]
