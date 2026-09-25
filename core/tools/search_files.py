"""Unified file and content search with a private native regex engine."""

from __future__ import annotations

import contextlib
import itertools
import os
import re
import shlex
import tempfile
from functools import cache
from pathlib import Path
from typing import Any

from core.tools._argument_repair import normalize_call_arguments
from core.tools._field_aliases import SpellingAliases as _SpellingAliases
from core.tools._field_aliases import spelling as _spelling
from core.tools._path_suggestions import corrected_paths
from core.tools._search_arguments import parse_search_args
from core.tools._search_execution import (
    content_events,
    file_types,
    pattern_retry,
    validate_patterns,
)
from core.tools._search_options import SearchOptions, help_text, parse_options
from core.tools._search_results import ResultPage, path_label, render_events
from core.tools._search_selection import FileSelection
from core.tools._tool_context import _path_argument
from core.tools.arguments import optional_int
from core.tools.contracts import ToolContractError, _load_json_value, compile_tool_contract
from core.tools.search import SearchBudget
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
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
        field_normalizers={"args": _repair_argument_array, "output": _normalize_output},
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
        raise ValueError(f"{name} must be an array of strings containing no NUL characters.")
    return value


_GLOB_SHAPE = re.compile(r"[^\s()|^$+\\]+")
_LISTING_BLOCKERS = {"literal", "glob", "context", "before", "after", "output", "quiet", "only"}


def _glob_shaped(pattern: str) -> bool:
    """Whether a pattern can only be meant as a file name glob such as *.py."""
    return bool(_GLOB_SHAPE.fullmatch(pattern)) and (
        pattern.startswith("*") or "**" in pattern or "/*." in pattern
    )


def interpret_search_call(arguments: Any) -> dict[str, Any]:
    """Interpret one call's named fields and args as a single ripgrep query.

    Returns the normalized arguments with the query's action, kind, patterns,
    paths, option tokens, and notes that explain reinterpretations.
    """
    arguments = normalize_search_arguments(arguments)
    if not isinstance(arguments, dict):
        raise ValueError("Provide one search argument object.")
    pattern = arguments.get("pattern")
    if pattern is not None and not isinstance(pattern, str):
        raise ValueError("pattern must be a string.")
    query = parse_search_args(
        _strings(arguments, "args"),
        patterns=[pattern] if pattern else [],
        roots=_strings(arguments, "path"),
    )
    query["options"] = [
        *(token for glob in _strings(arguments, "glob") for token in ("-g", glob)),
        *query["options"],
    ]
    output = arguments.get("output", "content")
    context_lines = optional_int(arguments.get("context"), field_name="context", minimum=0)
    notes: list[str] = []
    patterns = query["patterns"]
    if (
        query["action"] == "content"
        and len(patterns) == 1
        and _glob_shaped(patterns[0])
        and output != "count"
        and not context_lines
    ):
        listing = [*query["options"], "-g", patterns[0]]
        try:
            blocked = any(
                option.key in _LISTING_BLOCKERS
                for option, _ in parse_options(listing, action="paths", kind="files").entries[:-1]
            )
        except ValueError:
            blocked = True
        if not blocked:
            notes.append(
                f'pattern "{patterns[0]}" is a file name glob, so matching files were '
                "listed. To search file contents, put a regex in pattern and the file "
                "filter in glob."
            )
            query.update(action="paths", kind="files", patterns=[], options=listing)
    if query["action"] == "content":
        extra = {"files": ["-l"], "count": ["-c"]}.get(output, [])
        query["options"] = [*extra, *query["options"]]
        if context_lines:
            query["options"] = ["-C", str(context_lines), *query["options"]]
    elif output == "count":
        raise ValueError(
            'output "count" counts matching lines per file and needs a pattern; '
            "omit output to list files."
        )
    elif context_lines:
        raise ValueError(
            "context shows lines around content matches and needs a pattern; "
            "omit context to list files."
        )
    query["arguments"] = arguments
    query["notes"] = notes
    return query


