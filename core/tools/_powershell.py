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
cannot enter with the code after it, and an initially restricted language mode
with the second code after it, before the command runs and without output;
the shell Tool reports these as a failure of the call, not as the
command's result (``CommandReport.not_run``). The command cannot know the code:
it travels only in the request, which the bootstrap has read from stdin before
the command runs, and in bootstrap variables removed by then. A coincidental
match is possible: the base is drawn from almost 900 million values. A fixed
code was no proof: a command that
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
own shell; native programs keep the user's settings. ``powershell_culture``
says which format that is, for the shell Tool's description.

Parsing after the setup. The command arrives as data and is parsed after the
setup by PowerShell's own parser: ``using`` statements, ``#requires``, a
``param`` block and named blocks keep their meaning, a syntax error is reported
as PowerShell's own ParserError in English, and nothing runs. Incomplete input,
such as a trailing backtick or an unclosed string, is reported the same way,
where ``pwsh -Command`` exits with 1 without a word.

Running. The command's text runs unchanged as its own script in a fresh
runspace, in its global scope, as in ``pwsh -Command``:
``$MyInvocation``, error positions and ``Set-PSDebug`` traces show the command's
own lines and no line of the bootstrap. Its host delegates the console's UI
and native console state to the original ConsoleHost. A nested prompt evaluates
the command's prompt once and returns at the input's known EOF. The
runner's separate runspace stays able to report the result when the command
changes its own language mode; an initially restricted mode runs no command.

Exit status. ``pwsh -Command`` exits with 1 whenever the last statement failed,
so a native program's own exit code is lost. The success the command's last
statement left (``$?``) gives 0 or 1, and a native program's or a script's exit
code takes its place when that
program decided it: it ran as the command's last statement, or as the last
statement of the function, script block or script the last statement called,
as bash reports a function's status. vBot judges what ran, not the text: a
program that never ran changes nothing, and an error recorded after the program
leaves the status to ``$?``. A program's own NativeCommandExitException is part
of that exit, not an unrelated failure. Where the runtime cannot show the
relationship, vBot keeps plain pwsh's status: for example a ``switch`` clause,
``ForEach-Object``'s script block, ``Invoke-Expression``, a loop the program does
not end, or an already resolved ExternalScriptInfo invoked without command
lookup. An ``exit`` in the command and ``$Host.SetShouldExit`` retain their
codes. PowerShell ignores ``exit`` inside ``clean``; a clean block with no
executed status-changing statement keeps the preceding status.

Output. The command's output and its errors go to PowerShell's Out-Default as
one pipeline (``2>&1``), as ``pwsh -Command`` merges them, so an error shows
between the rows the formatter printed before it and those after it. Native
programs' stderr is not captured by that merge and passes as bytes. A clean
block runs after Out-Default ended, so its output and errors are dropped, as in
plain pwsh. As in ``pwsh -Command``, an ``exit`` in the command discards
objects the table formatter still holds.

