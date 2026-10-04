"""Bounded, cancellable ripgrep runs for the file-search owner.

ripgrep selects the files (ignore rules, hidden files, globs, types, binary
detection) and matches them. A search runs in phases: a parallel counting or
listing pass over the requested roots, then, for line results, a ``--json`` pass
over only the files of the requested page.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import unicodedata
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil  # type: ignore[import-untyped]

from core.tools._search_query import SearchQuery
from core.tools.file_state import os_error_reason
from core.tools.search import SearchBudget
from core.tools.tools import ToolContext
from core.utils.processes import subprocess_creation_flags

MAX_PROTOCOL_LINE = 8 * 1024 * 1024
# Bound on one native command line; a longer path list runs in several batches.
MAX_COMMAND_LINE_BYTES = 28000
# Polling interval for the child memory bound; each poll is a process query.
MEMORY_POLL_SECONDS = 0.05
# Bound on the entries one search collects before ordering them.
MAX_ENTRIES = 500_000
# Bound on the bytes one counting or listing pass may print.
MAX_SCAN_BYTES = 256 * 1024 * 1024

# A --debug line naming a path ripgrep's walker skipped. File type filters skip
# files only and report every file, so their lines are left out.
_SKIPPED = re.compile(
    rb"^rg: DEBUG\|ignore::walk\|.*?: ignoring (.*?): Ignore\((?!IgnoreMatch\(Types\()"
)

# Defaults that differ from ripgrep's own; later args items override them.
DEFAULT_ARGUMENTS = ("--no-config", "--hidden", "--no-require-git", "--glob-case-insensitive")
ALWAYS_EXCLUDED = "!.git"


@dataclass
class NativeOutcome:
    """How one native run ended, for callers that judge its diagnostics themselves."""

    returncode: int | None = None
    diagnostics: str = ""
    interrupted: bool = False
    # Paths ripgrep's walker skipped, as --debug reports them, in its own spelling.
    skipped: list[bytes] = field(default_factory=list)


def native_lines(
    binary: Path,
    arguments: list[str],
    context: ToolContext,
    budget: SearchBudget,
    *,
    cwd: Path | None = None,
    outcome: NativeOutcome | None = None,
) -> Generator[bytes]:
    """Drain both pipes with bounded storage and interrupt even a silent process.

    With ``outcome``, the exit code and diagnostics are recorded there for the
    caller to judge. Without it, a failed run raises ``RuntimeError``.
    """
    cancelled = threading.Event()
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
    # Shut down when this generator stops, which also releases an output thread blocked in put.
    messages: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=8)
    diagnostics = bytearray()

    def kill(child: subprocess.Popen[bytes]) -> None:
        with contextlib.suppress(OSError):
            if child.poll() is None:
                child.kill()

    monitored = None

    def put(kind: str, data: bytes) -> bool:
        """Queue one message; ``False`` once the consumer has stopped."""
        try:
            messages.put((kind, data))
        except queue.ShutDown:
            return False
        return True

    def output() -> None:
        assert stdout is not None
        try:
            while True:
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
                if not put("line", line):
                    break
        finally:
            put("end", b"")

    def errors() -> None:
        assert stderr is not None
        while line := stderr.readline(65536):
            if line.startswith(b"rg: DEBUG|"):
                skipped = _SKIPPED.match(line)
                if skipped and outcome is not None and len(outcome.skipped) < MAX_ENTRIES:
                    outcome.skipped.append(skipped[1])
                continue
            if len(diagnostics) < 8192:
                diagnostics.extend(line[: 8192 - len(diagnostics)])

    threads = [
        threading.Thread(target=output, daemon=True),
        threading.Thread(target=errors, daemon=True),
    ]
    started_threads = []
    next_memory_poll = 0.0
    output_finished = False
    process = subprocess.Popen(
        [str(binary), "--no-config", *arguments],
        cwd=cwd or context.effective_cwd,
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


@dataclass
class Scope:
    """One ripgrep invocation: its working directory and the roots it searches.

    Roots inside the Agent's working directory share one scope there, so
    ripgrep anchors globs that contain a slash at that directory, as in a
    shell. A root outside it gets its own scope at that root.
    """

    cwd: Path
    paths: list[str]
    prefixes: list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    """What one counting or listing pass found, in ripgrep's own path spelling."""

    entries: list[tuple[bytes, int]] = field(default_factory=list)
    files_searched: int | None = None
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False
    interrupted: bool = False
    skipped: list[Path] = field(default_factory=list)


