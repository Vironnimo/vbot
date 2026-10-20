"""One keepassxc-cli call: windowless, bounded by a timeout, its input on stdin.

The master password and new entry passwords travel only on stdin, never in the
argument list, which other processes on the host can read. A failed or timed-out
call raises without chaining the ``subprocess`` exception, whose captured output
could hold a value.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from core.utils.processes import subprocess_creation_flags


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)


class CommandTimeoutError(Exception):
    """The call did not finish in time; the process was killed."""


class CommandNotStartedError(Exception):
    """The program could not be started (missing, not executable, refused)."""


class CommandRunner(Protocol):
    def __call__(self, argv: Sequence[str], *, stdin: str, timeout: float) -> CommandResult: ...


def run_command(argv: Sequence[str], *, stdin: str, timeout: float) -> CommandResult:
    """Run ``argv`` with ``stdin`` as UTF-8 input and return its decoded output."""
    try:
        completed = subprocess.run(
            list(argv),
            input=stdin.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=subprocess_creation_flags(),
        )
    except subprocess.TimeoutExpired:
        raise CommandTimeoutError from None
    except OSError as error:
        raise CommandNotStartedError(error.strerror or type(error).__name__) from None
    return CommandResult(completed.returncode, _decode(completed.stdout), _decode(completed.stderr))


def _decode(data: bytes | None) -> str:
    # keepassxc-cli writes UTF-8 on every platform; Windows adds CRLF line ends.
    return (data or b"").decode("utf-8", errors="replace").replace("\r\n", "\n")
