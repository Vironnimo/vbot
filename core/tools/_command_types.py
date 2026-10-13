"""Public types, limits and errors of the shell's ``CommandManager``.

``command_manager`` re-exports everything here; callers import from there.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from core.runs import RunExecutionOwner
from core.sessions import SessionAddress
from core.utils.errors import VBotError

from .process_tree import ProcessTree, ProcessTreeTracker, RunningProcess

# At most this many commands run at once, server-wide; launches in progress count.
COMMAND_MAX_LIVE = 64
# A command stays queryable as long as a process of it runs, and this long after
# its last process ended.
COMMAND_RETENTION_SECONDS = 30 * 60.0
# How often commands past their retention are forgotten.
COMMAND_SWEEP_INTERVAL_SECONDS = 60.0
# A command belongs to the vBot Session that started it: its lifecycle scope
# and the target of its result delivery.
CommandOwner = SessionAddress
# Temporary-file category of the live log files (``core/storage/temp_files.py``).
COMMAND_TEMPORARY_CATEGORY = "commands"
# The transcript keeps the first whole lines within COMMAND_HEAD_CHARS characters
# and the last whole lines within COMMAND_TAIL_CHARS, lines joined by line breaks:
# what an Agent's view of the output needs (``_shell_results``). The log file has
# every byte.
COMMAND_HEAD_CHARS = 2_000
COMMAND_TAIL_CHARS = 30_000
# The newest lines ``latest_output`` can return.
COMMAND_RECENT_LINES = 200
# ``on_output`` receives this many newest lines at most this often.
COMMAND_PROGRESS_LINES = 40
COMMAND_PROGRESS_SECONDS = 0.5
# After the shell exited, and after its last process ended, output is read for
# at most this long before the command's outcome is fixed.
COMMAND_DRAIN_SECONDS = 1.0
# After the shell exited, processes it left running that print nothing, start
# nothing and use no CPU this long end the command: they wait, like a server.
COMMAND_LEFTOVER_QUIET_SECONDS = 2.0
# A wait that passes tells whether the command's processes were quiet in the same
# sense over its last this many seconds (``idle``). Long enough that work in
# bursts, or a transfer that uses more than 1.5 % of a core (``IDLE_CPU_SECONDS``
# in this window), is not idle; short enough to fit the default wait with room.
COMMAND_IDLE_WINDOW_SECONDS = 10.0
# POSIX: a stopped command gets SIGINT first; what still runs after this long is killed.
COMMAND_STOP_GRACE_SECONDS = 3.0

StopReason = Literal["agent", "parent", "user", "run_cancelled", "shutdown", "scope"]
CommandStatus = Literal["running", "completed", "failed", "stopped"]
# ``idle``: the wait passed while the command's processes were quiet over its end.
CommandWaitOutcome = Literal["ended", "deadline", "idle"]
# The label of a command in lists and deliveries is cut to this many characters.
_LABEL_MAX_CHARS = 120


@dataclass(frozen=True, slots=True)
class CommandHandle:
    """A started command: its id and the process id of its root process.

    The root leads the command's processes: on Windows it is the shell, and
    ``taskkill /PID <pid> /T /F`` stops the shell and its descendants; on POSIX
    it leads the command's session and process group (the shell, or the
    guardian that runs it), so ``kill -- -<pid>`` stops the group.
    """

    command_id: str
    pid: int


@dataclass(frozen=True, slots=True)
class CommandTranscript:
    """The clean output, bounded: its first lines and its last ones.

    ``head`` holds the first whole lines within ``COMMAND_HEAD_CHARS`` characters,
    lines joined by line breaks. A first line longer than that is held there as
    its start, ``head_cut`` characters short, and ``tail`` can hold it as well.
    ``tail`` holds the last whole lines after the head within
    ``COMMAND_TAIL_CHARS``; a last line longer than that is held alone, as its
    end, ``tail_cut`` characters short. The line the command is still writing,
    if any, is the last line.
    """

    head: tuple[str, ...]
    tail: tuple[str, ...]
    total_lines: int
    head_cut: int = 0
    tail_cut: int = 0

    @property
    def omitted_lines(self) -> int:
        """The lines neither ``head`` nor ``tail`` holds, in whole or in part."""
        return max(0, self.total_lines - len(self.head) - len(self.tail))


@dataclass(frozen=True, slots=True)
class CommandReport:
    """What a command did, as of its end or the moment the report was taken."""

    command_id: str
    # The vBot Session the command belongs to: its lifecycle scope and the
    # target of its result delivery.
    owner: CommandOwner
    # The root process (see ``CommandHandle``).
    pid: int
    command: str
    description: str | None
    workdir: Path
    # The command ended: its shell exited - on its own, stopped by vBot or killed
    # from outside - and every process it left running ended or went quiet.
    ended: bool
    # No process of the command runs any longer.
    finished: bool
    # The shell's exit code; None while it runs or when it is unknown.
    exit_code: int | None
    # Why vBot stopped the command before it ended; None when it did not.
    stop_reason: StopReason | None
    transcript: CommandTranscript
    # Every byte the command wrote, unchanged, appended as it arrives; None
    # without temporary-file storage and once writing it failed.
    log_path: Path | None
    # Direct child programs of the shell that exited with a non-zero code before
    # any stop, such as ``git.exe exited with code 128`` (Windows only).
    failed_programs: tuple[str, ...]
    # Processes the command started that still run after it ended; empty before
    # the end and once they ended.
    still_running: tuple[RunningProcess, ...]
    # Moved to the background: listed, with status events.
    background: bool
    # The result is delivered as a new message when the command ends.
    delivers_result: bool
    # From the start until the command ended, or until the report was taken.
    elapsed_seconds: float

    @property
    def status(self) -> CommandStatus:
        """``running``, ``completed`` (exit code 0), ``failed`` (another or an
        unknown exit code) or ``stopped`` (vBot stopped it)."""
        return command_status(
            ended=self.ended, stop_reason=self.stop_reason, exit_code=self.exit_code
        )

    @property
    def label(self) -> str:
        """The description, else the command's first line, at most 120 characters."""
        text = " ".join((self.description or "").split())
        if not text:
            text = next((line.strip() for line in self.command.splitlines() if line.strip()), "")
        if len(text) > _LABEL_MAX_CHARS:
            return text[: _LABEL_MAX_CHARS - 3] + "..."
        return text


