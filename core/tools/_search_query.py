"""Interpretation of one search_files call as a ripgrep query.

The named fields and ``args`` become one :class:`SearchQuery`. ripgrep owns
file selection and matching, so ``args`` items reach it unchanged except for
the flags this module owns: the ones that select the output shape (which the
Tool renders itself), page controls, display flags that the fixed output
already satisfies, grep spellings with a different meaning in ripgrep, and the
few flags that would run other programs.
"""

from __future__ import annotations

import codecs
import json
import re
from dataclasses import dataclass, field
from typing import Literal

type Mode = Literal[
    "content",
    "files_with_matches",
    "files_without_match",
    "count",
    "list_files",
    "list_dirs",
    "help",
    "reference",
]

CONTENT_MODES = frozenset({"content", "files_with_matches", "files_without_match", "count"})
LIST_MODES = frozenset({"list_files", "list_dirs"})
SORT_KEYS = ("path", "modified", "accessed", "created", "none")
MAX_LIMIT = 10000
MAX_OFFSET = 1_000_000
MAX_CONTEXT = 10000

# ripgrep's short flags by long name; -r/-R are grep's recursive switch here,
# never ripgrep's --replace, and -h is help only without anything to search.
_SHORT_NAMES = {
    "e": "regexp",
    "f": "file",
    "E": "encoding",
    "m": "max-count",
    "j": "threads",
    "g": "glob",
    "d": "max-depth",
    "t": "type",
    "T": "type-not",
    "A": "after-context",
    "B": "before-context",
    "C": "context",
    "M": "max-columns",
    "s": "case-sensitive",
    "i": "ignore-case",
    "F": "fixed-strings",
    "v": "invert-match",
    "x": "line-regexp",
    "U": "multiline",
    "P": "pcre2",
    "S": "smart-case",
    "a": "text",
    "w": "word-regexp",
    "L": "follow",
    ".": "hidden",
    "u": "unrestricted",
    "z": "search-zip",
    "b": "byte-offset",
    "h": "help",
    "n": "line-number",
    "N": "no-line-number",
    "0": "null",
    "o": "only-matching",
    "p": "pretty",
    "q": "quiet",
    "H": "with-filename",
    "I": "no-filename",
    "c": "count",
    "l": "files-with-matches",
    "V": "version",
    "r": "recursive",
    "R": "recursive",
}
_VALUED = frozenset(
    {
        # ripgrep
        "regexp",
        "file",
        "pre",
        "pre-glob",
        "dfa-size-limit",
        "encoding",
        "engine",
        "max-count",
        "regex-size-limit",
        "threads",
        "glob",
        "iglob",
        "ignore-file",
        "max-depth",
        "max-filesize",
        "type",
        "type-not",
        "type-add",
        "type-clear",
        "after-context",
        "before-context",
        "color",
        "colors",
        "context",
        "context-separator",
        "field-context-separator",
        "field-match-separator",
        "hostname-bin",
        "hyperlink-format",
        "max-columns",
        "path-separator",
        "replace",
        "sort",
        "sortr",
        "generate",
        # vBot page controls
        "limit",
        "offset",
        # grep
        "include",
        "exclude",
        "exclude-dir",
    }
)
# Display and process flags whose effect the fixed output already has, or that
# have no observable effect on what the Agent receives.
_SILENT = frozenset(
    {
        "line-number",
        "with-filename",
        "heading",
        "no-heading",
        "color",
        "colors",
        "column",
        "no-column",
        "byte-offset",
        "vimgrep",
        "json",
        "no-json",
        "null",
        "trim",
        "no-trim",
        "context-separator",
        "no-context-separator",
        "field-context-separator",
        "field-match-separator",
        "line-buffered",
        "block-buffered",
        "hyperlink-format",
        "stats",
        "no-stats",
        "debug",
        "trace",
        "messages",
        "no-messages",
        "ignore-messages",
        "no-ignore-messages",
        "path-separator",
        "threads",
        "mmap",
        "no-mmap",
        "pretty",
        "max-columns",
        "max-columns-preview",
        "no-max-columns-preview",
        "no-config",
        "recursive",
        "dereference-recursive",
    }
)
_IGNORED_WITH_NOTE = {
    "no-line-number": "results always name each match's file and line",
    "no-filename": "results always name each match's file and line",
    "passthru": "results show matching lines and the context you request",
    # interpret adds the offered file edit Tool.
    "replace": "results show the original lines",
}
_BLOCKED = {
    "pre": "runs another program on every file",
    "pre-glob": "only selects files for --pre",
    "hostname-bin": "runs another program",
    "generate": "prints ripgrep's own documentation files",
}
_REFERENCE = frozenset({"help", "type-list", "version", "pcre2-version"})
_OUTPUT_FLAGS: dict[str, Mode] = {
    "files-with-matches": "files_with_matches",
    "quiet": "files_with_matches",
    "files-without-match": "files_without_match",
    "count": "count",
    "count-matches": "count",
}
OUTPUT_FIELD_MODES: dict[str, Mode] = {
    "content": "content",
    "files": "files_with_matches",
    "count": "count",
}
_MODE_NAMES = {
    "files_with_matches": "file-list",
    "files_without_match": "file-list",
    "count": "count",
}


