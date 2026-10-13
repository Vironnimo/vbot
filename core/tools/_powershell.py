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

Request. ASCII lines: the request's not-run code (below) in nine decimal
digits, then each field as Base64 of its UTF-8 text: the workdir, the command,
one line per environment change (the name, then a space and the value; the name
alone removes the variable), and ``.`` as the last line, so a request that
ended early is never taken for a complete one. PowerShell hands stdin to
the command as ``$input`` lines, and its own reader holds the input: a direct
read of the console input stream races with it, ``$input`` does not. A trailing
empty line does not reach ``$input``, which the closing ``.`` makes harmless.

Workdir. The bootstrap sets PowerShell's location and the process's current
directory, which native programs and .NET methods use. A shell started in its
workdir skips both. A shell started ahead of its command first undoes its move
to where it waited, so the command finds the location history and drive
locations of a shell started in its workdir: ``cd -`` returns to the location
PowerShell opened at, and the drive where the shell waited is at its root.

Nothing ran. vBot draws a random code for each request (``PowerShellRequest``).
A request the bootstrap cannot read ends the shell with that code, a workdir it
cannot enter with the code after it, before the command runs and without
output; the shell Tool reports either as a failure of the call, not as the
command's result (``CommandReport.not_run``). The command cannot know the code:
it travels only in the request, which the bootstrap has read from stdin before
the command runs, and in bootstrap variables removed by then. Only a command
that exits with such a code anyway, by a chance of one in 900 million, is
reported as not run although it ran. A fixed code was no proof: a command that
ran, printed nothing and exited with it read as one that never ran (shell
review 2026-10-13). A request too short to carry its code ends the shell with
``REQUEST_UNREADABLE_EXIT_CODE``, reported as an exit code like any other: vBot
writes every request whole, so only a shell whose input was cut meets it.

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
so a native program's own exit code is lost. vBot reports the last statement's
status as ``bash -c`` reports it: a failed statement reports the last native
program's code, else 1; a successful one 0, unless it calls a function, script
block or script whose last statement runs a native program, which reports that
program's code as bash reports a function's status (``STATUS_FUNCTION``). A
closing statement at the end of the last block that runs, the command's clean
block if it has one, else its end block, records it. It is the only text added
to the command and follows the block's last line, so no line or column of the
command moves; a block that another named block follows in the text gets none.
A command without it, or one that returns before it with a top-level ``return``,
reports the success its last statement left (``$?``), which the last element of
the pipeline the command runs in reads (``__vbotReturned``), without the look
into a called function. An ``exit`` in the command ends the shell with
its own code. A ``break`` or ``continue`` outside a loop ends the command with
0, where plain pwsh reports the last statement's success.

Output. The command's output and its errors go to PowerShell's Out-Default as
one pipeline (``2>&1``), as ``pwsh -Command`` merges them, so an error shows
between the rows the formatter printed before it and those after it. Native
programs' stderr is not captured by that merge and passes as bytes. A clean
block runs after Out-Default ended, so its output and errors are dropped, as in
plain pwsh. The bootstrap exits with the recorded code after that
pipeline ended. As in ``pwsh -Command``, an ``exit`` in the command discards
objects the table formatter still holds.

