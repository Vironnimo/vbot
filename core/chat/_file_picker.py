"""Bounded native ripgrep walks for Chat's file-mention picker."""

from __future__ import annotations

import contextlib
import io
import os
import queue
import re
import subprocess
import threading
import time
from collections.abc import Generator, Iterable, Iterator
from pathlib import Path

import psutil  # type: ignore[import-untyped]

from core.utils.processes import subprocess_creation_flags

# Include dotfiles but honor ignore rules, also outside a Git repository.
_SELECTION = (
    "--no-config",
    "--hidden",
    "--no-require-git",
    "--glob-case-insensitive",
    "--glob=!__pycache__",
    "--glob=!.git",
    "--files",
)
_MAX_CHILD_MEMORY = 512 * 1024 * 1024
_POLL_SECONDS = 0.05
_MAX_RECORD = 8 * 1024 * 1024
_DEBUG_MESSAGE_BYTES = 1024 * 1024
_SKIPPED = re.compile(
    rb"^rg: DEBUG\|ignore::walk\|.*?: ignoring (.*?): Ignore\(IgnoreMatch\((?!Types\()\w+",
    re.DOTALL,
)


def list_selected_files(
    binary: Path, root: Path, *, timeout: float, limit: int
) -> tuple[list[str], bool]:
    """Collect picker paths in the time and file budgets. Unreadable entries are skipped."""
    files: list[str] = []
    records = _walk(binary, root, ["--null"], timeout=timeout, debug=False)
    with contextlib.closing(records):
        try:
            for record in records:
                if len(files) >= limit:
                    return files, True
                files.append(os.fsdecode(record).replace(os.sep, "/"))
        except TimeoutError:
            return files, True
    return files, False


def unselected_names(
    binary: Path, root: Path, directory: str, names: Iterable[str], *, timeout: float
) -> set[str]:
    """Mark direct children excluded from the index, including by an ignored ancestor."""
    names = set(names)
    parts = [part for part in directory.split("/") if part]
    # One shallow walk per ancestor reveals whether ripgrep would enter its child.
    walked = [os.path.join(*parts[:depth]) if depth else "." for depth in range(len(parts) + 1)]
    arguments = ["--debug", "--max-depth=1", "--", *walked]
    levels = {root.joinpath(*parts[:depth]) for depth in range(1, len(parts) + 1)}
    base = root.joinpath(*parts)
    children = {base / name: name for name in names}
    ignored: set[str] = set()
    records = _walk(binary, root, arguments, timeout=timeout, debug=True)
    with contextlib.closing(records):
        for record in records:
            path = root / os.fsdecode(record)
            if path in levels:
                return names
            if name := children.get(path):
                ignored.add(name)
    return ignored


def _walk(
    binary: Path, root: Path, arguments: list[str], *, timeout: float, debug: bool
) -> Generator[bytes]:
    """Read file paths from stdout or skipped paths from stderr, draining just that pipe."""
    deadline = time.monotonic() + timeout
    if time.monotonic() >= deadline:
        raise TimeoutError
    messages: queue.Queue[list[bytes] | Exception | None] = queue.Queue(maxsize=8)
    process = subprocess.Popen(
        [str(binary), *_SELECTION, *arguments],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL if debug else subprocess.PIPE,
        stderr=subprocess.PIPE if debug else subprocess.DEVNULL,
        creationflags=subprocess_creation_flags(),
    )
    stream = process.stderr if debug else process.stdout
    assert isinstance(stream, io.BufferedIOBase)

    def drain() -> None:
        try:
            for records in _skipped_paths(stream) if debug else _file_paths(stream):
                messages.put(records)
            messages.put(None)
        except queue.ShutDown:
            pass
        except Exception as error:
            with contextlib.suppress(queue.ShutDown):
                messages.put(error)

    reader = threading.Thread(target=drain, daemon=True)
    try:
        monitored = None
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            monitored = psutil.Process(process.pid)
        reader.start()
        next_memory_poll = 0.0
        while time.monotonic() < deadline:
            if monitored is not None and time.monotonic() >= next_memory_poll:
                next_memory_poll = time.monotonic() + _POLL_SECONDS
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    if monitored.memory_info().rss > _MAX_CHILD_MEMORY:
                        raise RuntimeError("The file picker exceeded its search memory limit.")
            try:
                batch = messages.get(timeout=_POLL_SECONDS)
            except queue.Empty:
                continue
            if batch is None:
                # A child may close its pipe before it exits.
                try:
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as error:
                    raise TimeoutError from error
                return
            if isinstance(batch, Exception):
                raise batch
            yield from batch
        raise TimeoutError
    finally:
        messages.shutdown(immediate=True)
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)
        if reader.ident is not None:
            reader.join(timeout=2)
        stream.close()
        # Release Windows handles in this worker even if a traceback is kept.
        del process


def _file_paths(stream: io.BufferedIOBase) -> Iterator[list[bytes]]:
    """Split a bounded read into NUL-separated paths. Newlines belong to the name."""
    pending = bytearray()
    while chunk := stream.read1(64 * 1024):
        parts = chunk.split(b"\0")
        pending.extend(parts[0])
        if len(pending) > _MAX_RECORD:
            raise RuntimeError("A file picker path exceeded the output record limit.")
        if len(parts) > 1:
            parts[0] = bytes(pending)
            pending = bytearray(parts.pop())
            yield parts
    if pending:
        yield [bytes(pending)]


def _skipped_paths(stream: io.BufferedIOBase) -> Iterator[list[bytes]]:
    """Read ripgrep's skip reports; a filename can continue on another line."""
    debug = bytearray()
    while line := stream.readline(65536):
        if line.startswith(b"rg: "):
            if skipped := _SKIPPED.match(debug):
                yield [skipped[1]]
            debug.clear()
            if line.startswith(b"rg: DEBUG|"):
                debug.extend(line)
        elif debug:
            debug.extend(line[: _DEBUG_MESSAGE_BYTES - len(debug)])
    if skipped := _SKIPPED.match(debug):
        yield [skipped[1]]
