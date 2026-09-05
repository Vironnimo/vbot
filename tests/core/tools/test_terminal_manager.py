"""Tests for Session-scoped interactive terminal lifecycle and attention."""

from __future__ import annotations

import ast
import asyncio
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import core.tools.terminal_manager as terminal_module
from core.tools.terminal_manager import (
    TerminalCapacityError,
    TerminalClosedError,
    TerminalManager,
    TerminalManagerError,
    TerminalNotFoundError,
    TerminalNotOwnedError,
    TerminalOwner,
    TerminalStaleScreenError,
)

TEST_ACTIVITY_QUIET_SECONDS = 1.0


class FakeTerminalAdapter:
    def __init__(self, initial_output: str | None = None) -> None:
        self._output: queue.Queue[str | None] = queue.Queue()
        if initial_output is not None:
            self._output.put(initial_output)
        self.writes: list[str] = []
        self.resizes: list[tuple[int, int]] = []
        self.alive = True
        self.code: int | None = None
        self.write_error: BaseException | None = None

    @property
    def pid(self) -> int:
        return 987_654

    def read(self, _size: int) -> str:
        value = self._output.get()
        if value is None:
            raise EOFError
        return value

    def write(self, text: str) -> None:
        if self.write_error is not None:
            error = self.write_error
            self.finish(0)
            raise error
        self.writes.append(text)

    def resize(self, rows: int, columns: int) -> None:
        self.resizes.append((rows, columns))

    def is_alive(self) -> bool:
        return self.alive

    def exit_code(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.finish(-1)

    def emit(self, text: str) -> None:
        self._output.put(text)

    def finish(self, code: int = 0) -> None:
        if not self.alive:
            return
        self.code = code
        self.alive = False
        self._output.put(None)


class AdapterFactory:
    def __init__(self, initial_output: str | None = None) -> None:
        self.initial_output = initial_output
        self.adapters: list[FakeTerminalAdapter] = []
        self.calls: list[tuple[list[str], Path, dict[str, str], int, int]] = []

    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        rows: int,
        columns: int,
    ) -> FakeTerminalAdapter:
        adapter = FakeTerminalAdapter(self.initial_output)
        self.adapters.append(adapter)
        self.calls.append((list(argv), cwd, dict(env), rows, columns))
        return adapter


class PendingTriggerService:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.submissions: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.cancellations: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def submit_completion(self, *args: Any, **kwargs: Any) -> Any:
        self.submissions.append((args, kwargs))

        async def pending() -> None:
            await self.release.wait()

        return pending()

    def cancel_completion(self, *args: Any, **kwargs: Any) -> None:
        self.cancellations.append((args, kwargs))


class FakeClock:
    """Controllable monotonic time for quiet/grace state-machine tests."""

    def __init__(self) -> None:
        self.now = 0.0
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        deadline = self.now + delay
        future = asyncio.get_running_loop().create_future()
        waiter = (deadline, future)
        self._waiters.append(waiter)
        try:
            await future
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    @property
    def sleeping(self) -> bool:
        return any(not future.done() for _, future in self._waiters)

    async def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("Fake time cannot move backwards")
        self.now += seconds
        due = [future for deadline, future in self._waiters if deadline <= self.now]
        for future in due:
            if not future.done():
                future.set_result(None)
        await asyncio.sleep(0)


