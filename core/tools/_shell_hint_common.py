"""Shared failure facts, host mode and bounded output scanning."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from core.permissions import PermissionBoundary

# The shell Tool runs commands in PowerShell on Windows and in bash elsewhere.
_POWERSHELL = sys.platform == "win32"
# Bounded scan windows at both ends: error headers open short output and end
# long output; the middle is noise.
_SCAN_CHARS = 4000

# Registry names of the Tools hints can point to.
_SEARCH_TOOL = "search_files"
_READ_TOOL = "read"
_EDIT_TOOLS = ("apply_patch", "edit")
_TERMINAL_TOOL = "terminal"
HINT_TOOL_NAMES = (_SEARCH_TOOL, _READ_TOOL, *_EDIT_TOOLS, _TERMINAL_TOOL)


def _offers_none(_name: str) -> bool:
    return False


@dataclass(frozen=True)
class _Failure:
    command: str
    output: str
    workdir: Path | None
    offers: Callable[[str], bool]
    # Where the output reports the failure: from the last Python traceback's
    # exception line on, else all of it.
    error_start: int = 0
    # The Session's boundary: a hint names only paths it may read.
    permissions: PermissionBoundary = field(kw_only=True)

    @property
    def error(self) -> str:
        """The output from where it reports the failure on."""
        return self.output[self.error_start :]

    def edit_tool(self) -> str | None:
        return next((name for name in _EDIT_TOOLS if self.offers(name)), None)


# A Python traceback header at the start of a line, and the line that ends the
# traceback: the first one after it that is not indented, the exception.
_TRACEBACK_HEADER = re.compile(r"^Traceback \(most recent call last\):", re.M)
_UNINDENTED_LINE = re.compile(r"^\S", re.M)


def _error_start(output: str) -> int:
    """Where *output* reports the failure: the last Python traceback's exception line.

    A probe printed "[Errno 13] Permission denied" as data and then failed with a
    TypeError; the permission hint answered the data. Without a complete
    traceback, all of the output counts.
    """
    headers = [header.end() for header in _TRACEBACK_HEADER.finditer(output)]
    if not headers:
        return 0
    exception = _UNINDENTED_LINE.search(output, headers[-1])
    return exception.start() if exception is not None else 0


# Exit-code-only hints for codes POSIX shells give a fixed meaning; on Windows
# they arrive through bash, wsl, docker or ssh.
_EXIT_CODE_HINTS: dict[int, str] = {
    126: "Exit code 126: the file was found but is not executable. Make it executable with "
    "`chmod +x`, or run it through its interpreter, such as `bash script.sh`.",
    137: "Exit code 137: the process was killed with SIGKILL, for example by the kernel when "
    "memory ran out. Check `dmesg | tail` on the machine that ran it for an out-of-memory "
    "entry before running it again.",
}
# The `timeout` program exits with 124 when its time limit stops the command.
_TIMEOUT_PROGRAM = re.compile(r"(?:^|[\s;|&(])timeout\s", re.M)
_TIMEOUT_PROGRAM_HINT = (
    "Exit code 124: the `timeout` program in the command stopped it at its time limit. "
    "If the command needs more time, give `timeout` a larger limit."
)
_BASH_SYNTAX_HINT = (
    "This is bash syntax: in PowerShell write `if (cond) { ... }`, "
    "`foreach ($f in Get-ChildItem *.md) { ... }`, and `Test-Path` for file tests."
)


_ESCAPED_QUOTE_FIX = (
    "Put the argument in single quotes (double quotes inside stay literal), or pass longer "
    "code through a here-string piped to the program, such as `@'...'@ | python -`."
)


# Windows has no /dev/null: PowerShell resolves it as \dev\null on the current
# drive, so a redirection or a cmdlet reports the missing C:\dev\null, quoted in
# its path error, and a program reports /dev/null as it was given, as a missing
# file. Output that merely names /dev/null is no failure about it: a diff
# header for an added file reads `--- /dev/null`.
_DEV_NULL = re.compile(r"[/\\]dev[/\\]null", re.IGNORECASE)
_DEV_NULL_RESOLVED = re.compile(
    r"\b[A-Za-z]:\\dev\\null\b|['\"][^'\"\r\n]*\\dev\\null['\"]", re.IGNORECASE
)
_DEV_NULL_MISSING = re.compile(
    r"No such file or directory\W+[/\\]dev[/\\]null\b"
    r"|[/\\]dev[/\\]null\W+No such file or directory",
    re.IGNORECASE,
)
_DEV_NULL_HINT = (
    "Windows has no /dev/null: PowerShell and programs read it as the path \\dev\\null on the "
    "current drive. Discard output with `2>$null`, `*>$null` or `| Out-Null`, and give a "
    "program `NUL` as its output file, such as `curl -o NUL`."
)


def _scan_window(output: str) -> str:
    """The first and the last ``_SCAN_CHARS`` characters of *output*."""
    if len(output) > 2 * _SCAN_CHARS:
        return f"{output[:_SCAN_CHARS]}\n{output[-_SCAN_CHARS:]}"
    return output
