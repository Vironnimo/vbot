"""The environment of every shell vBot starts: commands and Terminal Sessions.

One owner builds it from explicit layers, each applied over the previous one:

1. **Login base.** Windows: a fresh environment block of the server's user, as
   a new login would get it (registry ``PATH`` included, so programs installed
   after the server started are found). POSIX: the server's environment.
   Either way without what vBot's own process added (virtual environment,
   Python search paths, vBot variables).
2. **This vBot instance.** ``VBOT_DATA_DIR``, ``VBOT_SERVER_PORT`` and
   ``VBOT_INSTALL_ROOT`` exactly when the server itself has them, so a ``vbot``
   command reaches the instance it runs under, and a worktree's own marker still
   decides otherwise. A server that runs from a source checkout also leads
   ``PATH`` with its own ``vbot`` command (``provide_instance_command``): the
   ``vbot`` an installation put on ``PATH`` always reaches the installed
   instance.
3. **Output.** ``TERM``: a Terminal Session runs in a real terminal
   (``xterm-256color``); a command writes to a pipe that no one answers or
   renders (``dumb``).
4. **Unattended defaults** (commands only). No editor, pager or credential
   prompt can be answered, so each fails or passes through at once. Programs
   write plain text without colors, and Python writes each line as it prints it
   and reads and writes files as UTF-8 unless the login base sets its own mode.
5. **Agent variables and granted credentials** (commands only).
6. **Session scratch** (Agent processes): temporary files and the own Scratch
   folder, applied after explicit variables and credentials.
7. **Run identity** (commands only): who runs the command, and the one-time
   update handoff. Applied last, so nothing above replaces it.

A Session with Permission Rules gets a confined environment instead of layer 7:
without the instance variables of layer 2, the Run identity and this server's
own ``vbot`` command on ``PATH`` (``VBOT_SCRATCH_DIR`` stays), so a command
cannot find or address the vBot server through its environment; and without
the credentials vBot knows (``Runtime.environment_credential_names``) that the
login base carries, such as a Provider key the server was started with, so a
command gets a credential only as a granted one (layer 5).

A command's environment comes in two parts (``CommandEnvironment``): layers 1
to 4 are its shell's start environment, the same for every call until the login
environment or the instance changes, so a shell started before the call can
serve it; layers 5 to 7 are the call's own, applied by the shell before the
command runs. A command vBot runs outside a Run, such as a Cron watch check,
gets layers 1 to 4 only (``unattended_command_environment``).

The environment is built fresh for every start; building it costs about a
millisecond, so no cache can go stale.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Container, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.tools.scratch_areas import ScratchFolder

_LOGGER = get_logger("tools.shell_environment")

RUN_AGENT_ID_ENV = "VBOT_RUN_AGENT_ID"
RUN_SESSION_ID_ENV = "VBOT_RUN_SESSION_ID"
RUN_PROJECT_ID_ENV = "VBOT_RUN_PROJECT_ID"
UPDATE_HANDOFF_ENV = "VBOT_UPDATE_HANDOFF"
SCRATCH_DIR_ENV = "VBOT_SCRATCH_DIR"

# Variables that identify this vBot instance; passed on when the server has them.
_INSTANCE_VARIABLES = ("VBOT_DATA_DIR", "VBOT_SERVER_PORT", "VBOT_INSTALL_ROOT")
# The checkout this code runs from; an installed payload has no ``.git``.
_CHECKOUT = Path(__file__).resolve().parents[2]
# The directory of this server's own ``vbot`` command, set once at server start.
_instance_command_directory: Path | None = None
# Variables vBot's own Python process carries and a user's shell must not.
_PROCESS_VARIABLES = frozenset({"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"})

# Git runs its editor through ``sh -c '<editor> "$@"'`` with the file to edit, so
# each value ends in a function call that takes the file name; only sh builtins.
#
# The message editor accepts a message git prepared, as closing an editor without
# changes would: git rebase/merge/cherry-pick/revert --continue, git revert and
# git commit --amend then work as typed. A message of comments only (git commit or
# git tag -a without -m) fails with the fix; lines below the scissors line of
# ``commit -v`` are no message.
_MESSAGE_EDITOR = (
    '_vbot_editor() { while IFS= read -r line || [ -n "$line" ]; do case $line in '
    "'# '*'>8'*) break ;; '#'*) ;; *[![:space:]]*) return 0 ;; esac; done < \"$1\"; "
    "echo 'No editor is available for this command: pass the message as an argument, "
    'for example git commit -m "Fix the parser".\' >&2; return 1; }; _vbot_editor'
)
# The todo list of git rebase -i is never accepted unchanged: the Agent asked to
# edit it, and running it as prepared would report a rebase that changed nothing.
_SEQUENCE_ASSIGNMENT = (
    '$env:GIT_SEQUENCE_EDITOR = ":"' if sys.platform == "win32" else "GIT_SEQUENCE_EDITOR=:"
)
_SEQUENCE_EDITOR = (
    "echo 'No editor is available for the git rebase -i todo list. To run "
    f"the list as git prepared it, for example with --autosquash, set {_SEQUENCE_ASSIGNMENT} "
    "for the git command.' >&2; false"
)
_UNATTENDED_DEFAULTS: dict[str, str] = {
    "GIT_EDITOR": _MESSAGE_EDITOR,
    "GIT_SEQUENCE_EDITOR": _SEQUENCE_EDITOR,
    "GIT_MERGE_AUTOEDIT": "no",
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "GIT_PAGER": "cat",
    "GH_PAGER": "cat",
    "PAGER": "cat",
    "NO_COLOR": "1",
    # Python writes UTF-8 when its output is piped or captured, which is how the
    # shell decodes it; otherwise non-ASCII text arrives garbled.
    "PYTHONIOENCODING": "utf-8",
    # On a pipe Python would hold its output until the buffer fills or it exits;
    # line by line, the live log and the progress view stay current.
    "PYTHONUNBUFFERED": "1",
}
# Defaults that give way to a value the login base sets.
_UNATTENDED_FALLBACKS: dict[str, str] = {
    # Python's UTF-8 mode: open() reads and writes UTF-8, as the Agent's files
    # are, instead of the Windows code page; a script that read a UTF-8 file as
    # cp1252 garbled every non-ASCII character without an error.
    "PYTHONUTF8": "1",
}
_TERMINAL_TERM = "xterm-256color"
_COMMAND_TERM = "dumb"


@dataclass(frozen=True)
class RunIdentity:
    """Who runs a command: exposed to it as ``VBOT_RUN_*`` variables."""

    agent_id: str
    session_id: str
    project_id: str | None
    update_handoff: str | None = None


def terminal_environment(
    overlay: Mapping[str, str] | None = None,
    *,
    confined: bool = False,
    credential_names: Container[str] = frozenset(),
) -> dict[str, str]:
    """Environment of an interactive Terminal Session, with *overlay* applied last.

    *confined* and *credential_names* (a Session with Permission Rules) as for
    ``command_environment``.
    """
    environment = _instance_environment()
    _apply(environment, {"TERM": _TERMINAL_TERM})
    if confined:
        _withhold(environment, credential_names)
    _apply(environment, overlay or {})
    if confined:
        _confine(environment)
    return environment


def scratch_environment(folder: ScratchFolder) -> dict[str, str]:
    """The Session's temporary directories, already ensured by ``ScratchAreas``."""
    return {
        "TEMP": str(folder.tmp),
        "TMP": str(folder.tmp),
        "TMPDIR": str(folder.tmp),
        SCRATCH_DIR_ENV: str(folder.own),
    }


