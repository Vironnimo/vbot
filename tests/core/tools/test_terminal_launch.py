"""Manual launch commands start inside the interactive shell through its own start options."""

from __future__ import annotations

import ast
import base64
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.tools._terminal_launch as terminal_launch
from core.tools._terminal_launch import ShellLaunch, shell_launch
from core.tools.terminal_backend import windows_command_processor_line

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
    assert _history_entry(line) in script[0]
    assert (launch.command_line, launch.environment, launch.scratch) == (None, {}, None)


@pytest.mark.skipif(os.name != "nt", reason="Batch programs exist only on Windows")
def test_powershell_runs_a_batch_program_through_cmd_when_an_argument_needs_protection(
    tmp_path: Path,
) -> None:
    """PowerShell hands a batch program (such as an npm program shim) quotes and
    cmd metacharacters unprotected, so cmd runs it with the exact command line."""
    (tmp_path / "codex.cmd").write_text("@echo off\n", encoding="utf-8")
    environment = {"PATH": str(tmp_path), "COMSPEC": "C:\\Windows\\system32\\cmd.exe"}

    unsafe = shell_launch(["pwsh.exe"], "codex", _SPECIAL, environment=environment)
    plain = shell_launch(
        ["pwsh.exe"], "codex", ["--profile", "work space"], environment=environment
    )

    script = _powershell_script(unsafe)
    exact = windows_command_processor_line("codex", _SPECIAL)
    assert script[-1] == f"& 'C:\\Windows\\system32\\cmd.exe' --% /d /s /c \"{exact}\""
    # The history keeps the line as the operator would type it in PowerShell.
    assert _history_entry(r"""codex 'a "b" c' '100%' 'x&y|z' 'it''s'""") in script[0]
    assert _powershell_script(plain)[-1] == "codex --profile 'work space'"


def _powershell_script(launch: ShellLaunch) -> list[str]:
    return base64.b64decode(launch.argv[-1]).decode("utf-16-le").splitlines()


def _history_entry(line: str) -> str:
    return "AddToHistory('" + line.replace("'", "''") + "')"


def test_cmd_runs_an_exact_command_line_with_escaped_metacharacters() -> None:
    launch = shell_launch(["cmd.exe"], "codex", _SPECIAL, environment={})

    assert launch.argv == ["cmd.exe"]
    # An argument cmd could act on is quoted even without whitespace, since a
    # batch program hands it to cmd once more.
    assert launch.command_line == r'''/d /s /k "codex ^"a \^"b\^" c^" 100^% ^"x^&y^|z^" it's"'''
    assert (launch.environment, launch.scratch) == ({}, None)


@pytest.mark.usefixtures("posix_host")
def test_bash_rcfile_loads_the_users_bashrc_then_runs_the_command() -> None:
    launch = shell_launch(["/bin/bash"], "codex", ["--profile", "work space"], environment={})
    scratch = launch.scratch
    assert scratch is not None
    try:
        rcfile = scratch / "bashrc"
        assert launch.argv == ["/bin/bash", "--rcfile", str(rcfile), "-i"]
        assert (launch.command_line, launch.environment) == (None, {})
        removal, user_rc, history, run = rcfile.read_text(encoding="utf-8").splitlines()
        # The shell removes its private files first, then starts like any interactive bash.
        assert shlex.split(removal) == ["command", "rm", "-rf", "--", str(scratch)]
        assert user_rc == 'if [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi'
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
        assert setup[-1] == '[[ -f "$ZDOTDIR/.zshrc" ]] && source "$ZDOTDIR/.zshrc"'
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
            ["/usr/bin/fish", "-C", r"""codex --profile 'work space' 'it\'s'"""],
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
            r'''/d /s /c "codex --profile ^"work space^" it's & C:\tools\nu.exe"''',
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
