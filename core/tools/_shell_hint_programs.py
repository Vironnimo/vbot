"""Missing-program and ordinary tool failure diagnostics."""

from __future__ import annotations

import re
from collections.abc import Callable

from core.utils.edit_distance import edit_distance

from . import _shell_hint_common as _common
from ._shell_hint_code import (
    _POWERSHELL_NOT_RECOGNIZED,
    _powershell_code,
    _quoted_in,
    _skipped_before,
)
from ._shell_hint_common import (
    _BASH_SYNTAX_HINT,
    _READ_TOOL,
    _SEARCH_TOOL,
    _TERMINAL_TOOL,
    _Failure,
)

# A title in place of a command: one line of at least three plain words that
# starts with a capitalized word in any language, such as "Check shell files
# compile and behave"; the first word can end in a hyphen, as German "Quell- und
# ..." does. Programs are rarely capitalized; a cmdlet's hyphen, a flag or a
# file's dot ends the match.
_PROSE = re.compile(r"([^\W\d_]+)-?(?:[ \t]+[^\W_][\w'’-]*){2,}\.?")


def _description_as_command(failure: _Failure, missing: str) -> str | None:
    """The hint for a call whose command holds a description, or None.

    Agents sent the title meant for description as the command; the shell ran
    its first word, and the not-installed hint sent them to install `Check`.
    A command equal to the call's description proves nothing: Models also copy
    a short command into description.
    """
    command = failure.command.strip()
    prose = _PROSE.fullmatch(command)
    if prose is None or command.split(maxsplit=1)[0] != missing:
        return None
    first = prose[1]
    if not first[0].isupper() or not (first[1:].islower() or len(first) == 1):
        return None
    return (
        f"command holds a description, not a command, so its first word `{missing}` was run "
        "as a program. Send the command to run in command, and the description in description."
    )


# POSIX: bash/sh report "<name>: command not found", after a prefix such as
# "bash: line 1: " or "sh: 1: ". The name is a whole run of name characters; the
# lookbehind keeps a failed attempt from rescanning the rest of the same run.
_COMMAND_NOT_FOUND = re.compile(r"(?<![\w.+-])([\w.+-]++): command not found")


def _hint_command_not_found(failure: _Failure) -> str | None:
    match = _COMMAND_NOT_FOUND.search(failure.output)
    if not match:
        return None
    missing = match.group(1)
    described = _description_as_command(failure, missing)
    if described is not None:
        return described
    if missing == "python":
        return (
            "This system has no `python` command: use `python3`, or the interpreter of the "
            "project's virtual environment, such as `.venv/bin/python`."
        )
    if missing == "pip":
        return (
            "This system has no `pip` command: use `pip3`, `python3 -m pip`, or the pip of "
            "the project's virtual environment, such as `.venv/bin/pip`."
        )
    return (
        f"`{missing}` is not installed or not on PATH. Check with `command -v {missing}`; "
        "install it or run it by its absolute path instead of retrying the same command."
    )


def _grep_hint(failure: _Failure) -> str:
    hint = (
        "`grep` does not exist in PowerShell: filter command output with "
        "`| Select-String 'text'` (add -CaseSensitive to match grep, -NotMatch for -v)"
    )
    if failure.offers(_SEARCH_TOOL):
        return f"{hint}, and search files with {_SEARCH_TOOL}."
    return f"{hint}, and search files with `Get-ChildItem -Recurse -File | Select-String 'text'`."


def _sed_hint(failure: _Failure) -> str:
    edit_tool = failure.edit_tool()
    change = (
        f"change files with {edit_tool}, replace text in command output with "
        "`-replace 'old', 'new'`"
        if edit_tool
        else "replace text in a file with `(Get-Content f) -replace 'old', 'new' | Set-Content f`"
    )
    lines = (
        f"read a line range with {_READ_TOOL}"
        if failure.offers(_READ_TOOL)
        else "print a line range with `Get-Content f | Select-Object -Skip 9 -First 10`"
    )
    return f"`sed` does not exist in PowerShell: {change}, and {lines}."


# Long output keeps its first and last lines on its own, so the hints for head and
# tail point there instead of to a trim stage, which holds or cuts the output.
_UNTRIMMED_OUTPUT = (
    "Run the command without it: long output keeps its first and last lines, and the "
    "result names a file with all of it."
)


def _head_hint(failure: _Failure) -> str:
    lines = (
        f"Read the first lines of a file with {_READ_TOOL} and limit."
        if failure.offers(_READ_TOOL)
        else "Print the first lines of a file with `Get-Content path -TotalCount 10`."
    )
    return f"`head` does not exist in PowerShell. {_UNTRIMMED_OUTPUT} {lines}"


