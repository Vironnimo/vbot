"""PowerShell's console host adapter and its disposable compiled-assembly cache.

The C# host (``_powershell_host.cs``) remains owned by the shell runner, and so
do the checks and guarded cmdlets it compiles with it (``_powershell.py``,
Prepare and Guards): compiled, a check walks the command without a script block
call for each node. Loading its compiled bytes avoids
compiler initialization and its allocations in every command process. Cache
failures must not change a command's execution, output or status; in-memory compilation is
the fallback. No cache data is part of the installation or canonical data store.
"""

from __future__ import annotations

from pathlib import Path

# The C# source, read by each shell from this file: inline it would push the
# bootstrap past the 32,767 characters Windows allows a command line.
HOST_SOURCE_PATH = Path(__file__).with_name("_powershell_host.cs")


def cache_directory(home: Path) -> Path:
    """Executable host code shared by the Windows shells of this user."""
    return home / ".cache" / "vbot-powershell-host"


# One bounded, checksummed entry is published atomically, never a DLL/receipt pair.
# Loading bytes leaves no file locked and uses the actual shell's dependencies.
# The runtime key includes module ids: a patch-level assembly replacement must
# invalidate the entry even when its public assembly version did not change.
_LOAD_HOST = r"""& {
    $ErrorActionPreference = 'Stop'
    $source = [IO.File]::ReadAllText({source_path})
    $root = {cache_dir}
    # The assembly of the file cmdlets the guards extend.
    # Naming one reference drops Add-Type's default ones, so all are named.
    $cmdlets = @(
        (Get-Command Microsoft.PowerShell.Management\Get-Location
            ).ImplementingType.Assembly.Location
        [psobject].Assembly.Location
        'netstandard', 'System.Runtime', 'System.Collections', 'System.Linq', 'System.Console',
        'System.Runtime.Extensions', 'System.Runtime.InteropServices', 'System.ObjectModel',
        'System.Text.RegularExpressions'
    )
    $hash = [System.Security.Cryptography.SHA256]::Create()
    $loaded = $false
    $lock = $null
    $compiled = $null
    $staged = $null
    try {
        $identity = [Convert]::ToBase64String($hash.ComputeHash(
            [Text.Encoding]::UTF8.GetBytes($source))) + '|' +
            $PSVersionTable.PSVersion + '|' + $PSHOME + '|' +
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
        Add-Type -TypeDefinition $source -ReferencedAssemblies $cmdlets `
            -OutputAssembly $compiled -ErrorAction Stop
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
    if (-not $loaded) {
        Add-Type -TypeDefinition $source -ReferencedAssemblies $cmdlets -ErrorAction Stop
    }
}
"""


def host_statement(cache_dir_literal: str, source_path_literal: str) -> str:
    """Return the host loader for an already quoted, absolute cache directory and source file."""
    return _LOAD_HOST.replace("{cache_dir}", cache_dir_literal).replace(
        "{source_path}", source_path_literal
    )
