"""PowerShell command text for ``pwsh -Command``: readable output and a faithful exit status.

PowerShell encodes redirected output in the console output code page, usually a
legacy OEM page that cannot represent most non-ASCII text, while Bash decodes
every process pipe as UTF-8. Setting the console output encoding before the
command runs makes cmdlet output, captured native output, and native console
programs started by the command (``cmd``, ``dir``, ...) use UTF-8 as well.
PowerShell's own messages use English whatever the Windows display language, so
Agents and output hints read one wording; number and date formats keep the
user's culture.

``pwsh -Command`` parses its whole text before running any of it, so a syntax
error in the Agent's command would be reported before that setup, in the console
code page and the display language. The command therefore travels as a string
literal and is parsed after the setup, by PowerShell's own parser: ``using``
statements, a ``param`` block and named blocks keep their meaning, and error
positions are the command's own line numbers.

Agents habitually pipe into ``head``, ``tail``, and ``wc -l``, which PowerShell
lacks. When such a command is genuinely missing, a command-not-found hook runs
an equivalent built from Select-Object and Get-Content for the line-count
forms (``-n N``, ``-N``, ``tail -n +N``, ``wc -l``, pipeline or files); any
other option stops the script with a message naming the PowerShell equivalent.
An installed ``head`` (for example from Git for Windows) still runs itself.

``pwsh -Command`` exits with 1 whenever the last statement failed, so a native
program's own exit code is lost. A closing statement at the end of the command's
last block exits with that code instead, as ``bash -c`` does, and otherwise with
0 or 1 by the last statement's success.

If the last statement succeeded after PowerShell recorded an error, the exit
status stays zero but a bounded diagnostic is appended. This uses ErrorRecord
identity, not output text or a count increase: printed test failures are not
errors, and PowerShell's bounded error collection may already be full. An error
the command's own text silenced does not count: its command passes
``-ErrorAction SilentlyContinue`` or ``Ignore``, it or an enclosing command
redirects the error stream (``2>$null``, ``2>&1``, ``*>...``), or
``$ErrorActionPreference`` is SilentlyContinue at the end. Other errors can have
been caught intentionally, which the diagnostic states explicitly.
"""

from __future__ import annotations

import re

