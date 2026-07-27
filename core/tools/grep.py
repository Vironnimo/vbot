"""Built-in grep tool adapted for vBot tool envelopes."""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import subprocess
import threading
from bisect import bisect_right
from pathlib import Path
from typing import NamedTuple

from core.tools.arguments import (
    coerce_bool,
    normalize_aliases,
    optional_int,
    optional_string,
)
from core.tools.process_manager import subprocess_creation_flags
from core.tools.search import (
    MAX_OUTPUT_BYTES,
    OUTPUT_TRUNCATED_MARKER,
    RESULTS_LIMITED_MARKER,
    SEARCH_CANCELLED_FAILURE_CODE,
    SEARCH_CANCELLED_FAILURE_MESSAGE,
    SEARCH_TIMEOUT_MARKER,
    SearchBudget,
    display_search_path,
    file_filter_matches,
    ignore_rules_apply,
    iter_search_entries,
    normalize_file_filter_pattern,
    relative_forward_path,
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

DEFAULT_LIMIT = 100
MAX_LINE_CHARS = 500
SUPPORTED_OUTPUT_MODES = {"content", "files_with_matches", "count"}
ALLOWED_ARGUMENTS = {
    "pattern",
    "path",
    "glob",
    "ignore_case",
    "literal",
    "multiline",
    "context",
    "limit",
    "offset",
    "include_ignored",
    "output_mode",
}
# camelCase variants some models emit; normalized before validation like edit's aliases.
_GREP_ARGUMENT_ALIASES = {"ignoreCase": "ignore_case", "includeIgnored": "include_ignored"}
# Bounded drain of a finished/killed ripgrep process; generous, never load-bearing.
_RG_DRAIN_TIMEOUT_SECONDS = 5.0
# rg exclusion for version-control internals; composes with any user glob.
# Matches the .git entry itself so it covers both the directory (never
# descended) and a worktree's .git pointer file.
_GIT_INTERNALS_EXCLUSION_GLOB = "!**/.git"

GREP_TOOL_NAME = "grep"
GREP_TOOL_DESCRIPTION = (
    "Search file contents with a regex pattern by default. Set literal=true for "
    "fixed-string matching, multiline=true for patterns spanning lines. Optional "
    "glob filters candidate files (case-insensitive). Skips .gitignore'd files and "
    ".git internals unless include_ignored=true. Returns path:line:text rows unless "
    "output_mode requests matching files or counts; paths are relative to the "
    "working directory (absolute when outside it). Page with offset."
)
GREP_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "minLength": 1,
            "description": "Regex search pattern. Set literal=true for fixed-string matching.",
        },
        "path": {
            "type": "string",
            "description": "Directory or file to search in (default: working directory).",
        },
        "glob": {
            "type": "string",
            "description": "Optional search-root-relative file glob filter for candidate files.",
        },
        "ignore_case": {
            "type": "boolean",
            "description": "Case-insensitive search (default: false).",
        },
        "literal": {
            "type": "boolean",
            "description": "Treat pattern as fixed text instead of regex (default: false).",
        },
        "multiline": {
            "type": "boolean",
            "description": (
                "Match across lines: '.' matches newlines and patterns can span "
                "lines (default: false)."
            ),
        },
        "context": {
            "type": "integer",
            "minimum": 0,
            "description": "Number of context lines before and after content matches (default: 0).",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Maximum results (default: 100). content limits matches; "
                "files_with_matches/count limit returned file rows."
            ),
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "Skip the first N results before applying limit (default: 0).",
        },
        "include_ignored": {
            "type": "boolean",
            "description": (
                "Also search .gitignore'd files (default: false). Hidden dotfiles are "
                "always searched; .git internals never."
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


def _truncate_line(content: str) -> str:
    if len(content) <= MAX_LINE_CHARS:
        return content
    return f"{content[:MAX_LINE_CHARS]}...[truncated]"


def _filter_relative_path(file_path: Path, *, base: Path) -> str:
    """Return the search-root-relative path used for glob filtering and sorting."""
    try:
        return relative_forward_path(file_path, base=base)
    except ValueError:
        return file_path.name


def _iter_candidate_files(
    search_target: Path,
    glob_pattern: str | None,
    *,
    budget: SearchBudget,
    apply_ignore_rules: bool,
) -> tuple[list[Path], Path]:
    base = search_target if search_target.is_dir() else search_target.parent
    candidates: list[Path] = []
    if search_target.is_file():
        # An explicitly named file is always searched, ignored or not.
        candidates = [search_target]
    elif search_target.is_dir():
        candidates = [
            path
            for path, _ in iter_search_entries(
                search_target,
                budget=budget,
                apply_ignore_rules=apply_ignore_rules,
                include_directories=False,
            )
        ]

    if glob_pattern:
        candidates = [
            candidate
            for candidate in candidates
            if file_filter_matches(_filter_relative_path(candidate, base=base), glob_pattern)
        ]

    candidates.sort(key=lambda path: _filter_relative_path(path, base=base))
    return candidates, base


def _compile_pattern(
    pattern: str, *, literal: bool, ignore_case: bool, multiline: bool
) -> re.Pattern[str]:
    regex_pattern = re.escape(pattern) if literal else pattern
    flags = re.IGNORECASE if ignore_case else 0
    if multiline:
        flags |= re.MULTILINE | re.DOTALL
    return re.compile(regex_pattern, flags)


def _read_text(file_path: Path) -> str | None:
    try:
        return file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _line_start_offsets(text: str) -> list[int]:
    offsets = [0]
    newline_index = text.find("\n")
    while newline_index != -1:
        offsets.append(newline_index + 1)
        newline_index = text.find("\n", newline_index + 1)
    return offsets


def _emit_match_lines(
    rendered: list[str],
    emitted_line_numbers: set[int],
    *,
    display: str,
    all_lines: list[str],
    first_line: int,
    last_line: int,
    context_lines: int,
) -> int:
    """Append a match's lines (with context, deduped); return the bytes appended."""
    appended_bytes = 0
    start = max(1, first_line - context_lines)
    end = min(len(all_lines), last_line + context_lines)
    for line_number in range(start, end + 1):
        if line_number in emitted_line_numbers:
            continue
        emitted_line_numbers.add(line_number)
        rendered_line = f"{display}:{line_number}: {_truncate_line(all_lines[line_number - 1])}"
        rendered.append(rendered_line)
        appended_bytes += len(rendered_line.encode("utf-8")) + 1
    return appended_bytes


def _match_line_spans(
    text: str, all_lines: list[str], pattern: re.Pattern[str], *, multiline: bool
) -> list[tuple[int, int]]:
    """Return each match as an inclusive 1-based ``(first_line, last_line)`` span."""
    if not multiline:
        return [
            (index, index)
            for index, line in enumerate(all_lines, start=1)
            if pattern.search(line) is not None
        ]

    line_starts = _line_start_offsets(text)
    return [
        (
            bisect_right(line_starts, match.start()),
            bisect_right(line_starts, max(match.end() - 1, match.start())),
        )
        for match in pattern.finditer(text)
    ]


def _grep_content_python(
    files: list[Path],
    *,
    cwd: Path,
    pattern: re.Pattern[str],
    context_lines: int,
    limit: int,
    offset: int,
    multiline: bool,
    budget: SearchBudget,
) -> tuple[list[str], int]:
    rendered: list[str] = []
    rendered_bytes = 0
    match_count = 0

    for file_path in files:
        if not budget.keep_going():
            break
        text = _read_text(file_path)
        if text is None:
            continue
        all_lines = text.splitlines()

        display = display_search_path(file_path, cwd=cwd)
        emitted_line_numbers: set[int] = set()
        for first_line, last_line in _match_line_spans(
            text, all_lines, pattern, multiline=multiline
        ):
            match_count += 1
            if match_count <= offset:
                continue
            if match_count > offset + limit:
                return rendered, match_count
            rendered_bytes += _emit_match_lines(
                rendered,
                emitted_line_numbers,
                display=display,
                all_lines=all_lines,
                first_line=first_line,
                last_line=last_line,
                context_lines=context_lines,
            )
            if rendered_bytes > MAX_OUTPUT_BYTES:
                # Already past the byte cap; scanning further only buys output
                # the cap would discard.
                return rendered, match_count

    return rendered, match_count


def _grep_files_with_matches_python(
    files: list[Path],
    *,
    cwd: Path,
    pattern: re.Pattern[str],
    limit: int,
    offset: int,
    multiline: bool,
    budget: SearchBudget,
) -> tuple[list[str], int]:
    rendered: list[str] = []
    file_count = 0
    for file_path in files:
        if not budget.keep_going():
            break
        text = _read_text(file_path)
        if text is None:
            continue
        if multiline:
            matched = pattern.search(text) is not None
        else:
            matched = any(pattern.search(line) is not None for line in text.splitlines())
        if not matched:
            continue
        file_count += 1
        if file_count <= offset:
            continue
        if file_count > offset + limit:
            break
        rendered.append(display_search_path(file_path, cwd=cwd))
    return rendered, file_count


def _grep_count_python(
    files: list[Path],
    *,
    cwd: Path,
    pattern: re.Pattern[str],
    limit: int,
    offset: int,
    multiline: bool,
    budget: SearchBudget,
) -> tuple[list[str], int]:
    rendered: list[str] = []
    file_count = 0
    for file_path in files:
        if not budget.keep_going():
            break
        text = _read_text(file_path)
        if text is None:
            continue

        if multiline:
            count = sum(1 for _ in pattern.finditer(text))
        else:
            count = sum(1 for line in text.splitlines() if pattern.search(line) is not None)
        if count <= 0:
            continue
        file_count += 1
        if file_count <= offset:
            continue
        if file_count > offset + limit:
            break
        rendered.append(f"{display_search_path(file_path, cwd=cwd)}:{count}")
    return rendered, file_count


def _normalize_rg_path(path_value: str) -> str:
    normalized = path_value.replace("\\", "/")
    if normalized.startswith("./"):
        return normalized[2:]
    return normalized


class _RipgrepOutcome(NamedTuple):
    handled: bool
    content: str = ""
    error_code: str | None = None
    error_message: str | None = None
    total_results: int = 0
    counted_all: bool = False


def _grep_with_rg(
    *,
    context: ToolContext,
    budget: SearchBudget,
    cwd: Path,
    pattern: str,
    search_target: Path,
    glob_pattern: str | None,
    ignore_case: bool,
    literal: bool,
    multiline: bool,
    apply_ignore_rules: bool,
    output_mode: str,
    context_lines: int,
    limit: int,
    offset: int,
) -> _RipgrepOutcome:
    if context_lines > 0:
        return _RipgrepOutcome(handled=False)
    rg_path = shutil.which("rg")
    if not rg_path:
        return _RipgrepOutcome(handled=False)

    base = search_target if search_target.is_dir() else search_target.parent
    search_argument = "." if search_target.is_dir() else search_target.name
    command = [
        rg_path,
        "--color",
        "never",
        "--no-messages",
        "--no-config",
        "--hidden",
        # Honor .gitignore files outside git repositories too, matching the
        # Python fallback's behavior.
        "--no-require-git",
        "--glob-case-insensitive",
        "--sort",
        "path",
        "--text",
    ]
    if not apply_ignore_rules:
        command.append("--no-ignore")
    if ".git" not in search_target.parts:
        command.extend(["--glob", _GIT_INTERNALS_EXCLUSION_GLOB])

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
    if multiline:
        command.extend(["--multiline", "--multiline-dotall"])
    if glob_pattern:
        command.extend(["--glob", glob_pattern])
    command.extend(["--regexp", pattern, "--", search_argument])

    try:
        process = subprocess.Popen(
            command,
            cwd=str(base),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess_creation_flags(),
        )
    except OSError as error:
        return _RipgrepOutcome(
            handled=True,
            error_code="grep_error",
            error_message=f"failed to execute ripgrep: {error}",
        )

    def kill_process() -> None:
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.kill()

    def expire() -> None:
        budget.timed_out = True
        kill_process()

    # Killing the process closes its stdout, so the blocking line reads below
    # always terminate: the watchdog covers a silent long scan, the cancel hook
    # covers a per-call user cancel arriving while this thread is blocked.
    watchdog = threading.Timer(budget.remaining_seconds(), expire)
    watchdog.daemon = True
    watchdog.start()
    context.on_cancel(kill_process)

    rendered: list[str] = []
    observed_results = 0
    rendered_bytes = 0
    stopped_early = False
    stderr_output = ""
    try:
        stdout_stream = process.stdout
        if stdout_stream is not None:
            for raw_line in stdout_stream:
                if not budget.keep_going():
                    stopped_early = True
                    break
                line = raw_line.rstrip("\n")
                if not line.strip():
                    continue
                rendered_line = _render_rg_line(line, output_mode, base=base, cwd=cwd)
                if rendered_line is None:
                    continue
                observed_results += 1
                if observed_results <= offset:
                    continue
                if observed_results > offset + limit:
                    stopped_early = True
                    break
                rendered.append(rendered_line)
                rendered_bytes += len(rendered_line.encode("utf-8")) + 1
                if rendered_bytes > MAX_OUTPUT_BYTES:
                    stopped_early = True
                    break
    finally:
        watchdog.cancel()
        if stopped_early:
            kill_process()
        try:
            _, stderr_output = process.communicate(timeout=_RG_DRAIN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            kill_process()
            _, stderr_output = process.communicate()

    # A kill by the cancel hook or the watchdog surfaces above as plain EOF;
    # poll once more so that exit is not misread as an rg failure.
    if not stopped_early:
        budget.keep_going()
    intentionally_stopped = stopped_early or budget.stopped
    if not intentionally_stopped and process.returncode not in (0, 1):
        message = stderr_output.strip() or "ripgrep failed"
        # The executing engine (Rust regex) rejected the pattern — surface it
        # as an invalid pattern, not an infrastructure failure.
        error_code = "invalid_regex" if "regex parse error" in message.lower() else "grep_error"
        return _RipgrepOutcome(handled=True, error_code=error_code, error_message=message)

    content = render_limited_results(
        rendered,
        observed_results=max(observed_results - offset, 0),
        limit=limit,
        timed_out=budget.timed_out,
    )
    return _RipgrepOutcome(
        handled=True,
        content=content,
        total_results=observed_results,
        counted_all=not stopped_early,
    )


def _render_rg_line(raw_line: str, output_mode: str, *, base: Path, cwd: Path) -> str | None:
    if output_mode == "content":
        parts = raw_line.split(":", 2)
        if len(parts) != 3:
            return None
        file_part, line_part, content_part = parts
        display = display_search_path(base / _normalize_rg_path(file_part), cwd=cwd)
        return f"{display}:{line_part}: {_truncate_line(content_part)}"
    if output_mode == "files_with_matches":
        return display_search_path(base / _normalize_rg_path(raw_line), cwd=cwd)

    parts = raw_line.rsplit(":", 1)
    if len(parts) != 2:
        return None
    file_part, count_part = parts
    display = display_search_path(base / _normalize_rg_path(file_part), cwd=cwd)
    return f"{display}:{count_part.strip()}"


def grep_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    """Handle a grep tool call and return a stable vBot result envelope.

    Sync core; registered behind an ``asyncio.to_thread`` wrapper so the
    ripgrep subprocess and the fallback file scan never block the kernel
    event loop.
    """
    arguments = normalize_aliases(arguments, _GREP_ARGUMENT_ALIASES)
    unknown_arguments = set(arguments) - ALLOWED_ARGUMENTS
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

    pattern_argument = arguments.get("pattern")
    if not isinstance(pattern_argument, str) or pattern_argument == "":
        return tool_failure("invalid_arguments", "pattern must be a non-empty string")

    try:
        path_argument = optional_string(arguments.get("path"), field_name="path")
        search_target = resolve_search_path(context, path_argument)
        context_lines = optional_int(
            arguments.get("context"), field_name="context", default=0, minimum=0
        )
        match_limit = optional_int(
            arguments.get("limit"), field_name="limit", default=DEFAULT_LIMIT, minimum=1
        )
        result_offset = optional_int(
            arguments.get("offset"), field_name="offset", default=0, minimum=0
        )
        ignore_case = coerce_bool(
            arguments.get("ignore_case"), field_name="ignore_case", default=False
        )
        literal = coerce_bool(arguments.get("literal"), field_name="literal", default=False)
        multiline = coerce_bool(arguments.get("multiline"), field_name="multiline", default=False)
        include_ignored = coerce_bool(
            arguments.get("include_ignored"), field_name="include_ignored", default=False
        )
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

    # The pattern is validated by whichever engine executes it: ripgrep's Rust
    # regex when available, Python re in the fallback. A Python compile error
    # therefore only matters when the fallback actually runs.
    compiled_pattern: re.Pattern[str] | None = None
    regex_error_message = ""
    try:
        compiled_pattern = _compile_pattern(
            pattern_argument, literal=literal, ignore_case=ignore_case, multiline=multiline
        )
    except re.error as error:
        regex_error_message = f"invalid regex pattern: {error}"

    cwd_root = context.effective_cwd.expanduser().resolve()
    budget = SearchBudget(context)
    apply_ignore = search_target.is_dir() and ignore_rules_apply(
        search_target, include_ignored=include_ignored
    )

    outcome = _grep_with_rg(
        context=context,
        budget=budget,
        cwd=cwd_root,
        pattern=pattern_argument,
        search_target=search_target,
        glob_pattern=glob_pattern,
        ignore_case=ignore_case,
        literal=literal,
        multiline=multiline,
        apply_ignore_rules=apply_ignore,
        output_mode=output_mode,
        context_lines=context_lines,
        limit=match_limit,
        offset=result_offset,
    )
    if outcome.handled:
        if outcome.error_code is not None:
            return tool_failure(outcome.error_code, outcome.error_message or "ripgrep failed")
        if budget.cancelled_by_user:
            return tool_failure(SEARCH_CANCELLED_FAILURE_CODE, SEARCH_CANCELLED_FAILURE_MESSAGE)
        content = outcome.content
        if not content:
            total_results = outcome.total_results if outcome.counted_all else 0
            content = _empty_result_content(
                pattern_argument, budget, offset=result_offset, total_results=total_results
            )
        return tool_success({"content": content})

    if compiled_pattern is None:
        return tool_failure("invalid_regex", regex_error_message)

    files, _base = _iter_candidate_files(
        search_target, glob_pattern, budget=budget, apply_ignore_rules=apply_ignore
    )
    if output_mode == "content":
        rendered, raw_count = _grep_content_python(
            files,
            cwd=cwd_root,
            pattern=compiled_pattern,
            context_lines=context_lines,
            limit=match_limit,
            offset=result_offset,
            multiline=multiline,
            budget=budget,
        )
    elif output_mode == "files_with_matches":
        rendered, raw_count = _grep_files_with_matches_python(
            files,
            cwd=cwd_root,
            pattern=compiled_pattern,
            limit=match_limit,
            offset=result_offset,
            multiline=multiline,
            budget=budget,
        )
    else:
        rendered, raw_count = _grep_count_python(
            files,
            cwd=cwd_root,
            pattern=compiled_pattern,
            limit=match_limit,
            offset=result_offset,
            multiline=multiline,
            budget=budget,
        )

    if budget.cancelled_by_user:
        return tool_failure(SEARCH_CANCELLED_FAILURE_CODE, SEARCH_CANCELLED_FAILURE_MESSAGE)

    content = render_limited_results(
        rendered,
        observed_results=max(raw_count - result_offset, 0),
        limit=match_limit,
        timed_out=budget.timed_out,
    )
    if not content:
        content = _empty_result_content(
            pattern_argument, budget, offset=result_offset, total_results=raw_count
        )
    return tool_success({"content": content})


def _empty_result_content(
    pattern: str, budget: SearchBudget, *, offset: int, total_results: int
) -> str:
    if offset > 0 and total_results > 0:
        content = f"No results at offset {offset}; {total_results} matches total."
    else:
        content = f"No matches found for pattern: {pattern}"
    if budget.timed_out:
        return f"{content}\n{SEARCH_TIMEOUT_MARKER}"
    return content


def _normalize_glob_argument(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("glob must be a string")
    normalized = normalize_file_filter_pattern(value, field_name="glob", allow_empty=True)
    return normalized or None


async def _grep_handler_async(context: ToolContext, arguments: JsonObject) -> JsonObject:
    return await asyncio.to_thread(grep_handler, context, arguments)


def register_grep_tool(registry: ToolRegistry) -> None:
    """Register the grep tool with a vBot tool registry."""
    registry.register(
        GREP_TOOL_NAME,
        GREP_TOOL_DESCRIPTION,
        GREP_TOOL_PARAMETERS,
        _grep_handler_async,
        display=ToolDisplay(summary_fields=("pattern", "path")),
    )


__all__ = [
    "DEFAULT_LIMIT",
    "GREP_TOOL_DESCRIPTION",
    "GREP_TOOL_NAME",
    "GREP_TOOL_PARAMETERS",
    "MAX_OUTPUT_BYTES",
    "OUTPUT_TRUNCATED_MARKER",
    "RESULTS_LIMITED_MARKER",
    "grep_handler",
    "register_grep_tool",
]
