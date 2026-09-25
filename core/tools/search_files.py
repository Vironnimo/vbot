"""Unified file and content search with a private native regex engine."""

from __future__ import annotations

import contextlib
import itertools
import os
import re
import tempfile
from functools import cache
from pathlib import Path
from typing import Any

from core.tools._argument_repair import normalize_call_arguments
from core.tools._search_arguments import parse_search_args
from core.tools._search_execution import content_events, file_types, validate_patterns
from core.tools._search_options import SearchOptions, help_text, parse_options
from core.tools._search_results import ResultPage, path_label, render_events
from core.tools._search_selection import FileSelection
from core.tools._tool_context import _path_argument
from core.tools.arguments import optional_int
from core.tools.contracts import _load_json_value, compile_tool_contract
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
    """Decode list syntax without turning a malformed list into a regex class."""
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


@cache
def _repair_contract():
    return compile_tool_contract(
        name=SEARCH_FILES_TOOL_NAME,
        input_schema=SEARCH_FILES_TOOL_PARAMETERS,
        require_closed_input=False,
    )


def normalize_search_arguments(arguments: Any) -> Any:
    return normalize_call_arguments(
        _repair_contract(),
        arguments,
        field_aliases={"argv": "args"},
        field_normalizers={"args": _repair_argument_array},
    )


def _strings(arguments: JsonObject, name: str, default: list[str]) -> list[str]:
    value = arguments.get(name, default)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and "\x00" not in item for item in value
    ):
        raise ValueError(f"{name} must be an array of strings containing no NUL characters.")
    return value


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


def search_files_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    try:
        arguments = normalize_search_arguments(arguments)
        if not isinstance(arguments, dict):
            raise ValueError("Provide one search argument object.")
        query = parse_search_args(_strings(arguments, "args", []))
        action, kind = query["action"], query["kind"]
        patterns = query["patterns"]
        options = parse_options(query["options"], action=action, kind=kind)
        reference = options.get("reference")
        if reference == "help":
            if (
                set(arguments) - {"args"}
                or patterns
                or query["paths"]
                or any(option.key != "reference" for option, _ in options.entries)
            ):
                raise ValueError("--help does not search; omit patterns, paths, and other options.")
            return tool_success({"content": help_text()})
        roots_input = query["paths"] or [str(context.effective_cwd)]
        cwd = Path(os.path.abspath(context.effective_cwd.expanduser()))
        roots = list(
            dict.fromkeys(
                Path(
                    os.path.abspath(
                        cwd / Path(_path_argument(p, windows=os.name == "nt")).expanduser()
                    )
                )
                for p in roots_input
            )
        )
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
                warnings = [
                    f"Search root not found: {root.as_posix()}. Correct this path."
                    for root in missing_roots
                ]
                if action == "content":
                    warnings.insert(
                        0,
                        "Operands after the first pattern are search paths. If a missing path "
                        "was intended as another pattern, pass each pattern with -e.",
                    )
                roots = [root for root in roots if root not in missing_roots]
                if not roots:
                    return tool_failure(
                        "path_not_found", "No roots were searched. " + " ".join(warnings)
                    )
        binary = require_binary()
    except (OSError, RuntimeError, ValueError) as error:
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
            if (
                set(arguments) - {"args"}
                or patterns
                or query["paths"]
                or any(
                    o.key not in {"type_add", "type_clear", "reference"} for o, _ in options.entries
                )
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
                    entries = selection.entries(action=action)
                    first = next(entries, None)
                    if first is None:
                        # The native search reports invalid patterns and options;
                        # without candidates it never runs, so check them alone.
                        empty = scratch / "empty"
                        empty.touch()
                        validate_patterns(binary, patterns, options, context, budget, empty)
                    else:
                        render_events(
                            content_events(
                                binary,
                                itertools.chain([first], entries),
                                patterns,
                                options,
                                context,
                                budget,
                                offset + limit,
                            ),
                            page,
                            options,
                            cwd,
                        )
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
    values = arguments.get("args")
    if isinstance(values, list):
        return [ToolDisplayPart(" ".join(value for value in values if isinstance(value, str)))]
    return []


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


SEARCH_FILES_TOOL_DESCRIPTION = (
    "Search file contents or list files and directories. Use this instead of shell "
    "commands for searching or listing paths; accepts ripgrep arguments. "
    'Content: ["-F","computeDamage(","src"]. Files: ["--files","-g","*.py","src"]. '
    "Use --dirs for directories (including empty ones), or --entries for both. "
    "Name filters use -g, case-insensitive by default; globs without / match at any depth. "
    "-i/-s controls case for content or path discovery. "
    "Normal output includes paths and line numbers. -l returns matching files; "
    "-c counts matching lines; --count-matches counts occurrences. "
    "Hidden entries are included; ignore rules apply unless -u is given; .git is excluded. "
    "Explicit ignored roots are searched. Content is ordered by path, discovery by newest "
    "modification. Use --help for the full supported options."
)
SEARCH_FILES_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "args": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "One ripgrep argument per item, without shell quoting or a shell command. "
                "Regex pattern first, then literal search roots; -F selects literal text. "
                "Repeat -e for multiple patterns. With --files/--dirs/--entries, all operands "
                "are roots. Relative search paths resolve against the working directory. "
                "Use absolute paths to search elsewhere. "
                "Omit paths to search the working directory."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "default": 100,
            "description": "Maximum results. Omit for 100.",
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "Results to skip. Omit for the first page; use next_offset to continue.",
        },
    },
    "required": ["args"],
}
