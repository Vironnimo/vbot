#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$PackageName = "vbot",
    [switch]$RemoveAutostart,
    [string]$TaskName = "vBot"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

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
        Write-Host "No autostart task named '$TaskName' exists."
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
Write-Host "Data directories such as ~/.vbot were not modified."
Write-Host "Source files, webui/node_modules, and webui/dist were not removed."