def _tail_hint(failure: _Failure) -> str:
    lines = (
        f"Read the last lines of a file with {_READ_TOOL} and a negative offset, such as -20."
        if failure.offers(_READ_TOOL)
        else "Print the last lines of a file with `Get-Content path -Tail 20`."
    )
    return f"`tail` does not exist in PowerShell. {_UNTRIMMED_OUTPUT} {lines}"


# PowerShell equivalents for bash commands Agents reach for on a Windows host.
_POWERSHELL_EQUIVALENTS: dict[str, str | Callable[[_Failure], str]] = {
    "grep": _grep_hint,
    "egrep": _grep_hint,
    "fgrep": _grep_hint,
    "sed": _sed_hint,
    "awk": (
        "`awk` does not exist in PowerShell: split fields with "
        "`ForEach-Object { ($_ -split '\\s+')[1] }`."
    ),
    "head": _head_hint,
    "tail": _tail_hint,
    "wc": (
        "`wc` does not exist in PowerShell: count lines with `(Get-Content path).Count` or "
        "`(command | Measure-Object).Count`, and words or characters with "
        "`Measure-Object -Word -Character`."
    ),
    "which": "`which` does not exist in PowerShell: use `(Get-Command name).Source`.",
    "touch": (
        "`touch` does not exist in PowerShell: create a file with `New-Item -ItemType File path`."
    ),
    "test": (
        "`test` and `[ ]` are bash: in PowerShell use `Test-Path path` (add `-PathType Leaf` or "
        "`-PathType Container` for -f or -d) inside `if (...) { ... }`."
    ),
    "env": (
        "`env` is bash: list environment variables with `Get-ChildItem env:` and read one with "
        "`$env:NAME`."
    ),
    "chmod": (
        "`chmod` has no PowerShell equivalent and Windows needs none: run the script directly, "
        "such as `./run.ps1` or `python script.py`."
    ),
    "export": "PowerShell sets environment variables with `$env:NAME = 'value'`, not export.",
    "source": "PowerShell runs a script in the current scope with `. ./script.ps1`, not source.",
    "true": (
        "`true` is not a PowerShell command: drop `|| true`, since a failing command "
        "does not stop the script."
    ),
    "xargs": "`xargs` does not exist in PowerShell: pipe into `ForEach-Object { command $_ }`.",
    "dirname": "Use `Split-Path path -Parent` in PowerShell instead of dirname.",
    "basename": "Use `Split-Path path -Leaf` in PowerShell instead of basename.",
    "python3": "Windows runs Python as `python` (or `py`), not python3.",
    "pip3": "Windows runs pip as `python -m pip`, not pip3.",
}
_BASH_SYNTAX_WORDS = frozenset({"if", "then", "fi", "do", "done", "esac", "elif"})


_CMDLET_CALL = re.compile(r"(?:^|[;\n|])[ \t]*+([A-Za-z]+-[A-Za-z]+)(?=\s|[;|()]|$)")


def _cmdlet_spelling_in_command(command: str, missing: str) -> str | None:
    """Suggest one nearby same-verb command already called, never a name in text."""
    # The shared edit distance is for short names; bound its work independently
    # of command size so scanning an oversized name cannot become quadratic.
    if len(missing) > 128 or re.fullmatch(r"[A-Za-z]+-[A-Za-z]+", missing) is None:
        return None
    folded = missing.lower()
    verb = folded.partition("-")[0]
    candidates = {
        name.lower(): name
        for match in _CMDLET_CALL.finditer(_powershell_code(command).flat)
        if (name := match[1]).lower() != folded
        and name.lower().partition("-")[0] == verb
        and edit_distance(name.lower(), folded, 1) == 1
    }
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def _hint_powershell_command_not_found(failure: _Failure, *, exit_zero: bool = False) -> str | None:
    """The hint for a term PowerShell did not recognize, and what it did not run.

    A missing later stage of a pipeline skipped the stages before it: Agents
    read the missing output of ``node t.js 2>&1 | tail -5`` as the program's
    and repeated the call. *exit_zero*: the command exited with code 0, which
    the hint then says did not show the failure.
    """
    # pwsh reports "The term 'X' is not recognized as a name of a cmdlet, function,
    # script file, or executable program."
    match = _POWERSHELL_NOT_RECOGNIZED.search(failure.output)
    if not match:
        return None
    missing = match.group(1)
    hint = _not_recognized_hint(failure, missing)
    if hint is None:
        return None
    zero = ", although the command exited with code 0" if exit_zero else ""
    skipped = _skipped_before(failure.command, missing)
    if skipped:
        parts = [f"`{part}`" for part in skipped]
        listed = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"
        return (
            f"{hint} PowerShell runs no part of a pipeline with a command it cannot find, so "
            f"{listed} did not run{zero}."
        )
    if exit_zero:
        return f"{hint} The statement with `{missing}` did not run{zero}."
    return hint


