# vBot installer for Windows.
#
# Downloads the signed vBot installer of the latest release, of a chosen release
# (-Version) or of the newest main build (-Main), verifies it against the digest
# its release identity records and installs it per user. Installations update
# from the same channel.
#   irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1 | iex
# To pass options, download and run as a file, or:
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1))) -Main
[CmdletBinding()]
param(
    [string]$InstallDir = "",
    [switch]$Main,
    [string]$Version = "",
    [string]$DataDir = (Join-Path $HOME ".vbot"),
    [string]$HostName = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 8420,
    [switch]$Desktop,
    [switch]$DesktopClient,
    [switch]$NoAutostart,
    [switch]$AllowElevatedInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
# The trap below covers the whole script, including the option checks, and
# reports this log.
$InstallLogPath = Join-Path ([System.IO.Path]::GetTempPath()) ("vbot-install-{0:yyyyMMdd-HHmmss}-{1}.log" -f (Get-Date), $PID)
[System.IO.File]::WriteAllText($InstallLogPath, "vBot installation log`r`n", (New-Object System.Text.UTF8Encoding($false)))

if ($Main -and -not [string]::IsNullOrWhiteSpace($Version)) {
    throw "-Version selects a specific release and cannot be combined with -Main."
}
if ($Desktop -and $DesktopClient) {
    throw "-Desktop and -DesktopClient are mutually exclusive."
}
# Accept a bare version (0.1.2) as well as the tag form (v0.1.2).
if (-not [string]::IsNullOrWhiteSpace($Version) -and ($Version -notmatch '^v')) {
    $Version = "v$Version"
}
if (-not [string]::IsNullOrWhiteSpace($Version) -and ($Version -cnotmatch '^v[0-9A-Za-z][0-9A-Za-z._+-]*$')) {
    throw "-Version needs a release version such as 0.2.0 or v0.2.0."
}

# Release downloads, unlike the GitHub API, have no anonymous request limit.
$ReleaseDownloads = "https://github.com/Vironnimo/vbot/releases"
$ReleaseIdentityName = "vbot-release.json"

trap {
    $message = $_.Exception.Message
    try {
        Add-Content -LiteralPath $InstallLogPath -Value "Error: $message" -Encoding UTF8
    }
    catch {
        # The original failure is more useful than a secondary logging failure.
    }
    [Console]::Error.WriteLine("")
    [Console]::Error.WriteLine("[ERROR] vBot installation failed: $message")
    [Console]::Error.WriteLine("Technical details: $InstallLogPath")
    # Under `irm | iex` or a script block, exit would close the user's PowerShell
    # window before the message can be read; rethrow there instead.
    if ($PSCommandPath) { exit 1 }
    break
}

function Write-Status {
    param([string]$State, [string]$Message)
    $color = "Cyan"
    $symbol = [string][char]0x2026
    switch ($State) {
        "OK" { $color = "Green"; $symbol = [string][char]0x2713 }
        "WARN" { $color = "Yellow"; $symbol = "!" }
        "ERROR" { $color = "Red"; $symbol = [string][char]0x2717 }
    }
    if (-not [Console]::IsOutputRedirected -and $env:TERM -ne "dumb") {
        if ([string]::IsNullOrEmpty($env:NO_COLOR)) {
            Write-Host "$symbol $State" -ForegroundColor $color -NoNewline
            Write-Host " $Message"
        }
        else { Write-Host "$symbol $State $Message" }
    }
    else { Write-Host "[$State] $Message" }
}
function Write-Step { param([string]$Message) Write-Status "WORK" $Message }

function Test-IsElevated {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole(
        [System.Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Get-ReleaseDownloadBase {
    # Release assets download from <base>/<asset name>.
    param([string]$Tag, [bool]$MainBuild)
    if ($MainBuild) { return "$ReleaseDownloads/download/main-build" }
    if ([string]::IsNullOrWhiteSpace($Tag)) { return "$ReleaseDownloads/latest/download" }
    return "$ReleaseDownloads/download/$Tag"
}

function Save-ReleaseDownload {
    param([string]$Uri, [string]$OutFile)
    $parsed = $null
    if (
        -not [System.Uri]::TryCreate($Uri, [System.UriKind]::Absolute, [ref]$parsed) -or
        $parsed.Scheme -cne "https" -or
        $parsed.Host -cne "github.com"
    ) {
        throw "$Uri is not an official HTTPS GitHub download URL."
    }
    $previousProgressPreference = $ProgressPreference
    $ProgressPreference = "SilentlyContinue"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $parsed.AbsoluteUri -OutFile $OutFile
    }
    finally {
        $ProgressPreference = $previousProgressPreference
    }
}

function Get-ReleaseIdentity {
    # Every release publishes vbot-release.json beside its assets: the version
    # and the SHA-256 digest of each other asset.
    param([string]$Base, [string]$Release)
    $path = Join-Path ([System.IO.Path]::GetTempPath()) ("vbot-release-{0}.json" -f $PID)
    try {
        try {
            Save-ReleaseDownload -Uri "$Base/$ReleaseIdentityName" -OutFile $path
        }
        catch {
            throw "Could not read $ReleaseIdentityName of $Release from $Base ($($_.Exception.Message)). The release does not exist, publishes no $ReleaseIdentityName, or GitHub cannot be reached."
        }
        try {
            $identity = Get-Content -Raw -Encoding UTF8 -LiteralPath $path | ConvertFrom-Json
        }
        catch {
            $identity = $null
        }
    }
    finally {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
    $valid = $identity -is [System.Management.Automation.PSCustomObject]
    if ($valid) {
        $schema = $identity.PSObject.Properties["schema_version"]
        $version = $identity.PSObject.Properties["version"]
        $valid = (
            $null -ne $schema -and $schema.Value -eq 1 -and
            $null -ne $version -and [string]$version.Value -cmatch '^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$'
        )
    }
    if (-not $valid) {
        throw "$Base/$ReleaseIdentityName is not a valid vBot release identity."
    }
    return $identity
}

function Get-RecordedDigest {
    # The lowercase SHA-256 hex digest the identity records for one asset, or $null.
    param($Identity, [string]$Name)
    $assets = $Identity.PSObject.Properties["assets"]
    if ($null -eq $assets -or $assets.Value -isnot [System.Management.Automation.PSCustomObject]) { return $null }
    $entry = $assets.Value.PSObject.Properties[$Name]
    if ($null -eq $entry -or [string]$entry.Value -notmatch '^[0-9a-fA-F]{64}$') { return $null }
    return ([string]$entry.Value).ToLowerInvariant()
}

function Get-ServerHealthUrl {
    # Mirrors the CLI's server URL: a wildcard bind address is reached through
    # loopback, and an IPv6 literal needs brackets.
    param([string]$ServerHost, [int]$ServerPort)
    $connectHost = $ServerHost
    if ($connectHost -in @("", "*", "0.0.0.0")) { $connectHost = "127.0.0.1" }
    elseif ($connectHost -eq "::") { $connectHost = "::1" }
    $connectHost = $connectHost.TrimStart("[").TrimEnd("]")
    if ($connectHost.Contains(":")) { $connectHost = "[$connectHost]" }
    return "http://${connectHost}:$ServerPort/health"
}

function Install-NativeRelease {
    param([string]$Tag, [string]$Shape, [bool]$MainBuild)
    $base = Get-ReleaseDownloadBase -Tag $Tag -MainBuild $MainBuild
    $release = if ($MainBuild) { "the newest main build" } elseif ([string]::IsNullOrWhiteSpace($Tag)) { "the latest release" } else { "release $Tag" }
    $identity = Get-ReleaseIdentity -Base $base -Release $release
    $releaseVersion = [string]$identity.version
    if (-not $MainBuild -and [string]::IsNullOrWhiteSpace($Tag)) {
        # Pin the tag the latest identity names: releases/latest can move to a
        # newer release before the download, which would mix two releases.
        if ($releaseVersion -cnotmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {
            throw "$base/$ReleaseIdentityName names no valid release version."
        }
        $base = "$ReleaseDownloads/download/v$releaseVersion"
    }
    # The installer carries the application version.
    $name = "vBot-$releaseVersion-windows-x86_64-$Shape.exe"
    $label = if ($MainBuild) { "the newest main build ($releaseVersion)" } else { "vBot $releaseVersion" }
    $recordedDigest = Get-RecordedDigest -Identity $identity -Name $name
    if ($null -eq $recordedDigest) {
        throw "$ReleaseIdentityName of $release records no valid SHA-256 digest for $name, so the release does not publish this installer or predates recorded digests; refusing to run it."
    }
    $expectedDigest = $recordedDigest.ToUpperInvariant()
    $installer = Join-Path ([System.IO.Path]::GetTempPath()) ("vbot-{0}-{1}.exe" -f $releaseVersion, $PID)
    try {
        Write-Step "Downloading $label"
        try {
            Save-ReleaseDownload -Uri "$base/$name" -OutFile $installer
        }
        catch {
            throw "Could not download $base/$name ($($_.Exception.Message))."
        }
        $actualDigest = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash
        if ($actualDigest -cne $expectedDigest) {
            throw "The downloaded installer does not match the SHA-256 digest in $ReleaseIdentityName. A newer build may have replaced the release files meanwhile (main builds are replaced in place); run the installer again."
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        if ($signature.Status -ne "NotSigned" -and $signature.Status -ne "Valid") {
            throw "The installer contains an invalid Authenticode signature."
        }
        foreach ($value in @($InstallDir, $DataDir, $HostName)) {
            if ($value -match '["\r\n]') {
                throw "Installer paths and host names cannot contain quotes or newlines."
            }
        }
        Write-Step "Installing $label"
        $tasks = if ($NoAutostart -or $Shape -eq "desktop-client") { "" } else { "startup" }
        $arguments = @(
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/DIR=`"$InstallDir`"",
            "/VBOTDATA=`"$DataDir`"",
            "/VBOTHOST=`"$HostName`"",
            "/VBOTPORT=$Port",
            "/TASKS=$tasks"
        )
        $process = Start-Process -FilePath $installer -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            throw "The vBot installer exited with code $($process.ExitCode)."
        }
    }
    finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
    $statePath = Join-Path $InstallDir "application.json"
    $activePath = Join-Path $InstallDir "active-version"
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf) -or -not (Test-Path -LiteralPath $activePath -PathType Leaf)) {
        throw "The vBot installer exited successfully but did not create a complete installation."
    }
    $state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
    if ([string]$state.install_shape -cne $Shape) {
        throw "The installed vBot shape does not match the requested installer."
    }
    if ($tasks -eq "startup") {
        # The logon task starts vBot only at the next sign-in; start the tray,
        # which starts the server, now.
        Write-Step "Starting vBot"
        Start-Process -FilePath (Join-Path $InstallDir "vBot.exe") -WindowStyle Hidden | Out-Null
        $ready = $false
        for ($attempt = 0; $attempt -lt 30 -and -not $ready; $attempt++) {
            try {
                $health = Invoke-RestMethod -Uri (Get-ServerHealthUrl -ServerHost $state.server_host -ServerPort $state.server_port) -TimeoutSec 1
                $ready = $health.status -eq "ok"
            }
            catch {
                Start-Sleep -Seconds 1
            }
        }
        if (-not $ready) {
            throw "vBot was installed, but its requested server startup could not be verified."
        }
    }
    $following = if ($MainBuild) { "it updates from main builds" } else { "it updates from releases" }
    Write-Status "OK" "$label is installed at $InstallDir; $following."
}

if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    $InstallDir = Join-Path $env:LOCALAPPDATA "Programs\vBot"
}
elseif ($InstallDir -eq "~") {
    $InstallDir = $HOME
}
elseif ($InstallDir.StartsWith("~\") -or $InstallDir.StartsWith("~/")) {
    $InstallDir = Join-Path $HOME $InstallDir.Substring(2)
}
elseif (-not [System.IO.Path]::IsPathRooted($InstallDir)) {
    $InstallDir = Join-Path (Get-Location).ProviderPath $InstallDir
}
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)

if (Test-Path -LiteralPath $InstallDir) {
    throw "$InstallDir already exists. To update an existing installation run 'vBot.exe update' there; otherwise remove it or pass -InstallDir to choose another location."
}
if ((Test-IsElevated) -and -not $AllowElevatedInstall) {
    throw "Refusing to install from an elevated PowerShell because the installation and its runtime files must belong to the normal user. Close this Administrator window and run the installer from a normal PowerShell. -AllowElevatedInstall is reserved for disposable automation."
}

$shape = if ($DesktopClient) { "desktop-client" } elseif ($Desktop) { "server-desktop" } else { "server" }
Install-NativeRelease -Tag $Version -Shape $shape -MainBuild ([bool]$Main)
Remove-Item -LiteralPath $InstallLogPath -Force -ErrorAction SilentlyContinue
# return, unlike exit, keeps an `irm | iex` window open to show the result.
# A caller running this script sees the last native probe's exit code unless the
# success resets it.
$global:LASTEXITCODE = 0
