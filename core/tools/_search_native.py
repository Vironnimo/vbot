"""Bounded cancellable native search readers with worker-owned processes."""

from __future__ import annotations

import contextlib
import io
import queue
import re
import subprocess
import threading
import time
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import psutil  # type: ignore[import-untyped]

from core.utils.processes import subprocess_creation_flags

if TYPE_CHECKING:
    # Annotations only: core.tools.search runs its listings through this module.
    from core.tools.search import SearchBudget
    from core.tools.tools import ToolContext


# Bound on one output record, such as a JSON event or a listed path.
MAX_PROTOCOL_LINE = 8 * 1024 * 1024
# Bytes read from a child's output at once; the records they complete travel together.
_READ_BYTES = 64 * 1024
# Bound on one native command line; a longer path list runs in several batches.
MAX_COMMAND_LINE_BYTES = 28000
# Bound on a child's resident memory, and its polling interval; each poll is a process query.
MAX_CHILD_MEMORY = 512 * 1024 * 1024
MEMORY_POLL_SECONDS = 0.05
# Bound on the entries one counting or listing pass collects before they are ordered.
MAX_ENTRIES = 500_000
# Bound on the bytes one counting or listing pass may print.
MAX_SCAN_BYTES = 256 * 1024 * 1024

# A --debug message naming a path ripgrep's walker skipped, and the kind of rule that
# skipped it. File type filters skip files only and report every file, so their
# messages are left out. A path can hold a line break outside Windows.
_SKIPPED = re.compile(
    rb"^rg: DEBUG\|ignore::walk\|.*?: ignoring (.*?): Ignore\(IgnoreMatch\((?!Types\()(\w+)",
    re.DOTALL,
)
# Bound on one --debug message kept for matching; a longer one is matched on its start.
_DEBUG_MESSAGE_BYTES = 1024 * 1024


class SearchBoundError(RuntimeError):
    """A native run stopped at a processing bound, so its output is unusable.

    ``bound`` is ``"record"`` when one output record exceeded ``MAX_PROTOCOL_LINE``
    and ``"memory"`` when the child exceeded ``MAX_CHILD_MEMORY``. ``path`` is the
    absolute path of the file whose output record was too large, when known.
    """

    def __init__(self, bound: Literal["record", "memory"], path: Path | None = None) -> None:
        if bound == "record":
            message = f"A native search output record exceeds {MAX_PROTOCOL_LINE} bytes."
        else:
            message = f"A native search child exceeds {MAX_CHILD_MEMORY} bytes of memory."
        super().__init__(message)
        self.bound: Literal["record", "memory"] = bound
        self.path = path


@dataclass
class NativeOutcome:
    """How one native run ended, for callers that judge its diagnostics themselves."""

    returncode: int | None = None
    diagnostics: str = ""
    interrupted: bool = False
    # Paths ripgrep's walker skipped, as --debug reports them, in its own spelling.
    skipped: list[bytes] = field(default_factory=list)
    # The skipped paths that ignore files (.gitignore, .ignore, .rgignore) excluded.
    ignored: list[bytes] = field(default_factory=list)


