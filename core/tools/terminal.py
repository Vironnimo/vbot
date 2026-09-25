"""Agent-facing interactive Terminal Sessions backed by PTY/ConPTY."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.projects import ProjectError, ProjectNotFoundError, ProjectStore, cwd_exists
from core.tools._shell_arguments import resolve_timeout
from core.tools._terminal_arguments import (
    TERMINAL_KEY_SUMMARY,
    command_words,
    normalize_terminal_arguments,
)
from core.tools._terminal_state import TERMINAL_ACTIVITY_QUIET_SECONDS
from core.tools.arguments import (
    optional_int,
    optional_number,
    optional_string,
    required_int,
    required_string,
)
from core.tools.bash import get_shell_env
from core.tools.terminal_backend import default_terminal_argv
from core.tools.terminal_manager import (
    TERMINAL_DEFAULT_COLUMNS,
    TERMINAL_DEFAULT_ROWS,
    TERMINAL_GROUP_NAME_MAX_CHARS,
    TERMINAL_INPUT_KEY_SEQUENCES,
    TERMINAL_INPUT_MAX_CHARS,
    TERMINAL_MAX_COLUMNS,
    TERMINAL_MAX_ROWS,
    TERMINAL_MIN_COLUMNS,
    TERMINAL_MIN_ROWS,
    TERMINAL_STATUS_DEFAULT_LINES,
    TERMINAL_STATUS_MAX_LINES,
    TerminalAlreadyAttachedError,
    TerminalCapacityError,
    TerminalClosedError,
    TerminalCursorError,
    TerminalLaunchError,
    TerminalManager,
    TerminalNotAttachedError,
    TerminalNotFoundError,
    TerminalNotOwnedError,
    TerminalOwner,
    TerminalSession,
    TerminalStaleScreenError,
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

TERMINAL_TOOL_NAME = "terminal"
TERMINAL_ACTIONS = (
    "start",
    "list",
    "attach",
    "detach",
    "status",
    "wait",
    "input",
    "resize",
    "kill",
)
# Longer than the quiet period that settles output, so a default wait can see it.
TERMINAL_DEFAULT_WAIT_MS = 5_000
TERMINAL_MAX_WAIT_MS = 10_000
TERMINAL_KEYS = tuple(TERMINAL_INPUT_KEY_SEQUENCES)
TERMINAL_PROJECT_WORKDIR_PREFIX = "project:"
# Terminals named in not-found errors, and the label length for each.
_LISTED_TERMINALS = 3
_TERMINAL_LABEL_CHARS = 40

TERMINAL_TOOL_DESCRIPTION = (
    "Operate interactive programs by typing into and reading their live terminal screen "
    "(REPLs, TUIs, debuggers). Terminal Sessions survive Runs. Attached terminals "
    "notify you when output settles, the process exits, or the terminal fails. Quiet "
    "output does not prove completion; decide from the supplied screen. Rendered "
    "terminal text is not exact file content."
)


class _ProjectWorkdirUnavailableError(ValueError):
    """A Project workdir reference exists syntactically but cannot be resolved."""


TERMINAL_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(TERMINAL_ACTIONS),
            "description": (
                "start launches and attaches; list discovers terminals; attach binds an "
                "unattached terminal to this Session; detach releases it without "
                "stopping; status reads screen/history; wait awaits activity; input "
                "types; resize changes dimensions; kill stops the process tree."
            ),
        },
        "terminal_id": {
            "type": "string",
            "description": (
                "Terminal Session id returned by start or list. Required except for start and list."
            ),
        },
        "command": {
            "type": "string",
            "description": (
                "Executable for start, without shell expansion. "
                "Omit for the default interactive shell."
            ),
        },
        "args": {
            "type": "array",
            "items": {"type": "string"},
            "description": ("Arguments for start with command. Omit for no arguments."),
        },
        "data": {
            "type": "string",
            "maxLength": TERMINAL_INPUT_MAX_CHARS,
            "description": (
                "Exact input, including control sequences. "
                "Omit when using text/key; cannot combine them."
            ),
        },
        "text": {
            "type": "string",
            "maxLength": TERMINAL_INPUT_MAX_CHARS,
            "description": (
                "For start, queue text plus Enter after launch; omit for no initial input. "
                'For input, type without Enter; add key: "enter" to '
                "submit. Multiline text uses bracketed paste when enabled."
            ),
        },
        "workdir": {
            "type": "string",
            "description": (
                "Start directory: absolute path, path relative to cwd, or project:<project-id>. "
                "Omit for cwd."
            ),
        },
        "name": {
            "type": "string",
            "maxLength": 80,
            "description": ("Label for start. Omit if unnecessary; calls use terminal_id."),
        },
        "group": {
            "type": "string",
            "maxLength": TERMINAL_GROUP_NAME_MAX_CHARS,
            "description": (
                "Group name for start; automatically joins or creates it. "
                "Omit for automatic grouping."
            ),
        },
        "columns": {
            "type": "integer",
            "minimum": TERMINAL_MIN_COLUMNS,
            "maximum": TERMINAL_MAX_COLUMNS,
            "description": ("Terminal width. Required for resize."),
        },
        "rows": {
            "type": "integer",
            "minimum": TERMINAL_MIN_ROWS,
            "maximum": TERMINAL_MAX_ROWS,
            "description": ("Terminal height. Required for resize."),
        },
        "lines": {
            "type": "integer",
            "minimum": 1,
            "maximum": TERMINAL_STATUS_MAX_LINES,
            "default": TERMINAL_STATUS_DEFAULT_LINES,
            "description": (
                "History page size for status. Omit for 30 lines. Without start_line, "
                "the current screen is also returned."
            ),
        },
        "start_line": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Zero-based buffer line for status; 0 is oldest retained. Returns only "
                "that page. Omit for newest scrollback plus screen."
            ),
        },
        "timeout_ms": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "wait: longest wait for new output to settle "
                f"({TERMINAL_ACTIVITY_QUIET_SECONDS:g} s without output); omit for "
                f"{TERMINAL_DEFAULT_WAIT_MS}. "
                "input: also wait this long and return the screen; omit to return at once. "
                f"At most {TERMINAL_MAX_WAIT_MS}; a timeout leaves the program running."
            ),
        },
        "key": {
            "type": "string",
            "description": (
                f"Named key for input, sent after text: {TERMINAL_KEY_SUMMARY}. "
                "Omit for text/data only; use data for other sequences."
            ),
        },
        "expected_screen_revision": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Screen revision required for input. Use the observed revision for "
                "prompts; omit for input independent of the screen."
            ),
        },
    },
    "required": ["action"],
}
# Accepted and validated, never advertised: seconds from other harnesses, and a
# revision the terminal tracks itself unless an Agent names one.
TERMINAL_UNADVERTISED_PARAMETERS: JsonObject = {
    "timeout": {"type": "number", "minimum": 0},
    "after_revision": {"type": "integer", "minimum": 0},
}


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
    action = arguments.get("action")
    if not isinstance(action, str) or action not in TERMINAL_ACTIONS:
        return tool_failure(
            "invalid_arguments",
            f"action must be one of: {', '.join(TERMINAL_ACTIONS)}",
            retryable=False,
        )
    terminal_id = arguments.get("terminal_id")
    if action not in {"start", "list"} and (
        not isinstance(terminal_id, str) or not terminal_id.strip()
    ):
        return tool_failure(
            "invalid_arguments",
            f"{action} needs terminal_id. {_terminals_text(terminal_manager, _owner(context))}",
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
        if action == "resize":
            return await _handle_resize(terminal_manager, context, arguments)
        return await _handle_kill(terminal_manager, context, arguments)
    except TerminalNotFoundError:
        return tool_failure(
            "terminal_not_found",
            _not_found_message(terminal_manager, _owner(context), str(terminal_id)),
            retryable=False,
        )
    except TerminalAlreadyAttachedError as error:
        return tool_failure("terminal_already_attached", str(error), retryable=False)
    except TerminalNotAttachedError as error:
        return tool_failure("terminal_not_attached", str(error), retryable=False)
    except TerminalNotOwnedError:
        return tool_failure(
            "terminal_not_owned",
            _not_owned_message(terminal_manager, str(terminal_id)),
            retryable=False,
        )
    except TerminalClosedError as error:
        return tool_failure("terminal_closed", str(error), retryable=False)
    except TerminalCapacityError as error:
        return tool_failure("terminal_capacity", str(error), retryable=True)
    except TerminalStaleScreenError as error:
        return tool_failure("stale_screen", str(error), retryable=True)
    except TerminalCursorError as error:
        return tool_failure("invalid_cursor", str(error), retryable=False)
    except ProjectNotFoundError as error:
        return tool_failure("project_not_found", str(error), retryable=False)
    except _ProjectWorkdirUnavailableError as error:
        return tool_failure("project_unavailable", str(error), retryable=False)
    except TerminalLaunchError as error:
        return tool_failure("terminal_launch_failed", str(error), retryable=False)
    except FileNotFoundError as error:
        command = error.filename or "the requested program"
        return tool_failure(
            "terminal_command_not_found",
            f"Interactive terminal executable was not found: {command}",
            retryable=False,
        )
    except (OSError, ValueError) as error:
        return tool_failure("invalid_arguments", str(error), retryable=False)


async def _handle_start(
    terminal_manager: TerminalManager,
    projects: ProjectStore,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    notes: list[str] = []
    requested_id = arguments.get("terminal_id")
    if isinstance(requested_id, str) and requested_id.strip():
        # A live terminal may be what the Agent means to use; a finished one cannot be.
        if any(
            item.terminal_id == requested_id and item.state not in {"exited", "error"}
            for item in terminal_manager.list_sessions()
        ):
            typing = json.dumps(
                {"action": "input", "terminal_id": requested_id, "text": "...", "key": "enter"}
            )
            raise ValueError(
                f"start opens a new terminal and was not run. To type into {requested_id}, "
                f"send {typing}; to start another terminal, omit terminal_id."
            )
    else:
        requested_id = None
    raw_command = arguments.get("command")
    args = _optional_string_array(arguments.get("args"), field_name="args")
    environment = await get_shell_env()
    if raw_command in (None, ""):
        if args:
            raise ValueError("args requires command to be set")
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
        raise ValueError("text must be a non-empty string when provided")
    raw_workdir = arguments.get("workdir")
    workdir_value = optional_string(raw_workdir, field_name="workdir")
    if raw_workdir == "":
        workdir_value = None
    workdir = _resolve_workdir(projects, context, workdir_value)
    raw_name = arguments.get("name")
    name = optional_string(raw_name, field_name="name")
    if raw_name == "":
        name = None
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("name must not be blank")
        if len(name) > 80:
            raise ValueError("name must be at most 80 characters")
    owner = _owner(context)
    group_id = None
    raw_group = arguments.get("group")
    if raw_group == "":
        raw_group = None
    if raw_group is not None:
        if not isinstance(raw_group, str) or not raw_group.strip():
            raise ValueError("group must be a non-empty string")
        if len(raw_group.strip()) > TERMINAL_GROUP_NAME_MAX_CHARS:
            raise ValueError(f"group must be at most {TERMINAL_GROUP_NAME_MAX_CHARS} characters")
        group_id = terminal_manager.resolve_or_create_agent_group(raw_group.strip()).group_id
    session = await terminal_manager.spawn(
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
    snapshot = await terminal_manager.snapshot(session.terminal_id, owner)
    _acknowledge_after_persistence(terminal_manager, context, owner, snapshot)
    data = _project_snapshot(snapshot, view="start")
    data["delivery"] = "automatic_terminal_activity"
    if requested_id is not None:
        notes.append(
            f"start assigns the terminal_id: use {session.terminal_id}, not {requested_id}."
        )
    return tool_success(_with_notes(data, notes))


def _handle_list(terminal_manager: TerminalManager, context: ToolContext) -> JsonObject:
    owner = _owner(context)
    sessions = terminal_manager.list_sessions()
    return tool_success(
        {
            "terminals": [
                {
                    key: value
                    for key, value in _terminal_summary(session, current_attachment=owner).items()
                    if value not in (None, "")
                }
                for session in sessions
            ]
        }
    )


def _handle_attach(
    terminal_manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    terminal_id = required_string(arguments.get("terminal_id"), field_name="terminal_id")
    owner = _owner(context)
    session, changed = terminal_manager.attach(
        terminal_id,
        owner,
        origin_run_id=context.run_id,
        execution_owner=context.execution_owner,
    )
    data = _terminal_summary(session, current_attachment=owner)
    data.update({"attached": True, "changed": changed, "delivery": "automatic_terminal_activity"})
    return tool_success(data)


def _handle_detach(
    terminal_manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    terminal_id = required_string(arguments.get("terminal_id"), field_name="terminal_id")
    session = terminal_manager.detach(terminal_id, _owner(context))
    data = _terminal_summary(session, current_attachment=_owner(context))
    data.update(
        {
            "attached": False,
            "changed": True,
            "process_continues": session.state not in {"exited", "error"},
        }
    )
    return tool_success(data)


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
    snapshot = await terminal_manager.snapshot(
        terminal_id,
        owner,
        lines=lines,
        start_line=start_line,
    )
    if start_line is None:
        _acknowledge_after_persistence(terminal_manager, context, owner, snapshot)
    return tool_success(
        _project_snapshot(
            snapshot, view="status", page_lines=lines, include_screen=start_line is None
        )
    )


async def _handle_wait(
    terminal_manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    terminal_id = required_string(arguments.get("terminal_id"), field_name="terminal_id")
    owner = _owner(context)
    session = terminal_manager.get_session(terminal_id, owner)
    after_revision = optional_int(
        arguments.get("after_revision"),
        field_name="after_revision",
        default=None,
        minimum=0,
    )
    if after_revision is None:
        after_revision = session.acknowledged_attention_revision
    timeout_ms, notes = _wait_milliseconds(arguments)
    snapshot, timed_out = await terminal_manager.wait_for_attention(
        terminal_id,
        owner,
        after_revision=after_revision,
        timeout_ms=TERMINAL_DEFAULT_WAIT_MS if timeout_ms is None else timeout_ms,
    )
    _acknowledge_after_persistence(terminal_manager, context, owner, snapshot)
    data = _project_snapshot(snapshot, view="wait")
    data["timed_out"] = timed_out
    return tool_success(_with_notes(data, notes if timed_out else []))


async def _handle_input(
    terminal_manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    terminal_id = required_string(arguments.get("terminal_id"), field_name="terminal_id")
    raw_data = arguments.get("data")
    if raw_data is not None and not isinstance(raw_data, str):
        raise ValueError("data must be a string")
    if raw_data == "":
        raw_data = None
    text = arguments.get("text")
    if text is not None and not isinstance(text, str):
        raise ValueError("text must be a string")
    if text == "":
        text = None
    key = optional_string(arguments.get("key"), field_name="key")
    if key == "":
        key = None
    if raw_data is not None and (text is not None or key is not None):
        raise ValueError(
            "data is sent exactly as given, so it cannot be combined with text or key; put the "
            'whole sequence in data (end it with "\\r" to press Enter), or send text and key '
            "without data."
        )
    if key is not None and key not in TERMINAL_KEYS:
        raise ValueError(
            f'key "{key}" is not a named key. Named keys: {TERMINAL_KEY_SUMMARY}. Type other '
            "characters as text, or send exact sequences as data."
        )
    wait_ms, notes = _wait_milliseconds(arguments)
    expected_revision = optional_int(
        arguments.get("expected_screen_revision"),
        field_name="expected_screen_revision",
        default=None,
        minimum=0,
    )
    owner = _owner(context)
    session = terminal_manager.get_session(terminal_id, owner)
    prior_attention_revision = session.attention_revision if session.attention is not None else None
    revision_before_input = session.attention_revision
    data = await terminal_manager.send_input(
        terminal_id,
        owner,
        data=raw_data if isinstance(raw_data, str) else None,
        text=text if isinstance(text, str) else None,
        key=key,
        expected_screen_revision=expected_revision,
        origin_run_id=context.run_id,
        execution_owner=context.execution_owner,
    )
    if not data["characters_sent"]:
        return tool_success(data)
    if prior_attention_revision is not None:
        context.after_result_persisted(
            lambda: terminal_manager.acknowledge_attention(
                terminal_id, owner, prior_attention_revision
            )
        )
    data["delivery"] = "automatic_terminal_activity"
    if not wait_ms:
        return tool_success(data)
    # The reply to this input settles after it; wait for that, as wait would.
    snapshot, timed_out = await terminal_manager.wait_for_attention(
        terminal_id, owner, after_revision=revision_before_input, timeout_ms=wait_ms
    )
    _acknowledge_after_persistence(terminal_manager, context, owner, snapshot)
    screen = _project_snapshot(snapshot, view="wait")
    waited: JsonObject = {
        "terminal_id": terminal_id,
        "state": screen.pop("state", None),
        "characters_sent": data["characters_sent"],
        "key": data["key"],
        **{key: value for key, value in screen.items() if key != "terminal_id"},
        "timed_out": timed_out,
    }
    if timed_out:
        waited["delivery"] = "automatic_terminal_activity"
    return tool_success(_with_notes(waited, notes if timed_out else []))


async def _handle_resize(
    terminal_manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    terminal_id = required_string(arguments.get("terminal_id"), field_name="terminal_id")
    columns = required_int(
        arguments.get("columns"),
        field_name="columns",
        minimum=TERMINAL_MIN_COLUMNS,
        maximum=TERMINAL_MAX_COLUMNS,
    )
    rows = required_int(
        arguments.get("rows"),
        field_name="rows",
        minimum=TERMINAL_MIN_ROWS,
        maximum=TERMINAL_MAX_ROWS,
    )
    data = await terminal_manager.resize(terminal_id, _owner(context), columns=columns, rows=rows)
    return tool_success(data)


async def _handle_kill(
    terminal_manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    terminal_id = required_string(arguments.get("terminal_id"), field_name="terminal_id")
    owner = _owner(context)
    snapshot = await terminal_manager.kill(terminal_id, owner)
    _acknowledge_after_persistence(terminal_manager, context, owner, snapshot)
    return tool_success(_project_snapshot(snapshot, view="kill"))


# Snapshot facts no Tool result shows: attention records repeat what state,
# timed_out, and the screen already say, and timestamps change no next call.
_HIDDEN_SNAPSHOT_FIELDS = frozenset(
    {"attention", "attention_revision", "started_at", "finished_at", "pid"}
)
# Launch facts: start and status show them; results that follow up on a
# running terminal do not repeat them.
_LAUNCH_FIELDS = ("command", "title", "arguments", "workdir", "log_file", "name")


def _project_snapshot(
    snapshot: dict[str, Any],
    *,
    view: str,
    page_lines: int = TERMINAL_STATUS_DEFAULT_LINES,
    include_screen: bool = True,
) -> JsonObject:
    """Shape a terminal snapshot for one action's result.

    History text above the screen becomes its own multi-line ``history`` field;
    ``scrollback`` keeps the paging facts and appears only when there is history
    or an older page to read.
    """
    projected = {
        key: value
        for key, value in snapshot.items()
        if key not in _HIDDEN_SNAPSHOT_FIELDS and value is not None
    }
    for key in ("title", "arguments"):
        if not projected.get(key):
            projected.pop(key, None)
    if view == "start":
        # status shows the raw log; a new terminal has written nothing to it yet.
        projected.pop("log_file", None)
    elif view != "status":
        for key in _LAUNCH_FIELDS:
            projected.pop(key, None)
        if "size_change" not in projected:
            # The dimensions are unchanged since a screen showed them.
            projected.pop("columns", None)
            projected.pop("rows", None)
    screen = projected.pop("screen", None)
    scrollback = dict(projected.pop("scrollback", None) or {})
    scrollback.pop("next_before", None)
    scrollback.pop("next_cursor", None)
    history = scrollback.pop("text", "")
    next_start = scrollback.get("next_start_line")
    scrollback["next_request"] = (
        {
            "action": "status",
            "terminal_id": str(snapshot["terminal_id"]),
            "start_line": next_start,
            "lines": page_lines,
        }
        if isinstance(next_start, int)
        else None
    )
    if history:
        projected["history"] = history
    if include_screen:
        projected["screen"] = screen if isinstance(screen, str) else ""
    if not include_screen or history or scrollback["next_request"] is not None:
        projected["scrollback"] = scrollback
    return projected


def _with_notes(data: JsonObject, notes: list[str]) -> JsonObject:
    if notes:
        data["note"] = " ".join(notes)
    return data


def _wait_milliseconds(arguments: JsonObject) -> tuple[int | None, list[str]]:
    """Return the requested wait in milliseconds, capped, and notes about reading it."""
    timeout = optional_number(arguments.get("timeout"), field_name="timeout", minimum=0)
    timeout_ms = optional_number(arguments.get("timeout_ms"), field_name="timeout_ms", minimum=0)
    seconds, note = resolve_timeout(timeout, timeout_ms, tool_name=TERMINAL_TOOL_NAME)
    notes = [note] if note else []
    if seconds is None:
        return None, notes
    milliseconds = round(seconds * 1000)
    if milliseconds > TERMINAL_MAX_WAIT_MS:
        notes.append(
            f"A wait lasts at most {TERMINAL_MAX_WAIT_MS} ms; wait again if the program is "
            "still busy."
        )
        milliseconds = TERMINAL_MAX_WAIT_MS
    return milliseconds, notes


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


def _terminal_label(session: TerminalSession) -> str:
    label = session.name or session.renderer.title or session.command
    if len(label) > _TERMINAL_LABEL_CHARS:
        return label[: _TERMINAL_LABEL_CHARS - 3] + "..."
    return label


def _terminals_text(terminal_manager: TerminalManager, owner: TerminalOwner) -> str:
    """Name the terminals attached to this Session, live and newest first."""
    sessions = terminal_manager.list_sessions()
    attached = sorted(
        (session for session in reversed(sessions) if session.attachment == owner),
        key=lambda session: session.state in {"exited", "error"},
    )
    if not attached:
        if sessions:
            return (
                'No terminal is attached to this Session; {"action": "list"} shows every '
                "terminal, and attach makes one usable here."
            )
        return "You have no terminals; start one."
    shown = "; ".join(
        f"{session.terminal_id} ({session.state}: {_terminal_label(session)})"
        for session in attached[:_LISTED_TERMINALS]
    )
    more = ' {"action": "list"} shows all of them.' if len(attached) > _LISTED_TERMINALS else ""
    return f"Terminals attached to this Session: {shown}.{more}"


def _not_found_message(
    terminal_manager: TerminalManager, owner: TerminalOwner, terminal_id: str
) -> str:
    if terminal_id.startswith("proc_"):
        return (
            f"{terminal_id} is a background command, not a terminal. Use the process Tool with "
            "this process_id."
        )
    return f"No terminal has the id {terminal_id}. {_terminals_text(terminal_manager, owner)}"


def _not_owned_message(terminal_manager: TerminalManager, terminal_id: str) -> str:
    session = next(
        (item for item in terminal_manager.list_sessions() if item.terminal_id == terminal_id),
        None,
    )
    if session is not None and session.attachment is not None:
        return (
            f"{terminal_id} is attached to another Session, so this Session cannot use it "
            "until that Session detaches it."
        )
    attach = json.dumps({"action": "attach", "terminal_id": terminal_id})
    return f"{terminal_id} is not attached to this Session. Attach it first with {attach}."


def _terminal_summary(session: TerminalSession, *, current_attachment: TerminalOwner) -> JsonObject:
    attention = session.attention
    if session.attachment == current_attachment:
        attachment = "current"
    elif session.attachment is None:
        attachment = "none"
    else:
        attachment = "other"
    return {
        "terminal_id": session.terminal_id,
        "state": session.state,
        "command": session.command,
        "name": session.name,
        "title": session.renderer.title,
        "workdir": model_path(session.cwd),
        "exit_code": session.exit_code,
        "started_at": session.started_at.isoformat(),
        "finished_at": session.finished_at.isoformat() if session.finished_at else None,
        "screen_revision": session.renderer.revision,
        "attention_revision": session.attention_revision,
        "attention_kind": attention.kind if attention is not None else None,
        "attachment": attachment,
    }


def _acknowledge_after_persistence(
    terminal_manager: TerminalManager,
    context: ToolContext,
    owner: TerminalOwner,
    snapshot: dict[str, Any],
) -> None:
    attention = snapshot.get("attention")
    terminal_id = str(snapshot["terminal_id"])
    screen_revision = int(snapshot["screen_revision"])
    columns, rows = int(snapshot["columns"]), int(snapshot["rows"])

    def acknowledge() -> None:
        if not terminal_manager.acknowledge_screen(
            terminal_id, owner, screen_revision=screen_revision, columns=columns, rows=rows
        ):
            return
        if isinstance(attention, dict) and isinstance(attention.get("revision"), int):
            terminal_manager.acknowledge_attention(terminal_id, owner, attention["revision"])

    context.after_result_persisted(acknowledge)


def _optional_string_array(value: object, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be an array of strings")
    if any("\x00" in item for item in value):
        raise ValueError(f"{field_name} must not contain NUL characters")
    return list(value)


def _resolve_workdir(
    projects: ProjectStore,
    context: ToolContext,
    workdir_value: str | None,
) -> Path:
    if workdir_value is None:
        return context.effective_cwd.resolve()
    if not workdir_value.startswith(TERMINAL_PROJECT_WORKDIR_PREFIX):
        return context.resolve_path(workdir_value)

    project_id = workdir_value.removeprefix(TERMINAL_PROJECT_WORKDIR_PREFIX)
    if not project_id:
        raise ValueError(
            "workdir Project reference must use project:<project-id> with a non-empty id"
        )
    try:
        project = projects.get(project_id)
    except ProjectNotFoundError:
        raise
    except (ProjectError, OSError) as error:
        raise _ProjectWorkdirUnavailableError(
            f"Project '{project_id}' could not be resolved: {error}"
        ) from error
    if not cwd_exists(project.cwd):
        raise _ProjectWorkdirUnavailableError(
            f"Project '{project.project_id}' has no reachable cwd: {model_path(project.cwd)}"
        )
    return Path(project.cwd).resolve()


def _owner(context: ToolContext) -> TerminalOwner:
    return TerminalOwner(context.project_id, context.agent_id, context.session_id)


def register_terminal_tool(
    registry: ToolRegistry,
    terminal_manager: TerminalManager,
    projects: ProjectStore,
) -> None:
    """Register the Agent-facing interactive terminal Tool."""
    registry.register(
        TERMINAL_TOOL_NAME,
        TERMINAL_TOOL_DESCRIPTION,
        TERMINAL_TOOL_PARAMETERS,
        make_terminal_handler(terminal_manager, projects),
        family="execution",
        open_input_schema=True,
        unadvertised_parameters=TERMINAL_UNADVERTISED_PARAMETERS,
        argument_normalizer=normalize_terminal_arguments,
        result_schema={"type": "object"},
        display=ToolDisplay(
            parts_builder=_terminal_display_parts,
            fact_builder=result_count_fact_builder("terminals", when_arguments={"action": "list"}),
        ),
    )


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
        command_label = command if isinstance(command, str) and command else "default shell"
        parts.append(ToolDisplayPart(command_label, kind="command"))
    return tuple(parts)


__all__ = [
    "TERMINAL_ACTIONS",
    "TERMINAL_DEFAULT_WAIT_MS",
    "TERMINAL_KEYS",
    "TERMINAL_MAX_WAIT_MS",
    "TERMINAL_PROJECT_WORKDIR_PREFIX",
    "TERMINAL_TOOL_DESCRIPTION",
    "TERMINAL_TOOL_NAME",
    "TERMINAL_TOOL_PARAMETERS",
    "TERMINAL_UNADVERTISED_PARAMETERS",
    "make_terminal_handler",
    "register_terminal_tool",
]