# A rooted path: a drive, or the current drive's root.
_ROOTED_PATH = re.compile(r"[A-Za-z]:[/\\]|/")
# A statement that changes the current location.
_LOCATION_CHANGE_CALL = re.compile(
    r"(?:^|[;\n{(|&])[ \t]*+(?:cd|chdir|sl|Set-Location|Push-Location|pushd|Pop-Location|popd)"
    r"(?=[ \t;\n)}]|$)",
    re.IGNORECASE,
)


def _missing_program_file(failure: _Failure, missing: str) -> str:
    """The hint for a program PowerShell did not find at the path the command gave.

    A path names a file, not a program to install: a venv's
    ``.\\backend\\.venv\\Scripts\\python.exe`` called from another directory and a
    mistyped absolute path got the not-installed hint, which also told them to
    use an absolute path (Sessions 2026-10-26 and 2026-10-27, 3 calls). A
    relative path names the working directory it was resolved from when the
    command does not change its location and the Session may read it.
    """
    if _ROOTED_PATH.match(missing):
        return (
            f"No file exists at `{missing}`, so PowerShell could not run it. Check the path for "
            "a typo, or find where the program is installed."
        )
    where = "the current location"
    workdir = failure.workdir
    if (
        workdir is not None
        and _LOCATION_CHANGE_CALL.search(_powershell_code(failure.command).flat) is None
        and failure.permissions.check_path(workdir, "read") is None
    ):
        where = f"the working directory `{workdir.as_posix()}`"
    return (
        f"No file `{missing}` exists relative to {where}, so PowerShell could not run it. "
        "Run it from the directory the path starts in, or give its absolute path."
    )


def _not_recognized_hint(failure: _Failure, missing: str) -> str | None:
    described = _description_as_command(failure, missing)
    if described is not None:
        return described
    equivalent = _POWERSHELL_EQUIVALENTS.get(missing)
    if equivalent is not None:
        return equivalent if isinstance(equivalent, str) else equivalent(failure)
    if "=" in missing and not missing.startswith("="):
        name, value = missing.split("=", 1)
        return (
            f"PowerShell sets a variable with `$env:{name} = '{value}'` on its own line "
            f"before the command, not `{missing} command`."
        )
    if missing in _BASH_SYNTAX_WORDS:
        return _BASH_SYNTAX_HINT
    if spelling := _cmdlet_spelling_in_command(failure.command, missing):
        return (
            f"`{missing}` was not found. Did you mean `{spelling}`, "
            "which also appears as a command in your script?"
        )
    if re.search(r"\s|\$\(", missing) or missing.startswith("\\"):
        # Text PowerShell ran as a command, such as a string that lost its
        # quotes: no program to install.
        return None
    if re.search(r"[/\\]", missing):
        return _missing_program_file(failure, missing)
    return (
        f"`{missing}` is not installed or not on PATH. Check with `Get-Command {missing}`; "
        "install it or run it by its absolute path instead of retrying the same command."
    )


# Windows' app execution alias for Python when no Python is installed under that
# name: it prints this message and exits with code 9009.
_STORE_PYTHON_MESSAGE = (
    "Python was not found; run without arguments to install from the Microsoft Store"
)
_PYTHON3 = re.compile(r"(?<![\w.-])python3(?:\.exe)?(?![\w.-])", re.I)


def _hint_store_python(failure: _Failure) -> str | None:
    if _STORE_PYTHON_MESSAGE not in failure.output or not _PYTHON3.search(failure.command):
        return None
    return "`python3` is the Microsoft Store placeholder on this machine: run `python` instead."


# Windows' find.exe searches text in files; its own messages start with "FIND: "
# in every display language, such as "FIND: Parameter format not correct".
_WINDOWS_FIND_MESSAGE = re.compile(r"^FIND: ", re.M)
_FIND = re.compile(r"(?<![\w.-])find(?:\.exe)?(?![\w.-])", re.I)


