"""Interactive shells start with shell integration; manual launch commands run inside
them through each shell's own start options."""

from __future__ import annotations

import ast
import base64
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.tools._terminal_launch as terminal_launch
from core.tools._terminal_launch import (
    ShellLaunch,
    integrated_shell_kind,
    interactive_shell,
    shell_launch,
    starts_shell_prompt,
)
from core.tools._terminal_shell_state import ShellState
from core.tools.terminal_backend import (
    TerminalAdapter,
    spawn_terminal_adapter,
    windows_command_processor_line,
    windows_program,
)
from core.tools.terminal_emulator import TerminalEmulator

# Values each shell's own syntax would otherwise interpret.
_SPECIAL = ['a "b" c', "100%", "x&y|z", "it's"]


@pytest.fixture
def posix_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start shells as on Linux or macOS, whatever the test host is."""
    monkeypatch.setattr(terminal_launch, "os", SimpleNamespace(name="posix"))


@pytest.mark.parametrize(
    ("shell", "command", "line"),
    [
        (
            "pwsh.exe",
            "C:\\Program Files\\Codex\\codex.exe",
            r"""& 'C:\Program Files\Codex\codex.exe' 'a "b" c' '100%' 'x&y|z' 'it''s'""",
        ),
        # Windows PowerShell rebuilds the program's command line from the string
        # values without escaping their quotes, so each value is pre-quoted.
        (
            "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
            "codex",
            r"""codex '"a \"b\" c"' '100%' 'x&y|z' 'it''s'""",
        ),
    ],
    ids=["pwsh", "windows-powershell"],
)
def test_powershell_runs_the_command_last_in_an_encoded_script(
    shell: str, command: str, line: str
) -> None:
    # An empty PATH: the command is no batch program of this host.
    launch = shell_launch([shell, "-NoLogo"], command, _SPECIAL, environment={"PATH": ""})

    assert launch.argv[:4] == [shell, "-NoLogo", "-NoExit", "-EncodedCommand"]
    script = _powershell_script(launch)
    assert script[-1] == line
    # The command also lands in the shell's history, so Up repeats it.
    assert _history_entry(line) in script[-2]
    assert (launch.command_line, launch.environment, launch.scratch) == (None, {}, None)


@pytest.mark.skipif(os.name != "nt", reason="Batch programs exist only on Windows")
def test_powershell_runs_a_batch_program_through_cmd_when_an_argument_needs_protection(
    tmp_path: Path,
) -> None:
    """PowerShell hands a batch program (such as an npm program shim) quotes and
    cmd metacharacters unprotected, so cmd runs it with the exact command line."""
    shim = tmp_path / "codex.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    environment = {"PATH": str(tmp_path), "COMSPEC": "C:\\Windows\\system32\\cmd.exe"}

    unsafe = shell_launch(["pwsh.exe"], "codex", _SPECIAL, environment=environment)
    plain = shell_launch(
        ["pwsh.exe"], "codex", ["--profile", "work space"], environment=environment
    )

    def through_cmd(program: str) -> str:
        exact = windows_command_processor_line(program, _SPECIAL)
        return f"& 'C:\\Windows\\system32\\cmd.exe' --% /d /e:on /v:off /s /c \"{exact}\""

    script = _powershell_script(unsafe)
    program = windows_program("codex", str(tmp_path))
    assert Path(program) == shim
    assert script[-1] == through_cmd(program)
    # The history keeps the line as the operator would type it in PowerShell.
    assert _history_entry(r"""codex 'a "b" c' '100%' 'x&y|z' 'it''s'""") in script[-2]
    assert _powershell_script(plain)[-1] == "codex --profile 'work space'"
    # Windows drops trailing dots and spaces from a program name: such a name
    # starts the same batch program, so it gets the same protection.
    for name in ("codex.cmd.", "codex.cmd..", "codex.cmd. "):
        dotted = shell_launch(["pwsh.exe"], str(tmp_path / name), _SPECIAL, environment=environment)
        assert _powershell_script(dotted)[-1] == through_cmd(str(shim))


def _powershell_script(launch: ShellLaunch) -> list[str]:
    return base64.b64decode(launch.argv[-1]).decode("utf-16-le").splitlines()


def _history_entry(line: str) -> str:
    return "AddToHistory('" + line.replace("'", "''") + "')"


def test_cmd_runs_an_exact_command_line_with_escaped_metacharacters() -> None:
    launch = shell_launch(["cmd.exe"], "codex", _SPECIAL, environment={})

    assert launch.argv == ["cmd.exe"]
    # An argument cmd could act on is quoted even without whitespace, since a
    # batch program hands it to cmd once more; a percent sign stands between quotes.
    assert launch.command_line == r'''/d /s /k "codex "a ""b"" c" "100"%"" "x&y|z" "it's""'''
    # The shell integration is the prompt: its folder and mark, then the default prompt.
    assert (launch.environment, launch.scratch) == (
        {"PROMPT": r"$e]9;9;$P$e\$e]133;A$e\$P$G"},
        None,
    )


@pytest.mark.usefixtures("posix_host")
def test_bash_rcfile_loads_the_users_bashrc_then_runs_the_command() -> None:
    launch = shell_launch(["/bin/bash"], "codex", ["--profile", "work space"], environment={})
    scratch = launch.scratch
    assert scratch is not None
    try:
        rcfile = scratch / "bashrc"
        assert launch.argv == ["/bin/bash", "--rcfile", str(rcfile), "-i"]
        assert (launch.command_line, launch.environment) == (None, {})
        removal, user_rc, *integration, history, run = rcfile.read_text(
            encoding="utf-8"
        ).splitlines()
        # The shell removes its private files first, then starts like any interactive
        # bash and adds its marks to the user's prompt.
        assert shlex.split(removal) == ["command", "rm", "-rf", "--", str(scratch)]
        assert user_rc == 'if [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi'
        assert any(row.startswith("PROMPT_COMMAND=") for row in integration)
        line = "codex --profile 'work space'"
        assert shlex.split(history) == ["history", "-s", "--", line]
        assert run == line
    finally:
        launch.remove_scratch()
    assert not scratch.exists()


@pytest.mark.usefixtures("posix_host")
@pytest.mark.parametrize(
    ("user_dotdir", "restore"),
    [(None, 'ZDOTDIR="$HOME"'), ("/home/user/.config/zsh", "ZDOTDIR=/home/user/.config/zsh")],
    ids=["home", "user-zdotdir"],
)
def test_zsh_dotdir_loads_the_users_files_then_runs_the_command(
    user_dotdir: str | None, restore: str
) -> None:
    environment = {} if user_dotdir is None else {"ZDOTDIR": user_dotdir}
    launch = shell_launch(
        ["/bin/zsh"], "codex", ["--profile", "work space"], environment=environment
    )
    scratch = launch.scratch
    assert scratch is not None
    try:
        assert launch.argv == ["/bin/zsh"]
        assert (launch.command_line, launch.environment) == (None, {"ZDOTDIR": str(scratch)})
        zshenv = (scratch / ".zshenv").read_text(encoding="utf-8").splitlines()
        assert restore in zshenv
        assert '[[ -f "$ZDOTDIR/.zshenv" ]] && source "$ZDOTDIR/.zshenv"' in zshenv
        *setup, history, run = (scratch / ".zshrc").read_text(encoding="utf-8").splitlines()
        assert 'command rm -rf -- "$_vbot_launch_dotdir"' in setup
        # The prompt marks are hooked in after the user's files.
        user_rc = setup.index('[[ -f "$ZDOTDIR/.zshrc" ]] && source "$ZDOTDIR/.zshrc"')
        assert setup.index("add-zsh-hook precmd __vbot_prompt_mark") > user_rc
        line = "codex --profile 'work space'"
        assert shlex.split(history) == ["print", "-s", "-r", "--", line]
        assert run == line
    finally:
        launch.remove_scratch()


@pytest.mark.parametrize(
    ("os_name", "shell_argv", "argv", "command_line"),
    [
        (
            "posix",
            ["/usr/bin/fish"],
            [
                "/usr/bin/fish",
                "-C",
                terminal_launch._FISH_INTEGRATION + r"""; codex --profile 'work space' 'it\'s'""",
            ],
            None,
        ),
        (
            "posix",
            ["/bin/ksh", "-l"],
            [
                "/bin/sh",
                "-c",
                """trap : INT; codex --profile 'work space' 'it'"'"'s'; exec /bin/ksh -l""",
            ],
            None,
        ),
        (
            "nt",
            ["C:\\tools\\nu.exe"],
            ["C:\\Windows\\system32\\cmd.exe"],
            r'''/d /e:on /v:off /s /c "codex --profile "work space" "it's" & C:\tools\nu.exe"''',
        ),
    ],
    ids=["fish", "other-posix-shell", "other-windows-shell"],
)
def test_other_shells_run_the_command_before_the_interactive_shell(
    monkeypatch: pytest.MonkeyPatch,
    os_name: str,
    shell_argv: list[str],
    argv: list[str],
    command_line: str | None,
) -> None:
    monkeypatch.setattr(terminal_launch, "os", SimpleNamespace(name=os_name))

    launch = shell_launch(
        shell_argv,
        "codex",
        ["--profile", "work space", "it's"],
        environment={"COMSPEC": "C:\\Windows\\system32\\cmd.exe"},
    )

    assert (launch.argv, launch.command_line) == (argv, command_line)
    assert (launch.environment, launch.scratch) == ({}, None)


@pytest.mark.parametrize(
    ("command", "arguments"), [("  ", []), ("codex", [""])], ids=["command", "argument"]
)
def test_an_empty_command_or_argument_is_rejected(command: str, arguments: list[str]) -> None:
    with pytest.raises(ValueError):
        shell_launch(["pwsh.exe"], command, arguments, environment={})


@pytest.mark.parametrize(
    ("os_name", "argv", "integrated", "kind", "prompt"),
    [
        (
            "nt",
            ["pwsh.exe", "-NoLogo"],
            ["pwsh.exe", "-NoLogo", "-NoExit", "-EncodedCommand"],
            "powershell",
            True,
        ),
        (
            "nt",
            ["pwsh.exe", "-NoExit"],
            ["pwsh.exe", "-NoExit", "-EncodedCommand"],
            "powershell",
            True,
        ),
        ("nt", ["cmd.exe", "/d"], ["cmd.exe", "/d"], "cmd", True),
        # fish lines are not analyzed (Permission Rules): no kind.
        ("posix", ["/usr/bin/fish", "-l"], ["/usr/bin/fish", "-l", "-C"], None, True),
        ("posix", ["/bin/bash"], ["/bin/bash", "--rcfile"], "bash", True),
        ("posix", ["/bin/zsh", "-i"], ["/bin/zsh", "-i"], "bash", True),
        # Options beyond plain interactive flags: the shell starts as given.
        ("nt", ["pwsh.exe", "-Command", "Get-Date"], None, None, False),
        ("nt", ["cmd.exe", "/c", "dir"], None, None, False),
        ("posix", ["/bin/bash", "--norc"], None, None, False),
        # bash on Windows (Git Bash, MSYS) and programs other than shells.
        ("nt", ["C:\\Program Files\\Git\\bin\\bash.exe"], None, None, True),
        ("posix", ["/bin/sh"], None, None, True),
        ("posix", ["python3"], None, None, False),
    ],
)
def test_only_a_plain_interactive_shell_starts_with_shell_integration(
    monkeypatch: pytest.MonkeyPatch,
    os_name: str,
    argv: list[str],
    integrated: list[str] | None,
    kind: str | None,
    prompt: bool,
) -> None:
    monkeypatch.setattr(terminal_launch, "os", SimpleNamespace(name=os_name))

    launch = interactive_shell(argv, environment={"PROMPT": "$N$G"})

    # What Permission Rules check in such a terminal (``_terminal_permissions``).
    assert integrated_shell_kind(argv) == kind
    assert starts_shell_prompt(argv) is prompt
    if integrated is None:
        assert launch is None
        return
    assert launch is not None
    try:
        assert launch.argv[: len(integrated)] == integrated
        if Path(argv[0]).name == "cmd.exe":
            # cmd keeps the user's own prompt after the marks.
            assert launch.environment == {"PROMPT": r"$e]9;9;$P$e\$e]133;A$e\$N$G"}
    finally:
        launch.remove_scratch()


# Real shell startup can exceed 10 seconds on shared Windows CI runners.
@pytest.mark.timeout(120)
@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh.exe", "cmd.exe", "bash", "zsh", "fish"])
def test_real_integrated_shell_declares_its_prompt_and_command_start(
    shell: str, tmp_path: Path
) -> None:
    """In a terminal, an integrated shell marks every prompt and, except cmd, the
    start of every command line it runs; the emulator reads them as signals. Before
    each prompt it reports its folder, which Permission Rules check lines in."""
    executable = _shell_available(shell)
    environment = {key: value for key, value in os.environ.items() if key != "ZDOTDIR"}
    launch = interactive_shell([executable], environment=environment)
    assert launch is not None
    adapter = spawn_terminal_adapter(
        launch.argv,
        tmp_path,
        {**environment, **launch.environment},
        24,
        80,
        command_line=launch.command_line,
    )
    emulator = TerminalEmulator(80, 24, scrollback_lines=100)
    state = ShellState()
    try:
        _read_signals_until_prompt(adapter, emulator, state)
        folder = tmp_path / "next"
        folder.mkdir()
        adapter.write(f"cd {folder.name}\r")
        signals = _read_signals_until_prompt(
            adapter, emulator, state, after_command=shell != "cmd.exe"
        )
    finally:
        adapter.terminate()
        adapter.close()
        launch.remove_scratch()

    assert signals[-1] == "prompt"
    if shell != "cmd.exe":
        assert "command" in signals
    assert state.at_prompt
    assert state.cwd is not None and Path(state.cwd).resolve() == folder.resolve()


def _read_signals_until_prompt(
    adapter: TerminalAdapter,
    emulator: TerminalEmulator,
    state: ShellState,
    *,
    after_command: bool = False,
) -> list[str]:
    """Read through the next prompt, after a command start when requested."""
    signals: list[str] = []
    deadline = time.monotonic() + 60
    # Shell integration and the line editor can announce the same prompt in
    # separate reads. The second startup mark is not the command's next prompt.
    while not signals or signals[-1] != "prompt" or (after_command and "command" not in signals):
        assert time.monotonic() < deadline, f"no prompt mark; signals {signals}"
        try:
            chunk = adapter.read(4096)
        except TimeoutError:
            continue
        update = emulator.feed(chunk)
        state.feed(chunk)
        if update.responses:
            # ConPTY holds output until its device query is answered.
            adapter.write(update.responses)
        signals.extend(signal for signal in update.signals if signal in {"command", "prompt"})
    return signals


def _run(
    launch: ShellLaunch, environment: dict[str, str], private: Path
) -> subprocess.CompletedProcess[str]:
    """Run a launch outside a PTY and end the interactive shell it leaves open."""
    argv = list(launch.argv)
    environment = {**environment, **launch.environment}
    if "-NoExit" in argv:
        # PowerShell without a console runs the script, then ends. It never
        # starts its line editor; any history would still land in *private*,
        # never in the user's own.
        index = argv.index("-NoExit")
        argv[index : index + 1] = ["-NoProfile", "-NonInteractive"]
        environment["APPDATA"] = str(private)
        environment["LOCALAPPDATA"] = str(private)
    elif Path(argv[0]).name == "zsh":
        # zsh reads its .zshrc only as an interactive shell, as in the PTY.
        argv.append("-i")
    command: str | list[str] = argv
    if launch.command_line is not None:
        command = subprocess.list2cmdline(argv) + " " + launch.command_line
    return subprocess.run(
        command,
        input="exit\n",
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        # POSIX: an interactive shell takes over the controlling terminal's
        # foreground; its own session leaves the test runner untouched.
        start_new_session=True,
    )


def _shell_available(shell: str) -> str:
    executable = shutil.which(shell)
    if executable is None or (os.name == "nt") != shell.endswith(".exe"):
        pytest.skip("Shell is not available for this platform")
    return executable


# Real shell startup can exceed 10 seconds on shared Windows CI runners.
@pytest.mark.timeout(120)
@pytest.mark.parametrize(
    "shell", ["powershell.exe", "pwsh.exe", "cmd.exe", "bash", "zsh", "fish", "sh"]
)
def test_real_shell_passes_the_exact_arguments_to_the_program(shell: str, tmp_path: Path) -> None:
    executable = _shell_available(shell)
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
    environment = {key: value for key, value in os.environ.items() if key != "ZDOTDIR"}
    environment["VBOT_TERMINAL_TEST_VALUE"] = "MUST_NOT_EXPAND"
    launch = shell_launch(
        [executable],
        sys.executable,
        ["-c", "import sys; print(repr(sys.argv[1:]))", *arguments],
        environment=environment,
    )
    try:
        result = _run(launch, environment, tmp_path)
    finally:
        launch.remove_scratch()

    printed = [row for row in result.stdout.splitlines() if row.startswith("[")]
    assert printed, result.stdout + result.stderr
    assert ast.literal_eval(printed[0]) == arguments


# Real shell startup can exceed 10 seconds on shared Windows CI runners.
@pytest.mark.timeout(120)
@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh.exe", "cmd.exe"])
def test_real_windows_shell_runs_a_program_path_with_spaces(shell: str, tmp_path: Path) -> None:
    executable = _shell_available(shell)
    program = shutil.which("pwsh.exe")
    if program is None or " " not in program:
        pytest.skip("A Windows executable with a spaced path is required")
    launch = shell_launch([executable], program, ["-NoProfile", "-Version"], environment={})

    result = _run(launch, dict(os.environ), tmp_path)

    assert any(row.startswith("PowerShell ") for row in result.stdout.splitlines()), (
        result.stdout + result.stderr
    )
