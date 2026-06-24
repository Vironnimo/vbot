#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$DataDir = (Join-Path $HOME ".vbot"),
    [string]$HostName = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 8420,
    [switch]$EnableAutostart,
    [switch]$Desktop,
    [switch]$Dev,
    [switch]$StartServer,
    [switch]$SkipWebuiBuild,
    [switch]$SkipPathUpdate,
    [string]$TaskName = "vBot"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$WebUiDir = Join-Path $ProjectRoot "webui"

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

function Resolve-UserPath {
    param([string]$PathText)

    if ($PathText -eq "~") {
        return $HOME
    }
    if ($PathText.StartsWith("~\") -or $PathText.StartsWith("~/")) {
        return [System.IO.Path]::GetFullPath((Join-Path $HOME $PathText.Substring(2)))
    }
    return [System.IO.Path]::GetFullPath($PathText)
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

function Resolve-CommandSpec {
    param([string[]]$Names)

    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            return New-CommandSpec -Exe $command.Source
        }
    }
    throw "Required command not found: $($Names -join ', ')"
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

    throw "Python 3.11 or newer is required, but neither 'python' nor 'py' was found."
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

function Invoke-Capture {
    param(
        [object]$CommandSpec,
        [string[]]$Arguments,
        [string]$WorkingDirectory = $ProjectRoot
    )

    Push-Location $WorkingDirectory
    try {
        $output = & $CommandSpec.Exe @($CommandSpec.PrefixArguments + $Arguments)
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed with exit code ${LASTEXITCODE}: $($CommandSpec.Exe) $($Arguments -join ' ')"
        }
        return ($output | Out-String).Trim()
    }
    finally {
        Pop-Location
    }
}

