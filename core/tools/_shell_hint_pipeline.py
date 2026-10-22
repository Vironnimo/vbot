"""Pipeline failures, early termination and hidden outcomes."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from core.permissions import PermissionBoundary

from . import _shell_hint_common as _common
from ._shell_hint_code import (
    _COMMAND_WORD,
    _FIRST_STAGE,
    _POWERSHELL_NOT_RECOGNIZED,
    _RUN_START,
    _calls,
    _powershell_code,
    _program_at,
    _program_name,
    _top_level_split,
    _top_level_statements,
    _working_program,
)
from ._shell_hint_common import (
    _DEV_NULL,
    _DEV_NULL_HINT,
    _DEV_NULL_RESOLVED,
    _exit_codes,
    _Failure,
    _scan_window,
)
from ._shell_hint_programs import _hint_powershell_command_not_found

_FIRST_COUNT = re.compile(
    r"(?:^|[ \t])-f(?:i(?:r(?:st?)?)?)?(?:[ \t]*+:[ \t]*+|[ \t]++)(\d+)", re.I
)
_WAIT = re.compile(r"(?:^|[ \t])-w(?:a(?:it?)?)?(?=[ \t:]|$)", re.I)
# A redirection that merges a program's errors into its output.
_MERGED_ERRORS = re.compile(r"(?<![\w>&])[2*]>&1")
# What a program prints when it fails writing to an output that was closed:
# Python on Windows (EINVAL) and elsewhere (EPIPE), Node, and .NET.
_CLOSED_OUTPUT = re.compile(
    r"^(?:OSError: \[Errno 22\] Invalid argument|BrokenPipeError: \[Errno 32\] Broken pipe)\r?$"
    r"|write EPIPE|The pipe is being closed|The pipe has been ended",
    re.M,
)
_STAGE_SHOWN_CHARS = 60
_LAST_EXIT_CODE = re.compile(r"\$LASTEXITCODE\b", re.I)
# A line that says why a program failed: an exception (Python, Node, .NET), an
# error or fatal message in English, German, French, Italian, Spanish or
# Portuguese, possibly after the program's name (argparse's "prog: error: ..."),
# unittest's FAIL/ERROR lines, TAP's "not ok", or a failure mark; also as a
# Select-String context line ("> ").
_ERROR_LINE = re.compile(
    r"^[ \t]*+(?:>[ \t]*+)?(?:[\w.]*(?:Error|Exception)(?:[ \t]\[\w+\])?(?::|$)"
    r"|(?:[\w.-]++:[ \t]*+)?(?i:error|erreur|errore|erro|fehler|fatal)\b[:!]?[ \t]"
    r"|FAIL(?:ED)?\b|npm ERR!|not ok\b|[\u2717\u2718\u2716\u274c])",
    re.M,
)


def _stage_text(command: str, code: str, start: int, end: int) -> str:
    """The stage as written, up to a bracket that closes a block around it."""
    depth = 0
    for index in range(start, end):
        if code[index] in "([{":
            depth += 1
        elif code[index] in ")]}":
            if not depth:
                end = index
                break
            depth -= 1
    text = " ".join(command[start:end].split())
    if len(text) > _STAGE_SHOWN_CHARS:
        text = text[: _STAGE_SHOWN_CHARS - 3] + "..."
    return text


def _hint_first_stage(
    command: str,
    exit_code: int | None,
    output: str,
    output_lines: int,
    failed_programs: Sequence[str],
) -> str | None:
    """Explain a program that failed in a pipeline with a Select-Object -First stage.

    Once N lines have passed Select-Object -First N, PowerShell closes the
    output of the commands before it and moves on without waiting for them. A
    native program that writes no more finishes its work; one that writes again
    fails at that write: ``python build.py | Select-Object -First 8`` died at its
    ninth line and never wrote the file it writes at the end, exit code 0. The
    stage also drops an error the program merged into the output after line N:
    a script's own usage error and an HTTP error read as a stop it was not.

    So the stage must directly follow the program, as the first stage of a
    top-level statement without && or ||, and that program must have a failed
    run. A stop is named only with the closed output's error in the output and
    the program run once. Otherwise the stage must provably have passed N lines:
    the program's errors went into it (2>&1), it is the statement's last stage,
    and every other statement prints nothing or a literal string, so the
    statement's own lines are known. A stage behind another one, such as
    Select-String, or inside a block passed fewer lines than the command printed
    (Swarm run 2026-10-20: 4 of 6 such hints).
    """
    if not _common._POWERSHELL or "|" not in command or not failed_programs:
        return None
    flat = _powershell_code(command).flat
    statements = _top_level_statements(flat)
    window = _scan_window(output)
    for left, right in statements:
        if _top_level_split(flat, left, right, _CHAIN):
            continue
        pipes = _top_level_split(flat, left, right, _PIPE)
        if not pipes:
            continue
        stage = _FIRST_STAGE.match(flat, pipes[0], pipes[1] if len(pipes) > 1 else right)
        if stage is None:
            continue
        count = _FIRST_COUNT.search(stage["arguments"])
        if count is None or _WAIT.search(stage["arguments"]) or int(count[1]) <= 0:
            continue
        lines = int(count[1])
        program = _program_at(command, flat, left, pipes[0])
        codes = [] if program is None else _exit_codes(failed_programs, program)
        if program is None or not codes:
            continue
        written = _stage_text(command, flat, stage.start() + 1, stage.end())
        counted = f"{lines} line{'' if lines == 1 else 's'}"
        zero = ", although the command exited with code 0" if exit_code == 0 else ""
        runs = sum(
            _program_at(command, flat, run.end()) == program for run in _RUN_START.finditer(flat)
        ) + (_program_at(command, flat, 0) == program)
        closed = _CLOSED_OUTPUT.search(window)
        if closed is not None and runs == 1 and lines <= output_lines:
            return (
                f"`{written}` closed {program}'s output after {counted}, and {program} failed "
                f"at its next write ({closed[0].strip()}){zero}: what {program} does after "
                "that, such as writing files, did not happen. To let it finish, run it "
                "without that stage (long output keeps its first and last lines), or add "
                "-Wait to Select-Object."
            )
        # Errors that bypassed the stage count among the statement's lines.
        if len(pipes) > 1 or not _MERGED_ERRORS.search(flat, left, stage.start()):
            continue
        own = _own_lines(command, flat, statements, left, output_lines)
        if own is None or own < lines:
            continue
        parts: list[str] = []
        if not _ERROR_LINE.search(window):
            parts.append(
                f"Its output, errors included (2>&1), went through `{written}`, which passes on "
                f"only the first {counted} and then closes that output: the lines that say why "
                f"{program} failed can be missing, or {program} failed writing after the close. "
                "To see why it failed, run it again without that stage (long output keeps its "
                "first and last lines)."
            )
        if _LAST_EXIT_CODE.search(command, stage.end()):
            # PowerShell moved on without waiting for the program's exit; the
            # variable is read in the command itself or in an expandable string.
            parts.append(
                f"`{written}` stopped waiting for {program} once {counted} had passed it, so "
                f"$LASTEXITCODE after it does not hold {program}'s exit code."
            )
        if not parts:
            continue
        listed = codes[0] if len(codes) == 1 else f"{', '.join(codes[:-1])} and {codes[-1]}"
        return (
            f"{program} exited with code{'s' if len(codes) > 1 else ''} {listed}{zero}. "
            + " ".join(parts)
        )
    return None


# Statements that print nothing: a change of location, or an assignment.
_LOCATION_CHANGE = re.compile(
    r"(?:cd|chdir|sl|Set-Location|Push-Location|pushd|Pop-Location|popd)(?:[ \t]+[^|&(){}]*+)?",
    re.IGNORECASE,
)
_ASSIGNMENT = re.compile(r"\$[\w:]+[ \t]*+[-+*/]?=(?!=)")
# A statement that prints one literal string.
_LITERAL_PRINT = re.compile(
    r"(?:(?:Write-Output|Write-Host|echo|write)[ \t]+)?(?:'[^']*+'|\"[^\"`]*+\")", re.IGNORECASE
)


def _own_lines(
    command: str, flat: str, statements: list[tuple[int, int]], own: int, output_lines: int
) -> int | None:
    """The lines the statement starting at *own* printed: the command's lines
    less the other statements', when each of those prints nothing or a literal
    string; None when another statement can print more."""
    other = 0
    for left, right in statements:
        if left == own:
            continue
        code = flat[left:right].strip()
        text = command[left:right].strip()
        if _LOCATION_CHANGE.fullmatch(code) and not re.search(r"-pa", code, re.IGNORECASE):
            continue
        assignment = _ASSIGNMENT.match(code)
        if assignment is not None:
            value = code[assignment.end() :]
            if "&" in value or re.search(r"write-host", value, re.IGNORECASE):
                return None
            if any(_working_program(word[0]) for word in _COMMAND_WORD.finditer(value)):
                return None
            continue
        if _LITERAL_PRINT.fullmatch(text) and "$(" not in text:
            other += text.count("\n") + 1
            continue
        return None
    return output_lines - other


_CHAIN = re.compile(r"&&|\|\|")
_PIPE = re.compile(r"\|(?!\|)")


def _hint_skipped_after_pipeline(
    command: str, exit_code: int | None, failed_programs: Sequence[str]
) -> str | None:
    """Name what `&&` skipped after a pipeline whose program failed.

    In bash, ``checker | head -n 6 && next`` runs ``next`` after head's
    success. In PowerShell a pipeline fails when a program in it fails, so
    ``&&`` skipped ``next``, and the result showed only the checker's lines.
    Read for the command's last statement, whose outcome is the exit code; a
    Select-Object -First stage that stops the pipeline early lets it succeed.
    """
    if not _common._POWERSHELL or not exit_code or "&&" not in command or not failed_programs:
        return None
    parsed = _powershell_code(command)
    code, flat = parsed.text, parsed.flat
    statements = _top_level_statements(flat)
    if not statements:
        return None
    start, end = statements[-1]
    operators = _top_level_split(flat, start, end, _CHAIN)
    if not operators or any(flat.startswith("||", at) for at in operators):
        return None
    bounds = [start, *(at + 2 for at in operators)]
    ends = [*operators, end]
    for number, (left, right) in enumerate(zip(bounds[:-1], ends[:-1], strict=True)):
        pipes = _top_level_split(flat, left, right, _PIPE)
        if not pipes:
            continue
        stops_early = any(
            _FIRST_COUNT.search(stage["arguments"]) and not _WAIT.search(stage["arguments"])
            for stage in _FIRST_STAGE.finditer(code, left, right)
        )
        program = _program_name(command, code, left, pipes[0])
        if stops_early or program is None:
            continue
        codes = _exit_codes(failed_programs, program)
        if str(exit_code) not in codes:
            continue
        runs = sum(
            entry.partition(" exited with code ")[0].lower().removesuffix(".exe") == program
            for entry in failed_programs
        )
        if runs > 1:
            # Another run of the program failed too, maybe the part after &&.
            return None
        pipeline = _stage_text(command, code, left, right)
        skipped = _stage_text(command, code, bounds[number + 1], ends[number + 1])
        more = " and what follows it" if number + 2 < len(bounds) else ""
        return (
            f"In PowerShell, a pipeline fails when a program in it fails: {program} exited with "
            f"code {exit_code} in `{pipeline}`, so && did not run `{skipped}`{more}. To run it "
            "whatever that exit code, put it after ; instead of &&."
        )
    return None


# PowerShell's background operator: an & that is not doubled (&&), escaped (`&) or
# part of a redirection (2>&1). After a pipeline's opening it is the call operator.
_AMPERSAND = re.compile(r"(?<![&`>])&(?!&)")
_PIPELINE_OPENINGS = frozenset("\n;{(|=,")


def ended_jobs_hint(command: str) -> str | None:
    """Say that jobs the PowerShell *command* started with & ended with it.

    PowerShell runs a pipeline that ends with & as a job, whose host process it
    ends as it exits; Agents wrote a trailing & to keep a server running, as in
    bash. ``None`` when the command has no such &, such as one that started
    its jobs with Start-Job and waited for them.
    """
    flat = _powershell_code(command).flat
    for found in _AMPERSAND.finditer(flat):
        before = flat[: found.start()].rstrip(" \t\r")
        if before and before[-1] not in _PIPELINE_OPENINGS:
            return (
                "Jobs started with & ended when the command ended. To keep a program running "
                "after the command, run it without & and with run_in_background."
            )
    return None


# A shell command that waits, then reads the log file of a vBot background command.
_SLEEP = re.compile(r"(?<![\w-])(?:Start-Sleep|sleep)(?![\w-])", re.I)
_COMMAND_LOG = re.compile(r"[/\\]commands[/\\]tmp_[0-9a-z]{12}\.log", re.I)


def _hint_waiting_for_background(command: str) -> str | None:
    """Answer a command that sleeps to read a background command's log.

    Agents waited in loops of ``Start-Sleep 90; Get-Content <log> -Tail 3``
    for a background command whose result arrives as a message anyway.
    """
    if not _SLEEP.search(command) or not _COMMAND_LOG.search(command):
        return None
    return (
        "This command waited, then read a background command's log file. That command's "
        "result arrives as a new message when it exits: instead of waiting for it in a "
        "command, continue other work or end your turn."
    )


def _hint_hidden_failure(
    command: str,
    exit_code: int | None,
    output: str,
    offers: Callable[[str], bool],
    permissions: PermissionBoundary,
) -> str | None:
    """Name a statement PowerShell could not run behind exit code 0.

    ``checker 2>&1 | grep ...; python -c ...`` exited with 0 from its last
    statement: grep is not recognized, so no element of that pipeline ran, and
    the Agent read the checker's missing output as a pass. A redirection to
    /dev/null fails the same way: PowerShell cannot open ``C:\\dev\\null``, so
    the statement does not run. Only a name the command itself calls counts,
    not one an error message in the output merely mentions.
    """
    if not _common._POWERSHELL or exit_code != 0:
        return None
    window = _scan_window(output)
    missing = _POWERSHELL_NOT_RECOGNIZED.search(window)
    if missing is not None and _calls(command, missing[1]):
        # The hint names the statement or the pipeline part that did not run.
        hint = _hint_powershell_command_not_found(
            _Failure(command, window, None, offers, permissions=permissions), exit_zero=True
        )
        if hint is not None:
            return hint
    if _DEV_NULL.search(command) and _DEV_NULL_RESOLVED.search(window):
        return (
            f"{_DEV_NULL_HINT} The statement with /dev/null did not run, although the command "
            "exited with code 0."
        )
    return None
