"""File and content search: one Tool over the bundled ripgrep.

ripgrep selects and matches files; this module turns a call into a ripgrep
query, runs it in bounded phases, and renders pages the Agent can continue.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from core.tools._search_arguments import (
    interpret_search_call as interpret_search_call,
)
from core.tools._search_arguments import (
    normalize_search_arguments as normalize_search_arguments,
)
from core.tools._search_definition import (
    HELP_EXAMPLES as HELP_EXAMPLES,
)
from core.tools._search_definition import (
    SEARCH_FILES_TOOL_DESCRIPTION as SEARCH_FILES_TOOL_DESCRIPTION,
)
from core.tools._search_definition import (
    SEARCH_FILES_TOOL_NAME as SEARCH_FILES_TOOL_NAME,
)
from core.tools._search_definition import (
    SEARCH_FILES_TOOL_PARAMETERS as SEARCH_FILES_TOOL_PARAMETERS,
)
from core.tools._search_definition import (
    help_text as help_text,
)
from core.tools._search_execution import (
    MAX_CHILD_MEMORY,
    MAX_ENTRIES,
    MAX_PROTOCOL_LINE,
    MAX_SCAN_BYTES,
    ScanResult,
    Scope,
    SearchBoundError,
    SearchRefusedError,
    count_scan,
    explain_failure,
    ignored_scan,
    line_events,
    list_scan,
    match_names,
    pattern_retry,
    reference_text,
    widen_type_case,
)
from core.tools._search_hints import (
    can_miss_literal_text,
    can_span_lines,
    file_name_extensions,
    finds_fixed_text,
    literal_text_note,
    match_count_note,
    repaired_glob_note,
    spanning_note,
    unnamed_files_note,
)
from core.tools._search_query import (
    LIST_MODES,
    SearchArgumentError,
    SearchQuery,
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
from core.tools._search_roots import (
    _directory_globs,
    _missing_warnings,
    _nothing_searched,
    _resolve_roots,
)
from core.tools.contracts import ToolContractError
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


def _mebibytes(size: int) -> str:
    return f"{size // (1024 * 1024)} MiB"


def _bound_failure(error: SearchBoundError, query: SearchQuery, cwd: Path) -> str:
    """Say which bound stopped the search and which call gets results.

    Only the line pass prints records that large, one per matching or context
    line (or match, with -U), so it names the file and how to leave it out. The
    memory bound depends on how many and how large the files are, and on -U.
    """
    stopped = "The search stopped without results"
    if error.bound == "record":
        limit = f"the {_mebibytes(MAX_PROTOCOL_LINE)} limit"
        if error.path is None:
            return f"{stopped}: one result exceeds {limit}. Narrow path or glob."
        # A glob with a slash selects that path below the working directory; a name
        # selects it at any depth, also below a root outside the working directory.
        if error.path.is_relative_to(cwd):
            excluded = "!" + error.path.relative_to(cwd).as_posix()
        else:
            excluded = "!" + error.path.name
        unit = "match" if query.multiline else "line"
        return (
            f"{stopped}: a {unit} in {path_label(error.path, cwd)} exceeds {limit} for one "
            f"result. Exclude that file with glob {json.dumps(excluded, ensure_ascii=False)} to "
            'see the other results, or set output to "files" or "count", which show no lines.'
        )
    limit = f"its {_mebibytes(MAX_CHILD_MEMORY)} memory limit"
    if query.mode in LIST_MODES:
        listed = "files" if query.mode == "list_files" else "directories"
        return (
            f"The listing stopped without results: it exceeded {limit}. Narrow path or glob "
            f"to list fewer {listed}."
        )
    text = (
        f'{stopped}: it exceeded {limit}. Narrow path or glob, or add "--max-filesize", "50M" '
        "to args to skip files over 50M."
    )
    if query.multiline:
        text += " Leave out -U unless matches need to span lines."
    return text


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
        if result.stopped_by is not None:
            reason = ""
            if result.stopped_by == "output":
                reason = f", when their paths reached the {_mebibytes(MAX_SCAN_BYTES)} output limit"
            self.warnings.append(
                f"The search stopped after {len(result.entries)} files{reason}; narrow path or "
                "glob to see the rest."
            )
        if result.stopped_by is not None or result.interrupted or result.warnings:
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
    """Match listed names as a content search matches lines, with the same pattern repairs."""
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


def _restart_past_the_end(query: SearchQuery, total: int) -> None:
    """Show the first page when offset passes the last result, as after a changed query."""
    if total and query.offset >= total:
        query.notes.append(
            f"offset {query.offset} is past the last result, so the first page is shown."
        )
        query.offset = 0


@dataclass
class _Outcome:
    page: Page
    found: _Found
    total: int
    lines_total: int


def _names_matching(
    context: ToolContext,
    query: SearchQuery,
    scopes: list[Scope],
    patterns: list[str],
    cwd: Path,
    binary: Path,
    budget: SearchBudget,
) -> tuple[list[Entry], _Found]:
    """Return the searched files whose name or shown path the patterns match.

    The second value is the listing they were taken from; its ``ignored`` names
    the paths ignore files excluded, which are the ones they excluded from the search.
    """
    listed = _Found()
    for index, scope in enumerate(scopes):
        listed.take(index, scope, list_scan(binary, query, scope, context, budget, cwd), cwd)
    listed.finish(query)
    names = [entry.label for entry in listed.entries]
    names += [entry.path.name for entry in listed.entries]
    try:
        matched = match_names(binary, query, patterns, names, context, budget)
    except SearchRefusedError:
        # Unmatched names were not checked, so no claim about them holds.
        listed.complete = False
        return [], listed
    named = [
        entry for entry in listed.entries if entry.label in matched or entry.path.name in matched
    ]
    return named, listed


# Shown names of files whose names match a content pattern, before "and N more".
_NAMED_FILES_SHOWN = 5


def _note_named_files(
    context: ToolContext,
    query: SearchQuery,
    scopes: list[Scope],
    cwd: Path,
    binary: Path,
    budget: SearchBudget,
) -> None:
    """Name the files a file-list search's pattern matches by name, as the Agent may mean them.

    Without such files, a pattern naming an extension the searched files have
    states that none has a matching name; other patterns get no note.
    """
    patterns = query.patterns
    if len(patterns) != 1 or "--invert-match" in query.rg_args:
        return
    extensions = file_name_extensions(patterns[0])
    if extensions is None:
        return
    named, listed = _names_matching(context, query, scopes, patterns, cwd, binary, budget)
    if budget.stopped or not listed.complete:
        return
    if named:
        shown = ", ".join(entry.label for entry in named[:_NAMED_FILES_SHOWN])
        rest = len(named) - _NAMED_FILES_SHOWN
        more = f", and {rest} more (add --files to args to list them)" if rest > 0 else ""
        query.notes.append(
            f"The listed files contain text matching pattern; files whose names match it: "
            f"{shown}{more}."
        )
        return
    suffixes = {entry.path.suffix.casefold() for entry in listed.entries}
    if any(f".{extension}" in suffixes for extension in extensions):
        query.notes.append(
            "The listed files contain text matching pattern; no file name matches it."
        )


def _recount(
    context: ToolContext,
    query: SearchQuery,
    scopes: list[Scope],
    patterns: list[str],
    cwd: Path,
    binary: Path,
    budget: SearchBudget,
) -> _Found | None:
    """Count a variant of the query for a note; ``None`` when that count did not finish."""
    found = _Found()
    try:
        for index, scope in enumerate(scopes):
            result = count_scan(binary, query, patterns, scope, context, budget, cwd)
            found.take(index, scope, result, cwd)
    except SearchRefusedError, SearchBoundError:
        return None
    if not found.complete or budget.stopped:
        return None
    found.finish(query)
    return found


def _note_no_match(
    context: ToolContext,
    query: SearchQuery,
    scopes: list[Scope],
    patterns: list[str],
    listed: _Found,
    cwd: Path,
    binary: Path,
    budget: SearchBudget,
    repair_note: str | None = None,
) -> None:
    """Point a pattern that matched neither contents nor names to what it may mean.

    A regex whose text occurs literally names -F, and one that matches only
    across lines names -U. Otherwise a pattern shaped like file names says that
    no name matches it either, and how to list similar names; ``listed`` is the
    listing the names were taken from.
    """
    if len(patterns) != 1 or "--invert-match" in query.rg_args:
        return
    if (
        repair_note is not None
        and query.mode in {"content", "files_with_matches"}
        and not (query.before or query.after or query.only_matching)
        and listed.complete
        and not budget.stopped
    ):
        filtered = bool(query.globs) or any(arg.startswith("--iglob=") for arg in query.rg_args)
        note = repaired_glob_note(query.patterns[0], patterns[0], filtered=filtered)
        if note is not None:
            query.notes[query.notes.index(repair_note)] = note
            return
    # A repaired pattern's note already says how it was read.
    if patterns is query.patterns and not query.literal and can_miss_literal_text(patterns[0]):
        literal = replace(query, rg_args=[*query.rg_args, "--fixed-strings"], literal=True)
        found = _recount(context, literal, scopes, patterns, cwd, binary, budget)
        files = sum(1 for entry in found.entries if entry.count) if found else 0
        if files:
            query.notes.append(literal_text_note(patterns[0], files))
            return
    if patterns is query.patterns and not query.multiline and can_span_lines(patterns[0]):
        spanning = replace(query, rg_args=[*query.rg_args, "--multiline"])
        found = _recount(context, spanning, scopes, patterns, cwd, binary, budget)
        files = sum(1 for entry in found.entries if entry.count) if found else 0
        if files:
            query.notes.append(spanning_note(files))
            return
    if query.mode not in {"content", "files_with_matches"} or not listed.complete:
        return
    if budget.stopped:
        return
    note = unnamed_files_note(
        patterns[0],
        file_list=query.mode == "files_with_matches",
        files=[(entry.label, entry.path.name) for entry in listed.entries],
    )
    if note is not None:
        query.notes.append(note)


def _note_match_count(
    context: ToolContext,
    query: SearchQuery,
    scopes: list[Scope],
    patterns: list[str],
    lines: int,
    cwd: Path,
    binary: Path,
    budget: SearchBudget,
) -> None:
    """Say how many matches the counted lines hold when some line holds several.

    Only text the Agent can mean to count occurrences of qualifies: fixed text,
    not a shape such as ``.`` or ``\\w+``.
    """
    if query.count_matches or query.multiline or "--invert-match" in query.rg_args:
        return
    if not query.literal and not all(finds_fixed_text(pattern) for pattern in patterns):
        return
    matching = replace(query, count_matches=True)
    found = _recount(context, matching, scopes, patterns, cwd, binary, budget)
    matches = sum(entry.count for entry in found.entries) if found else 0
    if matches > lines:
        query.notes.append(match_count_note(matches))


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
        if query.name_patterns:
            # Like a content search's name fallback, a pattern matches a name or a shown path.
            names = [entry.label for entry in found.entries]
            names += [entry.path.name for entry in found.entries]
            matched = _matching_names(context, query, names, binary, budget)
            found.entries = [
                entry
                for entry in found.entries
                if entry.label in matched or entry.path.name in matched
            ]
    if query.mode in LIST_MODES:
        found.finish(query)
        total = len(found.entries)
        _restart_past_the_end(query, total)
        return _Outcome(entry_page(found.entries, query), found, total, total)

    patterns = query.patterns
    repair_note = None
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
        repair_note = note
    for index, (scope, result) in enumerate(zip(scopes, results, strict=True)):
        found.take(index, scope, result, cwd)
    found.finish(query)
    lines_total = sum(entry.count for entry in found.entries)
    if (
        not lines_total
        and query.mode in {"content", "files_with_matches", "count"}
        and found.complete
        and not budget.stopped
    ):
        # No content matches, so a pattern that matches file names was meant for them.
        named, listed = _names_matching(context, query, scopes, patterns, cwd, binary, budget)
        found.ignored = listed.ignored
        if named:
            query.mode = "list_files"
            query.notes.append(
                "No file contents match pattern, so the files whose names match it were listed."
            )
            found.entries = named
            _restart_past_the_end(query, len(named))
            return _Outcome(entry_page(named, query), found, len(named), len(named))
        _note_no_match(context, query, scopes, patterns, listed, cwd, binary, budget, repair_note)
    elif not found.entries:
        # Only a result without entries names the paths ignore files excluded.
        for scope in scopes:
            found.ignored.extend(ignored_scan(binary, query, scope, context, budget))
    if query.mode != "content":
        _restart_past_the_end(query, len(found.entries))
        if lines_total and not query.offset and found.complete and not budget.stopped:
            if query.mode == "files_with_matches":
                _note_named_files(context, query, scopes, cwd, binary, budget)
            elif query.mode == "count":
                _note_match_count(
                    context, query, scopes, patterns, lines_total, cwd, binary, budget
                )
        page = entry_page(found.entries, query)
        return _Outcome(page, found, len(found.entries), lines_total)
    _restart_past_the_end(query, lines_total)
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
    return _Outcome(page, found, lines_total, lines_total)


def search_files_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    try:
        query = interpret_search_call(arguments, edit_tool=offered_edit_tool(context))
        binary = require_binary()
        if query.mode == "help":
            return tool_success({"content": help_text()})
        budget = SearchBudget(context)
        cwd = Path(os.path.abspath(context.effective_cwd.expanduser()))
        if query.mode == "reference":
            text = reference_text(binary, query.reference_args, context, budget)
            return tool_success({"content": text})
        try:
            roots = _resolve_roots(query, cwd)
        except OSError as error:
            target = path_label(Path(error.filename), cwd) if error.filename else "the search paths"
            return tool_failure(
                "search_error",
                f"Nothing was searched. Could not check {target}: {os_error_reason(error)}. "
                "Check access to this path and retry.",
            )
        missing = _missing_warnings(query, roots, cwd, context.offers, arguments.get("args"))
        if not roots.paths:
            return tool_failure("path_not_found", _nothing_searched(missing, roots, query))
        _directory_globs(query, roots.paths, cwd)
        outcome = _search(context, query, roots.paths, cwd, binary, budget)
    except (SearchArgumentError, ToolContractError, ValueError) as error:
        return tool_failure("invalid_arguments", str(error))
    except SearchRefusedError as error:
        patterns = query.patterns or query.name_patterns
        return tool_failure("search_error", explain_failure(str(error), patterns))
    except SearchBoundError as error:
        # Only native runs raise it, and they start after query and cwd are set.
        return tool_failure("search_error", _bound_failure(error, query, cwd))
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
    if roots.missing:
        data["searched_paths"] = [path_label(path, cwd) for path in roots.paths]
    if found.ignored and (not outcome.total or query.mode in LIST_MODES):
        data["skipped"] = _ignored_summary(found.ignored, cwd)
    if page.lines:
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
        summary="Search file contents or find files by name.",
        family="files",
        result_schema={"type": "object"},
        display=ToolDisplay(parts_builder=_display_parts, detail_builder=_display_details),
        parallel_safe=True,
        open_input_schema=True,
        argument_normalizer=normalize_search_arguments,
        ready=lambda: available,
        readiness_hint=hint,
    )
