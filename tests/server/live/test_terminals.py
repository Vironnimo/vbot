"""Live Tools on coding Terminals: starting Codex and Claude Code with a task, typing,
keys, the Terminal layout, and the guards that keep text out of a shell or a menu."""

from __future__ import annotations

import asyncio
import base64
import queue
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import core.tools._terminal_session as terminal_session
import core.tools.terminal_backend as terminal_backend
import core.tools.terminal_manager as terminal_module
from core.tools.terminal_manager import TerminalManager, TerminalRenderHost
from server.live._context import LiveUiError
from server.live._terminals import TerminalTimings
from server.live._tools import LiveToolExecutor
from server.rpc.dispatcher import dispatch_method
from server.rpc.errors import RpcError
from server.rpc.methods import METHODS
from tests.server.live.tools_test_support import (
    CLAUDE_TRUST,
    CODEX_LOADING,
    CODEX_READY,
    CODEX_UPDATE,
    PROJECT_FOLDER,
    PROJECT_FOLDER_SHOWN,
    SHELL,
    STALE,
    Fixture,
    JsonObject,
)
from tests.server.rpc_test_support import StubAdapter, make_state


@pytest.fixture
def fx() -> Fixture:
    return Fixture()


# -- start_coding_terminal ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_starts_codex_and_types_the_task_once_it_is_ready(fx: Fixture) -> None:
    fx.app.screens["term_start1"] = [SHELL, CODEX_LOADING, CODEX_READY]
    text = await fx.ok("start_coding_terminal", program="codex", task='Fix "a" & 100%')
    assert f"in {PROJECT_FOLDER_SHOWN}: t1." in text
    assert "Typed the task into t1 and sent it." in text
    start = fx.app.params("terminal.start")
    # The task never becomes part of the command line.
    assert start == [{"command": "codex", "workdir": PROJECT_FOLDER, "group_id": "grp_new2"}]
    assert fx.app.params("terminal.group.create") == [{"name": "Codex"}]
    assert [(data, revision) for _id, data, revision in fx.app.inputs] == [
        ('\x1b[200~Fix "a" & 100%\x1b[201~', 5),
        ("\r", 6),
    ]
    assert fx.ui.of("terminal_view")[0] == {"op": "show", "terminal_id": "term_start1"}


@pytest.mark.asyncio
async def test_never_types_into_a_program_that_did_not_become_ready(fx: Fixture) -> None:
    fx.app.screens["term_start1"] = [SHELL]
    text = await fx.partial("start_coding_terminal", program="codex", task="Fix it")
    assert fx.app.inputs == []
    assert (
        "t1 did not show Codex's input line within 25 seconds, so the task was not typed." in text
    )
    assert 'send_message with {"target": "t1", "text": "<the task>"}' in text


@pytest.mark.asyncio
async def test_names_every_terminal_that_needs_the_same_answer(fx: Fixture) -> None:
    fx.app.screens["term_start1"] = [SHELL, CLAUDE_TRUST]
    fx.app.screens["term_start2"] = [SHELL, CLAUDE_TRUST]
    text = await fx.partial("start_coding_terminal", program="claude", task="Fix it", count=2)
    assert fx.app.inputs == []
    assert "Claude Code in t1 and t2 asks whether to trust the folder" in text
    assert text.endswith('"<the task>"}. Do the same for t2.')


@pytest.mark.asyncio
async def test_reports_a_pending_codex_update_question(fx: Fixture) -> None:
    fx.app.screens["term_start1"] = [SHELL, CODEX_LOADING, CODEX_UPDATE]
    text = await fx.partial("start_coding_terminal", program="codex", task="Fix it")
    assert fx.app.inputs == []
    assert "Codex in t1 offers an update and waits, so the task was not typed." in text


@pytest.mark.asyncio
async def test_starts_several_terminals_in_the_existing_program_group(fx: Fixture) -> None:
    fx.app.groups.append({"group_id": "grp_codex", "name": "codex", "kind": "user"})
    text = await fx.ok("start_coding_terminal", program="codex", task="Go", count=2, name="Pair")
    assert text.endswith("t1, t2. Typed the task into t1 and t2 and sent it.")
    assert [params["group_id"] for params in fx.app.params("terminal.start")] == ["grp_codex"] * 2
    assert all(params["name"] == "Pair" for params in fx.app.params("terminal.start"))
    assert fx.app.count("terminal.group.create") == 0
    assert len(fx.app.inputs) == 4