def command_status(
    *, ended: bool, stop_reason: StopReason | None, exit_code: int | None
) -> CommandStatus:
    if not ended:
        return "running"
    if stop_reason is not None:
        return "stopped"
    return "completed" if exit_code == 0 else "failed"


# Builds the body of the command's result delivery from its final report.
CommandReportFormatter = Callable[[CommandReport], str]
# Receives the newest clean output lines while the command runs in the foreground.
OutputCallback = Callable[[str], Awaitable[None]]
# Receives the id of a background command whose status may have changed, or
# that was forgotten (its status is then None).
CommandChangedCallback = Callable[[str], None]


class CompletionSink(Protocol):
    """Where background results go: ``TriggerService``'s completion delivery."""

    def submit_completion(
        self,
        agent_id: str,
        session_id: str,
        *,
        notice_id: str,
        origin_run_id: str,
        body: str,
        project_id: str | None = None,
        execution_owner: RunExecutionOwner | None = None,
        wake: bool = True,
    ) -> Awaitable[None]: ...

    def cancel_completion(
        self,
        agent_id: str,
        session_id: str,
        *,
        notice_id: str,
        project_id: str | None = None,
    ) -> bool: ...


class CommandProcess(Protocol):
    """A started command process: its root, its tracked tree and its output pipe."""

    @property
    def pid(self) -> int: ...

    @property
    def tree(self) -> ProcessTree: ...

    async def read(self) -> bytes:
        """The next output bytes; empty once the pipe ended or was closed."""
        ...

    async def wait(self) -> int:
        """The root's exit code, as soon as it exited (not when the pipe ends)."""
        ...

    def interrupt(self) -> bool:
        """Ask the command to stop (POSIX: SIGINT to its process group).

        Returns whether a request was sent; a kill follows when it was not.
        """

    def close_output(self) -> None:
        """Stop reading: the pipe ends after the bytes already received."""

    def close(self) -> None:
        """Release the process and its pipe once nothing of it runs any longer."""


