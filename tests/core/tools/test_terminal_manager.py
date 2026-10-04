"""Terminal manager: launch environment, start input, and manual launch commands."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.tools._bash_environment as bash_environment
import core.tools._terminal_launch as terminal_launch
import core.tools.terminal_manager as terminal_module
from core.tools._terminal_launch import shell_launch
from core.tools.terminal_manager import (
    TerminalLaunchError,
    TerminalManager,
    TerminalRenderHost,
    TerminalStaleScreenError,
)
from tests.core.tools.terminal_manager_helpers import (
    AdapterFactory,
    FakeClock,
    FakeTerminalAdapter,
    PendingTriggerService,
    eventually,
    owner,
    spawn,
    terminal_info,
)
from tests.core.tools.terminal_manager_helpers import clocked_manager as clocked_manager
from tests.core.tools.terminal_manager_helpers import quick_readiness as quick_readiness
from tests.core.tools.terminal_manager_helpers import shell_environment as shell_environment
from tests.core.tools.terminal_manager_helpers import terminal_manager as terminal_manager

Clocked = tuple[TerminalManager, AdapterFactory, PendingTriggerService, FakeClock]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inherited", "explicit", "expected"),
    [
        (None, None, "xterm-256color"),
        ("", None, "xterm-256color"),
        ("dumb", None, "xterm-256color"),
        ("screen-256color", None, "screen-256color"),
        ("screen-256color", {"TERM": "dumb"}, "dumb"),
    ],
    ids=["unset", "empty", "dumb", "real-terminal", "explicit-env"],
)
async def test_launch_corrects_an_inherited_dumb_term_and_keeps_explicit_env(
    terminal_manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inherited: str | None,
    explicit: dict[str, str] | None,
    expected: str,
) -> None:
    if inherited is None:
        monkeypatch.delenv("TERM", raising=False)
    else:
        monkeypatch.setenv("TERM", inherited)
    manager, factory = terminal_manager

    await manager.spawn(owner(), ["fake-tui"], cwd=tmp_path, env=explicit, origin_run_id="run-a")

    assert factory.calls[0][2]["TERM"] == expected
    assert os.environ.get("TERM") == inherited


@pytest.mark.asyncio
async def test_terminal_reprobes_missing_program_and_keeps_explicit_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, str]] = []
    probes = 0

    async def probe() -> dict[str, str]:
        nonlocal probes
        probes += 1
        return {"PATH": f"path-{probes}", "TERM": "dumb"}

    factory = AdapterFactory()

    def launch(
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        rows: int,
        columns: int,
        *,
        command_line: str | None = None,
    ) -> FakeTerminalAdapter:
        calls.append(dict(env))
        if len(calls) == 1:
            raise FileNotFoundError("test-owned absent executable")
        return factory(argv, cwd, env, rows, columns, command_line=command_line)

    monkeypatch.setattr(bash_environment, "_probe_shell_env", probe)
    manager = TerminalManager(adapter_factory=launch, render_host=TerminalRenderHost.in_process())
    try:
        await manager.spawn(
            owner(), ["new-program"], cwd=tmp_path, env={"EXPLICIT": "kept"}, origin_run_id="run"
        )
        assert [call["PATH"] for call in calls] == ["path-1", "path-2"]
        assert all(call["TERM"] == "xterm-256color" for call in calls)
        assert all(call["EXPLICIT"] == "kept" for call in calls)
    finally:
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("quick_readiness")
async def test_start_input_waits_for_a_settled_screen_and_answers_terminal_queries_meanwhile(
    clocked_manager: Clocked, tmp_path: Path
) -> None:
    manager, factory, _trigger, _clock = clocked_manager
    started = await spawn(manager, tmp_path, initial_text="agent task")
    assert started.state == "starting"

    # A cursor query draws nothing: the blank screen is no start screen yet.
    factory.adapters[0].emit("\x1b[6n")
    await eventually(lambda: factory.adapters[0].writes == ["\x1b[1;1R"])
    factory.adapters[0].emit("READY> ")
    await eventually(lambda: factory.adapters[0].writes == ["\x1b[1;1R", "agent task", "\r"])
    assert terminal_info(manager, started.terminal_id).state == "working"


@pytest.mark.asyncio
async def test_operator_input_supersedes_pending_agent_start_and_stale_observation(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    started = await spawn(manager, tmp_path, initial_text="agent task")
    # Only the pending start task shows that the Agent's first input was dropped:
    # it would otherwise be written once the start screen settles.
    initial_input = manager._sessions[started.terminal_id]._initial_input_task
    assert initial_input is not None

    await manager.send_operator_input(started.terminal_id, "human input")
    await eventually(initial_input.done)
    assert factory.adapters[0].writes == ["human input"]
    with pytest.raises(TerminalStaleScreenError):
        await manager.send_input(
            started.terminal_id,
            owner(),
            text=None,
            key="enter",
            expected_screen_revision=started.screen_revision,
            origin_run_id="run-a",
        )
    assert factory.adapters[0].writes == ["human input"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("quick_readiness")
async def test_empty_agent_input_preserves_startup_and_pending_input(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    started = await spawn(manager, tmp_path, initial_text="agent task")

    result = await manager.send_input(
        started.terminal_id,
        owner(),
        data="",
        text=None,
        key=None,
        expected_screen_revision=started.screen_revision,
        origin_run_id="run-a",
    )

    assert result["characters_sent"] == 0
    after = terminal_info(manager, started.terminal_id)
    assert (after.state, after.screen_revision) == ("starting", started.screen_revision)
    assert factory.adapters[0].writes == []
    # The pending first input still goes out once the start screen settles.
    factory.adapters[0].emit("READY> ")
    await eventually(lambda: factory.adapters[0].writes == ["agent task", "\r"])


@pytest.mark.asyncio
@pytest.mark.parametrize("shell", ["pwsh.exe", "cmd.exe"])
async def test_manual_command_starts_inside_the_default_shell_through_its_start_options(
    terminal_manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shell: str,
) -> None:
    """The shell receives the program through its own start options: PowerShell
    in its arguments, cmd in an exact command line. Nothing is typed."""
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda _env: [shell])
    manager, factory = terminal_manager
    arguments = ["--profile", "work space"]

    result = await manager.spawn_for_operator(command="codex", arguments=arguments, cwd=tmp_path)

    expected = shell_launch([shell], "codex", arguments, environment={})
    assert factory.calls[0][0] == expected.argv
    assert factory.command_lines == [expected.command_line]
    info = terminal_info(manager, result["terminal_id"])
    assert (info.owner, info.attachment) == (None, None)
    assert (info.command, info.arguments) == (shell, ())
    assert (info.launch_command, info.launch_arguments) == ("codex", tuple(arguments))
    assert (result["command"], result["arguments"]) == (shell, [])
    assert (result["launch_command"], result["launch_args"]) == ("codex", arguments)
    history = manager.list_operator_launch_history()
    assert [(entry["command"], entry["args"]) for entry in history] == [("codex", arguments)]
    # Early operator input reaches the shell as typed, with nothing queued before it.
    await manager.send_operator_input(result["terminal_id"], "x")
    assert factory.adapters[0].writes == ["x"]


@pytest.mark.asyncio
async def test_manual_command_launch_files_are_removed_when_the_terminal_ends_or_fails_to_start(
    terminal_manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """zsh reads the launch from private start files named by ``ZDOTDIR``."""
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda _env: ["/bin/zsh"])
    monkeypatch.setattr(terminal_launch, "os", SimpleNamespace(name="posix"))
    manager, factory = terminal_manager

    result = await manager.spawn_for_operator(command="codex", arguments=[], cwd=tmp_path)

    argv, _cwd, environment, _rows, _columns = factory.calls[0]
    started_files = Path(environment["ZDOTDIR"])
    assert argv == ["/bin/zsh"]
    assert (started_files / ".zshrc").is_file()
    factory.adapters[0].finish(0)
    await eventually(lambda: not started_files.exists())
    assert terminal_info(manager, result["terminal_id"]).state == "exited"

    factory.error = OSError("no PTY")
    with pytest.raises(TerminalLaunchError):
        await manager.spawn_for_operator(command="codex", arguments=[], cwd=tmp_path)
    failed_files = Path(factory.calls[1][2]["ZDOTDIR"])
    assert failed_files != started_files
    assert not failed_files.exists()
