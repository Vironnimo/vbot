"""Built-in glob tool adapted for vBot tool envelopes."""

from __future__ import annotations

from pathlib import Path

from core.tools.arguments import optional_string
from core.tools.search import normalize_file_filter_pattern, resolve_search_path
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)

MAX_GLOB_MATCHES = 100

GLOB_TOOL_NAME = "glob"
GLOB_TOOL_DESCRIPTION = (
    "Find paths by glob pattern. Returns matching file and directory paths relative to "
    "the search directory, sorted by path. Directory entries end with '/'. Capped at "
    "100 matches."
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
    },
    "required": ["pattern"],
    "additionalProperties": False,
}


def _relative_match(matched_path: Path, *, search_root: Path) -> str:
    relative_match = matched_path.relative_to(search_root).as_posix()
    if matched_path.is_dir():
        return f"{relative_match.rstrip('/')}/"
    return relative_match


def _find_glob_matches(search_root: Path, pattern: str) -> list[str]:
    # Path.glob("**") differs across supported Python versions (for example,
    # Python 3.10 can omit files). Use a stable equivalent that includes both
    # files and directories for this tool's path-level contract.
    runtime_pattern = "**/*" if pattern == "**" else pattern
    relative_matches: list[str] = []

    for matched_path in search_root.glob(runtime_pattern):
        relative_match = matched_path.relative_to(search_root).as_posix()
        if relative_match in {"", "."}:
            continue
        relative_matches.append(_relative_match(matched_path, search_root=search_root))

    relative_matches.sort()
    return relative_matches[:MAX_GLOB_MATCHES]


def glob_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    """Handle a glob tool call and return a stable vBot result envelope."""
    unknown_arguments = set(arguments) - {"pattern", "path"}
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
    except (RuntimeError, ValueError) as error:
        return tool_failure("invalid_arguments", str(error))

    if not search_root.exists():
        return tool_failure("path_not_found", f"path not found: {search_root}")
    if not search_root.is_dir():
        return tool_failure("not_a_directory", f"path is not a directory: {search_root}")

    try:
        relative_matches = _find_glob_matches(search_root, normalized_pattern)
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))
    except OSError as error:
        return tool_failure("filesystem_error", f"failed to search paths: {search_root}: {error}")

    if not relative_matches:
        return tool_success({"content": f"No paths matched pattern: {normalized_pattern}"})

    return tool_success({"content": "\n".join(relative_matches)})


def register_glob_tool(registry: ToolRegistry) -> None:
    """Register the glob tool with a vBot tool registry."""
    registry.register(
        GLOB_TOOL_NAME,
        GLOB_TOOL_DESCRIPTION,
        GLOB_TOOL_PARAMETERS,
        glob_handler,
        display=ToolDisplay(summary_fields=("pattern",)),
    )


__all__ = [
    "GLOB_TOOL_DESCRIPTION",
    "GLOB_TOOL_NAME",
    "GLOB_TOOL_PARAMETERS",
    "MAX_GLOB_MATCHES",
    "glob_handler",
    "register_glob_tool",
]
