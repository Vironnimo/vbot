"""Agent-facing live terminals backed by PTY/ConPTY.

The ``terminal`` Tool starts interactive programs that an Agent drives by
typing. No shell command runs in a terminal: the shell Tool runs commands
without one, and ``background_tasks`` serves the ones it left running, so the
``terminal`` Tool refuses their ids and those of Sub-Agents.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from collections.abc import Awaitable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.projects import ProjectStore
from core.sessions import SessionAddress
from core.storage.temp_files import TEMPORARY_FILE_RETENTION
from core.tools._background_tasks_arguments import (
    BACKGROUND_TASKS_TOOL_NAME,
    COMMAND_ID_PREFIX,
    SUBAGENT_ID_PREFIX,
)
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
from core.tools.scratch_areas import ScratchAreas, ScratchUnavailableError
from core.tools.shell_environment import scratch_environment, terminal_environment
from core.tools.terminal_backend import UnpassableArgumentError, default_terminal_argv
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
    TERMINAL_TEMPORARY_CATEGORY,
    TerminalAlreadyAttachedError,
    TerminalCapacityError,
    TerminalClosedError,
    TerminalInfo,
    TerminalLaunchError,
    TerminalManager,
    TerminalManagerError,
    TerminalNotAttachedError,
    TerminalNotFoundError,
    TerminalNotOwnedError,
    TerminalOwner,
    TerminalStaleScreenError,
    screen_behind_note,
    unseen_lines_note,
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
from core.utils.logging import get_logger
from core.utils.paths import model_path

if TYPE_CHECKING:
    from core.subagents import SubAgentTree

_LOGGER = get_logger("tools.terminal")

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
# An argument an error names is cut to this many characters.
_ARGUMENT_CHARS = 80
_TERMINAL_LABEL_CHARS = 40
# A list row shows this much of the program's first line.
_PROGRAM_CHARS = 80
_FINISHED_MINUTES = int(TERMINAL_FINISHED_TTL.total_seconds() // 60)
_LOG_RETENTION_HOURS = int(
    TEMPORARY_FILE_RETENTION[TERMINAL_TEMPORARY_CATEGORY].total_seconds() // 3600
)
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
    sentences.append(
        "In screens, ⟦ and ⟧ mark reverse-video text, such as a selection or a drawn cursor, "
        "and ⟪ and ⟫ mark faint suggestion text after the cursor, which Enter does not submit."
    )
    return " ".join(sentences)


TERMINAL_TOOL_DESCRIPTION = _terminal_description(shell=_SHELL_NAME)

TERMINAL_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(TERMINAL_ACTIONS),
            "description": (
                "start launches a program attached to this Session and returns its first "
                "screen; list shows terminals; status shows a terminal's screen; wait waits "
                "until the program exits, prints output matching pattern, or the timeout "
                "passes; without pattern, it also ends when the program goes idle showing "
                "something new; input types text and keys; kill stops the program and "
                "everything it started; attach takes over an unattached terminal; detach "
                "releases it for another Session."
            ),
        },
        "terminal_id": {
            "type": "string",
            "description": (
                "The terminal's id from start or list. Required except for start and list."
            ),
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
                'For input, text to type; add key "enter" to submit it. Each line break '
                "presses Enter; when the program accepts bracketed paste, multiline text is "
                "pasted as one block and only a final line break presses Enter."
            ),
        },
        "key": {
            "type": "string",
            "description": (f"Named key for input, pressed after text: {TERMINAL_KEY_SUMMARY}."),
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
                "by line; the wait ends when the output matches it, such as a prompt. Output "
                "printed before the call counts, except output before your last input and the "
                "echo of that input. Omit to wait for the exit, or for the program to go idle."
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
        projected.append(fitted)
    return projected


def make_terminal_handler(
    terminal_manager: TerminalManager,
    projects: ProjectStore,
    subagents: SubAgentTree | None = None,
    *,
    scratch_areas: ScratchAreas | None = None,
):
    """Create a terminal Tool handler bound to one Terminal Manager."""

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await _handle_terminal(
            terminal_manager, projects, subagents, context, arguments, scratch_areas
        )

    return handler


async def _handle_terminal(
    terminal_manager: TerminalManager,
    projects: ProjectStore,
    subagents: SubAgentTree | None,
    context: ToolContext,
    arguments: JsonObject,
    scratch_areas: ScratchAreas | None,
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
    if isinstance(terminal_id, str):
        refusal = _background_task_refusal(context, terminal_id.strip(), action)
        if refusal is not None:
            return tool_failure("not_a_terminal", refusal, retryable=False)

    try:
        if action == "start":
            return await _handle_start(
                terminal_manager, projects, context, arguments, scratch_areas
            )
        if action == "list":
            return await _handle_list(terminal_manager, subagents, context)
        if action == "attach":
            return _handle_attach(terminal_manager, context, arguments)
        if action == "detach":
            return _handle_detach(terminal_manager, context, arguments)
        if action == "status":
            return await _handle_status(terminal_manager, subagents, context, arguments)
        if action == "wait":
            return await _handle_wait(terminal_manager, context, arguments)
        if action == "input":
            return await _handle_input(terminal_manager, context, arguments)
        return await _handle_kill(terminal_manager, context, arguments)
    except TerminalNotFoundError:
        return tool_failure(
            "terminal_not_found",
            _not_found_message(terminal_manager, owner, str(terminal_id)),
            retryable=False,
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
            _closed_message(terminal_manager, str(terminal_id), action),
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
    scratch_areas: ScratchAreas | None,
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
    # start sends no input: the program gets it once it is ready for it.
    unsent = _UnsentStartInput.of(arguments)
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
    overlay = None
    if scratch_areas is not None:
        try:
            folder = await scratch_areas.folder_for(_address(owner))
        except ScratchUnavailableError:
            # The owner logged the failure; an optional Scratch folder must not
            # prevent the Agent's program from starting.
            pass
        else:
            overlay = scratch_environment(folder)
    try:
        started = await terminal_manager.spawn(
            owner,
            argv,
            cwd=workdir,
            env=overlay,
            columns=columns,
            rows=rows,
            origin_run_id=context.run_id,
            execution_owner=context.execution_owner,
            name=name,
            group_id=group_id,
        )
    except TerminalLaunchError as error:
        cause = error.__cause__
        if isinstance(cause, UnpassableArgumentError) and cause.index is not None:
            raise ToolContractError(_unpassable_argument_message(argv[0], cause.value)) from error
        raise
    terminal_id = started.terminal_id
    try:
        await terminal_manager.wait_for_startup(terminal_id)
        snapshot = await terminal_manager.snapshot(terminal_id, owner)
    except Exception as error:
        # The program runs: a screen that cannot be read must not hide that.
        data = _unread_screen_result(
            terminal_manager, terminal_id, error, done="The program started"
        )
    else:
        _acknowledge_after_persistence(terminal_manager, context, owner, snapshot)
        data = _screen_result(snapshot, view="start", unseen_from=_unseen_from(snapshot))
    running = data["state"] == "running"
    unsent_note = unsent.note(terminal_id, running=running)
    if unsent_note is not None:
        notes.append(unsent_note)
    if requested_id is not None:
        notes.append(f"start assigns the terminal_id: use {terminal_id}, not {requested_id}.")
    data = _with_notes(data, notes)
    if running:
        data["next"] = (
            _first_screen_pending_text(terminal_id)
            if data.get("screen") == ""
            else _DELIVERIES_TEXT
        )
    return tool_success(data)


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
    if info is not None and info.finished and info.exit_code is not None:
        data["exit_code"] = info.exit_code
    data["note"] = (
        f"{done}, but its screen could not be read ({error}). To try reading it again, call "
        f"terminal {_call('status', terminal_id)}."
    )
    return data


async def _handle_list(
    terminal_manager: TerminalManager,
    subagents: SubAgentTree | None,
    context: ToolContext,
) -> JsonObject:
    owner = _owner(context)
    terminals = terminal_manager.list_terminals()
    holders = await _subagent_holders(subagents, owner, terminals)
    rows: list[JsonObject] = []
    for info in terminals:
        row: JsonObject = {
            "terminal_id": info.terminal_id,
            "program": _short_program(info.program),
            "name": info.name,
            "state": _agent_state(info),
            "exit_code": info.exit_code if info.finished else None,
            **_attachment_fields(info, owner, holders),
        }
        rows.append({key: value for key, value in row.items() if value not in (None, "")})
    return tool_success({"terminals": rows})


async def _subagent_holders(
    subagents: SubAgentTree | None, owner: TerminalOwner, terminals: Sequence[TerminalInfo]
) -> Mapping[SessionAddress, str]:
    """The Sessions of this Session's Sub-Agents (any depth) by Sub-Agent id, when one of
    *terminals* is attached to another Session.

    A Session the user took over, and every Session below it, works for the user, so
    its terminals count as another Session's.
    """
    if subagents is None or all(info.attachment in (None, owner) for info in terminals):
        return {}
    return {
        subagent.session: subagent.id
        for subagent in await subagents.subagents(_address(owner))
        if subagent.within_takeover is None
    }


def _attachment_fields(
    info: TerminalInfo, owner: TerminalOwner, holders: Mapping[SessionAddress, str]
) -> JsonObject:
    """``attached``, and ``subagent`` for a terminal a Sub-Agent of this Session holds."""
    attached = _attachment(info, owner)
    subagent = (
        holders.get(_address(info.attachment))
        if attached == "other" and info.attachment is not None
        else None
    )
    if subagent is None:
        return {"attached": attached}
    return {"attached": "subagent", "subagent": subagent}


def _handle_attach(
    terminal_manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    terminal_id = required_string(arguments.get("terminal_id"), field_name="terminal_id")
    owner = _owner(context)
    info, changed = terminal_manager.attach(
        terminal_id,
        owner,
        origin_run_id=context.run_id,
        execution_owner=context.execution_owner,
    )
    data = _terminal_facts(info, owner)
    if not changed:
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
    info = terminal_manager.detach(terminal_id, owner)
    return tool_success(_terminal_facts(info, owner))


async def _handle_status(
    terminal_manager: TerminalManager,
    subagents: SubAgentTree | None,
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
    info = _terminal_info(terminal_manager, terminal_id)
    snapshot = await terminal_manager.read(terminal_id, lines=lines, start_line=start_line)
    attached_here = info.attachment == owner
    if attached_here and start_line is None:
        _acknowledge_after_persistence(terminal_manager, context, owner, snapshot)
    data = _screen_result(
        snapshot,
        view="status",
        page_lines=lines,
        include_screen=start_line is None,
        unseen_from=None if start_line is not None else _unseen_from(snapshot, attached_here),
        unseen_by_you=attached_here,
        requested_start=start_line,
    )
    if not attached_here:
        holders = await _subagent_holders(subagents, owner, [info])
        data.update(_attachment_fields(info, owner, holders))
        data["next"] = _not_attached_text(info, terminal_id)
    return tool_success(data)


async def _handle_wait(
    terminal_manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    terminal_id = required_string(arguments.get("terminal_id"), field_name="terminal_id")
    owner = _owner(context)
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
    if ended == "user":
        notes.append("The user ended this wait; the program keeps running.")
    elif ended == "timeout" and pattern is not None:
        notes.append(f"No output matched pattern within {seconds:g} seconds.")
    if capped and ended == "timeout":
        notes.append(_capped_wait_note())
    snapshot = await terminal_manager.snapshot(terminal_id, owner, include_name=False)
    _acknowledge_after_persistence(terminal_manager, context, owner, snapshot)
    data = _screen_result(snapshot, view="wait", unseen_from=_unseen_from(snapshot))
    data["wait_ended"] = _wait_end(ended, data)
    if matched:
        data["matched"] = _matched_line(matched[0])
    return tool_success(_with_notes(data, notes))


def _wait_end(ended: str, data: JsonObject) -> str:
    """How a wait ended, as its result's state tells it.

    A program that was stopped did not exit on its own: ``exited`` beside the
    state ``stopped`` contradicted it.
    """
    return "stopped" if ended == "exited" and data.get("state") == "stopped" else ended


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
        raise ToolContractError(_unknown_key_message(key))
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
    if isinstance(text, str) and ("\n" in text or "\r" in text):
        # Whether the lines went as one pasted block, or each line break as Enter.
        typed["pasted"] = bool(sent.get("pasted"))
    if isinstance(text, str) and key is None and raw_data is None:
        submit = json.dumps({"action": "input", "terminal_id": terminal_id, "key": "enter"})
        if typed.get("pasted") is False:
            # Its line breaks pressed Enter; only the last line waits.
            unsent = "its line breaks pressed Enter; the text after the last one was typed"
        else:
            unsent = "text was typed"
        notes.insert(0, f"{unsent} but not submitted. To submit it, call terminal {submit}.")
    key_unsent = sent.get("key_unsent")
    if key_unsent == "ended":
        # The program ended on the text, as on "q" for quit: typing it again
        # would repeat what it did.
        notes.insert(0, f'text was typed, but key "{key}" was not sent: the program had ended.')
    elif key_unsent == "refused":
        press = json.dumps({"action": "input", "terminal_id": terminal_id, "key": key})
        notes.insert(
            0,
            f'text was typed, but the terminal refused key "{key}". To press it, call '
            f"terminal {press}.",
        )
    matched: list[str] = []
    try:
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
            # The reply: the output pauses after the input, as start's first screen does.
            ended = await terminal_manager.wait_for_reply(
                terminal_id, owner, seconds=seconds, after_quiet=int(sent["quiet_boundaries"])
            )
        snapshot = await terminal_manager.snapshot(terminal_id, owner, include_name=False)
    except Exception as error:
        # The input was sent: a screen that cannot be read must not hide that.
        data = _unread_screen_result(
            terminal_manager, terminal_id, error, done="The input was sent"
        )
        return tool_success(_with_notes({**data, **typed}, notes))
    if capped and ended == "timeout":
        notes.append(_capped_wait_note())
    _acknowledge_after_persistence(terminal_manager, context, owner, snapshot)
    data = _screen_result(snapshot, view="input", unseen_from=_unseen_from(snapshot))
    if pattern is not None:
        data["wait_ended"] = _wait_end(ended, data)
    elif snapshot.get("state") == "working":
        # The reply paused or ran out of time while the program still works.
        data["next"] = _reply_pending_text(terminal_id)
    elif data["state"] == "running" and snapshot.get("idle_reason") != "declared":
        # Its work ended only by evidence: it can go on without declaring it.
        data["next"] = _PAUSED_TEXT
    if matched:
        data["matched"] = _matched_line(matched[0])
    result = _with_notes({**data, **typed}, notes)
    if "next" in result:
        # What to do next closes the result, after what was typed.
        result["next"] = result.pop("next")
    return tool_success(result)


def _unknown_key_message(key: str) -> str:
    return (
        f'terminal was not run: key "{key}" is not a named key. Named keys: '
        f"{TERMINAL_KEY_SUMMARY}. Type other characters as text, or send exact sequences as data."
    )


async def _handle_kill(
    terminal_manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    terminal_id = required_string(arguments.get("terminal_id"), field_name="terminal_id")
    owner = _owner(context)
    before = _terminal_info(terminal_manager, terminal_id)
    after = await terminal_manager.kill(terminal_id, owner)
    context.after_result_persisted(lambda: terminal_manager.acknowledge_exit(terminal_id, owner))
    data: JsonObject = {"terminal_id": terminal_id, "state": _agent_state(after)}
    if after.exit_code is not None:
        data["exit_code"] = after.exit_code
    if before.finished:
        snapshot = await terminal_manager.read(terminal_id)
        data["screen"] = snapshot.get("screen") or ""
        data["note"] = "The program had already ended; nothing was stopped."
    return tool_success(data)


# What an attached interactive program sends on its own; Agents polled for it.
_DELIVERIES_TEXT = (
    "While it runs, its screen arrives as a new message each time it goes idle showing something "
    "new, and so does its exit. Continue with your next step instead of polling with status."
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
        "It is still working: its screen arrives as a new message once it goes idle; continue "
        f"other work or end your turn. To wait for it now instead, call terminal "
        f"{_call('wait', terminal_id)}."
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
_LAUNCH_FIELDS = ("program", "title", "workdir", "log", "name")
# Results that follow the program: their history holds only lines the Agent
# has not seen, and they offer no pages (user decision D1).
_FOLLOWING_VIEWS = frozenset({"start", "input", "wait"})


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
    label = " ".join((info.name or info.program).split())
    if len(label) > _TERMINAL_LABEL_CHARS:
        return label[: _TERMINAL_LABEL_CHARS - 3] + "..."
    return label


def _attached_terminals(
    terminal_manager: TerminalManager, owner: TerminalOwner
) -> list[TerminalInfo]:
    """The terminals attached to this Session, running ones first, newest first."""
    return sorted(
        (info for info in reversed(terminal_manager.list_terminals()) if info.attachment == owner),
        key=lambda info: info.finished,
    )


def _terminals_text(terminal_manager: TerminalManager, owner: TerminalOwner) -> str:
    """Name the terminals attached to this Session, running ones first, newest first."""
    attached = _attached_terminals(terminal_manager, owner)
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
    terminal_manager: TerminalManager, owner: TerminalOwner, terminal_id: str
) -> str:
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


def _address(owner: TerminalOwner) -> SessionAddress:
    return SessionAddress(
        project_id=owner.project_id, agent_id=owner.agent_id, session_id=owner.session_id
    )


def register_terminal_tool(
    registry: ToolRegistry,
    terminal_manager: TerminalManager,
    projects: ProjectStore,
    *,
    subagents: SubAgentTree | None = None,
    scratch_areas: ScratchAreas | None = None,
) -> None:
    """Register the Agent-facing terminal Tool.

    *subagents* tells which Sessions descend from the caller, so ``list`` and
    ``status`` name the Sub-Agent that holds a terminal; without it such a
    terminal shows as attached to another Session.
    """
    registry.register(
        TERMINAL_TOOL_NAME,
        TERMINAL_TOOL_DESCRIPTION,
        TERMINAL_TOOL_PARAMETERS,
        make_terminal_handler(terminal_manager, projects, subagents, scratch_areas=scratch_areas),
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


def _attachment_text(item: Mapping[str, Any]) -> str:
    """Who holds a listed terminal, as the user reads it."""
    subagent = item.get("subagent")
    if item.get("attached") == "subagent" and isinstance(subagent, str) and subagent:
        return f"attached to Sub-Agent {subagent}"
    return _ATTACHMENT_TEXT.get(str(item.get("attached")), "")


def _visible_input(data: str) -> str:
    """Spell out control characters, such as Escape, that exact input data can hold."""
    return "".join(
        char
        if char in "\n\t" or (char >= " " and char != "\x7f")
        else char.encode("unicode_escape").decode("ascii")
        for char in data
    )


def _typed_input(arguments: JsonObject) -> str:
    """Return what an input call types, with a named key shown as ``<key>``."""
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
    state = item.get("state")
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
    typed = _typed_input(call) if action == "input" else ""
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
                    part for part in (_terminal_state_text(item), _attachment_text(item)) if part
                ),
            }
            for item in listed
        ]
        return [*blocks, display_results(items)]
    if "history" in data:
        blocks.append(display_text("scrollback", source="result", path=("data", "history")))
    if "screen" in data:
        blocks.append(display_text("screen", source="result", path=("data", "screen")))
    state = data.get("state")
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
