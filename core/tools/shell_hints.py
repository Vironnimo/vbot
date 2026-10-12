"""Failure hints for the shell Tool: one next action for well-known failure output.

When a command exits with a non-zero code, its raw output often sends Models
into wasted turns: retrying ``python`` where only ``python3`` exists, writing
bash in PowerShell, re-sending a gh field list the installed gh does not
support, or rerunning a merge that stopped at a conflict. A bounded scan of the
command and its output maps well-known failure shapes to one short hint that
names the next action.

Design rules (keep them when adding a pattern):

* Only non-zero exit codes get a hint, except for a command that did not run as
  written, which the command text shows: a typographic quote that ended a
  PowerShell string, or a program that failed in a pipeline with a
  Select-Object -First stage.
* At most one hint per result; the first match wins, and patterns are ordered
  by how often their failure shape wastes a turn.
* Only the first and the last ``_SCAN_CHARS`` characters of output are
  scanned: hints key on error headers, which open the output or, after long
  progress output, end it.
* A Python failure is judged from the last traceback's exception line on
  (``_Failure.error``): what a program printed before it, such as a probe's
  data, is not its failure.
* The command is not shortened, so a pattern over it runs in linear time: one
  whose failed attempts each scan on to the end of the command is quadratic and
  can stall the Event Loop.
* A hint states the next action in one or two sentences. It names another Tool
  only when the Agent is offered that Tool.
* No I/O beyond existence checks in the command's working directory.
"""

from __future__ import annotations

import bisect
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# The shell Tool runs commands in PowerShell on Windows and in bash elsewhere.
_POWERSHELL = sys.platform == "win32"
# Bounded scan windows at both ends: error headers open short output and end
# long output; the middle is noise.
_SCAN_CHARS = 4000

# Registry names of the Tools hints can point to.
_SEARCH_TOOL = "search_files"
_READ_TOOL = "read"
_EDIT_TOOLS = ("apply_patch", "edit")
_TERMINAL_TOOL = "terminal"
HINT_TOOL_NAMES = (_SEARCH_TOOL, _READ_TOOL, *_EDIT_TOOLS, _TERMINAL_TOOL)


def _offers_none(_name: str) -> bool:
    return False


@dataclass(frozen=True)
class _Failure:
    command: str
    output: str
    workdir: Path | None
    offers: Callable[[str], bool]
    # Where the output reports the failure: from the last Python traceback's
    # exception line on, else all of it.
    error_start: int = 0

    @property
    def error(self) -> str:
        """The output from where it reports the failure on."""
        return self.output[self.error_start :]

    def edit_tool(self) -> str | None:
        return next((name for name in _EDIT_TOOLS if self.offers(name)), None)


# A Python traceback header at the start of a line, and the line that ends the
# traceback: the first one after it that is not indented, the exception.
_TRACEBACK_HEADER = re.compile(r"^Traceback \(most recent call last\):", re.M)
_UNINDENTED_LINE = re.compile(r"^\S", re.M)


def _error_start(output: str) -> int:
    """Where *output* reports the failure: the last Python traceback's exception line.

    A probe printed "[Errno 13] Permission denied" as data and then failed with a
    TypeError; the permission hint answered the data. Without a complete
    traceback, all of the output counts.
    """
    headers = [header.end() for header in _TRACEBACK_HEADER.finditer(output)]
    if not headers:
        return 0
    exception = _UNINDENTED_LINE.search(output, headers[-1])
    return exception.start() if exception is not None else 0


# Exit-code-only hints for codes POSIX shells give a fixed meaning; on Windows
# they arrive through bash, wsl, docker or ssh.
_EXIT_CODE_HINTS: dict[int, str] = {
    126: "Exit code 126: the file was found but is not executable. Make it executable with "
    "`chmod +x`, or run it through its interpreter, such as `bash script.sh`.",
    137: "Exit code 137: the process was killed with SIGKILL, for example by the kernel when "
    "memory ran out. Check `dmesg | tail` on the machine that ran it for an out-of-memory "
    "entry before running it again.",
}
# The `timeout` program exits with 124 when its time limit stops the command.
_TIMEOUT_PROGRAM = re.compile(r"(?:^|[\s;|&(])timeout\s", re.M)
_TIMEOUT_PROGRAM_HINT = (
    "Exit code 124: the `timeout` program in the command stopped it at its time limit. "
    "If the command needs more time, give `timeout` a larger limit."
)


# A title in place of a command: one line of at least three plain words that
# starts with a capitalized word, such as "Check shell files compile and behave".
# Programs are rarely capitalized; a cmdlet's hyphen, a flag or a file's dot ends
# the match.
_PROSE = re.compile(r"[A-Z][a-z]*(?:[ \t]+[^\W_][\w'’-]*){2,}\.?")