class SearchRefusedError(RuntimeError):
    """ripgrep refused the query; the message says how to correct it."""


def rg_globs(globs: list[str], prefixes: list[str]) -> list[str]:
    """Return ripgrep glob arguments for one scope.

    A glob with a slash is tried relative to the scope's directory and relative
    to each searched root below it, so ``tests/*.py`` and ``*.py`` under the
    root ``tests`` both select ``tests/a.py``. ``./`` anchors like a leading
    ``/``. ``.git`` is always excluded, after every other glob.
    """
    arguments: list[str] = []
    for glob in globs:
        negated = glob.startswith("!")
        body = glob[1:] if negated else glob
        if os.name == "nt":
            body = re.sub(r"\\(?![*?\[\]{}!\\])", "/", body)
        if body.startswith("./"):
            body = "/" + body[2:].lstrip("/")
        variants = [body]
        if "/" in body.rstrip("/"):
            for prefix in prefixes:
                variants.append(f"/{prefix}{body}" if body.startswith("/") else f"{prefix}/{body}")
        arguments.extend(f"--glob={'!' if negated else ''}{variant}" for variant in variants)
    arguments.append(f"--glob={ALWAYS_EXCLUDED}")
    return arguments


def _base_arguments(query: SearchQuery, scope: Scope, *, globs: bool = True) -> list[str]:
    return [
        *DEFAULT_ARGUMENTS,
        *query.rg_args,
        *(rg_globs(query.globs, scope.prefixes) if globs else []),
    ]


def _pattern_arguments(patterns: list[str]) -> list[str]:
    return [token for pattern in patterns for token in ("-e", pattern)]


def _collect(
    binary: Path,
    arguments: list[str],
    scope: Scope,
    context: ToolContext,
    budget: SearchBudget,
) -> tuple[bytes, NativeOutcome, bool]:
    outcome = NativeOutcome()
    chunks: list[bytes] = []
    size = 0
    truncated = False
    lines = native_lines(binary, arguments, context, budget, cwd=scope.cwd, outcome=outcome)
    with contextlib.closing(lines):
        for line in lines:
            chunks.append(line)
            size += len(line)
            if size > MAX_SCAN_BYTES:
                truncated = True
                break
    return b"".join(chunks), outcome, truncated


def count_scan(
    binary: Path,
    query: SearchQuery,
    patterns: list[str],
    scope: Scope,
    context: ToolContext,
    budget: SearchBudget,
    cwd: Path,
) -> ScanResult:
    """Count matching lines (or matches) per file in one parallel pass."""
    if query.mode == "files_without_match":
        selector = ["--files-without-match", "--null"]
    else:
        selector = [
            # With -U ripgrep's line count merges matches unpredictably; count matches.
            "--count-matches" if query.count_matches or query.multiline else "--count",
            *(["--include-zero"] if query.include_zero and query.mode == "count" else []),
            "--with-filename",
            "--null",
            "--stats",
        ]
    arguments = [
        *_base_arguments(query, scope),
        *selector,
        *_pattern_arguments(patterns),
        "--",
        *scope.paths,
    ]
    data, outcome, truncated = _collect(binary, arguments, scope, context, budget)
    result = _judge(outcome, scope, cwd)
    result.truncated = truncated
    if query.mode == "files_without_match":
        result.entries = [(path, 0) for path in data.split(b"\0")[:-1] if path]
    else:
        result.entries, result.files_searched = _parse_counts(data)
    _bound(result)
    return result


def list_scan(
    binary: Path,
    query: SearchQuery,
    scope: Scope,
    context: ToolContext,
    budget: SearchBudget,
    cwd: Path,
    *,
    directories: bool = False,
) -> ScanResult:
    """List the files ripgrep would search, in one parallel pass.

    For ``directories``, only the excluding globs apply, since the others select
    directory names, and the result also names the paths ripgrep skipped.
    """
    if directories:
        excluding = [glob for glob in query.globs if glob.startswith("!")]
        selection = [
            *DEFAULT_ARGUMENTS,
            *query.rg_args,
            *rg_globs(excluding, scope.prefixes),
            "--debug",
        ]
    else:
        selection = _base_arguments(query, scope)
    arguments = [*selection, "--files", "--null", "--", *scope.paths]
    data, outcome, truncated = _collect(binary, arguments, scope, context, budget)
    result = _judge(outcome, scope, cwd)
    result.truncated = truncated
    result.entries = [(path, 0) for path in data.split(b"\0")[:-1] if path]
    result.skipped = [
        Path(os.path.normpath(scope.cwd / os.fsdecode(path))) for path in outcome.skipped
    ]
    _bound(result)
    return result


