"""Terminal manager: launch behavior."""

from __future__ import annotations

import ast
import asyncio
import os
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import core.tools._bash_environment as bash_environment
import core.tools._terminal_input as terminal_input
import core.tools._terminal_io as terminal_io
import core.tools.terminal_backend as terminal_backend
import core.tools.terminal_manager as terminal_module
from core.runs import RunExecutionOwner
from core.tools.terminal_manager import (
    TerminalClosedError,
    TerminalManager,
    TerminalStaleScreenError,
)
from tests.core.tools.terminal_manager_helpers import (
    TEST_ACTIVITY_QUIET_SECONDS,
    AdapterFactory,
    FakeClock,
    PendingTriggerService,
    establish_delivered_baseline,
    eventually,
    owner,
    settle_next_activity,
    spawn,
)
from tests.core.tools.terminal_manager_helpers import (
    terminal_manager as terminal_manager,
)


@pytest.mark.asyncio
async def test_execution_group_stop_keeps_unrelated_terminal_after_attachment_transfer(
    terminal_manager,
    tmp_path,
    monkeypatch,
):
    manager, _factory = terminal_manager
    monkeypatch.setattr(
        terminal_backend, "terminate_process_tree", lambda adapter, **_kwargs: adapter.terminate()
    )
    execution = RunExecutionOwner("fixture", "group", "peer", "generation", "epoch")
    owned = await manager.spawn(
        owner(),
        ["fake"],
        cwd=tmp_path,
        env=None,
        origin_run_id="run",
        execution_owner=execution,
    )
    unrelated = await manager.spawn(
        owner(), ["fake"], cwd=tmp_path, env=None, origin_run_id="other"
    )
    manager.detach(owned.terminal_id, owner())
    await manager.close_execution_group("fixture", "group", "epoch")
    assert not owned.adapter.is_alive()
    assert unrelated.adapter.is_alive()
    with pytest.raises(TerminalClosedError):
        await manager.spawn(
            owner(),
            ["fake"],
            cwd=tmp_path,
            env=None,
            origin_run_id="late",
            execution_owner=execution,
        )


