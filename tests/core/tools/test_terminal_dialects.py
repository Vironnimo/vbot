"""Terminal Tool: input, calls in other harnesses' shapes, and what results show."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from core.providers._tool_result_text import render_tool_result_envelope
from core.tools import terminal as terminal_module
from core.tools._terminal_arguments import normalize_terminal_arguments, terminal_key_name
from core.tools.terminal_manager import TerminalManager, TerminalOwner
from core.tools.tools import JsonObject, ToolContext, tool_failure
from tests.core.tools.terminal_helpers import call, details, make_context
from tests.core.tools.terminal_helpers import manager as manager
from tests.core.tools.terminal_manager_helpers import AdapterFactory, eventually

OWNER = TerminalOwner("project-a", "agent-a", "session-a")


class Terminal:
    """Make the Agent's terminal calls as the Tool executor runs them."""

    def __init__(self, terminal_manager: TerminalManager, tmp_path: Path) -> None:
        self.manager = terminal_manager
        self.context = make_context(tmp_path)
        self.tmp_path = tmp_path

    def details(self, arguments: JsonObject, result: dict[str, Any]) -> list[JsonObject]:
        """Return the detail blocks the user sees for one call."""
        return details(self.manager, self.tmp_path, arguments, result)

    async def __call__(
        self, arguments: JsonObject, context: ToolContext | None = None
    ) -> dict[str, Any]:
        return cast(dict[str, Any], await call(self.manager, context or self.context, arguments))

    async def start(self, **fields: Any) -> str:
        result = await self({"action": "start", "command": "fake-tui", **fields})
        assert result["ok"] is True, result
        return str(result["data"]["terminal_id"])


@pytest.fixture
def terminal(manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path) -> Terminal:
    return Terminal(manager[0], tmp_path)


def _error(result: dict[str, Any]) -> dict[str, Any]:
    assert result["ok"] is False, result
    return cast(dict[str, Any], result["error"])


