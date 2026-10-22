"""Agent-facing actions for interactive PTY/ConPTY terminals."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.permissions import CommandChecker, PermissionDenied
from core.projects import ProjectStore
from core.sessions import SessionAddress
from core.tools._terminal_arguments import (
    normalize_terminal_arguments,
)
from core.tools._workdir import ProjectWorkdirError, is_project_workdir, project_workdir
from core.tools.arguments import (
    optional_int,
    optional_string,
    required_string,
)
from core.tools.contracts import ToolContractError
from core.tools.model_names import BASH_TOOL_NAME
from core.tools.scratch_areas import ScratchAreas, ScratchUnavailableError
from core.tools.shell_environment import scratch_environment, terminal_environment
from core.tools.terminal_backend import UnpassableArgumentError, default_terminal_argv
from core.tools.terminal_manager import (
    TERMINAL_DEFAULT_COLUMNS,
    TERMINAL_DEFAULT_ROWS,
    TERMINAL_MAX_COLUMNS,
    TERMINAL_MAX_ROWS,
    TERMINAL_MIN_COLUMNS,
    TERMINAL_MIN_ROWS,
    TERMINAL_STATUS_DEFAULT_LINES,
    TERMINAL_STATUS_MAX_LINES,
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
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    result_count_fact_builder,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger
from core.utils.paths import model_path

if TYPE_CHECKING:
    from core.subagents import SubAgentTree

from ._terminal_definition import (
    _MATCHED_LINE_CHARS,
    TERMINAL_KEYS,
    TERMINAL_REPLY_SECONDS,
    TERMINAL_TOOL_DESCRIPTION,
    TERMINAL_TOOL_NAME,
    TERMINAL_TOOL_PARAMETERS,
    TERMINAL_UNADVERTISED_PARAMETERS,
    TERMINAL_WAIT_DEFAULT_SECONDS,
    TERMINAL_WAIT_MAX_SECONDS,
    _terminal_description,
)
from ._terminal_definition import (
    TERMINAL_ACTIONS as TERMINAL_ACTIONS,
)
from ._terminal_display import _terminal_detail_blocks, _terminal_display_parts
from ._terminal_permissions import (
    UnfollowableInput,
    attach_refused_message,
    check_input,
    check_start,
    may_attach,
    permission_scope,
    screen_hidden_note,
)
from ._terminal_results import (
    _DELIVERIES_TEXT,
    _PAUSED_TEXT,
    _agent_state,
    _attachment,
    _background_task_refusal,
    _call,
    _capacity_message,
    _capped_wait_note,
    _closed_message,
    _first_screen_pending_text,
    _not_attached_text,
    _not_found_message,
    _not_found_program_message,
    _not_owned_message,
    _reply_pending_text,
    _screen_result,
    _short_program,
    _split_command_line,
    _terminal_facts,
    _terminal_info,
    _terminals_text,
    _unknown_key_message,
    _unpassable_argument_message,
    _unread_screen_result,
    _unseen_from,
    _UnsentStartInput,
    _wait_pattern,
    _wait_seconds,
    _with_notes,
)

_LOGGER = get_logger("tools.terminal")


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
    command_checker: CommandChecker | None = None,
):
    """Create a terminal Tool handler bound to one Terminal Manager."""

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await _handle_terminal(
            terminal_manager,
            projects,
            subagents,
            context,
            arguments,
            scratch_areas,
            command_checker,
        )

    return handler


async def _handle_terminal(
    terminal_manager: TerminalManager,
    projects: ProjectStore,
    subagents: SubAgentTree | None,
    context: ToolContext,
    arguments: JsonObject,
    scratch_areas: ScratchAreas | None,
    command_checker: CommandChecker | None,
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
                terminal_manager, projects, context, arguments, scratch_areas, command_checker
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
            return await _handle_input(terminal_manager, context, arguments, command_checker)
        if action == "resize":
            return await _handle_resize(terminal_manager, context, arguments)
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
    command_checker: CommandChecker | None,
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
        # Checked here, before the terminal is launched.
        raise ToolContractError(
            f"No terminal was started: workdir {model_path(workdir)} is not a directory. Pass "
            "an existing directory, or omit workdir to use the working directory."
        )
    context.record_worked_paths(workdir)
    owner = _owner(context)
    if arguments.get("group") not in (None, ""):
        notes.append(
            "group has no effect: the user sees each terminal under the conversation that "
            "started it."
        )
    if arguments.get("name") not in (None, ""):
        notes.append("name has no effect: the user calls each terminal by its call_name.")
    overlay = None
    if scratch_areas is not None:
        try:
            folder = await scratch_areas.folder_for(owner.address)
        except ScratchUnavailableError:
            # The owner logged the failure; an optional Scratch folder must not
            # prevent the Agent's program from starting.
            pass
        else:
            overlay = scratch_environment(folder)
    scope = permission_scope(context.permissions)
    if scope is not None:
        confined = await terminal_manager.confined_environment(overlay)
        refused = await check_start(
            command_checker, context.permissions, argv, cwd=workdir, env=confined
        )
        if refused is not None:
            raise PermissionDenied(refused)
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
            permission_scope=scope,
        )
    except TerminalLaunchError as error:
        cause = error.__cause__
        if isinstance(cause, UnpassableArgumentError) and cause.index is not None:
            raise ToolContractError(_unpassable_argument_message(argv[0], cause.value)) from error
        raise
    terminal_id = started.terminal_id
    # An empty screen that is out of date does not show that nothing was printed.
    shown_nothing = False
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
        shown_nothing = data.get("screen") == "" and not snapshot.get("screen_behind")
    running = data["state"] == "running"
    unsent_note = unsent.note(terminal_id, running=running)
    if unsent_note is not None:
        notes.append(unsent_note)
    if requested_id is not None:
        notes.append(f"start assigns the terminal_id: use {terminal_id}, not {requested_id}.")
    data = _with_notes(data, notes)
    if running:
        data["next"] = (
            _first_screen_pending_text(terminal_id) if shown_nothing else _DELIVERIES_TEXT
        )
    return tool_success(data)


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
            "call_name": info.call_name,
            "program": _short_program(info.program),
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
    return {subagent.session: subagent.id for subagent in await subagents.subagents(owner.address)}


def _attachment_fields(
    info: TerminalInfo, owner: TerminalOwner, holders: Mapping[SessionAddress, str]
) -> JsonObject:
    """``attached``, and ``subagent`` for a terminal a Sub-Agent of this Session holds."""
    attached = _attachment(info, owner)
    subagent = (
        holders.get(info.attachment.address)
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
    if not may_attach(context.permissions, _terminal_info(terminal_manager, terminal_id)):
        return tool_failure(
            "permission_denied", attach_refused_message(terminal_id), retryable=False
        )
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
    if not may_attach(context.permissions, info):
        holders = await _subagent_holders(subagents, owner, [info])
        facts = {**_terminal_facts(info, owner), **_attachment_fields(info, owner, holders)}
        return tool_success({**facts, "note": screen_hidden_note()})
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
    snapshot = await terminal_manager.snapshot(terminal_id, owner)
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
    command_checker: CommandChecker | None,
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
    checked = await check_input(
        command_checker,
        context.permissions,
        terminal_manager.shell_state(terminal_id, owner),
        info.shell,
        data=raw_data,
        text=text,
        key=key,
    )
    if isinstance(checked, UnfollowableInput):
        return tool_failure("permission_denied", checked.message(), retryable=False)
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
    if checked is not None:
        checked.sent(pasted=bool(sent.get("pasted")))
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
            # A long wait is no typing: the program works without the focus.
            await terminal_manager.release_focus(terminal_id, owner)
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
        snapshot = await terminal_manager.snapshot(terminal_id, owner)
    except Exception as error:
        # The input was sent: a screen that cannot be read must not hide that.
        data = _unread_screen_result(
            terminal_manager, terminal_id, error, done="The input was sent"
        )
        return tool_success(_with_notes({**data, **typed}, notes))
    finally:
        with contextlib.suppress(Exception):
            await terminal_manager.release_focus(terminal_id, owner)
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


async def _handle_resize(
    terminal_manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    terminal_id = required_string(arguments.get("terminal_id"), field_name="terminal_id")
    owner = _owner(context)
    columns = optional_int(
        arguments.get("columns"),
        field_name="columns",
        default=None,
        minimum=TERMINAL_MIN_COLUMNS,
        maximum=TERMINAL_MAX_COLUMNS,
    )
    rows = optional_int(
        arguments.get("rows"),
        field_name="rows",
        default=None,
        minimum=TERMINAL_MIN_ROWS,
        maximum=TERMINAL_MAX_ROWS,
    )
    info = terminal_manager.terminal(terminal_id, owner)
    if columns is None and rows is None:
        raise ToolContractError(
            f"terminal was not run: resize needs columns, rows or both. {terminal_id} is "
            f"{info.columns} columns x {info.rows} rows; columns can be "
            f"{TERMINAL_MIN_COLUMNS}-{TERMINAL_MAX_COLUMNS}, rows "
            f"{TERMINAL_MIN_ROWS}-{TERMINAL_MAX_ROWS}."
        )
    columns = info.columns if columns is None else columns
    rows = info.rows if rows is None else rows
    changed = await terminal_manager.resize(
        terminal_id, owner, columns=columns, rows=rows, seconds=TERMINAL_REPLY_SECONDS
    )
    snapshot = await terminal_manager.snapshot(terminal_id, owner)
    _acknowledge_after_persistence(terminal_manager, context, owner, snapshot)
    data = _screen_result(snapshot, view="resize", unseen_from=_unseen_from(snapshot))
    # The Agent asked for this size; the notice is for sizes it did not set.
    data.pop("size_change", None)
    notes = [] if changed else [f"{terminal_id} already had this size; nothing changed."]
    result = _with_notes(data, notes)
    if snapshot.get("state") == "working":
        # What to do next closes the result.
        result["next"] = _reply_pending_text(terminal_id)
    return tool_success(result)


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
    return context.resolve_path(workdir_value, access="start_dir")


def _owner(context: ToolContext) -> TerminalOwner:
    return TerminalOwner(context.project_id, context.agent_id, context.session_id)


def register_terminal_tool(
    registry: ToolRegistry,
    terminal_manager: TerminalManager,
    projects: ProjectStore,
    *,
    subagents: SubAgentTree | None = None,
    scratch_areas: ScratchAreas | None = None,
    command_checker: CommandChecker | None = None,
) -> None:
    """Register the Agent-facing terminal Tool.

    *subagents* tells which Sessions descend from the caller, so ``list`` and
    ``status`` name the Sub-Agent that holds a terminal; without it such a
    terminal shows as attached to another Session. *command_checker* checks
    a restricted Session's programs and shell lines (``_terminal_permissions``).
    """
    registry.register(
        TERMINAL_TOOL_NAME,
        TERMINAL_TOOL_DESCRIPTION,
        TERMINAL_TOOL_PARAMETERS,
        make_terminal_handler(
            terminal_manager,
            projects,
            subagents,
            scratch_areas=scratch_areas,
            command_checker=command_checker,
        ),
        summary="Drive interactive programs such as REPLs and coding-agent CLIs.",
        family="execution",
        open_input_schema=True,
        unadvertised_parameters=TERMINAL_UNADVERTISED_PARAMETERS,
        argument_normalizer=normalize_terminal_arguments,
        # Answers password prompts; the length limit applies to the real value.
        secret_sinks={"text": "terminal", "data": "terminal"},
        result_schema={"type": "object"},
        display=ToolDisplay(
            parts_builder=_terminal_display_parts,
            fact_builder=result_count_fact_builder("terminals", when_arguments={"action": "list"}),
            detail_builder=_terminal_detail_blocks,
        ),
    )


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