@dataclass(frozen=True)
class CommandEnvironment:
    """One command's environment: its shell's start environment and the call's layer.

    ``base`` holds layers 1 to 4. ``changes`` is what layers 5 to 7 make of it:
    a value sets the variable, None removes it.
    """

    base: dict[str, str]
    changes: dict[str, str | None]

    def complete(self) -> dict[str, str]:
        """The environment the command runs with."""
        environment = dict(self.base)
        for name, value in self.changes.items():
            if value is None:
                _remove(environment, (name,))
            else:
                _apply(environment, {name: value})
        return environment


def command_environment(
    run: RunIdentity | None,
    *,
    variables: Mapping[str, str] | None = None,
    credentials: Mapping[str, str] | None = None,
    scratch: ScratchFolder | None = None,
    confined: bool = False,
    credential_names: Container[str] = frozenset(),
) -> CommandEnvironment:
    """Environment of one shell command run by an Agent.

    *confined* (a Session with Permission Rules) leaves out what exists only to
    reach or control the vBot server: the instance variables, the Run identity
    and this server's own ``vbot`` command on ``PATH``; ``VBOT_SCRATCH_DIR``
    stays. It also withholds the login base's variables named in
    *credential_names*, the names vBot knows as credentials; *variables* and
    *credentials* apply after that, so a granted credential still arrives.
    *run* is None only for a confined command.
    """
    base = _command_base()
    environment = dict(base)
    if confined:
        _withhold(environment, credential_names)
    _apply(environment, variables or {})
    _apply(environment, credentials or {})
    if scratch is not None:
        _apply(environment, scratch_environment(scratch))
    _remove(environment, (RUN_PROJECT_ID_ENV, UPDATE_HANDOFF_ENV))
    if confined:
        _confine(environment)
    elif run is not None:
        run_variables = {RUN_AGENT_ID_ENV: run.agent_id, RUN_SESSION_ID_ENV: run.session_id}
        if run.project_id is not None:
            run_variables[RUN_PROJECT_ID_ENV] = run.project_id
        if run.update_handoff is not None:
            run_variables[UPDATE_HANDOFF_ENV] = run.update_handoff
        _apply(environment, run_variables)
    changes: dict[str, str | None] = {
        name: value for name, value in environment.items() if base.get(name) != value
    }
    changes.update({name: None for name in base if name not in environment})
    return CommandEnvironment(base, changes)


