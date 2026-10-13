"""PowerShell's bootstrap for one command: the command as written, in a plain-text console.

The shell Tool runs every Windows command in its own ``pwsh -NoProfile
-NonInteractive -Command <bootstrap>`` on pipes, without a terminal:
``Read-Host``, ``Get-Credential`` and confirmation prompts fail at once instead
of waiting. The bootstrap is fixed text (``POWERSHELL_BOOTSTRAP``). It sets up
the console, then reads one request from stdin until the input ends
(``powershell_request``): the command, its workdir and the call's layer of the
environment. It applies the request and runs the command as written; the
command itself finds its input at its end. Because the request comes last, a
shell can be started before its command is known and serve it at once
(``_command_launch``); a shell serves one command and exits with it. The
bootstrap prints nothing of its own: the output is the command's.

Request. ASCII lines, each field Base64 of its UTF-8 text: the workdir, the
command, one line per environment change (the name, then a space and the value;
the name alone removes the variable), and ``.`` as the last line, so a request
that ended early is never taken for a complete one. PowerShell hands stdin to
the command as ``$input`` lines, and its own reader holds the input: a direct
read of the console input stream races with it, ``$input`` does not. A trailing
empty line does not reach ``$input``, which the closing ``.`` makes harmless.

Workdir. The bootstrap sets PowerShell's location and the process's current
directory, which native programs and .NET methods use. A shell started in its
workdir skips both. A shell started ahead of its command first undoes its move
to where it waited, so the command finds the location history and drive
locations of a shell started in its workdir: ``cd -`` returns to the location
PowerShell opened at, and the drive where the shell waited is at its root.

Nothing ran. A request the bootstrap cannot read ends the shell with
``REQUEST_UNREADABLE_EXIT_CODE``, a workdir it cannot enter with
``WORKDIR_UNAVAILABLE_EXIT_CODE``, before the command runs and without output.
The shell Tool reports either as a failure of the call, not as the command's
result. No program is expected to exit with these codes, and the shell Tool also
requires that nothing was printed and no program failed.

Console encodings. Native programs write and read bytes in the console's code
pages, which default to a legacy OEM page that cannot represent most non-ASCII
text, and PowerShell decodes the output it captures from native programs with
the same page. Both pages are set to UTF-8 before the command runs; vBot decodes
the output as UTF-8.

Plain output. PowerShell colors its formatting and errors with ANSI escape
sequences even on a pipe; the setup renders them as plain text. On a pipe it
also formats tables and lists for a 120-column host and cuts longer rows with
an ellipsis; the setup widens the host buffer to 200 columns, so a formatted row
keeps up to 199 characters.

English messages. PowerShell's own messages follow the Windows display language.
The setup switches them to en-US, so Agents and the failure hints read one
wording. Numbers and dates keep the user's regional format, as in the user's
own shell; native programs keep the user's settings.

Parsing after the setup. The command arrives as data and is parsed after the
setup by PowerShell's own parser: ``using`` statements, ``#requires``, a
``param`` block and named blocks keep their meaning, a syntax error is reported
as PowerShell's own ParserError in English, and error positions are the
command's own line numbers.

Exit status. ``pwsh -Command`` exits with 1 whenever the last statement failed,
so a native program's own exit code is lost. A closing statement at the end of
the command's last block records that code instead, as ``bash -c`` reports it,
and otherwise 0 or 1 by the last statement's success; an ``exit`` in the command
ends the shell with its own code. The statement is the only text added to the
command: PowerShell keeps the last statement's success (``$?``) only within the
command's statement list, so no statement outside it can read that success. It
follows the command's last line, so no line of the command moves.

Output. The command's output goes to PowerShell's Out-Default as one pipeline,
as ``pwsh -Command`` formats a script's output, and the bootstrap exits with the
recorded code after that pipeline ended. As in ``pwsh -Command``, an ``exit`` in
the command discards objects the table formatter still holds.

Hidden bookkeeping. The command sees none of the bootstrap's variables; it runs
as a function's dot-sourced body, and ``$Error`` is empty when it starts, as in
a new shell. Only the bootstrap's ``__vbot`` functions remain visible.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping

# Without a byte order mark: a console encoding with one would write it first.
UTF8_CONSOLE_STATEMENT = (
    "[Console]::OutputEncoding = [Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)"
)
# Messages in English; numbers, dates and parsing keep the user's culture. New
# threads, such as ForEach-Object -Parallel runspaces, take the default.
UI_CULTURE_STATEMENT = (
    "[cultureinfo]::CurrentUICulture = [cultureinfo]::DefaultThreadCurrentUICulture = 'en-US'"
)
# PowerShell's formatting and errors without ANSI escape sequences (PowerShell 7.2+).
PLAIN_TEXT_STATEMENT = "if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }"
# Formatted rows keep up to this many characters less one. A host whose buffer
# cannot be resized keeps its width; $Error is cleared before the command runs.
OUTPUT_COLUMNS = 200
OUTPUT_WIDTH_STATEMENT = (
    f"& {{ try {{ $ui = $Host.UI.RawUI; if ($ui.BufferSize.Width -lt {OUTPUT_COLUMNS}) {{ "
    "$ui.BufferSize = [System.Management.Automation.Host.Size]::new("
    f"{OUTPUT_COLUMNS}, $ui.BufferSize.Height) }} }} catch {{}} }}"
)
# Read $? before any bookkeeping changes it or $LASTEXITCODE. The statement only
# records the code; the bootstrap exits after the command's output is formatted.
# Explicit exit statements bypass this epilogue, preserving the caller's control
# flow. Get-Variable reads LASTEXITCODE even when it was never set under
# Set-StrictMode.
EXIT_STATUS_STATEMENT = (
    "$global:__vbotExitCode = if ($?) { 0 } else { "
    "$__vbotLastCode = Get-Variable LASTEXITCODE -ValueOnly -ErrorAction Ignore; "
    "if ($__vbotLastCode) { $__vbotLastCode } else { 1 } }"
)
SETUP_STATEMENT = "; ".join(
    (UTF8_CONSOLE_STATEMENT, UI_CULTURE_STATEMENT, PLAIN_TEXT_STATEMENT, OUTPUT_WIDTH_STATEMENT)
)
# The bootstrap's exit codes when the command did not run, without output: "VB"
# in the high bytes, which no program is expected to use (see the module docstring).
REQUEST_UNREADABLE_EXIT_CODE = 0x56420001
WORKDIR_UNAVAILABLE_EXIT_CODE = 0x56420002
# A shell started ahead of its command waits in Windows' system directory
# (``_command_launch``). While it waits it pays the first use of what follows:
# type lookups, the parser, PowerShell's utility and file cmdlets, formatting, an
# empty command through Out-Default, and moving back and forth, which leaves the
# location history as the shell started. That takes about 40 ms from each warm
# command; a shell started for its command skips it, since the request is
# already there. None of it writes output; an error it records is cleared before
# the command runs.
WARM_UP_STATEMENTS = r"""$__vbotUtf8 = [System.Text.Encoding]::UTF8
if ([Environment]::CurrentDirectory -eq [Environment]::SystemDirectory) {
    try {
        $null = [Convert]::FromBase64String('LA=='), [NullString]::Value
        $null = [System.Management.Automation.Language.Parser]::ParseInput(
            '"$a" | % { $_ }', [ref]$null, [ref]$null)
        Import-Module -ErrorAction Stop -Name (
            'Microsoft.PowerShell.Utility', 'Microsoft.PowerShell.Management')
        $null = [pscustomobject]@{ Name = 1 } | Out-String
        ${function:__vbotBody} = [scriptblock]::Create('')
        . __vbotInvoke | Out-Default
        Remove-Variable -Name __vbotWarm* -Scope Global
        Set-Location - -ErrorAction Stop
        Set-Location -LiteralPath ([Environment]::CurrentDirectory) -ErrorAction Stop
    } catch {}
}
"""
WARM_UP_STATEMENTS = re.sub(r"(?m)^[ \t]+", "", WARM_UP_STATEMENTS)
# Wait for the request, then apply it: the environment layer, then the location of
# PowerShell and of the process, unless the shell started there. [NullString]::Value
# removes a variable, since PowerShell passes $null to a string parameter as ''.
# A shell that waited elsewhere first undoes its move there, back to where
# PowerShell opened, so the move to the workdir leaves the location history of a
# shell started in it; the drive it waited on returns to its root. Either failure
# exits with its code and prints nothing.
REQUEST_STATEMENTS = r"""$__vbotRequest = @($input)
try {
    if ($__vbotRequest.Count -lt 3 -or $__vbotRequest[-1] -ne '.') { throw 'ended early' }
    $__vbotWorkdir = $__vbotUtf8.GetString([Convert]::FromBase64String($__vbotRequest[0]))
    $__vbotCommand = $__vbotUtf8.GetString([Convert]::FromBase64String($__vbotRequest[1]))
    for ($__vbotIndex = 2; $__vbotIndex -lt $__vbotRequest.Count - 1; $__vbotIndex++) {
        $__vbotPair = $__vbotRequest[$__vbotIndex].Split(' ')
        $__vbotValue = if ($__vbotPair.Count -gt 1) {
            $__vbotUtf8.GetString([Convert]::FromBase64String($__vbotPair[1]))
        } else { [NullString]::Value }
        [Environment]::SetEnvironmentVariable(
            $__vbotUtf8.GetString([Convert]::FromBase64String($__vbotPair[0])), $__vbotValue)
    }
} catch {
    exit {unreadable}
}
if ($PWD.ProviderPath -ne $__vbotWorkdir) {
    try {
        $__vbotDrive = $PWD.Drive
        try { Set-Location - -ErrorAction Stop } catch {}
        Set-Location -LiteralPath $__vbotWorkdir -ErrorAction Stop
        [Environment]::CurrentDirectory = $__vbotWorkdir
        if ($__vbotDrive -and $PWD.Drive -ne $__vbotDrive) { $__vbotDrive.CurrentLocation = '' }
    } catch {
        exit {unavailable}
    }
}
"""
REQUEST_STATEMENTS = re.sub(r"(?m)^[ \t]+", "", REQUEST_STATEMENTS)
# Parse the command after the setup; a syntax error is thrown as PowerShell reports
# its own. The exit status runs at the end of the last block: the unnamed body, a
# named end block, or an end block added after begin or process blocks. The
# command runs without the bootstrap's variables and with an empty $Error; the
# exit status statement sets the only variable, and a command that returns before
# reaching it exits with 0.
RUN_STATEMENTS = r"""$__vbotParseErrors = $null
$__vbotAst = [System.Management.Automation.Language.Parser]::ParseInput(
    $__vbotCommand, [ref]$null, [ref]$__vbotParseErrors)
