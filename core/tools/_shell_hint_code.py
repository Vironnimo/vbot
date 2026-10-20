"""Linear PowerShell lexical analysis shared by shell diagnostics."""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterator
from dataclasses import dataclass

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
# What follows a string that ends an argument; anything else continues it, as a
# word, a string or a block does after a \" written as an escape.
_ARGUMENT_END = frozenset(" \t\r\n;|&)},")


@dataclass(frozen=True)
class _PowerShellCode:
    """A PowerShell command with the contents of its strings and comments blanked.

    Offsets are the command's own. ``early_end`` is the first string opened with
    ' or " that a typographic quote of the same kind ended: (opening offset,
    closing offset). ``escaped_end`` is the offset of the first " that ended a
    double-quoted string after a backslash, written as an escape: a word, a
    string or a block follows it directly. ``flat`` is ``text`` with the line
    breaks inside strings and here-strings blanked too, so every line break left
    in it ends a statement or continues one in code. ``double_strings`` holds
    the opening and closing offsets of ordinary double-quoted strings.
    """

    text: str
    early_end: tuple[int, int] | None
    escaped_end: int | None = None
    flat: str = ""
    double_strings: tuple[tuple[int, int], ...] = ()


def _powershell_code(command: str) -> _PowerShellCode:
    """Blank strings, here-strings and comments of *command*, in one linear pass.

    Inside single quotes, two quotes of that kind in a row are one literal
    quote; inside double quotes, so are two double quotes or a backtick and the
    character after it. PowerShell counts every typographic quote of a kind as
    a quote of that kind (``it’s`` ends ``'it’s'`` early).
    """
    code = list(command)
    flat = list(command)
    early_end: tuple[int, int] | None = None
    escaped_end: int | None = None
    end = len(command)
    double_strings: list[tuple[int, int]] = []

    def blank(start: int, stop: int) -> None:
        for index in range(start, min(stop, end)):
            flat[index] = " "
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
            if not single and stop < end:
                double_strings.append((start, stop))
            if early_end is None and stop < end and char in "'\"" and command[stop] != char:
                early_end = (start, stop)
            if (
                escaped_end is None
                and char == '"'
                and stop + 1 < end
                and command[stop] == '"'
                and command[stop - 1] == "\\"
                and command[stop + 1] not in _ARGUMENT_END
            ):
                escaped_end = stop
            position = stop + 1
    return _PowerShellCode(
        "".join(code), early_end, escaped_end, "".join(flat), tuple(double_strings)
    )


# A stage that closes the output of the commands before it in its pipeline once
# N lines have passed: Select-Object -First N (any prefix down to -f) without
# -Wait. The stage's text runs to the next pipe or statement.
_FIRST_STAGE = re.compile(
    r"(?<!\|)\|(?!\|)[ \t]*+(?:Select-Object|select)(?=[ \t])(?P<arguments>[^|;\n]*+)",
    re.IGNORECASE,
)
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


_SUBEXPRESSION = re.compile(r"`[\s\S]|\$\(")


def _program_strings(command: str) -> Iterator[tuple[str, str, int, int]]:
    """Each double-quoted string a working program gets as a whole argument.

    Yields the program (lowercase, without .exe) that starts the string's
    pipeline, the text between the program and the string with strings blanked,
    and the string's opening and closing offsets. This bounded recognizer is
    not PowerShell's parser: a subexpression can contain its own quotes, and a
    nested block comment can contain apparent code after the inner #>, so such
    commands yield nothing and are left to the shell itself.
    """
    if any(match[0] == "$(" for match in _SUBEXPRESSION.finditer(command)):
        return
    if command.count("<#") > 1:
        return
    code = _powershell_code(command)
    starts = [0, *(match.end() for match in _PIPELINE_START.finditer(code.flat))]
    for start, stop in code.double_strings:
        if command[start] != '"' or command[stop] != '"':
            continue
        if stop + 1 < len(command) and command[stop + 1] not in _ARGUMENT_END:
            continue
        pipeline = starts[bisect.bisect_right(starts, start) - 1]
        first = _PIPELINE_PROGRAM.match(code.flat, pipeline, start)
        if first is None:
            continue
        program = _program_at(command, code.flat, pipeline, start)
        if program is not None:
            yield program, code.flat[first.end() : start], start, stop


def _top_level_statements(flat: str) -> list[tuple[int, int]]:
    """The (start, end) of each statement outside brackets in *flat* code."""
    statements: list[tuple[int, int]] = []
    depth = 0
    start = 0
    for index, char in enumerate(flat):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char in ";\n" and not depth:
            if flat[start:index].strip():
                statements.append((start, index))
            start = index + 1
    if flat[start:].strip():
        statements.append((start, len(flat)))
    return statements


def _top_level_split(flat: str, start: int, end: int, separator: re.Pattern[str]) -> list[int]:
    """Where *separator* occurs between *start* and *end*, outside brackets."""
    found: list[int] = []
    depth = 0
    index = start
    while index < end:
        char = flat[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif not depth and (match := separator.match(flat, index, end)):
            found.append(index)
            index = match.end()
            continue
        index += 1
    return found


def _calls(command: str, name: str) -> bool:
    """Whether *command* names *name* outside its strings and comments, or calls it with &."""
    if any(word[0] == name for word in _COMMAND_WORD.finditer(_powershell_code(command).text)):
        return True
    return re.search(rf"&[ \t]*[{_QUOTES}]{re.escape(name)}[{_QUOTES}]", command) is not None


# A word of the command, between blanks, quotes, parentheses and separators.
_COMMAND_WORD = re.compile(r"[^\s'\"(),;|&]++")


_LATER_STAGE = re.compile(r"(?<!\|)\|(?!\|)")
_SKIPPED_SHOWN_CHARS = 60


def _skipped_before(command: str, name: str) -> list[str]:
    """What PowerShell did not run because *name*, a later stage, was not found.

    PowerShell resolves every command of a pipeline before it starts one, so a
    stage it cannot find skips the stages before it too: ``node t.js | tail -5``
    runs no node. Each pipeline's part before that stage, as written within 60
    characters, for at most three pipelines; a part with an open bracket is left
    out, since its start is unclear.
    """
    flat = _powershell_code(command).flat
    starts = [0, *(match.end() for match in _PIPELINE_START.finditer(flat))]
    found: list[str] = []
    for pipe in _LATER_STAGE.finditer(flat):
        stage = _PIPELINE_PROGRAM.match(flat, pipe.end())
        if stage is None or command[stage.start(1) : stage.end(1)].strip("'\"") != name:
            continue
        start = starts[bisect.bisect_right(starts, pipe.start()) - 1]
        part = flat[start : pipe.start()]
        if not part.strip() or any(part.count(o) != part.count(c) for o, c in ("()", "{}", "[]")):
            continue
        text = " ".join(command[start : pipe.start()].split())
        if len(text) > _SKIPPED_SHOWN_CHARS:
            text = text[: _SKIPPED_SHOWN_CHARS - 3] + "..."
        if text not in found:
            found.append(text)
        if len(found) == 3:
            break
    return found
