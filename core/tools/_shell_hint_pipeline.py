"""Pipeline failures, early termination and hidden outcomes."""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable, Sequence
from pathlib import PureWindowsPath

from . import _shell_hint_common as _common
from ._shell_hint_code import (
    _FIRST_STAGE,
    _PIPELINE_PROGRAM,
    _PIPELINE_START,
    _POWERSHELL_NOT_RECOGNIZED,
    _calls,
    _powershell_code,
    _program_at,
    _top_level_split,
    _top_level_statements,
)
from ._shell_hint_common import (
    _DEV_NULL,
    _DEV_NULL_HINT,
    _DEV_NULL_RESOLVED,
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
# error or fatal message, possibly after the program's name (argparse's
# "prog: error: ..."), or unittest's FAIL/ERROR lines.
_ERROR_LINE = re.compile(
    r"^[ \t]*+(?:[\w.]*(?:Error|Exception)(?::|$)"
    r"|(?:[\w.-]++:[ \t]*+)?(?:error|fatal|ERROR|FATAL|Error)\b[:!]?[ \t]"
    r"|FAIL(?:ED)?\b|npm ERR!)",
    re.M,
)


def _exit_codes(failed_programs: Sequence[str], program: str) -> list[str]:
    """The distinct codes *program*'s failed runs exited with, in order."""
    codes: dict[str, None] = {}
    for entry in failed_programs:
        name, _, code = entry.partition(" exited with code ")
        if code and name.lower().removesuffix(".exe") == program:
            codes[code.split(" ", 1)[0]] = None
    return list(codes)


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


_LITERAL_PYTHON_SELECTOR = re.compile(
    r"[ \t]+(?:-[bBdEiIOPqsSuvx]+[ \t]+)*(?P<module>-m[ \t]+)?"
    r"(?P<selector>\"[^\"\r\n]*+\"|'[^'\r\n]*+'|[^\s|;&<>(){}'\"]++)"
    r"(?=$|[\s|;&<>])"
)
_EXIT_WITH_INVOCATION = re.compile(r"(\d+)(?: \(0x[0-9A-F]{8}\))? \(([^\r\n]+)\)")


def _invocation_key(selector: str) -> str | None:
    """Comparable literal module/script identity; inline code has no identity."""
    if selector.startswith("-m "):
        module = selector[3:]
        return selector if all(part.isidentifier() for part in module.split(".")) else None
    if (
        not selector.lower().endswith((".py", ".pyw", ".pyz"))
        or any(char in selector for char in "$`*?[]")
        or not selector.isprintable()
    ):
        return None
    return PureWindowsPath(selector).as_posix().casefold()


def _first_invocation_failures(
    command: str, code: str, program: str, failed_programs: Sequence[str]
) -> dict[int, tuple[str, list[str]]]:
    """Correlate one failure only within simple, separate literal Python calls.

    Nested expressions, another command family, unknown selectors or an extra
    native pipeline stage leave attribution unknown. A repeated selector also
    cannot tell which invocation failed, even when its arguments differ.
    """
    if not re.fullmatch(r"pythonw?(?:\d+(?:\.\d+)*)?", program):
        return {}
    if re.search(r"[(){}=`]|\|\|", code):
        return {}
    selectors: dict[int, str] = {}
    occurrences: dict[str, int] = {}
    for left, right in _top_level_statements(code):
        if _program_at(command, code, left, right) != program:
            return {}
        first = _PIPELINE_PROGRAM.match(code, left, right)
        assert first is not None
        # Only the leading call operator and stream-merging redirections may
        # contain &: chains or another invocation would defeat the correlation.
        if "&" in re.sub(r"[0-6*]>&[0-6]", "", code[first.end() : right]):
            return {}
        for pipe in re.finditer(r"\|", code[first.end() : right]):
            stage = _PIPELINE_PROGRAM.match(code, first.end() + pipe.end(), right)
            if stage is None or stage[1].lower() not in (
                "select",
                "select-object",
                "select-string",
            ):
                return {}
        match = _LITERAL_PYTHON_SELECTOR.match(command, first.end(), right)
        if match is None:
            return {}
        selector = match["selector"]
        if selector[0] in "'\"":
            selector = selector[1:-1]
        if match["module"]:
            selector = f"-m {selector}"
        key = _invocation_key(selector)
        if key is None:
            return {}
        selectors[left] = key
        occurrences[key] = occurrences.get(key, 0) + 1
    failures: dict[str, tuple[str, list[str]]] = {}
    for entry in failed_programs:
        name, _, detail = entry.partition(" exited with code ")
        if name.lower().removesuffix(".exe") != program:
            continue
        failure = _EXIT_WITH_INVOCATION.fullmatch(detail)
        if failure is None or (key := _invocation_key(failure[2])) is None:
            continue
        _, codes = failures.setdefault(key, (failure[2], []))
        if failure[1] not in codes:
            codes.append(failure[1])
    return {
        left: failures[key]
        for left, key in selectors.items()
        if occurrences[key] == 1 and key in failures
    }


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

    So a failed run of the pipeline's program is required, and a stop is named
    only with the closed output's error in the output. Otherwise, when the stage
    received the program's errors (2>&1), the hint names the program's failure
    and that the stage can have hidden why. A program also run outside such a
    pipeline gets a hint only when a recorded literal Python selector identifies
    this invocation among simple, separate calls; their shared output cannot
    prove which call failed at a closed pipe.
    """
    if not _common._POWERSHELL or "|" not in command or not failed_programs:
        return None
    code = _powershell_code(command).flat
    starts = [0, *(match.end() for match in _PIPELINE_START.finditer(code))]
    staged: dict[int, tuple[re.Match[str], int]] = {}
    for stage in _FIRST_STAGE.finditer(code):
        count = _FIRST_COUNT.search(stage["arguments"])
        if count is None or _WAIT.search(stage["arguments"]):
            continue
        start = starts[bisect.bisect_right(starts, stage.start()) - 1]
        if start not in staged and 0 < int(count[1]) <= output_lines:
            staged[start] = (stage, int(count[1]))
    if not staged:
        return None
    occurrences: dict[str, int] = {}
    for start in starts:
        if (program := _program_at(command, code, start)) is not None:
            occurrences[program] = occurrences.get(program, 0) + 1
    # Correlation requires every statement to call the same Python program, so
    # one attempt also settles every other candidate, without rescanning.
    invocations: dict[int, tuple[str, list[str]]] | None = None
    window = _scan_window(output)
    for start, (stage, lines) in staged.items():
        program = _program_at(command, code, start, stage.start())
        if program is None:
            continue
        correlated = occurrences.get(program, 0) > 1
        if correlated:
            if invocations is None:
                invocations = _first_invocation_failures(command, code, program, failed_programs)
            identity = invocations.get(start)
            if identity is None:
                continue
            selector, codes = identity
            program = f"{program} {selector}"
        else:
            codes = _exit_codes(failed_programs, program)
        if not codes:
            continue
        written = _stage_text(command, code, stage.start() + 1, stage.end())
        counted = f"{lines} line{'' if lines == 1 else 's'}"
        zero = ", although the command exited with code 0" if exit_code == 0 else ""
        closed = None if correlated else _CLOSED_OUTPUT.search(window)
        if closed is not None:
            return (
                f"`{written}` closed {program}'s output after {counted}, and {program} failed "
                f"at its next write ({closed[0].strip()}){zero}: what {program} does after "
                "that, such as writing files, did not happen. To let it finish, run it "
                "without that stage (long output keeps its first and last lines), or add "
                "-Wait to Select-Object."
            )
        parts: list[str] = []
        # Its errors went into the stage, and the output shows no line saying why.
        if _MERGED_ERRORS.search(code, start, stage.start()) and not _ERROR_LINE.search(window):
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


_CHAIN = re.compile(r"&&|\|\|")
_PIPE = re.compile(r"\|(?!\|)")


def _program_name(command: str, code: str, start: int, end: int) -> str | None:
    """The program a pipeline between *start* and *end* starts with, lowercase without .exe."""
    first = _PIPELINE_PROGRAM.match(code, start, end)
    if first is None:
        return None
    name = re.split(r"[/\\]", command[first.start(1) : first.end(1)].strip("'\""))[-1].lower()
    return name.removesuffix(".exe") or None


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
    command: str, exit_code: int | None, output: str, offers: Callable[[str], bool]
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
        hint = _hint_powershell_command_not_found(_Failure(command, window, None, offers))
        if hint is not None:
            return (
                f"{hint} The statement with `{missing[1]}` did not run, although the command "
                "exited with code 0."
            )
    if _DEV_NULL.search(command) and _DEV_NULL_RESOLVED.search(window):
        return (
            f"{_DEV_NULL_HINT} The statement with /dev/null did not run, although the command "
            "exited with code 0."
        )
    return None