UTF8_OUTPUT_STATEMENT = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)"
# New threads, such as ForEach-Object -Parallel runspaces, take the default.
ENGLISH_MESSAGES_STATEMENT = (
    "[cultureinfo]::CurrentUICulture = 'en-US'; "
    "[cultureinfo]::DefaultThreadCurrentUICulture = 'en-US'"
)
ERROR_RECORD_BASELINE_STATEMENT = "$__vbotInitialError = $Error | Select-Object -First 1"
POWERSHELL_ERROR_NOTE = (
    "PowerShell recorded errors before exiting with code 0. Errors may have been handled; "
    "inspect the error output and verify any changed files before relying on this result."
)
# Enter the success branch before any bookkeeping changes $? or $LASTEXITCODE.
# Explicit exit statements bypass this epilogue, preserving the caller's control
# flow. Clearing $Error also suppresses it; no note is not proof of no errors.
# Get-Variable reads LASTEXITCODE even when it was never set under Set-StrictMode.
EXIT_STATUS_STATEMENT = (
    "if ($?) { & $__vbotReportErrors; exit 0 }; "
    "$__vbotExitCode = Get-Variable LASTEXITCODE -ValueOnly -ErrorAction Ignore; "
    "if ($__vbotExitCode) { exit $__vbotExitCode }; exit 1"
)

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
SETUP_STATEMENT = "; ".join(
    (
        UTF8_OUTPUT_STATEMENT,
        ENGLISH_MESSAGES_STATEMENT,
        UNIX_LINE_FILTERS_STATEMENT,
        ERROR_RECORD_BASELINE_STATEMENT,
    )
)
# Parse the command after the setup; a syntax error is thrown as PowerShell reports
# its own. An error record's position names the command that wrote it, so the
# command's syntax tree shows whether that command silenced it; a record without
# such a command, such as a caught throw, stays reportable. The exit status runs
# at the end of the last block: the unnamed body, a named end block, or an end
# block added after begin or process blocks.
RUN_STATEMENTS = r"""
$__vbotParseErrors = $null
$__vbotAst = [System.Management.Automation.Language.Parser]::ParseInput(
    $__vbotCommand, [ref]$null, [ref]$__vbotParseErrors)
if ($__vbotParseErrors) {
    throw [System.Management.Automation.ParseException]::new($__vbotParseErrors)
}
$__vbotSilenced = {
    param($record)
    if ($ErrorActionPreference -in 'SilentlyContinue', 'Ignore') { return $true }
    if ($record -isnot [System.Management.Automation.ErrorRecord]) { return $false }
    $info = $record.InvocationInfo
    if (-not $info -or $info.ScriptName) { return $false }
    $command = $__vbotAst.Find({
        param($node)
        $node -is [System.Management.Automation.Language.CommandAst] -and
        $node.Extent.StartLineNumber -eq $info.ScriptLineNumber -and
        $node.Extent.StartColumnNumber -eq $info.OffsetInLine -and
        $node.GetCommandName() -eq $info.InvocationName
    }, $true)
    for ($node = $command; $node; $node = $node.Parent) {
        if ($node -isnot [System.Management.Automation.Language.CommandAst]) { continue }
        foreach ($redirection in $node.Redirections) {
            if ("$($redirection.FromStream)" -in 'Error', 'All') { return $true }
        }
        $elements = $node.CommandElements
        for ($index = 1; $index -lt $elements.Count; $index++) {
            $parameter = $elements[$index]
            if ($parameter -isnot [System.Management.Automation.Language.CommandParameterAst]) {
                continue
            }
            # -ea, or a prefix of -ErrorAction long enough to exclude -ErrorVariable.
            $name = $parameter.ParameterName
            $prefix = $name.Length -ge 6 -and 'ErrorAction'.StartsWith($name, 'OrdinalIgnoreCase')
            if ($name -ne 'ea' -and -not $prefix) { continue }
            $value = if ($parameter.Argument) { $parameter.Argument }
                elseif ($index + 1 -lt $elements.Count) { $elements[$index + 1] }
            if ($value -is [System.Management.Automation.Language.ConstantExpressionAst] -and
                "$($value.Value)" -in 'SilentlyContinue', 'Ignore', '0', '4') {
                return $true
            }
        }
    }
    $false
}
$__vbotReportErrors = {
    $shown = foreach ($record in @($Error)) {
        if ([object]::ReferenceEquals($record, $__vbotInitialError)) { break }
        if (-not (& $__vbotSilenced $record)) { $record; break }
    }
    if ($null -eq $shown) { return }
    $summary = "$shown" -replace '\s+', ' '
    if ($summary.Length -gt 500) { $summary = $summary.Substring(0, 500) + '...' }
    [Console]::Error.WriteLine('{note} Latest error: ' + $summary)
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
. __vbotInvoke
"""
# The command runs from the first line. Write-Error names its caller's position,
# which PowerShell shows only beyond the first line, so no wrapper line appears in
# the command's errors. Dot-sourcing the function keeps the command's own scope.
INVOKE_STATEMENT = "function __vbotInvoke { . ([scriptblock]::Create($__vbotCommand)) }"
_QUOTE = re.compile("(['‘’‚‛])")


def powershell_command(command: str) -> str:
    """Return the ``pwsh -Command`` text that sets up output and runs ``command``."""
    run = RUN_STATEMENTS.replace("{note}", _escaped(POWERSHELL_ERROR_NOTE)).replace(
        "{exit_status}", _escaped(EXIT_STATUS_STATEMENT)
    )
    literal = _escaped(command)
    return f"{SETUP_STATEMENT}; {INVOKE_STATEMENT}\n$__vbotCommand = '{literal}'{run}"


def _escaped(text: str) -> str:
    """Return ``text`` escaped for a single-quoted PowerShell string."""
    # PowerShell also treats typographic single quotes as quotes; a doubled one is literal.
    return _QUOTE.sub(r"\1\1", text)


__all__ = [
    "EXIT_STATUS_STATEMENT",
    "POWERSHELL_ERROR_NOTE",
    "SETUP_STATEMENT",
    "UNIX_LINE_FILTERS_STATEMENT",
    "UTF8_OUTPUT_STATEMENT",
    "powershell_command",
]