def unattended_command_environment(
    *, confined: bool = False, credential_names: Container[str] = frozenset()
) -> CommandEnvironment:
    """Environment of a command vBot runs outside a Run, such as a Cron watch check.

    Layers 1 to 4: no Run identity, Session scratch, variables or credentials.
    *confined* and *credential_names* (a job with a permission ceiling) as for
    ``command_environment``.
    """
    base = _command_base()
    if not confined:
        return CommandEnvironment(base, {})
    environment = dict(base)
    _withhold(environment, credential_names)
    _confine(environment)
    changes: dict[str, str | None] = {
        name: value for name, value in environment.items() if base.get(name) != value
    }
    changes.update({name: None for name in base if name not in environment})
    return CommandEnvironment(base, changes)


def _withhold(environment: dict[str, str], credential_names: Container[str]) -> None:
    """Remove the credentials vBot knows from a confined environment's login base."""
    _remove(environment, tuple(name for name in environment if name in credential_names))


def _confine(environment: dict[str, str]) -> None:
    """Remove what only reaches or controls the vBot server (Permission Rules)."""
    _remove(
        environment,
        (
            *_INSTANCE_VARIABLES,
            RUN_AGENT_ID_ENV,
            RUN_SESSION_ID_ENV,
            RUN_PROJECT_ID_ENV,
            UPDATE_HANDOFF_ENV,
        ),
    )
    directory = _instance_command_directory
    path = _get(environment, "PATH")
    if directory is not None and path:
        _apply(
            environment,
            {
                "PATH": os.pathsep.join(
                    entry
                    for entry in path.split(os.pathsep)
                    if entry and not _same_directory(entry, directory)
                )
            },
        )


def _command_base() -> dict[str, str]:
    """A command shell's start environment (layers 1 to 4)."""
    base = _instance_environment()
    _apply(base, {"TERM": _COMMAND_TERM})
    _apply(base, _UNATTENDED_DEFAULTS)
    _apply(
        base,
        {name: value for name, value in _UNATTENDED_FALLBACKS.items() if _get(base, name) is None},
    )
    _remove(base, (RUN_PROJECT_ID_ENV, UPDATE_HANDOFF_ENV))
    return base


