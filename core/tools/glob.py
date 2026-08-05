"""Built-in glob tool adapted for vBot tool envelopes."""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.tools.arguments import optional_bool, optional_int, optional_string
from core.tools.search import (
    SEARCH_CANCELLED_FAILURE_CODE,
    SEARCH_CANCELLED_FAILURE_MESSAGE,
    SEARCH_TIMEOUT_MARKER,
    SearchBudget,
    display_search_path,
    glob_path_matches,
    ignore_rules_apply,
    iter_search_entries,
    normalize_file_filter_pattern,
    render_limited_results,
    resolve_search_path,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayField,
    ToolRegistry,
    tool_failure,
    tool_success,
)

DEFAULT_GLOB_LIMIT = 100

GLOB_TOOL_NAME = "glob"
GLOB_TOOL_DESCRIPTION = (
    "Find paths by glob pattern (case-insensitive; '*.py' matches top level only, "
    "'**/*.py' any depth). Returns matching file and directory paths sorted by "
    "modification time (newest first), relative to the working directory (absolute "
    "when outside it). Directory entries end with '/'. Skips .gitignore'd paths "
    "unless include_ignored=true. Results beyond the limit (default 100) are cut "
    "and marked; page with offset."
)
GLOB_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "minLength": 1,
            "description": "Glob pattern to match paths, e.g. '**/*.py', 'src/*'.",
        },
        "path": {
            "type": "string",
            "description": "Directory to search in (default: working directory).",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum results (default: 100). Excess matches are cut and marked.",
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "Skip the first N results before applying limit (default: 0).",
        },
        "include_ignored": {
            "type": "boolean",
            "description": (
                "Also match .gitignore'd paths (default: false). Hidden dotfiles are "
                "always matched; .git internals never."
            ),
        },
    },
    "required": ["pattern"],
}


def _collect_glob_matches(
    search_root: Path,
    pattern: str,
    *,
    cwd: Path,
    budget: SearchBudget,
    apply_ignore_rules: bool,
) -> list[tuple[float, str, bool]]:
    """Collect ``(mtime, display_path, is_directory)`` for every pattern match.

    All matches must be collected before sorting by modification time, so the
    budget bounds a huge tree walk, not the limit.
    """
    collected: list[tuple[float, str, bool]] = []

    for matched_path, is_directory in iter_search_entries(
        search_root,
        budget=budget,
        apply_ignore_rules=apply_ignore_rules,
        include_directories=True,
    ):
        relative_match = matched_path.relative_to(search_root).as_posix()
        if not glob_path_matches(relative_match, pattern):
            continue
        display = display_search_path(matched_path, cwd=cwd)
        try:
            modified_at = matched_path.stat().st_mtime
        except OSError:
            # Broken symlink or vanished entry: keep it visible, oldest-ranked.
            modified_at = 0.0
        collected.append((modified_at, display, is_directory))

    return collected


def glob_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    """Handle a glob tool call and return a stable vBot result envelope.

    Sync core; registered behind an ``asyncio.to_thread`` wrapper so a large
    tree walk never blocks the kernel event loop.
    """
    unknown_arguments = set(arguments) - {"pattern", "path", "limit", "offset", "include_ignored"}
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

    pattern_argument = arguments.get("pattern")
    if not isinstance(pattern_argument, str) or not pattern_argument.strip():
        return tool_failure("invalid_arguments", "pattern must be a non-empty string")

    try:
        path_argument = optional_string(arguments.get("path"), field_name="path")
        search_root = resolve_search_path(context, path_argument)
        normalized_pattern = normalize_file_filter_pattern(pattern_argument, field_name="pattern")
        match_limit = optional_int(
            arguments.get("limit"), field_name="limit", default=DEFAULT_GLOB_LIMIT, minimum=1
        )
        result_offset = optional_int(
            arguments.get("offset"), field_name="offset", default=0, minimum=0
        )
        include_ignored = optional_bool(
            arguments.get("include_ignored"), field_name="include_ignored", default=False
        )
    except (RuntimeError, ValueError) as error:
        return tool_failure("invalid_arguments", str(error))

    if not search_root.exists():
        return tool_failure("path_not_found", f"path not found: {search_root}")
    if not search_root.is_dir():
        return tool_failure("not_a_directory", f"path is not a directory: {search_root}")

    cwd_root = context.effective_cwd.expanduser().resolve()
    budget = SearchBudget(context)
    try:
        collected = _collect_glob_matches(
            search_root,
            normalized_pattern,
            cwd=cwd_root,
            budget=budget,
            apply_ignore_rules=ignore_rules_apply(search_root, include_ignored=include_ignored),
        )
    except OSError as error:
        return tool_failure("filesystem_error", f"failed to search paths: {search_root}: {error}")

    if budget.cancelled_by_user:
        return tool_failure(SEARCH_CANCELLED_FAILURE_CODE, SEARCH_CANCELLED_FAILURE_MESSAGE)

    collected.sort(key=lambda entry: (-entry[0], entry[1]))
    page = collected[result_offset : result_offset + match_limit]
    rendered = [f"{display}/" if is_directory else display for _, display, is_directory in page]

    content = render_limited_results(
        rendered,
        observed_results=max(len(collected) - result_offset, 0),
        limit=match_limit,
        timed_out=budget.timed_out,
    )
    if not content:
        if result_offset > 0 and collected:
            content = f"No results at offset {result_offset}; {len(collected)} matches total."
        else:
            content = f"No paths matched pattern: {normalized_pattern}"
        if budget.timed_out:
            content = f"{content}\n{SEARCH_TIMEOUT_MARKER}"
    available_results = max(len(collected) - result_offset, 0)
    displayed_results = len(page)
    context.add_display_count(
        displayed_results,
        "results",
        at_least=displayed_results > 0 and (budget.timed_out or available_results > match_limit),
    )
    return tool_success({"content": content})


async def _glob_handler_async(context: ToolContext, arguments: JsonObject) -> JsonObject:
    return await asyncio.to_thread(glob_handler, context, arguments)


def register_glob_tool(registry: ToolRegistry) -> None:
    """Register the glob tool with a vBot tool registry."""
    registry.register(
        GLOB_TOOL_NAME,
        GLOB_TOOL_DESCRIPTION,
        GLOB_TOOL_PARAMETERS,
        _glob_handler_async,
        result_schema={"type": "object", "required": ["content"]},
        display=ToolDisplay(
            primary_candidates=(ToolDisplayField("pattern", kind="query", quote=True),)
        ),
        parallel_safe=True,
        open_input_schema=True,
    )


__all__ = [
    "DEFAULT_GLOB_LIMIT",
    "GLOB_TOOL_DESCRIPTION",
    "GLOB_TOOL_NAME",
    "GLOB_TOOL_PARAMETERS",
    "glob_handler",
    "register_glob_tool",
]
