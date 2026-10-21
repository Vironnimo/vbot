"""One 1Password CLI (``op``) call: windowless, bounded by a timeout, no input.

The service account token reaches ``op`` only through the environment of its
process (``OP_SERVICE_ACCOUNT_TOKEN``), never through the argument list, which
other processes on the host can read. A failed or timed-out call raises without
chaining the ``subprocess`` exception, whose captured output could hold a value.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
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
    def __call__(
        self, argv: Sequence[str], *, env: Mapping[str, str], timeout: float
    ) -> CommandResult: ...


def run_command(argv: Sequence[str], *, env: Mapping[str, str], timeout: float) -> CommandResult:
    """Run ``argv`` in exactly ``env`` with closed input and return its decoded output."""
    try:
        completed = subprocess.run(
            list(argv),
            input=b"",
            capture_output=True,
            timeout=timeout,
            check=False,
            env=dict(env),
            creationflags=subprocess_creation_flags(),
        )
    except subprocess.TimeoutExpired:
        raise CommandTimeoutError from None
    except OSError as error:
        raise CommandNotStartedError(error.strerror or type(error).__name__) from None
    return CommandResult(completed.returncode, _decode(completed.stdout), _decode(completed.stderr))


def _decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace").replace("\r\n", "\n")
