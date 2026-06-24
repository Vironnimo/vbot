#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$PackageName = "vbot",
    [switch]$RemoveAutostart,
    [string]$TaskName = "vBot"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# vBot uninstaller for Windows. Mirrors scripts/uninstall.sh. Two modes, picked by
# whether this is a self-contained bootstrap install:
#   - bootstrap install (a .vbot-bootstrap marker sits next to scripts\): remove
#     the whole tree (venv + source), the 'vbot' shim + its PATH entry, and the
#     autostart task.
#   - manual/editable install (no marker): uninstall the pip package from the
#     active interpreter and optionally remove the autostart task.
# Either way the data dir (~\.vbot) is never touched.

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Marker = Join-Path $ProjectRoot ".vbot-bootstrap"

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message"
}

function Test-RunningOnWindows {
    if (Get-Variable -Name IsWindows -Scope Global -ErrorAction SilentlyContinue) {
        return $IsWindows
    }
    return [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
}

function New-CommandSpec {
    param(
        [string]$Exe,
        [string[]]$PrefixArguments = @()
    )

    return [pscustomobject]@{
        Exe = $Exe
        PrefixArguments = $PrefixArguments
    }
}

function Resolve-PythonCommand {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        return New-CommandSpec -Exe $python.Source
    }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $py) {
        return New-CommandSpec -Exe $py.Source -PrefixArguments @("-3")
    }

    throw "Python is required to uninstall the pip package, but neither 'python' nor 'py' was found."
}

function Invoke-External {
    param(
        [object]$CommandSpec,
        [string[]]$Arguments,
        [string]$WorkingDirectory = $ProjectRoot
    )

    Push-Location $WorkingDirectory
    try {
        & $CommandSpec.Exe @($CommandSpec.PrefixArguments + $Arguments)
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed with exit code ${LASTEXITCODE}: $($CommandSpec.Exe) $($Arguments -join ' ')"
        }
    }
    finally {
        Pop-Location
    }
}

function Remove-VbotAutostart {
    if (-not (Test-RunningOnWindows)) {
        Write-Warning "Autostart removal is only implemented for Windows Task Scheduler."
        return
    }

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "No autostart task named '$TaskName' exists. If you installed with a custom -TaskName, pass the same one here."
        return
    }

    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed autostart task '$TaskName'."
}

function Warn-IfAutostartRemains {
    if (-not (Test-RunningOnWindows)) {
        return
    }

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        Write-Warning "Autostart task '$TaskName' still exists. Re-run with -RemoveAutostart to remove it."
    }
}

function Remove-FromUserPath {
    param([string]$PathToRemove)

    $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    if ([string]::IsNullOrWhiteSpace($userPath)) {
        return
    }

    $target = $PathToRemove.TrimEnd('\', '/')
    $kept = @()
    $removed = $false
    foreach ($entry in ($userPath -split [System.IO.Path]::PathSeparator)) {
        if ([string]::IsNullOrWhiteSpace($entry)) {
            continue
        }
        if ($entry.TrimEnd('\', '/') -ieq $target) {
            $removed = $true
            continue
        }
        $kept += $entry
    }

    if ($removed) {
        [System.Environment]::SetEnvironmentVariable("Path", ($kept -join [System.IO.Path]::PathSeparator), "User")
        Write-Host "Removed $PathToRemove from your user PATH."
    }
}

function Remove-DirectoryWithRetry {
    param([string]$Path)

    # The just-stopped server can hold a brief lock on the venv; retry rather than
    # fail the whole uninstall on a transient handle.
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return
        }
        catch {
            if ($attempt -eq 3) {
                throw
            }
            Start-Sleep -Seconds 1
        }
    }
}

function Invoke-BootstrapUninstall {
    $rootNormalized = [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\', '/')
    $homeNormalized = [System.IO.Path]::GetFullPath($HOME).TrimEnd('\', '/')
    if ([string]::IsNullOrWhiteSpace($rootNormalized) -or ($rootNormalized -ieq $homeNormalized)) {
        throw "Refusing to remove '$ProjectRoot'."
    }

    Write-Step "Removing bootstrap install at $ProjectRoot"

    if (Test-RunningOnWindows) {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -ne $task) {
            try {
                Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
                Write-Host "Removed autostart task '$TaskName'."
            }
            catch {
                Write-Warning "Could not remove autostart task '$TaskName' (this usually needs an elevated terminal). Remove it manually: Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
            }
        }
    }

    # Stop a running server so the venv unlocks before removal (best-effort).
    $venvVbot = Join-Path $ProjectRoot ".venv\Scripts\vbot.exe"
    if (Test-Path $venvVbot) {
        try {
            & $venvVbot server stop *> $null
        }
        catch {
            # best-effort
        }
    }

    # The shim itself lives inside ProjectRoot (removed with it); drop its PATH entry.
    Remove-FromUserPath -PathToRemove (Join-Path $ProjectRoot "bin")

    Set-Location $HOME
    Remove-DirectoryWithRetry -Path $ProjectRoot

    Write-Step "Uninstall complete"
    Write-Host "Removed $ProjectRoot (including its virtual environment)."
    Write-Host "Data directories such as ~\.vbot were not modified."
}

function Invoke-ManualUninstall {
    Write-Step "Uninstalling pip package: $PackageName"
    $python = Resolve-PythonCommand
    Invoke-External $python @("-m", "pip", "uninstall", "-y", $PackageName)

    if ($RemoveAutostart) {
        Write-Step "Removing autostart task"
        Remove-VbotAutostart
    }
    else {
        Warn-IfAutostartRemains
    }

    Write-Step "Uninstall complete"
    Write-Host "Data directories such as ~\.vbot were not modified."
    Write-Host "Source files, webui/node_modules, and webui/dist were not removed."
}

if (Test-Path $Marker) {
    Invoke-BootstrapUninstall
}
else {
    Invoke-ManualUninstall
}
