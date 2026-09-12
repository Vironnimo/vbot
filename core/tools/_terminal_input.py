"""Terminal input encoding and interactive shell command readiness."""

from __future__ import annotations

import asyncio
import contextlib
import re
import shlex
import subprocess
import time
from collections.abc import Sequence

from ._terminal_state import (
    TERMINAL_BRACKETED_PASTE_END,
    TERMINAL_BRACKETED_PASTE_START,
    TERMINAL_INITIAL_INPUT_QUIET_SECONDS,
    TERMINAL_INPUT_KEY_SEQUENCES,
    TERMINAL_INPUT_MAX_CHARS,
    TERMINAL_OPERATOR_READY_TIMEOUT_SECONDS,
    TerminalSession,
)


def _input_chunks(
    *,
    data: str | None,
    text: str | None,
    key: str | None,
    bracketed_paste: bool = False,
) -> tuple[str, ...]:
    if data == "":
        data = None
    if text == "":
        text = None
    if key == "":
        key = None
    if data is not None:
        if text is not None or key is not None:
            raise ValueError("data cannot be combined with text or key")
        if not data:
            raise ValueError("data must be a non-empty string")
        if len(data) > TERMINAL_INPUT_MAX_CHARS:
            raise ValueError(f"data must not exceed {TERMINAL_INPUT_MAX_CHARS} characters")
        return (data,)
    if key is not None and key not in TERMINAL_INPUT_KEY_SEQUENCES:
        raise ValueError(f"Unsupported terminal key: {key}")
    chunks: list[str] = []
    if text:
        if len(text) > TERMINAL_INPUT_MAX_CHARS:
            raise ValueError(f"text must not exceed {TERMINAL_INPUT_MAX_CHARS} characters")
        chunks.append(
            f"{TERMINAL_BRACKETED_PASTE_START}{text}{TERMINAL_BRACKETED_PASTE_END}"
            if bracketed_paste
            else text
        )
    if key is not None:
        chunks.append(TERMINAL_INPUT_KEY_SEQUENCES[key])
    return tuple(chunks)


def _shell_command(command: str | None, arguments: Sequence[str], *, shell: str) -> str | None:
    """Render one operator-requested command as shell input, or None."""
    if command is None:
        return None
    if not command.strip() or any(not argument for argument in arguments):
        return None
    shell_name = shell.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if shell_name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        # Windows PowerShell reparses native arguments through the Windows
        # command-line grammar. Modern PowerShell passes the string values.
        native_arguments = (
            [subprocess.list2cmdline([value]) for value in arguments]
            if shell_name in {"powershell", "powershell.exe"}
            else arguments
        )
        tokens = [_powershell_word(command), *map(_powershell_word, native_arguments)]
        prefix = "& " if tokens[0].startswith("'") else ""
        return prefix + " ".join(tokens)
    if shell_name in {"cmd", "cmd.exe"}:
        # Keep the executable's quotes for cmd's command lookup; escape the
        # argument command-line syntax before cmd hands it to the native child.
        return " ".join(
            [
                subprocess.list2cmdline([command]),
                *(_CMD_META.sub(r"^\1", subprocess.list2cmdline([value])) for value in arguments),
            ]
        )
    return " ".join(shlex.quote(token) for token in [command, *arguments])


def _powershell_word(value: str) -> str:
    if _POWERSHELL_SAFE_WORD.fullmatch(value):
        return value
    return "'" + value.replace("'", "''") + "'"


_POWERSHELL_SAFE_WORD = re.compile(r"[A-Za-z0-9_./\\:\-]+")


_CMD_META = re.compile(r'([()%!^"<>&|])')


async def _await_shell_ready(session: TerminalSession) -> bool:
    """Wait for a stable interactive shell prompt screen.

    Returns False when the terminal ends or no prompt can be established
    within the bounded window; the operator command is then never written.
    """
    deadline = time.monotonic() + TERMINAL_OPERATOR_READY_TIMEOUT_SECONDS
    last_revision = -1
    quiet_since: float | None = None
    while True:
        if time.monotonic() >= deadline:
            return False
        async with session.lock:
            if session.state in {"exited", "error"}:
                return False
            revision_now = session.renderer.revision
            text = session.renderer.screen_text()
            has_prompt = bool(text) and _screen_has_prompt_marker(text)
            if has_prompt and revision_now != last_revision:
                last_revision = revision_now
                quiet_since = time.monotonic()
            elif has_prompt and quiet_since is not None:
                if time.monotonic() - quiet_since >= TERMINAL_INITIAL_INPUT_QUIET_SECONDS:
                    break
            elif not has_prompt:
                if revision_now != last_revision:
                    last_revision = revision_now
                quiet_since = None
        session.output_event.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(session.output_event.wait(), timeout=0.1)
    return True


_SHELL_PROMPT_MARKERS = ("$ ", "# ", "> ", "PS ")


_SHELL_BARE_PROMPT_MARKERS = frozenset(("$", "#", ">", "❯", "➜", "❄", "λ"))


def _screen_has_prompt_marker(text: str) -> bool:
    """Detect a rendered shell prompt in one screen snapshot."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    if any(line.startswith(marker) for line in lines for marker in _SHELL_PROMPT_MARKERS):
        return True
    if any(line in _SHELL_BARE_PROMPT_MARKERS for line in lines):
        return True
    return _append_known_prompt_marker(lines[-1])


_KNOWN_PROMPT_PATTERNS = (
    r"[A-Za-z0-9_\-\./\\~]+:\s*$",  # pwsh "PS C:\work> " last line after the chevron
    r"[A-Za-z0-9_\-\./\\~]+(>|#|\$)\s*$",  # cmd "C:\work>", sh "user@host:~/x$"
)


def _append_known_prompt_marker(line: str) -> bool:
    return any(re.search(pattern, line) for pattern in _KNOWN_PROMPT_PATTERNS)