@pytest.mark.asyncio
async def test_retypes_after_a_stale_screen_rejection(fx: Fixture) -> None:
    fx.app.fail("terminal.input", STALE)
    await fx.ok("start_coding_terminal", program="codex", task="Go")
    assert [data for _id, data, _revision in fx.app.inputs] == ["\x1b[200~Go\x1b[201~", "\r"]
    assert fx.app.count("terminal.input") == 3


@pytest.mark.asyncio
async def test_resolves_the_folder_from_a_project_or_an_existing_path(
    fx: Fixture, tmp_path: Path
) -> None:
    fx.app.projects.append({"project_id": "site", "display_name": "Site", "cwd": "C:\\work\\site"})
    await fx.ok("start_coding_terminal", program="codex", folder="site")
    await fx.ok("start_coding_terminal", program="codex", folder=str(tmp_path))
    assert [params["workdir"] for params in fx.app.params("terminal.start")] == [
        "C:\\work\\site",
        str(tmp_path),
    ]
    code, message = await fx.failed(
        "start_coding_terminal", program="codex", folder=str(tmp_path / "missing")
    )
    assert code == "folder_not_found"
    assert "Projects: vBot, Site. Ask the user which Project or folder to use" in message


@pytest.mark.asyncio
async def test_asks_for_a_folder_when_no_project_is_selected(fx: Fixture) -> None:
    assert fx.context is not None
    fx.context["selected_project_id"] = None
    code, message = await fx.failed("start_coding_terminal", program="claude", task="Fix it")
    assert code == "folder_missing"
    assert message.startswith("No folder was given and no Project is selected in the app.")
    assert fx.app.effects() == []


@pytest.mark.asyncio
async def test_reports_started_terminals_when_a_later_start_fails(fx: Fixture) -> None:
    fx.app.fail("terminal.start", None, RpcError("invalid_request", "Too many Terminals."))
    code, message = await fx.failed("start_coding_terminal", program="codex", task="Go", count=3)
    assert code == "partial"
    assert message == (
        "Started Codex in t1; starting Terminal 2 of 3 failed: Too many Terminals. Nothing was "
        "retried and no task was typed."
    )
    assert fx.app.inputs == []


@pytest.mark.asyncio
async def test_reports_a_program_that_exits_before_it_is_ready(fx: Fixture) -> None:
    fx.app.screens["term_start1"] = [SHELL]

    def exit_on_read(params: JsonObject) -> JsonObject:
        terminal = fx.app._terminal(params["terminal_id"])
        terminal["state"] = "exited"
        return {"terminal": terminal, "screen": SHELL, "bracketed_paste": False}

    fx.app._terminal_read = exit_on_read  # type: ignore[method-assign]
    text = await fx.partial("start_coding_terminal", program="codex", task="Go")
    assert text.endswith("t1 ended before Codex was ready; the task was not typed.")


@pytest.mark.asyncio
async def test_reports_what_the_shell_shows_when_the_program_did_not_start(fx: Fixture) -> None:
    fx.app.screens["term_start1"] = [
        SHELL,
        SHELL + " codex\ncodex: The term 'codex' is not recognized.\nPS C:\\work\\vbot>",
    ]
    # A start without a task also waits until the program is ready.
    text = await fx.partial("start_coding_terminal", program="codex")
    assert text.endswith(
        "Codex did not start in t1. The Terminal shows: \"codex: The term 'codex' is not "
        'recognized." Tell the user.'
    )


# -- messages, reading and stopping ------------------------------------------------------


@pytest.mark.asyncio
async def test_types_a_message_into_a_coding_terminal(fx: Fixture) -> None:
    fx.app.add_terminal("term_a", name="Build")
    text = await fx.ok("send_message", target="Build", text="line one\nline two")
    assert text == "Sent to t1 (Codex)."
    assert [data for _id, data, _revision in fx.app.inputs] == [
        "\x1b[200~line one\nline two\x1b[201~",
        "\r",
    ]