def _instance_environment() -> dict[str, str]:
    """The login base with this vBot instance's variables (layers 1 and 2)."""
    environment = _login_environment()
    _apply(environment, _instance_variables())
    if _instance_command_directory is not None:
        path = _get(environment, "PATH")
        leading = str(_instance_command_directory)
        _apply(environment, {"PATH": f"{leading}{os.pathsep}{path}" if path else leading})
    return environment


def provide_instance_command(
    directory: Path,
    data_dir: Path,
    port: int,
    *,
    checkout: Path = _CHECKOUT,
    interpreter: str = sys.executable,
) -> Path | None:
    """Write a ``vbot`` command for this server and lead every shell's PATH with it.

    Only for a server that runs from a source checkout: an installed server's shells
    keep the installation's own ``vbot``. The command runs the checkout's CLI with
    this server's data directory and port, so a ``vbot`` call in a Run reaches the
    server that runs it. Returns the directory, or None for an installed server.
    """
    global _instance_command_directory
    cli = checkout / "cli" / "main.py"
    installed = os.environ.get("VBOT_INSTALL_ROOT") or not (checkout / ".git").exists()
    if installed or not cli.is_file():
        return None
    directory.mkdir(parents=True, exist_ok=True)
    data, python, script = str(data_dir), str(interpreter), str(cli)
    if os.name == "nt":
        # PowerShell runs vbot.ps1 in preference to vbot.cmd and passes each argument
        # unchanged; cmd.exe and programs that start vbot through cmd use the .cmd.
        (directory / "vbot.ps1").write_text(
            _powershell_command(data, port, python, script), encoding="utf-8"
        )
        (directory / "vbot.cmd").write_text(
            _cmd_command(data, port, python, script), encoding="utf-8", newline="\r\n"
        )
        # Git Bash finds a file without extension before an installation's vbot.exe.
        python, script = Path(python).as_posix(), Path(script).as_posix()
    sh = directory / "vbot"
    sh.write_text(
        "#!/bin/sh\n"
        f"VBOT_DATA_DIR={_sh_quote(data)} VBOT_SERVER_PORT={port} "
        f'exec {_sh_quote(python)} {_sh_quote(script)} "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    sh.chmod(0o755)
    _instance_command_directory = directory
    return directory


def _powershell_command(data: str, port: int, python: str, script: str) -> str:
    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    call = f"& {quote(python)} {quote(script)} @args"
    # $env: belongs to the whole shell: the previous values come back after the call.
    return (
        "$saved = $env:VBOT_DATA_DIR, $env:VBOT_SERVER_PORT\n"
        f"$env:VBOT_DATA_DIR = {quote(data)}\n"
        f"$env:VBOT_SERVER_PORT = '{port}'\n"
        "try {\n"
        f"    if ($MyInvocation.ExpectingInput) {{ $input | {call} }} else {{ {call} }}\n"
        "} finally {\n"
        "    $env:VBOT_DATA_DIR, $env:VBOT_SERVER_PORT = $saved\n"
        "}\n"
        "exit $LASTEXITCODE\n"
    )


def _cmd_command(data: str, port: int, python: str, script: str) -> str:
    def quote(value: str) -> str:
        return value.replace("%", "%%")

    return (
        "@echo off\n"
        "setlocal\n"
        f'set "VBOT_DATA_DIR={quote(data)}"\n'
        f'set "VBOT_SERVER_PORT={port}"\n'
        f'"{quote(python)}" "{quote(script)}" %*\n'
    )


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def inherited_value(name: str) -> str | None:
    """The value *name* has in a fresh shell, without vBot's own layers."""
    environment = _login_environment()
    return _get(environment, name)


def _login_environment() -> dict[str, str]:
    if sys.platform == "win32":
        try:
            return _windows_login_environment()
        except OSError:
            _LOGGER.warning(
                "Could not create a login environment block; shells get the server's environment",
                exc_info=True,
            )
    return _without_process_variables(os.environ)


def _without_process_variables(source: Mapping[str, str]) -> dict[str, str]:
    environment = {
        key: value
        for key, value in source.items()
        if not key.upper().startswith("VBOT_") and key.upper() not in _PROCESS_VARIABLES
    }
    if sys.prefix != sys.base_prefix:
        # The server runs in a virtual environment whose scripts lead PATH.
        scripts = Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")
        for key in [key for key in environment if key.upper() == "PATH"]:
            environment[key] = os.pathsep.join(
                entry
                for entry in environment[key].split(os.pathsep)
                if entry and not _same_directory(entry, scripts)
            )
    return environment


def _same_directory(entry: str, directory: Path) -> bool:
    try:
        return Path(entry).resolve() == directory.resolve()
    except OSError:
        return False


def _instance_variables() -> dict[str, str]:
    return {name: os.environ[name] for name in _INSTANCE_VARIABLES if os.environ.get(name)}


# Windows names are case-insensitive: one key per name, keeping the existing spelling
# (upper case for every variable of the login base).


def _apply(environment: dict[str, str], values: Mapping[str, str]) -> None:
    for name, value in values.items():
        existing = _key(environment, name)
        environment[existing if existing is not None else name] = value


def _remove(environment: dict[str, str], names: tuple[str, ...]) -> None:
    for name in names:
        existing = _key(environment, name)
        if existing is not None:
            del environment[existing]


def _get(environment: Mapping[str, str], name: str) -> str | None:
    existing = _key(environment, name)
    return environment[existing] if existing is not None else None


def _key(environment: Mapping[str, str], name: str) -> str | None:
    if name in environment:
        return name
    if os.name != "nt":
        return None
    folded = name.upper()
    return next((key for key in environment if key.upper() == folded), None)


def _windows_login_environment() -> dict[str, str]:
    import ctypes
    from ctypes import wintypes

    # Windows-only ctypes members, typed loosely so other platforms type-check.
    native = cast(Any, ctypes)
    advapi32 = native.WinDLL("advapi32", use_last_error=True)
    kernel32 = native.WinDLL("kernel32", use_last_error=True)
    userenv = native.WinDLL("userenv", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    userenv.CreateEnvironmentBlock.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.BOOL,
    ]
    userenv.CreateEnvironmentBlock.restype = wintypes.BOOL
    userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
    userenv.DestroyEnvironmentBlock.restype = wintypes.BOOL

    token_query, token_duplicate = 0x0008, 0x0002
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_query | token_duplicate, ctypes.byref(token)
    ):
        raise native.WinError(native.get_last_error())
    try:
        block = ctypes.c_void_p()
        # bInherit FALSE: the user's variables only, not this process's.
        if not userenv.CreateEnvironmentBlock(ctypes.byref(block), token, False):
            raise native.WinError(native.get_last_error())
        try:
            environment: dict[str, str] = {}
            address = block.value or 0
            while True:
                entry = ctypes.wstring_at(address)
                if not entry:
                    break
                address += (len(entry) + 1) * ctypes.sizeof(ctypes.c_wchar)
                name, separator, value = entry.partition("=")
                # Entries such as ``=C:=C:\\`` hold per-drive directories, not
                # variables. Names are upper case, as in ``os.environ`` on Windows.
                if separator and name:
                    environment[name.upper()] = value
            return environment
        finally:
            userenv.DestroyEnvironmentBlock(block)
    finally:
        kernel32.CloseHandle(token)


__all__ = [
    "RUN_AGENT_ID_ENV",
    "RUN_PROJECT_ID_ENV",
    "RUN_SESSION_ID_ENV",
    "SCRATCH_DIR_ENV",
    "UPDATE_HANDOFF_ENV",
    "CommandEnvironment",
    "RunIdentity",
    "command_environment",
    "inherited_value",
    "provide_instance_command",
    "scratch_environment",
    "terminal_environment",
]
