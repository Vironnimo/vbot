"""PowerShell command text for ``pwsh -Command``: UTF-8 output and a faithful exit status.

PowerShell encodes redirected output in the console output code page, usually a
legacy OEM page that cannot represent most non-ASCII text, while Bash decodes
every process pipe as UTF-8. Setting the console output encoding at the start of
the command makes cmdlet output, captured native output, and native console
programs started by the command (``cmd``, ``dir``, ...) use UTF-8 as well.

Agents habitually pipe into ``head``, ``tail``, and ``wc -l``, which PowerShell
lacks. When such a command is genuinely missing, a command-not-found hook runs
an equivalent built from Select-Object and Get-Content for the line-count
forms (``-n N``, ``-N``, ``tail -n +N``, ``wc -l``, pipeline or files); any
other option stops the script with a message naming the PowerShell equivalent.
An installed ``head`` (for example from Git for Windows) still runs itself.

``pwsh -Command`` exits with 1 whenever the last statement failed, so a native
program's own exit code is lost. A closing statement exits with that code
instead, as ``bash -c`` does, and otherwise with 0 or 1 by the last statement's
success.

If the last statement succeeded after PowerShell recorded an error, the exit
status stays zero but a bounded diagnostic is appended. This uses ErrorRecord
identity, not output text or a count increase: printed test failures are not
errors, and PowerShell's bounded error collection may already be full. Errors
can have been caught intentionally, which the diagnostic states explicitly.

``using`` statements and a script ``param`` block must precede every other
statement, and named script blocks must contain every statement, so the one-line
setup statement is inserted after those constructs, and a script of named blocks
keeps PowerShell's own exit status. The small scanner below only recognizes
this leading script structure; everything after it stays verbatim.
"""

from __future__ import annotations

