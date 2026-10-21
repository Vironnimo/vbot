"""A restricted Session's terminal: what start, input at a shell prompt, attach and
status check against its Permission Rules (``_terminal_permissions``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

import core.tools.terminal_manager as manager_module
from core.permissions import PermissionBoundary
from core.tools._terminal_permissions import permission_scope
from core.tools._terminal_shell_state import ShellState
from core.tools.terminal_manager import TerminalManager, TerminalOwner
from core.tools.tools import JsonObject
from tests.core.permissions.command_check_support import (
    FakeAnalyzer,
    fake_checker,
    plan,
    restricted,
)
from tests.core.tools.terminal_helpers import call, make_context, manager
from tests.core.tools.terminal_manager_helpers import AdapterFactory, eventually

__all__ = ["manager"]

_OWNER = TerminalOwner("project-a", "agent-a", "session-a")


def _prompt(folder: Path) -> str:
    """What vBot's PowerShell integration prints before each prompt."""
    return f'\x1b]9;9;"{folder}"\x07\x1b]133;A\x07PS> '


class _Terminal:
    """Calls of one restricted Session against a fake PTY terminal manager."""

    def __init__(
        self,
        manager: tuple[TerminalManager, AdapterFactory],
        tmp_path: Path,
        analyzer: FakeAnalyzer,
        permissions: PermissionBoundary,
    ) -> None:
        self.manager, self.factory = manager
        self.tmp_path = tmp_path
        self.analyzer = analyzer
        self.checker = fake_checker(analyzer)
        self.permissions = permissions

    async def __call__(
        self, arguments: JsonObject, *, session: str = "session-a", restricted: bool = True
    ) -> dict[str, Any]:
        context = make_context(
            self.tmp_path,
            session_id=session,
            permissions=self.permissions if restricted else None,
        )
        result = await call(self.manager, context, arguments, command_checker=self.checker)
        return cast(dict[str, Any], result)