if ($__vbotParseErrors) {
    throw [System.Management.Automation.ParseException]::new($__vbotParseErrors)
}
$__vbotEnd = $__vbotAst.EndBlock
$__vbotExitStatus = '{exit_status}'
$__vbotCommand = if ($__vbotEnd -and -not $__vbotEnd.Unnamed) {
    $__vbotCommand.Insert($__vbotEnd.Extent.EndOffset - 1, "`n`n$__vbotExitStatus`n")
} elseif ($__vbotAst.BeginBlock -or $__vbotAst.ProcessBlock -or $__vbotAst.DynamicParamBlock) {
    "$__vbotCommand`nend {`n$__vbotExitStatus`n}"
} else {
    "$__vbotCommand`n`n$__vbotExitStatus"
}
${function:__vbotBody} = [scriptblock]::Create($__vbotCommand)
Remove-Variable -Name __vbot* -Scope Global
$Error.Clear()
. __vbotInvoke | Out-Default
$__vbotExitCode = Get-Variable -Name __vbotExitCode -Scope Global -ValueOnly -ErrorAction Ignore
if (-not $__vbotExitCode) { exit 0 }
exit $__vbotExitCode
"""
RUN_STATEMENTS = re.sub(r"(?m)^[ \t]+", "", RUN_STATEMENTS)
# The command runs from the first line. Write-Error names its caller's position,
# which PowerShell shows only beyond the first line, so no bootstrap line appears in
# the command's errors. Dot-sourcing the function and its body keeps the command's
# own scope; the body is the command's script block, not a function of its own,
# whose name its errors would show.
INVOKE_STATEMENT = "function __vbotInvoke { . ${function:__vbotBody} }"
_QUOTE = re.compile("(['‘’‚‛])")


def powershell_literal(text: str) -> str:
    """Return *text* as a single-quoted PowerShell string that keeps it as data."""
    return f"'{_escaped(text)}'"


def _escaped(text: str) -> str:
    """Return *text* escaped for a single-quoted PowerShell string."""
    # PowerShell also treats typographic single quotes as quotes; a doubled one is literal.
    return _QUOTE.sub(r"\1\1", text)


# The setup and the invoking function share the first line. Everything before
# the request runs before the command is known.
POWERSHELL_BOOTSTRAP = (
    f"{SETUP_STATEMENT}; {INVOKE_STATEMENT}\n"
    + WARM_UP_STATEMENTS
    + REQUEST_STATEMENTS.replace("{unreadable}", str(REQUEST_UNREADABLE_EXIT_CODE)).replace(
        "{unavailable}", str(WORKDIR_UNAVAILABLE_EXIT_CODE)
    )
    + RUN_STATEMENTS.replace("{exit_status}", _escaped(EXIT_STATUS_STATEMENT))
)


def powershell_request(command: str, workdir: str, changes: Mapping[str, str | None]) -> bytes:
    """Return the request ``POWERSHELL_BOOTSTRAP`` reads: run *command* in *workdir*.

    *changes* is the call's layer of the environment over the shell's start
    environment: a value sets the variable, None removes it.
    """
    lines = [_base64(workdir), _base64(command)]
    for name, value in changes.items():
        lines.append(_base64(name) if value is None else f"{_base64(name)} {_base64(value)}")
    lines.append(".")
    return "\n".join(lines).encode("ascii") + b"\n"


def _base64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8", "surrogatepass")).decode("ascii")


__all__ = [
    "EXIT_STATUS_STATEMENT",
    "OUTPUT_COLUMNS",
    "OUTPUT_WIDTH_STATEMENT",
    "PLAIN_TEXT_STATEMENT",
    "POWERSHELL_BOOTSTRAP",
    "REQUEST_UNREADABLE_EXIT_CODE",
    "SETUP_STATEMENT",
    "UI_CULTURE_STATEMENT",
    "UTF8_CONSOLE_STATEMENT",
    "WORKDIR_UNAVAILABLE_EXIT_CODE",
    "powershell_literal",
    "powershell_request",
]
