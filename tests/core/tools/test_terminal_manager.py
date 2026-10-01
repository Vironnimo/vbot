"""Terminal manager: launch environment, start input, and manual launch commands."""

from __future__ import annotations

import ast
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import core.tools._bash_environment as bash_environment
import core.tools._terminal_input as terminal_input
import core.tools.terminal_manager as terminal_module
from core.tools.terminal_manager import TerminalManager, TerminalStaleScreenError
from tests.core.tools.terminal_manager_helpers import (
    AdapterFactory,
    eventually,
    owner,
    session_of,
    spawn,
)
from tests.core.tools.terminal_manager_helpers import quick_readiness as quick_readiness
from tests.core.tools.terminal_manager_helpers import shell_environment as shell_environment
from tests.core.tools.terminal_manager_helpers import terminal_manager as terminal_manager


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

    def launch(argv, cwd, env, rows, columns):  # type: ignore[no-untyped-def]
        calls.append(dict(env))
        if len(calls) == 1:
            raise FileNotFoundError("test-owned absent executable")
        return factory(argv, cwd, env, rows, columns)

    monkeypatch.setattr(bash_environment, "_probe_shell_env", probe)
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


@pytest.mark.asyncio
@pytest.mark.usefixtures("quick_readiness")
async def test_start_input_waits_for_a_settled_screen_and_answers_terminal_queries_meanwhile(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path, initial_text="agent task")
    assert session.state == "starting"

    factory.adapters[0].emit("\x1b[6n")
    await eventually(lambda: factory.adapters[0].writes == ["\x1b[1;1R"])
    assert session.initial_input_task is not None
    assert not session.initial_input_task.done()
    factory.adapters[0].emit("READY> ")
    await eventually(lambda: factory.adapters[0].writes == ["\x1b[1;1R", "agent task", "\r"])
    assert session.state == "working"


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


@pytest.fixture
def host_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda env: ["host-shell"])


@pytest.mark.asyncio
@pytest.mark.usefixtures("host_shell", "quick_readiness")
async def test_manual_command_is_typed_into_the_default_shell_with_quoted_arguments(
    tmp_path: Path,
) -> None:
    factory = AdapterFactory()
    # The default quiet period keeps the shell busy with its prompt output while
    # the command is typed.
    manager = TerminalManager(adapter_factory=factory, sweep_interval_seconds=3600)
    manager.start()
    try:
        result = await manager.spawn_for_operator(
            command="codex", arguments=["--profile", "work space"], cwd=tmp_path
        )
        session = session_of(manager, result["terminal_id"])

        assert session.owner is None
        assert (session.command, session.arguments) == ("host-shell", ())
        assert (session.launch_command, session.launch_arguments) == (
            "codex",
            ("--profile", "work space"),
        )
        assert (result["command"], result["launch_command"], result["launch_args"]) == (
            "host-shell",
            "codex",
            ["--profile", "work space"],
        )
        factory.adapters[0].emit("PS C:\\work> ")
        await eventually(
            lambda: factory.adapters[0].writes == ["codex --profile 'work space'", "\r"]
        )
    finally:
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "ready_timeout", "shell_ends"),
    [(None, 0.01, False), ("PS C:\\work> ", 0.25, False), (None, None, True)],
    ids=["no-prompt", "prompt-not-quiet-long-enough", "shell-exits"],
)
@pytest.mark.usefixtures("host_shell")
async def test_manual_command_is_not_written_without_a_ready_shell(
    terminal_manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prompt: str | None,
    ready_timeout: float | None,
    shell_ends: bool,
) -> None:
    # A prompt must stay unchanged for a whole minute before the command is typed.
    monkeypatch.setattr(terminal_input, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 60)
    if ready_timeout is not None:
        monkeypatch.setattr(
            terminal_input, "TERMINAL_OPERATOR_READY_TIMEOUT_SECONDS", ready_timeout
        )
    manager, factory = terminal_manager
    result = await manager.spawn_for_operator(command="codex", arguments=[], cwd=tmp_path)
    session = session_of(manager, result["terminal_id"])
    command_task = session.operator_command_task
    assert command_task is not None
    if prompt is not None:
        factory.adapters[0].emit(prompt)
    if shell_ends:
        factory.adapters[0].finish(1)
        await eventually(lambda: session.state == "exited")

    await eventually(command_task.done)
    assert factory.adapters[0].writes == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("host_shell", "quick_readiness")
async def test_manual_command_survives_early_operator_input(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    """Operator input queues behind the launch command instead of cancelling it.

    The WebUI takes control right after a manual start, so the first typed
    characters can arrive while the shell is still booting.
    """
    manager, factory = terminal_manager
    result = await manager.spawn_for_operator(command="opencode2", arguments=[], cwd=tmp_path)
    session = session_of(manager, result["terminal_id"])
    assert session.operator_command_task is not None
    assert not session.operator_command_task.done()

    factory.adapters[0].emit("PS C:\\work> ")
    await manager.send_operator_input(session.terminal_id, "x")

    await eventually(lambda: factory.adapters[0].writes == ["opencode2", "\r", "x"])
    assert session.operator_command_task.done()


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
    assert terminal_input._screen_has_prompt_marker("viro@mac project % ") is True
    assert terminal_input._screen_has_prompt_marker("% ") is True
    assert terminal_input._screen_has_prompt_marker("Downloading 50 %") is False
    assert terminal_input._screen_has_prompt_marker("") is False
    assert terminal_input._screen_has_prompt_marker("hello world") is False
    assert terminal_input._screen_has_prompt_marker("PS") is False