def _hint_windows_find(failure: _Failure) -> str | None:
    if not _WINDOWS_FIND_MESSAGE.search(failure.output) or not _FIND.search(failure.command):
        return None
    hint = (
        "`find` here is the Windows text-search program, not Unix find: list files with "
        "`Get-ChildItem -Recurse -Filter '*.md'`"
    )
    if failure.offers(_SEARCH_TOOL):
        return f"{hint}, or use {_SEARCH_TOOL}."
    return f"{hint}."


# PowerShell aliases that look like Unix commands but take cmdlet parameters.
_ALIAS_ADVICE: dict[str, tuple[tuple[str, ...], str]] = {
    "Get-ChildItem": (
        ("ls", "dir", "gci"),
        "use `Get-ChildItem -Force` to include hidden files and `-Recurse` for subdirectories",
    ),
    "Remove-Item": (("rm", "del", "rmdir", "rd"), "use `Remove-Item path -Recurse -Force`"),
    "Copy-Item": (("cp", "copy", "cpi"), "use `Copy-Item source destination -Recurse`"),
    "Get-Process": (("ps",), "use `Get-Process`, or `Get-Process name` to filter"),
    "Stop-Process": (("kill",), "use `Stop-Process -Id <pid> -Force`"),
}


def _hint_powershell_alias_flags(failure: _Failure) -> str | None:
    """A bash flag on a PowerShell alias, such as ``ls -la``, fails parameter binding."""
    for cmdlet, (aliases, advice) in _ALIAS_ADVICE.items():
        # A terminal drops the space after the cmdlet in PowerShell's
        # multi-line error header.
        if not re.search(rf"^{cmdlet}:(?: |$)", failure.output, re.M):
            continue
        for alias in aliases:
            match = re.search(rf"(?:^|[\s;|&(]){alias}\s+-([A-Za-z]+)", failure.command, re.M)
            if match and _quoted_in(match.group(1), failure.output):
                return (
                    f"`{alias} -{match.group(1)}` uses bash flags, but in PowerShell `{alias}` "
                    f"is {cmdlet}: {advice}."
                )
    return None


# -Recurse after Select-String in the same pipeline step, searched in the command
# with its strings blanked, so a | inside a quoted -Pattern does not end the step.
# An attempt ends at the next Select-String, which starts its own, so a long
# command is scanned once.
_SELECT_STRING_RECURSE = re.compile(r"Select-String\b(?:(?!Select-String\b)[^|;\n])*-Recurse", re.I)


def _hint_select_string_recurse(failure: _Failure) -> str | None:
    if "Select-String" not in failure.output or not _quoted_in("Recurse", failure.output):
        return None
    if not _SELECT_STRING_RECURSE.search(_powershell_code(failure.command).flat):
        return None
    hint = (
        "Select-String has no -Recurse: pipe files in with "
        "`Get-ChildItem -Recurse -File | Select-String 'text'`"
    )
    if failure.offers(_SEARCH_TOOL):
        return f"{hint}, or search with {_SEARCH_TOOL}."
    return f"{hint}."


_CONFLICT = re.compile(r"^CONFLICT |Automatic merge failed|needs merge", re.M)
# The git operation a conflict stopped, by the first word of the command that names
# one, in this order: a pull with --rebase is a rebase.
_CONFLICT_OPERATIONS = (
    ("rebase", re.compile(r"\brebase\b")),
    ("cherry-pick", re.compile(r"\bcherry-pick\b")),
    ("revert", re.compile(r"\brevert\b")),
    ("stash", re.compile(r"\bstash\b")),
    ("merge", re.compile(r"\b(?:merge|pull)\b")),
)


def _hint_merge_conflict(failure: _Failure) -> str | None:
    if not _CONFLICT.search(failure.output):
        return None
    operation = next(
        (name for name, pattern in _CONFLICT_OPERATIONS if pattern.search(failure.command)),
        None,
    )
    if operation == "stash":
        return (
            "Git applied the stash with conflicts in the files the output lists. Resolve them "
            "and `git add` them; the stash stays in the stash list until `git stash drop` "
            "removes it."
        )
    if operation is None:
        return (
            "Git stopped at a conflict in the files the output lists. Resolve them and `git add` "
            "them, then run the stopped git command with --continue, or with --abort to "
            "undo it."
        )
    return (
        f"Git stopped the {operation} at a conflict in the files the output lists. Resolve them "
        f"and `git add` them, then run `git {operation} --continue`; "
        f"`git {operation} --abort` undoes the {operation} instead."
    )


