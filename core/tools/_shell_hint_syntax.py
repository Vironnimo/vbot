"""Shell syntax, quoting and interpolation failure diagnostics."""

from __future__ import annotations

import re
from collections.abc import Sequence
from itertools import islice

from . import _shell_hint_common as _common
from ._shell_hint_code import (
    _COMMAND_WORD,
    _DOUBLE_QUOTES,
    _POWERSHELL_NOT_RECOGNIZED,
    _QUOTES,
    _SINGLE_QUOTES,
    _powershell_code,
    _program_strings,
)
from ._shell_hint_common import (
    _BASH_SYNTAX_HINT,
    _DEV_NULL,
    _DEV_NULL_HINT,
    _DEV_NULL_MISSING,
    _DEV_NULL_RESOLVED,
    _ESCAPED_QUOTE_FIX,
    _Failure,
    _scan_window,
)

_ESCAPED_QUOTE_HINT = f'In PowerShell, \\" does not escape a quote. {_ESCAPED_QUOTE_FIX}'
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
# PowerShell's parser refuses a here-string written on one line, such as
# @'import re ... '@, or one whose closing '@ does not start its line.
_HERE_STRING_LAYOUT = re.compile(
    r"No characters are allowed after a here-string header"
    r"|White space is not allowed before the string terminator"
    r"|The string is missing the terminator: [\"']@"
)
# PowerShell's parser refuses bash's `program < file`.
_INPUT_REDIRECTION = re.compile(rf"[{_QUOTES}]<[{_QUOTES}] operator is reserved for future use")
# A { } block passed to a native program, as when \" ended a python -c "..." string
# early: "python.exe: ScriptBlock should only be specified as a value of the Command parameter."
_SCRIPT_BLOCK_ARGUMENT = "ScriptBlock should only be specified as a value of the Command parameter"
# An argument a cmdlet has no parameter for, as when \" ended a -Pattern "..." early,
# on English and German hosts: "Select-String: A positional parameter cannot be found
# that accepts argument 'b\|c'."
_EXTRA_ARGUMENT = re.compile(
    rf"A positional parameter cannot be found that accepts argument [{_QUOTES}](.+)[{_QUOTES}]\."
    rf"|Es wurde kein Positionsparameter gefunden, der das Argument [{_QUOTES}](.+)[{_QUOTES}] "
    r"akzeptiert"
)
# A bare word, as PowerShell reads one in an argument: up to a blank, a quote or
# a separator.
_BARE_WORD = re.compile(rf"[^\s{_QUOTES};|&(){{}},]+")

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
    if not _common._POWERSHELL or not _PYTHON_SOURCE.match(failure.command):
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
        if _HERE_STRING_LAYOUT.search(output):
            return (
                "A here-string's @' must end its line, and its '@ must start a line, with "
                "nothing before it: @' on its own line, the text, then '@ at the start of a line."
            )
        if _INPUT_REDIRECTION.search(output):
            return (
                "PowerShell has no < input redirection. Pipe the file into the program "
                "instead, as in `Get-Content input.txt | python script.py`."
            )
        if re.search(rf"[{_QUOTES}]in[{_QUOTES}]", output):
            loop = _for_in_hint(command)
            if loop is not None:
                return loop
        if _BASH_SYNTAX.search(outside):
            return _BASH_SYNTAX_HINT
        if _has_bash_quote_idiom(outside):
            return _BASH_QUOTE_HINT
        return _ESCAPED_QUOTE_HINT if '\\"' in outside else None
    if _SCRIPT_BLOCK_ARGUMENT in output:
        # \" ended the string early, and a { } block of the code became an argument.
        return _ESCAPED_QUOTE_HINT if '\\"' in outside else None
    if not _common._POWERSHELL or "SyntaxError" not in output:
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


# A for loop over values, as other languages write it: `for ($z in 12,31) { ... }`.
_FOR_IN = re.compile(r"(?:^|[;{}])[ \t]*+for[ \t]*+(\([ \t]*+\$\w++[ \t]++in[ \t])", re.I | re.M)
_LOOP_SHOWN_CHARS = 60


def _for_in_hint(command: str) -> str | None:
    """Name foreach for a for loop over values, and how to pipe the loop.

    PowerShell's for takes (init; condition; step) and reports only "Unexpected
    token 'in'". A foreach statement cannot be piped, so a loop followed by
    ``2>&1 | Select-Object`` needs $( ) around it.
    """
    code = _powershell_code(command).text
    loop = _FOR_IN.search(code)
    if loop is None:
        return None
    header_end = _closing_bracket(code, loop.start(1))
    if header_end is None:
        return None
    header = " ".join(command[loop.start(1) : header_end + 1].split())
    if len(header) > _LOOP_SHOWN_CHARS:
        header = "($item in $list)"
    hint = (
        "PowerShell's `for` takes `(init; condition; step)`: loop over values with "
        f"`foreach {header} {{ ... }}`."
    )
    block = re.compile(r"[ \t]*+\{").match(code, header_end + 1)
    block_end = None if block is None else _closing_bracket(code, block.end() - 1)
    if block_end is None:
        return hint
    after = code[block_end + 1 :].split("\n", 1)[0]
    rest = command[block_end + 1 : block_end + 1 + len(after)].split(";", 1)[0].strip()
    if not re.match(r"(?:\d|\*)?>|\|", rest):
        return hint
    if len(rest) > _LOOP_SHOWN_CHARS:
        rest = "| ..."
    return (
        f"{hint} A foreach statement cannot be piped or redirected: wrap it in $( ), as in "
        f"`$(foreach {header} {{ ... }}) {rest}`."
    )


