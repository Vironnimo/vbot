"""Start an operator-requested program inside the interactive shell.

A manual Terminal with a launch command must behave like a normal terminal:
the shell starts as usual (profile, rc files, PATH), runs the program, and
stays open with its prompt after the program ends or is interrupted. Each
shell gets the program through its own start options instead of typed input,
so nothing depends on recognizing a prompt or on the timing of shell start:

- PowerShell: ``-NoExit -EncodedCommand`` (the profile loads first).
- cmd: ``/d /s /k "<line>"`` as an exact Windows command line.
- bash: ``--rcfile`` with a file that loads ``~/.bashrc`` first.
- zsh: a ``ZDOTDIR`` whose ``.zshenv``/``.zshrc`` load the user's files first.
- fish: ``-C`` (runs after ``config.fish``).
- other shells: ``/bin/sh`` runs the program, then starts the shell.

The rc files live in a private temporary directory that the shell removes
while loading them; the Session removes it too when it ends.
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from core.tools.terminal_backend import windows_command_processor_line

_POWERSHELL_SHELLS = frozenset({"pwsh", "powershell"})
_POSIX_RC_SHELLS = frozenset({"bash", "zsh"})


@dataclass(frozen=True, slots=True)
class ShellLaunch:
    """The process start that runs a program inside an interactive shell."""

    argv: list[str]
    # Windows: the exact command line after the executable, for a shell
    # (cmd) that does not parse its arguments with the C runtime rules.
    command_line: str | None = None
    environment: dict[str, str] = field(default_factory=dict)
    # Private files the shell reads at start; removed when the Session ends.
    scratch: Path | None = None

    def remove_scratch(self) -> None:
        if self.scratch is not None:
            shutil.rmtree(self.scratch, ignore_errors=True)


def shell_launch(
    shell_argv: Sequence[str],
    command: str,
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
) -> ShellLaunch:
    """Return the start of *shell_argv* that runs ``[command, *arguments]`` first."""
    if not command.strip() or any(not argument for argument in arguments):
        raise ValueError("A launch command and its arguments must not be empty")
    shell = shell_argv[0]
    name = _shell_name(shell)
    if name in _POWERSHELL_SHELLS:
        line = _powershell_line(command, arguments, windows_powershell=name == "powershell")
        run = line
        if _batch_program(command, environment) and any(
            _BATCH_UNSAFE.search(argument) for argument in arguments
        ):
            # PowerShell hands a batch file (such as an npm program shim)
            # quotes and cmd metacharacters unprotected, so cmd runs it with
            # the exact command line instead; the history keeps the line.
            comspec = environment.get("COMSPEC") or "cmd.exe"
            exact = windows_command_processor_line(command, arguments)
            run = f'& {_powershell_literal(comspec)} --% /d /s /c "{exact}"'
        # PSReadLine loads its history at the first prompt and drops what was
        # added before, so the line joins the history once the prompt idles.
        script = (
            "$null = Register-EngineEvent -SourceIdentifier PowerShell.OnIdle "
            "-MaxTriggerCount 1 -Action { try { "
            "[Microsoft.PowerShell.PSConsoleReadLine]::AddToHistory("
            f"{_powershell_literal(line)}) }} catch {{ }} }}\n{run}"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return ShellLaunch([*shell_argv, "-NoExit", "-EncodedCommand", encoded])
    if name == "cmd":
        line = windows_command_processor_line(command, arguments)
        return ShellLaunch(list(shell_argv), command_line=f'/d /s /k "{line}"')
    if os.name == "nt":
        comspec = environment.get("COMSPEC") or "cmd.exe"
        line = windows_command_processor_line(command, arguments)
        shell_line = windows_command_processor_line(shell, shell_argv[1:])
        return ShellLaunch([comspec], command_line=f'/d /s /c "{line} & {shell_line}"')
    line = " ".join(shlex.quote(token) for token in (command, *arguments))
    if name == "fish":
        return ShellLaunch([*shell_argv, "-C", " ".join(map(_fish_word, (command, *arguments)))])
    if name in _POSIX_RC_SHELLS:
        scratch = Path(tempfile.mkdtemp(prefix="vbot-terminal-"))
        try:
            if name == "bash":
                return _bash_launch(shell_argv, line, scratch)
            return _zsh_launch(shell_argv, line, scratch, environment)
        except BaseException:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
    # The interrupt trap keeps sh alive when Ctrl-C stops the program, so
    # the interactive shell still starts afterwards.
    shell_words = " ".join(shlex.quote(word) for word in shell_argv)
    return ShellLaunch(["/bin/sh", "-c", f"trap : INT; {line}; exec {shell_words}"])


def _bash_launch(shell_argv: Sequence[str], line: str, scratch: Path) -> ShellLaunch:
    rcfile = scratch / "bashrc"
    # bash reads the whole rc file before running it, so it can remove it first.
    rcfile.write_text(
        f"command rm -rf -- {shlex.quote(str(scratch))}\n"
        'if [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi\n'
        f"history -s -- {shlex.quote(line)}\n"
        f"{line}\n",
        encoding="utf-8",
        newline="\n",
    )
    # bash accepts long options only before its single-letter ones.
    return ShellLaunch(
        [shell_argv[0], "--rcfile", str(rcfile), *shell_argv[1:], "-i"], scratch=scratch
    )


def _zsh_launch(
    shell_argv: Sequence[str], line: str, scratch: Path, environment: Mapping[str, str]
) -> ShellLaunch:
    user_dotdir = environment.get("ZDOTDIR")
    restore = f"ZDOTDIR={shlex.quote(user_dotdir)}" if user_dotdir else 'ZDOTDIR="$HOME"'
    (scratch / ".zshenv").write_text(
        f"_vbot_launch_dotdir=$ZDOTDIR\n{restore}\n"
        '[[ -f "$ZDOTDIR/.zshenv" ]] && source "$ZDOTDIR/.zshenv"\n'
        "_vbot_user_dotdir=$ZDOTDIR\nZDOTDIR=$_vbot_launch_dotdir\n",
        encoding="utf-8",
        newline="\n",
    )
    (scratch / ".zshrc").write_text(
        'ZDOTDIR=$_vbot_user_dotdir\ncommand rm -rf -- "$_vbot_launch_dotdir"\n'
        "unset _vbot_launch_dotdir _vbot_user_dotdir\n"
        '[[ -f "$ZDOTDIR/.zshrc" ]] && source "$ZDOTDIR/.zshrc"\n'
        f"print -s -r -- {shlex.quote(line)}\n"
        f"{line}\n",
        encoding="utf-8",
        newline="\n",
    )
    return ShellLaunch(list(shell_argv), environment={"ZDOTDIR": str(scratch)}, scratch=scratch)


def _batch_program(command: str, environment: Mapping[str, str]) -> bool:
    if os.name != "nt":
        return False
    program = shutil.which(command, path=environment.get("PATH"))
    return program is not None and Path(program).suffix.lower() in {".bat", ".cmd"}


# What PowerShell passes to a batch file without the protection cmd needs.
_BATCH_UNSAFE = re.compile(r'["()^<>&|]')


def _shell_name(shell: str) -> str:
    name = shell.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.removesuffix(".exe")


def _powershell_line(command: str, arguments: Sequence[str], *, windows_powershell: bool) -> str:
    # Windows PowerShell reparses native arguments through the Windows
    # command-line grammar. Modern PowerShell passes the string values.
    native_arguments = (
        [subprocess.list2cmdline([value]) for value in arguments]
        if windows_powershell
        else arguments
    )
    tokens = [_powershell_word(command), *map(_powershell_word, native_arguments)]
    prefix = "& " if tokens[0].startswith("'") else ""
    return prefix + " ".join(tokens)


def _powershell_word(value: str) -> str:
    if _POWERSHELL_SAFE_WORD.fullmatch(value):
        return value
    return _powershell_literal(value)


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_POWERSHELL_SAFE_WORD = re.compile(r"[A-Za-z0-9_./\\:\-]+")


def _fish_word(value: str) -> str:
    if value and re.fullmatch(r"[A-Za-z0-9_./:=@+,-]+", value):
        return value
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