Hidden bookkeeping. The command sees none of the bootstrap's variables; it runs
as a function's dot-sourced body, and ``$Error`` is empty when it starts, as in
a new shell. Only the bootstrap's ``__vbot`` functions remain visible.
"""

from __future__ import annotations

import base64
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

from core.tools._command_types import NotRunReason

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
# Pass $? before any bookkeeping changes it or $LASTEXITCODE. The statement only
# records the code; the bootstrap exits after the command's output is formatted.
# Explicit exit statements bypass this epilogue, preserving the caller's control
# flow.
EXIT_STATUS_STATEMENT = "$global:__vbotExitCode = __vbotStatus $?"
# The status of the command's last statement, the one before the exit status
# statement, as bash -c reports it (see the module docstring). A command that
# returned before that statement gets its status from __vbotReturned instead, the
# last element of its pipeline, whose end block runs before any other statement
# can change $?. A failed statement reports the last native program's code,
# else 1. A successful one reports 0, unless it is a call of a function, filter,
# script block or script (also through an alias or as an assignment's value) whose
# body's last statement runs a native program: PowerShell's $? is true after such a
# call whatever the program's code, and bash reports a function's status as that of
# its last command. A body's last statement that is such a call again is followed
# (at most 10 deep); one that runs a native program, alone or in a pipeline or
# assignment, reports $LASTEXITCODE; anything else (a cmdlet, an expression, an if,
# foreach or try statement, a call that cannot be resolved) reports 0. It runs after
# the command's last statement and only reads: the command observes nothing of it,
# and it never throws into a trap of the command. Get-Variable reads LASTEXITCODE
# even when it was never set under Set-StrictMode; the variables carry the prefix,
# so none hides one of the command's from the lookup of `& $name`.
STATUS_FUNCTION = r"""function __vbotStatus($__vbotSucceeded) {
    $__vbotCode = Get-Variable -Name LASTEXITCODE -ValueOnly -ErrorAction Ignore
    if (-not $__vbotSucceeded) {
        if ($__vbotCode) { return $__vbotCode }
        return 1
    }
    if (-not $__vbotCode) { return 0 }
    try {
        $__vbotA = 'System.Management.Automation.Language.'
        $__vbotFind = {
            param($__vbotElement)
            if ($__vbotElement -isnot "${__vbotA}CommandAst") { return }
            $__vbotTarget = $__vbotElement.CommandElements[0]
            $__vbotTarget = if ($__vbotTarget -is "${__vbotA}ScriptBlockExpressionAst") {
                $__vbotTarget.ScriptBlock
            } elseif ($__vbotTarget -is "${__vbotA}VariableExpressionAst") {
                $__vbotName = $__vbotTarget.VariablePath.UserPath
                Get-Variable -Name $__vbotName -ValueOnly -ErrorAction Ignore
            } else {
                $__vbotElement.GetCommandName()
            }
            if ($__vbotTarget -is [string]) {
                $__vbotTarget = $ExecutionContext.InvokeCommand.GetCommand($__vbotTarget, 'All')
            }
            while ($__vbotTarget -is [System.Management.Automation.AliasInfo]) {
                $__vbotTarget = $__vbotTarget.ResolvedCommand
            }
            if ($__vbotTarget -is [System.Management.Automation.FunctionInfo] -or
                $__vbotTarget -is [System.Management.Automation.ExternalScriptInfo]) {
                $__vbotTarget = $__vbotTarget.ScriptBlock
            }
            if ($__vbotTarget -is [scriptblock]) { $__vbotTarget = $__vbotTarget.Ast }
            if ($__vbotTarget -is "${__vbotA}FunctionDefinitionAst") {
                $__vbotTarget = $__vbotTarget.Body
            }
            $__vbotTarget
        }
        $__vbotAst = ${function:__vbotBody}.Ast
        $__vbotBlock = $__vbotAst.PSObject.Properties['CleanBlock']
        $__vbotBlock = if ($__vbotBlock -and $__vbotBlock.Value) {
            $__vbotBlock.Value
        } else { $__vbotAst.EndBlock }
        $__vbotStatements = $__vbotBlock.Statements
        $__vbotStatement = if ($__vbotStatements.Count -gt 1) {
            $__vbotStatements[$__vbotStatements.Count - 2]
        }
        for ($__vbotDepth = 0; $__vbotStatement -and $__vbotDepth -le 10; $__vbotDepth++) {
            if ($__vbotStatement -is "${__vbotA}AssignmentStatementAst") {
                $__vbotStatement = $__vbotStatement.Right
            }
            if ($__vbotStatement -isnot "${__vbotA}PipelineAst") { break }
            $__vbotElements = $__vbotStatement.PipelineElements
            $__vbotStatement = $null
            foreach ($__vbotElement in $__vbotElements) {
                $__vbotTarget = & $__vbotFind $__vbotElement
                if ($__vbotTarget -is [System.Management.Automation.ApplicationInfo]) {
                    if ($__vbotDepth) { return $__vbotCode }
                } elseif ($__vbotElements.Count -eq 1 -and
                    $__vbotTarget -is "${__vbotA}ScriptBlockAst") {
                    $__vbotBlocks = $__vbotTarget.EndBlock, $__vbotTarget.ProcessBlock,
                        $__vbotTarget.BeginBlock
                    foreach ($__vbotBlock in $__vbotBlocks) {
                        if ($__vbotBlock -and $__vbotBlock.Statements.Count) {
                            $__vbotStatement = $__vbotBlock.Statements[-1]
                            break
                        }
                    }
                }
            }
        }
    } catch {}
    0
}
function __vbotReturned {
    end {
        $__vbotSucceeded = $?
        if (-not (Get-Variable -Name __vbotExitCode -Scope Global -ErrorAction Ignore)) {
            $global:__vbotExitCode = if ($__vbotSucceeded) { 0 } else { __vbotStatus $false }
        }
    }
}
"""
STATUS_FUNCTION = re.sub(r"(?m)^[ \t]+", "", STATUS_FUNCTION)
SETUP_STATEMENT = "; ".join(
    (UTF8_CONSOLE_STATEMENT, UI_CULTURE_STATEMENT, PLAIN_TEXT_STATEMENT, OUTPUT_WIDTH_STATEMENT)
)
# The bootstrap's exit code for a request too short to carry its not-run code:
# "VB" in the high bytes. A command can exit with it too, so it proves nothing.
REQUEST_UNREADABLE_EXIT_CODE = 0x56420001
# A request's not-run code has nine decimal digits; the code after it has too.
_NOT_RUN_CODE_FIRST = 100_000_000
_NOT_RUN_CODES = 899_999_998
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
        . __vbotInvoke
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
# exits with the request's not-run code, or the code after it, and prints nothing.
REQUEST_STATEMENTS = r"""$__vbotRequest = @($input)
$__vbotNotRun = {unreadable}
try {
    if ($__vbotRequest[0] -notmatch '^[0-9]{9}$') { throw 'no not-run code' }
    $__vbotNotRun = [int]$__vbotRequest[0]
    if ($__vbotRequest.Count -lt 4 -or $__vbotRequest[-1] -ne '.') { throw 'ended early' }
    $__vbotWorkdir = $__vbotUtf8.GetString([Convert]::FromBase64String($__vbotRequest[1]))
    $__vbotCommand = $__vbotUtf8.GetString([Convert]::FromBase64String($__vbotRequest[2]))
    for ($__vbotIndex = 3; $__vbotIndex -lt $__vbotRequest.Count - 1; $__vbotIndex++) {
        $__vbotPair = $__vbotRequest[$__vbotIndex].Split(' ')
        $__vbotValue = if ($__vbotPair.Count -gt 1) {
            $__vbotUtf8.GetString([Convert]::FromBase64String($__vbotPair[1]))
        } else { [NullString]::Value }
        [Environment]::SetEnvironmentVariable(
            $__vbotUtf8.GetString([Convert]::FromBase64String($__vbotPair[0])), $__vbotValue)
    }
} catch {
    exit $__vbotNotRun
}
if ($PWD.ProviderPath -ne $__vbotWorkdir) {
    try {
        $__vbotDrive = $PWD.Drive
        try { Set-Location - -ErrorAction Stop } catch {}
        Set-Location -LiteralPath $__vbotWorkdir -ErrorAction Stop
        [Environment]::CurrentDirectory = $__vbotWorkdir
        if ($__vbotDrive -and $PWD.Drive -ne $__vbotDrive) { $__vbotDrive.CurrentLocation = '' }
    } catch {
        exit ($__vbotNotRun + 1)
    }
}
"""
REQUEST_STATEMENTS = re.sub(r"(?m)^[ \t]+", "", REQUEST_STATEMENTS)
# Parse the command after the setup; a syntax error is thrown as PowerShell reports
# its own. The exit status runs at the end of the last block that runs: a clean
# block, else the unnamed body, a named end block, or an end block added after
# begin or process blocks. A named block that another named block follows in the
# text gets none, since the lines after it would move; __vbotReturned reads the
# status then. The command runs without the bootstrap's variables and with an
# empty $Error; the exit status sets the only variable, and a command that ends
# without it, through break or continue outside a loop, exits with 0.
RUN_STATEMENTS = r"""$__vbotParseErrors = $null
$__vbotAst = [System.Management.Automation.Language.Parser]::ParseInput(
    $__vbotCommand, [ref]$null, [ref]$__vbotParseErrors)