def _page_argument(
    arguments: JsonObject,
    options: SearchOptions,
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    values = [int(value) for value in options.values(name)]
    if name in arguments:
        values.append(
            optional_int(
                arguments[name],
                field_name=name,
                default=default,
                minimum=minimum,
                maximum=maximum,
            )
        )
    if len(set(values)) > 1:
        raise ValueError(f"Conflicting {name} values; provide one intended page.")
    return optional_int(
        values[0] if values else None,
        field_name=name,
        default=default,
        minimum=minimum,
        maximum=maximum,
    )


def _missing_root_message(root: Path, cwd: Path) -> str:
    message = f"Path not found: {path_label(root, cwd)}"
    suggestions = corrected_paths(root, cwd)
    if suggestions:
        message += f" (similar: {', '.join(path_label(path, cwd) for path in suggestions)})"
    return message + "."


def search_files_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    try:
        query = interpret_search_call(arguments)
        arguments = query["arguments"]
        action, kind = query["action"], query["kind"]
        patterns = query["patterns"]
        notes: list[str] = query["notes"]
        options = parse_options(query["options"], action=action, kind=kind)
        reference = options.get("reference")
        searching = set(arguments) - {"args"} or patterns or query["paths"]
        if reference == "help":
            if searching or any(option.key != "reference" for option, _ in options.entries):
                raise ValueError("--help does not search; omit patterns, paths, and other options.")
            return tool_success({"content": help_text()})
        roots_input = query["paths"] or [str(context.effective_cwd)]
        cwd = Path(os.path.abspath(context.effective_cwd.expanduser()))
        requested: dict[Path, str] = {}
        for raw in roots_input:
            resolved = cwd / Path(_path_argument(raw, windows=os.name == "nt")).expanduser()
            requested.setdefault(Path(os.path.abspath(resolved)), raw)
        roots = list(requested)
        limit = _page_argument(arguments, options, "limit", default=100, minimum=1, maximum=10000)
        offset = _page_argument(arguments, options, "offset", default=0, minimum=0, maximum=1000000)
        if max(options.context) > 10000:
            raise ValueError(
                "Context is limited to 10000 lines per side; use read for larger file sections."
            )
        if options.enabled("quiet") and any(
            name in arguments or options.values(name) for name in ("limit", "offset")
        ):
            raise ValueError("Existence searches (-q) do not paginate; omit limit and offset.")
        missing_roots: list[Path] = []
        warnings: list[str] = []
        if reference != "types":
            for root in roots:
                if not root.exists():
                    missing_roots.append(root)
                elif not root.is_file() and not root.is_dir():
                    return tool_failure(
                        "invalid_arguments",
                        f"Search root is not a regular file or directory: {root.as_posix()}",
                    )
            if missing_roots:
                warnings = [_missing_root_message(root, cwd) for root in missing_roots]
                if query["pattern_operand"]:
                    other = requested[missing_roots[0]]
                    combined = (
                        f'args ["-F", "-e", "{patterns[0]}", "-e", "{other}"]'
                        if options.enabled("literal")
                        else f'pattern "{patterns[0]}|{other}"'
                    )
                    warnings.append(
                        f'The first args operand "{patterns[0]}" is the pattern and later '
                        "operands are paths. If a missing path was meant as another pattern, "
                        f"search both with {combined}."
                    )
                roots = [root for root in roots if root not in missing_roots]
                if not roots:
                    return tool_failure(
                        "path_not_found", " ".join(warnings) + " Nothing was searched."
                    )
        binary = require_binary()
    except (OSError, RuntimeError, ValueError, ToolContractError) as error:
        return tool_failure("invalid_arguments", str(error))
    budget = SearchBudget(context)
    page = ResultPage(offset, limit)
    complete = True
    try:
        types = (
            file_types(binary, options, context, budget)
            if reference == "types"
            or any(
                o.key in {"type", "type_not", "type_add", "type_clear"} for o, _ in options.entries
            )
            else {}
        )
        if reference == "types":
            if searching or any(
                o.key not in {"type_add", "type_clear", "reference"} for o, _ in options.entries
            ):
                raise ValueError(
                    "--type-list accepts only type additions/clears; "
                    "omit search fields and filters."
                )
            return tool_success(
                {
                    "content": "\n".join(
                        f"{name}: {', '.join(globs)}" for name, globs in types.items()
                    )
                }
            )
        with tempfile.TemporaryDirectory(prefix="vbot-search-") as temporary:
            scratch = Path(temporary)
            selection = FileSelection(
                scratch / "selection.sqlite", roots, cwd, options, budget, warnings
            )
            with contextlib.closing(selection):
                selection.populate(
                    kind if action == "paths" else "files",
                    types,
                )
                complete = selection.complete and not warnings
                if action == "paths":
                    for path, directory in selection.entries(action=action):
                        if not budget.keep_going():
                            break
                        page.add([path_label(path, cwd) + ("/" if directory else "")])
                        if page.more:
                            break
                else:

                    def search_contents(active: list[str], active_options: SearchOptions) -> None:
                        entries = selection.entries(action=action)
                        first = next(entries, None)
                        if first is None:
                            # The native search reports invalid patterns and options;
                            # without candidates it never runs, so check them alone.
                            empty = scratch / "empty"
                            empty.touch()
                            validate_patterns(
                                binary, active, active_options, context, budget, empty
                            )
                        else:
                            render_events(
                                content_events(
                                    binary,
                                    itertools.chain([first], entries),
                                    active,
                                    active_options,
                                    context,
                                    budget,
                                    offset + limit,
                                ),
                                page,
                                active_options,
                                cwd,
                            )

                    try:
                        search_contents(patterns, options)
                    except RuntimeError as error:
                        retry = (
                            None if page.observed else pattern_retry(str(error), patterns, options)
                        )
                        if retry is None:
                            raise
                        retry_patterns, pcre2, note = retry
                        retry_options = (
                            parse_options([*query["options"], "-P"], action=action, kind=kind)
                            if pcre2
                            else options
                        )
                        try:
                            search_contents(retry_patterns, retry_options)
                        except RuntimeError:
                            # A retry that found nothing reports the original problem.
                            if page.observed:
                                raise
                            raise error from None
                        patterns, options = retry_patterns, retry_options
                        notes.append(note)
                if options.enabled("debug"):
                    warnings.append(
                        f"Inspected {selection.observed} entries; "
                        f"{selection.skipped} traversal issues. Roots: {', '.join(map(str, roots))}"
                    )
                stats = {"entries_inspected": selection.observed, "results_observed": page.observed}
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))
    except (OSError, RuntimeError) as error:
        if not page.observed:
            return tool_failure("search_error", str(error))
        warnings.append(str(error))
        complete = False
    if budget.cancelled_by_user or context.was_cancelled_by_user():
        return tool_failure("cancelled_by_user", "Search aborted by the user")
    if budget.stopped:
        complete = False
        warnings.append(
            "Search timed out; narrow paths or filters and retry."
            if budget.timed_out
            else "Run cancelled; search results are incomplete."
        )
    if options.get("max_count") or options.enabled("stop"):
        warnings.append(
            "Per-file early stopping was requested; counts and absence apply o"
            "nly to the searched portions."
        )
    data = page.data(complete=complete, warnings=warnings, quiet=options.enabled("quiet"))
    if notes:
        data["note"] = " ".join(notes)
    if missing_roots:
        data["missing_paths"] = [root.as_posix() for root in missing_roots]
        data["searched_paths"] = [root.as_posix() for root in roots]
    if not page.observed and not page.matched:
        data["searched_paths"] = [root.as_posix() for root in roots]
        data["patterns"] = patterns
    if options.enabled("stats"):
        data["stats"] = locals().get("stats", {"results_observed": page.observed})
    context.add_display_count(page.returned, "results", at_least=page.more or not complete)
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
        display=ToolDisplay(parts_builder=_display_parts),
        parallel_safe=True,
        open_input_schema=True,
        argument_normalizer=normalize_search_arguments,
        ready=lambda: available,
        readiness_hint=hint,
    )


_STRING_OR_LIST: JsonObject = {
    "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]
}
SEARCH_FILES_TOOL_DESCRIPTION = (
    "Search file contents with a regular expression, or list files. Use this instead of "
    "grep, rg, find, or ls in the shell. "
    'Find text: {"pattern": "def load_config", "path": "src"}. '
    'List files: {"glob": "*.py", "path": "src"}. '
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
                "Regular expression (ripgrep syntax) to find in file contents. Omit it to "
                'list files. To match text containing ( [ . * literally, add "-F" to args.'
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
                "File name filter such as *.py or *.{ts,tsx}; a leading ! excludes. Without "
                "a / it matches names at any depth. Case-insensitive. A list applies each."
            ),
        },
        "output": {
            "type": "string",
            "enum": ["content", "files", "count"],
            "description": (
                "content (default) shows matching lines; files lists only the matching "
                "files; count gives matching lines per file."
            ),
        },
        "context": {
            "type": "integer",
            "minimum": 0,
            "description": "Lines to show before and after each match.",
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
            "description": "Results to skip; pass next_offset to get the next page.",
        },
    },
}