@pytest.mark.asyncio
async def test_does_not_type_into_a_shell_a_menu_or_with_control_characters(fx: Fixture) -> None:
    fx.app.add_terminal("term_shell", "pwsh")
    fx.app.add_terminal("term_menu")
    fx.app.screens["term_menu"] = [CODEX_UPDATE]
    code, message = await fx.failed("send_message", target="term_shell", text="dir")
    assert code == "not_a_coding_terminal"
    assert message.startswith("t1 does not run Codex or Claude Code;")
    code, message = await fx.failed("send_message", target="term_menu", text="yes")
    assert code == "not_sent"
    assert "is asking a startup question, so nothing was sent" in message
    code, _message = await fx.failed("send_message", target="term_menu", text="a\x1b[201~b")
    assert code == "invalid_text"
    assert fx.app.inputs == []


@pytest.mark.asyncio
async def test_reads_a_coding_terminal_screen_as_quoted_text(fx: Fixture) -> None:
    fx.app.add_terminal("term_a", state="working")
    fx.app.screens["term_a"] = ["x" * 9000 + "\nlast line\n\n"]
    text = await fx.ok("read_output", target="term_a")
    assert text.startswith("t1 (Codex), working. Screen, last part, quoted:\n> ")
    assert text.endswith("> last line")


@pytest.mark.asyncio
async def test_stops_a_coding_terminal_with_its_interrupt_key(fx: Fixture) -> None:
    fx.app.add_terminal("term_a", state="working")
    text = await fx.ok("stop", target="term_a")
    assert text.startswith("Pressed Escape in t1 to interrupt it.")
    assert fx.app.inputs == [("term_a", "\x1b", 5)]


@pytest.mark.asyncio
async def test_other_calls_run_while_a_start_waits_but_never_write_into_it(fx: Fixture) -> None:
    fx.app.add_terminal("term_other")
    # The program stays loading until the other calls are done.
    fx.app.screens["term_start2"] = [SHELL]
    start = asyncio.create_task(fx.call("start_coding_terminal", program="codex", task="Go"))
    while fx.app.count("terminal.read") < 2:
        await asyncio.sleep(0)

    # The new Terminal got t1 when it started.
    assert "- t1 Codex" in await fx.ok("overview")
    await fx.ok("send_message", target="term_other", text="hello")
    code, message = await fx.failed("send_message", target="t1", text="other text")
    assert code == "terminal_busy"
    code, _message = await fx.failed("manage_terminals", action="key", target="t1", key="enter")
    assert code == "terminal_busy"
    assert not start.done()
    assert [terminal for terminal, _data, _revision in fx.app.inputs] == ["term_other"] * 2

    fx.app.screens["term_start2"] = [CODEX_READY]
    result = await start
    assert result["ok"] is True, result
    assert [data for terminal, data, _rev in fx.app.inputs if terminal == "term_start2"] == [
        "\x1b[200~Go\x1b[201~",
        "\r",
    ]
    # Once the task is sent, the Terminal takes messages again.
    await fx.ok("send_message", target="t1", text="more")


# -- terminal ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arranges_terminals_and_presses_keys(fx: Fixture) -> None:
    fx.app.add_terminal("term_a")
    assert await fx.ok("manage_terminals", action="maximize", target="term_a") == (
        "Maximized t1 in the Terminals view."
    )
    assert await fx.ok("manage_terminals", action="restore") == (
        "Restored the Terminals view to its group layout."
    )
    text = await fx.ok("manage_terminals", action="key", target="t1", key="enter")
    assert text == "Pressed Enter in t1. Call read_output with t1 to see the result."
    assert fx.app.inputs == [("term_a", "\r", 5)]
    code, message = await fx.failed("manage_terminals", action="key", target="t1")
    assert code == "missing_key"
    assert '{"action": "key", "target": "t1", "key": "enter"}' in message


@pytest.mark.asyncio
async def test_closes_a_terminal_by_stopping_then_removing_it(fx: Fixture) -> None:
    fx.app.add_terminal("term_a")
    assert await fx.ok("manage_terminals", action="close", target="term_a") == (
        "Closed t1: stopped and removed."
    )
    assert fx.app.effects() == ["terminal.kill", "terminal.forget"]
    assert fx.ui.of("terminal_view")[-1] == {"op": "refresh"}


