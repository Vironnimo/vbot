"""File and content search: one Tool over the bundled ripgrep.

ripgrep selects and matches files; this module turns a call into a ripgrep
query, runs it in bounded phases, and renders pages the Agent can continue.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from core.tools._path_suggestions import corrected_paths
from core.tools._search_execution import (
    MAX_ENTRIES,
    ScanResult,
    Scope,
    SearchRefusedError,
    count_scan,
    explain_failure,
    line_events,
    list_scan,
    match_names,
    pattern_retry,
    reference_text,
    widen_type_case,
)
from core.tools._search_query import (
    LIST_MODES,
    MAX_LIMIT,
    MAX_OFFSET,
    SearchArgumentError,
    SearchQuery,
    interpret,
)
from core.tools._search_results import (
    Entry,
    Page,
    content_page,
    content_window,
    entry_page,
    order,
    path_label,
    summary,
)
from core.tools._tool_context import _path_argument
from core.tools.arguments import optional_int
from core.tools.call_syntax import SpellingAliases as _SpellingAliases
from core.tools.call_syntax import normalize_call_arguments
from core.tools.call_syntax import spelling as _spelling
from core.tools.contracts import ToolContractError, _load_json_value, compile_tool_contract
from core.tools.edit import offered_edit_tool
from core.tools.file_state import os_error_reason
from core.tools.search import SearchBudget, _expand_brace_alternations
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
    display_notice,
    display_text,
    run_tool_worker,
    tool_failure,
    tool_success,
)
from core.utils.search_binary import require_binary

SEARCH_FILES_TOOL_NAME = "search_files"


def _repair_argument_array(value: Any) -> Any:
    """Decode list syntax without turning a malformed list into a regex class.

    A string that starts with an option is a command line such as
    "-F computeDamage( src"; it splits like one, with quotes grouping words.
    Backslashes would be shell escapes there but regex escapes here, so such a
    string stays one argument.
    """
    if isinstance(value, str) and value.lstrip().startswith("-") and "\\" not in value:
        with contextlib.suppress(ValueError):
            words = shlex.split(value)
            if len(words) > 1:
                return words
    if not isinstance(value, str) or not re.match(r'^\s*\[\s*"', value):
        return value
    try:
        return _load_json_value(value)
    except ValueError:
        pass

    # In encoded regex lists, \( and \w often omit the JSON escape for the
    # backslash. Preserve that backslash; never change members of real arrays.
    # Mixed JSON control/unicode escapes could mean text or regex instructions,
    # so a malformed list containing them needs clarification.
    ambiguous = False

    def escape(match: re.Match[str]) -> str:
        nonlocal ambiguous
        character = match[1]
        if character in "bfnrtu":
            ambiguous = True
        if character in '"\\/bfnrtu':
            return match[0]
        return "\\" + match[0]

    repaired = re.sub(r"\\(.)", escape, value, flags=re.DOTALL)
    if not ambiguous:
        try:
            return _load_json_value(repaired)
        except ValueError:
            pass
    raise ValueError(
        "args contains a malformed encoded list. Send each rg argument as a string, "
        'for example ["-F", "TODO"].'
    )


def _repair_name_list(value: Any) -> Any:
    """Read a path or glob list sent as JSON text, such as '["src", "tests"]'."""
    if not isinstance(value, str) or not re.match(r'^\s*\[\s*"', value):
        return value
    try:
        decoded = _load_json_value(value)
    except ValueError, ToolContractError:
        return value
    if isinstance(decoded, list) and decoded and all(isinstance(item, str) for item in decoded):
        return decoded
    return value


# Names other search Tools and command-line habits use for the same fields.
_FIELD_ALIASES = _SpellingAliases(
    {
        "args": ("argv", "options", "flags", "rg_args", "extra_args"),
        "pattern": (
            "query",
            "regexp",
            "search",
            "search_pattern",
            "search_term",
            "term",
            "patterns",
            "expression",
        ),
        "path": (
            "paths",
            "directory",
            "directories",
            "dir",
            "folder",
            "root",
            "roots",
            "file",
            "file_path",
            "search_path",
            "target_path",
            "target_directory",
            "cwd",
        ),
        "glob": (
            "include",
            "includes",
            "globs",
            "iglob",
            "file_glob",
            "glob_pattern",
            "file_pattern",
            "filename_pattern",
            "name_pattern",
        ),
        "output": ("output_mode", "mode"),
        "context": ("context_lines", "surrounding_lines"),
        "limit": ("head_limit", "max_results", "num_results", "max"),
        "offset": ("skip", "start"),
    }
)
_OUTPUT_VALUES = {
    **dict.fromkeys(("content", "contents", "lines", "matches", "text", "default"), "content"),
    **dict.fromkeys(
        (
            "files",
            "file",
            "fileswithmatches",
            "filesonly",
            "filenames",
            "filename",
            "paths",
            "names",
            "list",
            "l",
        ),
        "files",
    ),
    **dict.fromkeys(("count", "counts", "countmatches", "c"), "count"),
}
# Single-letter keys keep ripgrep's case: -c counts, -C adds context.
_SWITCH_LETTERS = {
    "i": ["-i"],
    "s": ["-s"],
    "S": ["-S"],
    "F": ["-F"],
    "w": ["-w"],
    "x": ["-x"],
    "v": ["-v"],
    "o": ["-o"],
    "P": ["-P"],
    "U": ["-U", "--multiline-dotall"],
    "u": ["-u"],
    "L": ["-L"],
    "n": [],
    "r": [],
    "R": [],
    "H": [],
}
# Word keys map to the args a true and a false value request.
_SWITCH_WORDS: dict[str, tuple[list[str], list[str]]] = {
    **dict.fromkeys(("ignorecase", "caseinsensitive", "insensitive", "nocase"), (["-i"], [])),
    "casesensitive": (["-s"], ["-i"]),
    **dict.fromkeys(("literal", "fixedstrings", "fixedstring", "fixed"), (["-F"], [])),
    **dict.fromkeys(("regex", "isregex", "useregex"), ([], ["-F"])),
    **dict.fromkeys(("word", "words", "wordregexp", "wholeword", "wholewords"), (["-w"], [])),
    **dict.fromkeys(("multiline", "dotall"), (["-U", "--multiline-dotall"], [])),
    **dict.fromkeys(("invert", "invertmatch"), (["-v"], [])),
    **dict.fromkeys(("unrestricted", "noignore", "includeignored"), (["-u"], [])),
    **dict.fromkeys(("linenumbers", "linenumber", "showlinenumbers", "withfilename"), ([], [])),
    "recursive": ([], ["-d", "1"]),
    "hidden": ([], ["--no-hidden"]),
}
_VALUED_LETTERS = {"A": "-A", "B": "-B", "t": "-t", "T": "-T", "m": "-m", "d": "-d"}
_VALUED_WORDS = {
    **dict.fromkeys(("after", "aftercontext", "contextafter", "linesafter"), "-A"),
    **dict.fromkeys(("before", "beforecontext", "contextbefore", "linesbefore"), "-B"),
    **dict.fromkeys(("type", "types", "filetype", "filetypes", "language"), "-t"),
    **dict.fromkeys(("maxdepth", "depth"), "-d"),
    **dict.fromkeys(("maxcount", "maxcountperfile"), "-m"),
}
_EXCLUDE_WORDS = {
    "exclude",
    "excludes",
    "excludeglob",
    "excludepattern",
    "excludedir",
    "excludedirs",
}
_FIELD_LETTERS = {"C": "context", "g": "glob", "e": "pattern"}
_TARGET_VALUES = {
    **dict.fromkeys(("files", "file", "filenames", "names", "paths"), "files"),
    **dict.fromkeys(("content", "contents", "text", "grep"), "content"),
}


def _normalize_output(value: Any) -> Any:
    return _OUTPUT_VALUES.get(_spelling(value), value) if isinstance(value, str) else value


def _switch(value: Any) -> bool | None:
    """Return the evident truth value of a flag field, or None if unclear."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        return {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}.get(
            value.strip().casefold()
        )
    return None


