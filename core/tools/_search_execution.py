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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from core.tools._search_confinement import Confinement, scope_confinement
from core.tools._search_query import SearchQuery, glob_separators
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

# Defaults that differ from ripgrep's own; later args items override them. --crlf lets
# ^ and $ match at the CRLF line ends that read shows as plain lines.
DEFAULT_ARGUMENTS = (
    "--no-config",
    "--hidden",
    "--no-require-git",
    "--glob-case-insensitive",
    "--crlf",
)
ALWAYS_EXCLUDED = "!.git"
# Python bytecode caches, skipped like .git unless the call asks for them (``skips_caches``).
CACHES_EXCLUDED = "!__pycache__"
_CACHE_NAMES = ("__pycache__", ".pyc", ".pyo")
# Arguments that select files by type; --type-add and --type-clear only define types.
_FILE_TYPE_FILTERS = ("--type=", "--type-not=")


@dataclass
class Scope:
    """One ripgrep invocation: its working directory and the roots it searches.

    Plain roots inside the Agent's working directory share one scope there, so
    ripgrep anchors globs that contain a slash at that directory, as in a
    shell. A root outside it gets its own scope at that root. Wildcard roots
    keep their selecting globs in separate scopes.
    """

    cwd: Path
    paths: list[str]
    prefixes: list[str] = field(default_factory=list)
    # Ordered globs that apply at these roots, including any wildcard root.
    globs: list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    """What one counting or listing pass found, in ripgrep's own path spelling."""

    entries: list[tuple[bytes, int]] = field(default_factory=list)
    files_searched: int | None = None
    warnings: list[str] = field(default_factory=list)
    # One "Could not read <path>: <reason>." per path ripgrep could not read, and the
    # number of further diagnostics past the native bound, which are such paths too.
    unreadable: list[str] = field(default_factory=list)
    unreadable_unnamed: int = 0
    # The bound that cut the pass short: MAX_ENTRIES ("entries") or MAX_SCAN_BYTES ("output").
    stopped_by: Literal["entries", "output"] | None = None
    interrupted: bool = False
    skipped: list[Path] = field(default_factory=list)
    ignored: list[Path] = field(default_factory=list)