@pytest.mark.asyncio
async def test_closes_working_terminals_only_after_the_user_agreed(fx: Fixture) -> None:
    fx.app.add_terminal("term_a", state="working")
    code, message = await fx.failed("manage_terminals", action="close", target="term_a")
    assert code == "terminal_working"
    assert message.endswith('{"action": "close", "target": "t1", "confirm": true}.')
    code, message = await fx.failed("manage_terminals", action="delete_group", target="Mine")
    assert code == "terminal_working"
    assert '{"action": "delete_group", "target": "Mine", "confirm": true}' in message
    assert fx.app.effects() == []

    await fx.ok("manage_terminals", action="close", target="t1", confirm=True)
    assert fx.app.effects() == ["terminal.kill", "terminal.forget"]


@pytest.mark.asyncio
async def test_close_reports_a_confirmed_stop_when_removal_fails(fx: Fixture) -> None:
    fx.app.add_terminal("term_a")
    fx.app.fail("terminal.forget", RpcError("invalid_request", "Busy."))
    code, message = await fx.failed("manage_terminals", action="close", target="term_a")
    assert code == "partial"
    assert message == (
        "t1 was stopped but may not have been removed. Busy. Call overview to check before "
        "closing it again."
    )


@pytest.mark.asyncio
async def test_manages_editable_groups_only(fx: Fixture) -> None:
    text = await fx.ok("manage_terminals", action="create_group", name="Review")
    assert text == 'Created the Terminal group "Review".'
    text = await fx.ok("manage_terminals", action="rename_group", target="mine", name="Ours")
    assert text == 'Renamed the group "Mine" to "Ours".'
    text = await fx.ok("manage_terminals", action="delete_group", target="Mine")
    assert text == 'Deleted the group "Mine" and stopped 2 Terminals.'
    code, message = await fx.failed("manage_terminals", action="delete_group", target="Finished")
    assert code == "group_not_editable"
    assert fx.app.count("terminal.group.delete") == 1


@pytest.mark.asyncio
async def test_reorders_a_group_by_refs_and_infers_the_group(fx: Fixture) -> None:
    fx.app.add_terminal("term_a")
    fx.app.add_terminal("term_b")
    await fx.ok("overview")
    text = await fx.ok("manage_terminals", action="reorder", order=["t2", "t1"])
    assert text == 'Reordered the group "Mine": t2, t1.'
    assert fx.app.params("terminal.group.order") == [
        {"group_id": "grp_mine", "order": ["term_b", "term_a"]}
    ]
    code, message = await fx.failed("manage_terminals", action="reorder", order=["t2"])
    assert code == "invalid_order"
    assert 'every Terminal of the group "Mine" exactly once: t1, t2.' in message


@pytest.mark.asyncio
async def test_keeps_a_completed_change_when_the_layout_refresh_fails(fx: Fixture) -> None:
    fx.app.add_terminal("term_a")
    fx.ui.error = LiveUiError("ui_unavailable")
    text = await fx.ok("manage_terminals", action="close", target="term_a")
    assert text == (
        "Closed t1: stopped and removed. The app window did not update its Terminals view."
    )


# -- through the real Terminal manager ---------------------------------------------------


# Each Terminal runs an emulated shell that starts emulated coding programs and draws
# screens like the observed ones, so typing, keys and the program guard take the
# production path: ``terminal.start`` / ``terminal.read`` / ``terminal.input`` ->
# ``TerminalManager`` -> the Terminal's screen renderer. The process guard asks the
# emulator whether the program still runs, as it asks the OS in production.

PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"
START_PROMPT = "PS C:\\work\\app> "
RULE = "─" * 60
CLAUDE_HINT = 'Try "refactor <filepath>"'


@dataclass
class Menu:
    """A question the program asks; ``selected`` is the highlighted answer."""

    question: str
    answers: list[str]
    selected: int = 0
    numbered: bool = True


