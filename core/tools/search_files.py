"""Unified file and content search with a private native regex engine."""

from __future__ import annotations

import contextlib
import os
import tempfile
from functools import cache
from pathlib import Path
from typing import Any

from core.tools._argument_repair import normalize_call_arguments
from core.tools._search_execution import content_events, file_types, validate_patterns
from core.tools._search_options import BY_NAME, help_text, parse_options
from core.tools._search_results import ResultPage, path_label, render_events
from core.tools._search_selection import FileSelection
from core.tools._tool_context import _path_argument
from core.tools.arguments import optional_bool, optional_int
from core.tools.contracts import compile_tool_contract
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


@cache
def _repair_contract():
    properties = dict(SEARCH_FILES_TOOL_PARAMETERS["properties"])
    for field in ("ignore_case", "literal", "include_ignored", "multiline"):
        properties[field] = {"type": "boolean"}
    properties["context"] = {"type": "integer"}
    for name, option in BY_NAME.items():
        if not option.argument:
            properties[name] = {"type": "boolean"}
    return compile_tool_contract(
        name=SEARCH_FILES_TOOL_NAME,
        input_schema={**SEARCH_FILES_TOOL_PARAMETERS, "properties": properties},
        require_closed_input=False,
    )


def normalize_search_arguments(arguments: Any) -> Any:
    value = normalize_call_arguments(
        _repair_contract(),
        arguments,
        enum_fields=("action", "kind"),
        field_aliases={
            "pattern": "patterns",
            "path": "paths",
            "args": "options",
            "head": "limit",
            "head_limit": "limit",
        },
    )
    if not isinstance(value, dict):
        return value
    tokens = value.get("options", [])
    if not isinstance(tokens, list) or not all(isinstance(t, str) for t in tokens):
        return value
    tokens = list(tokens)
    action, kind = value.get("action", "content"), value.get("kind", "all")
    aliases = {
        "ignore_case": ("-i", "-s"),
        "literal": ("-F", "--no-fixed-strings"),
        "include_ignored": ("--no-ignore", "--ignore"),
        "multiline": ("-U", "--no-multiline"),
    }
    extras: list[str] = []
    for field in list(value):
        if field in aliases:
            enabled = optional_bool(value.pop(field), field_name=field, default=False)
            extras.append(aliases[field][0 if enabled else 1])
            if field == "multiline" and enabled:
                extras.append("--multiline-dotall")
        elif field == "context":
            amount = optional_int(value.pop(field), field_name=field, default=0, minimum=0)
            extras.extend(["-C", str(amount)])
        elif field == "glob":
            pattern = value.pop(field)
            if not isinstance(pattern, str):
                raise ValueError(
                    "glob must be a string; use repeated -g options for several filters."
                )
            extras.extend(["-g", pattern])
        elif field == "output_mode":
            modes = {"content": [], "files_with_matches": ["-l"], "count": ["-c"]}
            mode = value.pop(field)
            if mode not in modes:
                raise ValueError(
                    "Unknown output_mode; use content options -l, -c, or --count-matches."
                )
            if mode == "content" and parse_options(tokens, action=action, kind=kind).get("output"):
                raise ValueError(
                    "output_mode conflicts with options; provide one intended output mode."
                )
            extras.extend(modes[mode])
        elif field in BY_NAME:
            option = BY_NAME[field]
            item = value.pop(field)
            if option.argument:
                if not isinstance(item, (str, int)) or isinstance(item, bool):
                    raise ValueError(f"{field} requires one option value.")
                extras.extend([field, str(item)])
            elif optional_bool(item, field_name=field, default=False):
                extras.append(field)
            else:
                raise ValueError(
                    f"{field}=false is ambiguous; put its supported reverse flag in options."
                )
    if extras:
        supplied = parse_options(tokens, action=action, kind=kind)
        repaired = parse_options(extras, action=action, kind=kind)
        for option, setting in repaired.entries:
            matching = [
                (old, old_value) for old, old_value in supplied.entries if old.key == option.key
            ]
            if option.key in {"ignore", "hidden", "binary"}:
                matching.extend(
                    (old, old_value)
                    for old, old_value in supplied.entries
                    if old.key == "unrestricted"
                )
            if matching and (option.repeat or supplied.get(option.key) != setting):
                raise ValueError(
                    f"Separate {option.names[0]} field conflicts with options; "
                    "express the complete ordered selection in options."
                )
        context_keys = {"before", "after", "context"}
        if (
            any(option.key in context_keys for option, _ in repaired.entries)
            and any(option.key in context_keys for option, _ in supplied.entries)
            and repaired.context != supplied.context
        ):
            raise ValueError("Separate context field conflicts with options.")
        value["options"] = tokens + extras
    return value


def _strings(arguments: JsonObject, name: str, default: list[str]) -> list[str]:
    value = arguments.get(name, default)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and "\x00" not in item for item in value
    ):
        raise ValueError(f"{name} must be an array of strings containing no NUL characters.")
    return value


