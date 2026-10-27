"""Failure-hint selection: bounded scans and one next action per result."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from core.permissions import PermissionBoundary

from ._shell_hint_common import (
    _EXIT_CODE_HINTS,
    _TIMEOUT_PROGRAM,
    _TIMEOUT_PROGRAM_HINT,
    _error_start,
    _Failure,
    _offers_none,
    _scan_window,
)
from ._shell_hint_common import HINT_TOOL_NAMES as HINT_TOOL_NAMES
from ._shell_hint_pipeline import (
    _hint_discarded_errors,
    _hint_first_stage,
    _hint_hidden_failure,
    _hint_reported_error,
    _hint_skipped_after_pipeline,
    _hint_waiting_for_background,
)
from ._shell_hint_pipeline import ended_jobs_hint as ended_jobs_hint
from ._shell_hint_pipeline import sleeps as sleeps
from ._shell_hint_pipeline import without_closed_pipes as without_closed_pipes
from ._shell_hint_programs import (
    _hint_already_exists,
    _hint_command_not_found,
    _hint_container_tty,
    _hint_end_of_input,
    _hint_gh_unknown_json_field,
    _hint_merge_conflict,
    _hint_permission_denied,
    _hint_port_in_use,
    _hint_powershell_alias_flags,
    _hint_powershell_command_not_found,
    _hint_powershell_prompt,
    _hint_rate_limit,
    _hint_select_string_recurse,
    _hint_store_python,
    _hint_windows_find,
)
from ._shell_hint_python import _hint_module_not_found, _hint_script_not_found
from ._shell_hint_syntax import (
    _hint_dev_null,
    _hint_escaped_quote,
    _hint_native_glob,
    _hint_nested_powershell,
    _hint_powershell_syntax,
    _hint_python_interpolation,
    _hint_python_source,
    _hint_typographic_quote,
)

# Ordered by how often each failure shape wastes a retry turn; the first match wins.
_OUTPUT_HINTS: tuple[Callable[[_Failure], str | None], ...] = (
    _hint_gh_unknown_json_field,
    _hint_merge_conflict,
    # Read from the command alone: every error that follows comes from it.
    _hint_python_source,
    _hint_powershell_prompt,
    _hint_container_tty,
    _hint_end_of_input,
    _hint_command_not_found,
    _hint_powershell_command_not_found,
    _hint_store_python,
    _hint_windows_find,
    _hint_powershell_syntax,
    _hint_powershell_alias_flags,
    _hint_select_string_recurse,
    _hint_dev_null,
    _hint_native_glob,
    _hint_script_not_found,
    _hint_module_not_found,
    _hint_already_exists,
    _hint_port_in_use,
    _hint_permission_denied,
    _hint_rate_limit,
)


def annotate_failure(
    command: str,
    exit_code: int | None,
    output: str,
    *,
    workdir: Path | None = None,
    offers: Callable[[str], bool] = _offers_none,
    failed_programs: Sequence[str] = (),
    output_lines: int | None = None,
    permissions: PermissionBoundary,
) -> str | None:
    """Return one short next action for a failed command, or None.

    *output* is the command's output as the Agent reads it; only its first and
    last ``_SCAN_CHARS`` characters are examined. *workdir* is the directory the
    command started in, when known. *offers* tells whether the Agent is offered
    the Tool of a registry name; a hint names only offered Tools.
    *failed_programs* are the command's failed child programs
    (``<name> exited with code <code>``) and *output_lines* the number of lines
    it printed (the lines of *output* when omitted). *permissions* is the
    boundary of the Session that reads the hint; a hint names only paths it
    may read.

    A command that did not run as written gets a hint whatever its exit code,
    since a later statement's success can hide its failure: a typographic quote
    or a \\" ended a string early, or this shell expanded the variables of a
    nested PowerShell's double-quoted -Command code (first, since every error
    follows from it), a statement PowerShell could not run behind exit code 0
    (a command it did not recognize, a redirection to /dev/null), or a program
    failed in a pipeline with a Select-Object -First stage, or a failed program's
    errors went to $null and the output shows no error line. A non-zero exit code
    can come from a pipeline whose failed program made && skip the rest. Last, a
    successful command with expandable variables in double-quoted Python source
    is told how to preserve literal text. A command that sleeps to read a
    background command's log is told that the result arrives as a message.
    Reported cmdlet errors and Blender Python tracebacks can explain a zero
    exit when the command itself invoked the reporting program.
    Otherwise exit code 0 or None stays unannotated.
    """
    command = command or ""
    output = output or ""
    try:
        quote = (
            _hint_typographic_quote(command)
            or _hint_escaped_quote(command, exit_code, output, failed_programs)
            or _hint_nested_powershell(command)
        )
    except Exception:
        quote = None
    if quote:
        return quote
    if exit_code:
        hint = _failure_hint(command, exit_code, output, workdir, offers, permissions)
    else:
        try:
            hint = _hint_hidden_failure(command, exit_code, output, offers, permissions)
        except Exception:
            hint = None
    if hint:
        return hint
    lines = len(output.splitlines()) if output_lines is None else output_lines
    for late in (
        lambda: _hint_skipped_after_pipeline(command, exit_code, failed_programs),
        lambda: _hint_first_stage(command, exit_code, output, lines, failed_programs),
        lambda: _hint_reported_error(command, exit_code, output),
        lambda: _hint_discarded_errors(command, exit_code, output, failed_programs),
        lambda: _hint_python_interpolation(command, exit_code),
        lambda: _hint_waiting_for_background(command),
    ):
        try:
            hint = late()
        except Exception:
            hint = None
        if hint:
            return hint
    return None


def _failure_hint(
    command: str,
    exit_code: int,
    output: str,
    workdir: Path | None,
    offers: Callable[[str], bool],
    permissions: PermissionBoundary,
) -> str | None:
    window = _scan_window(output)
    if window:
        failure = _Failure(
            command, window, workdir, offers, _error_start(window), permissions=permissions
        )
        for hint_for in _OUTPUT_HINTS:
            try:
                hint = hint_for(failure)
            except Exception:
                continue
            if hint:
                return hint
    if exit_code == 124 and _TIMEOUT_PROGRAM.search(command):
        return _TIMEOUT_PROGRAM_HINT
    return _EXIT_CODE_HINTS.get(exit_code)


__all__ = [
    "HINT_TOOL_NAMES",
    "annotate_failure",
    "ended_jobs_hint",
    "sleeps",
    "without_closed_pipes",
]