# Flags that change how a pattern matches text, as opposed to which files are searched.
_MATCHING_FLAGS = re.compile(
    r"--(?:(?:no-)?(?:fixed-strings|invert-match|pcre2|unicode|pcre2-unicode)|case-sensitive|"
    r"ignore-case|smart-case|word-regexp|line-regexp|auto-hybrid-regex|engine=.*|"
    r"(?:regex|dfa)-size-limit=.*)"
)


def match_names(
    binary: Path,
    query: SearchQuery,
    patterns: list[str],
    names: list[str],
    context: ToolContext,
    budget: SearchBudget,
) -> set[str]:
    """Return the names the patterns match, by ripgrep and with the query's matching flags.

    Each name is matched as one whole text, so ``-x`` compares the entire name.
    A rejected pattern raises ``SearchRefusedError`` like a content search, even
    without names to match.
    """
    flags = [argument for argument in query.rg_args if _MATCHING_FLAGS.fullmatch(argument)]
    with tempfile.TemporaryDirectory(prefix="vbot-names-") as directory:
        source = Path(directory) / "names"
        source.write_bytes(b"".join(os.fsencode(name) + b"\0" for name in names))
        arguments = [
            "--no-config",
            *flags,
            "--null-data",
            "--no-filename",
            "--no-line-number",
            "--color=never",
            *_pattern_arguments(patterns),
            "--",
            str(source),
        ]
        outcome = NativeOutcome()
        lines = native_lines(binary, arguments, context, budget, outcome=outcome)
        with contextlib.closing(lines):
            data = b"".join(lines)
    if outcome.returncode not in (0, 1, None):
        raise SearchRefusedError(outcome.diagnostics.strip().removeprefix("rg: "))
    return {os.fsdecode(name) for name in data.split(b"\0") if name}


def _bound(result: ScanResult) -> None:
    if len(result.entries) > MAX_ENTRIES:
        del result.entries[MAX_ENTRIES:]
        result.truncated = True


def _parse_counts(data: bytes) -> tuple[list[tuple[bytes, int]], int | None]:
    entries: list[tuple[bytes, int]] = []
    position = 0
    while position < len(data):
        if data[position : position + 1] == b"\n":
            break
        separator = data.find(b"\0", position)
        if separator < 0:
            break
        end = data.find(b"\n", separator)
        if end < 0:
            break
        with contextlib.suppress(ValueError):
            entries.append((data[position:separator], int(data[separator + 1 : end])))
        position = end + 1
    stats = data[position:].decode("utf-8", errors="replace")
    searched = re.search(r"^(\d+) files searched$", stats, re.MULTILINE)
    return entries, int(searched[1]) if searched else None