def _description_as_command(failure: _Failure, missing: str) -> str | None:
    """The hint for a call whose command holds a description, or None.

    Agents sent the title meant for description as the command; the shell ran
    its first word, and the not-installed hint sent them to install `Check`.
    A command equal to the call's description proves nothing: Models also copy
    a short command into description.
    """
    command = failure.command.strip()
    if not _PROSE.fullmatch(command) or command.split(maxsplit=1)[0] != missing:
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
_BASH_SYNTAX_HINT = (
    "This is bash syntax: in PowerShell write `if (cond) { ... }`, "
    "`foreach ($f in Get-ChildItem *.md) { ... }`, and `Test-Path` for file tests."
)
# PowerShell localizes its messages and their quotes; names appear in any of these.
_QUOTES = "'\"‘’“”„"
# English and German hosts: the bootstrap switches messages to English, but a
# message written before that, or by another PowerShell, keeps the host's language.
_POWERSHELL_NOT_RECOGNIZED = re.compile(
    rf"(?:The term|Der Begriff) [{_QUOTES}]([^{_QUOTES}]+)[{_QUOTES}] "
    r"(?:is not recognized|wird nicht)"
)


def _quoted_in(name: str, output: str) -> bool:
    return re.search(rf"[{_QUOTES}]{re.escape(name)}[{_QUOTES}]", output) is not None


def _hint_powershell_command_not_found(failure: _Failure) -> str | None:
    # pwsh reports "The term 'X' is not recognized as a name of a cmdlet, function,
    # script file, or executable program."
    match = _POWERSHELL_NOT_RECOGNIZED.search(failure.output)
    if not match:
        return None
    missing = match.group(1)
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


# -Recurse after Select-String in the same pipeline step. An attempt ends at the
# next Select-String, which starts its own, so a long command is scanned once.
_SELECT_STRING_RECURSE = re.compile(r"Select-String\b(?:(?!Select-String\b)[^|;\n])*-Recurse", re.I)


def _hint_select_string_recurse(failure: _Failure) -> str | None:
    if not _SELECT_STRING_RECURSE.search(failure.command):
        return None
    if "Select-String" not in failure.output or not _quoted_in("Recurse", failure.output):
        return None
    hint = (
        "Select-String has no -Recurse: pipe files in with "
        "`Get-ChildItem -Recurse -File | Select-String 'text'`"
    )
    if failure.offers(_SEARCH_TOOL):
        return f"{hint}, or search with {_SEARCH_TOOL}."
    return f"{hint}."


_ESCAPED_QUOTE_HINT = (
    'In PowerShell, \\" does not escape a quote. Put the argument in single quotes '
    "(double quotes inside stay literal), or pass longer code through a here-string "
    "piped to the program, such as `@'...'@ | python -`."
)
# Bash's ways to put a single quote into a single-quoted string; PowerShell
# doubles it instead.
_BASH_QUOTE_IDIOMS = ("'\\''", "'\"'\"'")
_BASH_QUOTE_HINT = (
    "PowerShell has no '\\'' or '\"'\"' escape. Inside single quotes, write a single quote "
    "twice, as in 'it''s'."
)
# A PowerShell here-string: @" or @' ends its line, and "@ or '@ starts a line.
_HERE_STRING_OPENER = re.compile(r"@(['\"])[ \t]*\r?\n")
# '' before a word is a quote doubled as in a single-quoted string; an empty
# string literal such as ''.join is followed by a sign instead.
_DOUBLED_QUOTE = re.compile(r"''\w")
# Bash control flow at the start of a statement: then, fi, done, a do that does not
# open PowerShell's do { } loop, or a [ or [[ test.
_BASH_SYNTAX = re.compile(
    r"(?:^|[;&|])[ \t]*+(?:(?:then|fi|done)\b|do\b(?![ \t]*+\{)|\[\[?[ \t])", re.M
)
# PowerShell's parser refuses bash's `program < file`.
_INPUT_REDIRECTION = re.compile(rf"[{_QUOTES}]<[{_QUOTES}] operator is reserved for future use")
# A { } block passed to a native program, as when \" ended a python -c "..." string
# early: "python.exe: ScriptBlock should only be specified as a value of the Command parameter."
_SCRIPT_BLOCK_ARGUMENT = "ScriptBlock should only be specified as a value of the Command parameter"