@pytest.mark.asyncio
async def test_start_ignores_placeholders_that_request_nothing(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    result = await terminal(
        {
            "action": "start",
            "command": "fake-tui",
            "terminal_id": "",
            "after_revision": 0,
            "expected_screen_revision": 0,
            "start_line": 0,
            "lines": 0,
            "columns": 0,
            "rows": 0,
            "timeout_ms": 1000,
            "data": "",
            "text": "",
            "key": "enter",
            "args": [],
        }
    )

    assert result["ok"] is True
    assert "note" not in result["data"]
    assert manager[1].calls[0][0] == ["fake-tui"]
    assert manager[1].calls[0][3:] == (24, 80)
    # No start input is queued, so nothing is typed.
    assert result["data"]["state"] == "running"
    assert manager[1].adapters[0].writes == []


@pytest.mark.asyncio
async def test_start_applies_a_requested_size(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    result = await terminal({"action": "start", "command": "fake-tui", "cols": 120, "rows": 32})

    assert result["ok"] is True
    assert manager[1].calls[0][3:] == (32, 120)
    too_narrow = await terminal({"action": "start", "command": "fake-tui", "columns": 30})
    assert "columns" in _error(too_narrow)["message"]
    assert len(manager[1].calls) == 1


@pytest.mark.asyncio
async def test_start_splits_a_command_line_whose_first_word_is_a_program(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    executable = Path(sys.executable).as_posix()
    result = await terminal({"action": "start", "command": f'"{executable}" -q'})

    assert result["ok"] is True
    assert manager[1].calls[-1][0] == [executable, "-q"]
    assert result["data"]["note"] == (
        f'command "\\"{executable}\\" -q" is a whole command line, so it started as command '
        f'"{executable}" with args ["-q"].'.replace('\\"', '"')
    )

    combined = await terminal(
        {"action": "start", "command": f'"{executable}" -q', "args": ["-c", "pass"]}
    )
    assert combined["ok"] is True
    assert manager[1].calls[-1][0] == [executable, "-q", "-c", "pass"]

    unknown = await terminal({"action": "start", "command": "no-such-program-xyz --flag"})
    assert unknown["ok"] is True
    assert manager[1].calls[-1][0] == ["no-such-program-xyz --flag"]
    assert "note" not in unknown["data"]

    spaced = tmp_path / "dir with space" / "tool"
    spaced.parent.mkdir()
    spaced.write_text("", encoding="utf-8")
    path_result = await terminal({"action": "start", "command": str(spaced)})
    assert path_result["ok"] is True
    assert manager[1].calls[-1][0] == [str(spaced)]


@pytest.mark.asyncio
async def test_start_takes_an_argument_list_as_command(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    result = await terminal({"action": "start", "command": ["fake-tui", "--flag", "a b"]})

    assert result["ok"] is True
    assert manager[1].calls[0][0] == ["fake-tui", "--flag", "a b"]
    both = await terminal({"action": "start", "command": ["fake-tui", "-x"], "args": ["-y"]})
    assert "every argument in args" in _error(both)["message"]
    assert len(manager[1].calls) == 1

    # Only start takes a command, so a call that names a program without an action starts it.
    implicit = await terminal({"command": "fake-tui", "args": ["-i"], "name": "console"})
    assert implicit["ok"] is True
    assert manager[1].calls[-1][0] == ["fake-tui", "-i"]


@pytest.mark.asyncio
async def test_start_with_a_terminal_id_opens_a_new_terminal_or_refuses_an_existing_one(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    fresh = await terminal({"action": "start", "command": "fake-tui", "terminal_id": "unused"})
    fresh_id = fresh["data"]["terminal_id"]
    assert fresh["data"]["note"] == f"start assigns the terminal_id: use {fresh_id}, not unused."

    existing = await terminal({"action": "start", "command": "fake-tui", "terminal_id": fresh_id})
    assert _error(existing)["message"] == (
        f"terminal was not run: start opens a new terminal. To type into {fresh_id}, call input "
        f'with terminal_id "{fresh_id}"; to start another terminal, omit terminal_id.'
    )
    assert len(manager[1].calls) == 1

    await terminal({"action": "kill", "terminal_id": fresh_id})
    restarted = await terminal({"action": "start", "command": "fake-tui", "terminal_id": fresh_id})
    restarted_id = restarted["data"]["terminal_id"]
    assert restarted_id != fresh_id
    assert restarted["data"]["note"] == (
        f"start assigns the terminal_id: use {restarted_id}, not {fresh_id}."
    )
    assert len(manager[1].calls) == 2


@pytest.mark.asyncio
async def test_start_refuses_input_it_cannot_send(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    key = await terminal({"action": "start", "command": "fake-tui", "key": "Esc"})
    assert 'send key "escape" with the input action' in _error(key)["message"]
    data = await terminal({"action": "start", "command": "fake-tui", "data": "\x03"})
    assert "send exact data with the input action" in _error(data)["message"]
    assert manager[1].calls == []


@pytest.mark.asyncio
async def test_input_types_text_keys_and_exact_data_against_the_current_screen(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    terminal_id = await terminal.start()
    adapter = manager[1].adapters[0]

    def revision() -> int:
        return terminal.manager.terminal(terminal_id, OWNER).screen_revision

    async def render(output: str) -> None:
        shown = revision()
        adapter.emit(output)
        await eventually(lambda: revision() > shown)

    async def send(**fields: Any) -> dict[str, Any]:
        return await terminal({"action": "input", "terminal_id": terminal_id, **fields})

    await render("QUESTION> ")
    stale = await send(text="answer", expected_screen_revision=0)
    assert stale == tool_failure(
        "stale_screen",
        f"The screen of {terminal_id} changed after the revision you passed, so the input was "
        "not sent. Read the current screen with terminal "
        + json.dumps({"action": "status", "terminal_id": terminal_id})
        + ", then send the input again if it still fits.",
        retryable=True,
    )
    typed = await send(text="answer", expected_screen_revision=revision())
    # The result shows the screen once the output settled after the input.
    assert typed["data"] == {
        "terminal_id": terminal_id,
        "state": "running",
        "screen": "QUESTION>",
        "note": "text was typed but not submitted. To submit it, call terminal "
        + json.dumps({"action": "input", "terminal_id": terminal_id, "key": "enter"})
        + ".",
    }
    submitted = await send(text="submit", key="enter")
    assert submitted["data"]["key"] == "enter"
    assert "note" not in submitted["data"]
    await send(key="f12")
    raw = "\x1b[200~more\r\n\x1b[201~"
    exact = await send(data=raw)
    # The user sees what was typed, with named keys and control characters spelled out,
    # and the screen the result shows.
    shown = [
        terminal.details({"action": "input", "terminal_id": terminal_id, **fields}, result)
        for fields, result in (
            ({"text": "submit", "key": "enter"}, submitted),
            ({"data": raw}, exact),
        )
    ]
    screen = {
        "type": "text",
        "label": "screen",
        "source": {"from": "result", "path": ["data", "screen"]},
    }
    assert shown == [
        [{"type": "text", "label": "input", "text": "submit <enter>"}, screen],
        [{"type": "text", "label": "input", "text": "\\x1b[200~more\\r\n\\x1b[201~"}, screen],
    ]
    assert adapter.writes == ["answer", "submit", "\r", "\x1b[24~", raw]
    # When the reply wait ends before the output settles, next says how the reply arrives.
    unsettled = await send(key="enter", timeout=0.01)
    assert unsettled["data"]["next"] == (
        "Its screen arrives as a new message when its output settles; continue other work or "
        "end your turn. To wait for it now instead, call terminal "
        + json.dumps({"action": "wait", "terminal_id": terminal_id})
        + "."
    )

    multiline = "first\n  second"
    await render("\x1b[?2004h")
    pasted = await send(text=multiline)
    assert adapter.writes[-1] == f"\x1b[200~{multiline}\x1b[201~"
    assert pasted["data"]["bracketed_paste"] is True
    await render("\x1b[?2004l")
    typed_lines = await send(text=multiline)
    assert adapter.writes[-1] == multiline
    assert typed_lines["data"]["bracketed_paste"] is False


@pytest.mark.parametrize(
    ("fields", "writes"),
    [({"text": "", "key": "enter"}, ["\r"]), ({"text": ""}, [])],
    ids=["key-remains", "nothing-remains"],
)
@pytest.mark.asyncio
async def test_empty_optional_input_fields_are_ignored(
    terminal: Terminal,
    manager: tuple[TerminalManager, AdapterFactory],
    fields: JsonObject,
    writes: list[str],
) -> None:
    terminal_id = await terminal.start()

    result = await terminal({"action": "input", "terminal_id": terminal_id, **fields})

    assert result["ok"] is True
    if writes:
        assert (result["data"]["state"], result["data"]["key"]) == ("running", "enter")
    else:
        # Input that sends nothing waits for no reply and says so.
        assert result["data"] == {
            "terminal_id": terminal_id,
            "note": "nothing was typed, because text, key and data were empty. To see what the "
            "terminal shows, call terminal "
            + json.dumps({"action": "status", "terminal_id": terminal_id})
            + ".",
        }
    assert manager[1].adapters[0].writes == writes


@pytest.mark.parametrize(
    ("spelling", "key"),
    [
        ("Ctrl+C", "ctrl_c"),
        ("ctrl-c", "ctrl_c"),
        ("CTRL_C", "ctrl_c"),
        ("^C", "ctrl_c"),
        ("C-c", "ctrl_c"),
        ("Control+D", "ctrl_d"),
        ("\x03", "ctrl_c"),
        ("Enter", "enter"),
        ("Return", "enter"),
        ("\r", "enter"),
        ("esc", "escape"),
        ("ArrowUp", "up"),
        ("Page Down", "page_down"),
        ("PgUp", "page_up"),
        ("Shift+Tab", "shift_tab"),
        ("F5", "f5"),
        ("del", "delete"),
    ],
)
def test_key_spellings_name_the_key(spelling: str, key: str) -> None:
    assert terminal_key_name(spelling) == key


@pytest.mark.parametrize("spelling", ["space", "ctrl+shift+c", "a", "Ctrl+1"])
def test_other_key_spellings_stay_as_sent(spelling: str) -> None:
    assert terminal_key_name(spelling) == spelling


@pytest.mark.asyncio
async def test_input_accepts_key_spellings_and_explains_unknown_keys(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    terminal_id = await terminal.start()
    adapter = manager[1].adapters[0]

    sent = await terminal({"action": "input", "terminal_id": terminal_id, "key": "Ctrl+C"})
    assert sent["data"]["key"] == "ctrl_c"
    await eventually(lambda: adapter.writes == ["\x03"])

    unknown = await terminal({"action": "input", "terminal_id": terminal_id, "key": "space"})
    assert _error(unknown)["message"] == (
        'terminal was not run: key "space" is not a named key. Named keys: enter, escape, tab, '
        "shift_tab, backspace, insert, delete, home, end, page_up, page_down, up, down, left, "
        "right, f1-f12, "
        "ctrl_a-ctrl_z. Type other characters as text, or send exact sequences as data."
    )
    assert adapter.writes == ["\x03"]


@pytest.mark.parametrize(
    ("fields", "writes"),
    [
        ({"text": "print(1)", "enter": True}, ["print(1)", "\r"]),
        ({"text": "print(1)", "submit": True, "key": "enter"}, ["print(1)", "\r"]),
        ({"data": "print(1)", "enter": True}, ["print(1)\r"]),
        ({"text": "print(1)", "enter": False}, ["print(1)"]),
        # Line breaks that end text submit it once, with or without key "enter".
        ({"text": "print(1)\n"}, ["print(1)", "\r"]),
        ({"text": "print(1)\r\n\r\n", "key": "enter"}, ["print(1)", "\r"]),
        ({"text": "a\nb\n"}, ["a\nb", "\r"]),
    ],
)
@pytest.mark.asyncio
async def test_enter_flags_and_ending_line_breaks_press_enter_after_the_input(
    terminal: Terminal,
    manager: tuple[TerminalManager, AdapterFactory],
    fields: JsonObject,
    writes: list[str],
) -> None:
    terminal_id = await terminal.start()

    result = await terminal({"action": "input", "terminal_id": terminal_id, **fields})

    assert result["ok"] is True
    assert manager[1].adapters[0].writes == writes
    # Only text typed without Enter carries the not-submitted note.
    assert ("note" in result["data"]) is (fields.get("enter") is False)


@pytest.mark.asyncio
async def test_enter_flag_and_another_key_conflict(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    terminal_id = await terminal.start()

    conflict = await terminal(
        {"action": "input", "terminal_id": terminal_id, "key": "escape", "enter": True}
    )
    assert 'enter asks for Enter and key asks for "escape"' in _error(conflict)["message"]
    unclear = await terminal(
        {"action": "input", "terminal_id": terminal_id, "text": "x", "enter": "y"}
    )
    assert "enter must be true or false" in _error(unclear)["message"]
    assert manager[1].adapters[0].writes == []


@pytest.mark.asyncio
async def test_codex_and_hermes_input_shapes_type_into_the_terminal(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    terminal_id = await terminal.start()
    adapter = manager[1].adapters[0]

    written = await terminal({"action": "write_stdin", "session_id": terminal_id, "chars": "ls\r"})
    assert written["ok"] is True
    await eventually(lambda: adapter.writes == ["ls\r"])

    submitted = await terminal({"action": "submit", "id": terminal_id, "input": "pwd"})
    assert submitted["ok"] is True
    await eventually(lambda: adapter.writes == ["ls\r", "pwd", "\r"])


@pytest.mark.parametrize(
    ("action", "canonical"),
    [
        ("close", "kill"),
        ("stop", "kill"),
        ("Terminate", "kill"),
        ("read", "status"),
        ("read_screen", "status"),
        ("type", "input"),
        ("send_keys", "input"),
        ("await", "wait"),
        ("launch", "start"),
        ("disconnect", "detach"),
        ("KillShell", "kill"),
        ("cancel", "kill"),
        ("join", "wait"),
    ],
)
def test_action_spellings_name_the_action(action: str, canonical: str) -> None:
    normalized = normalize_terminal_arguments({"action": action, "terminal_id": "term_a"})
    assert normalized["action"] == canonical


@pytest.mark.parametrize("field", ["process_id", "bash_id", "shell_id", "task_id"])
def test_background_command_id_spellings_name_the_terminal(field: str) -> None:
    normalized = normalize_terminal_arguments({"action": "status", field: "term_a"})
    assert normalized == {"action": "status", "terminal_id": "term_a"}


@pytest.mark.asyncio
async def test_close_stops_the_terminal(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    terminal_id = await terminal.start()

    arguments: JsonObject = {"action": "close", "terminal_id": terminal_id}
    result = await terminal(arguments)

    assert result["ok"] is True
    assert result["data"]["state"] == "stopped"
    assert manager[1].adapters[0].alive is False
    assert terminal.details(arguments, result)[-1] == {
        "type": "notice",
        "level": "info",
        "text": "The program was stopped.",
    }
    # Killing again stops nothing and says so.
    again = await terminal(arguments)
    assert (again["data"]["state"], again["data"]["note"]) == (
        "stopped",
        "The program had already ended; nothing was stopped.",
    )


@pytest.mark.asyncio
async def test_fields_another_action_owns_fail_with_the_call_that_uses_them(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    terminal_id = await terminal.start()

    waited = await terminal({"action": "wait", "terminal_id": terminal_id, "text": "y"})
    assert _error(waited)["message"] == (
        "terminal was not run: wait sends no input; type with "
        + json.dumps({"action": "input", "terminal_id": terminal_id, "text": "y"})
        + "."
    )
    sized = await terminal({"action": "status", "terminal_id": terminal_id, "columns": 120})
    assert _error(sized)["message"] == (
        "terminal was not run: status does not change the size: the terminal's size follows "
        "the user's view of it. Repeat the call without columns and rows."
    )
    resized = await terminal(
        {"action": "resize", "terminal_id": terminal_id, "columns": 120, "rows": 40}
    )
    assert _error(resized)["message"] == (
        "terminal was not run: the terminal's size follows the user's view of it, so no call "
        "resizes it; nothing was changed. To read the screen at its current size, call terminal "
        + json.dumps({"action": "status", "terminal_id": terminal_id})
        + "."
    )
    commanded = await terminal({"action": "input", "terminal_id": terminal_id, "command": "ls -la"})
    assert _error(commanded)["message"] == (
        "terminal was not run: input types text instead of running a command; to run it in "
        "this terminal, send "
        + json.dumps(
            {"action": "input", "terminal_id": terminal_id, "text": "ls -la", "key": "enter"}
        )
        + "."
    )
    assert manager[1].adapters[0].writes == []
    assert manager[1].adapters[0].resizes == []


@pytest.mark.asyncio
async def test_fields_that_only_shape_a_result_are_dropped_where_unused(
    terminal: Terminal,
) -> None:
    terminal_id = await terminal.start(name="build")

    listed = await terminal({"action": "list", "terminal_id": terminal_id})
    assert [item["terminal_id"] for item in listed["data"]["terminals"]] == [terminal_id]
    killed = await terminal(
        {
            "action": "kill",
            "terminal_id": terminal_id,
            "timeout_ms": 5000,
            "lines": 40,
            "workdir": "elsewhere",
            "name": "build",
        }
    )
    assert killed["ok"] is True


@pytest.mark.asyncio
async def test_wait_caps_a_long_timeout_and_says_so(
    terminal: Terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "TERMINAL_WAIT_MAX_SECONDS", 0.2)
    terminal_id = await terminal.start()

    waited = await terminal({"action": "wait", "terminal_id": terminal_id, "timeout": 90})
    assert (waited["data"]["wait_ended"], waited["data"]["note"]) == (
        "timeout",
        "A wait lasts at most 0.2 seconds, so this one ended after 0.2; wait again to keep "
        "following the program.",
    )
    # An unadvertised wait after input is capped the same way, and a reply that settles
    # before the cap needs no note.
    typed = await terminal(
        {"action": "input", "terminal_id": terminal_id, "key": "enter", "yield_time_ms": 60000}
    )
    assert "note" not in typed["data"]


@pytest.mark.asyncio
async def test_wait_reads_seconds_and_refuses_disagreeing_timeouts(terminal: Terminal) -> None:
    terminal_id = await terminal.start()

    for timeout in ({"timeout": 0.05}, {"timeout_ms": 50}):
        waited = await terminal({"action": "wait", "terminal_id": terminal_id, **timeout})
        assert waited["data"]["wait_ended"] == "timeout"
        assert "note" not in waited["data"]

    conflict = await terminal(
        {"action": "wait", "terminal_id": terminal_id, "timeout": 2, "timeout_ms": 50}
    )
    assert _error(conflict)["message"] == (
        "terminal was not run: timeout (2 s) and timeout_ms (50 ms) disagree; send one of them."
    )


@pytest.mark.parametrize("ending", ["matched", "quiet", "exited"])
@pytest.mark.asyncio
async def test_wait_ends_at_the_exit_a_matching_line_or_quiet_output(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory], ending: str
) -> None:
    terminal_id = await terminal.start()
    adapter = manager[1].adapters[0]
    arguments: JsonObject = {"action": "wait", "terminal_id": terminal_id}
    if ending == "matched":
        # Output printed before the call counts, matched case-insensitively and line by line.
        adapter.emit("booting\r\nServer LISTENING on :8080\r\n")
        await eventually(lambda: terminal.manager.terminal(terminal_id, OWNER).screen_revision > 0)
        arguments["pattern"] = r"^server listening on :\d+$"
    elif ending == "quiet":
        await terminal(
            {"action": "input", "terminal_id": terminal_id, "text": "go", "key": "enter"}
        )
        adapter.emit("done\r\n> ")
    else:
        adapter.emit("bye\r\n")
        adapter.finish(4)

    result = await terminal(arguments)

    data = result["data"]
    assert data["wait_ended"] == ending
    if ending == "exited":
        assert (data["state"], data["exit_code"], data["screen"]) == ("exited", 4, "bye")
        assert terminal.details(arguments, result)[-1] == {
            "type": "notice",
            "level": "warning",
            "text": "The program exited with code 4.",
        }
    else:
        assert data["state"] == "running"


@pytest.mark.asyncio
async def test_pattern_waits_through_quiet_output_and_reads_invalid_syntax_as_text(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    terminal_id = await terminal.start()
    adapter = manager[1].adapters[0]
    await terminal({"action": "input", "terminal_id": terminal_id, "text": "make", "key": "enter"})
    # The program echoes the input line, then prints.
    adapter.emit("make\r\n[error] disk full\r\n")

    # The output settles during the wait, which waits on for its pattern; the echo of the
    # input does not match it.
    arguments: JsonObject = {
        "action": "wait",
        "terminal_id": terminal_id,
        "pattern": "^make|build finished",
        "timeout": 0.3,
    }
    waited = await terminal(arguments)
    assert waited["data"]["wait_ended"] == "timeout"
    assert terminal.details(arguments, waited)[-1] == {
        "type": "notice",
        "level": "info",
        "text": "The program was still running when the wait ended.",
    }

    literal = await terminal({"action": "wait", "terminal_id": terminal_id, "pattern": "[ERROR"})
    assert literal["data"]["wait_ended"] == "matched"
    note = literal["data"]["note"]
    assert note.startswith("pattern is not a valid regular expression (")
    assert note.endswith("), so it was matched as literal text.")


@pytest.mark.asyncio
async def test_input_returns_the_reply_once_its_output_settles(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    terminal_id = await terminal.start()
    adapter = manager[1].adapters[0]

    async def answer() -> None:
        await eventually(lambda: adapter.writes == ["print(6*7)", "\r"])
        adapter.emit(">>> print(6*7)\r\n42\r\n>>> ")

    responder = asyncio.create_task(answer())
    result = await terminal({"action": "input", "terminal_id": terminal_id, "text": "print(6*7)\n"})
    await responder

    data = result["data"]
    assert data["screen"].splitlines() == [">>> print(6*7)", "42", ">>>"]
    assert list(data) == ["terminal_id", "state", "screen", "key"]


@pytest.mark.asyncio
async def test_unknown_terminal_id_lists_attached_terminals(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    removed = (
        "No terminal with the id term_missing is open for this Session: the id is wrong or "
        "belongs to another Session, or its program ended more than 30 minutes ago and the "
        "terminal was removed, with its output."
    )
    listing = 'To see all terminals, call terminal {"action": "list"}.'
    missing = await terminal({"action": "status", "terminal_id": "term_missing"})
    assert _error(missing)["message"] == f"{removed} {listing}"

    first = await terminal.start(name="build")
    second = await terminal.start()
    await terminal({"action": "kill", "terminal_id": first})
    listed = await terminal({"action": "wait", "terminal_id": "term_missing", "timeout": 0.01})
    assert _error(listed)["message"] == (
        f"{removed} Terminals attached to this Session: {second} (running: fake-tui); {first} "
        f"(stopped: build). {listing}"
    )

    # Another Session's terminals are not attached to it.
    other = make_context(tmp_path, session_id="session-b")
    unattached = await terminal({"action": "status", "terminal_id": "term_missing"}, other)
    assert _error(unattached)["message"] == f"{removed} {listing}"


@pytest.mark.asyncio
async def test_missing_terminal_id_names_the_attached_terminals(terminal: Terminal) -> None:
    terminal_id = await terminal.start()

    result = await terminal({"action": "status", "name": "build"})

    assert _error(result)["message"] == (
        f"terminal was not run: status needs terminal_id. The terminal attached to this Session "
        f'is {terminal_id} (running: fake-tui); repeat the call with terminal_id "{terminal_id}".'
    )


@pytest.mark.asyncio
async def test_follow_up_results_show_the_screen_without_repeating_launch_facts(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    started = await terminal({"action": "start", "command": "fake-tui", "args": ["-q"]})
    assert list(started["data"]) == ["terminal_id", "state", "screen"]
    terminal_id = started["data"]["terminal_id"]
    manager[1].adapters[0].emit("".join(f"line-{index}\r\n" for index in range(30)) + "ready> ")
    await eventually(lambda: terminal.manager.terminal(terminal_id, OWNER).screen_revision > 0)

    wait: JsonObject = {"action": "wait", "terminal_id": terminal_id, "pattern": "ready>"}
    waited = await terminal(wait)
    assert list(waited["data"]) == [
        "terminal_id",
        "state",
        "history",
        "screen",
        "scrollback",
        "wait_ended",
        "matched",
    ]
    assert waited["data"]["matched"] == "ready>"
    # The user sees the lines above the screen and the screen, read from the result.
    assert terminal.details(wait, waited) == [
        {"type": "text", "label": label, "source": {"from": "result", "path": ["data", key]}}
        for label, key in (("scrollback", "history"), ("screen", "screen"))
    ]
    assert waited["data"]["history"].splitlines() == [f"line-{index}" for index in range(7)]
    assert "text" not in waited["data"]["scrollback"]
    text = render_tool_result_envelope(waited)
    assert "history:\n  line-0\n  line-1\n" in text
    assert "attention" not in text

    status = await terminal({"action": "status", "terminal_id": terminal_id})
    assert {"program", "workdir"} <= set(status["data"])
    assert status["data"]["program"] == "fake-tui -q"
    # The log sentence says what the file holds; the file has the raw output.
    log = re.fullmatch(
        r"Everything the program printed is written live to (\S+), as raw text with its "
        r"terminal control sequences\.",
        status["data"]["log"],
    )
    assert log is not None, status["data"]["log"]
    assert "line-29\r\nready> " in Path(log[1]).read_bytes().decode("utf-8")
    assert "log_file" not in status["data"]
    hidden = {"attention", "attention_revision", "screen_revision", "started_at", "columns"}
    assert not hidden & set(status["data"])