Like a new shell. The command sees none of the bootstrap's variables or
functions, and ``$Error`` is empty when it starts. Its call stack contains only
its own frames. What still shows the runner: ``Get-Runspace`` lists the outer
runner's runspace, the host delegates to that runspace, transcript headers name
the process's bootstrap, ``$MyInvocation.PipelineLength`` counts the reader
after Out-Default (and an empty input stage for scripts that reference $input),
``$ExecutionContext.InvokeCommand.PostCommandLookupAction`` holds its hook, and
``$input.Reset()`` succeeds where plain pwsh's console input refuses it.
"""

from __future__ import annotations

import base64
import functools
import re
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

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
# The outer ConsoleHost remains the owner of its UI and console modes. The host
# delegates those operations while observing native starts/completions and exit
# requests. Nested prompts evaluate in the command's runspace and return at EOF;
# the original host's input loop would wait on the outer runspace and deadlock.
HOST_STATEMENT = r"""Add-Type @"
using System;
using System.Collections;
using System.Globalization;
using System.Linq;
using System.Management.Automation;
using System.Management.Automation.Host;
using System.Management.Automation.Runspaces;
public sealed class VBotPowerShellHost : PSHost, IHostSupportsInteractiveSession {
    readonly PSHost inner;
    bool promptInputEnded;
    public int? ExitCode;
    public PSVariableIntrinsics Variables;
    public Runspace CommandRunspace;
    public IList Errors;
    public object[] Record;
    public object NativeExit;
    public VBotPowerShellHost(PSHost inner) { this.inner = inner; }
    public override string Name => inner.Name;
    public override Version Version => inner.Version;
    public override Guid InstanceId => inner.InstanceId;
    public override PSHostUserInterface UI => inner.UI;
    public override CultureInfo CurrentCulture => inner.CurrentCulture;
    public override CultureInfo CurrentUICulture => inner.CurrentUICulture;
    public override PSObject PrivateData => inner.PrivateData;
    public override void SetShouldExit(int code) {
        ExitCode = code;
        inner.SetShouldExit(code);
    }
    public override void EnterNestedPrompt() {
        // The request consumed stdin to EOF. ConsoleHost would evaluate one
        // prompt and return. Its own runspace is waiting for this one, so the
        // prompt must run here; delegating its input loop would deadlock.
        if (ExitCode.HasValue || promptInputEnded) return;
        if (UI.RawUI.CursorPosition.X != 0) UI.WriteLine();
        string prompt = null;
        try {
            using (var pipeline = CommandRunspace.CreateNestedPipeline("prompt", false)) {
                var output = pipeline.Invoke();
                if (output.Count != 0) prompt = output[0]?.BaseObject?.ToString();
            }
        }
        catch { } // ConsoleHost falls back when prompt evaluation fails.
        UI.Write(string.IsNullOrEmpty(prompt) ? "PS>" : prompt);
        promptInputEnded = true;
    }
    public override void ExitNestedPrompt() => inner.ExitNestedPrompt();
    public override void NotifyBeginApplication() {
        inner.NotifyBeginApplication();
        RecordCommand();
    }
    public override void NotifyEndApplication() {
        try { NativeExit = Variables.GetValue("global:LASTEXITCODE"); }
        catch { Record = null; NativeExit = null; }
        finally { inner.NotifyEndApplication(); }
    }
    public void RecordCommand() {
        try {
            var frames = CommandRunspace.Debugger.GetCallStack().ToArray();
            Record = frames.Length == 0 ? null : new object[] { frames, frames[0].Position,
                Variables.GetValue("global:LASTEXITCODE"), Errors.Count == 0 ? null : Errors[0] };
        }
        catch { Record = null; NativeExit = null; }
    }
    public EventHandler<CommandLookupEventArgs> LookupHook => OnCommandLookup;
    void OnCommandLookup(object sender, CommandLookupEventArgs lookup) {
        try {
            var command = lookup.Command;
            if (command is AliasInfo alias) command = alias.ResolvedCommand;
            if (command is ExternalScriptInfo) RecordCommand();
        }
        catch { Record = null; NativeExit = null; }
    }
    public bool IsRunspacePushed => ((IHostSupportsInteractiveSession)inner).IsRunspacePushed;
    public Runspace Runspace => ((IHostSupportsInteractiveSession)inner).Runspace;
    public void PushRunspace(Runspace runspace) =>
        ((IHostSupportsInteractiveSession)inner).PushRunspace(runspace);
    public void PopRunspace() => ((IHostSupportsInteractiveSession)inner).PopRunspace();
}
"@
"""
# The runner. The command runs unchanged as its own script (AddScript without a
# local scope) in a fresh runspace, followed by Out-Default and a status reader.
# The original ConsoleHost is delegated through the public PSHost interface;
# the outer runspace owns the runner and can still finish after the command
# changes its language mode. InitialSessionState supplies the normal defaults,
# with ConsoleHost's PROFILE copied explicitly. Runspace.Open happens after the
# request applies the environment and workdir. The pipeline exposes control-flow
# exceptions; a terminating error is formatted in the command's own runspace.
#
# The exit code follows the contract in the module docstring. It starts from the
# success the command's last statement left ($?), which the reader records at the
# end of the pipeline, or in its clean block when the command's own clean block
# ran statements after its end block. A native program's or a script's code
# replaces 1, or 0 for a call of a function, script block or script, only when
# the runtime shows that it decided that status. The host observes native starts
# and completions directly, including CommandInfo and debugger-hidden calls.
# A command-lookup hook covers external scripts and their aliases. Both capture
# the call stack, $LASTEXITCODE and newest $Error entry before execution; native
# completion records the resulting code. The code counts when execution changed
# $LASTEXITCODE, no unrelated error followed, and its statement ends every frame up to
# the command's last statement. $walk checks the frames from the innermost one
# out, and $climb checks one statement's way up to its frame's root: no later
# statement in its block except a bare return or an exit, no later named block
# that runs statements, no finally block after it, and no loop it does not end on
# every pass. A program in the command's own last statement skips the walk.
RUNNER_KEY = "vBot.PowerShell"
RUNNER_STATEMENTS = r"""& {
    $state = @{ Host = [VBotPowerShellHost]::new($Host); Profile = $PROFILE }
    $state.Fail = Get-Command Microsoft.PowerShell.Utility\Write-Error -CommandType Cmdlet
    $state.OutDefault = Get-Command Microsoft.PowerShell.Core\Out-Default -CommandType Cmdlet
    $state.Settings = [System.Management.Automation.PSInvocationSettings]@{
        ExposeFlowControlExceptions = $true
    }
    $reader = '[System.Diagnostics.DebuggerHidden()] param($state) ' +
        'begin { $PSDefaultParameterValues = @{} } ' +
        # Recording $? changes it. Restore a failure without output or an $Error
        # entry so the command's clean block observes the original status. The
        # bound cmdlet cannot be replaced by a user function or disabled autoload.
        # Only its local scope clears defaults, preventing helper side effects.
        'end { $state.Ended = $?; if (-not $state.Ended) { & $state.Fail "" -ErrorAction Ignore } }'
    if ([enum]::IsDefined([System.Management.Automation.Language.TokenKind], 'Clean')) {
        $reader += ' clean { $state.Cleaned = $?; $state.CleanRan = $true; ' +
            '$state.CleanExit = $state.Variables.GetValue(''global:LASTEXITCODE'') }'
        $state.ReaderCleans = $true
    }
    $state.Prepare = {
        param($text)
        foreach ($name in 'Ended', 'Cleaned', 'CleanExit', 'CleanRan') {
            $state[$name] = $null
        }
        if ($state.Pipeline) { $state.Pipeline.Dispose(); $state.Runspace.Dispose() }
        $state.Host.ExitCode = $null
        $state.Host.Record = $null
        $state.Host.NativeExit = $null
        $initial = [System.Management.Automation.Runspaces.InitialSessionState]::CreateDefault2()
        $initial.LanguageMode = $ExecutionContext.SessionState.LanguageMode
        $state.Runspace = [runspacefactory]::CreateRunspace($state.Host, $initial)
        $state.Runspace.Open()
        $state.Host.CommandRunspace = $state.Runspace
        $state.Variables = $state.Runspace.SessionStateProxy.PSVariable
        $state.Variables.Set('PROFILE', $state.Profile)
        $state.Errors = $state.Variables.GetValue('Error')
        $state.Host.Variables = $state.Variables
        $state.Host.Errors = $state.Errors
        $state.Pipeline = [PowerShell]::Create()
        $state.Pipeline.Runspace = $state.Runspace
        # ConsoleHost supplies an empty input stream only when the outer script
        # refers to $input. An empty upstream stage gives the command pipeline
        # the same distinction between no input and a standalone invocation.
        $ast = [System.Management.Automation.Language.Parser]::ParseInput(
            $text, [ref]$null, [ref]$null)
        $usesInput = $ast.Find({
            param($node)
            $node -is [System.Management.Automation.Language.VariableExpressionAst] -and
                $node.VariablePath.UserPath.Split(':')[-1] -eq 'input'
        }, $false)
        if ($usesInput) {
            $null = $state.Pipeline.AddScript(
                '[System.Diagnostics.DebuggerHidden()] param() begin {}', $true)
        }
        $null = $state.Pipeline.AddScript($text, $false)
        $commands = $state.Pipeline.Commands.Commands
        $commands[$commands.Count - 1].MergeMyResults('Error', 'Output')
        $null = $state.Pipeline.AddCommand($state.OutDefault)
        $null = $state.Pipeline.AddScript($reader, $true).AddArgument($state)
        $state.Runspace.SessionStateProxy.InvokeCommand.PostCommandLookupAction =
            $state.Host.LookupHook
    }.GetNewClosure()
    $climb = {
        [System.Diagnostics.DebuggerHidden()] param($node, $root)
        $leaves = {
            [System.Diagnostics.DebuggerHidden()] param($ast)
            $ast -is [System.Management.Automation.Language.BreakStatementAst] -or
                $ast -is [System.Management.Automation.Language.ContinueStatementAst] -or
                $ast -is [System.Management.Automation.Language.ReturnStatementAst] -or
                $ast -is [System.Management.Automation.Language.ExitStatementAst] -or
                $ast -is [System.Management.Automation.Language.ThrowStatementAst]
        }
        $conditional = $false
        while (-not [object]::ReferenceEquals($node.Parent, $root)) {
            $parent = $node.Parent
            if ($parent -is [System.Management.Automation.Language.StatementBlockAst] -or
                $parent -is [System.Management.Automation.Language.NamedBlockAst]) {
                $after = $false
                foreach ($statement in $parent.Statements) {
                    if ([object]::ReferenceEquals($statement, $node)) { $after = $true }
                    elseif ($after) {
                        $type = $statement.GetType().Name
                        return $type -eq 'ExitStatementAst' -or
                            ($type -eq 'ReturnStatementAst' -and -not $statement.Pipeline)
                    } elseif ($statement.Find($leaves, $false)) { $conditional = $true }
                }
                if ($parent -is [System.Management.Automation.Language.NamedBlockAst]) {
                    $kind = "$($parent.BlockKind)"
                    $blocks = $parent.Parent
                    $clean = $blocks.PSObject.Properties['CleanBlock']
                    $later = @(if ($clean) { $clean.Value })
                    if ($kind -eq 'Begin') { $later += $blocks.ProcessBlock, $blocks.EndBlock }
                    elseif ($kind -eq 'Process' -and -not $conditional) {
                        $later += $blocks.EndBlock
                    }
                    elseif ($kind -eq 'Clean') { $later = @() }
                    elseif ($kind -ne 'End') { return $false }
                    foreach ($block in $later) {
                        if ($block -and $block.Statements.Count) { return $false }
                    }
                }
            } elseif ($parent -is [System.Management.Automation.Language.IfStatementAst]) {
                if ($node -isnot [System.Management.Automation.Language.StatementBlockAst]) {
                    return $false
                }
                $conditional = $true
            } elseif ($parent -is [System.Management.Automation.Language.LoopStatementAst]) {
                if ($conditional -or -not [object]::ReferenceEquals($node, $parent.Body)) {
                    return $false
                }
                $conditional = $true
            } elseif ($parent -is [System.Management.Automation.Language.TryStatementAst]) {
                if (-not [object]::ReferenceEquals($node, $parent.Finally) -and $parent.Finally -and
                    $parent.Finally.Statements.Count) { return $false }
            } elseif ($parent -is [System.Management.Automation.Language.CatchClauseAst]) {
                $conditional = $true
            } elseif ($parent -is [System.Management.Automation.Language.AssignmentStatementAst]) {
                if (-not [object]::ReferenceEquals($node, $parent.Right)) { return $false }
            } elseif ($parent -is [System.Management.Automation.Language.ParenExpressionAst] -or
                $parent -is [System.Management.Automation.Language.ArrayExpressionAst] -or
                $parent -is [System.Management.Automation.Language.SubExpressionAst] -or
                $parent -is [System.Management.Automation.Language.CommandExpressionAst] -or
                $parent -is [System.Management.Automation.Language.PipelineAst] -or
                $parent -is [System.Management.Automation.Language.ReturnStatementAst]) {
                # A returned expression executes unchanged; its native status
                # survives the return just as its output does.
            } elseif ($parent -is [System.Management.Automation.Language.PipelineChainAst]) {
                if ([object]::ReferenceEquals($node, $parent.LhsPipelineChain) -and
                    "$($parent.Operator)" -ne 'AndAnd') { return $false }
            } else { return $false }
            $node = $parent
        }
        $true
    }
    $walk = {
        [System.Diagnostics.DebuggerHidden()] param($frames, $position)
        $last = $frames.Count - 1
        for ($index = 0; $index -le $last; $index++) {
            $invocation = $frames[$index].InvocationInfo
            $root = $invocation.MyCommand.ScriptBlock.Ast
            if ($root -is [System.Management.Automation.Language.FunctionDefinitionAst]) {
                $root = $root.Body
            }
            if ($root -isnot [System.Management.Automation.Language.ScriptBlockAst]) { return -1 }
            if ($index -eq 0) {
                $node = $null
                foreach ($node in $root.FindAll({
                    [System.Diagnostics.DebuggerHidden()] param($ast)
                    $ast.Extent.StartOffset -le $position.StartOffset -and
                        $ast.Extent.EndOffset -ge $position.EndOffset -and
                        $ast -is [System.Management.Automation.Language.StatementAst] -and
                        $ast -isnot [System.Management.Automation.Language.CommandBaseAst]
                }, $true)) {}
            } else {
                $call = $frames[$index - 1].InvocationInfo
                $node = $root.Find({
                    [System.Diagnostics.DebuggerHidden()] param($ast)
                    $ast.Extent.StartLineNumber -eq $call.ScriptLineNumber -and
                        $ast.Extent.StartColumnNumber -eq $call.OffsetInLine -and
                        $ast -is [System.Management.Automation.Language.CommandAst]
                }, $true)
                if ($node) { $node = $node.Parent }
            }
            if (-not $node -or -not (& $climb $node $root)) { return -1 }
            if ($index -lt $last) {
                $kind = $invocation.MyCommand
                if (-not ($kind -is [System.Management.Automation.FunctionInfo] -or
                    $kind -is [System.Management.Automation.ExternalScriptInfo] -or
                    $kind -is [System.Management.Automation.ScriptInfo]) -or
                    $invocation.PipelinePosition -ne $invocation.PipelineLength) { return -1 }
            }
        }
        $last
    }.GetNewClosure()
    $decide = {
        [System.Diagnostics.DebuggerHidden()] param($caught)
        $lastExit = $state.Variables.GetValue('global:LASTEXITCODE')
        if ($caught) {
            $exception = $caught.Exception
            if ($exception -is [System.Management.Automation.MethodInvocationException] -and
                $exception.InnerException) { $exception = $exception.InnerException }
            if ($exception -isnot [System.Management.Automation.LoopFlowException]) {
                return 1
            }
            return $(if ($state.Cleaned -eq $false) { 1 } else { 0 })
        }
        if ($null -eq $state.Ended -or
            ($state.CleanRan -and -not [object]::ReferenceEquals($lastExit, $state.CleanExit))) {
            return $(if ($lastExit -is [int]) { $lastExit } else { 0 })
        }
        # An error in the command's clean block ends the pipeline before the reader's.
        if ($state.ReaderCleans -and -not $state.CleanRan) { return 1 }
        $code = 1
        try {
            $succeeded = $state.Ended
            if ($state.CleanRan) { $succeeded = $state.Cleaned }
            if ($succeeded) { $code = 0 }
            $record = $state.Host.Record
            if ($record -and $lastExit -is [int] -and $lastExit -ne 0 -and
                -not [object]::ReferenceEquals($lastExit, $record[2]) -and
                -not ($state.Errors.Count -and
                    -not [object]::ReferenceEquals($state.Errors[0], $record[3]) -and
                    -not ($state.Errors[0].Exception -is
                        [System.Management.Automation.NativeCommandExitException] -and
                        $state.Errors[0].Exception.ExitCode -eq $state.Host.NativeExit))) {
                $frames = $record[0]
                $position = $record[1]
                $depth = $null
                if ($frames.Count -eq 1) {
                    $root = $frames[0].InvocationInfo.MyCommand.ScriptBlock.Ast
                    $statements = $root.EndBlock.Statements
                    $statement = if ($statements.Count) { $statements[$statements.Count - 1] }
                    $clean = $root.PSObject.Properties['CleanBlock']
                    if ($statement -is [System.Management.Automation.Language.PipelineAst] -and
                        -not ($clean -and $clean.Value)) {
                        foreach ($ast in @($statement) + $statement.PipelineElements) {
                            if ($ast.Extent.StartOffset -eq $position.StartOffset -and
                                $ast.Extent.EndOffset -eq $position.EndOffset) { $depth = 0 }
                        }
                    }
                }
                if ($null -eq $depth) { $depth = & $walk $frames $position }
                if ($depth -ge 1 -or ($depth -eq 0 -and -not $succeeded)) { $code = $lastExit }
            }
        } catch {}
        $code
    }.GetNewClosure()
    $state.Finish = {
        [System.Diagnostics.DebuggerHidden()] param($caught)
        if ($caught) {
            $exception = $caught.Exception
            if ($exception -is [System.Management.Automation.MethodInvocationException] -and
                $exception.InnerException) { $exception = $exception.InnerException }
            if ($exception -is [System.Management.Automation.RuntimeException]) {
                # A terminating error escapes Invoke. Format its original record
                # in the command's runspace, with its ErrorView and type data.
                # ConsoleHost resolves Out-Default again for such an error, so a
                # user override must apply here too (unlike the initial pipeline).
                $formatter = [PowerShell]::Create()
                $formatter.Runspace = $state.Runspace
                $null = $formatter.AddCommand('Out-Default')
                $null = $formatter.Invoke(@($exception.ErrorRecord))
                $formatter.Dispose()
            }
        }
        if ($null -ne $state.Host.ExitCode) {
            exit $state.Host.ExitCode
        }
        exit (& $decide $caught)
    }.GetNewClosure()
    $state.WarmUp = {
        [System.Diagnostics.DebuggerHidden()] param()
        $record = $state.Host.Record
        if ($record) { $null = & $walk $record[0] $record[1] }
        $null = & $decide $null
    }.GetNewClosure()
    $state.Run = {
        [System.Diagnostics.DebuggerHidden()] param()
        try {
            $null = [AppDomain]::CurrentDomain.GetData('{key}').Pipeline.Invoke(
                $null, [AppDomain]::CurrentDomain.GetData('{key}').Settings)
        } catch {
            & ([AppDomain]::CurrentDomain.GetData('{key}').Finish) $_
        }
        & ([AppDomain]::CurrentDomain.GetData('{key}').Finish) $null
    }
    [AppDomain]::CurrentDomain.SetData('{key}', $state)
}
"""
RUNNER_STATEMENTS = re.sub(r"(?m)^[ \t]+", "", RUNNER_STATEMENTS).replace("{key}", RUNNER_KEY)
SETUP_STATEMENT = "; ".join(
    (UTF8_CONSOLE_STATEMENT, UI_CULTURE_STATEMENT, PLAIN_TEXT_STATEMENT, OUTPUT_WIDTH_STATEMENT)
)
# The bootstrap's exit code for a request too short to carry its not-run code:
# "VB" in the high bytes. A command can exit with it too, so it proves nothing.
REQUEST_UNREADABLE_EXIT_CODE = 0x56420001
# A request's not-run code and the two codes after it have nine decimal digits.
_NOT_RUN_CODE_FIRST = 100_000_000
_NOT_RUN_CODES = 899_999_998
LANGUAGE_MODE_STATEMENT = r"""if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    $__vbotRequest = @($input)
    if ($__vbotRequest.Count -and $__vbotRequest[0] -match '^[0-9]{9}$') {
        exit ([int]$__vbotRequest[0] + 2)
    }
    exit {unreadable}
}
"""
# A shell started ahead of its command waits in Windows' system directory
# (``_command_launch``). While it waits it pays the first use of what follows:
# type lookups, the parser, PowerShell's utility and file cmdlets, formatting, the
# runner with an empty command and its walk through a call stack, removing
# variables, and moving back and forth, which leaves the location history as the
# shell started. A shell started for its command skips it, since the request is
# already there. None of it writes output or leaves a variable; an error it
# records is cleared before the command runs.
WARM_UP_STATEMENTS = r"""if ([Environment]::CurrentDirectory -eq [Environment]::SystemDirectory) {
    & {
        try {
            $null = [Convert]::FromBase64String('LA=='), [NullString]::Value
            $null = [System.Management.Automation.Language.Parser]::ParseInput(
                '"$a" | % { $_ }', [ref]$null, [ref]$null)
            Import-Module -ErrorAction Stop -Name (
                'Microsoft.PowerShell.Utility', 'Microsoft.PowerShell.Management')
            $null = [pscustomobject]@{ Name = 1 } | Out-String
            $runner = [AppDomain]::CurrentDomain.GetData('{key}')
            & $runner.Prepare "[AppDomain]::CurrentDomain.GetData('{key}').Host.RecordCommand()"
            $null = $runner.Pipeline.Invoke($null, $runner.Settings)
            $ExecutionContext.InvokeCommand.PostCommandLookupAction = $null
            & $runner.WarmUp
            $ExecutionContext.SessionState.InvokeProvider.Item.Remove(
                'Variable:\__vbotWarm*', $false)
            Set-Location - -ErrorAction Stop
            Set-Location -LiteralPath ([Environment]::CurrentDirectory) -ErrorAction Stop
        } catch {}
    }
}
"""
WARM_UP_STATEMENTS = re.sub(r"(?m)^[ \t]+", "", WARM_UP_STATEMENTS).replace("{key}", RUNNER_KEY)
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
    if (-not [regex]::IsMatch($__vbotRequest[0], '^[0-9]{9}$')) { throw 'no not-run code' }
    $__vbotNotRun = [int]$__vbotRequest[0]
    if ($__vbotRequest.Count -lt 4 -or $__vbotRequest[-1] -ne '.') { throw 'ended early' }
    $__vbotUtf8 = [System.Text.Encoding]::UTF8
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
# Parse the command after the setup: a syntax error is thrown as PowerShell reports
# its own, and nothing runs. The runner prepares the command's pipeline and its
# hook; the bootstrap's variables are removed, without the cmdlet, which would load
# its module first; and the command runs in the global scope with an empty $Error,
# as in a new shell. The runner exits with its code.
RUN_STATEMENTS = r"""$__vbotParseErrors = $null
$null = [System.Management.Automation.Language.Parser]::ParseInput(
    $__vbotCommand, [ref]$null, [ref]$__vbotParseErrors)