def _values(value: Any) -> list[str] | None:
    items = value if isinstance(value, list) else [value]
    if any(isinstance(item, bool) or not isinstance(item, str | int) for item in items):
        return None
    return [str(item) for item in items]


def _translate_flag_fields(arguments: dict[str, Any]) -> dict[str, Any]:
    """Turn flag-shaped fields from other search interfaces into args.

    A field is translated only when its meaning is exact; anything else stays
    for validation to report.
    """
    result = dict(arguments)
    tokens: list[str] = []

    def set_field(field: str, value: Any) -> None:
        if field in result and result[field] != value:
            raise ToolContractError(f"Conflicting values for {field}; provide one intended value.")
        result[field] = value

    for key, value in arguments.items():
        if key in SEARCH_FILES_TOOL_PARAMETERS["properties"]:
            continue
        letter = key.lstrip("-") if len(key.lstrip("-")) == 1 else ""
        word = _spelling(key)
        switch = _switch(value)
        if word == "regex" and isinstance(value, str) and switch is None:
            set_field("pattern", value)
        elif letter in _SWITCH_LETTERS:
            if switch is None:
                continue
            if switch:
                tokens.extend(_SWITCH_LETTERS[letter])
        elif word in _SWITCH_WORDS:
            if switch is None:
                continue
            tokens.extend(_SWITCH_WORDS[word][0 if switch else 1])
        elif letter in {"l", "c"} or word in {"count", "fileswithmatches", "filesonly"}:
            if switch is None:
                continue
            if switch:
                set_field("output", "count" if letter == "c" or word == "count" else "files")
        elif letter in _VALUED_LETTERS or word in _VALUED_WORDS:
            values = _values(value)
            if values is None:
                continue
            flag = _VALUED_LETTERS.get(letter) or _VALUED_WORDS[word]
            tokens.extend(token for item in values for token in (flag, item))
        elif word in _EXCLUDE_WORDS:
            values = _values(value)
            if values is None:
                continue
            tokens.extend(token for item in values for token in ("-g", "!" + item.lstrip("!")))
        elif letter in _FIELD_LETTERS:
            set_field(_FIELD_LETTERS[letter], value)
        elif word == "target" and isinstance(value, str) and _spelling(value) in _TARGET_VALUES:
            # Some search Tools select name matching with target="files".
            if _TARGET_VALUES[_spelling(value)] == "files" and "pattern" in result:
                set_field("glob", result.pop("pattern"))
        else:
            continue
        del result[key]
    pattern = result.get("pattern")
    if isinstance(pattern, list) and all(isinstance(item, str) for item in pattern):
        # Several patterns match any of them, like repeated -e.
        tokens.extend(token for item in pattern[1:] for token in ("-e", item))
        if pattern:
            result["pattern"] = pattern[0]
        else:
            del result["pattern"]
    if tokens:
        existing = result.get("args", [])
        result["args"] = [*tokens, *(existing if isinstance(existing, list) else [existing])]
    return result


