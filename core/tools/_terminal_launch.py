"""Start interactive shells with shell integration, optionally running a program first.

Shell integration makes a shell mark its prompt (OSC 133 A) and, where the
shell can, the start of each command line it runs (OSC 133 C), the standard
marks terminals use to tell a working shell from one waiting for input. Before
each prompt it reports its working folder (OSC 9;9 from PowerShell and cmd,
OSC 633 P ``Cwd=`` from bash, zsh and fish). The Terminal Session reads the
marks as declared signals (``terminal_emulator``) and, for Permission Rules,
the prompt and folder (``_terminal_shell_state``).

A manual Terminal with a launch command must behave like a normal terminal:
the shell starts as usual (profile, rc files, PATH), runs the program, and
stays open with its prompt after the program ends or is interrupted. Each
shell gets the integration and the program through its own start options
instead of typed input, so nothing depends on recognizing a prompt or on the
timing of shell start:

- PowerShell: ``-NoExit -EncodedCommand`` (the profile loads first); the
  integration wraps ``prompt`` and PSReadLine's ``PSConsoleHostReadLine``.
- cmd: ``/d /s /k "<line>"`` as an exact Windows command line; the
  integration is the ``PROMPT`` variable (cmd marks no command start).
- bash: ``--rcfile`` with a file that loads ``~/.bashrc`` first, then adds
  ``PROMPT_COMMAND`` and ``PS0`` marks.
- zsh: a ``ZDOTDIR`` whose ``.zshenv``/``.zshrc`` load the user's files first,
  then add ``precmd``/``preexec`` hooks.
- fish: ``-C`` (runs after ``config.fish``) with ``fish_prompt``/``fish_preexec``
  handlers.
- other shells: ``/bin/sh`` runs the program, then starts the shell, without
  integration.

bash, zsh and fish get the integration on POSIX only. A shell started with
options other than plain interactive flags starts as given. The rc files
live in a private temporary directory that the shell removes while loading
them; the Session removes it too when it ends.
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
from typing import Literal

from core.tools.terminal_backend import (
    WINDOWS_COMMAND_PROCESSOR_OPTIONS,
    is_batch_program,
    windows_command_processor_line,
    windows_program,
)

_POWERSHELL_SHELLS = frozenset({"pwsh", "powershell"})
_POSIX_RC_SHELLS = frozenset({"bash", "zsh"})
# Options that keep a shell interactive and change nothing the integration needs.
_PLAIN_POWERSHELL_OPTIONS = frozenset(
    {
        "-nologo",
        "-nol",
        "-noprofile",
        "-nop",
        "-noexit",
        "-noe",
        "-interactive",
        "-i",
        "-login",
        "-l",
        "-mta",
        "-sta",
        "-noprofileloadtime",
    }
)
_PLAIN_SHELL_OPTIONS = {
    "pwsh": _PLAIN_POWERSHELL_OPTIONS,
    "powershell": _PLAIN_POWERSHELL_OPTIONS,
    "cmd": frozenset({"/d", "/q", "/a", "/u", "/e:on", "/e:off", "/v:on", "/v:off"}),
    "bash": frozenset({"-i"}),
    "zsh": frozenset({"-i"}),
    "fish": frozenset({"-i", "-l", "--login", "--interactive"}),
}

# PowerShell: mark the prompt, and the command line PSReadLine read, keeping
# the status ($?, $LASTEXITCODE) the user's prompt shows. PSReadLine may load
# after this script, so its reader is wrapped at the first prompt.
_POWERSHELL_INTEGRATION = """\
$global:__vbot_prompt = $function:prompt
function global:prompt {
    $vbotFailed = -not $global:?
    $vbotExitCode = $global:LASTEXITCODE
    if (-not $global:__vbot_readline -and (Test-Path Function:\\PSConsoleHostReadLine)) {
        $global:__vbot_readline = $function:PSConsoleHostReadLine
        function global:PSConsoleHostReadLine {
            $line = & $global:__vbot_readline
            [Console]::Write([string][char]27 + ']133;C' + [char]7)
            $line
        }
    }
    $vbotFolder = if ($PWD.Provider.Name -eq 'FileSystem') { $PWD.ProviderPath } else { '' }
    $mark = [string][char]27 + ']9;9;"' + $vbotFolder + '"' + [char]7
    $mark += [string][char]27 + ']133;A' + [char]7
    if (-not $global:__vbot_prompt) { return $mark + 'PS ' + $PWD + '> ' }
    $global:LASTEXITCODE = $vbotExitCode
    if ($vbotFailed) { Write-Error 'failure' -ErrorAction Ignore }
    $mark + ((& $global:__vbot_prompt) -join '')
}
"""
# cmd's PROMPT codes: $e is the escape character.
_CMD_PROMPT_MARK = "$e]9;9;$P$e\\$e]133;A$e\\"
_BASH_INTEGRATION = """\
__vbot_prompt_mark() {
    local __vbot_status=$?
    printf '\\033]633;P;Cwd=%s\\007\\033]133;A\\007' "$PWD"
    return "$__vbot_status"
}
PROMPT_COMMAND="__vbot_prompt_mark${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
PS0="${PS0}"$'\\033]133;C\\007'
"""
_ZSH_INTEGRATION = """\
autoload -Uz add-zsh-hook
__vbot_prompt_mark() { print -n -- $'\\e]633;P;Cwd='"$PWD"$'\\a\\e]133;A\\a' }
__vbot_command_mark() { print -n -- $'\\e]133;C\\a' }
add-zsh-hook precmd __vbot_prompt_mark
add-zsh-hook preexec __vbot_command_mark
"""
_FISH_INTEGRATION = (
    "function __vbot_prompt_mark --on-event fish_prompt; "
    "printf '\\e]633;P;Cwd=%s\\a\\e]133;A\\a' \"$PWD\"; end; "
    "function __vbot_command_mark --on-event fish_preexec; printf '\\e]133;C\\a'; end"
)


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


def interactive_shell(argv: Sequence[str], *, environment: Mapping[str, str]) -> ShellLaunch | None:
    """Return the start of the interactive shell *argv* with shell integration.

    None when *argv* starts another program, or a shell with options beyond
    plain interactive flags: it then starts exactly as given. *environment*
    is the environment the shell starts in.
    """
    if not argv:
        return None
    name = _shell_name(argv[0])
    plain = _PLAIN_SHELL_OPTIONS.get(name)
    if plain is None or any(option.lower() not in plain for option in argv[1:]):
        return None
    if name in _POWERSHELL_SHELLS:
        options = [*argv[1:]]
        if not any(option.lower() in {"-noexit", "-noe"} for option in options):
            options.append("-NoExit")
        script = _encoded(_POWERSHELL_INTEGRATION)
        return ShellLaunch([argv[0], *options, "-EncodedCommand", script])
    if name == "cmd":
        return ShellLaunch(list(argv), environment={"PROMPT": _cmd_prompt(environment)})
    if os.name == "nt":
        return None
    if name == "fish":
        return ShellLaunch([*argv, "-C", _FISH_INTEGRATION])
    scratch = Path(tempfile.mkdtemp(prefix="vbot-terminal-"))
    try:
        if name == "bash":
            return _bash_launch(argv, None, scratch)
        return _zsh_launch(argv, None, scratch, environment)
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise


def integrated_shell_kind(argv: Sequence[str]) -> Literal["powershell", "cmd", "bash"] | None:
    """The command language of *argv* when it starts with vBot's shell integration.

    None for a program, a shell started as given (options beyond plain
    interactive flags, or bash and zsh on Windows), or fish, whose lines
    Permission Rules cannot analyze.
    """
    if not argv:
        return None
    name = _shell_name(argv[0])
    plain = _PLAIN_SHELL_OPTIONS.get(name)
    if plain is None or any(option.lower() not in plain for option in argv[1:]):
        return None
    if name in _POWERSHELL_SHELLS:
        return "powershell"
    if name == "cmd":
        return "cmd"
    if os.name == "nt" or name == "fish":
        return None
    return "bash"


# Shells whose prompt reads command lines; ``starts_shell_prompt``.
_PROMPT_SHELLS = frozenset(
    {"sh", "dash", "ash", "ksh", "mksh", "tcsh", "csh", "nu", "elvish", "xonsh", "wsl"}
)
_PLAIN_OTHER_SHELL_OPTIONS = frozenset({"-i", "-l", "--login", "--interactive"})


def starts_shell_prompt(argv: Sequence[str]) -> bool:
    """Whether *argv* starts a shell that waits at its prompt for command lines.

    True for a known shell with no options beyond plain interactive flags, with
    or without vBot's integration (``integrated_shell_kind``); a shell given a
    command or a script runs that instead.
    """
    if not argv:
        return False
    name = _shell_name(argv[0])
    plain = _PLAIN_SHELL_OPTIONS.get(name)
    if plain is None:
        if name not in _PROMPT_SHELLS:
            return False
        plain = _PLAIN_OTHER_SHELL_OPTIONS
    return all(option.lower() in plain for option in argv[1:])


def shell_launch(
    shell_argv: Sequence[str],
    command: str,
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
) -> ShellLaunch:
    """Return the start of *shell_argv* that runs ``[command, *arguments]`` first,
    with shell integration where ``interactive_shell`` gives it."""
    if not command.strip() or any(not argument for argument in arguments):
        raise ValueError("A launch command and its arguments must not be empty")
    shell = shell_argv[0]
    name = _shell_name(shell)
    if name in _POWERSHELL_SHELLS:
        line = _powershell_line(command, arguments, windows_powershell=name == "powershell")
        run = line
        batch = _batch_program(command, environment)
        if batch is not None and any(_BATCH_UNSAFE.search(argument) for argument in arguments):
            # PowerShell hands a batch file (such as an npm program shim)
            # quotes and cmd metacharacters unprotected, so cmd runs it with
            # the exact command line instead; the history keeps the line.
            comspec = environment.get("COMSPEC") or "cmd.exe"
            exact = windows_command_processor_line(batch, arguments)
            run = (
                f"& {_powershell_literal(comspec)} --% "
                f'{WINDOWS_COMMAND_PROCESSOR_OPTIONS} /s /c "{exact}"'
            )
        # PSReadLine loads its history at the first prompt and drops what was
        # added before, so the line joins the history once the prompt idles.
        script = (
            f"{_POWERSHELL_INTEGRATION}"
            "$null = Register-EngineEvent -SourceIdentifier PowerShell.OnIdle "
            "-MaxTriggerCount 1 -Action { try { "
            "[Microsoft.PowerShell.PSConsoleReadLine]::AddToHistory("
            f"{_powershell_literal(line)}) }} catch {{ }} }}\n{run}"
        )
        return ShellLaunch([*shell_argv, "-NoExit", "-EncodedCommand", _encoded(script)])
    if name == "cmd":
        line = windows_command_processor_line(command, arguments)
        return ShellLaunch(
            list(shell_argv),
            command_line=f'/d /s /k "{line}"',
            environment={"PROMPT": _cmd_prompt(environment)},
        )
    if os.name == "nt":
        comspec = environment.get("COMSPEC") or "cmd.exe"
        line = windows_command_processor_line(command, arguments)
        shell_line = windows_command_processor_line(shell, shell_argv[1:])
        return ShellLaunch(
            [comspec],
            command_line=f'{WINDOWS_COMMAND_PROCESSOR_OPTIONS} /s /c "{line} & {shell_line}"',
        )
    line = " ".join(shlex.quote(token) for token in (command, *arguments))
    if name == "fish":
        program = " ".join(map(_fish_word, (command, *arguments)))
        return ShellLaunch([*shell_argv, "-C", f"{_FISH_INTEGRATION}; {program}"])
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


def _bash_launch(shell_argv: Sequence[str], line: str | None, scratch: Path) -> ShellLaunch:
    rcfile = scratch / "bashrc"
    program = "" if line is None else f"history -s -- {shlex.quote(line)}\n{line}\n"
    # bash reads the whole rc file before running it, so it can remove it first.
    rcfile.write_text(
        f"command rm -rf -- {shlex.quote(str(scratch))}\n"
        'if [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi\n'
        f"{_BASH_INTEGRATION}{program}",
        encoding="utf-8",
        newline="\n",
    )
    # bash accepts long options only before its single-letter ones.
    return ShellLaunch(
        [shell_argv[0], "--rcfile", str(rcfile), *shell_argv[1:], "-i"], scratch=scratch
    )


def _zsh_launch(
    shell_argv: Sequence[str], line: str | None, scratch: Path, environment: Mapping[str, str]
) -> ShellLaunch:
    program = "" if line is None else f"print -s -r -- {shlex.quote(line)}\n{line}\n"
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
        f"{_ZSH_INTEGRATION}{program}",
        encoding="utf-8",
        newline="\n",
    )
    return ShellLaunch(list(shell_argv), environment={"ZDOTDIR": str(scratch)}, scratch=scratch)


def _encoded(script: str) -> str:
    """A PowerShell script as ``-EncodedCommand`` takes it."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _cmd_prompt(environment: Mapping[str, str]) -> str:
    """The user's cmd prompt (``$P$G`` by default) after the prompt mark."""
    return _CMD_PROMPT_MARK + (environment.get("PROMPT") or "$P$G")


def _batch_program(command: str, environment: Mapping[str, str]) -> str | None:
    """The batch program Windows starts for *command*, else None."""
    if os.name != "nt":
        return None
    program = windows_program(command, environment.get("PATH"))
    return program if is_batch_program(program) else None


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