class EmulatedTerminal:
    """A Terminal's shell and the coding program it started, drawn like the real ones.

    PowerShell runs the program its ``-EncodedCommand`` start option names. The
    screen is the shell's history plus the program's current screen; when the
    program exits, its last screen stays above the next prompt, as in a real
    Terminal.
    """

    _last_pid = 990_000

    def __init__(self, argv: Sequence[str] = ()) -> None:
        EmulatedTerminal._last_pid += 1
        self._pid = EmulatedTerminal._last_pid
        self._output: queue.Queue[str | None] = queue.Queue()
        self._alive = True
        self._line = ""
        self.history: list[str] = [START_PROMPT]
        self.writes: list[str] = []
        self.program: str | None = None
        self.input = ""
        self.submitted: list[str] = []
        self.menu: Menu | None = None
        self.menu_on_paste: Menu | None = None
        self.start_menu: Menu | None = None
        self._launch = _encoded_program(argv)
        self._draw()

    def launch(self) -> None:
        """Run the program the shell was started with, if any."""
        if self._launch in {"codex", "claude"}:
            self.history = []
            self._start_program(self._launch)

    # -- TerminalAdapter ----------------------------------------------------

    @property
    def pid(self) -> int:
        return self._pid

    def read(self, _size: int) -> str:
        try:
            value = self._output.get(timeout=0.05)
        except queue.Empty:
            raise TimeoutError from None
        if value is None:
            raise EOFError
        return value

    def write(self, text: str) -> None:
        self.writes.append(text)
        if self.program is None:
            self._shell(text)
        else:
            self._program(text)

    def resize(self, rows: int, columns: int) -> None:
        return None

    def is_alive(self) -> bool:
        return self._alive

    def exit_code(self) -> int | None:
        return None if self._alive else 0

    def terminate(self) -> None:
        if self._alive:
            self._alive = False
            self._output.put(None)

    def close(self) -> None:
        self.terminate()

    # -- emulation ----------------------------------------------------------

    def exit_program(self, prompt: str) -> None:
        """The program ends; the shell prints *prompt* below its last screen."""
        self.history += [*self._program_lines(), *prompt.split("\n")]
        self.program = None
        self.menu = None
        self._output.put("\x1b[?2004l")
        self._draw()

    def ask(self, menu: Menu) -> None:
        self.menu = menu
        self._draw()

    def _shell(self, text: str) -> None:
        if text != "\r":
            self._line += text
            self.history[-1] += text
            self._draw()
            return
        command, self._line = self._line.strip(), ""
        if command in {"codex", "claude"}:
            self._start_program(command)
            return
        self.history.append(START_PROMPT)
        self._draw()

    def _start_program(self, program: str) -> None:
        self.program = program
        self.menu, self.start_menu = self.start_menu, None
        self._output.put("\x1b[?2004h")
        self._draw()

    def _program(self, text: str) -> None:
        menu = self.menu
        if text.startswith(PASTE_START):
            if self.menu_on_paste is not None:
                self.menu, self.menu_on_paste = self.menu_on_paste, None
            else:
                self.input += text[len(PASTE_START) : -len(PASTE_END)]
        elif menu is not None and text in {"\x1b[A", "\x1b[B"}:
            step = 1 if text == "\x1b[B" else -1
            menu.selected = (menu.selected + step) % len(menu.answers)
        elif menu is not None and text == "\r":
            answer = menu.answers[menu.selected]
            self.submitted.append(f"answer: {answer}")
            self.menu = None
            if answer.startswith("No"):
                self.exit_program(START_PROMPT)
                return
        elif text == "\r":
            self.submitted.append(self.input)
            self.input = ""
        self._draw()

    def _program_lines(self) -> list[str]:
        if self.program is None:
            return []
        if self.menu is not None:
            menu = self.menu
            marker = "›" if self.program == "codex" else "❯"
            lines = ["", f" {menu.question}", ""]
            for index, answer in enumerate(menu.answers):
                label = f"{index + 1}. {answer}" if menu.numbered else answer
                lines.append(f" {marker} {label}" if index == menu.selected else f"   {label}")
            return [*lines, "", " Enter to confirm · Esc to cancel"]
        working = ["• Working (0s • esc to interrupt)", ""] if self.submitted else []
        if self.program == "codex":
            return [
                "╭──────────────────────────────╮",
                "│ >_ OpenAI Codex (v0.153.2)   │",
                "│ model:     gpt-5 high        │",
                "╰──────────────────────────────╯",
                "",
                *working,
                f"› {self.input or 'Ask Codex to do anything'}",
                "",
                "  gpt-5 high · Context 100% left",
            ]
        return [
            " Claude Code v2.1.280",
            "",
            *working,
            RULE,
            "❯\xa0" + (self.input or CLAUDE_HINT),
            RULE,
            "  ⏵⏵ auto mode on (shift+tab to cycle)",
        ]

    def _draw(self) -> None:
        lines = [*self.history, *self._program_lines()]
        self.drawn = [line.rstrip() for line in lines]
        self._output.put("\x1b[2J\x1b[H" + "\r\n".join(lines))


