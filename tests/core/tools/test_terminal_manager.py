"""Terminal manager: launch environment, start-up, input, and manual launch commands."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.tools._terminal_launch as terminal_launch
import core.tools.terminal_manager as terminal_module
from core.tools._terminal_launch import shell_launch
from core.tools.terminal_manager import (
    TerminalLaunchError,
    TerminalManager,
    TerminalStaleScreenError,
)
from tests.core.tools.terminal_manager_helpers import (
    TEST_ACTIVITY_QUIET_SECONDS,
    AdapterFactory,
    FakeClock,
    PendingTriggerService,
    eventually,
    owner,
    spawn,
    terminal_info,
)
from tests.core.tools.terminal_manager_helpers import clocked_manager as clocked_manager
from tests.core.tools.terminal_manager_helpers import terminal_manager as terminal_manager

Clocked = tuple[TerminalManager, AdapterFactory, PendingTriggerService, FakeClock]


@pytest.mark.asyncio
async def test_launch_runs_in_the_terminal_environment_and_keeps_explicit_env(
    terminal_manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VBOT_RUN_AGENT_ID", "server-only")
    manager, factory = terminal_manager

    await manager.spawn(owner(), ["fake-tui"], cwd=tmp_path, env=None, origin_run_id="run-a")
    await manager.spawn(
        owner(), ["fake-tui"], cwd=tmp_path, env={"TERM": "dumb"}, origin_run_id="run-a"
    )

    default, explicit = (call[2] for call in factory.calls)
    assert default["TERM"] == "xterm-256color"
    assert "VBOT_RUN_AGENT_ID" not in default
    assert explicit["TERM"] == "dumb"


@pytest.mark.asyncio
@pytest.mark.parametrize("resized", [False, True], ids=["output", "output-after-resize"])
async def test_startup_waits_for_a_visible_screen_and_answers_terminal_queries_meanwhile(
    clocked_manager: Clocked, tmp_path: Path, resized: bool
) -> None:
    """A terminal's own start-up sequences draw nothing, so they show no first
    screen; the program's first visible output does, also when it comes right
    after a resize, where it counts as a redraw (code review R6: start waited
    its whole wait). Starting types nothing."""
    manager, factory, _trigger, clock = clocked_manager
    started = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    waiting = asyncio.ensure_future(manager.wait_for_startup(started.terminal_id))

    # A cursor query draws nothing: the blank screen is no first screen.
    adapter.emit("\x1b[6n")
    await eventually(lambda: adapter.writes == ["\x1b[1;1R"])
    await clock.advance(TEST_ACTIVITY_QUIET_SECONDS)
    await eventually(lambda: terminal_info(manager, started.terminal_id).state == "ready")
    assert not waiting.done()

    if resized:
        await manager.resize_for_operator(started.terminal_id, columns=100, rows=30)
    revision = terminal_info(manager, started.terminal_id).screen_revision
    adapter.emit("READY> ")
    if resized:
        # A redraw keeps the screen revision.
        async def drawn() -> bool:
            return bool((await manager.snapshot(started.terminal_id, owner()))["screen"])

        await eventually(drawn)
    else:
        await eventually(lambda: terminal_info(manager, started.terminal_id).state == "working")
        assert terminal_info(manager, started.terminal_id).screen_revision > revision
    await clock.advance(TEST_ACTIVITY_QUIET_SECONDS)
    await waiting
    assert adapter.writes == ["\x1b[1;1R"]


@pytest.mark.asyncio
async def test_a_key_after_text_follows_at_the_latest_half_the_quiet_period_later(
    clocked_manager: Clocked, tmp_path: Path
) -> None:
    """A program that keeps printing (a spinner) never pauses for the echo:
    the key still follows half the quiet period after the text."""
    manager, factory, _trigger, clock = clocked_manager
    started = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]

    def revision() -> int:
        return terminal_info(manager, started.terminal_id).screen_revision

    sending = asyncio.ensure_future(
        manager.send_input(
            started.terminal_id,
            owner(),
            data=None,
            text="fix the tests",
            key="enter",
            expected_screen_revision=None,
            origin_run_id="run-a",
        )
    )
    await eventually(lambda: adapter.writes == ["fix the tests"])
    for index, frame in enumerate("|/-|/"):
        if index:
            await clock.advance(TEST_ACTIVITY_QUIET_SECONDS / 10)
        shown = revision()
        adapter.emit(f"\r{frame}")
        await eventually(lambda shown=shown: revision() > shown)
    assert adapter.writes == ["fix the tests"]

    # The last frame came 0.4 quiet periods after the text: only the limit is due.
    await clock.advance(TEST_ACTIVITY_QUIET_SECONDS * 0.11)
    await sending
    assert adapter.writes == ["fix the tests", "\r"]


@pytest.mark.asyncio
async def test_a_program_asking_for_focus_reports_is_told_its_user_is_away(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    """Programs that report focus notify only a user who is away. The terminal
    answers that it has no focus, and again after each submitted input."""
    manager, factory = terminal_manager
    started = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    adapter.emit("\x1b[?1004h")
    await eventually(lambda: adapter.writes == ["\x1b[O"])

    async def send(text: str, key: str | None = None) -> None:
        await manager.send_input(
            started.terminal_id,
            owner(),
            data=None,
            text=text,
            key=key,
            expected_screen_revision=None,
            origin_run_id="run-a",
        )

    await send("go", "enter")
    await send("draft")
    assert adapter.writes == ["\x1b[O", "go", "\r", "\x1b[O", "draft"]


@pytest.mark.asyncio
async def test_operator_input_makes_the_agent_observation_stale(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    started = await spawn(manager, tmp_path)

    await manager.send_operator_input(started.terminal_id, "human input")
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
async def test_empty_agent_input_changes_nothing(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    started = await spawn(manager, tmp_path)

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
    assert (after.state, after.screen_revision) == ("ready", started.screen_revision)
    assert factory.adapters[0].writes == []


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
