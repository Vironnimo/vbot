"""Bounded, cancellable ripgrep runs for the file-search owner.

ripgrep selects the files (ignore rules, hidden files, globs, types, binary
detection) and matches them. A search runs in phases: a parallel counting or
listing pass over the requested roots, then, for line results, a ``--json`` pass
over only the files of the requested page. Results that name the paths ignore
files excluded get them from a ``--debug`` walk (``ignored_scan``).
"""

from __future__ import annotations

import base64
import contextlib
import errno
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from core.tools._search_query import SearchQuery
from core.tools.file_state import os_error_reason

if TYPE_CHECKING:
    # Annotations only: core.tools.search runs its listings through this module.
    from core.tools.search import SearchBudget
    from core.tools.tools import ToolContext


from core.tools._search_native import MAX_CHILD_MEMORY as MAX_CHILD_MEMORY
from core.tools._search_native import (
    MAX_COMMAND_LINE_BYTES,
    MAX_ENTRIES,
    MAX_SCAN_BYTES,
    native_lines,
)
from core.tools._search_native import MAX_PROTOCOL_LINE as MAX_PROTOCOL_LINE
from core.tools._search_native import NativeOutcome as NativeOutcome
from core.tools._search_native import SearchBoundError as SearchBoundError

# Defaults that differ from ripgrep's own; later args items override them.
DEFAULT_ARGUMENTS = ("--no-config", "--hidden", "--no-require-git", "--glob-case-insensitive")
ALWAYS_EXCLUDED = "!.git"
# Python bytecode caches, skipped like .git unless the call asks for them (``skips_caches``).
CACHES_EXCLUDED = "!__pycache__"
_CACHE_NAMES = ("__pycache__", ".pyc", ".pyo")
# Arguments that select files by type; --type-add and --type-clear only define types.
_FILE_TYPE_FILTERS = ("--type=", "--type-not=")


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
    # The bound that cut the pass short: MAX_ENTRIES ("entries") or MAX_SCAN_BYTES ("output").
    stopped_by: Literal["entries", "output"] | None = None
    interrupted: bool = False
    skipped: list[Path] = field(default_factory=list)
    ignored: list[Path] = field(default_factory=list)


class SearchRefusedError(RuntimeError):
    """ripgrep refused the query; the message says how to correct it."""


def skips_caches(query: SearchQuery) -> bool:
    """Whether the query skips ``__pycache__`` directories, as it does by default.

    ``-u`` and ``--no-ignore`` include them, and so does a selecting glob that
    names them or compiled Python files: once skipped, no later glob can select
    the files inside. A root inside one is searched anyway, as by ripgrep.
    """
    if any(argument in {"--unrestricted", "--no-ignore"} for argument in query.rg_args):
        return False
    selecting = [glob for glob in query.globs if not glob.startswith("!")]
    selecting += [arg for arg in query.rg_args if arg.startswith("--iglob=") and "=!" not in arg]
    return not any(name in glob.casefold() for glob in selecting for name in _CACHE_NAMES)


