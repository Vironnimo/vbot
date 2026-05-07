"""Built-in grep tool adapted for vBot tool envelopes."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from core.tools.search import (
    file_filter_matches,
    normalize_file_filter_pattern,
    relative_forward_path,
    resolve_search_path,
)
from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure, tool_success

DEFAULT_LIMIT = 100
MAX_LINE_CHARS = 500
MAX_OUTPUT_BYTES = 50 * 1024
OUTPUT_TRUNCATED_MARKER = "[... output truncated ...]"
RESULTS_LIMITED_MARKER = "[Results limited to {limit} matches.]"
SUPPORTED_OUTPUT_MODES = {"content", "files_with_matches", "count"}
ALLOWED_ARGUMENTS = {
    "pattern",
    "path",
    "glob",
    "ignoreCase",
    "literal",
    "context",
    "limit",
    "output_mode",
}

GREP_TOOL_NAME = "grep"
GREP_TOOL_DESCRIPTION = (
    "Search file contents with a regex pattern by default. Set literal=true for "
    "fixed-string matching. Optional glob filters candidate files only. Returns "
    "path:line:text rows unless output_mode requests matching files or counts."
)
GREP_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Regex search pattern. Set literal=true for fixed-string matching.",
        },
        "path": {
            "type": "string",
            "description": "Directory or file to search in (default: agent workspace).",
        },
        "glob": {
            "type": "string",
            "description": "Optional search-root-relative file glob filter for candidate files.",
        },
        "ignoreCase": {
            "type": "boolean",
            "description": "Case-insensitive search (default: false).",
        },
        "literal": {
            "type": "boolean",
            "description": "Treat pattern as fixed text instead of regex (default: false).",
        },
        "context": {
            "type": "number",
            "description": "Number of context lines before and after content matches (default: 0).",
        },
        "limit": {
            "type": "number",
            "description": (
                "Maximum results (default: 100). content limits matches; "
                "files_with_matches/count limit returned file rows."
            ),
        },
        "output_mode": {
            "type": "string",
            "enum": ["content", "files_with_matches", "count"],
            "description": (
                "Output format: content returns path:line:text rows (default), "
                "files_with_matches returns matching file paths, count returns path:count rows."
            ),
        },
    },
    "required": ["pattern"],
    "additionalProperties": False,
}


def _coerce_non_negative_int(value: object, *, field_name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer >= 0")
    if isinstance(value, int):
        coerced = value
    elif isinstance(value, float) and value.is_integer():
        coerced = int(value)
    else:
        raise ValueError(f"{field_name} must be an integer >= 0")
    if coerced < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return coerced


def _coerce_positive_int(value: object, *, field_name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer >= 1")
    if isinstance(value, int):
        coerced = value
    elif isinstance(value, float) and value.is_integer():
        coerced = int(value)
    else:
        raise ValueError(f"{field_name} must be an integer >= 1")
    if coerced < 1:
        raise ValueError(f"{field_name} must be >= 1")
    return coerced


def _coerce_bool(value: object, *, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _truncate_line(content: str) -> str:
    if len(content) <= MAX_LINE_CHARS:
        return content
    return f"{content[:MAX_LINE_CHARS]}...[truncated]"


def _cap_output_bytes(content: str, *, trailing_lines: list[str] | None = None) -> str:
    encoded = content.encode("utf-8")
    suffix = "".join(f"\n{line}" for line in (trailing_lines or []) if line)
    suffix_bytes = suffix.encode("utf-8")
    if len(encoded) + len(suffix_bytes) <= MAX_OUTPUT_BYTES:
        return content + suffix

    marker = f"\n{OUTPUT_TRUNCATED_MARKER}{suffix}"
    marker_bytes = marker.encode("utf-8")
    keep_bytes = max(MAX_OUTPUT_BYTES - len(marker_bytes), 0)
    clipped = encoded[:keep_bytes].decode("utf-8", errors="ignore")
    return clipped + marker


def _render_limited_results(lines: list[str], *, observed_results: int, limit: int) -> str:
    if not lines:
        return ""
    trailing_lines: list[str] = []
    if observed_results > limit:
        trailing_lines.append(RESULTS_LIMITED_MARKER.format(limit=limit))
    return _cap_output_bytes("\n".join(lines), trailing_lines=trailing_lines)


def _rendered_relative_path(file_path: Path, *, base: Path) -> str:
    try:
        return relative_forward_path(file_path, base=base)
    except ValueError:
        return file_path.name


def _iter_candidate_files(search_target: Path, glob_pattern: str | None) -> tuple[list[Path], Path]:
    base = search_target if search_target.is_dir() else search_target.parent
    if search_target.is_file():
        candidates = [search_target]
    elif search_target.is_dir():
        candidates = [path for path in search_target.rglob("*") if path.is_file()]
    else:
        candidates = []

    if glob_pattern:
        candidates = [
            candidate
            for candidate in candidates
            if file_filter_matches(_rendered_relative_path(candidate, base=base), glob_pattern)
        ]

    candidates.sort(key=lambda path: _rendered_relative_path(path, base=base))
    return candidates, base


def _compile_pattern(pattern: str, *, literal: bool, ignore_case: bool) -> re.Pattern[str]:
    regex_pattern = re.escape(pattern) if literal else pattern
    flags = re.IGNORECASE if ignore_case else 0
    return re.compile(regex_pattern, flags)


def _read_lines(file_path: Path) -> list[str] | None:
    try:
        return file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def _grep_content_python(
    files: list[Path],
    *,
    base: Path,
    pattern: re.Pattern[str],
    context_lines: int,
    limit: int,
) -> tuple[list[str], int]:
    rendered: list[str] = []
    match_count = 0

    for file_path in files:
        all_lines = _read_lines(file_path)
        if all_lines is None:
            continue

        relative = _rendered_relative_path(file_path, base=base)
        emitted_line_numbers: set[int] = set()
        for line_index, line in enumerate(all_lines, start=1):
            if pattern.search(line) is None:
                continue
            match_count += 1
            if match_count > limit:
                return rendered, match_count

            start = max(1, line_index - context_lines)
            end = min(len(all_lines), line_index + context_lines)
            for context_index in range(start, end + 1):
                if context_index in emitted_line_numbers:
                    continue
                emitted_line_numbers.add(context_index)
                rendered.append(
                    f"{relative}:{context_index}: {_truncate_line(all_lines[context_index - 1])}"
                )

    return rendered, match_count


def _grep_files_with_matches_python(
    files: list[Path], *, base: Path, pattern: re.Pattern[str], limit: int
) -> tuple[list[str], int]:
    rendered: list[str] = []
    file_count = 0
    for file_path in files:
        all_lines = _read_lines(file_path)
        if all_lines is None or not any(pattern.search(line) for line in all_lines):
            continue
        file_count += 1
        if file_count > limit:
            break
        rendered.append(_rendered_relative_path(file_path, base=base))
    return rendered, file_count


def _grep_count_python(
    files: list[Path], *, base: Path, pattern: re.Pattern[str], limit: int
) -> tuple[list[str], int]:
    rendered: list[str] = []
    file_count = 0
    for file_path in files:
        all_lines = _read_lines(file_path)
        if all_lines is None:
            continue

        count = sum(1 for line in all_lines if pattern.search(line))
        if count <= 0:
            continue
        file_count += 1
        if file_count > limit:
            break
        rendered.append(f"{_rendered_relative_path(file_path, base=base)}:{count}")
    return rendered, file_count


def _normalize_rg_path(path_value: str) -> str:
    normalized = path_value.replace("\\", "/")
    if normalized.startswith("./"):
        return normalized[2:]
    return normalized


def _grep_with_rg(
    *,
    pattern: str,
    search_target: Path,
    glob_pattern: str | None,
    ignore_case: bool,
    literal: bool,
    output_mode: str,
    context_lines: int,
    limit: int,
) -> tuple[str | None, str | None]:
    if context_lines > 0:
        return None, None
    rg_path = shutil.which("rg")
    if not rg_path:
        return None, None

    cwd = search_target if search_target.is_dir() else search_target.parent
    search_argument = "." if search_target.is_dir() else search_target.name
    command = [
        rg_path,
        "--color",
        "never",
        "--no-messages",
        "--no-config",
        "--hidden",
        "--no-ignore",
        "--sort",
        "path",
        "--text",
    ]

    if output_mode == "content":
        command.extend(["--line-number", "--with-filename", "--no-heading"])
    elif output_mode == "files_with_matches":
        command.append("--files-with-matches")
    else:
        command.extend(["--count", "--with-filename"])
    if ignore_case:
        command.append("--ignore-case")
    if literal:
        command.append("--fixed-strings")
    if glob_pattern:
        command.extend(["--glob", glob_pattern])
    command.extend(["--regexp", pattern, "--", search_argument])

    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        return None, f"failed to execute ripgrep: {error}"

    if completed.returncode not in (0, 1):
        return None, completed.stderr.strip() or "ripgrep failed"

    raw_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not raw_lines:
        return "", None

    rendered: list[str] = []
    observed_results = 0
    for raw_line in raw_lines:
        rendered_line = _render_rg_line(raw_line, output_mode)
        if rendered_line is None:
            continue
        observed_results += 1
        if observed_results > limit:
            break
        rendered.append(rendered_line)
    return _render_limited_results(rendered, observed_results=observed_results, limit=limit), None


def _render_rg_line(raw_line: str, output_mode: str) -> str | None:
    if output_mode == "content":
        parts = raw_line.split(":", 2)
        if len(parts) != 3:
            return None
        file_part, line_part, content_part = parts
        return f"{_normalize_rg_path(file_part)}:{line_part}: {_truncate_line(content_part)}"
    if output_mode == "files_with_matches":
        return _normalize_rg_path(raw_line)

    parts = raw_line.rsplit(":", 1)
    if len(parts) != 2:
        return None
    file_part, count_part = parts
    return f"{_normalize_rg_path(file_part)}:{count_part.strip()}"


def grep_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    """Handle a grep tool call and return a stable vBot result envelope."""
    unknown_arguments = set(arguments) - ALLOWED_ARGUMENTS
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

    pattern_argument = arguments.get("pattern")
    if not isinstance(pattern_argument, str) or pattern_argument == "":
        return tool_failure("invalid_arguments", "pattern must be a non-empty string")

    try:
        path_argument = arguments.get("path")
        if path_argument is not None and not isinstance(path_argument, str):
            raise ValueError("path must be a non-empty string")
        search_target = resolve_search_path(context, path_argument)
        context_lines = _coerce_non_negative_int(
            arguments.get("context"), field_name="context", default=0
        )
        match_limit = _coerce_positive_int(
            arguments.get("limit"), field_name="limit", default=DEFAULT_LIMIT
        )
        ignore_case = _coerce_bool(
            arguments.get("ignoreCase"), field_name="ignoreCase", default=False
        )
        literal = _coerce_bool(arguments.get("literal"), field_name="literal", default=False)
        output_mode = str(arguments.get("output_mode") or "content").strip() or "content"
        if output_mode not in SUPPORTED_OUTPUT_MODES:
            raise ValueError("output_mode must be one of: content, files_with_matches, count")
        glob_pattern = _normalize_glob_argument(arguments.get("glob"))
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))

    if not search_target.exists():
        return tool_failure("path_not_found", f"path not found: {search_target}")
    if not (search_target.is_file() or search_target.is_dir()):
        return tool_failure("invalid_path", f"path is not a file or directory: {search_target}")

    try:
        compiled_pattern = _compile_pattern(
            pattern_argument, literal=literal, ignore_case=ignore_case
        )
    except re.error as error:
        return tool_failure("invalid_regex", f"invalid regex pattern: {error}")

    rg_result, rg_error = _grep_with_rg(
        pattern=pattern_argument,
        search_target=search_target,
        glob_pattern=glob_pattern,
        ignore_case=ignore_case,
        literal=literal,
        output_mode=output_mode,
        context_lines=context_lines,
        limit=match_limit,
    )
    if rg_error is not None:
        return tool_failure("grep_error", rg_error)
    if rg_result is not None:
        return tool_success(
            {"content": rg_result or f"No matches found for pattern: {pattern_argument}"}
        )

    files, base = _iter_candidate_files(search_target, glob_pattern)
    if output_mode == "content":
        rendered, observed_results = _grep_content_python(
            files,
            base=base,
            pattern=compiled_pattern,
            context_lines=context_lines,
            limit=match_limit,
        )
    elif output_mode == "files_with_matches":
        rendered, observed_results = _grep_files_with_matches_python(
            files,
            base=base,
            pattern=compiled_pattern,
            limit=match_limit,
        )
    else:
        rendered, observed_results = _grep_count_python(
            files,
            base=base,
            pattern=compiled_pattern,
            limit=match_limit,
        )

    content = _render_limited_results(
        rendered, observed_results=observed_results, limit=match_limit
    )
    if not content:
        content = f"No matches found for pattern: {pattern_argument}"
    return tool_success({"content": content})


def _normalize_glob_argument(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("glob must be a string")
    normalized = normalize_file_filter_pattern(value, field_name="glob", allow_empty=True)
    return normalized or None


def register_grep_tool(registry: ToolRegistry) -> None:
    """Register the grep tool with a vBot tool registry."""
    registry.register(
        GREP_TOOL_NAME,
        GREP_TOOL_DESCRIPTION,
        GREP_TOOL_PARAMETERS,
        grep_handler,
    )


__all__ = [
    "DEFAULT_LIMIT",
    "GREP_TOOL_DESCRIPTION",
    "GREP_TOOL_NAME",
    "GREP_TOOL_PARAMETERS",
    "grep_handler",
    "register_grep_tool",
]
