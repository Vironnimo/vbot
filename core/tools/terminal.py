"""Agent-facing interactive Terminal Sessions backed by PTY/ConPTY."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.projects import ProjectError, ProjectNotFoundError, ProjectStore, cwd_exists
from core.tools.arguments import (
    optional_int,
    optional_string,
    required_int,
    required_string,
)
from core.tools.terminal_backend import default_terminal_argv
from core.tools.terminal_manager import (
    TERMINAL_DEFAULT_COLUMNS,
    TERMINAL_DEFAULT_ROWS,
    TERMINAL_INPUT_KEY_SEQUENCES,
    TERMINAL_INPUT_MAX_CHARS,
    TERMINAL_MAX_COLUMNS,
    TERMINAL_MAX_ROWS,
    TERMINAL_MIN_COLUMNS,
    TERMINAL_MIN_ROWS,
    TERMINAL_STATUS_DEFAULT_LINES,
    TERMINAL_STATUS_MAX_LINES,
    TerminalCapacityError,
    TerminalClosedError,
    TerminalCursorError,
    TerminalManager,
    TerminalNotFoundError,
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
TERMINAL_ACTIONS = ("start", "list", "status", "wait", "input", "resize", "kill")
TERMINAL_DEFAULT_WAIT_MS = 1_000
TERMINAL_MAX_WAIT_MS = 10_000
TERMINAL_KEYS = tuple(TERMINAL_INPUT_KEY_SEQUENCES)
TERMINAL_PROJECT_WORKDIR_PREFIX = "project:"

TERMINAL_TOOL_DESCRIPTION = (
    "Run and control a program through a real PTY/ConPTY when it waits for interactive input or "
    "must be operated by typing into and observing its live screen, such as a REPL, TUI, prompt, "
    "or debugger. Terminal Sessions survive individual Runs and stay owned by this vBot Session. "
    "An omitted command opens the host user's default interactive shell. Use start with text to "
    "launch a program and send its first input in one call. After Agent input, vBot wakes you when "
    "output has been quiet for a short period, or when the process exits or the terminal fails; "
    "quiet output is only an activity boundary, so inspect status to decide whether the program "
    "is working, waiting for input, or finished. Use data for exact terminal sequences, text/key "
    'for convenient input (key: "enter" submits text; multiline text uses bracketed paste and '
    "does not append Enter). status returns the rendered cell screen plus paginated scrollback "
    "\u2014 follow scrollback.next_request unchanged for older pages. Rendered cells cannot "
    "distinguish tabs from equivalent spaces or cursor movement, so use read for exact file "
    "contents. Reuse a live Terminal Session for later work instead of starting a duplicate "
    "process."
)


class _ProjectWorkdirUnavailableError(ValueError):
    """A Project workdir reference exists syntactically but cannot be resolved."""


_ACTION_FIELDS = {
    "start": frozenset({"action", "command", "args", "text", "workdir", "name", "columns", "rows"}),
    "list": frozenset({"action"}),
    "status": frozenset({"action", "terminal_id", "lines", "cursor"}),
    "wait": frozenset({"action", "terminal_id", "after_revision", "timeout_ms"}),
    "input": frozenset(
        {
            "action",
            "terminal_id",
            "data",
            "text",
            "key",
            "expected_screen_revision",
        }
    ),
    "resize": frozenset({"action", "terminal_id", "columns", "rows"}),
    "kill": frozenset({"action", "terminal_id"}),
}

TERMINAL_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(TERMINAL_ACTIONS),
            "description": (
                "start launches a program, list returns owned Terminal Sessions, status reads a "
                "bounded screen page, wait pauses briefly for a new activity boundary, input sends "
                "exact data or convenient text/keys, resize changes dimensions, kill terminates "
                "the process tree."
            ),
        },
        "terminal_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Terminal Session id returned by start or list. Required except for start and list."
            ),
        },
        "command": {
            "type": "string",
            "description": (
                "Executable for start; omit to open the host user's default interactive shell. "
                "The value is the executable only \u2014 vBot does not interpolate it into a shell."
            ),
        },
        "args": {
            "type": "array",
            "items": {"type": "string"},
            "description": ("Exact argument tokens for start, passed verbatim."),
        },
        "data": {
            "type": "string",
            "maxLength": TERMINAL_INPUT_MAX_CHARS,
            "description": (
                "For input, exact terminal data sent in one write. Use for arbitrary "
                "escape/control sequences or protocols. Cannot be combined with text or key."
            ),
        },
        "text": {
            "type": "string",
            "maxLength": TERMINAL_INPUT_MAX_CHARS,
            "description": (
                "For start, the first input to send after launch; omit to leave the TUI ready. "
                "For input, text to type; multiline text uses bracketed paste when the program "
                'enables it and does not append Enter \u2014 combine with key: "enter" to '
                "submit. For exact control sequences, use data instead."
            ),
        },
        "workdir": {
            "type": "string",
            "description": (
                "Working directory for start. Relative paths use the current working directory; "
                "omit for that directory. Use 'project:<project-id>' to start in a registered "
                "Project's directory."
            ),
        },
        "name": {
            "type": "string",
            "maxLength": 80,
            "description": (
                "Human-friendly label for the Terminal Session. Tool calls always use "
                "terminal_id, never the name. Omit to leave it unnamed and rely on the "
                "announced title or command."
            ),
        },
        "columns": {
            "type": "integer",
            "minimum": TERMINAL_MIN_COLUMNS,
            "maximum": TERMINAL_MAX_COLUMNS,
            "default": TERMINAL_DEFAULT_COLUMNS,
            "description": ("Terminal width; default applies only on start. Required for resize."),
        },
        "rows": {
            "type": "integer",
            "minimum": TERMINAL_MIN_ROWS,
            "maximum": TERMINAL_MAX_ROWS,
            "default": TERMINAL_DEFAULT_ROWS,
            "description": ("Terminal height; default applies only on start. Required for resize."),
        },
        "lines": {
            "type": "integer",
            "minimum": 1,
            "maximum": TERMINAL_STATUS_MAX_LINES,
            "default": TERMINAL_STATUS_DEFAULT_LINES,
            "description": (
                "Prior scrollback lines for status. May be used with or without cursor; the "
                "current rendered screen is returned separately."
            ),
        },
        "cursor": {
            "type": "string",
            "description": (
                "Signed older-scrollback continuation returned by status. May be combined with "
                "lines to choose a page size; prefer passing scrollback.next_request unchanged."
            ),
        },
        "after_revision": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "For wait, return only after a newer generic activity revision. Omit to return for "
                "any currently unacknowledged revision or the next output-settled, exit, or error "
                "event."
            ),
        },
        "timeout_ms": {
            "type": "integer",
            "minimum": 0,
            "maximum": TERMINAL_MAX_WAIT_MS,
            "default": TERMINAL_DEFAULT_WAIT_MS,
            "description": (
                "Maximum same-Run wait in milliseconds; the terminal continues after timeout."
            ),
        },
        "key": {
            "type": "string",
            "enum": list(TERMINAL_KEYS),
            "description": (
                "Named terminal key for input; may be combined with text. Use data for any "
                "other sequence."
            ),
        },
        "expected_screen_revision": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "For input, require the exact screen_revision previously inspected. Use this "
                "when answering a prompt so stale input is rejected instead of sent elsewhere."
            ),
        },
    },
    "required": ["action"],
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
    unsupported = sorted(set(arguments) - _ACTION_FIELDS[action])
    if unsupported:
        return tool_failure(
            "invalid_arguments",
            f"Action '{action}' does not accept: {', '.join(unsupported)}",
            retryable=False,
        )

    try:
        if action == "start":
            return await _handle_start(terminal_manager, projects, context, arguments)
        if action == "list":
            return _handle_list(terminal_manager, context)
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
        return tool_failure("terminal_not_found", "Terminal Session not found", retryable=False)
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
    raw_command = arguments.get("command")
    if raw_command in (None, ""):
        argv = default_terminal_argv()
    else:
        argv = [required_string(raw_command, field_name="command")]
    argv.extend(_optional_string_array(arguments.get("args"), field_name="args"))
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
    assert columns is not None and rows is not None
    owner = _owner(context)
    session = await terminal_manager.spawn(
        owner,
        argv,
        cwd=workdir,
        env=None,
        columns=columns,
        rows=rows,
        origin_run_id=context.run_id,
        name=name,
        initial_text=text if isinstance(text, str) else None,
    )
    snapshot = await terminal_manager.snapshot(session.terminal_id, owner)
    data = _project_snapshot(terminal_manager, snapshot)
    data.update(
        {
            "delivery": "automatic_terminal_activity",
            "handoff_note": (
                "The Terminal Session continues independently of this vBot Run. vBot will wake "
                "you after output settles following Agent input, or if the process exits or the "
                "terminal fails. Quiet output does not prove that the program finished or needs "
                "input, so inspect status when resumed. You may finish this Run after reporting "
                "that the program is running; do not poll merely to wait and do not start a "
                "duplicate process."
            ),
        }
    )
    return tool_success(data)


def _handle_list(terminal_manager: TerminalManager, context: ToolContext) -> JsonObject:
    sessions = terminal_manager.list_sessions(_owner(context))
    return tool_success({"terminals": [_terminal_summary(session) for session in sessions]})


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
    cursor = arguments.get("cursor")
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor.strip():
            raise ValueError("cursor must be a non-empty string")
        before = terminal_manager.decode_cursor(cursor, terminal_id)
    else:
        before = None
    owner = _owner(context)
    snapshot = await terminal_manager.snapshot(terminal_id, owner, lines=lines, before=before)
    _acknowledge_after_persistence(terminal_manager, context, owner, snapshot)
    return tool_success(_project_snapshot(terminal_manager, snapshot, page_lines=lines))


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
    timeout_ms = optional_int(
        arguments.get("timeout_ms"),
        field_name="timeout_ms",
        default=TERMINAL_DEFAULT_WAIT_MS,
        minimum=0,
        maximum=TERMINAL_MAX_WAIT_MS,
    )
    assert timeout_ms is not None
    snapshot, timed_out = await terminal_manager.wait_for_attention(
        terminal_id,
        owner,
        after_revision=after_revision,
        timeout_ms=timeout_ms,
    )
    _acknowledge_after_persistence(terminal_manager, context, owner, snapshot)
    data = _project_snapshot(terminal_manager, snapshot)
    data["timed_out"] = timed_out
    return tool_success(data)


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
        raise ValueError("data cannot be combined with text or key")
    if key is not None and key not in TERMINAL_KEYS:
        raise ValueError(f"key must be one of: {', '.join(TERMINAL_KEYS)}")
    expected_revision = optional_int(
        arguments.get("expected_screen_revision"),
        field_name="expected_screen_revision",
        default=None,
        minimum=0,
    )
    owner = _owner(context)
    session = terminal_manager.get_session(terminal_id, owner)
    prior_attention_revision = session.attention_revision if session.attention is not None else None
    data = await terminal_manager.send_input(
        terminal_id,
        owner,
        data=raw_data if isinstance(raw_data, str) else None,
        text=text if isinstance(text, str) else None,
        key=key,
        expected_screen_revision=expected_revision,
        origin_run_id=context.run_id,
    )
    if data["characters_sent"]:
        if prior_attention_revision is not None:
            context.after_result_persisted(
                lambda: terminal_manager.acknowledge_attention(
                    terminal_id, owner, prior_attention_revision
                )
            )
        data["delivery"] = "automatic_terminal_activity"
    return tool_success(data)


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
    session = terminal_manager.get_session(terminal_id, owner)
    prior_attention_revision = session.attention_revision if session.attention is not None else None
    snapshot = await terminal_manager.kill(terminal_id, owner)
    if prior_attention_revision is not None:
        context.after_result_persisted(
            lambda: terminal_manager.acknowledge_attention(
                terminal_id, owner, prior_attention_revision
            )
        )
    return tool_success(_project_snapshot(terminal_manager, snapshot))


def _project_snapshot(
    terminal_manager: TerminalManager,
    snapshot: dict[str, Any],
    *,
    page_lines: int = TERMINAL_STATUS_DEFAULT_LINES,
) -> JsonObject:
    projected = dict(snapshot)
    scrollback = dict(projected.get("scrollback", {}))
    before = scrollback.pop("next_before", None)
    next_cursor = (
        terminal_manager.encode_cursor(str(snapshot["terminal_id"]), before)
        if isinstance(before, int)
        else None
    )
    scrollback["next_cursor"] = next_cursor
    scrollback["next_request"] = (
        {
            "action": "status",
            "terminal_id": str(snapshot["terminal_id"]),
            "cursor": next_cursor,
            "lines": page_lines,
        }
        if next_cursor is not None
        else None
    )
    projected["scrollback"] = scrollback
    return projected


def _terminal_summary(session: TerminalSession) -> JsonObject:
    attention = session.attention
    return {
        "terminal_id": session.terminal_id,
        "state": session.state,
        "command": session.command,
        "name": session.name,
        "title": session.renderer.title,
        "workdir": model_path(session.cwd),
        "pid": session.adapter.pid,
        "exit_code": session.exit_code,
        "started_at": session.started_at.isoformat(),
        "finished_at": session.finished_at.isoformat() if session.finished_at else None,
        "screen_revision": session.renderer.revision,
        "attention_revision": session.attention_revision,
        "attention_kind": attention.kind if attention is not None else None,
    }


def _acknowledge_after_persistence(
    terminal_manager: TerminalManager,
    context: ToolContext,
    owner: TerminalOwner,
    snapshot: dict[str, Any],
) -> None:
    attention = snapshot.get("attention")
    if not isinstance(attention, dict) or not isinstance(attention.get("revision"), int):
        return
    terminal_id = str(snapshot["terminal_id"])
    revision = int(attention["revision"])
    context.after_result_persisted(
        lambda: terminal_manager.acknowledge_attention(terminal_id, owner, revision)
    )


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
        result_schema={"type": "object"},
        display=ToolDisplay(
            parts_builder=_terminal_display_parts,
            fact_builder=result_count_fact_builder("terminals", when_arguments={"action": "list"}),
        ),
    )


def _terminal_display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
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
    "make_terminal_handler",
    "register_terminal_tool",
]
