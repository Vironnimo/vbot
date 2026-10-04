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
   decides otherwise.
3. **Terminal.** ``TERM``: every shell runs in a real terminal.
4. **Unattended defaults** (commands only). No editor, pager or credential
   prompt can be answered, so each fails or passes through at once.
5. **Agent variables and granted credentials** (commands only).
6. **Run identity** (commands only): who runs the command, and the one-time
   update handoff. Applied last, so nothing above replaces it.

The environment is built fresh for every start; building it costs about a
millisecond, so no cache can go stale.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from core.utils.logging import get_logger

_LOGGER = get_logger("tools.shell_environment")

RUN_AGENT_ID_ENV = "VBOT_RUN_AGENT_ID"
RUN_SESSION_ID_ENV = "VBOT_RUN_SESSION_ID"
RUN_PROJECT_ID_ENV = "VBOT_RUN_PROJECT_ID"
UPDATE_HANDOFF_ENV = "VBOT_UPDATE_HANDOFF"

# Variables that identify this vBot instance; passed on when the server has them.
_INSTANCE_VARIABLES = ("VBOT_DATA_DIR", "VBOT_SERVER_PORT", "VBOT_INSTALL_ROOT")
# Variables vBot's own Python process carries and a user's shell must not.
_PROCESS_VARIABLES = frozenset({"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"})

# Git runs its editor through ``sh -c '<editor> "$@"'``: the message goes to
# stderr and ``false`` takes the file name and fails.
_NO_EDITOR = (
    "echo 'No editor is available in this terminal: pass the text as an argument, "
    "for example git commit -m.' >&2; false"
)
_UNATTENDED_DEFAULTS: dict[str, str] = {
    "GIT_EDITOR": _NO_EDITOR,
    "GIT_SEQUENCE_EDITOR": _NO_EDITOR,
    "GIT_MERGE_AUTOEDIT": "no",
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
}
_TERM = "xterm-256color"


@dataclass(frozen=True)
class RunIdentity:
    """Who runs a command: exposed to it as ``VBOT_RUN_*`` variables."""

    agent_id: str
    session_id: str
    project_id: str | None
    update_handoff: str | None = None


def terminal_environment(overlay: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment of an interactive Terminal Session, with *overlay* applied last."""
    environment = _login_environment()
    _apply(environment, _instance_variables())
    _apply(environment, {"TERM": _TERM})
    _apply(environment, overlay or {})
    return environment


def command_environment(
    run: RunIdentity,
    *,
    variables: Mapping[str, str] | None = None,
    credentials: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Environment of one shell command run by an Agent."""
    environment = terminal_environment()
    _apply(environment, _UNATTENDED_DEFAULTS)
    _apply(environment, variables or {})
    _apply(environment, credentials or {})
    _remove(environment, (RUN_PROJECT_ID_ENV, UPDATE_HANDOFF_ENV))
    run_variables = {RUN_AGENT_ID_ENV: run.agent_id, RUN_SESSION_ID_ENV: run.session_id}
    if run.project_id is not None:
        run_variables[RUN_PROJECT_ID_ENV] = run.project_id
    if run.update_handoff is not None:
        run_variables[UPDATE_HANDOFF_ENV] = run.update_handoff
    _apply(environment, run_variables)
    return environment


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
    "UPDATE_HANDOFF_ENV",
    "RunIdentity",
    "command_environment",
    "inherited_value",
    "terminal_environment",
]
