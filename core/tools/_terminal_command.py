"""A shell command run as a Terminal Session: transcript, process tree and outcome.

A command session renders its output like any Terminal Session and also keeps
a transcript: the rendered output as final logical lines, written to a private
file and held as a bounded head and tail for the result. Its process tree is
tracked from the start, so the outcome records the facts the shell's own exit
code hides - failed child programs and processes still running - and why the
command was stopped when vBot stopped it.

The session decides when the command ends; this module only holds the state
and turns it into a ``CommandReport``. Agent-facing wording belongs to the
Tool that formats the report.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO

from core.storage.temp_files import TemporaryFileLease

from ._terminal_process_tree import ProcessTree, ProcessTreeFacts, ProgramExit, RunningProcess

COMMAND_TEMPORARY_CATEGORY = "commands"
COMMAND_HEAD_LINES = 40
COMMAND_TAIL_LINES = 80
# A transcript line kept in memory for the result; the file has every line whole.
COMMAND_MEMORY_LINE_MAX_CHARS = 65_536
# Ctrl+C first; a tree still running after this long is killed.
COMMAND_STOP_GRACE_SECONDS = 3.0
# A command that prints nothing and uses no CPU this long is idle.
COMMAND_IDLE_SECONDS = 15.0
# CPU time the whole tree may use within the idle window and still count as idle.
COMMAND_IDLE_CPU_SECONDS = 0.15
# After the shell exited, processes it left running that print nothing, start
# nothing and use no CPU this long end the command: they wait, like a server.
COMMAND_LEFTOVER_QUIET_SECONDS = 2.0
# How often a session whose output ended - a command's after its shell exited -
# checks whether its processes ended.
COMMAND_TREE_POLL_SECONDS = 1.0

StopReason = Literal["timeout", "user", "agent", "run_cancelled", "shutdown"]


@dataclass(frozen=True, slots=True)
class CommandTranscript:
    """The transcript as a bounded head and tail around the omitted middle."""

    head: tuple[str, ...]
    tail: tuple[str, ...]
    omitted_lines: int
    total_lines: int
    log_path: Path | None


@dataclass(frozen=True, slots=True)
class CommandReport:
    """What a command did, as of its end or the moment it was taken."""

    terminal_id: str
    command: str
    description: str | None
    workdir: Path
    # The command ended: its shell exited, on its own or after vBot stopped the
    # command, and every process it left running ended or went quiet.
    exited: bool
    exit_code: int | None
    stop_reason: StopReason | None
    transcript: CommandTranscript
    # Direct child programs of the shell that exited with a non-zero code.
    nonzero_exits: tuple[str, ...]
    # Processes the command started that still run after it ended; empty before.
    still_running: tuple[RunningProcess, ...]
    timeout_seconds: float | None
    # The result is delivered automatically when the command ends.
    delivers_result: bool
    # Until the timeout stops every process of the command still running, the
    # ones its exited shell left behind included; None without a timeout.
    timeout_remaining_seconds: float | None


CommandReportFormatter = Callable[[CommandReport], str]


class CommandState:
    """Transcript, process tree, stop reason and delivery of one command session."""

    def __init__(
        self,
        *,
        command: str,
        description: str | None,
        workdir: Path,
        tree: ProcessTree | None,
        transcript_lease: TemporaryFileLease | None,
        timeout_seconds: float | None,
        formatter: CommandReportFormatter,
        started_at: float,
    ) -> None:
        self.command = command
        self.description = description
        self.workdir = workdir
        self.tree = tree
        self.timeout_seconds = timeout_seconds
        self.formatter = formatter
        self.started_at = started_at
        self.exit_code: int | None = None
        self.stop_reason: StopReason | None = None
        # Failed children recorded before vBot stopped the command; later ones
        # ended because of the stop and are no failures of the command.
        self._exits_before_stop: int | None = None
        # Visible in the catalog and Terminal list; set when the command is handed off.
        self.hidden = True
        self.delivers_result = False
        # The shell exited; ``ended`` follows once its leftover processes ended
        # or went quiet.
        self.exited = asyncio.Event()
        self.ended = asyncio.Event()
        self._facts: ProcessTreeFacts | None = None
        self._head: list[str] = []
        self._tail: deque[str] = deque(maxlen=COMMAND_TAIL_LINES)
        self._total_lines = 0
        self._lease = transcript_lease
        # Kept after the lease ends: the file stays until its retention expires.
        self._log_path = transcript_lease.path if transcript_lease is not None else None
        self._file: TextIO | None = None
        if transcript_lease is not None:
            path = transcript_lease.path
            if os.name != "nt":
                path.chmod(0o600)
            self._file = path.open("a", encoding="utf-8", newline="\n")

    @property
    def shell_exited(self) -> bool:
        return self.exited.is_set()

    @property
    def has_ended(self) -> bool:
        return self.ended.is_set()

    @property
    def log_path(self) -> Path | None:
        return self._log_path

    def add_lines(self, lines: tuple[str, ...] | list[str]) -> None:
        """Append final transcript lines."""
        if not lines:
            return
        if self._file is not None:
            try:
                self._file.write("\n".join(lines) + "\n")
                self._file.flush()
            except OSError:
                self._close_file()
        for line in lines:
            kept = line[:COMMAND_MEMORY_LINE_MAX_CHARS]
            if len(self._head) < COMMAND_HEAD_LINES:
                self._head.append(kept)
            else:
                self._tail.append(kept)
        self._total_lines += len(lines)

    def record_exit(self, exit_code: int | None, facts: ProcessTreeFacts | None) -> None:
        """The shell exited: fix the outcome the report shows."""
        if self.shell_exited:
            return
        self.exit_code = exit_code
        self._facts = facts
        self.exited.set()

    def record_end(self, facts: ProcessTreeFacts | None) -> None:
        """The shell exited and what it left running ended or went quiet: the
        outcome is final, *facts* (when known) show what still runs."""
        if self.has_ended:
            return
        if facts is not None:
            self._facts = facts
        self.ended.set()

    def tree_ended(self) -> None:
        """No process of the command runs any longer."""
        if self._facts is not None and self._facts.running:
            self._facts = dataclasses.replace(self._facts, running=())

    def request_stop(self, reason: StopReason) -> None:
        """Remember why vBot stops the command; the first reason wins."""
        if self.stop_reason is None and not self.has_ended:
            self.stop_reason = reason
            if self.tree is not None:
                self._exits_before_stop = self.tree.exit_count()

    def transcript(self) -> CommandTranscript:
        # The tail holds only lines after the head, so nothing appears twice.
        return CommandTranscript(
            head=tuple(self._head),
            tail=tuple(self._tail),
            omitted_lines=self._total_lines - len(self._head) - len(self._tail),
            total_lines=self._total_lines,
            log_path=self.log_path,
        )

    def report(self, terminal_id: str, now: float) -> CommandReport:
        facts = self._facts
        return CommandReport(
            terminal_id=terminal_id,
            command=self.command,
            description=self.description,
            workdir=self.workdir,
            exited=self.has_ended,
            exit_code=self.exit_code,
            stop_reason=self.stop_reason,
            transcript=self.transcript(),
            nonzero_exits=tuple(exit.describe() for exit in self._failed_exits(facts)),
            still_running=facts.running if facts and self.has_ended else (),
            timeout_seconds=self.timeout_seconds,
            delivers_result=self.delivers_result,
            timeout_remaining_seconds=(
                max(0.0, self.timeout_seconds - (now - self.started_at))
                if self.timeout_seconds
                else None
            ),
        )

    def _failed_exits(self, facts: ProcessTreeFacts | None) -> tuple[ProgramExit, ...]:
        if facts is None:
            return ()
        if self._exits_before_stop is None:
            return facts.nonzero_exits
        return facts.nonzero_exits[: self._exits_before_stop]

    def tree_facts(self) -> ProcessTreeFacts | None:
        """Query the tree now (blocking; call off the Event Loop)."""
        if self.tree is None:
            return None
        try:
            return self.tree.facts()
        except OSError:
            return None

    def close(self) -> None:
        """Release the file and the tree once the session has finished."""
        self._close_file()
        if self._lease is not None:
            self._lease.finish()
            self._lease = None
        if self.tree is not None:
            with contextlib.suppress(OSError):
                self.tree.close()
            self.tree = None

    def _close_file(self) -> None:
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None


__all__ = [
    "COMMAND_IDLE_SECONDS",
    "COMMAND_LEFTOVER_QUIET_SECONDS",
    "COMMAND_STOP_GRACE_SECONDS",
    "COMMAND_TEMPORARY_CATEGORY",
    "CommandReport",
    "CommandReportFormatter",
    "CommandState",
    "CommandTranscript",
    "StopReason",
]