class SearchArgumentError(ValueError):
    """A call that cannot run as written; the message names the correction."""


@dataclass
class SearchQuery:
    """One interpreted search_files call."""

    mode: Mode = "content"
    patterns: list[str] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    globs: list[str] = field(default_factory=list)
    rg_args: list[str] = field(default_factory=list)
    before: int = 0
    after: int = 0
    count_matches: bool = False
    only_matching: bool = False
    literal: bool = False
    include_zero: bool = False
    sort: tuple[str, bool] | None = None
    limit: int = 100
    offset: int = 0
    pattern_operand: bool = False
    operand_roots: list[str] = field(default_factory=list)
    reference_args: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def searches_contents(self) -> bool:
        return self.mode in CONTENT_MODES

    @property
    def multiline(self) -> bool:
        """Whether a match can span lines; ripgrep then counts matches, not lines."""
        enabled = False
        for argument in self.rg_args:
            if argument in {"--multiline", "--no-multiline"}:
                enabled = argument == "--multiline"
        return enabled


@dataclass
class _Parsed:
    options: list[tuple[str, str | None, str]] = field(default_factory=list)
    operands: list[str] = field(default_factory=list)


def _is_encoding(value: str) -> bool:
    if value in {"auto", "none"}:
        return True
    try:
        codecs.lookup(value)
    except LookupError, ValueError:
        return False
    return True


def _parse_args(tokens: list[str]) -> _Parsed:
    """Split args into (name, value, as written) options and operands.

    Option values stay literal, even when they look like flags.
    """
    parsed = _Parsed()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token == "--":
            parsed.operands.extend(tokens[index:])
            break
        option_text = token.partition("=")[0] if token.startswith("--") else token
        if token.startswith("-") and any(char.isspace() for char in option_text):
            # A long option's value can contain spaces; option names never do.
            raise SearchArgumentError(
                f"args item {json.dumps(token)} holds several arguments; pass one per item, "
                f"for example {json.dumps(token.split())}."
            )
        if token.startswith("--"):
            name, equals, value = token[2:].partition("=")
            if name in _VALUED and not equals:
                if index >= len(tokens):
                    raise SearchArgumentError(
                        f'{token} in args needs a value as the next item, for example ["{token}", '
                        f'"{_example_value(name)}"].'
                    )
                value = tokens[index]
                index += 1
                equals = "="
            parsed.options.append((name, value if equals else None, token))
            continue
        if token.startswith("-") and token != "-":
            letters = token[1:]
            position = 0
            while position < len(letters):
                letter = letters[position]
                position += 1
                if letter == "=" and position > 1:
                    raise SearchArgumentError(
                        f'"{token}" in args: {token[:position]} takes no value. Pass it alone, '
                        f'as "{token[:position]}".'
                    )
                known = _SHORT_NAMES.get(letter)
                if known is None:
                    # Not a ripgrep flag; ripgrep reports it with its own message.
                    parsed.options.append((f"-{letter}", None, f"-{letter}"))
                    continue
                name = known
                if name not in _VALUED:
                    parsed.options.append((name, None, f"-{letter}"))
                    continue
                value = letters[position:].removeprefix("=")
                if not value:
                    if index >= len(tokens):
                        raise SearchArgumentError(
                            f"-{letter} in args needs a value as the next item, for example "
                            f'["-{letter}", "{_example_value(name)}"].'
                        )
                    if name == "encoding" and not _is_encoding(tokens[index]):
                        # grep's -E (extended regex) is ripgrep's default syntax.
                        break
                    value = tokens[index]
                    index += 1
                elif name == "encoding" and not _is_encoding(value):
                    raise SearchArgumentError(
                        f'-E selects a file encoding, and "{value}" is not one. Omit -E for '
                        "regular expressions; ripgrep syntax is the default."
                    )
                parsed.options.append((name, value, f"-{letter}"))
                break
            continue
        parsed.operands.append(token)
    return parsed


def _example_value(name: str) -> str:
    return {
        "glob": "*.py",
        "iglob": "*.py",
        "include": "*.py",
        "exclude": "*.min.js",
        "exclude-dir": "node_modules",
        "type": "py",
        "type-not": "py",
        "regexp": "pattern",
        "sort": "modified",
        "sortr": "modified",
        "encoding": "latin1",
    }.get(name, "2")