@cache
def _repair_contract():
    return compile_tool_contract(
        name=SEARCH_FILES_TOOL_NAME,
        input_schema=SEARCH_FILES_TOOL_PARAMETERS,
        require_closed_input=False,
    )


def normalize_search_arguments(arguments: Any) -> Any:
    normalized = normalize_call_arguments(
        _repair_contract(),
        arguments,
        field_aliases=_FIELD_ALIASES,
        field_normalizers={
            "args": _repair_argument_array,
            "path": _repair_name_list,
            "glob": _repair_name_list,
            "output": _normalize_output,
        },
        empty_as_omitted=("pattern", "path", "glob", "output", "context"),
    )
    if not isinstance(normalized, dict):
        return normalized
    return _translate_flag_fields(normalized)


def _strings(arguments: JsonObject, name: str) -> list[str]:
    value = arguments.get(name, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(
        isinstance(item, str) and "\x00" not in item for item in value
    ):
        raise SearchArgumentError(
            f"{name} must be a string or a list of strings without NUL characters."
        )
    return value


def interpret_search_call(arguments: Any, *, edit_tool: str | None = None) -> SearchQuery:
    """Interpret one call's named fields and args as a single ripgrep query.

    *edit_tool* is the file edit Tool the Agent is offered, which notes may name.
    """
    arguments = normalize_search_arguments(arguments)
    if not isinstance(arguments, dict):
        raise SearchArgumentError("Provide one search argument object.")
    pattern = arguments.get("pattern")
    if pattern is not None and not isinstance(pattern, str):
        raise SearchArgumentError("pattern must be a string.")
    output = arguments.get("output")
    return interpret(
        pattern=pattern,
        roots=_strings(arguments, "path"),
        globs=_strings(arguments, "glob"),
        output=output if isinstance(output, str) else None,
        context=optional_int(arguments.get("context"), field_name="context", minimum=0),
        args=_strings(arguments, "args"),
        limit=optional_int(
            arguments.get("limit"), field_name="limit", minimum=1, maximum=MAX_LIMIT
        ),
        offset=optional_int(
            arguments.get("offset"), field_name="offset", minimum=0, maximum=MAX_OFFSET
        ),
        edit_tool=edit_tool,
    )


_GLOB_CHARACTERS = re.compile(r"[*?\[]")


@dataclass
class _Roots:
    """The roots one call searches, and the requested paths that do not exist."""

    paths: list[Path] = field(default_factory=list)
    missing: list[tuple[str, Path]] = field(default_factory=list)


def _resolve_roots(query: SearchQuery, cwd: Path) -> _Roots:
    """Resolve the requested roots; a missing path is reported, never replaced.

    A missing path written with glob characters, braces or commas runs as what
    it evidently means when that names existing paths: ``src/**/*.py`` searches
    ``src`` with that glob, ``{src,tests}`` and ``src,tests`` search both.
    """
    roots = _Roots()
    for raw in query.roots or [str(cwd)]:
        text = str(_path_argument(raw, windows=os.name == "nt"))
        if not text.strip() or text == "-":
            raise SearchArgumentError(
                f'path must name a file or directory; received "{raw}". Omit path to search '
                "the working directory."
            )
        resolved = _absolute(text, cwd)
        if os.path.lexists(resolved):
            _add(roots.paths, resolved)
            continue
        alternatives = _existing_alternatives(text, cwd)
        if alternatives:
            for path in alternatives:
                _add(roots.paths, path)
            shown = ", ".join(path_label(path, cwd) for path in alternatives)
            query.notes.append(f'path "{raw}" names several paths, so {shown} were searched.')
            continue
        base = _glob_root(text, cwd, query)
        if base is not None:
            _add(roots.paths, base)
            continue
        roots.missing.append((raw, resolved))
    return roots


def _missing_warnings(query: SearchQuery, roots: _Roots, cwd: Path) -> list[str]:
    warnings = [_missing_root_message(raw, path, cwd) for raw, path in roots.missing]
    missing_operands = [raw for raw, _ in roots.missing if raw in query.operand_roots]
    if missing_operands and query.mode == "list_files":
        warnings.append(
            "--files lists files by name, so every args operand is a path. To list the files "
            "whose contents match a pattern, replace --files with -l."
        )
    if missing_operands and query.pattern_operand:
        # Words without a path separator were most likely meant as further patterns.
        words = [raw for raw in missing_operands if "/" not in raw and "\\" not in raw]
        patterns = [query.patterns[0], *(words or missing_operands[:1])]
        if query.literal:
            flags = ["-F"]
            for text in patterns:
                flags += ["-e", text]
            combined = "args " + json.dumps(flags, ensure_ascii=False)
        else:
            combined = f'pattern "{"|".join(patterns)}"'
        warnings.append(
            f'The first args operand "{query.patterns[0]}" is the pattern and later operands '
            "are paths. If a missing path was meant as another pattern, combine the patterns: "
            f"{combined}."
        )
    return warnings


def _absolute(text: str, cwd: Path) -> Path:
    return Path(os.path.abspath(cwd / Path(text).expanduser()))


def _add(paths: list[Path], path: Path) -> None:
    if all(os.path.normcase(path) != os.path.normcase(known) for known in paths):
        paths.append(path)


def _existing_alternatives(text: str, cwd: Path) -> list[Path]:
    """Return the paths a brace or comma list names, when every one of them exists."""
    if "{" in text:
        alternatives = _expand_brace_alternations(text)
    elif "," in text:
        alternatives = [part.strip() for part in text.split(",")]
    else:
        return []
    if len(alternatives) < 2 or not all(alternatives):
        return []
    paths = [_absolute(alternative, cwd) for alternative in alternatives]
    return paths if all(os.path.lexists(path) for path in paths) else []


def _glob_root(text: str, cwd: Path, query: SearchQuery) -> Path | None:
    """Search a path written as a glob, such as src/**/*.py, as its directory and glob."""
    if query.globs or not _GLOB_CHARACTERS.search(text):
        return None
    parts = Path(text).parts
    fixed = next(index for index, part in enumerate(parts) if _GLOB_CHARACTERS.search(part))
    base = _absolute(str(Path(*parts[:fixed])) if fixed else ".", cwd)
    if not base.is_dir():
        return None
    remainder = "/".join(parts[fixed:])
    try:
        glob = "/".join(("", *base.relative_to(cwd).parts, remainder))
    except ValueError:
        glob = "/" + remainder
    query.globs.append(glob)
    query.notes.append(
        f'path "{text}" contains glob characters, so {path_label(base, cwd)} was searched with '
        f'glob "{glob}".'
    )
    return base


def _missing_root_message(raw: str, root: Path, cwd: Path) -> str:
    message = f"Path not found: {path_label(root, cwd)}"
    suggestions = corrected_paths(root, cwd)
    if suggestions:
        message += f" (similar: {', '.join(path_label(path, cwd) for path in suggestions)})"
    elif not Path(raw).expanduser().is_absolute():
        # Agents pass paths relative to another directory than the one search_files uses.
        message += f" (relative to the working directory {cwd.as_posix()})"
    return message + "."


def _ignored_summary(paths: list[Path], cwd: Path) -> str:
    """Name the shallowest paths ignore files excluded, so empty or listed results are not
    mistaken for everything there is."""
    unique = {os.path.normcase(path): path for path in paths}
    ordered = sorted(unique.values(), key=lambda path: (len(path.parts), str(path).lower()))
    shown = [path_label(path, cwd) + ("/" if path.is_dir() else "") for path in ordered[:5]]
    listed = ", ".join(shown)
    if len(ordered) > len(shown):
        listed += f" and {len(ordered) - len(shown)} more"
    return f"Ignore rules such as .gitignore excluded {listed}. Add -u to args to include them."


def _scopes(roots: list[Path], cwd: Path) -> list[Scope]:
    """Group roots into ripgrep runs: one at the working directory, one per outside root."""
    inside = Scope(cwd=cwd, paths=[])
    scopes: list[Scope] = []
    for root in roots:
        try:
            relative = root.relative_to(cwd).as_posix()
        except ValueError:
            if root.is_dir():
                scopes.append(Scope(cwd=root, paths=["."]))
            else:
                scopes.append(Scope(cwd=root.parent, paths=[root.name]))
            continue
        inside.paths.append(relative)
        # Explicitly named files bypass globs, so only directories need prefixes.
        if relative != "." and root.is_dir():
            inside.prefixes.append(relative)
    if inside.paths:
        scopes.insert(0, inside)
    return scopes


def _entry(scope_index: int, scope: Scope, native: bytes, count: int, cwd: Path) -> Entry:
    path = Path(os.path.normpath(os.path.join(scope.cwd, os.fsdecode(native))))
    return Entry(path, path_label(path, cwd), scope_index, native, count)


@dataclass
class _Found:
    """Everything the counting or listing passes found, over all scopes."""

    entries: list[Entry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    files_searched: int | None = None
    complete: bool = True
    # Paths that ignore files excluded from the walk.
    ignored: list[Path] = field(default_factory=list)

    def take(self, index: int, scope: Scope, result: ScanResult, cwd: Path) -> None:
        self.entries.extend(
            _entry(index, scope, path, count, cwd) for path, count in result.entries
        )
        self.warnings.extend(result.warnings)
        self.ignored.extend(result.ignored)
        if result.truncated:
            self.warnings.append(
                "The search stopped after 500000 files; narrow path or glob to see the rest."
            )
        if result.truncated or result.interrupted or result.warnings:
            self.complete = False
        if result.files_searched is not None:
            self.files_searched = (self.files_searched or 0) + result.files_searched

    def finish(self, query: SearchQuery) -> None:
        seen: set[str] = set()
        unique = []
        for entry in self.entries:
            key = os.path.normcase(entry.path)
            if key not in seen:
                seen.add(key)
                unique.append(entry)
        self.entries = unique
        order(self.entries, query)


def _list_directories(
    context: ToolContext,
    query: SearchQuery,
    roots: list[Path],
    cwd: Path,
    binary: Path,
    budget: SearchBudget,
    found: _Found,
) -> None:
    """List the directories below the roots that ripgrep's walker enters, empty ones too.

    ripgrep lists files only. Its walk reports the paths it skips (ignore rules,
    hidden paths, excluding globs), and the directories are walked here without
    them. With -t or -T, only directories holding a selected file are listed.
    --max-depth and the selecting globs apply to the directories themselves.
    """
    depth = None
    selection = []
    for argument in query.rg_args:
        if argument.startswith("--max-depth="):
            with contextlib.suppress(ValueError):
                depth = int(argument.removeprefix("--max-depth="))
        else:
            selection.append(argument)
    query.rg_args = selection
    files = _Found()
    skipped: set[str] = set()
    for index, scope in enumerate(_scopes(roots, cwd)):
        result = list_scan(binary, query, scope, context, budget, cwd, directories=True)
        files.take(index, scope, result, cwd)
        skipped.update(os.path.normcase(path) for path in result.skipped)
    found.warnings, found.complete, found.ignored = files.warnings, files.complete, files.ignored
    holding: set[str] | None = None
    if any(argument.startswith(("--type=", "--type-not=")) for argument in query.rg_args):
        holding = set()
        for entry in files.entries:
            for parent in entry.path.parents:
                key = os.path.normcase(parent)
                if key in holding:
                    break
                holding.add(key)
    follow = False
    case_sensitive = False
    for argument in query.rg_args:
        if argument in {"--follow", "--no-follow"}:
            follow = argument == "--follow"
        elif argument in {"--glob-case-insensitive", "--no-glob-case-insensitive"}:
            case_sensitive = argument.startswith("--no-")
    bases = [root for root in roots if root.is_dir()]
    directories: dict[str, Path] = {}
    for root in bases:
        for path in _walk_directories(root, skipped, depth, follow, budget):
            key = os.path.normcase(path)
            if holding is None or key in holding:
                directories.setdefault(key, path)
            if len(directories) >= MAX_ENTRIES:
                found.warnings.append(
                    f"The search stopped after {MAX_ENTRIES} directories; narrow path or glob "
                    "to see the rest."
                )
                found.complete = False
                break
    if budget.stopped:
        found.complete = False
    selected = [
        path
        for path in directories.values()
        if _directory_selected(path, bases, query.globs, cwd, case_sensitive)
    ]
    if query.name_patterns:
        names = _matching_names(context, query, [path.name for path in selected], binary, budget)
        selected = [path for path in selected if path.name in names]
    found.entries = [Entry(path, path_label(path, cwd), directory=True) for path in selected]


def _matching_names(
    context: ToolContext,
    query: SearchQuery,
    names: list[str],
    binary: Path,
    budget: SearchBudget,
) -> set[str]:
    """Match directory names as a content search matches lines, with the same pattern repairs."""
    unique = list(dict.fromkeys(names))
    try:
        return match_names(binary, query, query.name_patterns, unique, context, budget)
    except SearchRefusedError as error:
        retry = None if query.literal else pattern_retry(str(error), query.name_patterns, query)
        if retry is None:
            raise
        patterns, pcre2, note = retry
        if pcre2:
            query.rg_args.append("--pcre2")
        try:
            matched = match_names(binary, query, patterns, unique, context, budget)
        except SearchRefusedError:
            raise error from None
        query.notes.append(note)
        return matched


def _walk_directories(
    root: Path, skipped: set[str], depth: int | None, follow: bool, budget: SearchBudget
) -> Iterator[Path]:
    """Yield the directories below root that ripgrep's walker enters.

    Links, Windows junctions included, are entered only with --follow, as by
    ripgrep; a link back to a directory being walked is not entered again.
    Unreadable directories are left out, since ripgrep already warned about them.
    """
    pending = [(root, 0, frozenset({_identity(root)}))]
    while pending and budget.keep_going():
        directory, level, ancestors = pending.pop()
        if depth is not None and level >= depth:
            continue
        try:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name, reverse=True)
        except OSError:
            continue
        for child in children:
            path = Path(child.path)
            try:
                linked = child.is_symlink() or child.is_junction()
                if child.name == ".git" or not child.is_dir() or (linked and not follow):
                    continue
            except OSError:
                continue
            if os.path.normcase(path) in skipped:
                continue
            identity = _identity(path)
            if identity in ancestors:
                continue
            yield path
            pending.append((path, level + 1, ancestors | {identity}))


def _identity(path: Path) -> tuple[int, int] | str:
    try:
        status = os.stat(path)
    except OSError:
        return os.path.normcase(path)
    return (status.st_dev, status.st_ino)


def _directory_selected(
    path: Path, roots: list[Path], globs: list[str], cwd: Path, case_sensitive: bool
) -> bool:
    """Apply ordered globs to one directory as ripgrep applies them to files."""
    selected = all(glob.startswith("!") for glob in globs)
    names = []
    with contextlib.suppress(ValueError):
        names.append(path.relative_to(cwd).as_posix())
    names.extend(path.relative_to(root).as_posix() for root in roots if path.is_relative_to(root))
    for glob in globs:
        negated = glob.startswith("!")
        body = (glob[1:] if negated else glob).replace("\\", "/").rstrip("/")
        body = body[2:] if body.startswith("./") else body.lstrip("/")
        anchored = "/" in body
        for alternative in _expand_brace_alternations(body):
            if any(
                Path(name if anchored else name.rsplit("/", 1)[-1]).full_match(
                    alternative, case_sensitive=case_sensitive
                )
                for name in names
            ):
                selected = not negated
                break
    return selected


@dataclass
class _Outcome:
    page: Page
    found: _Found
    total: int
    lines_total: int
    patterns: list[str]


def _search(
    context: ToolContext,
    query: SearchQuery,
    roots: list[Path],
    cwd: Path,
    binary: Path,
    budget: SearchBudget,
) -> _Outcome:
    widen_type_case(binary, query, context, budget)
    found = _Found()
    if query.mode == "list_dirs":
        _list_directories(context, query, roots, cwd, binary, budget, found)
    scopes = _scopes(roots, cwd)
    if query.mode == "list_files":
        for index, scope in enumerate(scopes):
            found.take(index, scope, list_scan(binary, query, scope, context, budget, cwd), cwd)
    if query.mode in LIST_MODES:
        found.finish(query)
        total = len(found.entries)
        return _Outcome(entry_page(found.entries, query), found, total, total, [])

    patterns = query.patterns
    try:
        results = [
            count_scan(binary, query, patterns, scope, context, budget, cwd) for scope in scopes
        ]
    except SearchRefusedError as error:
        retry = None if query.literal else pattern_retry(str(error), patterns, query)
        if retry is None:
            raise
        patterns, pcre2, note = retry
        if pcre2:
            query.rg_args.append("--pcre2")
        try:
            results = [
                count_scan(binary, query, patterns, scope, context, budget, cwd) for scope in scopes
            ]
        except SearchRefusedError:
            raise error from None
        query.notes.append(note)
    for index, (scope, result) in enumerate(zip(scopes, results, strict=True)):
        found.take(index, scope, result, cwd)
    found.finish(query)
    lines_total = sum(entry.count for entry in found.entries)
    if query.mode != "content":
        page = entry_page(found.entries, query)
        return _Outcome(page, found, len(found.entries), lines_total, patterns)
    window = content_window(found.entries, query.offset, query.limit)
    events: dict[tuple[int, bytes], list[dict[str, Any]]] = {}
    for index, scope in enumerate(scopes):
        selected = [(entry, skip) for entry, skip in window if entry.scope == index]
        if not selected:
            continue
        scope_events, warnings = line_events(
            binary,
            query,
            patterns,
            scope,
            [entry.native for entry, _ in selected],
            max(skip for _, skip in selected) + query.limit,
            context,
            budget,
            cwd,
        )
        found.warnings.extend(warnings)
        events.update(((index, path), value) for path, value in scope_events.items())
    page = content_page(window, events, query)
    return _Outcome(page, found, lines_total, lines_total, patterns)


def search_files_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    try:
        query = interpret_search_call(arguments, edit_tool=offered_edit_tool(context))
        binary = require_binary()
        if query.mode == "help":
            return tool_success({"content": help_text()})
        budget = SearchBudget(context)
        if query.mode == "reference":
            text = reference_text(binary, query.reference_args, context, budget)
            return tool_success({"content": text})
        cwd = Path(os.path.abspath(context.effective_cwd.expanduser()))
        roots = _resolve_roots(query, cwd)
        missing = _missing_warnings(query, roots, cwd)
        if not roots.paths:
            return tool_failure(
                "path_not_found",
                " ".join(missing)
                + " Nothing was searched. Correct path, or omit it to search the working "
                "directory.",
            )
        outcome = _search(context, query, roots.paths, cwd, binary, budget)
    except (SearchArgumentError, ToolContractError, ValueError) as error:
        return tool_failure("invalid_arguments", str(error))
    except SearchRefusedError as error:
        return tool_failure("search_error", explain_failure(str(error)))
    except OSError as error:
        return tool_failure(
            "search_error",
            f"search_files could not run the search: {os_error_reason(error)}. Retry the call.",
        )
    if budget.cancelled_by_user or context.was_cancelled_by_user():
        return tool_failure("cancelled_by_user", "Search aborted by the user")
    page, found = outcome.page, outcome.found
    warnings = [*missing, *found.warnings]
    if budget.timed_out:
        warnings.append(
            "The search stopped at its 30-second limit, so results are partial. Narrow path "
            "or glob to search the rest."
        )
    elif budget.stopped:
        warnings.append("The Run was cancelled, so results are partial.")
    complete = found.complete and not budget.stopped
    if query.mode in {"files_without_match", *LIST_MODES}:
        files = len(found.entries)
    else:
        files = sum(1 for entry in found.entries if entry.count)
    data: dict[str, Any] = {
        "summary": summary(
            page,
            query,
            total=outcome.total,
            files=files,
            lines_total=outcome.lines_total,
            files_searched=found.files_searched,
            complete=complete,
        )
    }
    end = page.offset + page.returned
    if page.returned and end < outcome.total:
        data["next_offset"] = end
    if query.notes:
        data["note"] = " ".join(dict.fromkeys(query.notes))
    if warnings:
        data["warnings"] = warnings[:20]
    if roots.missing or not outcome.total:
        data["searched_paths"] = [path.as_posix() for path in roots.paths]
    if not outcome.total and outcome.patterns:
        data["patterns"] = outcome.patterns
    if found.ignored and (not outcome.total or query.mode in LIST_MODES):
        data["skipped"] = _ignored_summary(found.ignored, cwd)
    data["content"] = "\n".join(page.lines)
    context.add_display_count(
        page.returned, "results", at_least="next_offset" in data or not complete
    )
    return tool_success(data)


async def _search_files_async(context: ToolContext, arguments: JsonObject) -> JsonObject:
    return await run_tool_worker(search_files_handler, context, arguments)


def _display_parts(arguments: JsonObject) -> list[ToolDisplayPart]:
    parts = []
    for name in ("pattern", "path", "glob", "args"):
        value = arguments.get(name)
        values = value if isinstance(value, list) else [value]
        text = " ".join(item for item in values if isinstance(item, str) and item)
        if text:
            parts.append(ToolDisplayPart(text))
    return parts


def _display_details(arguments: JsonObject, result: JsonObject | None) -> list[JsonObject]:
    """Show the user what was found, whether more follows, and the warnings.

    Page controls and searched roots are for the Agent and stay in the raw result.
    """
    data = result.get("data") if isinstance(result, dict) and result.get("ok") is True else None
    if not isinstance(data, dict):
        return []
    blocks = [display_text("results", source="result", path=("data", "content"))]
    next_offset = data.get("next_offset")
    if isinstance(next_offset, int) and not isinstance(next_offset, bool):
        blocks.append(
            display_notice(
                "info", f"More results follow; the next page starts at result {next_offset + 1}."
            )
        )
    warnings = data.get("warnings")
    for warning in warnings if isinstance(warnings, list) else []:
        if isinstance(warning, str) and warning.strip():
            blocks.append(display_notice("warning", warning))
    return blocks


def register_search_files_tool(registry: ToolRegistry) -> None:
    available = True
    hint = None
    try:
        require_binary()
    except ValueError as error:
        available, hint = False, str(error)
    registry.register(
        SEARCH_FILES_TOOL_NAME,
        SEARCH_FILES_TOOL_DESCRIPTION,
        SEARCH_FILES_TOOL_PARAMETERS,
        _search_files_async,
        family="files",
        result_schema={"type": "object", "required": ["content"]},
        display=ToolDisplay(parts_builder=_display_parts, detail_builder=_display_details),
        parallel_safe=True,
        open_input_schema=True,
        argument_normalizer=normalize_search_arguments,
        ready=lambda: available,
        readiness_hint=hint,
    )


HELP_EXAMPLES = (
    {"pattern": "def load_config", "path": ["src", "tests"], "glob": "*.py", "context": 2},
    {"pattern": "todo", "args": ["-i", "-w", "-t", "py"], "output": "count"},
    {"pattern": "connect(", "args": ["-F"]},
    {"pattern": "error", "args": ["-u", "-i"], "output": "files"},
    {"glob": "migrations", "args": ["--dirs"]},
)


def help_text() -> str:
    """Return the reference that args ["--help"] shows."""
    examples = "\n".join(f"  {json.dumps(example)}" for example in HELP_EXAMPLES)
    return f"""search_files runs ripgrep over the files below path.

Examples:
{examples}

Default selection: hidden files are searched, .gitignore and .ignore rules apply (also
outside git repositories), binary files are skipped, and .git is always skipped. A
glob that names files also selects files those rules exclude. Globs and file types
match names in any letter case unless --no-glob-case-insensitive is given.

Output: content shows path:line:text for matching lines and path-line-text for context
lines. Content results are ordered by path, file lists newest first. Every page says
how many results exist; continue with next_offset.

args items, one flag or value per item. Other ripgrep flags work as well:
  -i / -s / -S          ignore case / match case / ignore case unless the pattern has capitals
  -F                    match the pattern as literal text
  -w / -x               match whole words / whole lines
  -v                    show lines that do not match
  -e PATTERN            another pattern; a line matching any pattern matches
  -U                    let a match span lines; add --multiline-dotall for . to match newlines
  -P                    PCRE2 regex: look-around and backreferences
  -A N / -B N / -C N    lines after / before / around each match
  -o                    show only the matched text
  -m N                  at most N matching lines per file
  -l / -c / --count-matches / --files-without-match
                        list matching files / count matching lines / count matches /
                        list files without a match
  -t TYPE / -T TYPE     only / not files of a type; --type-list lists the types
  -g GLOB               like glob; a leading ! excludes
  -u / -uuu             also search ignored files / and binary files
  --no-hidden           skip hidden files
  -L                    follow symbolic links
  -d N                  descend at most N directory levels
  --max-filesize SIZE   skip larger files, such as 1M
  -E ENCODING / -a      read files in an encoding / search binary files as text
  --files / --dirs      list files / list directories, empty directories included; with --dirs
                        a pattern matches directory names
  --sort KEY / --sortr KEY
                        order ascending / descending by path, modified, accessed or created
"""


_STRING_OR_LIST: JsonObject = {
    "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]
}
SEARCH_FILES_TOOL_DESCRIPTION = (
    "Search file contents with a regular expression, or list files and directories. Use this "
    "instead of grep, rg, find, or ls in the shell. "
    'Find text: {"pattern": "def load_config", "path": "src"}. '
    'List files: {"glob": "*.py", "path": "src"}. '
    'List directories: {"glob": "build", "args": ["--dirs"]}. '
    "Matches come back as path:line:text; file lists are newest first. Hidden files are "
    "included, .gitignore rules apply, and .git is skipped. Results come in pages; "
    "continue with next_offset."
)
SEARCH_FILES_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": (
                "Regular expression (ripgrep syntax) to find in file contents. Omit to list "
                'files. To match text containing ( [ . * literally, add "-F" to args.'
            ),
        },
        "path": {
            **_STRING_OR_LIST,
            "description": (
                "File or directory to search, or a list of them. Relative paths start at the "
                "working directory. Omit to search the working directory."
            ),
        },
        "glob": {
            **_STRING_OR_LIST,
            "description": (
                "File name filter, or directory name filter with --dirs, such as *.py or "
                "*.{ts,tsx}; a leading ! excludes. Without a / it matches names at any depth. "
                "Case-insensitive. A list applies each. Omit to include every name."
            ),
        },
        "output": {
            "type": "string",
            "enum": ["content", "files", "count"],
            "description": (
                "content shows matching lines; files lists only the matching "
                'files; count gives matching lines per file, or every match with "--count-matches" '
                "in args. Omit for content."
            ),
        },
        "context": {
            "type": "integer",
            "minimum": 0,
            "description": "Lines to show before and after each match. Omit to show only "
            "the matching lines.",
        },
        "args": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "More ripgrep arguments, one per item: -i ignore case, -F literal text, "
                "-w whole words, -t py file type, -u include ignored files, --dirs list "
                "directories. A plain ripgrep argument list also works: the first operand "
                'is the pattern, later ones are paths. ["--help"] lists every option.'
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "default": 100,
            "description": "Maximum results per page. Omit for 100.",
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Results to skip; pass next_offset to get the next page. Omit to start at the "
                "first result."
            ),
        },
    },
}
