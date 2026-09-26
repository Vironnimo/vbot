"""Bounded, cancellable native execution for the file-search owner."""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import threading
import time
import unicodedata
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any

import psutil  # type: ignore[import-untyped]

from core.tools._search_options import SearchOptions
from core.tools.search import SearchBudget
from core.tools.tools import ToolContext
from core.utils.processes import subprocess_creation_flags

MAX_PROTOCOL_LINE = 8 * 1024 * 1024
# Polling interval for the child memory bound; each poll is a process query.
MEMORY_POLL_SECONDS = 0.05


def native_lines(
    binary: Path,
    arguments: list[str],
    context: ToolContext,
    budget: SearchBudget,
) -> Generator[bytes, None, None]:
    """Drain both pipes with bounded storage and interrupt even a silent process."""
    cancelled = threading.Event()
    # The Run retains this callback until dispatch finishes on the Event Loop.
    # Retaining Popen here would defer its Windows handle destructor to that
    # thread. Native process operations and lifetime belong to this worker.
    context.on_cancel(cancelled.set)

    def keep_going() -> bool:
        return budget.keep_going() and not cancelled.is_set()

    if not keep_going():
        return
    stopped = threading.Event()
    messages: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=8)
    diagnostics = bytearray()

    def kill(child: subprocess.Popen[bytes]) -> None:
        with contextlib.suppress(OSError):
            if child.poll() is None:
                child.kill()

    monitored = None

    def put(kind: str, data: bytes) -> None:
        while not stopped.is_set():
            try:
                messages.put((kind, data), timeout=0.05)
                return
            except queue.Full:
                continue

    def output() -> None:
        assert stdout is not None
        try:
            while not stopped.is_set():
                line = stdout.readline(MAX_PROTOCOL_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_PROTOCOL_LINE:
                    put(
                        "error",
                        (
                            b"A source record exceeds the 8 MiB processing bound; narrow the se"
                            b"arch or use a file/count output mode."
                        ),
                    )
                    break
                put("line", line)
        finally:
            put("end", b"")

    def errors() -> None:
        assert stderr is not None
        while chunk := stderr.read(4096):
            if len(diagnostics) < 8192:
                diagnostics.extend(chunk[: 8192 - len(diagnostics)])

    threads = [
        threading.Thread(target=output, daemon=True),
        threading.Thread(target=errors, daemon=True),
    ]
    started_threads = []
    next_memory_poll = 0.0
    output_finished = False
    process = subprocess.Popen(
        [str(binary), "--no-config", *arguments],
        cwd=context.effective_cwd,
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
                    if monitored.memory_info().rss > 512 * 1024 * 1024:
                        raise RuntimeError(
                            "Search exceeded its memory bound; narrow files or patterns."
                        )
            try:
                kind, line = messages.get(timeout=0.05)
            except queue.Empty:
                continue
            if kind == "end":
                output_finished = True
                break
            if kind == "error":
                raise RuntimeError(line.decode())
            yield line
        # EOF can precede process exit. Keep checking cancellation here as well
        # instead of waiting for the entire remaining search budget at once.
        while keep_going() and process.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.05)
        if output_finished and budget.timed_out:
            raise RuntimeError("Search timed out; narrow paths or filters and retry.")
        interrupted = budget.stopped or cancelled.is_set()
        if interrupted:
            kill(process)
        threads[1].join(timeout=1)
        if process.returncode not in (0, 1) and not interrupted:
            raise RuntimeError(
                diagnostics.decode("utf-8", errors="backslashreplace").strip()
                or f"Search engine exited with code {process.returncode}."
            )
        if diagnostics and not interrupted:
            raise RuntimeError(diagnostics.decode("utf-8", errors="backslashreplace").strip())
    finally:
        try:
            stopped.set()
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


def file_types(
    binary: Path, options: SearchOptions, context: ToolContext, budget: SearchBudget
) -> dict[str, list[str]]:
    args = ["--type-list"]
    for option, value in options.entries:
        if option.key in {"type_add", "type_clear"}:
            args.extend([option.names[0], value])
    result = {}
    for line in native_lines(binary, args, context, budget):
        name, _, patterns = line.decode("utf-8").strip().partition(": ")
        result[name] = patterns.split(", ") if patterns else []
    return result


def validate_patterns(
    binary: Path,
    patterns: list[str],
    options: SearchOptions,
    context: ToolContext,
    budget: SearchBudget,
    empty_file: Path,
) -> None:
    args = ["--json", *options.native_arguments()]
    for pattern in patterns:
        args.extend(["-e", pattern])
    args.extend(["--", str(empty_file)])
    with _explained_pattern_errors():
        for _ in native_lines(binary, args, context, budget):
            pass


@contextlib.contextmanager
def _explained_pattern_errors() -> Iterator[None]:
    """Add the literal-text correction to native pattern compilation errors."""
    try:
        yield
    except RuntimeError as error:
        if "regex parse error" in str(error) or "PCRE2: error compiling pattern" in str(error):
            raise RuntimeError(
                f"{error}\nIf you meant literal text, add -F to args. "
                "Otherwise correct the regular expression."
            ) from error
        raise


def pattern_retry(
    error: str, patterns: list[str], options: SearchOptions
) -> tuple[list[str], bool, str] | None:
    """Return the evident reading of patterns the regex engine rejected, if any.

    Returns the patterns to search, whether PCRE2 is needed, and a note for the
    Agent. A pattern that uses look-around or backreferences needs PCRE2. An
    unmatched parenthesis or a repetition operator with nothing to repeat cannot
    be regex syntax, so those characters match literally; everything else in the
    pattern keeps its regex meaning. Literal (-F) searches never reach here.
    """
    if "regex parse error" not in error and "PCRE2: error compiling pattern" not in error:
        return None
    if "--pcre2" in error and options.get("engine") != "pcre2":
        return (
            patterns,
            True,
            "The pattern needs PCRE2 (look-around or backreferences), so it ran with -P.",
        )
    unescaped = [_unescape_unicode_punctuation(pattern) for pattern in patterns]
    repaired = [_escape_unbalanced(pattern) for pattern in unescaped]
    changed = [(old, new) for old, new in zip(patterns, repaired, strict=True) if old != new]
    if not changed:
        return None
    described = "; ".join(f'"{old}" as "{new}"' for old, new in changed)
    if unescaped != patterns:
        explanation = "Unnecessary escapes before Unicode punctuation were removed"
        if repaired != unescaped:
            explanation += "; unbalanced regex characters were matched literally"
        explanation += "; the rest keeps its regex meaning: "
    else:
        explanation = "Unbalanced regex characters were matched literally: "
    return (
        repaired,
        False,
        explanation + f"searched {described}. Add -F to args to search plain text.",
    )


def _unescape_unicode_punctuation(pattern: str) -> str:
    """Remove only escapes that cannot be regex syntax, preserving escaped slashes."""
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        index += 1
        if char == "\\" and index < len(pattern):
            following = pattern[index]
            index += 1
            if not following.isascii() and unicodedata.category(following).startswith("P"):
                out.append(following)
            else:
                out.append(char + following)
        else:
            out.append(char)
    return "".join(out)


def _escape_unbalanced(pattern: str) -> str:
    out: list[str] = []
    open_groups: list[int] = []
    previous = ""  # "", "(", "|" or "atom": what a repetition operator would apply to
    class_depth = 0
    index = 0
    while index < len(pattern):
        char = pattern[index]
        index += 1
        if char == "\\":
            out.append(char + pattern[index : index + 1])
            index += 1
            previous = "atom"
        elif class_depth:
            class_depth += {"[": 1, "]": -1}.get(char, 0)
            out.append(char)
        elif char == "[":
            class_depth = 1
            out.append(char)
            for literal in ("^", "]"):
                if pattern.startswith(literal, index):
                    out.append(literal)
                    index += 1
            previous = "atom"
        elif char == "(":
            open_groups.append(len(out))
            out.append(char)
            previous = "("
        elif char == ")":
            out.append(char if open_groups else "\\)")
            if open_groups:
                open_groups.pop()
            previous = "atom"
        elif (
            char in "*+?{" and previous in {"", "(", "|"} and not (char == "?" and previous == "(")
        ):
            out.append("\\" + char)
            previous = "atom"
        else:
            out.append(char)
            previous = "|" if char == "|" else "atom"
    for position in open_groups:
        out[position] = "\\("
    return "".join(out)


def content_events(
    binary: Path,
    paths: Iterator[tuple[Path, bool]],
    patterns: list[str],
    options: SearchOptions,
    context: ToolContext,
    budget: SearchBudget,
    window: int,
) -> Generator[dict[str, Any], None, None]:
    mode = "files" if options.enabled("quiet") else options.get("output")
    base = [
        "--threads",
        "1",
        "--no-mmap",
        "--no-ignore",
        "--hidden",
        *options.native_arguments(),
    ]
    if mode:
        selectors = {
            "files": "-l",
            "without": "--files-without-match",
            "lines": "-c",
            "counts": "--count-matches",
        }
        base.extend(["--null", "--with-filename", "--color=never", selectors[mode]])
        if options.enabled("zero"):
            base.append("--include-zero")
        if options.enabled("only"):
            base.append("--only-matching")
    else:
        base.append("--json")
    if not options.get("output") and not options.enabled("quiet") and not options.get("max_count"):
        base.extend(["--max-count", str(window + 1)])
    for pattern in patterns:
        base.extend(["-e", pattern])
    length = sum(len(p) + 3 for p in base) + len(str(binary))
    if length > 20000:
        raise ValueError(
            "Search patterns/options exceed the process argument budget; split"
            " the pattern collection."
        )
    batch: list[str] = []
    size = length
    cwd = Path(os.path.abspath(context.effective_cwd.expanduser()))

    def execute() -> Generator[dict[str, Any], None, None]:
        buffer = b""
        with (
            _explained_pattern_errors(),
            contextlib.closing(
                native_lines(binary, [*base, "--", *batch], context, budget)
            ) as lines,
        ):
            for line in lines:
                if not mode:
                    yield json.loads(line)
                    continue
                buffer += line
                while b"\0" in buffer:
                    path_bytes, _, remainder = buffer.partition(b"\0")
                    count = None
                    if mode in {"lines", "counts"}:
                        if b"\n" not in remainder:
                            break
                        number, _, remainder = remainder.partition(b"\n")
                        count = int(number)
                    yield {
                        "type": "row",
                        "data": {"path": path_bytes.decode("utf-8"), "count": count},
                    }
                    buffer = remainder
            if buffer and not budget.stopped:
                raise RuntimeError("Search returned an incomplete result record.")

    for path, _ in paths:
        if not budget.keep_going():
            return
        try:
            argument = str(path.relative_to(cwd))
        except ValueError:
            argument = str(path)
        argument_size = len(argument.encode("utf-8")) + 3
        if argument_size + length > 28000:
            raise ValueError(f"Search path exceeds the native argument budget: {argument}")
        if batch and size + argument_size > 28000:
            yield from execute()
            batch.clear()
            size = length
        batch.append(argument)
        size += argument_size
    if batch:
        yield from execute()