def _closing_bracket(code: str, opening: int) -> int | None:
    """Where the bracket at *opening* closes in blanked *code*, or None."""
    pairs = {"(": ")", "{": "}", "[": "]"}
    depth = 0
    open_char, close_char = code[opening], pairs[code[opening]]
    for index in range(opening, len(code)):
        if code[index] == open_char:
            depth += 1
        elif code[index] == close_char:
            depth -= 1
            if not depth:
                return index
    return None


_STRING_SHOWN_CHARS = 40


def _hint_typographic_quote(command: str) -> str | None:
    """Name a typographic quote that ended a string early.

    Agents quote text copied from web pages: ``--quote 'Köln (lateinisch ‚Colonia‘) ...'``
    was cut at ‚, so the program got two arguments, or PowerShell reported a
    ParserError, and nothing said why. Read from the command alone, whatever the
    exit code: the command did not run as written either way.
    """
    if not _common._POWERSHELL:
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


def _hint_escaped_quote(
    command: str, exit_code: int | None, output: str, failed_programs: Sequence[str]
) -> str | None:
    """Name a \\" that ended a double-quoted argument early, whatever the exit code.

    Agents escape quotes in ``python -c "..."`` as bash does. PowerShell ends the
    string at the \\", and the rest of the code becomes more arguments, script
    blocks or commands of their own: a quoted title's ``(English)`` ran as a
    command, and the not-installed hint answered it; a SyntaxError and a
    ScriptBlock error went unexplained when a later statement exited with 0; and
    the rest of a Select-String -Pattern became an argument the cmdlet refused.
    Only an error the early end explains counts.
    """
    if not _common._POWERSHELL or '\\"' not in command:
        return None
    code = _powershell_code(command)
    if code.escaped_end is None:
        return None
    window = _scan_window(output)
    if "ParserError" in window and _INPUT_REDIRECTION.search(window):
        # The leaked Python/regex text, rather than a separate input
        # redirection, contains < before the next quote or statement boundary.
        exposed = re.split(r'[";\r\n]', code.text[code.escaped_end + 1 :], maxsplit=1)[0]
        if "<" in exposed:
            return _ESCAPED_QUOTE_HINT
    if _SCRIPT_BLOCK_ARGUMENT in window:
        return _ESCAPED_QUOTE_HINT
    missing = _POWERSHELL_NOT_RECOGNIZED.search(window)
    if missing is not None and re.search(
        rf"(?<![\w$.-]){re.escape(missing[1])}(?![\w-])", code.text[code.escaped_end :]
    ):
        return (
            f'In PowerShell, \\" does not escape a quote: it ended the string, so `{missing[1]}` '
            f"from inside it ran as a command. {_ESCAPED_QUOTE_FIX}"
        )
    extra = _EXTRA_ARGUMENT.search(window)
    if extra is not None:
        # The argument the cmdlet refused starts with the word after the \".
        argument = extra[1] or extra[2]
        word = _BARE_WORD.match(command, code.escaped_end + 1)
        if word is not None and argument.startswith(word[0]):
            if len(argument) > _STRING_SHOWN_CHARS:
                argument = argument[: _STRING_SHOWN_CHARS - 3] + "..."
            return (
                'In PowerShell, \\" does not escape a quote: it ended the string, so the rest '
                f"became the extra argument `{argument}`. {_ESCAPED_QUOTE_FIX}"
            )
    if "SyntaxError" in window and (exit_code or failed_programs):
        return _ESCAPED_QUOTE_HINT
    return None


_PYTHON_SOURCE_FLAGS = re.compile(r"[ \t]+(?:(?:-[BEOIPqsuUv]+|-3(?:\.\d+)?)[ \t]+)*-c[ \t]+")
_EXPANDED_VARIABLE = re.compile(r"`[\s\S]|\$(?:\{[\w:]+\}|[\w]+(?::[\w]+)?)")


def _shown_variables(names: Sequence[str]) -> str:
    """The first three of *names* in backticks, each bounded to 40 characters."""
    shown = ", ".join(f"`{name}`" if len(name) <= 40 else f"`{name[:37]}...`" for name in names[:3])
    return f"{shown} and other variables" if len(names) > 3 else shown