# Python source sent as the command: after blank and comment lines, its first line
# imports, or defines a function or a class. A PowerShell class opens a { block,
# also after `class Child : Parent`, so a Python class line ends in its colon.
_PYTHON_SOURCE = re.compile(
    r"(?:[ \t]*+(?:#[^\n]*+)?\r?\n)*+[ \t]*+"
    r"(?:import[ \t]++[\w.]++(?:[ \t]++as[ \t]++\w++)?(?:[ \t]*+,[ \t]*+[\w.]++"
    r"(?:[ \t]++as[ \t]++\w++)?)*+[ \t]*+(?:[;#][^\n]*+)?\r?$"
    r"|from[ \t]++[\w.]++[ \t]++import[ \t]"
    r"|def[ \t]++\w++[ \t]*+\("
    r"|class[ \t]++\w++[ \t]*+(?:\([^)\n]*+\))?[ \t]*+:[ \t]*+(?:#[^\n]*+)?\r?$)",
    re.M,
)


def _hint_python_source(failure: _Failure) -> str | None:
    """Name Python code sent as a PowerShell command.

    PowerShell reads it as its own code and fails on whatever line breaks its
    grammar, with a ParserError or a "not recognized" error that names no cause.
    """
    if not _POWERSHELL or not _PYTHON_SOURCE.match(failure.command):
        return None
    return (
        "The command is Python code, which PowerShell cannot run. Pipe it into Python as a "
        "here-string: @' on its own line, the code, then '@ at the start of a line, followed "
        "by `| python -`."
    )


def _here_strings(command: str) -> tuple[list[tuple[str, str]], str]:
    """Return each here-string's quote and text, and the command outside them.

    A here-string ends at the first line that starts with its quote and @. When
    one never ends, no later one with the same quote can end either, so each
    quote is searched to the end of the command at most once.
    """
    found: list[tuple[str, str]] = []
    outside: list[str] = []
    unclosed: set[str] = set()
    copied = position = 0
    while opener := _HERE_STRING_OPENER.search(command, position):
        quote, start = opener[1], opener.end()
        close = -1 if quote in unclosed else command.find(f"\n{quote}@", start)
        if close < 0:
            unclosed.add(quote)
            position = opener.start() + 1
            continue
        end = close - 1 if close > start and command[close - 1] == "\r" else close
        found.append((quote, command[start:end]))
        outside.append(command[copied : opener.start()])
        copied = position = close + 3
    outside.append(command[copied:])
    return found, "".join(outside)


def _hint_powershell_syntax(failure: _Failure) -> str | None:
    """Explain bash syntax and quote escapes that break PowerShell or the code it passes on.

    PowerShell itself reports a ParserError for heredocs and bash control flow.
    Escapes that PowerShell passes on unchanged break the code the called
    program reads, so a Python or Node SyntaxError follows; only the Windows
    shell is PowerShell.
    """
    command, output = failure.command, failure.output
    here_strings, outside = _here_strings(command)
    if "ParserError" in output:
        if re.search(r"<<-?\s*['\"]?[A-Za-z_]\w*['\"]?", command):
            return (
                "PowerShell has no heredoc (<<). Pipe a here-string instead: @' on its own "
                "line, the text, then '@ at the start of a line, followed by `| python -` or "
                "`| Set-Content file`."
            )
        if _INPUT_REDIRECTION.search(output):
            return (
                "PowerShell has no < input redirection. Pipe the file into the program "
                "instead, as in `Get-Content input.txt | python script.py`."
            )
        if _BASH_SYNTAX.search(outside):
            return _BASH_SYNTAX_HINT
        if _has_bash_quote_idiom(outside):
            return _BASH_QUOTE_HINT
        return _ESCAPED_QUOTE_HINT if '\\"' in outside else None
    if _SCRIPT_BLOCK_ARGUMENT in output:
        # \" ended the string early, and a { } block of the code became an argument.
        return _ESCAPED_QUOTE_HINT if '\\"' in outside else None
    if not _POWERSHELL or "SyntaxError" not in output:
        return None
    if any(quote == '"' and '\\"' in text for quote, text in here_strings):
        return (
            'Inside a @"..."@ here-string, \\" stays a backslash and a quote. Write the '
            "quotes there without backslashes, or use @'...'@, which also keeps $ literal."
        )
    if any(quote == "'" and _has_bash_quote_idiom(text) for quote, text in here_strings):
        return (
            "Inside a @'...'@ here-string, '\\'' and '\"'\"' stay as written. Write each quote "
            "once there."
        )
    if any(quote == "'" and _DOUBLED_QUOTE.search(text) for quote, text in here_strings):
        return "Inside a @'...'@ here-string, '' stays two quotes. Write each quote once there."
    if _has_bash_quote_idiom(outside):
        return _BASH_QUOTE_HINT
    return _ESCAPED_QUOTE_HINT if '\\"' in outside else None


def _has_bash_quote_idiom(text: str) -> bool:
    return any(idiom in text for idiom in _BASH_QUOTE_IDIOMS)


