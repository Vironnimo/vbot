"""PowerShell command text for ``pwsh -Command``: UTF-8 output and a faithful exit status.

PowerShell encodes redirected output in the console output code page, usually a
legacy OEM page that cannot represent most non-ASCII text, while Bash decodes
every process pipe as UTF-8. Setting the console output encoding at the start of
the command makes cmdlet output, captured native output, and native console
programs started by the command (``cmd``, ``dir``, ...) use UTF-8 as well.

``pwsh -Command`` exits with 1 whenever the last statement failed, so a native
program's own exit code is lost. A closing statement exits with that code
instead, as ``bash -c`` does, and otherwise with 0 or 1 by the last statement's
success.

``using`` statements and a script ``param`` block must precede every other
statement, and named script blocks must contain every statement, so the setup
statement is inserted after those constructs, and a script of named blocks
keeps PowerShell's own exit status. The small scanner below only recognizes
this leading script structure; everything after it stays verbatim.
"""

from __future__ import annotations

UTF8_OUTPUT_STATEMENT = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)"
EXIT_STATUS_STATEMENT = "if ($?) { exit 0 }; if ($LASTEXITCODE) { exit $LASTEXITCODE }; exit 1"

_SINGLE_QUOTES = "'‘’‚‛"
_DOUBLE_QUOTES = '"“”„'
_OPENERS = {"(": ")", "{": "}", "[": "]"}
_NAMED_BLOCKS = ("dynamicparam", "begin", "process", "end", "clean")


def powershell_command(command: str) -> str:
    """Return ``command`` with UTF-8 output and, where PowerShell allows it, its exit status."""
    position, named_blocks = _body_start(command)
    wrapped = _with_setup(command, position)
    if named_blocks:
        return wrapped
    return f"{wrapped}\n{EXIT_STATUS_STATEMENT}"


def with_utf8_output(command: str) -> str:
    """Return ``command`` with the UTF-8 output statement where PowerShell allows it."""
    return _with_setup(command, _body_start(command)[0])


def _with_setup(command: str, position: int) -> str:
    prefix = command[:position]
    if prefix and not prefix.endswith(("\n", "\r", "{")):
        prefix += "\n"
    return f"{prefix}{UTF8_OUTPUT_STATEMENT}\n{command[position:]}"


def _body_start(command: str) -> tuple[int, bool]:
    """Return the first position where an ordinary statement may appear.

    The flag is true for a script of named blocks, which admits no statement
    after its blocks.
    """
    body = 0
    position = _skip_trivia(command, 0)
    while _keyword_at(command, position, "using"):
        end = _construct_end(command, position, statement=True)
        if end is None:
            return body, False
        body = end
        position = _skip_trivia(command, end)

    probe = position
    while probe < len(command) and command[probe] == "[":
        attribute_end = _construct_end(command, probe)
        if attribute_end is None:
            return body, False
        probe = _skip_trivia(command, attribute_end)
    if _keyword_at(command, probe, "param"):
        parenthesis = _skip_trivia(command, probe + len("param"))
        if parenthesis < len(command) and command[parenthesis] == "(":
            parameters_end = _construct_end(command, parenthesis)
            if parameters_end is None:
                return body, False
            body = parameters_end
            position = _skip_trivia(command, parameters_end)

    for keyword in _NAMED_BLOCKS:
        if _keyword_at(command, position, keyword):
            brace = _skip_trivia(command, position + len(keyword))
            if brace < len(command) and command[brace] == "{":
                return brace + 1, True
    return body, False


def _keyword_at(command: str, position: int, keyword: str) -> bool:
    end = position + len(keyword)
    if command[position:end].casefold() != keyword:
        return False
    return end == len(command) or not (command[end].isalnum() or command[end] in "_-")


def _skip_trivia(command: str, position: int) -> int:
    """Skip whitespace, line continuations, and comments."""
    while position < len(command):
        char = command[position]
        if char.isspace():
            position += 1
        elif command.startswith("<#", position):
            close = command.find("#>", position + 2)
            if close < 0:
                return len(command)
            position = close + 2
        elif char == "#":
            position = _line_end(command, position)
        elif char == "`" and command[position + 1 : position + 2] in ("\n", "\r"):
            position += 2
        else:
            break
    return position


def _construct_end(command: str, start: int, *, statement: bool = False) -> int | None:
    """Return the index after one bracketed construct or one whole statement.

    ``None`` means the construct is unterminated, so no insertion point after it
    is trustworthy.
    """
    closers: list[str] = []
    position = start
    while position < len(command):
        char = command[position]
        if char in _SINGLE_QUOTES or char in _DOUBLE_QUOTES:
            end = _string_end(command, position)
            if end is None:
                return None
            position = end
            continue
        if command.startswith("<#", position):
            close = command.find("#>", position + 2)
            if close < 0:
                return None
            position = close + 2
            continue
        if char == "#" and (position == start or command[position - 1].isspace()):
            position = _line_end(command, position)
            continue
        if char == "`":
            position += 2
            continue
        if char in _OPENERS:
            closers.append(_OPENERS[char])
        elif closers and char == closers[-1]:
            closers.pop()
            if not closers and not statement:
                return position + 1
        elif statement and not closers and char in ";\n\r":
            if command.startswith("\r\n", position):
                return position + 2
            return position + 1
        position += 1
    return len(command) if statement and not closers else None


def _string_end(command: str, start: int) -> int | None:
    """Return the index after a quoted string or here-string starting at ``start``."""
    quotes = _SINGLE_QUOTES if command[start] in _SINGLE_QUOTES else _DOUBLE_QUOTES
    expandable = quotes == _DOUBLE_QUOTES
    if start > 0 and command[start - 1] == "@" and command[start + 1 : start + 2] in ("\n", "\r"):
        terminator = command.find("\n" + command[start] + "@", start)
        return None if terminator < 0 else terminator + 3
    position = start + 1
    while position < len(command):
        char = command[position]
        if expandable and char == "`":
            position += 2
            continue
        if char in quotes:
            if command[position + 1 : position + 2] in quotes and position + 1 < len(command):
                position += 2
                continue
            return position + 1
        position += 1
    return None


def _line_end(command: str, position: int) -> int:
    for index in range(position, len(command)):
        if command[index] in "\n\r":
            return index
    return len(command)


__all__ = [
    "EXIT_STATUS_STATEMENT",
    "UTF8_OUTPUT_STATEMENT",
    "powershell_command",
    "with_utf8_output",
]