def _hint_python_interpolation(command: str, exit_code: int | None) -> str | None:
    """Name variable expansion in a direct double-quoted Python source argument.

    This is a syntax observation, not proof that a branch ran, a variable was
    empty, or interpolation was unintended. Never inspect or rewrite Python
    code; even its single-quoted strings are inside PowerShell's outer quotes.
    """
    if not _common._POWERSHELL or exit_code != 0 or "$" not in command:
        return None
    for program, flags, start, stop in _program_strings(command):
        if re.fullmatch(r"(?:pythonw?|py)(?:[\d.]+)?", program) is None:
            continue
        if not _PYTHON_SOURCE_FLAGS.fullmatch(flags):
            continue
        # Scoped variables explicitly name PowerShell state; a whole $code
        # argument deliberately passes that variable's Python source.
        names = list(
            islice(
                dict.fromkeys(
                    match[0]
                    for match in _EXPANDED_VARIABLE.finditer(command, start + 1, stop)
                    if match[0].startswith("$") and ":" not in match[0]
                ),
                4,
            )
        )
        if not names:
            continue
        if command[start + 1 : stop].strip() in names:
            continue
        return (
            f"PowerShell expands {_shown_variables(names)} inside the double-quoted Python "
            "source before Python runs. If this was meant as literal text, use a single-quoted "
            "here-string. Check any files this command changed before repeating it."
        )
    return None


# The flag before a nested PowerShell's code: -Command or a prefix of it, such as -c.
_COMMAND_FLAG = re.compile(r"(?:^|[ \t])-c(?:o(?:m(?:m(?:a(?:n(?:d)?)?)?)?)?)?[ \t]+\Z", re.I)
# An assignment to the variable before it: = or an operator such as +=.
_ASSIGNMENT = re.compile(r"[ \t]*+[-+*/%]?=(?!=)")
# The variables a pipeline sets for the script block that handles each object.
_PIPELINE_VARIABLES = frozenset({"$_", "$psitem"})
_NESTED_SHELL = re.compile(r"pwsh|powershell", re.I)


def _hint_nested_powershell(command: str) -> str | None:
    """Name the variables this shell expanded in a nested PowerShell's own code.

    ``pwsh -Command "$r = ...; $_.Name"`` passes the code in double quotes, so
    this PowerShell replaces $r and $_ before the inner one starts, which then
    fails on ``= ...`` or ``.Name`` with an error that names no cause, or prints
    nothing. Read from the command alone, whatever the exit code: the code did
    not run as written either way. Only variables the inner code needs for
    itself count, the ones it assigns and $_ or $PSItem after one of its { };
    any other may be this shell's on purpose.
    """
    if not _common._POWERSHELL or "$" not in command or not _NESTED_SHELL.search(command):
        return None
    for program, flags, start, stop in _program_strings(command):
        if program not in ("pwsh", "powershell") or "|" in flags:
            continue
        if not _COMMAND_FLAG.search(flags):
            continue
        block = command.find("{", start + 1, stop)
        names = list(
            islice(
                dict.fromkeys(
                    match[0]
                    for match in _EXPANDED_VARIABLE.finditer(command, start + 1, stop)
                    if match[0].startswith("$")
                    and (
                        _ASSIGNMENT.match(command, match.end(), stop) is not None
                        or (match[0].lower() in _PIPELINE_VARIABLES and 0 <= block < match.start())
                    )
                ),
                4,
            )
        )
        if names:
            return (
                f"PowerShell expands {_shown_variables(names)} inside the double-quoted -Command "
                "code before the inner PowerShell runs, so that code did not run as written. Run "
                "the code directly in this shell, or escape each $ meant for the inner PowerShell "
                "with a backtick."
            )
    return None


# A program reports a wildcard word it was given; at most this many such words are checked.
_NATIVE_GLOB_WORDS = 20


def _hint_native_glob(failure: _Failure) -> str | None:
    """Name the wildcard PowerShell passed to a program unexpanded.

    Bash expands ``minidb/*.py`` before the program starts; PowerShell expands
    wildcards only for its own cmdlets, so ``python -m py_compile minidb/*.py``
    failed with ``[Errno 22] Invalid argument: 'minidb/*.py'``. A program that
    reports the word as written was given it; a cmdlet reports the resolved path.
    Quoted text was meant for the program as written, such as the regular
    expression ``"Melting point : .*"``, and git matches pathspecs itself.
    """
    command, output = failure.command, failure.output
    if not _common._POWERSHELL or "ParserError" in output or "did not match any file" in output:
        return None
    words: dict[str, None] = {}
    # Strings, here-strings and comments are blanked: no word comes from inside them.
    for match in _COMMAND_WORD.finditer(_powershell_code(command).text):
        word = match[0]
        if "*" in word and not word.startswith("-") and any(mark in word for mark in "/\\."):
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
    if not _common._POWERSHELL or not _DEV_NULL.search(failure.command):
        return None
    output = failure.output
    if _DEV_NULL_RESOLVED.search(output) or _DEV_NULL_MISSING.search(output):
        return _DEV_NULL_HINT
    return None