def _number(name: str, value: str, *, minimum: int, maximum: int, field_name: str = "") -> int:
    label = field_name or f"--{name}"
    text = value.strip()
    if not re.fullmatch(r"\d+", text):
        raise SearchArgumentError(
            f'{label} needs a whole number between {minimum} and {maximum}; received "{value}".'
        )
    number = int(text)
    if not minimum <= number <= maximum:
        raise SearchArgumentError(
            f"{label} needs a whole number between {minimum} and {maximum}; received {number}."
        )
    return number


_GLOB_EVERYTHING = frozenset({"*", "**", "*.*", "**/*", "./*", "./**"})
_GLOB_SHAPE = re.compile(r"[^\s()|^$+\\]+")
_NAME_GLOB = re.compile(r"[\w.\-/{},]*(?<![.\])])\*[\w.\-/*{},]*\.(?:\w{1,10}|\{[\w,]+\})")


def glob_shaped(pattern: str) -> bool:
    """Whether a pattern can only be meant as a file name glob such as *.py or test_*.py."""
    if not _GLOB_SHAPE.fullmatch(pattern):
        return False
    if pattern.startswith("*") or "**" in pattern or "/*." in pattern:
        return True
    return bool(_NAME_GLOB.fullmatch(pattern))


def interpret(
    *,
    pattern: str | None,
    roots: list[str],
    globs: list[str],
    output: str | None,
    context: int | None,
    args: list[str],
    limit: int | None,
    offset: int | None,
    edit_tool: str | None = None,
) -> SearchQuery:
    """Combine the named fields and args into one query.

    *edit_tool* is the file edit Tool the Agent is offered, for the note that
    replaces ripgrep's --replace.
    """
    query = SearchQuery(roots=list(roots), globs=list(globs))
    parsed = _parse_args(args)
    explicit_patterns = [pattern] if pattern is not None else []
    pattern_sources = 1 if pattern is not None else 0
    mode: Mode | None = OUTPUT_FIELD_MODES.get(output) if output else None
    listing: Mode | None = None
    reference: str | None = None
    before = after = context if context is not None else 0
    context_requested = bool(context)
    limits: list[int] = [limit] if limit is not None else []
    offsets: list[int] = [offset] if offset is not None else []
    noted: set[str] = set()
    grep_h = False
    output_flags: list[str] = []
    for name, value, written in parsed.options:
        if name in {"regexp", "file"}:
            pattern_sources += 1
            if name == "regexp":
                explicit_patterns.append(value or "")
            else:
                query.rg_args.append(f"--file={value}")
                if value == "-":
                    raise SearchArgumentError(
                        "-f - reads patterns from standard input, which search_files does not "
                        "have. Pass the patterns in pattern or as -e items."
                    )
        elif name in _BLOCKED:
            raise SearchArgumentError(
                f"{written} {_BLOCKED[name]} and is not available in search_files. Remove it "
                "from args."
            )
        elif name in _REFERENCE:
            reference = name
            grep_h = written == "-h"
        elif name in _OUTPUT_FLAGS:
            mode = _OUTPUT_FLAGS[name]
            query.count_matches = name == "count-matches"
            output_flags.append(written)
        elif name in {"include-zero", "no-include-zero"}:
            query.include_zero = name == "include-zero"
        elif name == "files":
            if listing == "list_dirs":
                raise SearchArgumentError(
                    "Choose one of --files or --dirs; they list different entries."
                )
            listing = "list_files"
        elif name == "dirs":
            if listing == "list_files":
                raise SearchArgumentError(
                    "Choose one of --files or --dirs; they list different entries."
                )
            listing = "list_dirs"
        elif name == "only-matching":
            query.only_matching = True
        elif name in {"context", "after-context", "before-context"}:
            lines = _number(name, value or "", minimum=0, maximum=MAX_CONTEXT)
            if name != "after-context":
                before = lines
            if name != "before-context":
                after = lines
            context_requested = context_requested or lines > 0
        elif name in {"sort", "sortr", "sort-files"}:
            key = value if name != "sort-files" else "path"
            if key not in SORT_KEYS:
                raise SearchArgumentError(
                    f'{written} sorts by one of {", ".join(SORT_KEYS)}; received "{value}".'
                )
            query.sort = (key, name == "sortr")
        elif name == "limit":
            limits.append(_number(name, value or "", minimum=1, maximum=MAX_LIMIT))
        elif name == "offset":
            offsets.append(_number(name, value or "", minimum=0, maximum=MAX_OFFSET))
        elif name in {"include", "glob"}:
            query.globs.append(value or "")
        elif name == "iglob":
            query.rg_args.append(f"--iglob={value}")
        elif name in {"exclude", "exclude-dir"}:
            query.globs.append("!" + (value or "").lstrip("!"))
        elif name in _SILENT:
            continue
        elif name in _IGNORED_WITH_NOTE:
            if written not in noted:
                noted.add(written)
                reason = _IGNORED_WITH_NOTE[name]
                if name == "replace" and edit_tool is not None:
                    reason += f"; use {edit_tool} to change files"
                query.notes.append(f"{written} was ignored: {reason}.")
        elif name == "encoding":
            query.rg_args.append(f"--encoding={value}")
        elif value is not None:
            query.rg_args.append(f"--{name}={value}")
        elif name.startswith("-"):
            query.rg_args.append(name)
        else:
            query.rg_args.append(f"--{name}")
            if name == "fixed-strings":
                query.literal = True
            elif name == "no-fixed-strings":
                query.literal = False

    operands = list(parsed.operands)
    if grep_h and (explicit_patterns or operands or roots or globs):
        # grep's -h hides file names; with something to search it is not a help request.
        reference = None
        query.notes.append(f"-h was ignored: {_IGNORED_WITH_NOTE['no-filename']}.")
    if reference is not None:
        if explicit_patterns or operands or roots or globs or output or context:
            raise SearchArgumentError(
                f"--{reference} shows a reference and does not search; omit pattern, path, glob "
                "and other search fields, or remove it from args."
            )
        query.mode = "help" if reference == "help" else "reference"
        query.reference_args = [f"--{reference}"] + [
            arg for arg in query.rg_args if arg.startswith(("--type-add=", "--type-clear="))
        ]
        return query

    if pattern_sources == 0 and operands and listing is None:
        query.pattern_operand = True
        pattern_sources = 1
        explicit_patterns.append(operands.pop(0))
    query.roots.extend(operands)
    query.operand_roots = operands
    query.patterns = explicit_patterns
    query.limit = _single(limits, "limit", 100)
    query.offset = _single(offsets, "offset", 0)

    if listing is not None:
        if query.patterns:
            sent = "pattern" if pattern is not None else "-e"
            raise SearchArgumentError(
                f"--{listing.removeprefix('list_')} lists entries by name and does not search file "
                f"contents, so it cannot be combined with {sent}. To select names, pass glob "
                f'(such as "tmp*"); to search contents, remove --{listing.removeprefix("list_")}.'
            )
        listed = "files" if listing == "list_files" else "directories"
        for written in [*output_flags, *([f'output "{output}"'] if output else [])]:
            query.notes.append(
                f"{written} was ignored because --{listing.removeprefix('list_')} lists "
                f"{listed} without searching their contents."
            )
        mode = listing
    elif pattern_sources == 0:
        if mode == "count":
            raise SearchArgumentError(
                'output "count" counts matching lines per file and needs a pattern; omit output '
                "to list files."
            )
        if context_requested:
            raise SearchArgumentError(
                "context shows lines around content matches and needs a pattern; omit context "
                "to list files."
            )
        mode = "list_files"
    elif mode is None:
        mode = "content"
    query.mode = mode

    if (
        query.mode in {"content", "files_with_matches"}
        and len(query.patterns) == 1
        and not query.literal
    ):
        _list_glob_shaped_pattern(query, context_requested, output)
    if query.searches_contents:
        if query.mode != "content" and context_requested:
            query.notes.append(
                f"Context was ignored because {_MODE_NAMES[query.mode]} output shows no lines."
            )
            context_requested = False
        elif query.only_matching and context_requested:
            query.notes.append("Context was ignored because -o shows only the matched text.")
            context_requested = False
        if context_requested:
            query.before, query.after = before, after
        if query.only_matching and query.mode == "content":
            query.count_matches = True
    return query


def _single(values: list[int], name: str, default: int) -> int:
    if len(set(values)) > 1:
        raise SearchArgumentError(f"Conflicting {name} values; provide one intended page.")
    return values[0] if values else default


def _list_glob_shaped_pattern(
    query: SearchQuery, context_requested: bool, output: str | None
) -> None:
    """Run a file name glob sent as the pattern as the file listing it asks for."""
    pattern = query.patterns[0]
    if output == "count" or context_requested or query.only_matching:
        return
    if pattern in _GLOB_EVERYTHING:
        query.patterns = []
        query.mode = "list_files"
        query.notes.append(
            f'pattern "{pattern}" matches every file name, so files were listed. To search file '
            "contents, put a regular expression in pattern."
        )
        return
    if query.globs or not glob_shaped(pattern):
        return
    query.patterns = []
    query.mode = "list_files"
    query.globs = [pattern]
    query.notes.append(
        f'pattern "{pattern}" is a file name glob, so matching files were listed. To search '
        "file contents, put a regular expression in pattern and the file filter in glob."
    )
