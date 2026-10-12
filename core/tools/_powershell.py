"""PowerShell's bootstrap for one command: English output and a faithful exit status.

The shell Tool runs every Windows command in its own ``pwsh -NoProfile
-NonInteractive -Command <bootstrap>`` on pipes, without a terminal:
``Read-Host``, ``Get-Credential`` and confirmation prompts fail at once instead
of waiting. The bootstrap is fixed text (``POWERSHELL_BOOTSTRAP``). It sets up
the console, then reads one request from stdin until the input ends
(``powershell_request``): the command, its workdir and the call's layer of the
environment. It applies the request and runs the command; the command itself
finds its input at its end. Because the request comes last, a shell can be
started before its command is known and serve it at once (``_command_launch``);
a shell serves one command and exits with it.

Request. ASCII lines, each field Base64 of its UTF-8 text: the workdir, ``1``
when the command can spell /dev/null (below) and ``0`` otherwise, the command,
one line per environment change (the name, then a space and the value; the name
alone removes the variable), and ``.`` as the last line, so a request that
ended early is never taken for a complete one. PowerShell hands stdin to the
command as ``$input`` lines, and its own reader holds the input: a direct read
of the console input stream races with it, ``$input`` does not. A trailing
empty line does not reach ``$input``, which the closing ``.`` makes harmless.

Workdir. The bootstrap sets PowerShell's location and the process's current
directory, which native programs and .NET methods use; a location it cannot
enter ends the shell with exit code 1 and the reason before the command runs.
A shell started in its workdir skips both. In a shell started ahead of its
command, ``cd -`` at the command's start returns to the directory where the
shell waited; a shell started in its workdir has no earlier location.

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

English output. PowerShell's own messages follow the Windows display language,
and the numbers and dates it formats and parses follow the user's regional format.
The setup switches the messages to en-US, so Agents and the failure hints read one
wording, and gives numbers a decimal point without grouping: a decimal comma
(``6,5``) is never read as a list or a thousands separator, and an Agent's
``[double]::Parse('3.14')`` reads 3.14 as PowerShell's own casts do, not 314. A
German decimal string such as ``1,5`` then fails to parse instead of reading as
15. Dates, day and month names and the list separator keep the user's format:
under en-US, ``Get-Date '11.10.2026'`` read the user's date as November 10.
Native programs keep the user's settings.

Parsing after the setup. The command arrives as data and is parsed after the
setup by PowerShell's own parser: ``using`` statements, ``#requires``, a
``param`` block and named blocks keep their meaning, a syntax error is reported
in English, and error positions are the command's own line numbers.

Bash's null device. PowerShell resolves ``/dev/null`` as a path on the current
drive: it writes the file ``C:\\dev\\null`` when ``C:\\dev`` exists, silently, and
fails otherwise; a native program given ``/dev/null`` opens the same file. Before
the command runs, the literal ``/dev/null`` or ``\\dev\\null`` (any case and
quoting) is therefore rewritten as a redirection target, such as ``2>/dev/null``
or ``*> '/dev/null'``, to ``$null``, and as a native program's argument, alone or
after ``=`` (``curl -o /dev/null``, ``git diff --output=/dev/null``), to ``NUL``,
Windows' null device. A program is native when its name resolves, before the
command runs, to an application. Programs built on MSYS2 or Cygwin (with
``msys-2.0.dll`` or ``cygwin1.dll`` beside them) keep /dev/null: they handle it
themselves and would create a file named NUL. So do shells and launchers that
hand their arguments to Linux: bash, sh, wsl, ssh, docker, podman and kubectl.
A cmdlet's argument, such as ``Out-File /dev/null``, keeps its PowerShell meaning.
The rewrite runs only for commands whose text can spell /dev/null.

Unix line filters. Agents habitually pipe into ``head``, ``tail`` and ``wc -l``,
which PowerShell lacks. When such a command is genuinely missing, a
command-not-found hook runs an equivalent built from Select-Object and
Get-Content for the line-count forms (``-n N``, ``-N``, ``tail -n +N``,
``wc -l``, pipeline or files); any other option stops the script with a message
naming the PowerShell equivalent. An installed ``head`` (for example from Git
for Windows) still runs itself.

Exit status. ``pwsh -Command`` exits with 1 whenever the last statement failed,
so a native program's own exit code is lost. A closing statement at the end of
the command's last block records that code instead, as ``bash -c`` reports it,
and otherwise 0 or 1 by the last statement's success.

Formatted output. Objects without a fixed-width view, such as Select-Object
results or Get-Location, are held by the table formatter until its pipeline
ends, and an ``exit`` before that discards them: ``Get-ChildItem | Select-Object
Name; exit 0`` prints nothing. The command's output is therefore piped through
Out-Default, and the bootstrap exits with the recorded code only after that
pipeline has ended.

Recorded errors. If the last statement succeeded after PowerShell recorded an
error, the exit status stays 0 but a bounded note is printed to the error
stream. It compares ErrorRecord identity, not output text or a count: printed
test failures are not errors, and PowerShell's bounded error collection can
already be full. An error the command's own text silenced does not count: its
command passes ``-ErrorAction SilentlyContinue`` or ``Ignore``, it or an
enclosing command redirects the error stream (``2>$null``, ``2>&1``,
``*>...``), or ``$ErrorActionPreference`` is SilentlyContinue at the end.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping

# Without a byte order mark: a console encoding with one would write it first.
UTF8_CONSOLE_STATEMENT = (
    "[Console]::OutputEncoding = [Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)"
)
# Messages in English; numbers with a decimal point and without grouping, in the
# user's regional format otherwise. New threads, such as ForEach-Object -Parallel
# runspaces, take the defaults.
CULTURE_STATEMENT = (
    "& { $culture = [cultureinfo]::CurrentCulture.Clone(); "
    "$numbers = $culture.NumberFormat; "
    "$numbers.NumberDecimalSeparator = $numbers.CurrencyDecimalSeparator = "
    "$numbers.PercentDecimalSeparator = '.'; "
    "$numbers.NumberGroupSeparator = $numbers.CurrencyGroupSeparator = "
    "$numbers.PercentGroupSeparator = ''; "
    "[cultureinfo]::CurrentCulture = [cultureinfo]::DefaultThreadCurrentCulture = $culture; "
    "[cultureinfo]::CurrentUICulture = [cultureinfo]::DefaultThreadCurrentUICulture = 'en-US' }"
)
# PowerShell's formatting and errors without ANSI escape sequences (PowerShell 7.2+).
PLAIN_TEXT_STATEMENT = "if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }"
# Formatted rows keep up to this many characters less one. A host whose buffer
# cannot be resized keeps its width; the setup's errors precede the error baseline.
OUTPUT_COLUMNS = 200
OUTPUT_WIDTH_STATEMENT = (
    f"& {{ try {{ $ui = $Host.UI.RawUI; if ($ui.BufferSize.Width -lt {OUTPUT_COLUMNS}) {{ "
    "$ui.BufferSize = [System.Management.Automation.Host.Size]::new("
    f"{OUTPUT_COLUMNS}, $ui.BufferSize.Height) }} }} catch {{}} }}"
)
ERROR_RECORD_BASELINE_STATEMENT = "$__vbotInitialError = if ($Error.Count) { $Error[0] }"
POWERSHELL_ERROR_NOTE = (
    "PowerShell recorded errors before exiting with code 0, which includes errors the "
    "command caught. Check the error output and verify any changed files before relying "
    "on this result."
)
# Read $? before any bookkeeping changes it or $LASTEXITCODE. The statement only
# records the code; the bootstrap exits after the command's output is formatted.
# Explicit exit statements bypass this epilogue, preserving the caller's control
# flow. Clearing $Error suppresses the error note; no note is not proof of no errors.
# Get-Variable reads LASTEXITCODE even when it was never set under Set-StrictMode.
EXIT_STATUS_STATEMENT = (
    "$global:__vbotExitCode = if ($?) { 0 } else { "
    "$__vbotLastCode = Get-Variable LASTEXITCODE -ValueOnly -ErrorAction Ignore; "
    "if ($__vbotLastCode) { $__vbotLastCode } else { 1 } }"
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
        UTF8_CONSOLE_STATEMENT,
        CULTURE_STATEMENT,
        PLAIN_TEXT_STATEMENT,
        OUTPUT_WIDTH_STATEMENT,
        UNIX_LINE_FILTERS_STATEMENT,
    )
)
# A redirection to /dev/null becomes one to $null and a native program's /dev/null
# argument becomes NUL, each padded to the same length, so every offset, line and
# column stays as written and the syntax tree keeps describing the text that runs.
# The walk costs about 20 ms of every call, so it runs only when the command can
# spell /dev/null (_may_spell_null_device); it changes nothing else.
NULL_DEVICE_STATEMENTS = r"""$__vbotCommand = & {
    $text = [System.Text.StringBuilder]::new($__vbotCommand)
    $put = {
        param($start, $end, $value)
        $null = $text.Remove($start, $end - $start).Insert($start, $value.PadRight($end - $start))
    }
    $constant = [System.Management.Automation.Language.StringConstantExpressionAst]
    $nullPath = {
        param($node)
        $node -is $constant -and $node.Value -in '/dev/null', '\dev\null' -and
        $node.Extent.StartLineNumber -eq $node.Extent.EndLineNumber
    }
    $posix = 'bash', 'sh', 'wsl', 'ssh', 'docker', 'podman', 'kubectl'
    foreach ($node in $__vbotAst.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.FileRedirectionAst] -or
        $node -is [System.Management.Automation.Language.CommandAst]
    }, $true)) {
        if ($node -isnot [System.Management.Automation.Language.CommandAst]) {
            $extent = $node.Location.Extent
            if (& $nullPath $node.Location) { & $put $extent.StartOffset $extent.EndOffset '$null' }
            continue
        }
        $elements = $node.CommandElements
        $arguments = @(for ($index = 1; $index -lt $elements.Count; $index++) {
            $element = $elements[$index]
            if ($element -is [System.Management.Automation.Language.CommandParameterAst]) {
                $element = $element.Argument
                if (-not $element) { continue }
            }
            $extent = $element.Extent
            if (& $nullPath $element) {
                , @($extent.StartOffset, $extent.EndOffset)
            } elseif ($element -is $constant -and
                "$($element.StringConstantType)" -eq 'BareWord' -and
                $extent.Text -match '=([''"]?)(/dev/null|\\dev\\null)\1$') {
                , @(($extent.EndOffset - $Matches[0].Length + 1), $extent.EndOffset)
            }
        })
        $name = $node.GetCommandName()
        if (-not $arguments.Count -or -not $name) { continue }
        $program = try { $ExecutionContext.InvokeCommand.GetCommand($name, 'All') } catch { $null }
        if ($program -isnot [System.Management.Automation.ApplicationInfo]) { continue }
        $folder = [System.IO.Path]::GetDirectoryName($program.Path)
        if ([System.IO.Path]::GetFileNameWithoutExtension($program.Path) -in $posix -or
            [System.IO.File]::Exists("$folder\msys-2.0.dll") -or
            [System.IO.File]::Exists("$folder\cygwin1.dll")) {
            continue
        }
        foreach ($argument in $arguments) { & $put $argument[0] $argument[1] 'NUL' }
    }
    $text.ToString()
}
"""
NULL_DEVICE_STATEMENTS = re.sub(r"(?m)^[ \t]+", "", NULL_DEVICE_STATEMENTS)
REQUEST_UNREADABLE_NOTE = (
    "The command did not run: vBot could not read it from the shell's input ({reason}). "
    "Run the command again; if it fails the same way, tell the user."
)
WORKDIR_UNAVAILABLE_NOTE = (
    "The command did not run: vBot could not enter {workdir}: {reason} If the command can "
    "run in another directory, pass that directory as workdir; otherwise tell the user."
)
# How recorded errors are judged and reported, defined before the request arrives.
# An error record's position names the command that wrote it, so the command's
# syntax tree shows whether that command silenced it; a record without such a
# command, such as a caught throw, stays reportable.
REPORT_STATEMENTS = r"""$__vbotSilenced = {
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
"""
REPORT_STATEMENTS = re.sub(r"(?m)^[ \t]+", "", REPORT_STATEMENTS)
# A shell started ahead of its command waits in Windows' system directory
# (``_command_launch``). While it waits it pays the first use of what follows:
# type lookups, the parser, PowerShell's utility and file cmdlets, setting the
# location, formatting, an empty command through Out-Default. That takes about
# 40 ms from each warm command; a shell started for its command skips it, since
# the request is already there. None of it writes output or leaves a variable
# but these; an error it records precedes the baseline.
WARM_UP_STATEMENTS = r"""$__vbotUtf8 = [System.Text.Encoding]::UTF8
if ([Environment]::CurrentDirectory -eq [Environment]::SystemDirectory) {
    try {
        $null = [Convert]::FromBase64String('LA=='), [NullString]::Value
        $null = [System.Management.Automation.Language.Parser]::ParseInput(
            '"$a" | % { $_ }', [ref]$null, [ref]$null)
        Import-Module -ErrorAction Stop -Name (
            'Microsoft.PowerShell.Utility', 'Microsoft.PowerShell.Management')
        Set-Location -LiteralPath $PWD.ProviderPath -ErrorAction Stop
        $null = [pscustomobject]@{ Name = 1 } | Out-String
        $__vbotCommand = ''
        . __vbotInvoke | Out-Default
    } catch {}
}
"""
WARM_UP_STATEMENTS = re.sub(r"(?m)^[ \t]+", "", WARM_UP_STATEMENTS)
# Wait for the request, then apply it: the environment layer, then the location of
# PowerShell and of the process, unless the shell started there. [NullString]::Value
# removes a variable, since PowerShell passes $null to a string parameter as ''.
REQUEST_STATEMENTS = r"""$__vbotRequest = @($input)
try {
    if ($__vbotRequest.Count -lt 4 -or $__vbotRequest[-1] -ne '.') {
        throw 'it ended early'
    }
    $__vbotWorkdir = $__vbotUtf8.GetString([Convert]::FromBase64String($__vbotRequest[0]))
    $__vbotRewrite = $__vbotRequest[1] -eq '1'
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
    [Console]::Error.WriteLine({unreadable})
    exit 1
}
if ($PWD.ProviderPath -ne $__vbotWorkdir) {
    try {
        Set-Location -LiteralPath $__vbotWorkdir -ErrorAction Stop
        [Environment]::CurrentDirectory = $__vbotWorkdir
    } catch {
        [Console]::Error.WriteLine({unavailable})
        exit 1
    }
}
"""
REQUEST_STATEMENTS = re.sub(r"(?m)^[ \t]+", "", REQUEST_STATEMENTS)
# Parse the command after the setup; a syntax error is thrown as PowerShell reports
# its own. The /dev/null rewrite follows when the command can spell /dev/null. The
# exit status runs at the end of the last block: the unnamed body, a named end
# block, or an end block added after begin or process blocks.
RUN_STATEMENTS = r"""$__vbotParseErrors = $null
$__vbotAst = [System.Management.Automation.Language.Parser]::ParseInput(
    $__vbotCommand, [ref]$null, [ref]$__vbotParseErrors)
if ($__vbotParseErrors) {
    throw [System.Management.Automation.ParseException]::new($__vbotParseErrors)
}
{null_device}$__vbotEnd = $__vbotAst.EndBlock
$__vbotExitStatus = '{exit_status}'
$__vbotCommand = if ($__vbotEnd -and -not $__vbotEnd.Unnamed) {
    $__vbotCommand.Insert($__vbotEnd.Extent.EndOffset - 1, "`n`n$__vbotExitStatus`n")
} elseif ($__vbotAst.BeginBlock -or $__vbotAst.ProcessBlock -or $__vbotAst.DynamicParamBlock) {
    "$__vbotCommand`nend {`n$__vbotExitStatus`n}"
} else {
    "$__vbotCommand`n`n$__vbotExitStatus"
}
$global:__vbotExitCode = 0
. __vbotInvoke | Out-Default
if ($global:__vbotExitCode -eq 0) { & $__vbotReportErrors }
exit $global:__vbotExitCode
"""
RUN_STATEMENTS = re.sub(r"(?m)^[ \t]+", "", RUN_STATEMENTS)
# The command runs from the first line. Write-Error names its caller's position,
# which PowerShell shows only beyond the first line, so no bootstrap line appears in
# the command's errors. Dot-sourcing the function keeps the command's own scope.
INVOKE_STATEMENT = "function __vbotInvoke { . ([scriptblock]::Create($__vbotCommand)) }"
_QUOTE = re.compile("(['‘’‚‛])")
# Printable ASCII and whitespace: text PowerShell compares letter by letter.
_PLAIN_ASCII = re.compile(r"[\t\n\v\f\r -~]*")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def powershell_literal(text: str) -> str:
    """Return *text* as a single-quoted PowerShell string that keeps it as data."""
    return f"'{_escaped(text)}'"


def _escaped(text: str) -> str:
    """Return *text* escaped for a single-quoted PowerShell string."""
    # PowerShell also treats typographic single quotes as quotes; a doubled one is literal.
    return _QUOTE.sub(r"\1\1", text)


def _joined(template: str, **expressions: str) -> str:
    """A PowerShell expression: *template*'s text with each ``{name}`` as its expression."""
    parts = _PLACEHOLDER.split(template)
    return " + ".join(
        expressions[part] if index % 2 else f"'{_escaped(part)}'"
        for index, part in enumerate(parts)
        if part or index % 2
    )


# The setup and the invoking function share the first line. Everything before
# the request runs before the command is known; the error baseline follows it.
POWERSHELL_BOOTSTRAP = (
    f"{SETUP_STATEMENT}; {INVOKE_STATEMENT}\n"
    + REPORT_STATEMENTS.replace("{note}", _escaped(POWERSHELL_ERROR_NOTE))
    + WARM_UP_STATEMENTS
    + REQUEST_STATEMENTS.replace(
        "{unreadable}", _joined(REQUEST_UNREADABLE_NOTE, reason="$_.Exception.Message")
    ).replace(
        "{unavailable}",
        _joined(
            WORKDIR_UNAVAILABLE_NOTE,
            workdir="$__vbotWorkdir.Replace('\\', '/')",
            reason="$_.Exception.Message",
        ),
    )
    + f"{ERROR_RECORD_BASELINE_STATEMENT}\n"
    + RUN_STATEMENTS.replace(
        "{null_device}", f"if ($__vbotRewrite) {{\n{NULL_DEVICE_STATEMENTS}}}\n"
    ).replace("{exit_status}", _escaped(EXIT_STATUS_STATEMENT))
)


def powershell_request(command: str, workdir: str, changes: Mapping[str, str | None]) -> bytes:
    """Return the request ``POWERSHELL_BOOTSTRAP`` reads: run *command* in *workdir*.

    *changes* is the call's layer of the environment over the shell's start
    environment: a value sets the variable, None removes it.
    """
    lines = [_base64(workdir), "1" if _may_spell_null_device(command) else "0", _base64(command)]
    for name, value in changes.items():
        lines.append(_base64(name) if value is None else f"{_base64(name)} {_base64(value)}")
    lines.append(".")
    return "\n".join(lines).encode("ascii") + b"\n"


def _base64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8", "surrogatepass")).decode("ascii")


def _may_spell_null_device(command: str) -> bool:
    """Return whether a string in *command* can have the value /dev/null or \\dev\\null.

    Without the letters themselves, a backtick escape (``nu`ll``, ``"`u{6c}"``)
    spells them, and PowerShell's culture-aware comparison takes non-ASCII text
    and ASCII control characters other than whitespace for ignorable characters
    or other spellings: it reads ``nu<U+00AD>ll`` and ``nu<U+0001>ll`` as ``null``.
    """
    return "`" in command or not _PLAIN_ASCII.fullmatch(command) or "null" in command.lower()


__all__ = [
    "EXIT_STATUS_STATEMENT",
    "NULL_DEVICE_STATEMENTS",
    "OUTPUT_COLUMNS",
    "OUTPUT_WIDTH_STATEMENT",
    "PLAIN_TEXT_STATEMENT",
    "POWERSHELL_BOOTSTRAP",
    "POWERSHELL_ERROR_NOTE",
    "REQUEST_UNREADABLE_NOTE",
    "SETUP_STATEMENT",
    "UNIX_LINE_FILTERS_STATEMENT",
    "UTF8_CONSOLE_STATEMENT",
    "WORKDIR_UNAVAILABLE_NOTE",
    "powershell_literal",
    "powershell_request",
]