if ($__vbotParseErrors) {
    throw [System.Management.Automation.ParseException]::new($__vbotParseErrors)
}
$__vbotLast = $__vbotAst.CleanBlock
if (-not $__vbotLast) { $__vbotLast = $__vbotAst.EndBlock }
$__vbotLater = @($__vbotAst.DynamicParamBlock, $__vbotAst.BeginBlock, $__vbotAst.ProcessBlock,
    $__vbotAst.EndBlock).Where({ $_ -and $_.Extent.StartOffset -gt $__vbotLast.Extent.StartOffset })
$__vbotExitStatus = '{exit_status}'
$__vbotCommand = if ($__vbotLast -and -not $__vbotLast.Unnamed) {
    if ($__vbotLater) { $__vbotCommand } else {
        $__vbotCommand.Insert($__vbotLast.Extent.EndOffset - 1, "`n`n$__vbotExitStatus`n")
    }
} elseif ($__vbotAst.BeginBlock -or $__vbotAst.ProcessBlock -or $__vbotAst.DynamicParamBlock) {
    "$__vbotCommand`nend {`n$__vbotExitStatus`n}"
} else {
    "$__vbotCommand`n`n$__vbotExitStatus"
}
${function:__vbotBody} = [scriptblock]::Create($__vbotCommand)
Remove-Variable -Name __vbot* -Scope Global
$Error.Clear()
. __vbotInvoke
$__vbotExitCode = Get-Variable -Name __vbotExitCode -Scope Global -ValueOnly -ErrorAction Ignore
if (-not $__vbotExitCode) { exit 0 }
exit $__vbotExitCode
"""
RUN_STATEMENTS = re.sub(r"(?m)^[ \t]+", "", RUN_STATEMENTS)
# The command runs from the first line. Write-Error names its caller's position,
# which PowerShell shows only beyond the first line, so no bootstrap line appears in
# the command's errors. Dot-sourcing the function and its body keeps the command's
# own scope; the body is the command's script block, not a function of its own,
# whose name its errors would show. Its errors merge into Out-Default's input, as
# pwsh -Command merges a script's; __vbotReturned reads the status of a command
# that returned early.
INVOKE_STATEMENT = (
    "function __vbotInvoke { . ${function:__vbotBody} 2>&1 | Out-Default | __vbotReturned }"
)
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
    + STATUS_FUNCTION
    + WARM_UP_STATEMENTS
    + REQUEST_STATEMENTS.replace("{unreadable}", str(REQUEST_UNREADABLE_EXIT_CODE))
    + RUN_STATEMENTS.replace("{exit_status}", _escaped(EXIT_STATUS_STATEMENT))
)


@dataclass(frozen=True, slots=True)
class PowerShellRequest:
    """What ``POWERSHELL_BOOTSTRAP`` reads from stdin, and how it says it ran nothing."""

    data: bytes
    # The exit codes the bootstrap ends with, without output, when it runs nothing
    # of the command, and why (``CommandManager.launch``). Drawn for this request.
    not_run_codes: Mapping[int, NotRunReason]


def powershell_request(
    command: str, workdir: str, changes: Mapping[str, str | None]
) -> PowerShellRequest:
    """Return the request ``POWERSHELL_BOOTSTRAP`` reads: run *command* in *workdir*.

    *changes* is the call's layer of the environment over the shell's start
    environment: a value sets the variable, None removes it.
    """
    code = _NOT_RUN_CODE_FIRST + secrets.randbelow(_NOT_RUN_CODES)
    lines = [str(code), _base64(workdir), _base64(command)]
    for name, value in changes.items():
        lines.append(_base64(name) if value is None else f"{_base64(name)} {_base64(value)}")
    lines.append(".")
    return PowerShellRequest(
        "\n".join(lines).encode("ascii") + b"\n",
        {code: "request_unreadable", code + 1: "workdir_unavailable"},
    )


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
    "PowerShellRequest",
    "powershell_literal",
    "powershell_request",
]