UTF8_OUTPUT_STATEMENT = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)"
ERROR_RECORD_BASELINE_STATEMENT = "$__vbotInitialError = $Error | Select-Object -First 1"
POWERSHELL_ERROR_NOTE = (
    "PowerShell recorded errors before exiting with code 0. Errors may have been handled; "
    "inspect the error output and verify any changed files before relying on this result."
)
# Enter the success branch before any bookkeeping changes $? or $LASTEXITCODE.
# Explicit exit statements bypass this epilogue, preserving the caller's control
# flow. Clearing $Error also suppresses it; no note is not proof of no errors.
EXIT_STATUS_STATEMENT = (
    "if ($?) { "
    "if ($Error.Count -and -not [object]::ReferenceEquals($Error[0], $__vbotInitialError)) { "
    "$__vbotErrorSummary = ([string]$Error[0]) -replace '\\s+', ' '; "
    "if ($__vbotErrorSummary.Length -gt 500) { "
    "$__vbotErrorSummary = $__vbotErrorSummary.Substring(0, 500) + '...' }; "
    f"[Console]::Error.WriteLine('{POWERSHELL_ERROR_NOTE} Latest error: ' + "
    "$__vbotErrorSummary) }; exit 0 }; "
    "if ($LASTEXITCODE) { exit $LASTEXITCODE }; exit 1"
)
# One line, so PowerShell's error positions stay one line off the Agent's command.
UNIX_LINE_FILTERS_STATEMENT = "".join(
    (
        "$ExecutionContext.InvokeCommand.CommandNotFoundAction = {param($Name, $EventArgs); ",
        "if ($Name -notin 'head', 'tail', 'wc') { return }; $EventArgs.StopSearch = $true; ",
        "$EventArgs.CommandScriptBlock = {begin {",
        "$count = 10; $from = 0; $lines = $Name -ne 'wc'; $files = @(); $options = $true; ",
        "$piped = $MyInvocation.ExpectingInput; $seen = 0; ",
        "$last = [System.Collections.Generic.Queue[object]]::new(); ",
        "$alternative = if ($Name -eq 'head') { 'Select-Object -First N' } ",
        "else { 'Select-Object -Last N' }; ",
        "for ($i = 0; $i -lt $args.Count; $i++) {$a = [string]$args[$i]; ",
        "if ($a -eq '-') { continue }; ",
        "if ($options -and $a -eq '--') { $options = $false; continue }; ",
        "if (-not $options -or $a -notmatch '^-.') { $files += $a; continue }; ",
        "if ($Name -eq 'wc') { if ($a -in '-l', '--lines') { $lines = $true; continue } } ",
        "elseif ($a -match '^-(\\d+)$') { $count = [int]$Matches[1]; continue } ",
        "elseif ($a -match '^(?:-n|--lines=?)(.*)$') {$value = $Matches[1]; ",
        "if ($value -eq '') { $i++; $value = [string]$args[$i] }; ",
        "if ($value -match '^(\\+?)(\\d+)$') {$count = [int]$Matches[2]; ",
        "if ($Matches[1] -and $Name -eq 'tail') { $from = [math]::Max(1, $count) }; continue}}; ",
        "if ($Name -eq 'wc') { throw \"wc: $a is not supported here; use wc -l, or ",
        'Measure-Object -Word or -Character." }; ',
        'throw "${Name}: $a is not supported here; use $Name -n N or $alternative."}; ',
        "if (-not $lines) { throw 'wc: only wc -l is supported here; use Measure-Object ",
        "-Word or -Character for other counts.' }} ",
        "process {if ($files.Count -or -not $piped) { return }; $seen++; ",
        "if ($Name -eq 'head') { if ($seen -le $count) { $_ } } ",
        "elseif ($Name -eq 'tail') {if ($from) { if ($seen -ge $from) { $_ } } ",
        "else { $last.Enqueue($_); if ($last.Count -gt $count) { [void]$last.Dequeue() } }}} ",
        "end {if (-not $files.Count) { if ($Name -eq 'wc') { $seen } ",
        "elseif (-not $from) { $last }; return }; ",
        "$total = 0; foreach ($file in $files) {",
        "if ($Name -eq 'wc') { $n = @(Get-Content -LiteralPath $file -ErrorAction Stop).Count; ",
        '$total += $n; "$n $file"; continue }; ',
        'if ($files.Count -gt 1) { "==> $file <==" }; ',
        "if ($Name -eq 'head') { ",
        "Get-Content -LiteralPath $file -TotalCount $count -ErrorAction Stop } ",
        "elseif ($from) { Get-Content -LiteralPath $file -ErrorAction Stop | ",
        "Select-Object -Skip ($from - 1) } ",
        "else { Get-Content -LiteralPath $file -Tail $count -ErrorAction Stop }}; ",
        "if ($Name -eq 'wc' -and $files.Count -gt 1) { \"$total total\" }}",
        "}.GetNewClosure()}",
    )
)
SETUP_STATEMENT = (
    f"{UTF8_OUTPUT_STATEMENT}; {UNIX_LINE_FILTERS_STATEMENT}; {ERROR_RECORD_BASELINE_STATEMENT}"
)

_SINGLE_QUOTES = "'‘’‚‛"
_DOUBLE_QUOTES = '"“”„'
_OPENERS = {"(": ")", "{": "}", "[": "]"}
_NAMED_BLOCKS = ("dynamicparam", "begin", "process", "end", "clean")


def powershell_command(command: str) -> str:
    """Return ``command`` with the setup line and, where PowerShell allows it, its exit status."""
    position, named_blocks = _body_start(command)
    wrapped = _with_setup(command, position)
    if named_blocks:
        return wrapped
    return f"{wrapped}\n{EXIT_STATUS_STATEMENT}"


def with_setup(command: str) -> str:
    """Return ``command`` with the setup line where PowerShell allows it."""
    return _with_setup(command, _body_start(command)[0])


def _with_setup(command: str, position: int) -> str:
    prefix = command[:position]
    if prefix and not prefix.endswith(("\n", "\r", "{")):
        prefix += "\n"
    return f"{prefix}{SETUP_STATEMENT}\n{command[position:]}"


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
    "SETUP_STATEMENT",
    "UNIX_LINE_FILTERS_STATEMENT",
    "UTF8_OUTPUT_STATEMENT",
    "powershell_command",
    "with_setup",
]