def search_files_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    try:
        arguments = normalize_search_arguments(arguments)
        if not isinstance(arguments, dict):
            raise ValueError("Provide one search argument object.")
        unknown = set(arguments) - set(SEARCH_FILES_TOOL_PARAMETERS["properties"])
        if unknown:
            raise ValueError(
                f"Unknown argument(s): {', '.join(sorted(unknown))}. "
                "Use options for supported flags; action='help' lists them."
            )
        action = arguments.get("action")
        if action not in {"content", "paths", "help"}:
            raise ValueError("action must be content, paths, or help.")
        kind = arguments.get("kind", "all")
        if kind not in {"files", "directories", "all"}:
            raise ValueError("kind must be files, directories, or all.")
        if action != "paths" and "kind" in arguments:
            raise ValueError("kind applies only to action='paths'.")
        tokens = _strings(arguments, "options", [])
        options = parse_options(tokens, action=action, kind=kind)
        reference = options.get("reference")
        if action == "help" or reference == "help":
            if set(arguments) - {"action", "options"} or any(
                option.key != "reference" for option, _ in options.entries
            ):
                raise ValueError(
                    "Help does not search; omit patterns, paths, kind, limit, offset, "
                    "and search options."
                )
            return tool_success({"content": help_text()})
        patterns = _strings(arguments, "patterns", [])
        if action == "content" and not patterns and reference != "types":
            raise ValueError(
                "Content search requires one or more patterns. Use action='paths' to find names."
            )
        if "patterns" in arguments and not patterns:
            raise ValueError("patterns must not be empty; omit it to list all paths.")
        roots_input = _strings(arguments, "paths", [str(context.effective_cwd)])
        if not roots_input or any(not p.strip() for p in roots_input):
            raise ValueError(
                "paths must contain at least one nonempty literal file or directory path."
            )
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
        limit = optional_int(
            arguments.get("limit"), field_name="limit", default=100, minimum=1, maximum=10000
        )
        offset = optional_int(
            arguments.get("offset"), field_name="offset", default=0, minimum=0, maximum=1000000
        )
        if max(options.context) > 10000:
            raise ValueError(
                "Context is limited to 10000 lines per side; use read for larger file sections."
            )
        if options.enabled("quiet") and ("limit" in arguments or "offset" in arguments):
            raise ValueError("Existence searches (-q) do not paginate; omit limit and offset.")
        if reference != "types":
            for root in roots:
                if not root.exists():
                    return tool_failure(
                        "path_not_found",
                        f"Search root not found: {root}. Correct paths; no roots were searched.",
                    )
                if not root.is_file() and not root.is_dir():
                    return tool_failure(
                        "invalid_arguments",
                        f"Search root is not a regular file or directory: {root}",
                    )
        binary = require_binary()
    except (OSError, RuntimeError, ValueError) as error:
        return tool_failure("invalid_arguments", str(error))
    budget = SearchBudget(context)
    warnings: list[str] = []
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
            if set(arguments) - {"action", "options"} or any(
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
            if action == "content":
                empty = scratch / "empty"
                empty.touch()
                validate_patterns(binary, patterns, options, context, budget, empty)
            selection = FileSelection(
                scratch / "selection.sqlite", roots, cwd, options, budget, warnings
            )
            with contextlib.closing(selection):
                selection.populate(
                    patterns if action == "paths" else [],
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
                    render_events(
                        content_events(
                            binary,
                            selection.entries(action=action),
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
    if options.enabled("stats"):
        data["stats"] = locals().get("stats", {"results_observed": page.observed})
    context.add_display_count(page.returned, "results", at_least=page.more or not complete)
    return tool_success(data)


async def _search_files_async(context: ToolContext, arguments: JsonObject) -> JsonObject:
    return await run_tool_worker(search_files_handler, context, arguments)


def _display_parts(arguments: JsonObject) -> list[ToolDisplayPart]:
    parts = []
    for field, kind in (("patterns", "query"), ("paths", "path")):
        values = arguments.get(field)
        if isinstance(values, list):
            values = [value for value in values if isinstance(value, str) and value.strip()]
            if values:
                parts.append(
                    ToolDisplayPart(
                        ", ".join(values),
                        kind=kind if len(values) == 1 else "text",
                        quote=field == "patterns",
                    )
                )
    return parts or [ToolDisplayPart(str(arguments.get("action", "search")))]


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
    "Search file contents or find file and directory paths. Content pa"
    "tterns are regex unless -F is given. Path patterns are case-insen"
    "sitive root-relative globs: '*.py' at top level, '**/*.py' at any"
    " depth. Hidden paths are included; ignore rules apply unless -u i"
    "s given; .git entries are always excluded. Explicit ignored targe"
    "ts are searched. File filters narrow this scope. Results have usa"
    "ble paths and content line numbers. Content is ordered by path; p"
    "aths by newest modification. Use help to inspect supported option"
    "s."
)
SEARCH_FILES_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["content", "paths", "help"],
            "description": (
                "Search contents, find paths, or list supported options and their effects."
            ),
        },
        "patterns": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Patterns to match; any one may match. Required for content. Omit "
                "for paths to list all entries."
            ),
        },
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Files or directories to search. Relative paths use the working di"
                "rectory; omit to search it."
            ),
        },
        "options": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Ripgrep-style options as separate tokens, e.g. ['-i','-w','-g','*"
                ".py','-g','!generated/**','-C','3']. -F: literal text; -U: multil"
                "ine; --multiline-dotall: dot matches newlines; -l: matching file "
                "paths; -c: matching-line counts; --count-matches: occurrence coun"
                "ts. Omit for normal matching-line output. Patterns and search roo"
                "ts belong in patterns and paths."
            ),
        },
        "kind": {
            "type": "string",
            "enum": ["files", "directories", "all"],
            "description": "For paths only: entry types to return. Omit for all.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "default": 100,
            "description": "Maximum matches or path/count rows to return. Omit for 100.",
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "Results to skip before limit. Omit to start at the beginning.",
        },
    },
    "required": ["action"],
}