def _encoded_program(argv: Sequence[str]) -> str | None:
    """The program PowerShell's ``-EncodedCommand`` runs: the first word of its last line."""
    if "-EncodedCommand" not in argv:
        return None
    script = base64.b64decode(argv[list(argv).index("-EncodedCommand") + 1]).decode("utf-16-le")
    return script.splitlines()[-1].split()[0]


class EmulatedTerminals:
    """The adapter factory and process probe of the Terminal manager."""

    def __init__(self) -> None:
        self.all: list[EmulatedTerminal] = []
        # The question the next started program asks first.
        self.start_menu: Menu | None = None

    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        rows: int,
        columns: int,
        *,
        command_line: str | None = None,
    ) -> EmulatedTerminal:
        terminal = EmulatedTerminal(argv)
        terminal.start_menu, self.start_menu = self.start_menu, None
        terminal.launch()
        self.all.append(terminal)
        return terminal

    def runs(self, pid: int, program: str) -> bool:
        return any(item.pid == pid and item.program == program for item in self.all)


async def ui(action: str, args: JsonObject) -> JsonObject:
    """The owning app window, which applies every layout change."""
    return {"applied": True}


class Call:
    """One Live call's Tool executor on a real RPC dispatcher.

    The app window shows the Project ``app`` in *folder*; the Project list
    answers with it, everything else is the real dispatcher.
    """

    def __init__(self, state: Any, folder: Path, terminals: EmulatedTerminals) -> None:
        self.state = state
        self.terminals = terminals
        self.rpc_calls: list[tuple[str, JsonObject]] = []
        project = {"project_id": "app", "display_name": "App", "cwd": str(folder)}

        async def rpc(method: str, params: JsonObject) -> JsonObject:
            self.rpc_calls.append((method, dict(params)))
            if method == "project.list":
                return {"projects": [project]}
            return await dispatch_method(state, method, params, METHODS)

        self.executor = LiveToolExecutor(
            rpc=rpc,
            ui=ui,
            app_context=lambda: {
                "view": "chat",
                "selected_agent_id": "coder",
                "selected_project_id": "app",
                "chat_session": None,
            },
            is_active=lambda: True,
            started_at=datetime.now(UTC),
            end_call=lambda: None,
            timings=TerminalTimings(
                poll_seconds=0.01,
                ready_timeout_seconds=5.0,
                stable_seconds=0.05,
                echo_timeout_seconds=0.06,
            ),
        )

    async def run(self, tool: str, **arguments: Any) -> JsonObject:
        return await self.executor.execute(tool, arguments)

    async def ok(self, tool: str, **arguments: Any) -> str:
        result = await self.run(tool, **arguments)
        assert result["ok"] is True, result
        return str(result["data"]["content"])

    async def failed(self, tool: str, **arguments: Any) -> tuple[str, str]:
        result = await self.run(tool, **arguments)
        assert result["ok"] is False, result
        return str(result["error"]["code"]), str(result["error"]["message"])

    async def start(self, program: str, **arguments: Any) -> EmulatedTerminal:
        """Start *program* through Live and wait until the Terminal shows it."""
        await self.ok("start_coding_terminal", program=program, **arguments)
        terminal = self.terminals.all[-1]
        await _eventually(lambda: terminal.program == program)
        await self.settled(terminal)
        return terminal

    async def settled(self, terminal: EmulatedTerminal) -> None:
        """Wait until the Terminal's rendered screen is what the emulator drew last."""
        manager = self.state.runtime.terminal_manager
        terminal_id = next(
            item.terminal_id for item in manager.list_terminals() if item.pid == terminal.pid
        )
        deadline = time.monotonic() + 5
        while True:
            screen = (await manager.read_for_operator(terminal_id))["screen"]
            lines = [line.rstrip() for line in screen.splitlines()]
            while lines and not lines[-1]:
                lines.pop()
            if lines == terminal.drawn:
                return
            assert time.monotonic() < deadline, "timed out"
            await asyncio.sleep(0.01)