# PowerShell takes the typographic quotes for quotes of their kind, as it does ' and ".
_SINGLE_QUOTES = "'‘’‚‛"
_DOUBLE_QUOTES = '"“”„'
# Where something in PowerShell code can start a string, a comment or an escape.
_CODE_MARK = re.compile(rf"[#<@`{_SINGLE_QUOTES}{_DOUBLE_QUOTES}]")
_SINGLE_MARK = re.compile(rf"[{_SINGLE_QUOTES}]")
_DOUBLE_MARK = re.compile(rf"[`{_DOUBLE_QUOTES}]")
_HERE_STRING_START = re.compile(rf"@([{_SINGLE_QUOTES}{_DOUBLE_QUOTES}])[ \t]*\r?\n")
# A comment's # starts a token: after a blank, a separator or a bracket.
_TOKEN_BOUNDARY = frozenset(" \t\r\n;|&(){},")


@dataclass(frozen=True)
class _PowerShellCode:
    """A PowerShell command with the contents of its strings and comments blanked.

    Offsets are the command's own. ``early_end`` is the first string opened with
    ' or " that a typographic quote of the same kind ended: (opening offset,
    closing offset).
    """

    text: str
    early_end: tuple[int, int] | None


def _powershell_code(command: str) -> _PowerShellCode:
    """Blank strings, here-strings and comments of *command*, in one linear pass.

    Inside single quotes, two quotes of that kind in a row are one literal
    quote; inside double quotes, so are two double quotes or a backtick and the
    character after it. PowerShell counts every typographic quote of a kind as
    a quote of that kind (``it’s`` ends ``'it’s'`` early).
    """
    code = list(command)
    early_end: tuple[int, int] | None = None
    end = len(command)

    def blank(start: int, stop: int) -> None:
        for index in range(start, min(stop, end)):
            if code[index] not in "\r\n":
                code[index] = " "

    position = 0
    while mark := _CODE_MARK.search(command, position):
        start = mark.start()
        char = command[start]
        if char == "`":
            position = start + 2
        elif char == "#":
            if start and command[start - 1] not in _TOKEN_BOUNDARY:
                position = start + 1
                continue
            stop = command.find("\n", start)
            stop = end if stop < 0 else stop
            blank(start, stop)
            position = stop
        elif char == "<":
            if not command.startswith("<#", start):
                position = start + 1
                continue
            stop = command.find("#>", start + 2)
            stop = end if stop < 0 else stop + 2
            blank(start, stop)
            position = stop
        elif char == "@":
            opener = _HERE_STRING_START.match(command, start)
            if opener is None:
                position = start + 1
                continue
            kind = _SINGLE_QUOTES if opener[1] in _SINGLE_QUOTES else _DOUBLE_QUOTES
            close = re.compile(rf"\n[{kind}]@").search(command, opener.end())
            stop = end if close is None else close.start()
            blank(opener.end(), stop)
            position = end if close is None else close.end()
        else:
            single = char in _SINGLE_QUOTES
            inner = _SINGLE_MARK if single else _DOUBLE_MARK
            kind = _SINGLE_QUOTES if single else _DOUBLE_QUOTES
            index = start + 1
            stop = end
            while found := inner.search(command, index):
                at = found.start()
                if command[at] == "`" or at + 1 < end and command[at + 1] in kind:
                    index = at + 2
                else:
                    stop = at
                    break
            blank(start + 1, stop)
            if early_end is None and stop < end and char in "'\"" and command[stop] != char:
                early_end = (start, stop)
            position = stop + 1
    return _PowerShellCode("".join(code), early_end)


_STRING_SHOWN_CHARS = 40


def _hint_typographic_quote(command: str) -> str | None:
    """Name a typographic quote that ended a string early.

    Agents quote text copied from web pages: ``--quote 'Köln (lateinisch ‚Colonia‘) ...'``
    was cut at ‚, so the program got two arguments, or PowerShell reported a
    ParserError, and nothing said why. Read from the command alone, whatever the
    exit code: the command did not run as written either way.
    """
    if not _POWERSHELL:
        return None
    early_end = _powershell_code(command).early_end
    if early_end is None:
        return None
    start, stop = early_end
    single = command[start] == "'"
    kind, name = (_SINGLE_QUOTES, "single") if single else (_DOUBLE_QUOTES, "double")
    typographic = "".join(dict.fromkeys(char for char in command if char in kind[1:]))
    listed = " and ".join(typographic) if len(typographic) <= 2 else ", ".join(typographic)
    plural = len(typographic) > 1
    text = command[start + 1 : stop]
    if len(text) > _STRING_SHOWN_CHARS:
        text = text[: _STRING_SHOWN_CHARS - 3] + "..."
    doubled = " and ".join(char * 2 for char in typographic)
    return (
        f"PowerShell reads {listed} as {name} quote{'s' if plural else ''}, so {command[stop]} "
        f"ended the string {command[start]}{text}{command[stop]} early and the command did not "
        f"run as written. Inside {name} quotes, write {'each of them' if plural else 'it'} "
        f"twice ({doubled}), or pass the text in a here-string: @' on its own line, the text, "
        "then '@ at the start of a line."
    )


