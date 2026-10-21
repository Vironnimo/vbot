"""What an integrated shell in a Terminal Session reports about its prompt and folder.

vBot's shell integration (``_terminal_launch``) marks each prompt (OSC 133 A),
the start of each command line where the shell can (OSC 133 C), and the
shell's working folder before each prompt (OSC 9;9 on Windows shells, OSC 7
on POSIX shells; VS Code's OSC 633 P ``Cwd=`` is read too). Permission Rules
check a line typed at such a prompt with that folder (``_terminal_permissions``).
Only standard terminal sequences are read; output that imitates them, such as
a background job printing escape sequences, is read the same way.
"""

from __future__ import annotations

import re
import socket
import sys
from typing import Final
from urllib.parse import unquote, urlsplit

# Longest OSC payload kept; a longer one is not a mark vBot's integration writes.
_PAYLOAD_CHARS: Final = 4096
_OSC: Final = re.compile(rf"\x1b\]([^\x07\x1b]{{0,{_PAYLOAD_CHARS}}})(?:\x07|\x1b\\)")


class ShellState:
    """The prompt and folder an integrated shell reported, fed with its raw output.

    ``at_prompt``: the last mark was a prompt and no command line started
    since; a shell that has not shown its first prompt yet counts as at it,
    since what is typed then reaches that prompt. ``prompt_count`` counts
    prompts, so a line typed at one prompt is not mistaken for one typed at the
    next. ``cwd`` is the folder the shell reported last (at first the folder it
    started in), or None when it reported one vBot cannot read.
    """

    def __init__(self, cwd: str | None = None) -> None:
        self.at_prompt = True
        self.prompt_count = 0
        self.cwd = cwd
        # What the Agent typed at prompt ``_typed_prompt`` (the shell holds it
        # while that prompt lasts), and what it typed after the last line it
        # submitted there (the shell reads it at a later prompt, up to
        # ``_carry_until``: each submitted line normally ends at one prompt).
        self._typed = ""
        self._typed_prompt = -1
        self._carry = ""
        self._carry_until = -1
        self._pending = ""

    def line(self) -> str:
        """What the shell holds at the current prompt from the Agent's typing."""
        if self._typed_prompt == self.prompt_count:
            return self._typed
        if self._typed_prompt < self.prompt_count <= self._carry_until:
            return self._carry
        return ""

    def typed(self, prompt: int, line: str, *, carry: str = "", submitted: int = 0) -> None:
        """At *prompt* the shell holds *line*; after it ran the *submitted* lines,
        a later prompt gets *carry*."""
        self._typed, self._typed_prompt = line, prompt
        self._carry, self._carry_until = carry, prompt + submitted

    def forget(self) -> None:
        """What the shell holds is unknown, as after input vBot did not follow."""
        self._typed_prompt = self._carry_until = -1

    def feed(self, text: str) -> None:
        text = self._pending + text
        self._pending = ""
        end = 0
        for match in _OSC.finditer(text):
            self._mark(match.group(1))
            end = match.end()
        start = text.rfind("\x1b]", end)
        if start == -1 and text.endswith("\x1b"):
            start = len(text) - 1
        if start != -1 and len(text) - start <= _PAYLOAD_CHARS + 4:
            # A sequence the next chunk completes.
            self._pending = text[start:]

    def line_submitted(self, prompt_count: int) -> None:
        """A line typed at prompt *prompt_count* was submitted: the shell runs it."""
        if self.prompt_count == prompt_count:
            self.at_prompt = False

    def _mark(self, payload: str) -> None:
        head, _, rest = payload.partition(";")
        if head in {"133", "633"}:
            mark, _, value = rest.partition(";")
            if mark == "A":
                self.at_prompt = True
                self.prompt_count += 1
            elif mark == "C":
                self.at_prompt = False
            elif head == "633" and mark == "P" and value.startswith("Cwd="):
                self.cwd = _vscode_value(value[4:]) or None
        elif head == "7":
            self.cwd = _file_url_path(rest)
        elif head == "9" and rest.startswith("9;"):
            folder = rest[2:]
            if len(folder) >= 2 and folder[0] == folder[-1] == '"':
                folder = folder[1:-1]
            self.cwd = folder or None


def _file_url_path(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme != "file":
        return None
    host = parts.hostname or ""
    if host not in ("", "localhost") and host.casefold() != socket.gethostname().casefold():
        # A shell on another machine (ssh) reports a folder of that machine.
        return None
    path = unquote(parts.path)
    if sys.platform == "win32" and re.match(r"/[A-Za-z]:", path):
        path = path[1:]
    return path or None


def _vscode_value(value: str) -> str:
    return re.sub(
        r"\\(\\|x([0-9A-Fa-f]{2}))",
        lambda match: "\\" if match.group(1) == "\\" else chr(int(match.group(2), 16)),
        value,
    )


__all__ = ["ShellState"]