def rg_globs(globs: list[str], prefixes: list[str], *, skip_caches: bool = False) -> list[str]:
    """Return ripgrep glob arguments for one scope.

    A glob with a slash is tried relative to the scope's directory and relative
    to each searched root below it, so ``tests/*.py`` and ``*.py`` under the
    root ``tests`` both select ``tests/a.py``. ``./`` anchors like a leading
    ``/``. ``.git`` is always excluded, and with ``skip_caches`` ``__pycache__``,
    after every other glob.
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
    if skip_caches:
        arguments.append(f"--glob={CACHES_EXCLUDED}")
    arguments.append(f"--glob={ALWAYS_EXCLUDED}")
    return arguments


def _base_arguments(query: SearchQuery, scope: Scope, *, globs: bool = True) -> list[str]:
    return [
        *DEFAULT_ARGUMENTS,
        *query.rg_args,
        *(rg_globs(query.globs, scope.prefixes, skip_caches=skips_caches(query)) if globs else []),
    ]


def _pattern_arguments(patterns: list[str]) -> list[str]:
    return [token for pattern in patterns for token in ("-e", pattern)]


def _without_file_types(arguments: list[str]) -> list[str]:
    return [argument for argument in arguments if not argument.startswith(_FILE_TYPE_FILTERS)]


def _walk_paths(paths: list[bytes], scope: Scope) -> list[Path]:
    """Return the absolute paths of ripgrep walk reports, which are relative to the scope."""
    return [Path(os.path.normpath(scope.cwd / os.fsdecode(path))) for path in paths]


# Flags that change how a pattern matches text, as opposed to which files are searched.
_MATCHING_FLAGS = re.compile(
    r"--(?:(?:no-)?(?:fixed-strings|invert-match|pcre2|unicode|pcre2-unicode)|case-sensitive|"
    r"ignore-case|smart-case|word-regexp|line-regexp|auto-hybrid-regex|engine=.*|"
    r"(?:regex|dfa)-size-limit=.*)"
)


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
# OS error codes for a missing file: Windows' file and path not found, POSIX ENOENT.
_NOT_FOUND = frozenset({2, 3}) if os.name == "nt" else frozenset({errno.ENOENT})


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
            if code in _NOT_FOUND and not os.path.lexists(scope.cwd / match["path"]):
                # Deleted after the walk listed it, such as a SQLite journal: nothing to read.
                continue
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


_REGEX_ERROR = re.compile(
    r"regex parse error:\n {4}(?P<wrapped>[^\n]*)\n {4}(?P<pad> *)(?P<caret>\^+)\n"
    r"error: (?P<kind>[^\n]+)"
)


def _pattern_error(message: str, patterns: Sequence[str]) -> tuple[str, str] | None:
    """Restate a regex parse error against the pattern as sent.

    ripgrep reports the error in its combined form ``(?:p1)|(?:p2)`` with the
    caret placed for that form. Returns the error kind, and the offending pattern
    over a caret line under the offending part; None when the error does not map
    onto one pattern, such as a PCRE2 error or one about ripgrep's own wrapper.
    """
    match = _REGEX_ERROR.search(message)
    if match is None or match["wrapped"] != "|".join(f"(?:{pattern})" for pattern in patterns):
        return None
    start = len(match["pad"])
    begin = 0
    for pattern in patterns:
        begin += len("(?:")
        if begin <= start < begin + len(pattern):
            local = start - begin
            width = min(len(match["caret"]), len(pattern) - local)
            kind = match["kind"]
            if kind.endswith("escape sequence"):
                kind += f' "{pattern[local : local + width]}"'
            return kind, f"    {pattern}\n    {' ' * local}{'^' * width}"
        begin += len(pattern) + len(")|")
    return None


def explain_failure(message: str, patterns: Sequence[str] = ()) -> str:
    """Say what ripgrep refused and how to correct the call. Nothing was searched.

    *patterns* are the regular expressions the call sent, in ripgrep's order, so
    a regex error can point at the pattern as sent.
    """
    if any(marker in message for marker in _PATTERN_ERRORS):
        advice = (
            'To search literal text, add "-F" to args; otherwise correct the regular expression.'
        )
        mapped = _pattern_error(message, patterns)
        if mapped is not None:
            kind, shown = mapped
            return (
                "The pattern is not a valid regular expression (ripgrep syntax): "
                f"{kind}. Nothing was searched.\n{shown}\n{advice}"
            )
        return (
            "The pattern is not a valid regular expression (ripgrep syntax). Nothing was "
            f"searched.\n{message}\n{advice}"
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


class SearchExecution:
    """Execute the bounded native phases belonging to one file search."""

    def __init__(self, binary: Path, context: ToolContext, budget: SearchBudget, cwd: Path) -> None:
        self.binary = binary
        self.context = context
        self.budget = budget
        self.cwd = cwd

    def _collect(
        self, arguments: list[str], scope: Scope, terminator: bytes
    ) -> tuple[bytes, NativeOutcome, Literal["entries", "output"] | None]:
        """Read one counting or listing pass, up to the first bound it reaches.

        Each pass prints its entries with --null, so a record holding NUL is one
        entry; the statistics after the counts hold none. Reading stops at the entry
        after ``MAX_ENTRIES``, which shows that more follow and is left out, or at the
        record that takes the output over ``MAX_SCAN_BYTES``. Stopping closes the
        records, which ends the child. The third value names the bound that stopped it.
        """
        outcome = NativeOutcome()
        chunks: list[bytes] = []
        size = 0
        entries = 0
        stopped_by: Literal["entries", "output"] | None = None
        records = native_lines(
            self.binary,
            arguments,
            self.context,
            self.budget,
            cwd=scope.cwd,
            outcome=outcome,
            terminator=terminator,
        )
        with contextlib.closing(records):
            for record in records:
                if b"\0" in record:
                    entries += 1
                    if entries > MAX_ENTRIES:
                        stopped_by = "entries"
                        break
                chunks.append(record)
                size += len(record)
                if size > MAX_SCAN_BYTES:
                    stopped_by = "output"
                    break
        return b"".join(chunks), outcome, stopped_by

    def count_scan(self, query: SearchQuery, patterns: list[str], scope: Scope) -> ScanResult:
        """Count matching lines (or matches) per file in one parallel pass."""
        # With --null, a file list ends each path with NUL; counts still end with a newline.
        terminator = b"\n"
        if query.mode == "files_without_match":
            selector = ["--files-without-match", "--null"]
            terminator = b"\0"
        else:
            selector = [
                # With -U ripgrep's line count merges matches unpredictably; count matches.
                "--count-matches" if query.count_matches or query.multiline else "--count",
                *(["--include-zero"] if query.include_zero and query.mode == "count" else []),
                "--with-filename",
                "--null",
                "--stats",
            ]
        # No --debug: only a result without entries names the paths ignore files excluded,
        # and ignored_scan walks for them then.
        arguments = [
            *_base_arguments(query, scope),
            *selector,
            *_pattern_arguments(patterns),
            "--",
            *scope.paths,
        ]
        data, outcome, stopped_by = self._collect(arguments, scope, terminator)
        result = _judge(outcome, scope, self.cwd)
        result.stopped_by = stopped_by
        if query.mode == "files_without_match":
            result.entries = [(path, 0) for path in data.split(b"\0")[:-1] if path]
        else:
            result.entries, result.files_searched = _parse_counts(data)
        return result

    def list_scan(
        self, query: SearchQuery, scope: Scope, *, directories: bool = False
    ) -> ScanResult:
        """List the files ripgrep would search, in one parallel pass.

        The result names the paths ignore files excluded (``ignored``). For
        ``directories``, only the excluding globs apply, since the others select
        directory names, and the result also names every path ripgrep skipped.
        """
        if directories:
            excluding = [glob for glob in query.globs if glob.startswith("!")]
            skip_caches = skips_caches(query)
            selection = [
                *DEFAULT_ARGUMENTS,
                *query.rg_args,
                *rg_globs(excluding, scope.prefixes, skip_caches=skip_caches),
            ]
            report = True
        else:
            selection = _base_arguments(query, scope)
            # --debug reports every file a file type leaves out, which costs several times
            # the listing; with types, ignored_scan walks once more without them instead.
            report = _without_file_types(selection) == selection
        debug = ["--debug"] if report else []
        arguments = [*selection, *debug, "--files", "--null", "--", *scope.paths]
        data, outcome, stopped_by = self._collect(arguments, scope, b"\0")
        result = _judge(outcome, scope, self.cwd)
        result.stopped_by = stopped_by
        result.entries = [(path, 0) for path in data.split(b"\0")[:-1] if path]
        if report:
            result.skipped = _walk_paths(outcome.skipped, scope)
            result.ignored = _walk_paths(outcome.ignored, scope)
        else:
            result.ignored = self.ignored_scan(query, scope)
        return result

    def ignored_scan(self, query: SearchQuery, scope: Scope) -> list[Path]:
        """Return the paths ignore files exclude from the walk of the query's files.

        ripgrep names them only in --debug reports, which also report every file a glob
        or file type leaves out and so cost a search several times its own time. Only
        results that show these paths run this walk. File types never apply to
        directories and apply after ignore files, so the walk leaves them out and
        skips the same paths. Its output is bounded like a listing's.
        """
        selection = _without_file_types(_base_arguments(query, scope))
        arguments = [*selection, "--debug", "--files", "--null", "--", *scope.paths]
        outcome = NativeOutcome()
        records = native_lines(
            self.binary,
            arguments,
            self.context,
            self.budget,
            cwd=scope.cwd,
            outcome=outcome,
            terminator=b"\0",
        )
        listed = size = 0
        with contextlib.closing(records):
            for record in records:
                listed += 1
                size += len(record)
                if listed > MAX_ENTRIES or size > MAX_SCAN_BYTES:
                    break
        return _walk_paths(outcome.ignored, scope)

    def match_names(self, query: SearchQuery, patterns: list[str], names: list[str]) -> set[str]:
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
            lines = native_lines(
                self.binary, arguments, self.context, self.budget, outcome=outcome, terminator=b"\0"
            )
            with contextlib.closing(lines):
                data = b"".join(lines)
        if outcome.returncode not in (0, 1, None):
            raise SearchRefusedError(outcome.diagnostics.strip().removeprefix("rg: "))
        return {os.fsdecode(name) for name in data.split(b"\0") if name}

    def line_events(
        self,
        query: SearchQuery,
        patterns: list[str],
        scope: Scope,
        paths: list[bytes],
        max_count: int,
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
        length = sum(len(argument) + 3 for argument in base) + len(str(self.binary))
        events: dict[bytes, list[dict[str, Any]]] = {}
        warnings: list[str] = []
        batch: list[str] = []
        size = length

        def execute() -> None:
            outcome = NativeOutcome()
            current: list[dict[str, Any]] | None = None
            current_path: bytes | None = None
            lines = native_lines(
                self.binary,
                [*base, *batch],
                self.context,
                self.budget,
                cwd=scope.cwd,
                outcome=outcome,
            )
            try:
                with contextlib.closing(lines):
                    for line in lines:
                        event = json.loads(line)
                        kind = event["type"]
                        if kind == "begin":
                            current_path = _event_path(event["data"]["path"])
                            current = events.setdefault(current_path, [])
                        elif kind in {"match", "context"} and current is not None:
                            current.append(event)
                        elif kind == "end":
                            current, current_path = None, None
            except SearchBoundError as error:
                # Only a match or context event can be that large; its file began before it.
                if error.bound != "record" or current_path is None:
                    raise
                path = Path(os.path.normpath(scope.cwd / os.fsdecode(current_path)))
                raise SearchBoundError("record", path) from None
            judged = _judge(outcome, scope, self.cwd)
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

    def reference_text(self, arguments: list[str]) -> str:
        """Return ripgrep's own reference output, such as its file type list."""
        outcome = NativeOutcome()
        lines = native_lines(self.binary, arguments, self.context, self.budget, outcome=outcome)
        with contextlib.closing(lines):
            text = b"".join(lines).decode("utf-8", errors="replace").strip()
        if outcome.returncode not in (0, None):
            raise SearchRefusedError(outcome.diagnostics.removeprefix("rg: ") or text)
        return text

    def widen_type_case(self, query: SearchQuery) -> None:
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
        listing = self.reference_text([*definitions, "--type-list"])
        for line in listing.splitlines():
            name, _, globs = line.partition(": ")
            if name not in names:
                continue
            for glob in globs.split(", "):
                widened = _any_case(glob)
                if widened != glob:
                    query.rg_args.append(f"--type-add={name}:{widened}")