def native_lines(
    binary: Path,
    arguments: list[str],
    context: ToolContext | None,
    budget: SearchBudget,
    *,
    cwd: Path | None = None,
    outcome: NativeOutcome | None = None,
    terminator: bytes = b"\n",
) -> Generator[bytes]:
    """Yield the child's output records, draining both pipes with bounded storage.

    A record ends with ``terminator``, kept in the record: a line by default, a
    path for ``--null`` listings and a name for ``--null-data``. Each record is
    bounded at ``MAX_PROTOCOL_LINE``, never the whole output. Even a silent
    process is interrupted. With ``outcome``, the exit code and diagnostics are
    recorded there for the caller to judge. Without it, a failed run raises
    ``RuntimeError``. A record over its bound or a child over its memory bound
    raises ``SearchBoundError`` either way. A caller outside a Tool call passes no
    ``context`` and its ``cwd``; only its budget stops the child then.
    """
    if context is None and cwd is None:
        raise ValueError("A native search outside a Tool call needs its working directory.")
    if len(terminator) != 1:
        raise ValueError("A record terminator is one byte.")
    cancelled = threading.Event()
    if context is not None:
        # The Run retains this callback until dispatch finishes on the Event Loop.
        # Retaining Popen here would defer its Windows handle destructor to that
        # thread. Native process operations and lifetime belong to this worker.
        context.on_cancel(cancelled.set)

    def keep_going() -> bool:
        return budget.keep_going() and not cancelled.is_set()

    if not keep_going():
        if outcome is not None:
            outcome.interrupted = True
        return
    # Each message holds the records one read completed. Shut down when this generator
    # stops, which also releases an output thread blocked in put.
    messages: queue.Queue[tuple[str, list[bytes]]] = queue.Queue(maxsize=8)
    diagnostics = bytearray()

    def kill(child: subprocess.Popen[bytes]) -> None:
        with contextlib.suppress(OSError):
            if child.poll() is None:
                child.kill()

    monitored = None

    def put(kind: str, records: list[bytes]) -> bool:
        """Queue one message; ``False`` once the consumer has stopped."""
        try:
            messages.put((kind, records))
        except queue.ShutDown:
            return False
        return True

    def output() -> None:
        assert isinstance(stdout, io.BufferedIOBase)
        try:
            for records in _records(stdout, terminator):
                if records is None:
                    put("too_long", [])
                    break
                if not put("records", records):
                    break
        finally:
            put("end", [])

    def note_skip(message: bytearray) -> None:
        skipped = _SKIPPED.match(message)
        if skipped and outcome is not None and len(outcome.skipped) < MAX_ENTRIES:
            outcome.skipped.append(skipped[1])
            if skipped[2] == b"Gitignore":
                outcome.ignored.append(skipped[1])

    def errors() -> None:
        assert stderr is not None
        # A message starts with "rg: "; lines without it continue the previous one,
        # such as the regex error inside a debug message about engine fallback, or
        # the rest of a skipped path that holds a line break. So a debug message is
        # matched once the next message starts, or at the end.
        debug: bytearray | None = None
        while line := stderr.readline(65536):
            if line.startswith(b"rg: "):
                if debug is not None:
                    note_skip(debug)
                debug = bytearray() if line.startswith(b"rg: DEBUG|") else None
            if debug is not None:
                debug += line[: _DEBUG_MESSAGE_BYTES - len(debug)]
                continue
            if len(diagnostics) < 8192:
                diagnostics.extend(line[: 8192 - len(diagnostics)])
        if debug is not None:
            note_skip(debug)

    threads = [
        threading.Thread(target=output, daemon=True),
        threading.Thread(target=errors, daemon=True),
    ]
    started_threads = []
    next_memory_poll = 0.0
    output_finished = False
    if cwd is None:
        assert context is not None
        cwd = context.effective_cwd
    process = subprocess.Popen(
        [str(binary), "--no-config", *arguments],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess_creation_flags(),
    )
    try:
        stdout, stderr = process.stdout, process.stderr
        assert stdout is not None and stderr is not None
        # A fast child may exit before psutil attaches. Its pipes and exit
        # status still belong to Popen and must be drained normally.
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            monitored = psutil.Process(process.pid)
        for thread in threads:
            thread.start()
            started_threads.append(thread)
        while keep_going():
            if monitored is not None and time.monotonic() >= next_memory_poll:
                next_memory_poll = time.monotonic() + MEMORY_POLL_SECONDS
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    if monitored.memory_info().rss > MAX_CHILD_MEMORY:
                        raise SearchBoundError("memory")
            try:
                kind, records = messages.get(timeout=0.05)
            except queue.Empty:
                continue
            if kind == "end":
                output_finished = True
                break
            if kind == "too_long":
                raise SearchBoundError("record")
            yield from records
        # EOF can precede process exit. Keep checking cancellation here as well
        # instead of waiting for the entire remaining search budget at once.
        while keep_going() and process.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.05)
        interrupted = budget.stopped or cancelled.is_set()
        if interrupted:
            kill(process)
        threads[1].join(timeout=1)
        text = diagnostics.decode("utf-8", errors="backslashreplace").strip()
        if outcome is not None:
            outcome.returncode = process.poll()
            outcome.diagnostics = text
            outcome.interrupted = interrupted
            return
        if output_finished and budget.timed_out:
            raise RuntimeError("Search timed out; narrow paths or filters and retry.")
        if process.returncode not in (0, 1) and not interrupted:
            raise RuntimeError(
                text
                or "search_files could not complete the search: the search engine exited "
                f"with code {process.returncode}. Retry the call."
            )
        if text and not interrupted:
            raise RuntimeError(text)
    finally:
        try:
            messages.shutdown(immediate=True)
            kill(process)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)
            for thread in started_threads:
                thread.join(timeout=2)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        finally:
            # Also release it if a consumer retains this generator's traceback.
            # Reader threads hold only their pipes, never the process object.
            del process


def _records(stream: io.BufferedIOBase, terminator: bytes) -> Iterator[list[bytes] | None]:
    """Yield the records each read of ``stream`` completes, each ending with ``terminator``.

    A last record may lack its terminator. A record that exceeds
    ``MAX_PROTOCOL_LINE`` is not read on: ``None`` takes its place and ends the
    iteration.
    """
    pending = bytearray()
    while chunk := stream.read1(_READ_BYTES):
        # Only the new bytes can hold the terminator of the record in progress.
        searched = len(pending)
        pending += chunk
        records: list[bytes] = []
        start = 0
        while (end := pending.find(terminator, searched)) >= 0:
            if end + 1 - start > MAX_PROTOCOL_LINE:
                break
            records.append(bytes(pending[start : end + 1]))
            start = searched = end + 1
        del pending[:start]
        if records:
            yield records
        if len(pending) > MAX_PROTOCOL_LINE:
            yield None
            return
    if pending:
        yield [bytes(pending)]