# A command gets no input and has no terminal: PowerShell runs -NonInteractive, so
# Read-Host, Get-Credential and confirmation prompts fail at once (English and
# German PowerShell).
_POWERSHELL_PROMPT = re.compile(r"PowerShell is in NonInteractive mode|Nichtinteraktiv-Modus")


def _hint_powershell_prompt(failure: _Failure) -> str | None:
    if not _POWERSHELL_PROMPT.search(failure.output):
        return None
    return (
        "PowerShell cannot prompt in a command: pass the value as a parameter instead of "
        "Read-Host or Get-Credential, and add -Confirm:$false or -Force to a cmdlet that asks "
        "for confirmation."
    )


def _hint_container_tty(failure: _Failure) -> str | None:
    # docker and podman run -t (or -it) refuse an input that is no terminal.
    if "the input device is not a tty" not in failure.output.casefold():
        return None
    hint = (
        "Commands run without a terminal: run the container without -t, and without -i "
        "unless you pipe input into it."
    )
    if failure.offers(_TERMINAL_TOOL):
        return f"{hint} To work inside the container interactively, run it with {_TERMINAL_TOOL}."
    return hint


# A program that asked for input and read the end of its empty input: Python's
# input() (EOFError), programs that check for a terminal first, prompt libraries
# (inquirer: "User force closed the prompt"; click: "Aborted!" or its exception).
_END_OF_INPUT = re.compile(
    r"EOF when reading|stdin is not a terminal|not a tty|Inappropriate ioctl for device"
    r"|User force closed the prompt|click\.exceptions\.Abort|^Aborted!$",
    re.IGNORECASE | re.MULTILINE,
)


def _hint_end_of_input(failure: _Failure) -> str | None:
    if not _END_OF_INPUT.search(failure.error):
        return None
    hint = (
        "The program asked for input, and commands get none. Pass the answer as an option or "
        "argument, such as -y or --yes, or pipe it into the program within the command."
    )
    if failure.offers(_TERMINAL_TOOL):
        return f"{hint} To answer it yourself, run the program with {_TERMINAL_TOOL}."
    return hint


def _hint_already_exists(failure: _Failure) -> str | None:
    match = re.search(r"(?:fatal|error):.*?'([^']+)' already exists", failure.error)
    if not match:
        return None
    return (
        f"'{match.group(1)}' already exists, so the same command fails again. Reuse it, "
        "choose another name, or delete it first if it is no longer needed."
    )


_PORT_IN_USE = re.compile(
    r"(?:address already in use|"
    r"port (\d+) (?:is )?already in use|"
    r"bind\(\) to .*? port (\d+)|"
    r"bind on address \([^)]*,\s*(\d+)|"
    r"Only one usage of each socket address)",
    re.I,
)


def _hint_port_in_use(failure: _Failure) -> str | None:
    match = _PORT_IN_USE.search(failure.error)
    if not match:
        return None
    port = next((group for group in match.groups() if group), "")
    subject = f"Port {port} is" if port else "The requested port is"
    return (
        f"{subject} already in use, possibly by an earlier start of the same service. Check "
        "whether that service already runs before starting it again; otherwise stop the "
        "program on the port or choose another port."
    )


# PowerShell's and Windows' own wording of a refused access.
_ACCESS_DENIED = re.compile(r"Access (?:to the path .*? )?is denied")


def _hint_permission_denied(failure: _Failure) -> str | None:
    output = failure.error
    if (
        "Permission denied" not in output
        and "EACCES" not in output
        and not (_common._POWERSHELL and _ACCESS_DENIED.search(output))
    ):
        return None
    if _common._POWERSHELL:
        return (
            "Access to the path was denied. Check the path's type and access permissions; "
            "on Windows, reading a directory as a file can cause this error."
        )
    return (
        "Permission denied: your user cannot access the target path. Check its owner and "
        "permissions, or use a location your user can write; if the task needs elevated "
        "rights, ask the user to run the command."
    )


def _hint_rate_limit(failure: _Failure) -> str | None:
    error = failure.error
    if "API rate limit" not in error and "was submitted too quickly" not in error:
        return None
    return (
        "The API rate limit is reached, so an immediate retry fails the same way. Continue "
        "with other work and retry this operation later."
    )


def _hint_gh_unknown_json_field(failure: _Failure) -> str | None:
    match = re.search(r"Unknown JSON field: \"?(\w+)", failure.output)
    if not match:
        return None
    return (
        f"The installed gh has no JSON field '{match.group(1)}'. Use only fields from the "
        "list of valid fields in the output."
    )
