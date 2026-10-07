"""Terminal Tool argument dialects.

Agents call the terminal Tool with habits from other harnesses: Codex
``write_stdin`` ``chars`` and ``yield_time_ms``, ``session_id`` for the terminal
id, ``enter: true``, key names such as ``"Ctrl+C"``, ``close`` for kill, text
ending in a line break to submit it, and every optional field filled with a
placeholder. The normalizer maps each shape whose intent is exact onto the
advertised fields and drops values that request nothing. A value that asks for
an effect the chosen action cannot provide fails before anything runs, with the
call that provides it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from functools import cache
from typing import Any

from core.tools.call_syntax import SpellingAliases, normalize_call_arguments
from core.tools.contracts import ToolContract, compile_tool_contract
from core.tools.terminal_manager import TERMINAL_INPUT_KEY_SEQUENCES

TERMINAL_KEY_SUMMARY = (
    "enter, escape, tab, shift_tab, backspace, insert, delete, home, end, page_up, "
    "page_down, up, down, left, right, f1-f12, ctrl_a-ctrl_z"
)
_ENTER = TERMINAL_INPUT_KEY_SEQUENCES["enter"]

_FIELD_ALIASES = SpellingAliases(
    {
        "terminal_id": (
            "session_id",
            "terminal",
            "term_id",
            "terminal_session_id",
            "id",
            # Background-command ids of other harnesses (Claude Code, Codex) and older vBot.
            "process_id",
            "proc_id",
            "bash_id",
            "shell_id",
            "task_id",
        ),
        "command": ("cmd", "program", "executable"),
        "workdir": ("cwd", "dir", "directory", "working_directory", "working_dir"),
        "text": ("input",),
        "data": ("chars", "raw"),
        "columns": ("cols", "width"),
        "rows": ("height",),
        "lines": ("limit", "max_lines"),
        "timeout_ms": (
            "timeout_millis",
            "timeout_milliseconds",
            "yield_time_ms",
            "yield_ms",
            "wait_ms",
        ),
        "timeout": ("timeout_seconds", "timeout_secs", "timeout_sec", "timeout_s", "seconds"),
        "expected_screen_revision": ("screen_revision", "expected_revision"),
        "after_revision": ("attention_revision", "since_revision"),
        "pattern": ("regex", "regexp", "until", "wait_for", "expect", "expected", "match"),
    }
)
_SUBMIT = "submit"
# The terminal's size follows the user's view of it; no call resizes it.
_RESIZE = "resize"
_ACTION_VALUES = {
    **dict.fromkeys(("start", "launch", "spawn", "create", "new"), "start"),
    **dict.fromkeys(("list", "ls", "listterminals", "terminals"), "list"),
    **dict.fromkeys(("attach", "connect", "bind"), "attach"),
    **dict.fromkeys(("detach", "disconnect", "release", "unbind"), "detach"),
    **dict.fromkeys(
        (
            "status",
            "read",
            "readscreen",
            "screen",
            "view",
            "show",
            "get",
            "poll",
            "check",
            "inspect",
            "info",
            "peek",
            "snapshot",
            "capture",
            "output",
            "log",
            "logs",
            "history",
        ),
        "status",
    ),
    **dict.fromkeys(("wait", "await", "waitfor", "waitforoutput", "join", "block"), "wait"),
    **dict.fromkeys(
        (
            "input",
            "type",
            "write",
            "send",
            "sendinput",
            "sendtext",
            "sendkeys",
            "key",
            "keys",
            "press",
            "presskey",
            "paste",
            "stdin",
            "writestdin",
        ),
        "input",
    ),
    **dict.fromkeys(("submit", "sendline", "writeline"), _SUBMIT),
    **dict.fromkeys(("resize", "setsize", "size"), _RESIZE),
    **dict.fromkeys(
        (
            "kill",
            "stop",
            "close",
            "terminate",
            "end",
            "destroy",
            "killsession",
            "killshell",
            "cancel",
            "abort",
        ),
        "kill",
    ),
}
# Flags other harnesses use to press Enter after the input.
_ENTER_FLAGS = frozenset({"enter", "submit", "pressenter", "sendenter", "appendenter"})

_KEY_ALIASES = {
    **{re.sub(r"[\s_+-]+", "", name): name for name in TERMINAL_INPUT_KEY_SEQUENCES},
    **dict.fromkeys(("return", "ret", "cr", "newline", "\r", "\n", "\r\n"), "enter"),
    **dict.fromkeys(("esc", "\x1b"), "escape"),
    **dict.fromkeys(("\t",), "tab"),
    **dict.fromkeys(("backtab",), "shift_tab"),
    **dict.fromkeys(("bs", "bksp", "\x7f", "\x08"), "backspace"),
    **dict.fromkeys(("ins",), "insert"),
    **dict.fromkeys(("del",), "delete"),
    **dict.fromkeys(("pgup", "pageup", "prior"), "page_up"),
    **dict.fromkeys(("pgdn", "pgdown", "pagedown", "next"), "page_down"),
    **{
        f"{prefix}{direction}{suffix}": direction
        for direction in ("up", "down", "left", "right")
        for prefix, suffix in (("arrow", ""), ("", "arrow"), ("cursor", ""), ("key", ""))
    },
}
_CTRL_KEY = re.compile(r"(?:ctrl|control|ctl|strg|\^)([a-z])")
_EMACS_CTRL_KEY = re.compile(r"c-([a-z])", re.IGNORECASE)

# Fields that only shape what a result shows, how long it waits, or guard input:
# where the action has no use for them they request nothing.
_OBSERVATION_FIELDS = (
    "lines",
    "start_line",
    "after_revision",
    "timeout_ms",
    "timeout",
    "pattern",
    "expected_screen_revision",
)
# Labels and start settings that change nothing on an existing terminal.
_START_LABELS = ("name", "group", "workdir")
_ACCEPTED_FIELDS = {
    "start": frozenset(
        {"terminal_id", "command", "args", "text", "workdir", "name", "group", "columns", "rows"}
    ),
    "list": frozenset(),
    "attach": frozenset({"terminal_id"}),
    "detach": frozenset({"terminal_id"}),
    "status": frozenset({"terminal_id", "lines", "start_line"}),
    "wait": frozenset({"terminal_id", "after_revision", "timeout_ms", "timeout", "pattern"}),
    # A timeout makes input wait for the reply, as wait does.
    "input": frozenset(
        {
            "terminal_id",
            "text",
            "data",
            "key",
            "expected_screen_revision",
            "timeout_ms",
            "timeout",
            "pattern",
        }
    ),
    "kill": frozenset({"terminal_id"}),
}
# Zero requests nothing for these fields; elsewhere zero is a real value.
_ZERO_PLACEHOLDERS = frozenset({"columns", "rows", "lines"})


@cache
def _repair_contract() -> ToolContract:
    # Imported lazily: terminal.py imports this module for its registration.
    from core.tools.terminal import (
        TERMINAL_TOOL_NAME,
        TERMINAL_TOOL_PARAMETERS,
        TERMINAL_UNADVERTISED_PARAMETERS,
    )

    schema = {
        **TERMINAL_TOOL_PARAMETERS,
        "properties": {
            **TERMINAL_TOOL_PARAMETERS["properties"],
            **TERMINAL_UNADVERTISED_PARAMETERS,
        },
    }
    return compile_tool_contract(
        name=TERMINAL_TOOL_NAME, input_schema=schema, require_closed_input=False
    )


def normalize_terminal_arguments(arguments: Any) -> Any:
    """Map one terminal call onto its action and the fields that action uses."""
    normalized = normalize_call_arguments(
        _repair_contract(),
        arguments,
        field_aliases=_FIELD_ALIASES,
        enum_fields=("action",),
        field_normalizers={"action": _action_value, "key": terminal_key_name},
        empty_as_omitted=("terminal_id", "command", "workdir", "name", "group", "key"),
    )
    if not isinstance(normalized, dict):
        return normalized
    press_enter = _pop_enter_flags(normalized)
    if normalized.get("action") == _SUBMIT:
        normalized["action"] = "input"
        press_enter = True
    _drop_placeholders(normalized)
    if "action" not in normalized and "command" in normalized and "terminal_id" not in normalized:
        # Only start takes a command, so a call that names a program starts it.
        normalized["action"] = "start"
    action = normalized.get("action")
    if action == _RESIZE:
        status = _call("status", normalized)
        raise ValueError(
            _not_run(
                "the terminal's size follows the user's view of it, so no call resizes it; "
                f"nothing was changed. To read the screen at its current size, call terminal "
                f"{status}."
            )
        )
    if not isinstance(action, str) or action not in _ACCEPTED_FIELDS:
        return normalized
    if action == "start":
        # start submits its text with Enter; Enter on a new program's prompt adds nothing.
        _normalize_start(normalized)
    elif action == "input":
        _normalize_input(normalized, press_enter)
        for key in ("timeout_ms", "timeout"):
            # A zero wait after input is no wait.
            if normalized.get(key) == 0:
                del normalized[key]
    elif press_enter:
        raise ValueError(_not_run(f"{action} sends no input; press Enter with the input action."))
    accepted = _ACCEPTED_FIELDS[action]
    for key in list(normalized):
        if key == "action" or key in accepted:
            continue
        if key in _OBSERVATION_FIELDS or key in _START_LABELS or key == "terminal_id":
            del normalized[key]
            continue
        if key in {"text", "data", "key"}:
            raise ValueError(_not_run(_input_elsewhere_problem(action, normalized)))
        if key in {"command", "args"}:
            raise ValueError(_not_run(_command_elsewhere_problem(action, normalized)))
        if key in {"columns", "rows"}:
            raise ValueError(
                _not_run(
                    f"{action} does not change the size: the terminal's size follows the user's "
                    "view of it. Repeat the call without columns and rows."
                )
            )
    return normalized


def terminal_key_name(value: Any) -> Any:
    """Return the named key a spelling such as "Ctrl+C", "^C", or "Return" means."""
    if not isinstance(value, str) or value in TERMINAL_INPUT_KEY_SEQUENCES:
        return value
    if value in _KEY_ALIASES:
        return _KEY_ALIASES[value]
    if len(value) == 1 and 1 <= ord(value) <= 26:
        return f"ctrl_{chr(ord(value) + 96)}"
    raw = value.strip()
    emacs = _EMACS_CTRL_KEY.fullmatch(raw)
    if emacs is not None:
        return f"ctrl_{emacs.group(1).lower()}"
    spelling = re.sub(r"[\s_+-]+", "", raw.casefold())
    if spelling in _KEY_ALIASES:
        return _KEY_ALIASES[spelling]
    control = _CTRL_KEY.fullmatch(spelling)
    if control is not None:
        return f"ctrl_{control.group(1)}"
    return value


def command_words(command: str) -> list[str]:
    """Split a command line into words the way the host's own shell groups them.

    Windows keeps backslashes, which are path separators there; quotes group
    words on both hosts.
    """
    if os.name != "nt":
        return shlex.split(command)
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    lexer.commenters = ""
    return list(lexer)


def _action_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return _ACTION_VALUES.get(re.sub(r"[\s_-]+", "", value.casefold()), value)


def _pop_enter_flags(arguments: dict[str, Any]) -> bool:
    press = False
    for key in list(arguments):
        if re.sub(r"[\s_-]+", "", key.casefold()) not in _ENTER_FLAGS:
            continue
        value = arguments.pop(key)
        if value in (None, False, 0, ""):
            continue
        if value is True or value == 1 or (isinstance(value, str) and value.casefold() == "true"):
            press = True
            continue
        raise ValueError(_not_run(f"{key} must be true or false."))
    return press


def _drop_placeholders(arguments: dict[str, Any]) -> None:
    for key in list(arguments):
        value = arguments[key]
        if (
            value is None
            or value == []
            or (key in {"text", "data"} and value == "")
            or (key in _ZERO_PLACEHOLDERS and value == 0 and not isinstance(value, bool))
            or (key == "args" and value == [""])
        ):
            del arguments[key]


def _normalize_start(arguments: dict[str, Any]) -> None:
    command = arguments.get("command")
    if isinstance(command, list) and command and all(isinstance(word, str) for word in command):
        # A Codex-style argv array: the program, then its arguments.
        if "args" in arguments:
            raise ValueError(
                _not_run(
                    "command is an argument list and args is set too; send the program in "
                    "command and every argument in args."
                )
            )
        arguments["command"], arguments["args"] = command[0], command[1:]
        if not arguments["args"]:
            del arguments["args"]
    key = arguments.pop("key", None)
    if key not in (None, "enter"):
        raise ValueError(
            _not_run(f'start presses no keys; send key "{key}" with the input action after start.')
        )
    # start presses Enter after its text; a line break at its end would be a second one.
    _submit_line_break(arguments)
    if "data" in arguments:
        raise ValueError(
            _not_run(
                "start sends only text, followed by Enter once the program is ready; send exact "
                "data with the input action after start."
            )
        )


def _normalize_input(arguments: dict[str, Any], press_enter: bool) -> None:
    if press_enter:
        key = arguments.get("key")
        if "data" in arguments and "text" not in arguments and key is None:
            # data is exact; Enter is the carriage return the enter key sends.
            arguments["data"] = f"{arguments['data']}{_ENTER}"
            return
        if key not in (None, "enter"):
            raise ValueError(
                _not_run(
                    f'enter asks for Enter and key asks for "{key}"; one input call sends one '
                    "key. Send them in two input calls, or send both sequences as data."
                )
            )
        arguments["key"] = "enter"
    if arguments.get("key") in (None, "enter") and _submit_line_break(arguments):
        # Text that ends in a line break is meant to be submitted: press Enter
        # once, instead of typing the break into a pasted block.
        arguments["key"] = "enter"


def _submit_line_break(arguments: dict[str, Any]) -> bool:
    """Drop the line breaks that end text; True when there were any."""
    text = arguments.get("text")
    if not isinstance(text, str):
        return False
    stripped = text.rstrip("\r\n")
    if stripped == text:
        return False
    if stripped:
        arguments["text"] = stripped
    else:
        del arguments["text"]
    return True


def _input_elsewhere_problem(action: str, arguments: dict[str, Any]) -> str:
    fields = {key: arguments[key] for key in ("text", "data", "key") if key in arguments}
    return f"{action} sends no input; type with {_call('input', arguments, **fields)}."


def _command_elsewhere_problem(action: str, arguments: dict[str, Any]) -> str:
    command = arguments.get("command")
    args = arguments.get("args")
    words = [command] if isinstance(command, str) else []
    if isinstance(args, list):
        words.extend(str(word) for word in args)
    line = " ".join(words)
    if action == "input" and line:
        return (
            "input types text instead of running a command; to run it in this terminal, send "
            f"{_call('input', arguments, text=line, key='enter')}."
        )
    return f"command and args start a new program only with start; {action} leaves them unused."


def _call(action: str, arguments: dict[str, Any], **fields: Any) -> str:
    call: dict[str, Any] = {"action": action}
    if "terminal_id" in arguments:
        call["terminal_id"] = arguments["terminal_id"]
    call.update(fields)
    return json.dumps(call, ensure_ascii=False)


def _not_run(problem: str) -> str:
    return f"terminal was not run: {problem}"


__all__ = [
    "TERMINAL_KEY_SUMMARY",
    "command_words",
    "normalize_terminal_arguments",
    "terminal_key_name",
]
