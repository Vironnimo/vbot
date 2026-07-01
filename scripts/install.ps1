#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$DataDir = (Join-Path $HOME ".vbot"),
    [string]$HostName = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 8420,
    [switch]$Desktop,
    [switch]$DesktopClient,
    [switch]$Dev,
    [switch]$NoAutostart,
    [switch]$SkipWebuiBuild,
    [switch]$SkipPathUpdate,
    [string]$TaskName = "vBot"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$WebUiDir = Join-Path $ProjectRoot "webui"

if ($Desktop -and $DesktopClient) {
    throw "-Desktop and -DesktopClient are mutually exclusive: -Desktop adds the accessor to a full server install, -DesktopClient installs the accessor with no server stack."
}
if ($DesktopClient -and $Dev) {
    throw "-DesktopClient and -Dev are mutually exclusive: -DesktopClient installs the accessor with no server stack, -Dev installs the full development environment."
}

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
    if ([System.IO.Path]::IsPathRooted($PathText)) {
        return [System.IO.Path]::GetFullPath($PathText)
    }
    # A bare GetFullPath resolves against the process working directory, which
    # does not follow Set-Location; anchor relative paths to the PowerShell
    # location instead.
    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location).ProviderPath $PathText))
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

        # Match the Linux installer's strictness: only a JSON integer counts —
        # no booleans, strings, or fractional numbers (a cast would round).
        if ($value -is [bool] -or -not ($value -is [int] -or $value -is [long])) {
            throw "settings.json value '$key' must be an integer port."
        }
        if ($value -lt 1 -or $value -gt 65535) {
            throw "settings.json value '$key' must be between 1 and 65535."
        }
        $configuredPort = [int]$value
        Write-Host "Using port $configuredPort from existing settings.json ($key). Pass -Port to override installer commands."
        return $configuredPort
    }

    return $DefaultPort
}

# Write an explicit -Port into an existing settings.json, updating the port key
# the app actually reads (first present wins, like the server's resolver). Keeps
# the autostart entry and later flag-less commands (server status/stop) on the
# same port. Runs through Python because PowerShell's JSON round-trip mangles
# value types; prints the updated key, or nothing when the port already matches.
# Python-side strings use single quotes only: Windows PowerShell 5.1 does not
# escape embedded double quotes when passing arguments to native commands.
$SyncSettingsPortScript = @'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
port = int(sys.argv[2])
settings = json.loads(path.read_text(encoding='utf-8'))
if not isinstance(settings, dict):
    raise SystemExit('settings.json must hold a JSON object')
keys = ('server_port', 'SERVER_PORT', 'port', 'PORT')
key = next((k for k in keys if k in settings), 'server_port')
if settings.get(key) != port:
    settings[key] = port
    path.write_text(json.dumps(settings, indent=4, ensure_ascii=False) + '\n', encoding='utf-8')
    print(key)
'@

