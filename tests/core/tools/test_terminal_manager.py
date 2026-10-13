"""Terminal manager: launch environment, start-up, input, and manual launch commands."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.tools._terminal_launch as terminal_launch
import core.tools.terminal_manager as terminal_module
from core.storage.temp_files import TemporaryFileManager
from core.tools._terminal_launch import shell_launch
from core.tools.terminal_manager import (
    TerminalLaunchError,
    TerminalManager,
    TerminalProgramNotRunningError,
    TerminalRenderHost,
    TerminalStaleScreenError,
)
from tests.core.tools.terminal_manager_helpers import (
    TEST_ACTIVITY_QUIET_SECONDS,
    TEST_IDLE_SECONDS,
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


@pytest.mark.parametrize(
    ("key", "before", "during", "expected"),
    [
        ("enter", "", "", "\r"),
        ("up", "\x1b[?1l", "\x1b[?1h", "\x1bOA"),
        ("up", "\x1b[?1h", "\x1b[?1l", "\x1b[A"),
    ],
    ids=["enter", "enter-application-cursor-mode", "leave-application-cursor-mode"],
)
@pytest.mark.asyncio
async def test_a_key_after_text_follows_at_the_latest_half_the_quiet_period_later(
    clocked_manager: Clocked,
    tmp_path: Path,
    key: str,
    before: str,
    during: str,
    expected: str,
) -> None:
    """A program that keeps printing (a spinner) never pauses for the echo:
    the key still follows half the quiet period after the text."""
    manager, factory, _trigger, clock = clocked_manager
    started = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]

    def revision() -> int:
        return terminal_info(manager, started.terminal_id).screen_revision

    if before:
        shown = revision()
        adapter.emit(before)
        await eventually(lambda: revision() > shown)

    sending = asyncio.ensure_future(
        manager.send_input(
            started.terminal_id,
            owner(),
            data=None,
            text="fix the tests",
            key=key,
            expected_screen_revision=None,
            origin_run_id="run-a",
        )
    )
    await eventually(lambda: adapter.writes == ["fix the tests"])
    for index, frame in enumerate("|/-|/"):
        if index:
            await clock.advance(TEST_ACTIVITY_QUIET_SECONDS / 10)
        shown = revision()
        # The program may change cursor-key mode in response to the text,
        # before the following key is typed. Advance only the fake clock.
        adapter.emit((during if index == 0 else "") + f"\r{frame}")
        await eventually(lambda shown=shown: revision() > shown)
    assert adapter.writes == ["fix the tests"]

    # The last frame came 0.4 quiet periods after the text: only the limit is due.
    await clock.advance(TEST_ACTIVITY_QUIET_SECONDS * 0.11)
    sent = await sending
    assert adapter.writes == ["fix the tests", expected]
    assert sent["characters_sent"] == len("fix the tests") + len(expected)


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
@pytest.mark.parametrize("route", ["agent-data", "agent-text-key", "operator"])
async def test_output_and_queries_keep_flowing_while_input_waits_for_the_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    clock = FakeClock()
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        render_host=TerminalRenderHost.in_process(),
        temporary_files=TemporaryFileManager(tmp_path / "temp"),
        activity_quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        idle_seconds=TEST_IDLE_SECONDS,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    manager.start()
    release = threading.Event()
    sending = None
    reply = None
    try:
        started = await spawn(manager, tmp_path)
        adapter = factory.adapters[0]
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        write = adapter.write
        writing: list[str] = []

        def blocked_write(text: str) -> None:
            writing.append(text)
            if text == "large input":
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(5), "test did not release the blocked input"
            write(text)

        monkeypatch.setattr(adapter, "write", blocked_write)
        sending = asyncio.create_task(
            manager.send_operator_input(started.terminal_id, "large input")
            if route == "operator"
            else manager.send_input(
                started.terminal_id,
                owner(),
                data="large input" if route == "agent-data" else None,
                text="large input" if route == "agent-text-key" else None,
                key="enter" if route == "agent-text-key" else None,
                expected_screen_revision=None,
                origin_run_id="run-a",
            )
        )
        async with asyncio.timeout(2):
            await entered.wait()
            # A query must queue its answer without blocking later reads, even
            # when the program cannot accept more input until it has printed.
            adapter.emit("\x1b[6nFIRST\r\n")
            adapter.emit("SECOND\x07")

            async def drawn() -> bool:
                snapshot = await manager.snapshot(started.terminal_id, owner())
                return bool(snapshot["screen"] == "FIRST\nSECOND")

            await eventually(drawn)
        snapshot = await manager.snapshot(started.terminal_id, owner())
        assert snapshot["log_complete"] is True
        assert Path(snapshot["log_file"]).read_bytes() == b"\x1b[6nFIRST\r\nSECOND\x07"
        assert writing == ["large input"]
        assert adapter.writes == []

        reply = asyncio.create_task(
            manager.wait_for_reply(started.terminal_id, owner(), seconds=100, after_quiet=0)
        )
        await eventually(lambda: clock.sleeping)
        await clock.advance(TEST_IDLE_SECONDS + TEST_ACTIVITY_QUIET_SECONDS)
        # Give the due timer and reply waiter their loop turns. Time alone,
        # even a declared end in the output, cannot finish pending input.
        for _ in range(10):
            await asyncio.sleep(0)
        assert terminal_info(manager, started.terminal_id).state == "working"
        assert not reply.done()
        assert trigger.submissions == []

        release.set()
        async with asyncio.timeout(2):
            await eventually(lambda: len(adapter.writes) >= 2)
            if route == "agent-text-key":
                # Echo pacing starts after the physical text write completes.
                for _ in range(10):
                    await asyncio.sleep(0)
                await clock.advance(TEST_ACTIVITY_QUIET_SECONDS / 2)
            await sending
            await eventually(lambda: len(adapter.writes) == (3 if route == "agent-text-key" else 2))
        assert adapter.writes == ["large input", "\x1b[1;1R"] + (
            ["\r"] if route == "agent-text-key" else []
        )
        # Output advanced the activity generation while writing blocked.
        # Completion rearms that current generation, without further output.
        await eventually(lambda: clock.sleeping)
        await clock.advance(TEST_ACTIVITY_QUIET_SECONDS)
        async with asyncio.timeout(2):
            assert await reply == "quiet"
            await eventually(lambda: terminal_info(manager, started.terminal_id).state == "ready")
    finally:
        release.set()
        for task in (sending, reply):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "following", ["agent", "operator", "agent-stale", "operator-stale", "operator-program-ended"]
)
async def test_cancelled_input_keeps_its_physical_write_before_later_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    following: str,
) -> None:
    factory = AdapterFactory()
    clock = FakeClock()
    program_running = True
    probes: list[tuple[int, str]] = []

    def probe(pid: int, program: str) -> bool:
        probes.append((pid, program))
        return program_running

    manager = TerminalManager(
        adapter_factory=factory,
        render_host=TerminalRenderHost.in_process(),
        program_probe=probe,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    manager.start()
    started = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    release = threading.Event()
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    write = adapter.write
    writing: list[str] = []

    def blocked_write(text: str) -> None:
        writing.append(text)
        if text == "first":
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "test did not release the cancelled input"
        write(text)

    monkeypatch.setattr(adapter, "write", blocked_write)
    first = asyncio.create_task(
        manager.send_input(
            started.terminal_id,
            owner(),
            text="first",
            key="enter",
            expected_screen_revision=None,
            origin_run_id="run-a",
        )
    )
    second = None
    try:
        async with asyncio.timeout(2):
            await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        revision = terminal_info(manager, started.terminal_id).screen_revision
        attempted = asyncio.Event()

        async def next_input() -> None:
            attempted.set()
            expected = revision if following.endswith("-stale") else None
            if following.startswith("operator"):
                await manager.send_operator_input(
                    started.terminal_id,
                    "second",
                    expected_screen_revision=expected,
                    expected_program="fake-tui" if following.endswith("-ended") else None,
                )
            else:
                await manager.send_input(
                    started.terminal_id,
                    owner(),
                    data="second",
                    text=None,
                    key=None,
                    expected_screen_revision=expected,
                    origin_run_id="run-b",
                )

        second = asyncio.create_task(next_input())
        await attempted.wait()
        for _ in range(10):
            await asyncio.sleep(0)
        assert writing == ["first"]
        assert not second.done()
        assert terminal_info(manager, started.terminal_id).screen_revision == revision
        assert probes == []
        if "-" in following:
            # These guards must observe what changed while earlier input was
            # still in flight, not approve against the state before waiting.
            adapter.emit("changed")
            await eventually(
                lambda: terminal_info(manager, started.terminal_id).screen_revision > revision
            )
            program_running = False
        release.set()
        async with asyncio.timeout(2):
            if following.endswith("-stale"):
                with pytest.raises(TerminalStaleScreenError):
                    await second
            elif following.endswith("-ended"):
                with pytest.raises(TerminalProgramNotRunningError):
                    await second
                assert probes == [(adapter.pid, "fake-tui")]
            else:
                await second
        assert writing == adapter.writes == (["first"] if "-" in following else ["first", "second"])
    finally:
        release.set()
        for task in (first, second):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await manager.aclose()


@pytest.mark.asyncio
async def test_stopping_releases_input_and_shutdown_awaits_the_writer(
    clocked_manager: Clocked, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, factory, _trigger, _clock = clocked_manager
    started = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    entered = asyncio.Event()
    transport_closed = threading.Event()
    thread_finishing = asyncio.Event()
    finish_thread = threading.Event()
    exited = threading.Event()
    shutdown_started = asyncio.Event()
    shutting_down = False
    loop = asyncio.get_running_loop()
    close = adapter.close

    def blocked_write(_text: str) -> None:
        loop.call_soon_threadsafe(entered.set)
        assert transport_closed.wait(5), "stop did not close the transport"
        loop.call_soon_threadsafe(thread_finishing.set)
        assert finish_thread.wait(5), "test did not release the finishing writer"
        exited.set()
        raise EOFError("closed")

    def close_transport() -> None:
        close()
        transport_closed.set()
        if shutting_down:
            loop.call_soon_threadsafe(shutdown_started.set)

    monkeypatch.setattr(adapter, "write", blocked_write)
    monkeypatch.setattr(adapter, "close", close_transport)
    sending = asyncio.create_task(manager.send_operator_input(started.terminal_id, "blocked"))
    closing = None
    try:
        async with asyncio.timeout(2):
            await entered.wait()
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        async with asyncio.timeout(2):
            stopped = await manager.kill(started.terminal_id, owner())
            await thread_finishing.wait()
        assert stopped.stopped
        assert not exited.is_set()

        shutting_down = True
        closing = asyncio.create_task(manager.aclose())
        async with asyncio.timeout(2):
            await shutdown_started.wait()
        for _ in range(10):
            await asyncio.sleep(0)
        assert not closing.done()
        finish_thread.set()
        async with asyncio.timeout(2):
            await closing
        assert exited.is_set()
    finally:
        transport_closed.set()
        finish_thread.set()
        sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)
        if closing is not None:
            await closing


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
