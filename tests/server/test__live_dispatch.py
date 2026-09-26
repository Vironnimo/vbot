"""Live Tools through the canonical RPC handlers and a real Terminal manager.

Each Terminal runs an emulated shell that starts emulated coding programs and
draws screens like the observed ones, so typing, keys and the program guard take
the production path: ``terminal.start`` / ``terminal.read`` / ``terminal.input``
-> ``TerminalManager`` -> the Terminal's screen renderer. The process guard asks
the emulator whether the program still runs, as it asks the OS in production.
"""

from __future__ import annotations

import asyncio
import queue
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import core.tools._terminal_input as terminal_input
import core.tools.terminal_backend as terminal_backend
import core.tools.terminal_manager as terminal_module
from core.tools.terminal_manager import TerminalManager
from server._live_terminals import TerminalTimings
from server._live_tools import LiveToolExecutor
from server.rpc.dispatcher import dispatch_method
from server.rpc.methods import METHODS
from tests.server.rpc_test_support import StubAdapter, make_state

JsonObject = dict[str, Any]

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

    The screen is the shell's history plus the program's current screen; when the
    program exits, its last screen stays above the next prompt, as in a real
    Terminal.
    """

    _last_pid = 990_000

    def __init__(self) -> None:
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
        self._draw()

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
            self.program = command
            self.menu, self.start_menu = self.start_menu, None
            self._output.put("\x1b[?2004h")
        else:
            self.history.append(START_PROMPT)
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


class EmulatedTerminals:
    """The adapter factory and process probe of the Terminal manager."""

    def __init__(self) -> None:
        self.all: list[EmulatedTerminal] = []
        # The question the next started program asks first.
        self.start_menu: Menu | None = None

    def __call__(
        self, argv: Sequence[str], cwd: Path, env: Mapping[str, str], rows: int, columns: int
    ) -> EmulatedTerminal:
        terminal = EmulatedTerminal()
        terminal.start_menu, self.start_menu = self.start_menu, None
        self.all.append(terminal)
        return terminal

    def runs(self, pid: int, program: str) -> bool:
        return any(item.pid == pid and item.program == program for item in self.all)


class Ui:
    """The owning app window: its selection and Terminal layout."""

    def __init__(self, folder: Path) -> None:
        self.context: JsonObject = {
            "view": "chat",
            "selected_agent_id": "coder",
            "selected_project_id": "app",
            "agents": [{"agent_id": "coder", "name": "Coder"}],
            "projects": [{"project_id": "app", "name": "App", "cwd": str(folder)}],
            "selected_project_team": [],
        }

    async def __call__(self, action: str, args: JsonObject) -> JsonObject:
        return self.context if action == "context" else {"applied": True}


class Call:
    """One Live call's Tool executor on a real RPC dispatcher."""

    def __init__(self, state: Any, ui: Ui, terminals: EmulatedTerminals) -> None:
        self.state = state
        self.terminals = terminals
        self.rpc_calls: list[tuple[str, JsonObject]] = []

        async def rpc(method: str, params: JsonObject) -> JsonObject:
            self.rpc_calls.append((method, dict(params)))
            return await dispatch_method(state, method, params, METHODS)

        self.executor = LiveToolExecutor(
            rpc=rpc,
            ui=ui,
            is_active=lambda: True,
            started_at=datetime.now(UTC),
            timings=TerminalTimings(
                poll_seconds=0.01,
                ready_timeout_seconds=5.0,
                stable_seconds=0.05,
                enter_delay_seconds=0.02,
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
            key for key, session in manager._sessions.items() if session.adapter is terminal
        )

        def rendered() -> bool:
            screen = manager.read_for_operator(terminal_id)["screen"]
            lines = [line.rstrip() for line in screen.splitlines()]
            while lines and not lines[-1]:
                lines.pop()
            return lines == terminal.drawn

        await _eventually(rendered)


async def _eventually(predicate: Any) -> None:
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Call]:
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda env: ["pwsh.exe"])
    monkeypatch.setattr(terminal_input, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
    # Emulated Terminals have no OS processes to stop.
    monkeypatch.setattr(
        terminal_backend, "terminate_process_tree", lambda adapter, targets=None: None
    )
    terminals = EmulatedTerminals()
    manager = TerminalManager(
        adapter_factory=terminals,
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
        yield Call(state, Ui(folder), terminals)
    finally:
        await manager.aclose()


def _started(terminal: EmulatedTerminal) -> list[str]:
    """What reached the Terminal after the shell started the program."""
    return terminal.writes[terminal.writes.index("\r") + 1 :]


# -- typing a task and a message -----------------------------------------------


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


# -- the program ended: never type into its shell ------------------------------------

PROMPTS_AFTER_EXIT = {
    "powershell": "PS C:\\work\\app> ",
    "cmd": "C:\\work\\app>",
    "bash": "dev@box:~/app$ ",
    "zsh": "dev@box ~/app % ",
    "fish": "dev@box ~/app> ",
    "starship": "~/app on  main\n❯ ",
    "oh-my-posh on PowerShell": "  C:\\work\\app   main ❯ ",
    # A custom prompt no screen rule recognizes: only the process guard knows.
    "custom": "work ",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("program", ["codex", "claude"])
@pytest.mark.parametrize("prompt", PROMPTS_AFTER_EXIT.values(), ids=PROMPTS_AFTER_EXIT.keys())
async def test_after_the_program_exited_nothing_reaches_the_shell(
    call: Call, program: str, prompt: str
) -> None:
    terminal = await call.start(program)
    terminal.exit_program(prompt)
    await call.settled(terminal)
    before = len(terminal.writes)

    code, message = await call.failed("send_message", target="t1", text="Remove-Item -Recurse *")
    assert code == "not_sent"
    assert "nothing was sent" in message
    for tool, arguments in (
        ("terminal", {"action": "key", "target": "t1", "key": "enter"}),
        ("terminal", {"action": "key", "target": "t1", "key": "ctrl-c"}),
        ("stop", {"target": "t1"}),
    ):
        code, message = await call.failed(tool, **arguments)
        assert code == "program_not_running"
        assert "no longer runs in t1" in message
    assert terminal.writes[before:] == []


@pytest.mark.asyncio
async def test_the_process_guard_stops_what_the_screen_cannot_tell(call: Call) -> None:
    terminal = await call.start("claude")
    terminal.exit_program(PROMPTS_AFTER_EXIT["custom"])
    await call.settled(terminal)
    before = len(terminal.writes)
    # The stale input line above the unrecognized prompt looks typeable.
    code, message = await call.failed("send_message", target="t1", text="dir")
    assert code == "not_sent"
    assert message.startswith("Claude Code no longer runs in t1, so nothing was sent.")
    assert terminal.writes[before:] == []


# -- menus and questions -------------------------------------------------------------

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
async def test_a_trust_question_is_confirmed_only_on_the_answer_the_guidance_names(
    call: Call,
) -> None:
    call.terminals.start_menu = Menu(
        "Quick safety check: Is this a project you created or one you trust?",
        ["No, exit", "Yes, I trust this folder"],
        numbered=False,
    )
    # The Terminal runs, but the task is not typed: the call reports a partial result.
    code, text = await call.failed("start_coding_terminal", program="claude", task="Fix it")
    assert code == "partial"
    assert "Started Claude Code in a Terminal" in text
    assert '"key": "down"} then {"action": "key", "target": "t1", "key": "enter"}' in text
    terminal = call.terminals.all[0]
    before = len(terminal.writes)

    code, message = await call.failed("terminal", action="key", target="t1", key="enter")
    assert code == "answer_not_selected"
    assert '"No, exit" selected, not "Yes, I trust this folder"' in message
    assert terminal.writes[before:] == []

    await call.ok("terminal", action="key", target="t1", key="down")
    await call.settled(terminal)
    await call.ok("terminal", action="key", target="t1", key="enter")
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
    terminal = await call.start("codex")
    code, _message = await call.failed("terminal", action="key", target="t1", key="enter")
    assert code == "answer_not_selected"
    await call.ok("terminal", action="key", target="t1", key="down")
    await call.settled(terminal)
    await call.ok("terminal", action="key", target="t1", key="enter")
    assert terminal.submitted == ["answer: Skip"]