class CommandLauncher(Protocol):
    """Starts command processes (``PipeLauncher``); ``CommandManager`` closes it with itself."""

    async def launch(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        tracker: ProcessTreeTracker,
        request: bytes | None = None,
        *,
        warm: bool = True,
    ) -> CommandProcess:
        """Start *argv* in *cwd* with exactly *env*, tracking its tree with *tracker*
        from creation on.

        *request*, when given, is written to the process's stdin, which then
        ends. Such a process can have been started earlier with the same
        *argv*, *env* and *tracker* in another directory: the request carries
        everything the call adds, the workdir included. With *warm* False, *env*
        holds values of this call, so the process is started for this launch
        alone. Raises OSError when the process cannot be started or tracked.
        """
        ...

    async def aclose(self) -> None:
        """End every process the launcher started ahead of a launch."""

    def close(self) -> None:
        """``aclose`` without an Event Loop."""


class CommandManagerError(VBotError):
    """Base class for expected ``CommandManager`` failures."""


class CommandNotFoundError(CommandManagerError):
    """No command with this id exists here (any more)."""


class CommandNotStartedError(CommandManagerError):
    """The command was refused: its Run was cancelled, its execution group closed,
    or vBot is shutting down."""


class CommandCapacityError(CommandManagerError):
    """``COMMAND_MAX_LIVE`` commands run already; retry once one has ended."""

    def __init__(self, message: str, *, limit: int) -> None:
        super().__init__(message)
        self.limit = limit


class CommandLaunchError(CommandManagerError):
    """The command's process could not be started or its processes tracked."""


class CommandStopError(CommandManagerError):
    """The operating system did not confirm the kill; ``survivors`` may still run.

    The command stays live; stopping it again retries the kill. ``kill_sent``
    tells whether the kill reached its processes: then the stop keeps its
    reason, and the end the kill causes reads ``stopped``. Otherwise, unless
    it ended meanwhile, the command runs on as if not stopped.
    """

    def __init__(
        self, message: str, *, survivors: tuple[RunningProcess, ...], kill_sent: bool = False
    ) -> None:
        super().__init__(message)
        self.survivors = survivors
        self.kill_sent = kill_sent


__all__ = [
    "COMMAND_DRAIN_SECONDS",
    "COMMAND_HEAD_CHARS",
    "COMMAND_IDLE_WINDOW_SECONDS",
    "COMMAND_LEFTOVER_QUIET_SECONDS",
    "COMMAND_MAX_LIVE",
    "COMMAND_PROGRESS_LINES",
    "COMMAND_PROGRESS_SECONDS",
    "COMMAND_RECENT_LINES",
    "COMMAND_RETENTION_SECONDS",
    "COMMAND_STOP_GRACE_SECONDS",
    "COMMAND_SWEEP_INTERVAL_SECONDS",
    "COMMAND_TAIL_CHARS",
    "COMMAND_TEMPORARY_CATEGORY",
    "CommandCapacityError",
    "CommandChangedCallback",
    "CommandHandle",
    "CommandLaunchError",
    "CommandLauncher",
    "CommandManagerError",
    "CommandNotFoundError",
    "CommandNotStartedError",
    "CommandOwner",
    "CommandProcess",
    "CommandReport",
    "CommandReportFormatter",
    "CommandStatus",
    "CommandStopError",
    "CommandTranscript",
    "CommandWaitOutcome",
    "CompletionSink",
    "OutputCallback",
    "StopReason",
]
