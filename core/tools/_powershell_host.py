"""PowerShell's console host adapter and its disposable compiled-assembly cache.

The C# host remains owned by the shell runner. Loading its compiled bytes avoids
compiler initialization and its allocations in every command process. Cache
failures must not change a command's execution, output or status; in-memory compilation is
the fallback. No cache data is part of the installation or canonical data store.
"""

from __future__ import annotations

import hashlib

HOST_SOURCE = r"""using System;
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
"""

# One bounded, checksummed entry is published atomically, never a DLL/receipt pair.
# Loading bytes leaves no file locked and uses the actual shell's dependencies.
# The runtime key includes module ids: a patch-level assembly replacement must
# invalidate the entry even when its public assembly version did not change.
_LOAD_HOST = r"""& {
    $ErrorActionPreference = 'Stop'
    $source = @'
{source}
'@
    $root = {cache_dir}
    $hash = [System.Security.Cryptography.SHA256]::Create()
    $loaded = $false
    $lock = $null
    $compiled = $null
    $staged = $null
    try {
        $identity = '{source_hash}' + '|' + $PSVersionTable.PSVersion + '|' + $PSHOME + '|' +
            [System.Management.Automation.PSObject].Assembly.ManifestModule.ModuleVersionId + '|' +
            [object].Assembly.ManifestModule.ModuleVersionId
        $key = [BitConverter]::ToString($hash.ComputeHash(
            [Text.Encoding]::UTF8.GetBytes($identity))).Replace('-', '').ToLowerInvariant()
        $path = [IO.Path]::Combine($root, 'v1-' + $key + '.bin')
        $load = {
            try {
                $stream = [IO.File]::Open($path, 'Open', 'Read', 'ReadWrite, Delete')
                try {
                    $length = $stream.Length
                    if ($length -le 32 -or $length -gt 1MB) { return $false }
                    $entry = [byte[]]::new($length)
                    $offset = 0
                    while ($offset -lt $length) {
                        $read = $stream.Read($entry, $offset, $length - $offset)
                        if ($read -eq 0) { return $false }
                        $offset += $read
                    }
                } finally { $stream.Dispose() }
                $bytes = [byte[]]::new($length - 32)
                [Array]::Copy($entry, 32, $bytes, 0, $bytes.Length)
                if ([Convert]::ToBase64String($entry, 0, 32) -cne
                    [Convert]::ToBase64String($hash.ComputeHash($bytes))) { return $false }
                $assembly = [Reflection.Assembly]::Load($bytes)
                return [System.Management.Automation.Host.PSHost].IsAssignableFrom(
                    $assembly.GetType('VBotPowerShellHost', $true))
            } catch { return $false }
        }
        if (& $load) { return }
        $null = [IO.Directory]::CreateDirectory($root)
        $wait = [Diagnostics.Stopwatch]::StartNew()
        while ($null -eq $lock) {
            try { $lock = [IO.File]::Open($path + '.lock', 'OpenOrCreate', 'ReadWrite', 'None') }
            catch [IO.IOException] {
                # Only Windows sharing/lock violations mean another builder.
                if (($_.Exception.GetBaseException().HResult -band 0xffff) -notin 32, 33 -or
                    $wait.ElapsedMilliseconds -ge 3000) { throw }
                [Threading.Thread]::Sleep(20)
            }
        }
        if (& $load) { return }
        $name = [Guid]::NewGuid().ToString('N')
        $compiled = [IO.Path]::Combine($root, $name + '.dll')
        Add-Type -TypeDefinition $source -OutputAssembly $compiled -ErrorAction Stop
        $bytes = [IO.File]::ReadAllBytes($compiled)
        $null = [Reflection.Assembly]::Load($bytes)
        $loaded = $true
        $entry = [byte[]]::new(32 + $bytes.Length)
        [Array]::Copy($hash.ComputeHash($bytes), 0, $entry, 0, 32)
        [Array]::Copy($bytes, 0, $entry, 32, $bytes.Length)
        $staged = [IO.Path]::Combine($root, $name + '.tmp')
        [IO.File]::WriteAllBytes($staged, $entry)
        [IO.File]::Move($staged, $path, $true)
    } catch {
        # A disposable cache must not prevent user execution or print into its
        # raw output. If compilation already succeeded, keep that loaded host.
    } finally {
        if ($null -ne $lock) { $lock.Dispose() }
        foreach ($temporary in $compiled, $staged) {
            if ($null -ne $temporary) { try { [IO.File]::Delete($temporary) } catch {} }
        }
        $hash.Dispose()
    }
    if (-not $loaded) { Add-Type -TypeDefinition $source -ErrorAction Stop }
}
"""


def host_statement(cache_dir_literal: str) -> str:
    """Return the host loader for an already quoted, absolute cache directory."""
    return (
        _LOAD_HOST.replace("{source_hash}", hashlib.sha256(HOST_SOURCE.encode()).hexdigest())
        .replace("{cache_dir}", cache_dir_literal)
        .replace("{source}", HOST_SOURCE)
    )