async def _eventually(predicate: Any) -> None:
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Call]:
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda env: ["pwsh.exe"])
    monkeypatch.setattr(terminal_session, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
    # Emulated Terminals have no OS processes to stop.
    monkeypatch.setattr(
        terminal_backend, "terminate_process_tree", lambda adapter, targets=None: None
    )
    terminals = EmulatedTerminals()
    manager = TerminalManager(
        adapter_factory=terminals,
        render_host=TerminalRenderHost.in_process(),
        sweep_interval_seconds=3600,
        data_dir=tmp_path / "terminals",
        program_probe=terminals.runs,
    )
    manager.start()
    state = make_state(tmp_path / "data", StubAdapter())
    state.runtime.terminal_manager = manager
    folder = tmp_path / "app"
    folder.mkdir()
    try:
        yield Call(state, folder, terminals)
    finally:
        await manager.aclose()


def _started(terminal: EmulatedTerminal) -> list[str]:
    """What reached the Terminal; the shell started the program from its start options."""
    return terminal.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("program", ["codex", "claude"])
async def test_start_types_the_task_and_sends_it_once_the_input_line_shows_it(
    call: Call, program: str
) -> None:
    text = await call.ok("start_coding_terminal", program=program, task='Fix "a" & 100%')
    terminal = call.terminals.all[0]
    assert terminal.submitted == ['Fix "a" & 100%']
    assert _started(terminal) == [f'{PASTE_START}Fix "a" & 100%{PASTE_END}', "\r"]
    assert "t1" in text
    # Every write named the program, so the manager could refuse it.
    inputs = [params for method, params in call.rpc_calls if method == "terminal.input"]
    assert {params["expected_program"] for params in inputs} == {program}


@pytest.mark.asyncio
async def test_a_message_reaches_the_program_and_is_sent(call: Call) -> None:
    terminal = await call.start("codex")
    await call.ok("send_message", target="t1", text="line one\nline two")
    assert terminal.submitted == ["line one\nline two"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("program", "prompt", "refusal"),
    [
        # A prompt the screen rules recognize ends the input line. Every observed prompt
        # style is a screen-rule case of tests/server/live/test_programs.py.
        pytest.param(
            "codex",
            "PS C:\\work\\app> ",
            "t1 does not show Codex's input line",
            id="codex-recognized-prompt",
        ),
        # Starship draws the same marker as Claude Code's input line.
        pytest.param(
            "claude",
            "~/app on  main\n❯ ",
            "t1 does not show Claude Code's input line",
            id="claude-starship-prompt",
        ),
        # A custom prompt no screen rule recognizes: the stale input line above it looks
        # typeable, and only the process guard knows the program ended.
        pytest.param(
            "codex",
            "work ",
            "Codex no longer runs in t1, so nothing was sent.",
            id="codex-custom-prompt",
        ),
        pytest.param(
            "claude",
            "work ",
            "Claude Code no longer runs in t1, so nothing was sent.",
            id="claude-custom-prompt",
        ),
    ],
)
async def test_after_the_program_exited_nothing_reaches_the_shell(
    call: Call, program: str, prompt: str, refusal: str
) -> None:
    terminal = await call.start(program)
    terminal.exit_program(prompt)
    await call.settled(terminal)
    before = len(terminal.writes)

    code, message = await call.failed("send_message", target="t1", text="Remove-Item -Recurse *")
    assert code == "not_sent"
    assert message.startswith(refusal)
    for tool, arguments in (
        ("manage_terminals", {"action": "key", "target": "t1", "key": "enter"}),
        ("manage_terminals", {"action": "key", "target": "t1", "key": "ctrl-c"}),
        ("stop", {"target": "t1"}),
    ):
        code, message = await call.failed(tool, **arguments)
        assert code == "program_not_running"
        assert "no longer runs in t1" in message
    assert terminal.writes[before:] == []


APPROVALS = {
    "codex": Menu(
        "Would you like to run the following command?",
        ["Yes, proceed (y)", "No, and tell Codex what to do differently (esc)"],
    ),
    "claude": Menu(
        "Do you want to make this edit to app.py?",
        ["Yes", "Yes, allow all edits during this session (shift+tab)", "No"],
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("program", ["codex", "claude"])
async def test_a_numbered_menu_is_never_typed_into(call: Call, program: str) -> None:
    terminal = await call.start(program)
    terminal.ask(APPROVALS[program])
    await call.settled(terminal)
    before = len(terminal.writes)
    code, message = await call.failed("send_message", target="t1", text="yes")
    assert code == "not_sent"
    assert "does not show" in message
    assert terminal.writes[before:] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("program", ["codex", "claude"])
async def test_enter_is_not_pressed_when_a_menu_replaced_the_typed_text(
    call: Call, program: str
) -> None:
    terminal = await call.start(program)
    terminal.menu_on_paste = APPROVALS[program]
    code, message = await call.failed("send_message", target="t1", text="Add tests")
    assert code == "not_sent"
    assert "input line does not show it, so it was not sent" in message
    assert _started(terminal) == [f"{PASTE_START}Add tests{PASTE_END}"]
    assert terminal.submitted == []


@pytest.mark.asyncio
async def test_a_trust_question_is_left_to_the_user_and_confirmed_only_on_its_answer(
    call: Call,
) -> None:
    call.terminals.start_menu = Menu(
        "Quick safety check: Is this a project you created or one you trust?",
        ["No, exit", "Yes, I trust this folder"],
        numbered=False,
    )
    # The Terminal runs, but the task is not typed: the call reports a partial result
    # that names the keys to press once the user agrees.
    code, text = await call.failed("start_coding_terminal", program="claude", task="Fix it")
    assert code == "partial"
    assert "Started Claude Code in a Terminal" in text
    assert (
        "Claude Code in t1 asks whether to trust the folder, so the task was not typed. Ask the "
        'user; if they agree, call manage_terminals with {"action": "key", "target": "t1", "key": '
        '"down"} then {"action": "key", "target": "t1", "key": "enter"}.'
    ) in text
    assert text.endswith(
        'Afterwards, call send_message with {"target": "t1", "text": "<the task>"}.'
    )
    terminal = call.terminals.all[0]
    assert _started(terminal) == []

    code, message = await call.failed("manage_terminals", action="key", target="t1", key="enter")
    assert code == "answer_not_selected"
    assert '"No, exit" selected, not "Yes, I trust this folder"' in message
    assert _started(terminal) == []

    await call.ok("manage_terminals", action="key", target="t1", key="down")
    await call.settled(terminal)
    await call.ok("manage_terminals", action="key", target="t1", key="enter")
    assert terminal.submitted == ["answer: Yes, I trust this folder"]
    await call.settled(terminal)
    await call.ok("send_message", target="t1", text="Fix it")
    assert terminal.submitted[-1] == "Fix it"


@pytest.mark.asyncio
async def test_an_update_is_skipped_but_never_installed_by_position(call: Call) -> None:
    call.terminals.start_menu = Menu(
        "✨ Update available! 0.153.2 -> 0.157.0",
        ["Update now (runs `npm install -g @openai/codex`)", "Skip", "Skip until next version"],
    )
    code, message = await call.failed("start_coding_terminal", program="codex")
    assert code == "partial"
    assert "Codex in t1 offers an update and waits. Ask the user;" in message
    terminal = call.terminals.all[-1]
    await call.settled(terminal)
    code, _message = await call.failed("manage_terminals", action="key", target="t1", key="enter")
    assert code == "answer_not_selected"
    await call.ok("manage_terminals", action="key", target="t1", key="down")
    await call.settled(terminal)
    await call.ok("manage_terminals", action="key", target="t1", key="enter")
    assert terminal.submitted == ["answer: Skip"]