# A stage that closes the output of the commands before it in its pipeline once
# N lines have passed: Select-Object -First N (any prefix down to -f) without
# -Wait. The bootstrap's head reads its input to the end. The stage's text runs
# to the next pipe or statement.
_FIRST_STAGE = re.compile(
    r"(?<!\|)\|(?!\|)[ \t]*+(?:Select-Object|select)(?=[ \t])(?P<arguments>[^|;\n]*+)",
    re.IGNORECASE,
)
_FIRST_COUNT = re.compile(
    r"(?:^|[ \t])-f(?:i(?:r(?:st?)?)?)?(?:[ \t]*+:[ \t]*+|[ \t]++)(\d+)", re.I
)
_WAIT = re.compile(r"(?:^|[ \t])-w(?:a(?:it?)?)?(?=[ \t:]|$)", re.I)
# Where a pipeline starts: after a statement separator or an opening bracket.
_PIPELINE_START = re.compile(r"[;\n{(]|&&|\|\|")
# The program a pipeline starts with, after PowerShell's call operator.
_PIPELINE_PROGRAM = re.compile(r"[ \t]*+(?:&[ \t]*+)?(\"[^\"]*+\"|'[^']*+'|[^\s|;&(){}]++)")
# Programs that do work before or after printing, as opposed to queries such as
# git log or rg, which lose nothing when they are stopped early.
_WORKING_PROGRAMS = frozenset(
    {
        *("python", "python3", "py", "pythonw", "pytest", "uv", "uvx", "poetry", "pipenv"),
        *("tox", "nox", "node", "npm", "npx", "pnpm", "yarn", "bun", "deno", "tsx"),
        *("ruby", "perl", "php", "dotnet", "cargo", "go", "java", "gradle", "gradlew"),
        *("mvn", "make", "cmake", "msbuild", "pwsh", "powershell"),
    }
)
_SCRIPT_SUFFIXES = (".py", ".ps1", ".js", ".mjs", ".cmd", ".bat")
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


def _working_program(word: str) -> str | None:
    """The program *word* names, as written without its path, when it does work."""
    name = re.split(r"[/\\]", word.strip("'\""))[-1]
    if name.lower().endswith(_SCRIPT_SUFFIXES):
        return name
    stem = name[:-4] if name.lower().endswith(".exe") else name
    return stem if re.sub(r"[\d.]+$", "", stem.lower()) in _WORKING_PROGRAMS else None