def line_events(
    binary: Path,
    query: SearchQuery,
    patterns: list[str],
    scope: Scope,
    paths: list[bytes],
    max_count: int,
    context: ToolContext,
    budget: SearchBudget,
    cwd: Path,
) -> tuple[dict[bytes, list[dict[str, Any]]], list[str]]:
    """Return ripgrep's match and context events for explicit files of one scope."""
    base = [
        *DEFAULT_ARGUMENTS,
        *query.rg_args,
        "--json",
        "--max-count",
        str(max_count),
        *(["--before-context", str(query.before)] if query.before else []),
        *(["--after-context", str(query.after)] if query.after else []),
        *_pattern_arguments(patterns),
        "--",
    ]
    length = sum(len(argument) + 3 for argument in base) + len(str(binary))
    events: dict[bytes, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    batch: list[str] = []
    size = length

    def execute() -> None:
        outcome = NativeOutcome()
        current: list[dict[str, Any]] | None = None
        lines = native_lines(
            binary, [*base, *batch], context, budget, cwd=scope.cwd, outcome=outcome
        )
        with contextlib.closing(lines):
            for line in lines:
                event = json.loads(line)
                kind = event["type"]
                if kind == "begin":
                    current = events.setdefault(_event_path(event["data"]["path"]), [])
                elif kind in {"match", "context"} and current is not None:
                    current.append(event)
                elif kind == "end":
                    current = None
        judged = _judge(outcome, scope, cwd)
        warnings.extend(judged.warnings)

    for path in paths:
        argument = os.fsdecode(path)
        argument_size = len(argument.encode("utf-8", errors="surrogateescape")) + 3
        if batch and size + argument_size > MAX_COMMAND_LINE_BYTES:
            execute()
            batch.clear()
            size = length
        batch.append(argument)
        size += argument_size
    if batch:
        execute()
    return events, warnings


def _event_path(value: dict[str, str]) -> bytes:
    if "text" in value:
        return os.fsencode(value["text"])
    return base64.b64decode(value["bytes"])


def decode_event_text(value: dict[str, str]) -> bytes:
    return value["text"].encode("utf-8") if "text" in value else base64.b64decode(value["bytes"])


_IO_ERROR = re.compile(
    r"^rg: (?P<path>.+?): (?:IO error for operation on .+?: )?(?P<message>.*?)"
    r" \(os error (?P<code>\d+)\)$"
)
_PATTERN_ERRORS = ("regex parse error", "PCRE2: error compiling pattern")


def _judge(outcome: NativeOutcome, scope: Scope, cwd: Path) -> ScanResult:
    """Turn ripgrep's exit status and diagnostics into a failure or warnings.

    Files ripgrep could not read become warnings in English; any other
    diagnostic means ripgrep refused the query.
    """
    result = ScanResult(interrupted=outcome.interrupted)
    if outcome.interrupted:
        return result
    messages: list[str] = []
    for line in outcome.diagnostics.splitlines():
        if line.startswith("rg: ") or not messages:
            messages.append(line)
        else:
            messages[-1] += "\n" + line
    refused = []
    for message in messages:
        text = message.removeprefix("rg: ")
        match = _IO_ERROR.match(message)
        if match is not None:
            code = int(match["code"])
            error = OSError(None, None, None, code) if os.name == "nt" else OSError(code, "")
            label = _label(match["path"], scope, cwd)
            result.warnings.append(f"Could not read {label}: {os_error_reason(error)}.")
            continue
        if text.startswith("File system loop found: "):
            result.warnings.append(f"Skipped a link loop: {text.split(': ', 1)[1]}.")
            continue
        # Other problems with one path, such as an invalid line in an ignore file,
        # name that path first; a refused query names none.
        path, separator, problem = text.partition(": ")
        if separator and path and os.path.lexists(scope.cwd / path):
            reason = problem.splitlines()[0].rstrip(".") if problem else "unknown problem"
            result.warnings.append(f"ripgrep reported for {_label(path, scope, cwd)}: {reason}.")
            continue
        refused.append(text)
    if refused:
        raise SearchRefusedError("\n".join(refused))
    if outcome.returncode not in (0, 1, None) and not result.warnings:
        raise SearchRefusedError(
            "search_files could not complete the search: the search engine exited with "
            f"code {outcome.returncode}. Retry the call."
        )
    return result


def _label(path: str, scope: Scope, cwd: Path) -> str:
    absolute = Path(os.path.normpath(scope.cwd / path))
    try:
        return absolute.relative_to(cwd).as_posix()
    except ValueError:
        return absolute.as_posix()


def reference_text(
    binary: Path, arguments: list[str], context: ToolContext, budget: SearchBudget
) -> str:
    """Return ripgrep's own reference output, such as its file type list."""
    outcome = NativeOutcome()
    lines = native_lines(binary, arguments, context, budget, outcome=outcome)
    with contextlib.closing(lines):
        text = b"".join(lines).decode("utf-8", errors="replace").strip()
    if outcome.returncode not in (0, None):
        raise SearchRefusedError(outcome.diagnostics.removeprefix("rg: ") or text)
    return text


def widen_type_case(
    binary: Path, query: SearchQuery, context: ToolContext, budget: SearchBudget
) -> None:
    """Make the file types the query selects match names in any letter case.

    ripgrep compares type globs case-sensitively even where file names are not,
    so ``-t py`` would miss ``b.PY``. While globs ignore case (the default), each
    selected type gets every glob of its definition again with letters in any case.
    """
    insensitive = True
    for argument in [*DEFAULT_ARGUMENTS, *query.rg_args]:
        if argument in {"--glob-case-insensitive", "--no-glob-case-insensitive"}:
            insensitive = argument == "--glob-case-insensitive"
    names = {
        argument.split("=", 1)[1]
        for argument in query.rg_args
        if argument.startswith(("--type=", "--type-not="))
    } - {"all"}
    if not insensitive or not names:
        return
    definitions = [
        argument
        for argument in query.rg_args
        if argument.startswith(("--type-add=", "--type-clear="))
    ]
    listing = reference_text(binary, [*definitions, "--type-list"], context, budget)
    for line in listing.splitlines():
        name, _, globs = line.partition(": ")
        if name not in names:
            continue
        for glob in globs.split(", "):
            widened = _any_case(glob)
            if widened != glob:
                query.rg_args.append(f"--type-add={name}:{widened}")


def _any_case(glob: str) -> str:
    """Return glob with each letter outside character classes matching either case."""
    result: list[str] = []
    index = 0
    while index < len(glob):
        character = glob[index]
        if character == "\\" and index + 1 < len(glob):
            result.append(glob[index : index + 2])
            index += 2
            continue
        if character == "[":
            end = glob.find("]", index + 2)
            if end > 0:
                result.append(glob[index : end + 1])
                index = end + 1
                continue
        if character.lower() != character.upper():
            result.append(f"[{character.lower()}{character.upper()}]")
        else:
            result.append(character)
        index += 1
    return "".join(result)


def explain_failure(message: str) -> str:
    """Say what ripgrep refused and how to correct the call. Nothing was searched."""
    if any(marker in message for marker in _PATTERN_ERRORS):
        return (
            "The pattern is not a valid regular expression (ripgrep syntax). Nothing was "
            f"searched.\n{message}\n"
            'To search literal text, add "-F" to args; otherwise correct the regular expression.'
        )
    if match := re.match(r"unrecognized flag (\S+)", message):
        return (
            f'ripgrep has no flag "{match[1]}". Nothing was searched. Remove or correct that '
            'args item; {"args": ["--help"]} lists the accepted flags.'
        )
    if match := re.match(r"error parsing flag (\S+): (.*)", message, re.DOTALL):
        return (
            f'The value of the args item "{match[1]}" is invalid: {match[2]}. Nothing was '
            "searched. Correct the value."
        )
    if match := re.match(r"error parsing glob '(.*)': (.*)", message, re.DOTALL):
        return (
            f'The glob "{match[1]}" is invalid: {match[2]}. Nothing was searched. Correct the glob.'
        )
    if match := re.match(r"unrecognized file type: (.*)", message):
        return (
            f'"{match[1]}" is not a file type name. Nothing was searched. {{"args": '
            '["--type-list"]} lists the names; or select files by name with glob, such as '
            '"*.ext".'
        )
    return f"{message}\nNothing was searched."


def pattern_retry(
    error: str, patterns: list[str], query: SearchQuery
) -> tuple[list[str], bool, str] | None:
    """Return the evident reading of patterns the regex engine rejected, if any.

    Returns the patterns to search, whether PCRE2 is needed, and a note for the
    Agent. A pattern that uses look-around or backreferences needs PCRE2. An
    unmatched parenthesis, a brace that starts no repetition, or a repetition
    operator with nothing to repeat cannot be regex syntax, so those characters
    match literally; everything else keeps its regex meaning. Literal (-F)
    searches never reach here.
    """
    if not any(marker in error for marker in _PATTERN_ERRORS):
        return None
    pcre2 = any(arg in {"--pcre2", "-P", "--engine=pcre2"} for arg in query.rg_args)
    if "--pcre2" in error and not pcre2:
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
        explanation + f'searched {described}. Add "-F" to args to search plain text.',
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


_REPETITION = re.compile(r"\d+(?:,\d*)?\}")


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
        elif char == "{" and (previous in {"", "(", "|"} or not _REPETITION.match(pattern, index)):
            out.append("\\{")
            previous = "atom"
        elif char in "*+?" and previous in {"", "(", "|"} and not (char == "?" and previous == "("):
            out.append("\\" + char)
            previous = "atom"
        else:
            out.append(char)
            previous = "|" if char == "|" else "atom"
    for position in open_groups:
        out[position] = "\\("
    return "".join(out)