class SearchRefusedError(RuntimeError):
    """ripgrep refused the query; the message says how to correct it."""

    def __init__(self, message: str, patterns: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.patterns = patterns


def skips_caches(query: SearchQuery, root_globs: Sequence[str] = ()) -> bool:
    """Whether the query skips ``__pycache__`` directories, as it does by default.

    ``-u`` and ``--no-ignore`` include them, and so does a selecting glob that
    names them or compiled Python files: once skipped, no later glob can select
    the files inside. A root inside one is searched anyway, as by ripgrep.
    """
    if any(argument in {"--unrestricted", "--no-ignore"} for argument in query.rg_args):
        return False
    selecting = [glob for glob in root_globs if not glob.startswith("!")]
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
        body = glob_separators(glob[1:] if negated else glob)
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
        *(
            rg_globs(
                scope.globs,
                scope.prefixes,
                skip_caches=skips_caches(query, scope.globs),
            )
            if globs
            else []
        ),
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


def _judge(
    outcome: NativeOutcome,
    scope: Scope,
    cwd: Path,
    shown: Callable[[str], bool] = lambda _path: True,
) -> ScanResult:
    """Turn ripgrep's exit status and diagnostics into a refusal or warnings.

    ripgrep refuses a query before it searches: it fails, prints nothing and
    names no path it could not search. Only such a run raises; its message is
    empty when ripgrep gave no reason. In a run that searched, files ripgrep
    could not read are counted and named in English (``unreadable``), and every
    other diagnostic becomes a warning, so a search with results never fails
    over them. ripgrep exits with 2 after any such diagnostic, matches or not.
    A diagnostic about a path ``shown`` refuses is left out.
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
    other = []
    path_diagnostic = False
    for message in messages:
        text = message.removeprefix("rg: ")
        match = _IO_ERROR.match(message)
        if match is not None:
            path_diagnostic = True
            if not shown(match["path"]):
                continue
            code = int(match["code"])
            if code in _NOT_FOUND and not os.path.lexists(scope.cwd / match["path"]):
                # Deleted after the walk listed it, such as a SQLite journal: nothing to read.
                continue
            error = OSError(None, None, None, code) if os.name == "nt" else OSError(code, "")
            label = _label(match["path"], scope, cwd)
            result.unreadable.append(f"Could not read {label}: {os_error_reason(error)}.")
            continue
        if text.startswith("File system loop found: "):
            result.warnings.append(f"Skipped a link loop: {text.split(': ', 1)[1]}.")
            continue
        # Other problems with one path, such as an invalid line in an ignore file,
        # name that path first; a refused query names none.
        if text.startswith(
            (*_PATTERN_ERRORS, "error parsing ", "unrecognized flag ", "unrecognized file type:")
        ):
            other.append(text)
            continue
        path, separator, problem = text.partition("\n")[0].partition(": ")
        if separator and path and not shown(path):
            path_diagnostic = True
            continue
        if separator and path and os.path.lexists(scope.cwd / path):
            reason = problem.rstrip(".") or "unknown problem"
            result.warnings.append(f"ripgrep reported for {_label(path, scope, cwd)}: {reason}.")
            continue
        other.append(text)
    searched = (
        outcome.printed or result.warnings or result.unreadable or (path_diagnostic and not other)
    )
    if outcome.returncode not in (0, 1, None) and not searched:
        raise SearchRefusedError("\n".join(other))
    result.warnings.extend(
        f"ripgrep reported: {text.splitlines()[0].rstrip('.')}." for text in other
    )
    result.unreadable_unnamed = outcome.dropped
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


def _pattern_error(
    message: str, originals: Sequence[str], patterns: Sequence[str]
) -> tuple[str, str] | None:
    """Restate a regex parse error against the pattern as sent.

    ripgrep reports the error in its combined form ``(?:p1)|(?:p2)`` with the
    caret placed for that form. Returns the error kind, and the offending pattern
    over a caret line under the offending part; None when the error does not map
    onto one pattern, such as a PCRE2 error or one about ripgrep's own wrapper.
    """
    match = _REGEX_ERROR.search(message)
    if (
        match is None
        or len(originals) != len(patterns)
        or match["wrapped"] != "|".join(f"(?:{pattern})" for pattern in patterns)
    ):
        return None
    start = len(match["pad"])
    begin = 0
    for pattern, original in zip(patterns, originals, strict=True):
        begin += len("(?:")
        if begin <= start < begin + len(pattern):
            local = start - begin
            width = min(len(match["caret"]), len(pattern) - local)
            kind = match["kind"]
            token = pattern[local : local + width]
            if kind.endswith("escape sequence"):
                kind += f' "{token}"'
            if original.endswith(pattern[local:]):
                local += len(original) - len(pattern)
            elif not original.startswith(pattern[: local + width]):
                if original.count(token) != 1:
                    return kind, f"    {original}"
                local = original.index(token)
            return kind, f"    {original}\n    {' ' * local}{'^' * width}"
        begin += len(pattern) + len(")|")
    return None


def explain_failure(
    message: str, patterns: Sequence[str] = (), *, executed: Sequence[str] = ()
) -> str:
    """Say what ripgrep refused and how to correct the call. Nothing was searched.

    *patterns* are the regular expressions the call sent, in ripgrep's order, so
    a regex error can point at the pattern as sent. An empty *message* stands for
    a failure ripgrep gave no reason for.
    """
    if not message:
        return (
            "The search engine failed without giving a reason. Nothing was searched. "
            "Retry the call."
        )
    if any(marker in message for marker in _PATTERN_ERRORS):
        advice = (
            'To search literal text, add "-F" to args; otherwise correct the regular expression.'
        )
        mapped = _pattern_error(message, patterns, executed or patterns)
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
    return (
        f"ripgrep refused the search:\n{message}\nNothing was searched. Correct the call as "
        'that message says; {"args": ["--help"]} lists the accepted flags.'
    )


def pattern_retry(
    error: str, patterns: list[str], query: SearchQuery
) -> tuple[list[str], str | None, str] | None:
    """Return the evident reading of patterns the regex engine rejected, if any.

    Returns the patterns to search, a flag to add, and a note for the Agent.
    A rejected newline needs multiline unless the call explicitly disabled it;
    its line feeds then match CRLF line ends too (``crlf_line_breaks``).
    A pattern that uses look-around or backreferences needs PCRE2. An
    unmatched parenthesis, a brace that starts no repetition, or a repetition
    operator with nothing to repeat cannot be regex syntax, so those characters
    match literally; everything else keeps its regex meaning. Literal (-F)
    searches never reach here.
    """
    if (
        error.startswith('the literal "\\n" is not allowed in a regex')
        and query.searches_contents
        and not query.literal
        and "--no-multiline" not in query.rg_args
    ):
        return (
            crlf_line_breaks(patterns, query),
            "--multiline",
            "The pattern matches across lines, so the search ran with -U.",
        )
    if not any(marker in error for marker in _PATTERN_ERRORS):
        return None
    pcre2 = _regex_engine(query) == "pcre2"
    if "--pcre2" in error and not pcre2:
        return (
            patterns,
            "--pcre2",
            "The pattern needs PCRE2 (look-around or backreferences), so it ran with -P.",
        )
    unescaped = [_unescape_unicode_punctuation(pattern) for pattern in patterns]
    engine = "pcre2" if "PCRE2:" in error else _regex_engine(query)
    repaired = [_escape_unbalanced(pattern, engine) for pattern in unescaped]
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
        None,
        explanation + f'searched {described}. Add "-F" to args to search plain text.',
    )


# A line feed written as an escape: \n, \x0A, \x{A}, \u000A, \u{A}, \U0000000A, \U{A}.
_LINE_FEED_ESCAPE = re.compile(r"\\(?:n|x0[aA]|[xuU]\{0*[aA]\}|u000[aA]|U0000000[aA])")
# A flag group that turns on verbose mode, where raw whitespace is not matched.
_VERBOSE_FLAGS = re.compile(r"\(\?[a-zA-Z]*x")
_CARRIAGE_RETURNS = frozenset({"\\r", "\r"})


def crlf_line_breaks(
    patterns: list[str], query: SearchQuery, *, engine: str | None = None
) -> list[str]:
    """Return the patterns with each line feed widened to ``\\r?\\n`` while --crlf applies.

    ``--crlf`` only moves ``^`` and ``$``; a line feed in a multi-line pattern
    still matches LF alone, so text copied from a CRLF file would never match.
    Line feeds inside character classes, after an explicit carriage return, and
    raw line breaks under verbose mode keep their meaning. Returns ``patterns``
    itself when nothing changes, including after an explicit ``--no-crlf``.
    """
    crlf = False
    for argument in [*DEFAULT_ARGUMENTS, *query.rg_args]:
        if argument in {"--crlf", "--no-crlf"}:
            crlf = argument == "--crlf"
    engine = engine or _regex_engine(query)
    widened = [_widen_line_feeds(pattern, engine) for pattern in patterns] if crlf else patterns
    return patterns if widened == patterns else widened


def _regex_engine(query: SearchQuery) -> str:
    for flag in reversed(query.rg_args):
        if flag.startswith("--engine="):
            return flag.partition("=")[2]
        if flag == "--pcre2":
            return "pcre2"
        if flag == "--auto-hybrid-regex":
            return "auto"
        if flag in {"--no-pcre2", "--no-auto-hybrid-regex"}:
            break
    return "default"


def _class_end(pattern: str, start: int, engine: str) -> int:
    """Skip a character class, including escaped brackets and POSIX classes."""
    depth = 1
    index = start + 1
    for literal in ("^", "]"):
        if pattern.startswith(literal, index):
            index += 1
    while index < len(pattern):
        char = pattern[index]
        index += 1
        if char == "\\":
            index += 1
        elif char == "[":
            if engine == "pcre2":
                if pattern.startswith(":", index) and (end := pattern.find(":]", index + 1)) >= 0:
                    index = end + 2
            else:
                depth += 1
                for literal in ("^", "]"):
                    if pattern.startswith(literal, index):
                        index += 1
        elif char == "]":
            depth -= 1
            if not depth:
                return index
    return len(pattern)


def _widen_line_feeds(pattern: str, engine: str) -> str:
    verbose = _VERBOSE_FLAGS.search(pattern) is not None
    tokens: list[str] = []
    index = 0
    while index < len(pattern):
        escape = _LINE_FEED_ESCAPE.match(pattern, index)
        if escape is not None:
            token, line_feed = escape.group(), True
        elif pattern[index] == "[":
            token, line_feed = pattern[index : _class_end(pattern, index, engine)], False
        elif pattern[index] == "\\":
            token, line_feed = pattern[index : index + 2], False
        else:
            token = pattern[index]
            line_feed = token == "\n" and not verbose
        index += len(token)
        if line_feed and not _after_carriage_return(tokens):
            # A repetition applies to the whole line break.
            repeated = pattern.startswith(("*", "+", "?", "{"), index)
            token = "(?:\\r?\\n)" if repeated else "\\r?\\n"
        tokens.append(token)
    return "".join(tokens)


def _after_carriage_return(tokens: list[str]) -> bool:
    """Whether the tokens end with ``\\r``, optionally repeated, such as ``\\r?``."""
    if tokens and tokens[-1] in {"?", "*", "+"}:
        tokens = tokens[:-1]
    return bool(tokens) and tokens[-1] in _CARRIAGE_RETURNS


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


def _escape_unbalanced(pattern: str, engine: str) -> str:
    out: list[str] = []
    open_groups: list[int] = []
    previous = ""  # "", "(", "|" or "atom": what a repetition operator would apply to
    index = 0
    while index < len(pattern):
        char = pattern[index]
        index += 1
        if char == "\\":
            out.append(char + pattern[index : index + 1])
            index += 1
            previous = "atom"
        elif char == "[":
            start = index - 1
            index = _class_end(pattern, start, engine)
            out.append(pattern[start:index])
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
        self.permissions = context.permissions
        self.roots: list[Path] = []
        self._confinements: dict[tuple[Path, tuple[str, ...]], Confinement] = {}

    def _allowed(self, path: Path) -> bool:
        return self.permissions.check_path(path, "read") is None or any(
            self.context.permission_access.search_read_allowed(path, root) for root in self.roots
        )

    def multiline_patterns(self, query: SearchQuery) -> list[str]:
        """Widen line breaks in the dialect rg actually uses for these patterns."""
        patterns = crlf_line_breaks(query.patterns, query)
        if _regex_engine(query) != "auto":
            return patterns
        pcre = crlf_line_breaks(query.patterns, query, engine="pcre2")
        if patterns == pcre:
            return patterns
        # auto selects one engine for the complete set, including -f files.
        # Only an ambiguous bracket class needs a compile on empty stdin.
        flags = [
            arg
            for arg in query.rg_args
            if _MATCHING_FLAGS.fullmatch(arg) or arg.startswith("--file=")
        ]
        args = [
            *DEFAULT_ARGUMENTS,
            *flags,
            "--engine=default",
            "-U",
            "--quiet",
            *_pattern_arguments(query.patterns),
            "--",
            "-",
        ]
        outcome = NativeOutcome()
        with contextlib.closing(
            native_lines(
                self.binary, args, self.context, self.budget, cwd=self.cwd, outcome=outcome
            )
        ) as records:
            for _ in records:
                pass
        if outcome.returncode not in (0, 1) and "regex parse error" in outcome.diagnostics:
            query.rg_args.append("--engine=pcre2")
            return pcre
        return patterns

    def _confinement(self, scope: Scope) -> Confinement:
        """The excluding globs and path filter of ``scope`` under the Run's rules."""
        key = (scope.cwd, tuple(scope.paths))
        if key not in self._confinements:
            self._confinements[key] = scope_confinement(scope, self.permissions)
        return self._confinements[key]

    def _judged(self, outcome: NativeOutcome, scope: Scope) -> ScanResult:
        """Judge one run; a confined run reports only paths the rules permit to read."""
        if not self._confinement(scope).filtered:
            return _judge(outcome, scope, self.cwd)
        return _judge(
            outcome,
            scope,
            self.cwd,
            lambda path: self._allowed(Path(os.path.normpath(scope.cwd / os.fsdecode(path)))),
        )

    def _readable(self, paths: list[Path], scope: Scope) -> list[Path]:
        if not self._confinement(scope).filtered:
            return paths
        return [path for path in paths if self._allowed(path)]

    def _filtered(self, result: ScanResult, scope: Scope) -> ScanResult:
        """Leave out the entries and ignored paths of ``result`` the rules refuse to read.

        The count of searched files leaves out the refused files ripgrep searched.
        """
        if self._confinement(scope).filtered:
            kept = [
                entry
                for entry in result.entries
                if self._allowed(Path(os.path.normpath(scope.cwd / os.fsdecode(entry[0]))))
            ]
            if result.files_searched is not None:
                removed = len(result.entries) - len(kept)
                result.files_searched = max(0, result.files_searched - removed)
            result.entries = kept
            result.ignored = self._readable(result.ignored, scope)
        return result

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
            *self._confinement(scope).arguments(_base_arguments(query, scope)),
            *selector,
            *_pattern_arguments(patterns),
            "--",
            *scope.paths,
        ]
        data, outcome, stopped_by = self._collect(arguments, scope, terminator)
        try:
            result = self._judged(outcome, scope)
        except SearchRefusedError as error:
            error.patterns = patterns
            raise
        result.stopped_by = stopped_by
        if query.mode == "files_without_match":
            result.entries = [(path, 0) for path in data.split(b"\0")[:-1] if path]
        else:
            result.entries, result.files_searched = _parse_counts(data)
        return self._filtered(result, scope)

    def list_scan(
        self, query: SearchQuery, scope: Scope, *, directories: bool = False
    ) -> ScanResult:
        """List the files ripgrep would search, in one parallel pass.

        The result names the paths ignore files excluded (``ignored``). For a
        directory listing it also names every path ripgrep skipped. Directory
        selection needs the full ordered glob list so a later inclusion can
        reopen a directory excluded by an earlier glob.
        """
        selection = _base_arguments(query, scope)
        # Directory walks need the skip reports even when type filters apply.
        report = directories or _without_file_types(selection) == selection
        debug = ["--debug"] if report else []
        confined = self._confinement(scope).arguments(selection)
        arguments = [*confined, *debug, "--files", "--null", "--", *scope.paths]
        data, outcome, stopped_by = self._collect(arguments, scope, b"\0")
        result = self._judged(outcome, scope)
        result.stopped_by = stopped_by
        result.entries = [(path, 0) for path in data.split(b"\0")[:-1] if path]
        if report:
            result.skipped = _walk_paths(outcome.skipped, scope)
            result.ignored = _walk_paths(outcome.ignored, scope)
        else:
            result.ignored = self.ignored_scan(query, scope)
        return self._filtered(result, scope)

    def ignored_scan(self, query: SearchQuery, scope: Scope) -> list[Path]:
        """Return the paths ignore files exclude from the walk of the query's files.

        ripgrep names them only in --debug reports, which also report every file a glob
        or file type leaves out and so cost a search several times its own time. Only
        results that show these paths run this walk. File types never apply to
        directories and apply after ignore files, so the walk leaves them out and
        skips the same paths. Its output is bounded like a listing's.
        """
        selection = self._confinement(scope).arguments(
            _without_file_types(_base_arguments(query, scope))
        )
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
        return self._readable(_walk_paths(outcome.ignored, scope), scope)

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
            raise SearchRefusedError(outcome.diagnostics.strip().removeprefix("rg: "), patterns)
        return {os.fsdecode(name) for name in data.split(b"\0") if name}

    def line_events(
        self,
        query: SearchQuery,
        patterns: list[str],
        scope: Scope,
        paths: dict[bytes, int],
        max_count: int,
    ) -> tuple[dict[bytes, list[dict[str, Any]]], ScanResult]:
        """Return ripgrep's match and context events for explicit files of one scope.

        The second value holds the warnings and unreadable files of every batch.
        """
        user_max = next(
            (
                arg.partition("=")[2]
                for arg in reversed(query.rg_args)
                if arg.startswith("--max-count=")
            ),
            None,
        )
        native_max = min(max_count, int(user_max)) if user_max is not None else max_count
        base = [
            *DEFAULT_ARGUMENTS,
            *(arg for arg in query.rg_args if not arg.startswith("--max-count=")),
            "--json",
            *(["--before-context", str(query.before)] if query.before else []),
            *(["--after-context", str(query.after)] if query.after else []),
            *_pattern_arguments(patterns),
        ]
        length = sum(len(argument) + 3 for argument in base) + len(str(self.binary)) + 32
        events: dict[bytes, list[dict[str, Any]]] = {}
        problems = ScanResult()
        batch: list[str] = []
        size = length

        def execute(*, multiline_limit: int | None = None) -> None:
            outcome = NativeOutcome()
            current: list[dict[str, Any]] | None = None
            current_path: bytes | None = None
            options = (
                ["--max-count", str(native_max)] if multiline_limit is None else ["--line-buffered"]
            )
            lines = native_lines(
                self.binary,
                [*base, *options, "--", *batch],
                self.context,
                self.budget,
                cwd=scope.cwd,
                outcome=outcome,
            )
            try:
                with contextlib.closing(lines):
                    matches = 0
                    last_needed = None
                    for line in lines:
                        event = json.loads(line)
                        kind = event["type"]
                        if kind == "begin":
                            current_path = _event_path(event["data"]["path"])
                            current = events.setdefault(current_path, [])
                        elif kind in {"match", "context"} and current is not None:
                            data = event["data"]
                            first = data.get("line_number") or 1
                            if last_needed is not None and first > last_needed:
                                break
                            current.append(event)
                            if multiline_limit is not None:
                                matches += len(data.get("submatches") or [])
                                if matches >= multiline_limit:
                                    raw = decode_event_text(data["lines"])
                                    last = first + raw.count(b"\n") - int(raw.endswith(b"\n"))
                                    if last_needed is None:
                                        last_needed = last + query.after
                                    if last >= last_needed:
                                        break
                        elif kind == "end":
                            current, current_path = None, None
            except SearchBoundError as error:
                # Only a match or context event can be that large; its file began before it.
                if error.bound != "record" or current_path is None:
                    raise
                path = Path(os.path.normpath(scope.cwd / os.fsdecode(current_path)))
                raise SearchBoundError("record", path) from None
            judged = self._judged(outcome, scope)
            problems.warnings.extend(judged.warnings)
            problems.unreadable.extend(judged.unreadable)
            problems.unreadable_unnamed += judged.unreadable_unnamed

        for path, count in paths.items():
            argument = os.fsdecode(path)
            argument_size = len(argument.encode("utf-8", errors="surrogateescape")) + 3
            # rg's JSON printer can panic when -m splits a multiline event
            # whose next match shares the last line. Read that file's whole
            # events and close the child at the page boundary instead.
            partial_multiline = query.multiline and not query.inverted and count > native_max
            if batch and (partial_multiline or size + argument_size > MAX_COMMAND_LINE_BYTES):
                execute()
                batch.clear()
                size = length
            batch.append(argument)
            size += argument_size
            if partial_multiline:
                execute(multiline_limit=min(max_count, count))
                batch.clear()
                size = length
        if batch:
            execute()
        return events, problems

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