def _program_at(command: str, code: str, start: int, end: int | None = None) -> str | None:
    """The working program the pipeline at *start* begins with, lowercase without .exe."""
    first = _PIPELINE_PROGRAM.match(code, start, len(code) if end is None else end)
    if first is None:
        return None
    program = _working_program(command[first.start(1) : first.end(1)])
    return None if program is None else program.lower().removesuffix(".exe")


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
    pipeline may have failed there, and gets no hint.
    """
    if not _POWERSHELL or "|" not in command or not failed_programs:
        return None
    code = _powershell_code(command).text
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
    # The program that starts each pipeline outside the stages' pipelines.
    elsewhere = {_program_at(command, code, start) for start in starts if start not in staged}
    window = _scan_window(output)
    for start, (stage, lines) in staged.items():
        program = _program_at(command, code, start, stage.start())
        if program is None or program in elsewhere:
            continue
        codes = _exit_codes(failed_programs, program)
        if not codes:
            continue
        written = _stage_text(command, code, stage.start() + 1, stage.end())
        counted = f"{lines} line{'' if lines == 1 else 's'}"
        zero = ", although the command exited with code 0" if exit_code == 0 else ""
        closed = _CLOSED_OUTPUT.search(window)
        if closed is not None:
            return (
                f"`{written}` closed {program}'s output after {counted}, and {program} failed "
                f"at its next write ({closed[0].strip()}){zero}: what {program} does after "
                "that, such as writing files, did not happen. To let it finish, run it "
                "without that stage (long output keeps its first and last lines), or add "
                "-Wait to Select-Object."
            )
        if not _MERGED_ERRORS.search(code, start, stage.start()):
            continue
        listed = codes[0] if len(codes) == 1 else f"{', '.join(codes[:-1])} and {codes[-1]}"
        return (
            f"{program} exited with code{'s' if len(codes) > 1 else ''} {listed}{zero}. Its "
            f"output, errors included (2>&1), went through `{written}`, which passes on "
            f"only the first {counted} and then closes that output: the lines that say why "
            f"{program} failed can be missing, or {program} failed writing after the close. "
            "To see why it failed, run it again without that stage (long output keeps its "
            "first and last lines)."
        )
    return None


# A word of the command, between blanks, quotes, parentheses and separators.
_COMMAND_WORD = re.compile(r"[^\s'\"(),;|&]++")
# A program reports a wildcard word it was given; at most this many such words are checked.
_NATIVE_GLOB_WORDS = 20


def _hint_native_glob(failure: _Failure) -> str | None:
    """Name the wildcard PowerShell passed to a program unexpanded.

    Bash expands ``minidb/*.py`` before the program starts; PowerShell expands
    wildcards only for its own cmdlets, so ``python -m py_compile minidb/*.py``
    failed with ``[Errno 22] Invalid argument: 'minidb/*.py'``. A program that
    reports the word as written was given it; a cmdlet reports the resolved path.
    A quoted word was meant for the program, and git matches pathspecs itself.
    """
    command, output = failure.command, failure.output
    if not _POWERSHELL or "ParserError" in output or "did not match any file" in output:
        return None
    words: dict[str, None] = {}
    for match in _COMMAND_WORD.finditer(command):
        word = match[0]
        quoted = match.start() > 0 and command[match.start() - 1] in "'\""
        if (
            "*" in word
            and not quoted
            and not word.startswith("-")
            and any(mark in word for mark in "/\\.")
        ):
            words[word] = None
            if len(words) == _NATIVE_GLOB_WORDS:
                break
    for word in words:
        if re.search(rf"(?:^|[\s'\"`]){re.escape(word)}(?=[\s'\"`:,]|$)", output, re.M):
            return (
                f"PowerShell passes `{word}` to programs as written, without expanding the "
                f"wildcard. Pass the matching files instead: `(Get-ChildItem {word}).FullName`."
            )
    return None


def _hint_dev_null(failure: _Failure) -> str | None:
    # The bootstrap runs redirections to /dev/null as $null and passes it to native
    # programs as NUL; a cmdlet that takes it as a path, such as Out-File /dev/null,
    # reports the missing C:\dev\null.
    if "\\dev\\null" not in failure.output:
        return None
    return (
        "PowerShell has no /dev/null file: discard output with `| Out-Null` instead of "
        "passing /dev/null as a path."
    )


_MISSING_MODULE = re.compile(
    r"(?:ModuleNotFoundError|ImportError): No module named '?([\w.]+)'?"
    r"(; '[\w.]+' is not a package)?"
)
# A Python script run by its path, such as `python tests/test_x.py` or `py -u tools\run.py`:
# the interpreter as written, its options (after a closing quote of the
# interpreter's path; -X and -W with their value, as in `-X utf8`), and the script.
_PYTHON_SCRIPT = re.compile(
    r"(?:^|[\s;&|('\"])(?P<interpreter>(?:[^\s;&|()'\"]*[/\\])?(?:python[\d.]*|py)(?:\.exe)?)"
    r"(?P<options>['\"]?(?:\s+-(?:[XW][ \t]*+[^\s;&|<>'\"-][^\s;&|<>'\"]*+|[A-Za-z]\w*))*)"
    r"\s+['\"]?(?P<script>[^\s;&|<>'\"]+\.py)\b",
    re.I | re.M,
)


# Python run with -m, which puts the working directory on the import path.
_PYTHON_MODULE_RUN = re.compile(
    r"(?:^|[\s;&|('\"])(?:[^\s;&|()'\"]*[/\\])?(?:python[\d.]*|py)(?:\.exe)?['\"]?\s+"
    r"(?:-[A-Za-z]\w*\s+)*?-m(?=\s)",
    re.I | re.M,
)
# A traceback frame; <string> and <stdin> are code passed with -c or on stdin.
_TRACEBACK_FILE = re.compile(r'^[ \t]*File "([^"\n]*)"', re.M)


def _hint_module_not_found(failure: _Failure) -> str | None:
    match = _MISSING_MODULE.search(failure.output, failure.error_start)
    if not match:
        return None
    name = match.group(1)
    if "." in name:
        parent, _, leaf = name.rpartition(".")
        if match.group(2):
            return (
                f"Python cannot import '{name}': '{parent}' is a single module, not a "
                f"package, so it has no submodules. Import '{leaf}' from the module "
                "that defines it."
            )
        return _missing_submodule(name, failure.workdir)
    workdir = failure.workdir
    if workdir is None or not _in_workdir(name, workdir):
        return (
            f"Python cannot import '{name}': this interpreter finds no package or module "
            "of that name. Check the spelling; if the project has a virtual environment, "
            "run its interpreter, otherwise install the package."
        )
    found = _script_in_subdirectory(failure.command, failure.output, workdir)
    if found is None and _imported_elsewhere(failure, match.start()):
        return (
            f"'{name}' is in the working directory, but the Python process that could not "
            "import it runs with another working directory or import path, such as a process "
            "the command started. Start that process in the working directory, or add the "
            "working directory to its PYTHONPATH."
        )
    if found is None:
        return (
            f"'{name}' is in the working directory, but the working directory is not on "
            "Python's import path for this command. Run Python from the working directory "
            "with -m, such as `python -m pytest`, or add the working directory to PYTHONPATH."
        )
    script, run = found
    cause = (
        f"Python cannot import '{name}' because running `{script.as_posix()}` by path puts "
        f"its directory `{script.parent.as_posix()}` on the import path instead of the "
        f"working directory, where '{name}' is."
    )
    if all(part.isidentifier() for part in script.with_suffix("").parts):
        return (
            f"{cause} Run it as a module from the working directory instead: "
            f"{_run_as_module(failure.command, script, run)}."
        )
    # A directory such as .scratch is no module name, so -m cannot run the script.
    return (
        f"{cause} Put the working directory on the import path for this run: "
        f"{_run_with_working_directory(failure.command, run)}."
    )


def _missing_submodule(name: str, workdir: Path | None) -> str:
    """The hint for a package that has no module of the imported name.

    "Create the module" fired when tests imported a teammate's module that had
    not landed yet, inviting Agents to write someone else's file. When the
    module is in the working directory, Python imported another package of the
    same name: a ``tests`` folder without ``__init__.py`` lost to an installed
    ``tests`` package.
    """
    parent, _, leaf = name.rpartition(".")
    parts = name.split(".")
    top = parts[0]
    package = Path(*parts[:-1])
    if workdir is not None:
        module = next(
            (
                path
                for path in (package / f"{leaf}.py", package / leaf)
                if (workdir / path).is_file() or (path.name == leaf and (workdir / path).is_dir())
            ),
            None,
        )
        if module is not None:
            shown = module.as_posix()
            if not (workdir / top / "__init__.py").is_file():
                return (
                    f"{shown} exists in the working directory, but Python imported another "
                    f"package named '{top}': the working directory's {top}/ has no __init__.py, "
                    f"so a '{top}' package elsewhere on the import path shadows it. Add an empty "
                    f"{top}/__init__.py."
                )
            return (
                f"{shown} exists in the working directory, but Python imported another "
                f"package named '{top}', which comes earlier on its import path and shadows "
                "the working directory's. Start Python in the working directory with -m, which "
                "puts the working directory first."
            )
        if (workdir / package).is_dir():
            return (
                f"Python imported the package '{parent}', but it has no module '{leaf}', and "
                f"the working directory has no {package.as_posix()}/{leaf}.py. Check the name "
                f"against the files in {package.as_posix()}/; if it is right, that module is "
                "not written yet."
            )
    return (
        f"Python imported the package '{parent}', but it has no module '{leaf}'. Check the "
        f"name against the files of '{parent}'."
    )


def _imported_elsewhere(failure: _Failure, error_at: int) -> bool:
    """Whether the failed import ran in a Python process outside the working directory.

    Python run with -m, or code run with -c or from stdin, has the directory it
    starts in on its import path. When such an import of a module in the working
    directory fails, the process ran elsewhere: often a process the command
    started, such as a test's subprocess. "Run it with -m" was then wrong advice.
    """
    if _PYTHON_MODULE_RUN.search(failure.command):
        return True
    frames = _TRACEBACK_FILE.findall(failure.output, 0, error_at)
    return bool(frames) and frames[-1] in ("<string>", "<stdin>")


def _in_workdir(module: str, workdir: Path) -> bool:
    return (workdir / module).is_dir() or (workdir / f"{module}.py").is_file()


def _script_in_subdirectory(
    command: str, output: str, workdir: Path
) -> tuple[Path, re.Match[str]] | None:
    """The script in a subdirectory of ``workdir`` that the command ran by path,
    with the match of the Python call that ran it.

    With several scripts, the first one a traceback names wins; None when no
    traceback names one.
    """
    scripts: dict[Path, re.Match[str]] = {}
    for match in _PYTHON_SCRIPT.finditer(command):
        path = Path(match["script"].replace("\\", "/"))
        if path.is_absolute():
            try:
                path = path.relative_to(workdir)
            except ValueError:
                continue
        if len(path.parts) > 1 and ".." not in path.parts and path not in scripts:
            scripts[path] = match
    found = list(scripts.items())
    if len(found) > 1:
        traceback = output.replace("\\", "/")
        found = [item for item in found if f'/{item[0].as_posix()}"' in traceback]
    return found[0] if found else None


# Longer arguments are named, not repeated, in a suggested call.
_ARGUMENTS_SHOWN_CHARS = 400
# The arguments after a script's path, up to a redirection or the end of its
# statement: quoted or plain words.
_SCRIPT_ARGUMENTS = re.compile(
    r"""(?:[ \t]++(?![0-9*]*+>)(?:'[^'\n]*+'|"[^"\n]*+"|[^\s;&|<>'"])++)*+"""
)


def _run_as_module(command: str, script: Path, run: re.Match[str]) -> str:
    """The Python call that ran *script* by path, rewritten to run it with -m.

    The interpreter as written, its options and every argument after the script
    stay unchanged: one suggestion dropped `--impl minidb.sqlite_float.format_real`,
    which chose the implementation the script tested.
    """
    start, _script_end, rest = _script_call(command, run)
    call = f"{command[start : run.end('options')]} -m {'.'.join(script.with_suffix('').parts)}"
    return _shown_call(call, rest)


def _run_with_working_directory(command: str, run: re.Match[str]) -> str:
    """The Python call that ran a script by path, with the working directory on PYTHONPATH.

    For a script in a directory that is no module name, such as ``.scratch``,
    where -m cannot run it. The variable is set in the shell's own syntax.
    """
    start, script_end, rest = _script_call(command, run)
    setting = "$env:PYTHONPATH = (Get-Location).Path; " if _POWERSHELL else "PYTHONPATH=. "
    return _shown_call(f"{setting}{command[start:script_end]}", rest)


def _script_call(command: str, run: re.Match[str]) -> tuple[int, int, str]:
    """Where the Python call *run* starts and its script ends, and the arguments after it."""
    start = run.start("interpreter")
    if start > 0 and command[start - 1] in "'\"":
        # A quoted interpreter path, run with PowerShell's call operator.
        start -= 1
        before = command[:start].rstrip(" \t")
        if before.endswith("&") and not before.endswith("&&"):
            start = len(before) - 1
    end = run.end("script")
    if end < len(command) and command[end] in "'\"":
        end += 1
    arguments = _SCRIPT_ARGUMENTS.match(command, end)
    return start, end, arguments[0].rstrip() if arguments else ""


def _shown_call(call: str, rest: str) -> str:
    if len(rest) > _ARGUMENTS_SHOWN_CHARS:
        return f"`{call}` followed by the same arguments"
    return f"`{call}{rest}`"


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
        and not (_POWERSHELL and _ACCESS_DENIED.search(output))
    ):
        return None
    if _POWERSHELL:
        return (
            "Permission denied: another program holds the file open, or your user cannot "
            "write to the path. Close that program or use another location; if the task needs "
            "elevated rights, ask the user to run the command."
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
) -> str | None:
    """Return one short next action for a failed command, or None.

    *output* is the command's output as the Agent reads it; only its first and
    last ``_SCAN_CHARS`` characters are examined. *workdir* is the directory the
    command started in, when known. *offers* tells whether the Agent is offered
    the Tool of a registry name; a hint names only offered Tools.
    *failed_programs* are the command's failed child programs
    (``<name> exited with code <code>``) and *output_lines* the number of lines
    it printed (the lines of *output* when omitted).

    A command that did not run as written gets a hint whatever its exit code,
    since a later statement's success can hide its failure: a typographic quote
    ended a string early (first, since every error follows from it), or a
    program failed in a pipeline with a Select-Object -First stage (last).
    Otherwise exit code 0 or None stays unannotated.
    """
    command = command or ""
    output = output or ""
    try:
        quote = _hint_typographic_quote(command)
    except Exception:
        quote = None
    if quote:
        return quote
    hint = _failure_hint(command, exit_code, output, workdir, offers) if exit_code else None
    if hint:
        return hint
    lines = len(output.splitlines()) if output_lines is None else output_lines
    try:
        return _hint_first_stage(command, exit_code, output, lines, failed_programs)
    except Exception:
        return None


def _scan_window(output: str) -> str:
    """The first and the last ``_SCAN_CHARS`` characters of *output*."""
    if len(output) > 2 * _SCAN_CHARS:
        return f"{output[:_SCAN_CHARS]}\n{output[-_SCAN_CHARS:]}"
    return output


def _failure_hint(
    command: str,
    exit_code: int,
    output: str,
    workdir: Path | None,
    offers: Callable[[str], bool],
) -> str | None:
    window = _scan_window(output)
    if window:
        failure = _Failure(command, window, workdir, offers, _error_start(window))
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


__all__ = ["HINT_TOOL_NAMES", "annotate_failure"]