function Initialize-DataDirectory {
    param(
        [string]$ResolvedDataDir,
        [int]$ResolvedPort,
        [object]$Python,
        [bool]$SyncPortIntoSettings
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
    elseif ($SyncPortIntoSettings) {
        $updatedKey = Invoke-Capture $Python @("-c", $SyncSettingsPortScript, $settingsPath, "$ResolvedPort")
        if (-not [string]::IsNullOrWhiteSpace($updatedKey)) {
            Write-Host "Updated $updatedKey to $ResolvedPort in existing settings.json (-Port)."
        }
        else {
            Write-Host "Keeping existing settings.json (already at port $ResolvedPort)."
        }
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

    # -Dev swaps the base groups; -Desktop stays an add-on on top of either base,
    # so a dev install with the desktop accessor gets both dependency groups.
    # -DesktopClient is its own accessor-only shape and excludes -Dev.
    $groups = if ($Dev) { @("dev") } else { @("server", "cli") }
    if ($DesktopClient) {
        $groups = @("cli", "desktop")
    }
    elseif ($Desktop) {
        $groups += "desktop"
    }
    $extra = ".[{0}]" -f ($groups -join ",")

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

    # Prepend for this session so every later resolution — including shutil.which
    # inside 'vbot autostart enable' — prefers the just-installed vbot over a
    # stale one elsewhere on PATH.
    $env:Path = $PathToAdd + [System.IO.Path]::PathSeparator + $env:Path

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

    # Prefer the just-installed command over whatever PATH resolves first: a
    # stale vbot elsewhere must not win verification or autostart registration.
    foreach ($candidateName in @("vbot.exe", "vbot.cmd", "vbot")) {
        $candidate = Join-Path $ScriptsPath $candidateName
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    $command = Get-Command vbot -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    throw "The vbot command was not found after installation. Check pip output for installation errors."
}

# Start-menu shortcut filename. Kept in sync with scripts/uninstall.ps1, which
# removes a shortcut by this exact name on both the bootstrap and manual paths.
$DesktopShortcutName = "vBot Desktop.lnk"

function New-DesktopShortcut {
    param([string]$TargetPath)

    if (-not (Test-RunningOnWindows)) {
        Write-Warning "Start-menu shortcut creation is only implemented for Windows."
        return
    }

    $programsDir = [System.Environment]::GetFolderPath("Programs")
    if ([string]::IsNullOrWhiteSpace($programsDir)) {
        Write-Warning "Could not resolve the Start-menu Programs folder; skipping shortcut creation."
        return
    }

    New-Item -ItemType Directory -Path $programsDir -Force | Out-Null
    $shortcutPath = Join-Path $programsDir $DesktopShortcutName

    $shell = New-Object -ComObject WScript.Shell
    try {
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $TargetPath
        $shortcut.Arguments = "desktop"
        $shortcut.WorkingDirectory = (Split-Path -Parent $TargetPath)
        $shortcut.Description = "Open the vBot desktop window"
        $shortcut.Save()
    }
    finally {
        [System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell) | Out-Null
    }

    Write-Host "Created Start-menu shortcut: $shortcutPath"
}

$resolvedDataDir = Resolve-UserPath $DataDir
$effectivePort = Resolve-EffectivePort `
    -ResolvedDataDir $resolvedDataDir `
    -DefaultPort $Port `
    -PortWasProvided ($PSBoundParameters.ContainsKey("Port"))
# The desktop-client mode installs only the accessor: no server stack, no local
# WebUI build, no data dir, no autostart. It still needs Python, the package, the
# vbot command on PATH, and the Start-menu shortcut.
$buildWebUi = (-not $SkipWebuiBuild) -and (-not $DesktopClient)

$python = Resolve-PythonCommand
if ($buildWebUi) {
    $node = Resolve-CommandSpec @("node.exe", "node")
    $npm = Resolve-CommandSpec @("npm.cmd", "npm")
}

Write-Step "Checking prerequisites"
Test-PythonVersion $python
if ($buildWebUi) {
    Invoke-External $node @("--version")
    Invoke-External $npm @("--version")
}

if (-not $DesktopClient) {
    Initialize-DataDirectory `
        -ResolvedDataDir $resolvedDataDir `
        -ResolvedPort $effectivePort `
        -Python $python `
        -SyncPortIntoSettings ($PSBoundParameters.ContainsKey("Port"))
}
Install-PythonPackage $python
if ($DesktopClient) {
    Write-Step "Skipping WebUI build (desktop-client install has no local server)"
}
elseif ($SkipWebuiBuild) {
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

if ($DesktopClient) {
    Write-Step "Verifying vBot command"
    Invoke-External $vbotCommand @("--help")
}
else {
    Write-Step "Verifying vBot command and settings"
    Invoke-External $vbotCommand @("--help")
    Invoke-External $vbotCommand @("doctor", "settings", "--data-dir", $resolvedDataDir)
}

if ($Desktop -or $DesktopClient) {
    Write-Step "Creating Start-menu shortcut"
    New-DesktopShortcut -TargetPath $vbotPath
}

# A desktop-client install has no local server, so autostart never applies.
if (-not $DesktopClient -and -not $NoAutostart) {
    Write-Step "Enabling autostart and starting the server"
    & $vbotPath autostart enable --host $HostName --port $effectivePort --data-dir $resolvedDataDir --task-name $TaskName
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Warning: enabling autostart failed (see message above). On Windows, run 'vbot autostart enable' from an elevated terminal."
    }
}

Write-Step "Installation complete"
Write-Host "vBot command: $vbotPath"
if ($DesktopClient) {
    Write-Host "Installed the desktop client (no local server)."
    Write-Host "Launch it from the Start menu (vBot Desktop) or run: vbot desktop"
}
else {
    Write-Host "Data directory: $resolvedDataDir"
    Write-Host "Server URL: http://${HostName}:$effectivePort"
    Write-Host "Try: vbot server status --host $HostName --port $effectivePort --data-dir `"$resolvedDataDir`""
}