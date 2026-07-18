#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$PackageName = "vbot",
    [switch]$RemoveAutostart,
    [string]$TaskName = "vBot"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# vBot uninstaller for Windows. Mirrors scripts/uninstall.sh. Managed fresh
# installs remove their complete installer-owned tree; managed installations in
# an existing checkout remove only the installer-owned venv and launcher; direct
# internal setup installs uninstall only the pip package.
# Either way the data dir (~\.vbot) is never touched.

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RootMarker = Join-Path $ProjectRoot ".vbot-install-root"
$VenvMarker = Join-Path $ProjectRoot ".vbot-install-venv"
$LegacyRootMarker = Join-Path $ProjectRoot ".vbot-bootstrap"
$InstallManifest = Join-Path $ProjectRoot ".vbot-install.json"

# Start-menu shortcut filename. Kept in sync with scripts/setup.ps1, which
# creates a shortcut by this exact name under -Desktop / -DesktopClient.
$DesktopShortcutName = "vBot Desktop.lnk"

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

function Remove-DesktopShortcut {
    if (-not (Test-RunningOnWindows)) {
        return
    }

    $programsDir = [System.Environment]::GetFolderPath("Programs")
    if ([string]::IsNullOrWhiteSpace($programsDir)) {
        return
    }

    $shortcutPath = Join-Path $programsDir $DesktopShortcutName
    if (Test-Path $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath -Force
        Write-Host "Removed Start-menu shortcut '$DesktopShortcutName'."
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

function Resolve-UninstallPython {
    if (Test-Path -LiteralPath $InstallManifest -PathType Leaf) {
        try {
            $state = Get-Content -Raw -LiteralPath $InstallManifest | ConvertFrom-Json
            $recordedPython = $state.python_executable
            if (-not [string]::IsNullOrWhiteSpace($recordedPython) -and (Test-Path -LiteralPath $recordedPython -PathType Leaf)) {
                Write-Host "Using the Python interpreter recorded by the installer: $recordedPython"
                return New-CommandSpec -Exe $recordedPython
            }
            Write-Warning "The installation manifest's Python interpreter is unavailable; falling back to PATH."
        }
        catch {
            Write-Warning "The installation manifest is invalid; falling back to PATH: $($_.Exception.Message)"
        }
    }
    return Resolve-PythonCommand
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

function Get-VbotAutostartTask {
    $expectedPath = "\" + $TaskName.TrimStart('\')
    try {
        $tasks = @(Get-ScheduledTask -ErrorAction Stop)
    }
    catch {
        throw "Could not query Windows Task Scheduler: $($_.Exception.Message)"
    }
    return $tasks | Where-Object {
        ($_.TaskPath + $_.TaskName) -ieq $expectedPath
    } | Select-Object -First 1
}

function Remove-VbotAutostart {
    if (-not (Test-RunningOnWindows)) {
        Write-Warning "Autostart removal is only implemented for Windows Task Scheduler."
        return
    }

    $task = Get-VbotAutostartTask
    if ($null -eq $task) {
        Write-Host "No autostart task named '$TaskName' exists. If you installed with a custom -TaskName, pass the same one here."
        return
    }

    Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Confirm:$false -ErrorAction Stop
    Write-Host "Removed autostart task '$TaskName'."
}

function Warn-IfAutostartRemains {
    if (-not (Test-RunningOnWindows)) {
        return
    }

    try {
        $task = Get-VbotAutostartTask
    }
    catch {
        Write-Warning $_.Exception.Message
        return
    }
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

function Get-RecordedServerStopArguments {
    $stopArguments = @("server", "stop")
    if (-not (Test-Path -LiteralPath $InstallManifest -PathType Leaf)) {
        return $stopArguments
    }

    try {
        $state = Get-Content -Raw -LiteralPath $InstallManifest | ConvertFrom-Json
        $hostProperty = $state.PSObject.Properties["server_host"]
        $portProperty = $state.PSObject.Properties["server_port"]
        $dataProperty = $state.PSObject.Properties["server_data_directory"]
        if (
            $null -ne $hostProperty -and $null -ne $hostProperty.Value -and
            $null -ne $portProperty -and $null -ne $portProperty.Value -and
            $null -ne $dataProperty -and $null -ne $dataProperty.Value
        ) {
            $stopArguments += @(
                "--host", [string]$hostProperty.Value,
                "--port", [string]$portProperty.Value,
                "--data-dir", [string]$dataProperty.Value
            )
        }
    }
    catch {
        Write-Warning "Could not read the recorded server target; stopping the default instance instead: $($_.Exception.Message)"
    }
    return $stopArguments
}

function Invoke-ManagedUninstall {
    param([switch]$PreserveCheckout)

    if ($PreserveCheckout) {
        Write-Step "Removing managed vBot environment from $ProjectRoot"
    }
    else {
        $rootNormalized = [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\', '/')
        $homeNormalized = [System.IO.Path]::GetFullPath($HOME).TrimEnd('\', '/')
        $driveRootNormalized = [System.IO.Path]::GetPathRoot($rootNormalized).TrimEnd('\', '/')
        if (
            [string]::IsNullOrWhiteSpace($rootNormalized) -or
            ($rootNormalized -ieq $homeNormalized) -or
            ($rootNormalized -ieq $driveRootNormalized)
        ) {
            throw "Refusing to remove '$ProjectRoot'."
        }
        Write-Step "Removing managed install at $ProjectRoot"
    }

    if (Test-RunningOnWindows) {
        try {
            $task = Get-VbotAutostartTask
        }
        catch {
            Write-Warning "$($_.Exception.Message) The install will be removed, but its autostart task may remain."
            $task = $null
        }
        if ($null -ne $task) {
            try {
                Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Confirm:$false -ErrorAction Stop
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
            $stopArguments = Get-RecordedServerStopArguments
            & $venvVbot @stopArguments *> $null
        }
        catch {
            # best-effort
        }
    }

    # The shim itself lives inside ProjectRoot; drop its PATH entry.
    Remove-FromUserPath -PathToRemove (Join-Path $ProjectRoot "bin")

    # The Start-menu shortcut lives outside ProjectRoot, so remove it explicitly.
    Remove-DesktopShortcut

    Set-Location $HOME
    if ($PreserveCheckout) {
        $venvPath = Join-Path $ProjectRoot ".venv"
        $binPath = Join-Path $ProjectRoot "bin"
        if (Test-Path -LiteralPath $venvPath) {
            Remove-DirectoryWithRetry -Path $venvPath
        }
        if (Test-Path -LiteralPath $binPath) {
            Remove-DirectoryWithRetry -Path $binPath
        }
        foreach ($path in @($InstallManifest, $VenvMarker)) {
            if (Test-Path -LiteralPath $path) {
                Remove-Item -LiteralPath $path -Force
            }
        }
    }
    else {
        Remove-DirectoryWithRetry -Path $ProjectRoot
    }

    Write-Step "Uninstall complete"
    if ($PreserveCheckout) {
        Write-Host "Removed the installer-managed virtual environment; preserved $ProjectRoot."
    }
    else {
        Write-Host "Removed $ProjectRoot (including its virtual environment)."
    }
    Write-Host "Data directories such as ~\.vbot were not modified."
}

function Invoke-ManualUninstall {
    Write-Step "Uninstalling pip package: $PackageName"
    $python = Resolve-UninstallPython
    Invoke-External $python @("-m", "pip", "uninstall", "-y", $PackageName)

    if (Test-Path -LiteralPath $InstallManifest) {
        Remove-Item -LiteralPath $InstallManifest -Force
        Write-Host "Removed installation manifest."
    }

    Remove-DesktopShortcut

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

if ((Test-Path $RootMarker) -or (Test-Path $LegacyRootMarker)) {
    Invoke-ManagedUninstall
}
elseif (Test-Path $VenvMarker) {
    Invoke-ManagedUninstall -PreserveCheckout
}
else {
    Invoke-ManualUninstall
}