@pytest_asyncio.fixture
async def terminal_manager() -> AsyncIterator[tuple[TerminalManager, AdapterFactory]]:
    factory = AdapterFactory()
    manager = TerminalManager(
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        yield manager, factory
    finally:
        await manager.aclose()


def owner(session_id: str = "session-a") -> TerminalOwner:
    return TerminalOwner("project-a", "agent-a", session_id)


async def spawn(
    manager: TerminalManager,
    tmp_path: Path,
    *,
    command: str = "fake-tui",
    initial_text: str | None = None,
) -> Any:
    return await manager.spawn(
        owner(),
        [command],
        cwd=tmp_path,
        env=None,
        columns=120,
        rows=32,
        origin_run_id="run-a",
        initial_text=initial_text,
    )


async def eventually(predicate: Any, *, attempts: int = 200) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not reached")


async def settle_next_activity(
    clock: FakeClock,
    session: Any,
    *,
    after_generation: int,
    quiet_seconds: float,
) -> None:
    await eventually(lambda: session.activity_generation > after_generation)
    await eventually(lambda: clock.sleeping)
    await clock.advance(quiet_seconds)
    await eventually(lambda: session.state == "ready")


async def establish_delivered_baseline(
    manager: TerminalManager,
    factory: AdapterFactory,
    trigger: PendingTriggerService,
    clock: FakeClock,
    tmp_path: Path,
    *,
    quiet_seconds: float,
) -> Any:
    session = await spawn(manager, tmp_path)
    session.state = "working"
    manager.attach(session.terminal_id, owner(), origin_run_id="attach-run")
    await manager.send_input(
        session.terminal_id,
        owner(),
        data="go\r",
        text=None,
        key=None,
        expected_screen_revision=None,
        origin_run_id="run-0",
    )
    generation = session.activity_generation
    factory.adapters[0].emit("MENU> ")
    await settle_next_activity(
        clock,
        session,
        after_generation=generation,
        quiet_seconds=quiet_seconds,
    )
    await eventually(lambda: len(trigger.submissions) == 1)
    trigger.release.set()
    await eventually(lambda: session.attention is not None and session.attention.delivered)
    return session


@pytest.mark.asyncio
async def test_initial_task_waits_for_tui_and_sends_enter_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
    factory = AdapterFactory("READY> ")
    manager = TerminalManager(adapter_factory=factory, sweep_interval_seconds=3600)
    manager.start()
    try:
        session = await spawn(manager, tmp_path, initial_text="do the work")
        assert session.state == "starting"
        await eventually(lambda: factory.adapters[0].writes == ["do the work", "\r"])
        assert session.state == "working"
        assert manager.get_session(session.terminal_id, owner()) is session
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_manual_command_runs_inside_the_default_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda: ["host-shell"])
    monkeypatch.setattr(terminal_module, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
    factory = AdapterFactory("PS C:\\work> ")
    manager = TerminalManager(adapter_factory=factory, sweep_interval_seconds=3600)
    manager.start()
    try:
        result = await manager.spawn_for_operator(
            command="codex",
            arguments=["--profile", "work"],
            cwd=tmp_path,
        )
        session = manager._sessions[result["terminal_id"]]

        assert session.owner is None
        assert session.command == "host-shell"
        assert session.arguments == ()
        assert session.launch_command == "codex"
        assert session.launch_arguments == ("--profile", "work")
        assert result["command"] == "host-shell"
        assert result["launch_command"] == "codex"
        assert result["launch_args"] == ["--profile", "work"]
        await eventually(lambda: factory.adapters[0].writes == ["codex --profile work", "\r"])
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_manual_command_quotes_arguments_with_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda: ["host-shell"])
    monkeypatch.setattr(terminal_module, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
    factory = AdapterFactory("PS C:\\work> ")
    manager = TerminalManager(adapter_factory=factory, sweep_interval_seconds=3600)
    manager.start()
    try:
        await manager.spawn_for_operator(
            command="codex",
            arguments=["--profile", "work space"],
            cwd=tmp_path,
        )
        await eventually(
            lambda: factory.adapters[0].writes == ["codex --profile 'work space'", "\r"]
        )
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_manual_command_is_not_written_without_a_shell_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda: ["host-shell"])
    monkeypatch.setattr(terminal_module, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
    monkeypatch.setattr(terminal_module, "TERMINAL_OPERATOR_READY_TIMEOUT_SECONDS", 0.01)
    factory = AdapterFactory()
    manager = TerminalManager(adapter_factory=factory, sweep_interval_seconds=3600)
    manager.start()
    try:
        result = await manager.spawn_for_operator(
            command="codex",
            arguments=[],
            cwd=tmp_path,
        )
        session = manager._sessions[result["terminal_id"]]
        assert session.operator_command_task is not None
        await eventually(lambda: session.operator_command_task.done())
        assert factory.adapters[0].writes == []
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_manual_command_is_not_written_when_shell_ends_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda: ["host-shell"])
    monkeypatch.setattr(terminal_module, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
    factory = AdapterFactory()
    manager = TerminalManager(adapter_factory=factory, sweep_interval_seconds=3600)
    manager.start()
    try:
        result = await manager.spawn_for_operator(
            command="codex",
            arguments=[],
            cwd=tmp_path,
        )
        session = manager._sessions[result["terminal_id"]]
        factory.adapters[0].finish(1)
        await eventually(lambda: session.state == "exited")
        assert session.operator_command_task is not None
        await eventually(lambda: session.operator_command_task.done())
        assert factory.adapters[0].writes == []
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_manual_command_survives_early_operator_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator input must not cancel the launch command write.

    The WebUI takes control immediately after a manual start; the first typed
    characters used to cancel the shared initial-input task and the launch
    command was never entered. The launch command has its own task and the
    operator input waits briefly for it instead of cancelling it.
    """
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda: ["host-shell"])
    monkeypatch.setattr(terminal_module, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
    factory = AdapterFactory("PS C:\\work> ")
    manager = TerminalManager(adapter_factory=factory, sweep_interval_seconds=3600)
    manager.start()
    try:
        result = await manager.spawn_for_operator(
            command="opencode2",
            arguments=[],
            cwd=tmp_path,
        )
        session = manager._sessions[result["terminal_id"]]
        assert session.operator_command_task is not None
        assert not session.operator_command_task.done()

        await manager.send_operator_input(session.terminal_id, "x")

        await eventually(lambda: factory.adapters[0].writes == ["opencode2", "\r", "x"])
        assert session.operator_command_task.done()
    finally:
        await manager.aclose()


def test_shell_command_renders_exact_typed_shell_input() -> None:
    for command, arguments in [(None, []), ("", ["arg"]), ("codex", [""])]:
        assert terminal_module._shell_command(command, arguments, shell="sh") is None
    arguments = ["--profile", "work space", "$null", "$(echo BAD)", "a'b", 'a"b', "C:\\Tools\\"]
    rendered = terminal_module._shell_command("/path with spaces/tool", arguments, shell="bash")
    assert rendered is not None
    assert shlex.split(rendered) == ["/path with spaces/tool", *arguments]


@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh.exe", "cmd.exe", "sh", "bash"])
def test_manual_launch_preserves_arguments_through_real_shell(shell: str) -> None:
    executable = shutil.which(shell)
    if executable is None or (os.name == "nt" and shell in {"sh", "bash"}):
        pytest.skip("Shell is not available for this platform")
    arguments = [
        "space value",
        'a"b',
        'a\\"b',
        "trailing space\\",
        "$null",
        "$(echo SHOULD_NOT_RUN)",
        "x`ny",
        "%VBOT_TERMINAL_TEST_VALUE%",
        "a,b",
        "a'b",
        "x&y",
        "a|b",
        "(arg)",
        "bang!",
    ]
    line = terminal_module._shell_command(
        sys.executable,
        ["-c", "import sys; print(repr(sys.argv[1:]))", *arguments],
        shell=executable,
    )
    assert line is not None
    environment = dict(os.environ, VBOT_TERMINAL_TEST_VALUE="MUST_NOT_EXPAND")
    if shell == "cmd.exe":
        result = subprocess.run(
            [executable, "/d", "/v:off"],
            input=line + "\nexit\n",
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = next(row for row in result.stdout.splitlines() if row.startswith("["))
    else:
        flags = ["-NoProfile", "-NonInteractive", "-Command"] if shell.endswith(".exe") else ["-c"]
        result = subprocess.run(
            [executable, *flags, line], env=environment, capture_output=True, text=True, timeout=10
        )
        output = result.stdout.strip()
    assert result.returncode == 0, result.stderr
    assert ast.literal_eval(output) == arguments


@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh.exe", "cmd.exe"])
def test_manual_launch_executes_program_path_with_spaces(shell: str) -> None:
    shell_path = shutil.which(shell)
    program = shutil.which("pwsh.exe")
    if os.name != "nt" or shell_path is None or program is None or " " not in program:
        pytest.skip("A Windows executable with a spaced path is required")
    line = terminal_module._shell_command(program, ["-NoProfile", "-Version"], shell=shell_path)
    assert line is not None
    if shell == "cmd.exe":
        result = subprocess.run(
            [shell_path, "/d", "/v:off"],
            input=line + "\nexit\n",
            capture_output=True,
            text=True,
            timeout=10,
        )
    else:
        result = subprocess.run(
            [shell_path, "-NoProfile", "-NonInteractive", "-Command", line],
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode == 0, result.stderr
    assert any(row.startswith("PowerShell ") for row in result.stdout.splitlines())


def test_screen_prompt_markers_detect_common_shell_prompts() -> None:
    assert terminal_module._screen_has_prompt_marker("PS C:\\work> ") is True
    assert terminal_module._screen_has_prompt_marker("PS C:\\work>") is True
    assert terminal_module._screen_has_prompt_marker("C:\\work>") is True
    assert terminal_module._screen_has_prompt_marker("user@host:~/project$") is True
    assert terminal_module._screen_has_prompt_marker("$ ") is True
    assert terminal_module._screen_has_prompt_marker("> ") is True
    assert terminal_module._screen_has_prompt_marker("❯ ") is True
    assert terminal_module._screen_has_prompt_marker("") is False
    assert terminal_module._screen_has_prompt_marker("hello world") is False
    assert terminal_module._screen_has_prompt_marker("PS") is False


@pytest.mark.asyncio
async def test_attachment_isolated_access_transfers_without_rewriting_origin(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, _factory = terminal_manager
    session = await spawn(manager, tmp_path)
    other = owner("session-b")

    with pytest.raises(TerminalNotOwnedError):
        manager.get_session(session.terminal_id, other)

    assert manager.transfer_scope(owner(), other) == 1
    assert manager.get_session(session.terminal_id, other) is session
    assert manager.list_sessions() == [session]
    assert session.owner == owner()
    assert session.lifecycle_owner == other
    assert session.attachment == other


@pytest.mark.asyncio
async def test_live_terminal_capacity_is_owner_scoped_and_globally_bounded(
    terminal_manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _factory = terminal_manager
    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_LIVE_PER_SESSION", 2)
    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_LIVE_GLOBAL", 3)

    await spawn(manager, tmp_path, command="owner-a-1")
    await spawn(manager, tmp_path, command="owner-a-2")
    with pytest.raises(TerminalCapacityError):
        await spawn(manager, tmp_path, command="owner-a-3")

    other_owner = TerminalOwner("project-a", "agent-a", "session-b")
    await manager.spawn(
        other_owner,
        ["owner-b-1"],
        cwd=tmp_path,
        env=None,
        columns=120,
        rows=32,
        origin_run_id="run-b",
    )

    with pytest.raises(TerminalCapacityError, match="3"):
        await manager.spawn_for_operator(
            command="manual-terminal",
            arguments=[],
            cwd=tmp_path,
        )


@pytest.mark.asyncio
async def test_waiting_reader_does_not_block_input_resize_or_stop(tmp_path, monkeypatch) -> None:
    reading = threading.Event()

    class WaitingAdapter(FakeTerminalAdapter):
        def read(self, size: int) -> str:
            reading.set()
            return super().read(size)

    adapter = WaitingAdapter()
    manager = TerminalManager(adapter_factory=lambda *args: adapter)
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(loop, "_default_executor", executor)
        monkeypatch.setattr(
            terminal_module, "terminate_process_tree", lambda child: child.terminate()
        )
        try:
            session = await spawn(manager, tmp_path)
            await eventually(reading.is_set)
            await asyncio.wait_for(manager.send_operator_input(session.terminal_id, "hello"), 1)
            await asyncio.wait_for(
                manager.resize_for_operator(session.terminal_id, columns=90, rows=24), 1
            )
            await asyncio.wait_for(manager.kill_for_operator(session.terminal_id), 1)
            assert adapter.writes == ["hello"]
            assert adapter.resizes == [(24, 90)]
            assert not adapter.alive
        finally:
            adapter.finish()
            await manager.aclose()


@pytest.mark.asyncio
async def test_cancelled_start_waits_for_child_cleanup(tmp_path, monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    adapter = FakeTerminalAdapter()

    def factory(*args):
        entered.set()
        assert release.wait(5)
        return adapter

    monkeypatch.setattr(terminal_module, "terminate_process_tree", lambda child: child.terminate())
    manager = TerminalManager(adapter_factory=factory)
    task = asyncio.create_task(spawn(manager, tmp_path))
    try:
        await eventually(entered.is_set)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert not adapter.alive
        assert not manager._pending_spawns
        assert all(session.state == "exited" for session in manager.list_sessions())
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await manager.aclose()


@pytest.mark.asyncio
async def test_pending_starts_reserve_owner_and_global_capacity(tmp_path, monkeypatch) -> None:
    release = threading.Event()
    adapters: list[FakeTerminalAdapter] = []

    def factory(*args):
        adapter = FakeTerminalAdapter()
        adapters.append(adapter)
        assert release.wait(5)
        return adapter

    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_LIVE_PER_SESSION", 2)
    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_LIVE_GLOBAL", 3)
    monkeypatch.setattr(terminal_module, "terminate_process_tree", lambda child: child.terminate())
    manager = TerminalManager(adapter_factory=factory)
    tasks = [asyncio.create_task(spawn(manager, tmp_path)) for _ in range(2)]
    try:
        await eventually(lambda: len(adapters) == 2)
        with pytest.raises(TerminalCapacityError):
            await asyncio.wait_for(spawn(manager, tmp_path), 1)
        tasks.append(
            asyncio.create_task(
                manager.spawn(
                    owner("other"), ["fake"], cwd=tmp_path, env=None, origin_run_id="run-other"
                )
            )
        )
        await eventually(lambda: len(adapters) == 3)
        with pytest.raises(TerminalCapacityError):
            await asyncio.wait_for(
                manager.spawn_for_operator(command=None, arguments=[], cwd=tmp_path), 1
            )
        release.set()
        await asyncio.gather(*tasks)
        assert len(manager.list_sessions()) == 3
        assert not manager._pending_spawns
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.aclose()


@pytest.mark.asyncio
async def test_shutdown_waits_for_pending_start_and_closes_its_child(tmp_path, monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    adapter = FakeTerminalAdapter()

    def factory(*args):
        entered.set()
        assert release.wait(5)
        return adapter

    monkeypatch.setattr(terminal_module, "terminate_process_tree", lambda child: child.terminate())
    manager = TerminalManager(adapter_factory=factory)
    task = asyncio.create_task(spawn(manager, tmp_path))
    try:
        await eventually(entered.is_set)
        closing = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(closing, 1)
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], terminal_module.TerminalLaunchError)
        assert not adapter.alive
        assert not manager.list_sessions()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await manager.aclose()


@pytest.mark.asyncio
async def test_failed_start_releases_its_capacity_reservation(tmp_path, monkeypatch) -> None:
    attempts = 0
    adapter = FakeTerminalAdapter()

    def factory(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("test-owned launch failure")
        return adapter

    monkeypatch.setattr(terminal_module, "TERMINAL_MAX_LIVE_GLOBAL", 1)
    monkeypatch.setattr(terminal_module, "terminate_process_tree", lambda child: child.terminate())
    manager = TerminalManager(adapter_factory=factory)
    try:
        with pytest.raises(terminal_module.TerminalLaunchError):
            await spawn(manager, tmp_path)
        assert not manager._pending_spawns
        await spawn(manager, tmp_path)
        assert len(manager.list_sessions()) == 1
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_screen_revision_guards_input_and_resize_updates_both_sides(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    adapter.emit("PROMPT> ")
    await eventually(lambda: session.renderer.revision > 0)

    with pytest.raises(TerminalStaleScreenError):
        await manager.send_input(
            session.terminal_id,
            owner(),
            text="answer",
            key="enter",
            expected_screen_revision=0,
            origin_run_id="run-b",
        )

    revision = session.renderer.revision
    await manager.send_input(
        session.terminal_id,
        owner(),
        text="answer",
        key="enter",
        expected_screen_revision=revision,
        origin_run_id="run-b",
    )
    assert adapter.writes == ["answer", "\r"]

    result = await manager.resize(session.terminal_id, owner(), columns=100, rows=24)
    assert adapter.resizes == [(24, 100)]
    assert result["columns"] == 100
    assert result["rows"] == 24


@pytest.mark.asyncio
async def test_resize_to_current_dimensions_is_a_no_op(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)

    result = await manager.resize(session.terminal_id, owner(), columns=120, rows=32)
    operator_result = await manager.resize_for_operator(session.terminal_id, columns=120, rows=32)

    assert factory.adapters[0].resizes == []
    assert result["columns"] == 120
    assert result["rows"] == 32
    assert result["screen_revision"] == session.renderer.revision
    assert operator_result["screen_revision"] == session.renderer.revision
    assert session.attention is None
    assert session.state != "working"


@pytest.mark.asyncio
async def test_resize_hard_deadline_caps_repaint_suppression(tmp_path: Path) -> None:
    """A repaint stream must not suppress Agent delivery past the hard cap."""
    clock = FakeClock()
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    manager.start()
    try:
        session = await establish_delivered_baseline(
            manager,
            factory,
            trigger,
            clock,
            tmp_path,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        await manager.resize(session.terminal_id, owner(), columns=90, rows=24)
        generation = session.activity_generation
        factory.adapters[0].emit("repaint wave 1")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        assert len(trigger.submissions) == 1

        await clock.advance(terminal_module.TERMINAL_RESIZE_GRACE_MAX_SECONDS)
        generation = session.activity_generation
        factory.adapters[0].emit("work after grace cap")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        await eventually(lambda: len(trigger.submissions) == 2)
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_agent_input_clears_resize_grace_and_wakes_on_later_output(
    tmp_path: Path,
) -> None:
    """Input after a resize is work, so it ends the grace: a settle following
    it must deliver immediately instead of being treated as repaint noise."""
    clock = FakeClock()
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    manager.start()
    try:
        session = await establish_delivered_baseline(
            manager,
            factory,
            trigger,
            clock,
            tmp_path,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        await manager.resize(session.terminal_id, owner(), columns=100, rows=24)
        await manager.send_input(
            session.terminal_id,
            owner(),
            data="answer\r",
            text=None,
            key=None,
            expected_screen_revision=None,
            origin_run_id="run-c",
        )
        assert session.resize_grace_deadline == 0.0
        generation = session.activity_generation
        factory.adapters[0].emit("output after agent input")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        await eventually(lambda: len(trigger.submissions) == 2)
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_unchanged_screen_stays_silent_until_content_changes(
    tmp_path: Path,
) -> None:
    """Suppression only covers an unchanged screen: once the screen actually
    changes, the next quiet boundary wakes the agent again."""
    clock = FakeClock()
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    manager.start()
    try:
        session = await establish_delivered_baseline(
            manager,
            factory,
            trigger,
            clock,
            tmp_path,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        generation = session.activity_generation
        factory.adapters[0].emit("\rMENU> ")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        assert len(trigger.submissions) == 1

        generation = session.activity_generation
        factory.adapters[0].emit("\rMENU> \nsecond option")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        await eventually(lambda: len(trigger.submissions) == 2)
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_textless_agent_start_suppresses_the_startup_settle(
    tmp_path: Path,
) -> None:
    """A text-less Agent start stays silent until the first explicit input:
    the startup screen (banner, prompt, TUI boot) is observed by the starting
    Agent and must not wake the session."""
    clock = FakeClock()
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    manager.start()
    try:
        session = await spawn(manager, tmp_path)
        session.state = "working"

        generation = session.activity_generation
        factory.adapters[0].emit("TUI banner")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        assert trigger.submissions == []
        assert session.attention is None or not session.attention.delivered

        generation = session.activity_generation
        factory.adapters[0].emit("\rTUI banner")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        assert trigger.submissions == []

        await manager.send_input(
            session.terminal_id,
            owner(),
            data="go\r",
            text=None,
            key=None,
            expected_screen_revision=None,
            origin_run_id="run-0",
        )
        generation = session.activity_generation
        factory.adapters[0].emit("output after input")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        await eventually(lambda: len(trigger.submissions) == 1)
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_resize_repaint_is_swallowed_until_stable_then_uses_new_baseline(
    tmp_path: Path,
) -> None:
    """After a real resize the TUI repaints in several waves. Settles stay
    suppressed until the repainted screen is stable across two boundaries;
    that stable screen becomes the new baseline — identical refreshes stay
    silent, a changed screen delivers."""
    clock = FakeClock()
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    manager.start()
    try:
        session = await establish_delivered_baseline(
            manager,
            factory,
            trigger,
            clock,
            tmp_path,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )

        await manager.resize(session.terminal_id, owner(), columns=90, rows=24)
        generation = session.activity_generation
        factory.adapters[0].emit("\rMENU> ")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        generation = session.activity_generation
        factory.adapters[0].emit("\rMENU> status")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        generation = session.activity_generation
        factory.adapters[0].emit("\rMENU> status")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        assert len(trigger.submissions) == 1

        generation = session.activity_generation
        factory.adapters[0].emit("\rMENU> status")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        assert len(trigger.submissions) == 1

        await clock.advance(terminal_module.TERMINAL_RESIZE_GRACE_MAX_SECONDS)
        generation = session.activity_generation
        factory.adapters[0].emit("\rMENU> status\nnew work output line")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        await eventually(lambda: len(trigger.submissions) == 2)
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_operator_stream_starts_with_ansi_snapshot_and_continues_in_sequence(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    adapter.emit(
        "\x1b]0;Codex auth refactor\x07"
        + "".join(f"history-{index}\r\n" for index in range(40))
        + "\x1b[31mREADY>\x1b[0m "
    )
    await eventually(lambda: session.renderer.page(before=None, limit=100)["line_count"] > 0)

    stream = manager.watch_for_operator(session.terminal_id)
    ready = await anext(stream)
    assert ready["type"] == "terminal_ready"
    assert ready["terminal"]["owner"] == {
        "project_id": "project-a",
        "agent_id": "agent-a",
        "session_id": "session-a",
    }
    assert ready["terminal"]["title"] == "Codex auth refactor"
    assert "history-0" in ready["ansi"]
    assert "READY>" in ready["ansi"]
    assert "\x1b[2J" in ready["ansi"]

    next_event = asyncio.create_task(anext(stream))
    adapter.emit("\x1b]0;Codex tests\x07next")
    event = await asyncio.wait_for(next_event, timeout=1)
    while event["type"] != "terminal_output":
        assert event["sequence"] > ready["sequence"]
        event = await asyncio.wait_for(anext(stream), timeout=1)
    assert event["data"].endswith("next")
    assert event["sequence"] > ready["sequence"]
    state_event = await asyncio.wait_for(anext(stream), timeout=1)
    assert state_event["type"] == "terminal_state"
    assert state_event["terminal"]["title"] == "Codex tests"
    await stream.aclose()


@pytest.mark.asyncio
async def test_operator_stream_refreshes_authoritative_screen_after_alternate_screen_exit(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    adapter.emit("PS> ")
    await eventually(lambda: session.renderer.screen_text() == "PS>")
    stream = manager.watch_for_operator(session.terminal_id)
    ready = await anext(stream)

    adapter.emit("\x1b[?1049h\x1b[2J\x1b[Hnvim\x1b[?1049lPS> ")
    output = await asyncio.wait_for(anext(stream), timeout=1)
    while output["type"] != "terminal_output":
        output = await asyncio.wait_for(anext(stream), timeout=1)
    snapshot = await asyncio.wait_for(anext(stream), timeout=1)

    assert output["type"] == "terminal_output"
    assert snapshot["type"] == "terminal_snapshot"
    assert snapshot["sequence"] == output["sequence"] + 1
    assert snapshot["sequence"] > ready["sequence"]
    assert "PS>" in snapshot["ansi"]
    assert "nvim" not in snapshot["ansi"]
    await stream.aclose()


@pytest.mark.asyncio
async def test_operator_stream_refreshes_after_tui_disables_bracketed_paste(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    adapter.emit("\x1b[?2004hTUI")
    await eventually(lambda: session.renderer.bracketed_paste_enabled)
    stream = manager.watch_for_operator(session.terminal_id)
    ready = await anext(stream)

    adapter.emit("\x1b[?2004lPS> ")
    snapshot: dict[str, Any] | None = None
    while snapshot is None:
        event = await asyncio.wait_for(anext(stream), timeout=1)
        if event["type"] == "terminal_snapshot":
            snapshot = event

    assert snapshot["sequence"] > ready["sequence"]
    assert "PS>" in snapshot["ansi"]
    assert session.renderer.bracketed_paste_enabled is False
    await stream.aclose()


@pytest.mark.asyncio
async def test_operator_stream_publishes_final_snapshot_before_terminal_state(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    stream = manager.watch_for_operator(session.terminal_id)
    ready = await anext(stream)

    factory.adapters[0].emit("final screen")
    await eventually(lambda: "final screen" in session.renderer.screen_text())
    factory.adapters[0].finish(0)
    events: list[dict[str, Any]] = []
    while not any(
        event["type"] == "terminal_state" and event["terminal"]["state"] == "exited"
        for event in events
    ):
        events.append(await asyncio.wait_for(anext(stream), timeout=1))

    terminal_snapshot_index = next(
        index for index, event in enumerate(events) if event["type"] == "terminal_snapshot"
    )
    terminal_state_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "terminal_state" and event["terminal"]["state"] == "exited"
    )
    assert terminal_snapshot_index < terminal_state_index
    assert events[terminal_snapshot_index]["sequence"] > ready["sequence"]
    assert "final screen" in events[terminal_snapshot_index]["ansi"]
    await stream.aclose()


@pytest.mark.asyncio
async def test_operator_controls_same_live_session_and_changed_callbacks(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    changed: list[str] = []
    unsubscribe = manager.add_changed_callback(changed.append)
    session = await spawn(manager, tmp_path)

    listed = manager.list_for_operator()
    assert [item["terminal_id"] for item in listed] == [session.terminal_id]
    assert changed == [session.terminal_id]

    result = await manager.send_operator_input(session.terminal_id, "hello\r")
    assert result["state"] == "working"
    assert factory.adapters[0].writes == ["hello\r"]

    resized = await manager.resize_for_operator(session.terminal_id, columns=90, rows=28)
    assert resized["columns"] == 90
    assert resized["rows"] == 28
    assert factory.adapters[0].resizes == [(28, 90)]

    killed = await manager.kill_for_operator(session.terminal_id)
    assert killed["state"] == "exited"
    assert manager.list_for_operator()[0]["state"] == "exited"
    forgotten = manager.forget_for_operator(session.terminal_id)
    assert forgotten["state"] == "exited"
    assert manager.list_for_operator() == []
    assert changed.count(session.terminal_id) >= 5
    unsubscribe()


@pytest.mark.asyncio
async def test_closed_pty_write_marks_terminal_exited_and_delivers_attention(
    tmp_path: Path,
) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        session = await spawn(manager, tmp_path)
        manager.attach(session.terminal_id, owner(), origin_run_id="attach-run")
        factory.adapters[0].write_error = EOFError("Pty is closed")

        with pytest.raises(TerminalClosedError):
            await manager.send_operator_input(session.terminal_id, "late input")

        await eventually(lambda: len(trigger.submissions) == 1)
        assert session.state == "exited"
        assert session.attention is not None
        assert session.attention.kind == "exited"
        assert trigger.submissions[0][1]["origin_run_id"] == "attach-run"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_unattached_operator_terminal_has_no_agent_scope_or_attention_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda: ["host-shell"])
    manager.start()
    try:
        result = await manager.spawn_for_operator(
            command=None,
            arguments=["--login"],
            cwd=tmp_path,
        )
        terminal_id = result["terminal_id"]
        session = manager._sessions[terminal_id]

        assert factory.calls[0][0] == ["host-shell", "--login"]
        assert result["owner"] is None
        assert result["attachment"] is None
        assert session.owner is None
        assert session.lifecycle_owner is None
        assert session.attachment is None
        assert manager.list_sessions() == [session]

        await manager.close_project_scope("project-a")
        assert factory.adapters[0].alive is True

        await manager.send_operator_input(terminal_id, "echo ready\r")
        factory.adapters[0].emit("ready\r\n")
        await eventually(lambda: session.state == "ready")
        assert trigger.submissions == []

        factory.adapters[0].finish(0)
        await eventually(lambda: session.state == "exited")
        assert trigger.submissions == []
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_operator_terminal_attach_delivers_activity_and_detach_preserves_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda: ["host-shell"])
    manager.start()
    try:
        result = await manager.spawn_for_operator(command=None, arguments=[], cwd=tmp_path)
        terminal_id = result["terminal_id"]
        session = manager._sessions[terminal_id]

        attached, changed = manager.attach(terminal_id, owner(), origin_run_id="attach-run")
        assert attached is session
        assert changed is True
        assert session.owner is None
        assert session.lifecycle_owner is None
        assert session.attachment == owner()
        assert manager.get_session(terminal_id, owner()) is session

        same, changed = manager.attach(terminal_id, owner(), origin_run_id="attach-run-2")
        assert same is session
        assert changed is False

        await manager.send_operator_input(terminal_id, "echo ready\r")
        factory.adapters[0].emit("ready\r\n")
        await eventually(lambda: len(trigger.submissions) == 1)
        assert trigger.submissions[0][0] == ("agent-a", "session-a")
        assert trigger.submissions[0][1]["origin_run_id"] == "attach-run-2"

        assert manager.detach(terminal_id, owner()) is session
        assert session.attachment is None
        assert factory.adapters[0].alive is True
        with pytest.raises(TerminalNotOwnedError):
            manager.get_session(terminal_id, owner())

        await manager.close_scope(owner())
        assert factory.adapters[0].alive is True
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_attach_arms_an_already_working_terminal_for_its_next_quiet_boundary(
    tmp_path: Path,
) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        result = await manager.spawn_for_operator(command=None, arguments=[], cwd=tmp_path)
        session = manager._sessions[result["terminal_id"]]
        session.state = "working"

        manager.attach(session.terminal_id, owner(), origin_run_id="attach-run")

        await eventually(lambda: len(trigger.submissions) == 1)
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
        assert trigger.submissions[0][1]["origin_run_id"] == "attach-run"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_attach_rejects_another_session_and_detached_agent_origin_still_owns_lifecycle(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    other = owner("session-b")

    with pytest.raises(terminal_module.TerminalAlreadyAttachedError):
        manager.attach(session.terminal_id, other, origin_run_id="run-b")

    manager.detach(session.terminal_id, owner())
    attached, changed = manager.attach(session.terminal_id, other, origin_run_id="run-b")
    assert attached is session
    assert changed is True
    assert session.owner == owner()
    assert session.lifecycle_owner == owner()
    assert session.attachment == other

    await manager.close_scope(other)
    assert factory.adapters[0].alive is True
    assert session.attachment is None

    await manager.close_scope(owner())
    assert factory.adapters[0].alive is False


@pytest.mark.asyncio
async def test_session_move_transfers_operator_terminal_attachment_not_lifecycle(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    result = await manager.spawn_for_operator(command=None, arguments=[], cwd=tmp_path)
    session = manager._sessions[result["terminal_id"]]
    target = owner("session-b")
    manager.attach(session.terminal_id, owner(), origin_run_id="run-a")

    assert manager.transfer_scope(owner(), target) == 1
    assert session.owner is None
    assert session.lifecycle_owner is None
    assert session.attachment == target
    assert manager.get_session(session.terminal_id, target) is session

    await manager.close_scope(target)
    assert session.attachment is None
    assert factory.adapters[0].alive is True


@pytest.mark.asyncio
async def test_manual_launch_history_is_persistent_mru_and_deduplicated(tmp_path: Path) -> None:
    history_path = tmp_path / "terminals" / "launch-history.json"
    factory = AdapterFactory()
    manager = TerminalManager(
        adapter_factory=factory,
        launch_history_path=history_path,
        data_dir=tmp_path,
        sweep_interval_seconds=3600,
    )
    manager.start()
    try:
        await manager.spawn_for_operator(
            command="python",
            arguments=["-m", "http.server", "8080"],
            cwd=tmp_path,
            launch_workdir="~/sites/docs",
        )
        await manager.spawn_for_operator(
            command="codex",
            arguments=["--profile", "work space"],
            cwd=tmp_path,
            launch_workdir="C:\\Development\\vBot",
        )
        await manager.spawn_for_operator(
            command="python",
            arguments=["-m", "http.server", "8080"],
            cwd=tmp_path,
            launch_workdir="~/sites/docs",
        )

        history = manager.list_operator_launch_history()
        assert len(history) == 2
        assert history[0]["command"] == "python"
        assert history[0]["args"] == ["-m", "http.server", "8080"]
        assert history[0]["workdir"] == "~/sites/docs"
        assert len(history[0]["id"]) == 64
        assert history[1]["command"] == "codex"
        assert history_path.is_file()
    finally:
        await manager.aclose()

    reloaded = TerminalManager(
        launch_history_path=history_path,
        data_dir=tmp_path,
        adapter_factory=AdapterFactory(),
    )
    assert reloaded.list_operator_launch_history() == history


@pytest.mark.asyncio
async def test_operator_kill_recovers_a_partially_recorded_finish(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, _factory = terminal_manager
    session = await spawn(manager, tmp_path)
    session.finished_at = session.started_at

    result = await manager.kill_for_operator(session.terminal_id)

    assert result["state"] == "exited"
    assert manager.list_for_operator()[0]["state"] == "exited"


@pytest.mark.asyncio
async def test_finished_operator_history_expires_with_a_catalog_change(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, _factory = terminal_manager
    changed: list[str] = []
    manager.add_changed_callback(changed.append)
    session = await spawn(manager, tmp_path)
    await manager.kill_for_operator(session.terminal_id)
    session.finished_at = (
        terminal_module._utc_now() - terminal_module.TERMINAL_FINISHED_TTL - timedelta(seconds=1)
    )
    changed.clear()

    await manager.sweep_finished()

    assert manager.list_for_operator() == []
    assert changed == [session.terminal_id]


@pytest.mark.asyncio
async def test_status_is_bounded_and_pages_back_with_absolute_lines(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    factory.adapters[0].emit("".join(f"line-{index}\r\n" for index in range(50)))
    await eventually(lambda: session.renderer.revision > 0)

    snapshot = await manager.snapshot(session.terminal_id, owner(), lines=3)
    assert snapshot["scrollback"]["line_count"] == 3
    assert snapshot["scrollback"]["total_lines"] == 50
    assert snapshot["scrollback"]["next_start_line"] is not None

    older = await manager.snapshot(
        session.terminal_id,
        owner(),
        lines=3,
        start_line=snapshot["scrollback"]["next_start_line"],
    )
    assert older["scrollback"]["line_count"] == 3
    assert older["scrollback"]["start_line"] == snapshot["scrollback"]["next_start_line"]


@pytest.mark.asyncio
async def test_launch_passes_every_program_exact_argv_without_private_environment(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await manager.spawn(
        owner(),
        ["codex", "--profile", "work"],
        cwd=tmp_path,
        env={"CALLER_VALUE": "unchanged"},
        columns=120,
        rows=32,
        origin_run_id="run-a",
    )
    launch_argv, _cwd, launch_env, _rows, _columns = factory.calls[0]
    assert launch_argv == ["codex", "--profile", "work"]
    assert launch_env["CALLER_VALUE"] == "unchanged"
    assert not any(name.startswith("VBOT_TERMINAL_") for name in launch_env)
    assert not hasattr(session, "codex_integration")


@pytest.mark.asyncio
async def test_exact_agent_data_and_named_keys_share_the_generic_pty(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)

    raw = "\x1b[200~paste\r\n\x1b[201~"
    sent = await manager.send_input(
        session.terminal_id,
        owner(),
        data=raw,
        text=None,
        key=None,
        expected_screen_revision=None,
        origin_run_id="run-b",
    )
    await manager.send_input(
        session.terminal_id,
        owner(),
        data=None,
        text=None,
        key="f12",
        expected_screen_revision=None,
        origin_run_id="run-b",
    )

    assert factory.adapters[0].writes == [raw, "\x1b[24~"]
    assert sent["characters_sent"] == len(raw)


@pytest.mark.asyncio
async def test_multiline_text_uses_bracketed_paste_only_when_terminal_enables_it(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    multiline = "first\n  second\n    third"
    adapter.emit("\x1b[?2004h")
    await eventually(lambda: session.renderer.bracketed_paste_enabled)

    pasted = await manager.send_input(
        session.terminal_id,
        owner(),
        data=None,
        text=multiline,
        key=None,
        expected_screen_revision=None,
        origin_run_id="run-b",
    )

    assert adapter.writes == [f"\x1b[200~{multiline}\x1b[201~"]
    assert pasted["bracketed_paste"] is True

    adapter.emit("\x1b[?2004l")
    await eventually(lambda: not session.renderer.bracketed_paste_enabled)
    typed = await manager.send_input(
        session.terminal_id,
        owner(),
        data=None,
        text=multiline,
        key=None,
        expected_screen_revision=None,
        origin_run_id="run-b",
    )

    assert adapter.writes[-1] == multiline
    assert typed["bracketed_paste"] is False


@pytest.mark.asyncio
async def test_operator_activity_wakes_the_attached_agent_session(tmp_path: Path) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        session = await spawn(manager, tmp_path)
        # A text-less Agent start suppresses the first settle (startup screen
        # is not work); operator input re-arms delivery.
        await manager.send_operator_input(session.terminal_id, "look\r")
        factory.adapters[0].emit("screen changed")
        await eventually(lambda: session.attention_revision == 1)

        assert session.state == "ready"
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
        await eventually(lambda: len(trigger.submissions) == 1)
        assert len(trigger.submissions) == 1
        assert trigger.submissions[0][0] == ("agent-a", "session-a")
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_wait_exit_and_explicit_kill_have_distinct_attention(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    natural = await spawn(manager, tmp_path)
    factory.adapters[0].finish(7)
    await eventually(lambda: natural.state == "exited")
    snapshot, timed_out = await manager.wait_for_attention(
        natural.terminal_id, owner(), after_revision=0, timeout_ms=10
    )
    assert not timed_out
    assert snapshot["attention"]["kind"] == "exited"
    assert snapshot["exit_code"] == 7

    killed = await spawn(manager, tmp_path)
    result = await manager.kill(killed.terminal_id, owner())
    assert result["state"] == "exited"
    assert result["attention"] is None


@pytest.mark.asyncio
async def test_attention_auto_delivers_and_manual_ack_cancels_exactly_once(
    tmp_path: Path,
) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        session = await spawn(manager, tmp_path)
        await manager.send_input(
            session.terminal_id,
            owner(),
            data=None,
            text="do work",
            key="enter",
            expected_screen_revision=None,
            origin_run_id="run-b",
        )
        factory.adapters[0].emit("working...\r\nREADY> ")
        await eventually(lambda: len(trigger.submissions) == 1)

        args, kwargs = trigger.submissions[0]
        assert args == ("agent-a", "session-a")
        assert kwargs["origin_run_id"] == "run-b"
        assert kwargs["project_id"] == "project-a"
        assert isinstance(kwargs["body"], str)
        assert session.terminal_id in kwargs["body"]
        assert "```" in kwargs["body"]
        assert "working..." in kwargs["body"]
        assert session.attention is not None
        assert session.attention.kind == "output_settled"

        manager.acknowledge_attention(session.terminal_id, owner(), 1)
        await asyncio.sleep(0)
        assert len(trigger.cancellations) == 1
        assert session.acknowledged_attention_revision == 1
        assert session.notification_task is not None
        assert session.notification_task.cancelled()
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_new_output_postpones_a_pending_agent_wakeup_to_the_next_quiet_boundary(
    tmp_path: Path,
) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        session = await spawn(manager, tmp_path)
        await manager.send_input(
            session.terminal_id,
            owner(),
            data="begin",
            text=None,
            key=None,
            expected_screen_revision=None,
            origin_run_id="run-b",
        )
        await eventually(lambda: len(trigger.submissions) == 1)

        factory.adapters[0].emit("late output")
        await eventually(lambda: len(trigger.cancellations) == 1)
        await eventually(lambda: len(trigger.submissions) == 2)

        assert session.attention_revision == 2
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
        assert trigger.submissions[0][1]["notice_id"] != trigger.submissions[1][1]["notice_id"]
        assert trigger.submissions[1][1]["origin_run_id"] == "run-b"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_session_move_reroutes_pending_attention_to_new_owner(tmp_path: Path) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        session = await spawn(manager, tmp_path)
        await manager.send_input(
            session.terminal_id,
            owner(),
            data=None,
            text="do work",
            key="enter",
            expected_screen_revision=None,
            origin_run_id="run-b",
        )
        await eventually(lambda: len(trigger.submissions) == 1)
        target = TerminalOwner("project-b", "agent-b", "session-a")

        assert manager.transfer_scope(owner(), target) == 1
        await eventually(lambda: len(trigger.submissions) == 2)

        assert trigger.submissions[0][0] == ("agent-a", "session-a")
        assert trigger.submissions[1][0] == ("agent-b", "session-a")
        assert trigger.submissions[1][1]["project_id"] == "project-b"
        assert len(trigger.cancellations) == 1
        assert manager.get_session(session.terminal_id, target) is session
    finally:
        await manager.aclose()


async def _spawn_manual(
    manager: TerminalManager,
    tmp_path: Path,
    *,
    group_id: str | None = None,
) -> str:
    result = await manager.spawn_for_operator(
        command=None,
        arguments=[],
        cwd=tmp_path,
        group_id=group_id,
    )
    return str(result["terminal_id"])


@pytest.mark.asyncio
async def test_user_groups_are_unique_persistent_and_renamable(tmp_path: Path) -> None:
    groups_path = tmp_path / "terminals" / "groups.json"
    manager = TerminalManager(
        groups_path=groups_path,
        data_dir=tmp_path,
        sweep_interval_seconds=3600,
    )
    try:
        created = manager.create_group_for_operator("Work")
        assert created["kind"] == "user"
        assert created["terminal_count"] == 0
        with pytest.raises(TerminalManagerError, match="already exists"):
            manager.create_group_for_operator("work")

        renamed = manager.rename_group_for_operator(created["group_id"], "Dev")
        assert renamed["name"] == "Dev"
        assert groups_path.is_file()
    finally:
        await manager.aclose()

    reloaded = TerminalManager(
        groups_path=groups_path,
        data_dir=tmp_path,
        sweep_interval_seconds=3600,
    )
    try:
        groups = reloaded.list_groups_for_operator()
        assert [(group["name"], group["kind"]) for group in groups] == [("Dev", "user")]
    finally:
        await reloaded.aclose()


@pytest.mark.asyncio
async def test_agent_group_is_created_and_reused_by_name(tmp_path: Path) -> None:
    manager = TerminalManager(sweep_interval_seconds=3600)
    try:
        first = manager.resolve_or_create_agent_group("codex")
        second = manager.resolve_or_create_agent_group("codex")
        assert first.group_id == second.group_id
        assert first.kind == "agent"

        # An operator user group with the same name wins the reuse lookup.
        user = manager.create_group_for_operator("My Codex")
        reused = manager.resolve_or_create_agent_group("my codex")
        assert reused.group_id == user["group_id"]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_spawned_terminals_join_explicit_and_automatic_groups(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, _factory = terminal_manager
    group = manager.create_group_for_operator("Work")
    agent_session = await spawn(manager, tmp_path)
    manual_id = await _spawn_manual(manager, tmp_path)
    grouped_id = await _spawn_manual(manager, tmp_path, group_id=group["group_id"])

    by_id = {summary["terminal_id"]: summary for summary in manager.list_for_operator()}
    assert by_id[agent_session.terminal_id]["group_id"] == "auto:agent:agent-a"
    assert by_id[manual_id]["group_id"] == "auto:manual"
    assert by_id[grouped_id]["group_id"] == group["group_id"]

    groups = manager.list_groups_for_operator()
    by_name = {item["name"]: item for item in groups}
    assert by_name["Work"]["terminal_count"] == 1
    assert by_name["Manual"]["terminal_count"] == 1
    assert by_name["Agent agent-a"]["terminal_count"] == 1
    assert "finished" not in by_name


@pytest.mark.asyncio
async def test_killed_terminal_moves_to_finished_group_only_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
    factory = AdapterFactory("READY> ")
    manager = TerminalManager(adapter_factory=factory, sweep_interval_seconds=3600)
    manager.start()
    try:
        session = await spawn(manager, tmp_path)
        await manager.kill_for_operator(session.terminal_id)

        by_id = {summary["terminal_id"]: summary for summary in manager.list_for_operator()}
        assert by_id[session.terminal_id]["group_id"] == "finished"
        groups = manager.list_groups_for_operator()
        finished = [group for group in groups if group["kind"] == "finished"]
        assert len(finished) == 1
        assert finished[0]["terminal_count"] == 1

        manager.forget_for_operator(session.terminal_id)
        groups = manager.list_groups_for_operator()
        assert all(group["kind"] != "finished" for group in groups)
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_group_order_is_persisted_and_new_terminals_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    groups_path = tmp_path / "terminals" / "groups.json"
    factory = AdapterFactory()
    manager = TerminalManager(
        adapter_factory=factory,
        groups_path=groups_path,
        data_dir=tmp_path,
        sweep_interval_seconds=3600,
    )
    manager.start()
    try:
        group = manager.create_group_for_operator("Work")
        group_id = group["group_id"]
        first = await _spawn_manual(manager, tmp_path, group_id=group_id)
        second = await _spawn_manual(manager, tmp_path, group_id=group_id)

        manager.set_group_order_for_operator(group_id, [second, first])
        listed = [item["terminal_id"] for item in manager.list_for_operator()]
        assert listed[:2] == [second, first]

        third = await _spawn_manual(manager, tmp_path, group_id=group_id)
        listed = [item["terminal_id"] for item in manager.list_for_operator()]
        assert listed == [second, first, third]

        with pytest.raises(TerminalManagerError, match="do not belong"):
            manager.set_group_order_for_operator(group_id, ["other-terminal"])
    finally:
        await manager.aclose()

    reloaded = TerminalManager(
        groups_path=groups_path,
        data_dir=tmp_path,
        sweep_interval_seconds=3600,
    )
    groups = reloaded.list_groups_for_operator()
    assert groups[0]["order"] == [second, first]


@pytest.mark.asyncio
async def test_deleting_a_group_kills_every_terminal_in_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
    factory = AdapterFactory("READY> ")
    manager = TerminalManager(adapter_factory=factory, sweep_interval_seconds=3600)
    manager.start()
    try:
        group = manager.create_group_for_operator("Work")
        group_id = group["group_id"]
        first = await _spawn_manual(manager, tmp_path, group_id=group_id)
        second = await _spawn_manual(manager, tmp_path, group_id=group_id)
        outsider = await _spawn_manual(manager, tmp_path)

        result = await manager.delete_group_for_operator(group_id)
        assert result["terminals_killed"] == 2
        group_ids = {item["group_id"] for item in manager.list_groups_for_operator()}
        assert group_id not in group_ids

        by_id = {summary["terminal_id"]: summary for summary in manager.list_for_operator()}
        assert by_id[first]["state"] == "exited"
        assert by_id[second]["state"] == "exited"
        assert by_id[first]["group_id"] == "finished"
        assert by_id[second]["group_id"] == "finished"
        assert by_id[outsider]["state"] != "exited"

        with pytest.raises(TerminalNotFoundError):
            await manager.delete_group_for_operator(group_id)
    finally:
        await manager.aclose()
