"""Built-in glob tool adapted for vBot tool envelopes."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

from core.tools.arguments import optional_int, optional_string
from core.tools.search import (
    SEARCH_CANCELLED_FAILURE_CODE,
    SEARCH_CANCELLED_FAILURE_MESSAGE,
    SEARCH_TIMEOUT_MARKER,
    SearchBudget,
    display_search_path,
    normalize_file_filter_pattern,
    render_limited_results,
    resolve_search_path,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)

DEFAULT_GLOB_LIMIT = 100

GLOB_TOOL_NAME = "glob"
GLOB_TOOL_DESCRIPTION = (
    "Find paths by glob pattern. Returns matching file and directory paths sorted by "
    "modification time (newest first), relative to the working directory (absolute "
    "when outside it). Directory entries end with '/'. Results beyond the limit "
    "(default 100) are cut and marked."
)
GLOB_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Glob pattern to match paths, e.g. '**/*.py', 'src/*'.",
        },
        "path": {
            "type": "string",
            "description": "Directory to search in (default: working directory).",
        },
        "limit": {
            "type": "number",
            "description": "Maximum results (default: 100). Excess matches are cut and marked.",
        },
    },
    "required": ["pattern"],
    "additionalProperties": False,
}


def _collect_glob_matches(
    search_root: Path,
    pattern: str,
    *,
    cwd: Path,
    budget: SearchBudget,
) -> list[tuple[float, str, bool]]:
    """Collect ``(mtime, display_path, is_directory)`` for every pattern match.

    All matches must be collected before sorting by modification time, so the
    budget is polled per entry to keep a huge tree walk cancellable and bounded.
    """
    # Path.glob("**") differs across supported Python versions (for example,
    # Python 3.10 can omit files). Use a stable equivalent that includes both
    # files and directories for this tool's path-level contract.
    runtime_pattern = "**/*" if pattern == "**" else pattern
    collected: list[tuple[float, str, bool]] = []

    for matched_path in search_root.glob(runtime_pattern):
        if not budget.keep_going():
            break
        relative_match = matched_path.relative_to(search_root).as_posix()
        if relative_match in {"", "."}:
            continue
        display = display_search_path(matched_path, cwd=cwd)
        try:
            stat_result = matched_path.stat()
        except OSError:
            # Broken symlink or vanished entry: keep it visible, oldest-ranked.
            collected.append((0.0, display, False))
            continue
        collected.append((stat_result.st_mtime, display, stat.S_ISDIR(stat_result.st_mode)))

    return collected


def glob_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    """Handle a glob tool call and return a stable vBot result envelope.

    Sync core; registered behind an ``asyncio.to_thread`` wrapper so a large
    tree walk never blocks the kernel event loop.
    """
    unknown_arguments = set(arguments) - {"pattern", "path", "limit"}
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
            search_root, normalized_pattern, cwd=cwd_root, budget=budget
        )
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))
    except OSError as error:
        return tool_failure("filesystem_error", f"failed to search paths: {search_root}: {error}")

    if budget.cancelled_by_user:
        return tool_failure(SEARCH_CANCELLED_FAILURE_CODE, SEARCH_CANCELLED_FAILURE_MESSAGE)

    collected.sort(key=lambda entry: (-entry[0], entry[1]))
    rendered = [
        f"{display}/" if is_directory else display
        for _, display, is_directory in collected[:match_limit]
    ]

    content = render_limited_results(
        rendered,
        observed_results=len(collected),
        limit=match_limit,
        timed_out=budget.timed_out,
    )
    if not content:
        content = f"No paths matched pattern: {normalized_pattern}"
        if budget.timed_out:
            content = f"{content}\n{SEARCH_TIMEOUT_MARKER}"
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
        display=ToolDisplay(summary_fields=("pattern",)),
    )


__all__ = [
    "DEFAULT_GLOB_LIMIT",
    "GLOB_TOOL_DESCRIPTION",
    "GLOB_TOOL_NAME",
    "GLOB_TOOL_PARAMETERS",
    "glob_handler",
    "register_glob_tool",
]