@pytest.fixture
def work(tmp_path: Path) -> Path:
    folder = tmp_path / "work"
    folder.mkdir()
    return folder


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "rules", "fragment"),
    [
        pytest.param(
            {"command": "git", "args": ["push"]},
            {"deny": ["Shell(git push *)"]},
            'refuse running "git push", because the rule Shell(git push *) denies it',
            id="denied-program",
        ),
        pytest.param(
            {"workdir": "OUTSIDE"},
            {},
            "refuse working in",
            id="workdir-outside",
        ),
        pytest.param(
            {"command": "/bin/sh"},
            {},
            "vBot cannot check the lines typed into this shell. Start the terminal without command",
            id="shell-without-integration",
        ),
    ],
)
async def test_a_restricted_start_launches_nothing_the_rules_refuse(
    manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    work: Path,
    arguments: JsonObject,
    rules: dict[str, object],
    fragment: str,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    if arguments.get("workdir") == "OUTSIDE":
        arguments = {**arguments, "workdir": str(outside)}
    terminal = _Terminal(manager, work, FakeAnalyzer(plan("git push")), restricted(work, **rules))

    refused = await terminal({"action": "start", **arguments})

    assert refused["error"]["code"] == "permission_denied"
    assert fragment in refused["error"]["message"]
    assert terminal.factory.calls == []


@pytest.mark.asyncio
async def test_a_restricted_shell_starts_confined_and_its_prompt_lines_are_checked(
    manager: tuple[TerminalManager, AdapterFactory],
    work: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(manager_module, "default_terminal_argv", lambda env: ["pwsh.exe"])
    monkeypatch.setenv("VBOT_DATA_DIR", str(work.parent / "data"))
    terminal_manager, factory = manager
    factory.initial_output = _prompt(work)
    analyzer = FakeAnalyzer(plan("Remove-Item secret.txt"))
    boundary = restricted(work, deny=["Shell(Remove-Item *)"])
    terminal = _Terminal(manager, work, analyzer, boundary)

    started = await terminal({"action": "start"})
    terminal_id = started["data"]["terminal_id"]
    adapter = factory.adapters[0]

    async def send(**arguments: object) -> dict[str, Any]:
        return await terminal({"action": "input", "terminal_id": terminal_id, **arguments})

    # The shell itself runs nothing, so only its folder is checked; it keeps the
    # Session's rules and starts without vBot's control variables.
    assert analyzer.argvs == []
    assert "VBOT_DATA_DIR" not in factory.calls[0][2]
    assert terminal_manager.terminal(terminal_id, _OWNER).permission_scope == permission_scope(
        boundary
    )
    # Typing is not submitting; Enter checks the whole line in the reported folder.
    assert (await send(text="Remove-Item secret"))["ok"] is True
    assert analyzer.lines == []
    refused = await send(key="enter")
    assert refused["error"]["code"] == "permission_denied"
    assert analyzer.lines[-1][:3] == ("Remove-Item secret", "powershell", work)
    # Keys whose effect on the line vBot cannot follow are refused at the prompt.
    completion = await send(key="tab")
    assert completion["error"]["message"].startswith(
        "terminal was not run: the permission rules check each command line typed at this "
        'shell prompt, and key "tab" would change the line'
    )
    assert adapter.writes == ["Remove-Item secret"]

    analyzer.result = plan("Get-ChildItem")
    assert (await send(key="ctrl_c"))["ok"] is True
    assert (await send(text="Get-ChildItem", key="enter"))["ok"] is True
    assert analyzer.lines[-1][0] == "Get-ChildItem"
    # A line that starts another shell would hand the next lines to an unchecked prompt.
    analyzer.result = plan("pwsh")
    nested = await send(text="pwsh\n")
    assert "a prompt vBot cannot check" in nested["error"]["message"]

    # A running program reads its input unchecked.
    adapter.emit("\x1b]133;C\x07")
    await eventually(lambda: not _shell(terminal_manager, terminal_id).at_prompt)
    checked = len(analyzer.lines)
    assert (await send(text="y\n"))["ok"] is True
    assert len(analyzer.lines) == checked


@pytest.mark.asyncio
async def test_a_restricted_session_attaches_and_reads_only_terminals_under_its_rules(
    manager: tuple[TerminalManager, AdapterFactory],
    work: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(manager_module, "default_terminal_argv", lambda env: ["host-shell"])
    terminal_manager, factory = manager
    factory.initial_output = "secret output"
    terminal = _Terminal(manager, work, FakeAnalyzer(plan("host-shell")), restricted(work))
    operator = await terminal_manager.spawn_for_operator(command=None, arguments=[], cwd=work)
    operator_id = operator["terminal_id"]
    own = (await terminal({"action": "start"}, session="session-b"))["data"]["terminal_id"]
    await terminal({"action": "detach", "terminal_id": own}, session="session-b")

    refused = await terminal({"action": "attach", "terminal_id": operator_id})
    status = await terminal({"action": "status", "terminal_id": operator_id})

    assert refused["error"] == {
        "code": "permission_denied",
        "retryable": False,
        "message": f"terminal was not run: the permission rules refuse attaching {operator_id}, "
        "because it was started outside your permission rules (by the user, or by a Session "
        'with other rules). To work in a terminal, start one with terminal {"action": "start"}.',
    }
    assert "screen" not in status["data"]
    assert status["data"]["note"].startswith("Its screen and history are not shown")
    # A terminal another Session started under the same rules, and every terminal
    # for a Session with full access, can be attached.
    assert (await terminal({"action": "attach", "terminal_id": own}))["ok"] is True
    unrestricted = await terminal(
        {"action": "status", "terminal_id": operator_id}, restricted=False
    )
    assert "secret output" in unrestricted["data"]["screen"]


def _shell(terminal_manager: TerminalManager, terminal_id: str) -> ShellState:
    state = terminal_manager.shell_state(terminal_id, _OWNER)
    assert state is not None
    return state


@pytest.mark.parametrize(
    ("chunks", "cwd"),
    [
        pytest.param(['\x1b]9;9;"C:\\work"\x07', "\x1b]133;A\x07"], "C:\\work", id="osc-9-9"),
        pytest.param(
            ["\x1b]633;P;Cwd=/srv/a\\x3bb\\\\c\x07\x1b]133;A\x07"], "/srv/a;b\\c", id="633"
        ),
        pytest.param(
            ["\x1b]7;file://localhost/srv/a%20b\x1b\\", "\x1b]133;A\x07"], "/srv/a b", id="osc-7"
        ),
        pytest.param(
            ["\x1b]7;file://elsewhere.example/srv\x07\x1b]133;A\x07"], None, id="other-host"
        ),
        pytest.param(
            ["out\x1b]633;P;Cw", "d=/srv\x07\x1b", "]133;A\x07"], "/srv", id="split-reads"
        ),
    ],
)
def test_the_shell_state_reads_the_prompt_and_folder_its_shell_reports(
    chunks: list[str], cwd: str | None
) -> None:
    state = ShellState("start")

    for chunk in chunks:
        state.feed(chunk)

    assert (state.at_prompt, state.prompt_count, state.cwd) == (True, 1, cwd)
    state.feed("\x1b]133;C\x07")
    assert state.at_prompt is False
