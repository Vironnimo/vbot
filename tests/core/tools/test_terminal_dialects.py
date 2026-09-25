"""Terminal: calls in other harnesses' shapes, and what results show."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from core.projects import ProjectStore
from core.providers._tool_result_text import render_tool_result_envelope
from core.tools import terminal as terminal_module
from core.tools._terminal_arguments import normalize_terminal_arguments, terminal_key_name
from core.tools.terminal import TERMINAL_TOOL_NAME, register_terminal_tool
from core.tools.terminal_manager import TerminalManager, TerminalOwner
from core.tools.tools import JsonObject, ToolContext, ToolRegistry
from tests.core.tools.terminal_helpers import make_context
from tests.core.tools.terminal_helpers import manager as manager
from tests.core.tools.terminal_manager_helpers import AdapterFactory, eventually

OWNER = TerminalOwner("project-a", "agent-a", "session-a")


class Terminal:
    """Dispatch terminal calls through the production registry path."""

    def __init__(self, terminal_manager: TerminalManager, tmp_path: Path) -> None:
        self.manager = terminal_manager
        self.registry = ToolRegistry()
        register_terminal_tool(self.registry, terminal_manager, ProjectStore(tmp_path))
        self.context = make_context(tmp_path)

    async def __call__(
        self, arguments: JsonObject, context: ToolContext | None = None
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.registry.dispatch(context or self.context, arguments, [TERMINAL_TOOL_NAME]),
        )

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
    await asyncio.sleep(0.1)
    assert manager[1].adapters[0].writes == []


@pytest.mark.asyncio
async def test_start_applies_a_requested_size(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    result = await terminal({"action": "start", "command": "fake-tui", "cols": 120, "rows": 32})

    assert result["ok"] is True
    assert (result["data"]["columns"], result["data"]["rows"]) == (120, 32)
    assert manager[1].calls[0][3:] == (32, 120)
    with pytest.raises(ValueError, match="columns"):
        await terminal({"action": "start", "command": "fake-tui", "columns": 30})
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
    with pytest.raises(ValueError, match="every argument in args"):
        await terminal({"action": "start", "command": ["fake-tui", "-x"], "args": ["-y"]})
    assert len(manager[1].calls) == 1


@pytest.mark.asyncio
async def test_start_with_a_terminal_id_opens_a_new_terminal_or_refuses_an_existing_one(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    fresh = await terminal({"action": "start", "command": "fake-tui", "terminal_id": "unused"})
    fresh_id = fresh["data"]["terminal_id"]
    assert fresh["data"]["note"] == f"start assigns the terminal_id: use {fresh_id}, not unused."

    existing = await terminal({"action": "start", "command": "fake-tui", "terminal_id": fresh_id})
    message = _error(existing)["message"]
    assert message.startswith("start opens a new terminal and was not run.")
    assert (
        json.dumps({"action": "input", "terminal_id": fresh_id, "text": "...", "key": "enter"})
        in message
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
    with pytest.raises(ValueError, match='send key "escape" with the input action'):
        await terminal({"action": "start", "command": "fake-tui", "key": "Esc"})
    with pytest.raises(ValueError, match="send exact data with the input action"):
        await terminal({"action": "start", "command": "fake-tui", "data": "\x03"})
    assert manager[1].calls == []


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
        'key "space" is not a named key. Named keys: enter, escape, tab, shift_tab, backspace, '
        "insert, delete, home, end, page_up, page_down, up, down, left, right, f1-f12, "
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
    ],
)
@pytest.mark.asyncio
async def test_enter_flags_press_enter_after_the_input(
    terminal: Terminal,
    manager: tuple[TerminalManager, AdapterFactory],
    fields: JsonObject,
    writes: list[str],
) -> None:
    terminal_id = await terminal.start()

    result = await terminal({"action": "input", "terminal_id": terminal_id, **fields})

    assert result["ok"] is True
    await eventually(lambda: manager[1].adapters[0].writes == writes)


@pytest.mark.asyncio
async def test_enter_flag_and_another_key_conflict(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    terminal_id = await terminal.start()

    with pytest.raises(ValueError, match='enter asks for Enter and key asks for "escape"'):
        await terminal(
            {"action": "input", "terminal_id": terminal_id, "key": "escape", "enter": True}
        )
    with pytest.raises(ValueError, match="enter must be true or false"):
        await terminal({"action": "input", "terminal_id": terminal_id, "text": "x", "enter": "y"})
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
    ],
)
def test_action_spellings_name_the_action(action: str, canonical: str) -> None:
    normalized = normalize_terminal_arguments({"action": action, "terminal_id": "term_a"})
    assert normalized["action"] == canonical


@pytest.mark.asyncio
async def test_close_stops_the_terminal(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    terminal_id = await terminal.start()

    result = await terminal({"action": "close", "terminal_id": terminal_id})

    assert result["ok"] is True
    assert result["data"]["state"] == "exited"
    assert manager[1].adapters[0].alive is False


@pytest.mark.asyncio
async def test_fields_another_action_owns_fail_with_the_call_that_uses_them(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    terminal_id = await terminal.start()

    with pytest.raises(ValueError) as waited:
        await terminal({"action": "wait", "terminal_id": terminal_id, "text": "y"})
    assert str(waited.value) == (
        "terminal was not run: wait sends no input; type with "
        + json.dumps({"action": "input", "terminal_id": terminal_id, "text": "y"})
        + "."
    )
    with pytest.raises(ValueError) as sized:
        await terminal({"action": "status", "terminal_id": terminal_id, "columns": 120})
    assert str(sized.value) == (
        "terminal was not run: status does not change the size; resize with "
        + json.dumps({"action": "resize", "terminal_id": terminal_id, "columns": 120})
        + "."
    )
    with pytest.raises(ValueError) as commanded:
        await terminal({"action": "input", "terminal_id": terminal_id, "command": "ls -la"})
    assert str(commanded.value) == (
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
    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_WAIT_MS", 50)
    terminal_id = await terminal.start()

    result = await terminal({"action": "wait", "terminal_id": terminal_id, "timeout_ms": 90000})

    assert result["ok"] is True
    assert result["data"]["timed_out"] is True
    assert result["data"]["note"] == (
        "A wait lasts at most 50 ms; wait again if the program is still busy."
    )


@pytest.mark.asyncio
async def test_wait_reads_seconds_and_refuses_disagreeing_timeouts(terminal: Terminal) -> None:
    terminal_id = await terminal.start()

    seconds = await terminal({"action": "wait", "terminal_id": terminal_id, "timeout": 0.05})
    assert seconds["data"]["timed_out"] is True
    assert "note" not in seconds["data"]

    conflict = await terminal(
        {"action": "wait", "terminal_id": terminal_id, "timeout": 2, "timeout_ms": 50}
    )
    assert _error(conflict)["message"] == (
        "terminal was not run: timeout (2 s) and timeout_ms (50 ms) disagree; send one of them."
    )


@pytest.mark.asyncio
async def test_input_with_a_timeout_returns_the_settled_reply(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    terminal_id = await terminal.start()
    adapter = manager[1].adapters[0]

    async def answer() -> None:
        await eventually(lambda: adapter.writes == ["print(6*7)", "\r"])
        adapter.emit(">>> print(6*7)\r\n42\r\n>>> ")

    responder = asyncio.create_task(answer())
    result = await terminal(
        {
            "action": "input",
            "terminal_id": terminal_id,
            "text": "print(6*7)",
            "key": "enter",
            "timeout_ms": 5000,
        }
    )
    await responder

    data = result["data"]
    assert data["timed_out"] is False
    assert data["characters_sent"] == 11
    assert data["screen"].splitlines() == [">>> print(6*7)", "42", ">>>"]
    assert "delivery" not in data
    assert list(data) == [
        "terminal_id",
        "state",
        "characters_sent",
        "key",
        "screen_revision",
        "screen",
        "timed_out",
    ]


@pytest.mark.asyncio
async def test_input_without_a_positive_timeout_returns_at_once(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    terminal_id = await terminal.start()

    result = await terminal(
        {"action": "input", "terminal_id": terminal_id, "text": "x", "timeout_ms": 0}
    )

    assert "screen" not in result["data"]
    assert result["data"]["delivery"] == "automatic_terminal_activity"


@pytest.mark.asyncio
async def test_input_wait_that_times_out_keeps_automatic_delivery(
    terminal: Terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shorter than the fixture's quiet period, so the reply cannot settle in time.
    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_WAIT_MS", 1)
    terminal_id = await terminal.start()

    result = await terminal(
        {"action": "input", "terminal_id": terminal_id, "text": "x", "yield_time_ms": 60000}
    )

    data = result["data"]
    assert data["timed_out"] is True
    assert data["delivery"] == "automatic_terminal_activity"
    assert data["note"] == "A wait lasts at most 1 ms; wait again if the program is still busy."


@pytest.mark.asyncio
async def test_unknown_terminal_id_lists_attached_terminals(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    missing = await terminal({"action": "status", "terminal_id": "term_missing"})
    assert _error(missing)["message"] == (
        "No terminal has the id term_missing. You have no terminals; start one."
    )

    first = await terminal.start(name="build")
    second = await terminal.start()
    await terminal({"action": "kill", "terminal_id": first})
    listed = await terminal({"action": "wait", "terminal_id": "term_missing", "timeout_ms": 0})
    assert _error(listed)["message"] == (
        "No terminal has the id term_missing. Terminals attached to this Session: "
        f"{second} (ready: fake-tui); {first} (exited: build)."
    )

    other = make_context(tmp_path, session_id="session-b")
    unattached = await terminal({"action": "status", "terminal_id": "term_missing"}, other)
    assert _error(unattached)["message"] == (
        'No terminal has the id term_missing. No terminal is attached to this Session; {"action": '
        '"list"} shows every terminal, and attach makes one usable here.'
    )

    process = await terminal({"action": "status", "terminal_id": "proc_abc123"})
    assert _error(process)["message"] == (
        "proc_abc123 is a background command, not a terminal. Use the process Tool with this "
        "process_id."
    )


@pytest.mark.asyncio
async def test_missing_terminal_id_names_the_attached_terminals(terminal: Terminal) -> None:
    terminal_id = await terminal.start()

    result = await terminal({"action": "status", "name": "build"})

    assert _error(result)["message"] == (
        f"status needs terminal_id. Terminals attached to this Session: {terminal_id} "
        "(ready: fake-tui)."
    )


@pytest.mark.asyncio
async def test_unattached_terminal_names_the_attach_call(
    terminal: Terminal, tmp_path: Path
) -> None:
    terminal_id = await terminal.start()
    await terminal({"action": "detach", "terminal_id": terminal_id})

    detached = await terminal({"action": "status", "terminal_id": terminal_id})
    assert _error(detached)["code"] == "terminal_not_owned"
    assert _error(detached)["message"] == (
        f"{terminal_id} is not attached to this Session. Attach it first with "
        + json.dumps({"action": "attach", "terminal_id": terminal_id})
        + "."
    )

    other = make_context(tmp_path, session_id="session-b")
    await terminal({"action": "attach", "terminal_id": terminal_id}, other)
    taken = await terminal({"action": "status", "terminal_id": terminal_id})
    assert _error(taken)["message"] == (
        f"{terminal_id} is attached to another Session, so this Session cannot use it until "
        "that Session detaches it."
    )


@pytest.mark.asyncio
async def test_follow_up_results_show_the_screen_without_repeating_launch_facts(
    terminal: Terminal, manager: tuple[TerminalManager, AdapterFactory]
) -> None:
    started = await terminal({"action": "start", "command": "fake-tui", "args": ["-q"]})
    assert list(started["data"]) == [
        "terminal_id",
        "state",
        "command",
        "arguments",
        "workdir",
        "columns",
        "rows",
        "screen_revision",
        "screen",
        "delivery",
    ]
    terminal_id = started["data"]["terminal_id"]
    session = terminal.manager.get_session(terminal_id, OWNER)
    manager[1].adapters[0].emit("".join(f"line-{index}\r\n" for index in range(30)) + "ready> ")
    await eventually(lambda: session.renderer.revision > 0)

    waited = await terminal({"action": "wait", "terminal_id": terminal_id, "timeout_ms": 2000})
    assert list(waited["data"]) == [
        "terminal_id",
        "state",
        "screen_revision",
        "history",
        "screen",
        "scrollback",
        "timed_out",
    ]
    assert waited["data"]["history"].splitlines() == [f"line-{index}" for index in range(7)]
    assert "text" not in waited["data"]["scrollback"]
    text = render_tool_result_envelope(waited)
    assert "history:\n  line-0\n  line-1\n" in text
    assert "attention" not in text

    status = await terminal({"action": "status", "terminal_id": terminal_id})
    assert {"command", "arguments", "workdir", "columns", "rows"} <= set(status["data"])
    assert not {"attention", "attention_revision", "started_at"} & set(status["data"])