function Write-Utf8NoBomFile {
    param(
        [string]$Path,
        [string]$Content
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Test-PythonVersion {
    param([object]$Python)

    $version = Invoke-Capture $Python @(
        "-c",
        "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    )
    $parts = $version.Split(".")
    if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) {
        throw "Python 3.11 or newer is required; found Python $version."
    }
}

function Assert-ValidSettingsJson {
    param([string]$SettingsPath)

    if (-not (Test-Path $SettingsPath)) {
        return
    }

    try {
        Get-Content -Raw -Path $SettingsPath | ConvertFrom-Json | Out-Null
    }
    catch {
        throw "Existing settings.json is not valid JSON and was not overwritten: $SettingsPath"
    }
}

function Read-SettingsJson {
    param([string]$SettingsPath)

    if (-not (Test-Path $SettingsPath)) {
        return $null
    }

    try {
        return Get-Content -Raw -Path $SettingsPath | ConvertFrom-Json
    }
    catch {
        throw "Existing settings.json is not valid JSON and was not overwritten: $SettingsPath"
    }
}

function Get-JsonPropertyValue {
    param(
        [object]$Object,
        [string]$Name
    )

    if ($null -eq $Object) {
        return $null
    }

    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    return $property.Value
}

function Resolve-EffectivePort {
    param(
        [string]$ResolvedDataDir,
        [int]$DefaultPort,
        [bool]$PortWasProvided
    )

    if ($PortWasProvided) {
        return $DefaultPort
    }

    $settingsPath = Join-Path $ResolvedDataDir "settings.json"
    $settings = Read-SettingsJson $settingsPath
    if ($null -eq $settings) {
        return $DefaultPort
    }

    foreach ($key in @("server_port", "SERVER_PORT", "port", "PORT")) {
        $value = Get-JsonPropertyValue -Object $settings -Name $key
        if ($null -eq $value) {
            continue
        }

        try {
            $configuredPort = [int]$value
        }
        catch {
            throw "settings.json value '$key' must be an integer port."
        }
        if ($configuredPort -lt 1 -or $configuredPort -gt 65535) {
            throw "settings.json value '$key' must be between 1 and 65535."
        }
        Write-Host "Using port $configuredPort from existing settings.json ($key). Pass -Port to override installer commands."
        return $configuredPort
    }

    return $DefaultPort
}

function Initialize-DataDirectory {
    param(
        [string]$ResolvedDataDir,
        [int]$ResolvedPort
    )

    Write-Step "Preparing data directory: $ResolvedDataDir"
    New-Item -ItemType Directory -Path $ResolvedDataDir -Force | Out-Null

    $settingsPath = Join-Path $ResolvedDataDir "settings.json"
    Assert-ValidSettingsJson $settingsPath
    if (-not (Test-Path $settingsPath)) {
        $settings = [ordered]@{ server_port = $ResolvedPort } | ConvertTo-Json
        Write-Utf8NoBomFile -Path $settingsPath -Content ($settings + [Environment]::NewLine)
        Write-Host "Created settings.json with server_port $ResolvedPort."
    }
    else {
        Write-Host "Keeping existing valid settings.json."
    }

    $envPath = Join-Path $ResolvedDataDir ".env"
    if (-not (Test-Path $envPath)) {
        $envTemplate = @(
            "# vBot provider credentials",
            "# OPENAI_API_KEY=...",
            "# OPENROUTER_API_KEY=...",
            "# ANTHROPIC_API_KEY=..."
        ) -join [Environment]::NewLine
        Write-Utf8NoBomFile -Path $envPath -Content ($envTemplate + [Environment]::NewLine)
        Write-Host "Created .env template."
    }
    else {
        Write-Host "Keeping existing .env."
    }
}

function Install-PythonPackage {
    param([object]$Python)

    $extra = ".[server,cli]"
    if ($Desktop) {
        $extra = ".[server,cli,desktop]"
    }
    if ($Dev) {
        $extra = ".[dev]"
    }

    Write-Step "Installing Python package in editable mode: $extra"
    Invoke-External $Python @("-m", "pip", "install", "-e", $extra)
}

function Build-WebUi {
    param([object]$Npm)

    if (-not (Test-Path $WebUiDir)) {
        throw "WebUI directory not found: $WebUiDir"
    }

    Write-Step "Installing WebUI dependencies"
    Invoke-External $Npm @("install") $WebUiDir

    Write-Step "Building WebUI"
    Invoke-External $Npm @("run", "build") $WebUiDir

    $indexPath = Join-Path $WebUiDir "dist\index.html"
    if (-not (Test-Path $indexPath)) {
        throw "WebUI build did not create $indexPath."
    }
}

function Get-PythonScriptsPath {
    param([object]$Python)
    return Invoke-Capture $Python @("-c", "import sysconfig; print(sysconfig.get_path('scripts'))")
}

function Test-PathListContains {
    param(
        [string]$PathList,
        [string]$PathToFind
    )

    if ([string]::IsNullOrWhiteSpace($PathList)) {
        return $false
    }

    $target = [System.IO.Path]::GetFullPath($PathToFind).TrimEnd('\', '/')
    foreach ($entry in ($PathList -split [System.IO.Path]::PathSeparator)) {
        if ([string]::IsNullOrWhiteSpace($entry)) {
            continue
        }
        try {
            $normalizedEntry = [System.IO.Path]::GetFullPath($entry).TrimEnd('\', '/')
        }
        catch {
            continue
        }
        if ([string]::Equals($normalizedEntry, $target, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Ensure-PathContains {
    param([string]$PathToAdd)

    if ($SkipPathUpdate) {
        return
    }

    if (-not (Test-PathListContains -PathList $env:Path -PathToFind $PathToAdd)) {
        $env:Path = $env:Path + [System.IO.Path]::PathSeparator + $PathToAdd
    }

    if (-not (Test-RunningOnWindows)) {
        return
    }

    $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    if (Test-PathListContains -PathList $userPath -PathToFind $PathToAdd) {
        return
    }

    $updatedUserPath = if ([string]::IsNullOrWhiteSpace($userPath)) {
        $PathToAdd
    }
    else {
        $userPath + [System.IO.Path]::PathSeparator + $PathToAdd
    }
    [System.Environment]::SetEnvironmentVariable("Path", $updatedUserPath, "User")
    Write-Host "Added Python Scripts directory to the user PATH. Open a new terminal to inherit it."
}

function Resolve-VbotCommandPath {
    param([string]$ScriptsPath)

    $command = Get-Command vbot -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    foreach ($candidateName in @("vbot.exe", "vbot.cmd", "vbot")) {
        $candidate = Join-Path $ScriptsPath $candidateName
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    throw "The vbot command was not found after installation. Check pip output for installation errors."
}

function Enable-VbotAutostart {
    param(
        [string]$VbotPath,
        [string]$ResolvedDataDir,
        [int]$ResolvedPort
    )

    if (-not (Test-RunningOnWindows)) {
        throw "Autostart setup in this script currently supports Windows Task Scheduler only."
    }

    Write-Step "Configuring Windows autostart task: $TaskName"
    $arguments = "server start --host `"$HostName`" --port $ResolvedPort --data-dir `"$ResolvedDataDir`""
    $action = New-ScheduledTaskAction -Execute $VbotPath -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Description "Start vBot server at user login" `
        -Force |
        Out-Null
}

function Start-VbotServer {
    param(
        [string]$VbotPath,
        [string]$ResolvedDataDir,
        [int]$ResolvedPort
    )

    if (-not $StartServer) {
        return
    }

    Write-Step "Starting vBot server"
    $vbotCommand = New-CommandSpec -Exe $VbotPath
    Invoke-External $vbotCommand @(
        "server",
        "start",
        "--host",
        $HostName,
        "--port",
        "$ResolvedPort",
        "--data-dir",
        $ResolvedDataDir
    )
}

$resolvedDataDir = Resolve-UserPath $DataDir
$effectivePort = Resolve-EffectivePort `
    -ResolvedDataDir $resolvedDataDir `
    -DefaultPort $Port `
    -PortWasProvided ($PSBoundParameters.ContainsKey("Port"))
$python = Resolve-PythonCommand
if (-not $SkipWebuiBuild) {
    $node = Resolve-CommandSpec @("node.exe", "node")
    $npm = Resolve-CommandSpec @("npm.cmd", "npm")
}

Write-Step "Checking prerequisites"
Test-PythonVersion $python
if (-not $SkipWebuiBuild) {
    Invoke-External $node @("--version")
    Invoke-External $npm @("--version")
}

Initialize-DataDirectory $resolvedDataDir $effectivePort
Install-PythonPackage $python
if ($SkipWebuiBuild) {
    Write-Step "Skipping WebUI build (-SkipWebuiBuild)"
    $skipBuildIndex = Join-Path $WebUiDir "dist\index.html"
    if (-not (Test-Path $skipBuildIndex)) {
        throw "webui/dist/index.html not found. Build the WebUI on another machine and copy webui/dist here, or re-run without -SkipWebuiBuild."
    }
    Write-Host "Using existing webui/dist."
}
else {
    Build-WebUi $npm
}

$scriptsPath = Get-PythonScriptsPath $python
Ensure-PathContains $scriptsPath
$vbotPath = Resolve-VbotCommandPath $scriptsPath
$vbotCommand = New-CommandSpec -Exe $vbotPath

Write-Step "Verifying vBot command and settings"
Invoke-External $vbotCommand @("--help")
Invoke-External $vbotCommand @("doctor", "settings", "--data-dir", $resolvedDataDir)

if ($EnableAutostart) {
    Enable-VbotAutostart $vbotPath $resolvedDataDir $effectivePort
}

Start-VbotServer $vbotPath $resolvedDataDir $effectivePort

Write-Step "Installation complete"
Write-Host "vBot command: $vbotPath"
Write-Host "Data directory: $resolvedDataDir"
Write-Host "Server URL: http://${HostName}:$effectivePort"
Write-Host "Try: vbot server status --host $HostName --port $effectivePort --data-dir `"$resolvedDataDir`""