@pytest.mark.asyncio
async def test_execution_group_stop_drains_pending_terminal_launch(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    factory = AdapterFactory()

    def blocked_factory(*args):
        started.set()
        assert release.wait(5)
        return factory(*args)

    monkeypatch.setattr(
        terminal_backend, "terminate_process_tree", lambda adapter, **_kwargs: adapter.terminate()
    )
    manager = TerminalManager(adapter_factory=blocked_factory)
    execution = RunExecutionOwner("fixture", "group", "peer", "generation", "epoch")
    launch = asyncio.create_task(
        manager.spawn(
            owner(),
            ["fake"],
            cwd=tmp_path,
            env=None,
            origin_run_id="run",
            execution_owner=execution,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        close = asyncio.create_task(manager.close_execution_group("fixture", "group", "epoch"))
        await asyncio.sleep(0)
        assert not close.done()
        release.set()
        session = await launch
        await close
        assert not session.adapter.is_alive()
    finally:
        release.set()
        await asyncio.gather(launch, return_exceptions=True)
        await manager.aclose()


@pytest.mark.asyncio
async def test_terminal_completion_uses_activity_owner_without_transferring_process_lifetime(
    tmp_path,
):
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
    execution = RunExecutionOwner("swarm", "group", "peer", "generation", "epoch")
    try:
        session = await establish_delivered_baseline(
            manager,
            factory,
            trigger,
            clock,
            tmp_path,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        manager.attach(
            session.terminal_id, owner(), origin_run_id="owned-run", execution_owner=execution
        )
        await manager.send_input(
            session.terminal_id,
            owner(),
            data="next\r",
            text=None,
            key=None,
            expected_screen_revision=None,
            origin_run_id="owned-run",
            execution_owner=execution,
        )
        generation = session.activity_generation
        factory.adapters[0].emit("new result")
        await settle_next_activity(
            clock, session, after_generation=generation, quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS
        )
        await eventually(lambda: len(trigger.submissions) == 2)
        assert trigger.submissions[-1][1]["execution_owner"] == execution
        assert session.execution_owner is None
        await manager.close_execution_group("swarm", "group", "epoch")
        assert session.adapter.is_alive()
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_operator_input_supersedes_pending_agent_start_and_stale_observation(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path, initial_text="agent task")
    revision = session.renderer.revision
    initial_task = session.initial_input_task
    assert initial_task is not None

    await manager.send_operator_input(session.terminal_id, "human input")
    await eventually(initial_task.done)
    assert factory.adapters[0].writes == ["human input"]
    with pytest.raises(TerminalStaleScreenError):
        await manager.send_input(
            session.terminal_id,
            owner(),
            text=None,
            key="enter",
            expected_screen_revision=revision,
            origin_run_id="run-a",
        )
    assert factory.adapters[0].writes == ["human input"]


@pytest.mark.asyncio
async def test_empty_agent_input_preserves_startup_and_pending_input(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path, initial_text="agent task")
    session.suppress_until_activity = True
    revision = session.renderer.revision
    result = await manager.send_input(
        session.terminal_id,
        owner(),
        data="",
        text=None,
        key=None,
        expected_screen_revision=revision,
        origin_run_id="run-a",
    )
    assert result["characters_sent"] == 0
    assert session.renderer.revision == revision
    assert session.suppress_until_activity
    assert session.initial_input_task is not None
    assert session.initial_input_task.cancelling() == 0
    assert factory.adapters[0].writes == []


@pytest.mark.asyncio
async def test_guarded_input_cannot_be_replayed_before_echo(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    revision = session.renderer.revision
    arguments = {
        "text": None,
        "key": "enter",
        "expected_screen_revision": revision,
        "origin_run_id": "run-a",
    }
    await manager.send_input(session.terminal_id, owner(), **arguments)
    with pytest.raises(TerminalStaleScreenError):
        await manager.send_input(session.terminal_id, owner(), **arguments)
    assert factory.adapters[0].writes == ["\r"]


@pytest.mark.asyncio
async def test_headless_queries_do_not_cancel_queued_initial_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_io, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
    factory = AdapterFactory()
    manager = TerminalManager(adapter_factory=factory, sweep_interval_seconds=3600)
    manager.start()
    try:
        session = await spawn(manager, tmp_path, initial_text="agent task")
        factory.adapters[0].emit("\x1b[6n")
        await eventually(lambda: factory.adapters[0].writes == ["\x1b[1;1R"])
        assert session.initial_input_task is not None
        assert not session.initial_input_task.done()
        factory.adapters[0].emit("READY> ")
        await eventually(lambda: factory.adapters[0].writes == ["\x1b[1;1R", "agent task", "\r"])
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_initial_task_waits_for_tui_and_sends_enter_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_io, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
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
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda env: ["host-shell"])
    monkeypatch.setattr(terminal_io, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
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
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda env: ["host-shell"])
    monkeypatch.setattr(terminal_io, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
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
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda env: ["host-shell"])
    monkeypatch.setattr(terminal_io, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
    monkeypatch.setattr(terminal_input, "TERMINAL_OPERATOR_READY_TIMEOUT_SECONDS", 0.01)
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
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda env: ["host-shell"])
    monkeypatch.setattr(terminal_io, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
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
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda env: ["host-shell"])
    monkeypatch.setattr(terminal_io, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
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
        assert terminal_input._shell_command(command, arguments, shell="sh") is None
    arguments = ["--profile", "work space", "$null", "$(echo BAD)", "a'b", 'a"b', "C:\\Tools\\"]
    rendered = terminal_input._shell_command("/path with spaces/tool", arguments, shell="bash")
    assert rendered is not None
    assert shlex.split(rendered) == ["/path with spaces/tool", *arguments]


# Real shell startup can exceed 10 seconds on shared Windows CI runners.
@pytest.mark.timeout(120)
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
    line = terminal_input._shell_command(
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
            timeout=60,
        )
        output = next(row for row in result.stdout.splitlines() if row.startswith("["))
    else:
        flags = ["-NoProfile", "-NonInteractive", "-Command"] if shell.endswith(".exe") else ["-c"]
        result = subprocess.run(
            [executable, *flags, line], env=environment, capture_output=True, text=True, timeout=60
        )
        output = result.stdout.strip()
    assert result.returncode == 0, result.stderr
    assert ast.literal_eval(output) == arguments


# Real shell startup can exceed 10 seconds on shared Windows CI runners.
@pytest.mark.timeout(120)
@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh.exe", "cmd.exe"])
def test_manual_launch_executes_program_path_with_spaces(shell: str) -> None:
    shell_path = shutil.which(shell)
    program = shutil.which("pwsh.exe")
    if os.name != "nt" or shell_path is None or program is None or " " not in program:
        pytest.skip("A Windows executable with a spaced path is required")
    line = terminal_input._shell_command(program, ["-NoProfile", "-Version"], shell=shell_path)
    assert line is not None
    if shell == "cmd.exe":
        result = subprocess.run(
            [shell_path, "/d", "/v:off"],
            input=line + "\nexit\n",
            capture_output=True,
            text=True,
            timeout=60,
        )
    else:
        result = subprocess.run(
            [shell_path, "-NoProfile", "-NonInteractive", "-Command", line],
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert result.returncode == 0, result.stderr
    assert any(row.startswith("PowerShell ") for row in result.stdout.splitlines())


def test_screen_prompt_markers_detect_common_shell_prompts() -> None:
    assert terminal_input._screen_has_prompt_marker("PS C:\\work> ") is True
    assert terminal_input._screen_has_prompt_marker("PS C:\\work>") is True
    assert terminal_input._screen_has_prompt_marker("C:\\work>") is True
    assert terminal_input._screen_has_prompt_marker("user@host:~/project$") is True
    assert terminal_input._screen_has_prompt_marker("$ ") is True
    assert terminal_input._screen_has_prompt_marker("> ") is True
    assert terminal_input._screen_has_prompt_marker("❯ ") is True
    assert terminal_input._screen_has_prompt_marker("") is False
    assert terminal_input._screen_has_prompt_marker("hello world") is False
    assert terminal_input._screen_has_prompt_marker("PS") is False


@pytest.mark.asyncio
async def test_terminal_reprobes_missing_program_and_keeps_explicit_env(tmp_path, monkeypatch):
    import core.tools.bash as bash_module

    calls = []
    probes = 0

    async def probe():
        nonlocal probes
        probes += 1
        return {"PATH": f"path-{probes}", "TERM": "dumb"}

    factory = AdapterFactory()

    def launch(argv, cwd, env, rows, columns):
        calls.append(dict(env))
        if len(calls) == 1:
            raise FileNotFoundError("test-owned absent executable")
        return factory(argv, cwd, env, rows, columns)

    monkeypatch.setattr(bash_environment, "_probe_shell_env", probe)
    bash_module.reset_shell_env_cache()
    manager = TerminalManager(adapter_factory=launch)
    try:
        await manager.spawn(
            owner(), ["new-program"], cwd=tmp_path, env={"EXPLICIT": "kept"}, origin_run_id="run"
        )
        assert [call["PATH"] for call in calls] == ["path-1", "path-2"]
        assert all(call["TERM"] == "xterm-256color" for call in calls)
        assert all(call["EXPLICIT"] == "kept" for call in calls)
    finally:
        await manager.aclose()
        bash_module.reset_shell_env_cache()