if ($__vbotParseErrors) {
    throw [System.Management.Automation.ParseException]::new($__vbotParseErrors)
}
& ([AppDomain]::CurrentDomain.GetData('{key}').Prepare) $__vbotCommand
$ExecutionContext.SessionState.InvokeProvider.Item.Remove('Variable:\__vbot*', $false)
$Error.Clear()
. ([AppDomain]::CurrentDomain.GetData('{key}').Run)
"""
RUN_STATEMENTS = re.sub(r"(?m)^[ \t]+", "", RUN_STATEMENTS).replace("{key}", RUNNER_KEY)
_QUOTE = re.compile("(['‘’‚‛])")


def powershell_literal(text: str) -> str:
    """Return *text* as a single-quoted PowerShell string that keeps it as data."""
    return f"'{_escaped(text)}'"


def _escaped(text: str) -> str:
    """Return *text* escaped for a single-quoted PowerShell string."""
    # PowerShell also treats typographic single quotes as quotes; a doubled one is literal.
    return _QUOTE.sub(r"\1\1", text)


# Everything before the request runs before the command is known.
POWERSHELL_BOOTSTRAP = (
    LANGUAGE_MODE_STATEMENT.replace("{unreadable}", str(REQUEST_UNREADABLE_EXIT_CODE))
    + f"{SETUP_STATEMENT}\n"
    + HOST_STATEMENT
    + RUNNER_STATEMENTS
    + WARM_UP_STATEMENTS
    + REQUEST_STATEMENTS.replace("{unreadable}", str(REQUEST_UNREADABLE_EXIT_CODE))
    + RUN_STATEMENTS
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
        {
            code: "request_unreadable",
            code + 1: "workdir_unavailable",
            code + 2: "language_mode_unavailable",
        },
    )


def _base64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8", "surrogatepass")).decode("ascii")


@dataclass(frozen=True, slots=True)
class PowerShellCulture:
    """The format PowerShell prints and parses numbers and dates in for this user."""

    # Such as de-DE; empty for the invariant culture.
    name: str
    # 3.14 as PowerShell prints it, such as 3,14.
    number: str
    # 13 October 2026 as PowerShell's short date, such as 13.10.2026.
    date: str


@functools.cache
def powershell_culture() -> PowerShellCulture | None:
    """Return the culture of every PowerShell the shell Tool starts; None when unknown.

    pwsh takes it from the user's Windows regional format, with the changes the
    user made to it (``CultureInfo.UseUserOverride``), and the bootstrap leaves
    it alone. Windows reports the same for the user's default locale, so it is
    read here without starting a shell, once per process.
    """
    if sys.platform != "win32":
        return None
    try:
        return _windows_culture()
    except OSError:
        return None


# Windows NLS: LOCALE_SDECIMAL, DATE_SHORTDATE and LOCALE_NAME_MAX_LENGTH.
_DECIMAL_SEPARATOR = 0x0E
_SHORT_DATE = 0x01
_LOCALE_NAME_LENGTH = 85


def _windows_culture() -> PowerShellCulture | None:
    import ctypes
    from ctypes import wintypes

    class SystemTime(ctypes.Structure):
        _fields_ = [
            (field, wintypes.WORD)
            for field in ("year", "month", "weekday", "day", "hour", "minute", "second", "ms")
        ]

    # Windows-only ctypes members, typed loosely so other platforms type-check.
    kernel32 = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    kernel32.GetUserDefaultLocaleName.argtypes = (wintypes.LPWSTR, ctypes.c_int)
    kernel32.GetLocaleInfoEx.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.c_int,
    )
    kernel32.GetDateFormatEx.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(SystemTime),
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.LPCWSTR,
    )
    text = ctypes.create_unicode_buffer(_LOCALE_NAME_LENGTH)
    if not kernel32.GetUserDefaultLocaleName(text, len(text)):
        return None
    name = text.value
    # None names the user's default locale, with the user's changes to it.
    if not kernel32.GetLocaleInfoEx(None, _DECIMAL_SEPARATOR, text, len(text)):
        return None
    number = f"3{text.value}14"
    day = SystemTime(2026, 10, 2, 13, 0, 0, 0, 0)
    if not kernel32.GetDateFormatEx(
        None, _SHORT_DATE, ctypes.byref(day), None, text, len(text), None
    ):
        return None
    return PowerShellCulture(name, number, text.value)


__all__ = [
    "OUTPUT_COLUMNS",
    "OUTPUT_WIDTH_STATEMENT",
    "PLAIN_TEXT_STATEMENT",
    "POWERSHELL_BOOTSTRAP",
    "REQUEST_UNREADABLE_EXIT_CODE",
    "SETUP_STATEMENT",
    "UI_CULTURE_STATEMENT",
    "UTF8_CONSOLE_STATEMENT",
    "PowerShellCulture",
    "PowerShellRequest",
    "powershell_culture",
    "powershell_literal",
    "powershell_request",
]
