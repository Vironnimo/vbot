# vBot installer for Windows.
#
# Installs prerequisites, selects a release or the current checkout, creates an
# isolated virtual environment, and hands off to the internal scripts/setup.ps1.
# Release installs fetch the matching prebuilt WebUI, so the target needs no Node.
#   irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1 | iex
# To pass options, download and run as a file, or:
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1))) -Dev
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $HOME "vbot"),
    [switch]$Dev,
    [string]$Version = "",
    [string]$DataDir = (Join-Path $HOME ".vbot"),
    [string]$HostName = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 8420,
    [switch]$Desktop,
    [switch]$DesktopClient,
    [switch]$NoAutostart,
    [switch]$SkipWebuiBuild,
    [string]$TaskName = "vBot"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Dev -and -not [string]::IsNullOrWhiteSpace($Version)) {
    throw "-Version selects a specific release tag and cannot be combined with -Dev."
}
if ($Desktop -and $DesktopClient) {
    throw "-Desktop and -DesktopClient are mutually exclusive."
}
if ($DesktopClient -and $Dev) {
    throw "-DesktopClient and -Dev are mutually exclusive."
}
# Accept a bare version (0.1.2) as well as the tag form (v0.1.2).
if (-not [string]::IsNullOrWhiteSpace($Version) -and ($Version -notmatch '^v')) {
    $Version = "v$Version"
}

$RepoOwner = "Vironnimo"
$RepoName = "vbot"
$RepoUrl = "https://github.com/$RepoOwner/$RepoName.git"
$ApiBase = "https://api.github.com/repos/$RepoOwner/$RepoName"
$ApiHeaders = @{ "User-Agent" = "vbot-installer"; "Accept" = "application/vnd.github+json" }
$AssetWaitSeconds = 300
$AssetPollSeconds = 10
$RootMarkerName = ".vbot-install-root"
$VenvMarkerName = ".vbot-install-venv"
$LegacyRootMarkerName = ".vbot-bootstrap"

function Write-Step { param([string]$Message) Write-Host "==> $Message" }

function Test-Have {
    param([string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Update-SessionPath {
    $machine = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($machine, $user) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    $env:Path = $parts -join [System.IO.Path]::PathSeparator
}

function Install-WithWinget {
    param([string]$Id, [string]$Label)
    if (-not (Test-Have "winget")) {
        throw "$Label is required but not found, and winget is unavailable to install it automatically. Install $Label manually and re-run."
    }
    Write-Step "Installing $Label via winget ($Id)"
    winget install --id $Id --exact --silent --accept-package-agreements --accept-source-agreements
    Update-SessionPath
}

function Test-PythonOk {
    if (-not (Test-Have "python")) {
        return $false
    }
    & python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Confirm-Python {
    if (Test-PythonOk) {
        return
    }
    Install-WithWinget -Id "Python.Python.3.12" -Label "Python 3.12"
    if (-not (Test-PythonOk)) {
        throw "Python was installed but isn't usable in this session. Open a new terminal and re-run."
    }
}

function Confirm-Git {
    if (Test-Have "git") {
        return
    }
    Install-WithWinget -Id "Git.Git" -Label "Git"
    if (-not (Test-Have "git")) {
        throw "Git was installed but isn't on PATH in this session. Open a new terminal and re-run."
    }
}

function Confirm-Node {
    if ((Test-Have "node") -and (Test-Have "npm")) {
        return
    }
    Install-WithWinget -Id "OpenJS.NodeJS.LTS" -Label "Node.js LTS"
    if (-not ((Test-Have "node") -and (Test-Have "npm"))) {
        throw "Node.js was installed but isn't on PATH in this session. Open a new terminal and re-run."
    }
}

function Get-LatestTag {
    # A repo without releases answers 404 here; surface the -Dev hint instead of
    # the raw API error (mirrors install.sh).
    try {
        $release = Invoke-RestMethod -Uri "$ApiBase/releases/latest" -Headers $ApiHeaders
    }
    catch {
        throw "Could not determine the latest release ($($_.Exception.Message)). Use -Dev to install from main."
    }
    return $release.tag_name
}

function Get-WebuiAssetUrl {
    param([string]$Tag)
    $release = Invoke-RestMethod -Uri "$ApiBase/releases/tags/$Tag" -Headers $ApiHeaders
    $asset = $release.assets | Where-Object { $_.name -eq "webui-dist.tar.gz" } | Select-Object -First 1
    if ($null -eq $asset) {
        return $null
    }
    return $asset.browser_download_url
}

function Wait-WebuiAssetUrl {
    param([string]$Tag)

    $deadline = [DateTime]::UtcNow.AddSeconds($AssetWaitSeconds)
    do {
        try {
            $url = Get-WebuiAssetUrl -Tag $Tag
            if (-not [string]::IsNullOrWhiteSpace($url)) {
                return $url
            }
        }
        catch {
            throw "Could not query release $Tag while waiting for its WebUI asset: $($_.Exception.Message)"
        }
        if ([DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Seconds $AssetPollSeconds
        }
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "Release $Tag still has no webui-dist.tar.gz asset after $AssetWaitSeconds seconds. The install directory was not created; re-run once the release workflow finishes."
}

function Add-ToUserPath {
    param([string]$PathToAdd)
    $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $target = $PathToAdd.TrimEnd('\', '/')
    if (-not [string]::IsNullOrWhiteSpace($userPath)) {
        foreach ($entry in ($userPath -split [System.IO.Path]::PathSeparator)) {
            if (-not [string]::IsNullOrWhiteSpace($entry) -and ($entry.TrimEnd('\', '/') -ieq $target)) {
                return
            }
        }
    }
    $updated = if ([string]::IsNullOrWhiteSpace($userPath)) {
        $PathToAdd
    }
    else {
        "$userPath$([System.IO.Path]::PathSeparator)$PathToAdd"
    }
    [System.Environment]::SetEnvironmentVariable("Path", $updated, "User")
    Write-Host "Added $PathToAdd to your user PATH. Open a new terminal to use 'vbot'."
}

function Add-VbotShim {
    param([string]$InstallDir, [string]$VenvDir)
    $binDir = Join-Path $InstallDir "bin"
    New-Item -ItemType Directory -Path $binDir -Force | Out-Null
    $vbotExe = Join-Path $VenvDir "Scripts\vbot.exe"
    $shim = Join-Path $binDir "vbot.cmd"
    # Expose only vbot, so the venv's python/pip do not shadow the user's.
    $content = "@echo off`r`n`"$vbotExe`" %*`r`n"
    [System.IO.File]::WriteAllText($shim, $content, (New-Object System.Text.UTF8Encoding($false)))
    Write-Step "Exposing 'vbot' via $shim"
    Add-ToUserPath -PathToAdd $binDir
}

function Write-ManagedRootMarker {
    param([string]$InstallDir)
    # Mark this directory as a self-contained managed install so uninstall.ps1
    # knows it may remove the whole tree (venv + source), not just a pip package.
    # Written right after the clone, so an installer that fails mid-install still
    # leaves a marked tree that uninstall.ps1 can remove wholesale.
    $marker = Join-Path $InstallDir $RootMarkerName
    $lines = @(
        "# vBot managed install marker.",
        "# This directory is a self-contained vBot install created by scripts/install.ps1",
        "# (it has its own virtual environment in .venv). Running scripts/uninstall.ps1",
        "# (uninstall.sh on Linux) removes this entire directory, the 'vbot' launcher,",
        "# and the autostart task. Your data directory is never touched."
    )
    $content = ($lines -join "`r`n") + "`r`n"
    [System.IO.File]::WriteAllText($marker, $content, (New-Object System.Text.UTF8Encoding($false)))

    # Releases published before install.ps1 became the public entrypoint contain
    # an uninstaller that recognizes only the retired marker. Write it only for
    # those checkouts so installing an older explicit/latest tag still uninstalls
    # completely with the Uninstaller bundled in that tag.
    $uninstaller = Join-Path $InstallDir "scripts\uninstall.ps1"
    $supportsCurrentMarker = (Test-Path -LiteralPath $uninstaller -PathType Leaf) -and ((Get-Content -Raw -LiteralPath $uninstaller) -match '\.vbot-install-root')
    if (-not $supportsCurrentMarker) {
        $legacyMarker = Join-Path $InstallDir $LegacyRootMarkerName
        $legacyContent = "# Compatibility marker for a vBot release with the previous Uninstaller contract.`r`n"
        [System.IO.File]::WriteAllText($legacyMarker, $legacyContent, (New-Object System.Text.UTF8Encoding($false)))
    }
}

function Write-ManagedVenvMarker {
    param([string]$InstallDir)
    $marker = Join-Path $InstallDir $VenvMarkerName
    $lines = @(
        "# vBot managed virtual-environment marker.",
        "# scripts/install.ps1 created .venv in this existing checkout. Uninstall removes",
        "# the managed environment and launcher but preserves the checkout and data."
    )
    $content = ($lines -join "`r`n") + "`r`n"
    [System.IO.File]::WriteAllText($marker, $content, (New-Object System.Text.UTF8Encoding($false)))
}

function Expand-WebuiArchive {
    param(
        [string]$Archive,
        [string]$Destination
    )

    $extractScript = @'
import sys
import tarfile
from pathlib import Path

archive_path = Path(sys.argv[1])
destination = Path(sys.argv[2])
root = destination.resolve()
with tarfile.open(archive_path, mode='r:gz') as archive:
    for member in archive.getmembers():
        if not (member.isdir() or member.isfile()):
            raise SystemExit(f'unsafe member type in WebUI archive: {member.name}')
        target = (destination / member.name).resolve()
        if not target.is_relative_to(root):
            raise SystemExit(f'unsafe path in WebUI archive: {member.name}')
    archive.extractall(destination)
'@
    & python -c $extractScript $Archive $Destination
    if ($LASTEXITCODE -ne 0) {
        throw "Refusing to unpack the WebUI archive: it contains an unsafe path or member type."
    }
}

$installDirWasProvided = $PSBoundParameters.ContainsKey("InstallDir")
$localCheckout = $null
if (-not [string]::IsNullOrWhiteSpace($PSScriptRoot)) {
    $candidateRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
    if (
        (Test-Path -LiteralPath (Join-Path $candidateRoot "pyproject.toml") -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $candidateRoot "scripts\setup.ps1") -PathType Leaf)
    ) {
        $localCheckout = $candidateRoot
    }
}
$useExistingCheckout = (
    $null -ne $localCheckout -and
    -not $installDirWasProvided -and
    [string]::IsNullOrWhiteSpace($Version)
)
if ($useExistingCheckout) {
    $InstallDir = $localCheckout
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

if ($useExistingCheckout) {
    if (
        -not (Test-Path -LiteralPath (Join-Path $InstallDir "pyproject.toml") -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $InstallDir "scripts\setup.ps1") -PathType Leaf)
    ) {
        throw "The current checkout is incomplete: $InstallDir."
    }
}
elseif (Test-Path -LiteralPath $InstallDir) {
    throw "$InstallDir already exists. To update an existing install run 'vbot update'; otherwise remove it or pass -InstallDir to choose another location."
}

if (-not $useExistingCheckout) {
    Confirm-Git
}
Confirm-Python
if (-not $DesktopClient -and -not $SkipWebuiBuild -and ($Dev -or $useExistingCheckout)) {
    Confirm-Node
}

if ($useExistingCheckout) {
    $rootMarker = Join-Path $InstallDir $RootMarkerName
    $legacyRootMarker = Join-Path $InstallDir $LegacyRootMarkerName
    $venvMarker = Join-Path $InstallDir $VenvMarkerName
    if (-not (Test-Path -LiteralPath $rootMarker) -and -not (Test-Path -LiteralPath $legacyRootMarker) -and -not (Test-Path -LiteralPath $venvMarker)) {
        Write-ManagedVenvMarker -InstallDir $InstallDir
    }
}
elseif ($Dev) {
    Write-Step "Cloning $RepoUrl (main) into $InstallDir"
    git clone --depth 1 $RepoUrl $InstallDir
    if ($LASTEXITCODE -ne 0) {
        throw "git clone failed."
    }
    Write-ManagedRootMarker -InstallDir $InstallDir
}
else {
    if (-not [string]::IsNullOrWhiteSpace($Version)) {
        $tag = $Version
    }
    else {
        $tag = Get-LatestTag
        if ([string]::IsNullOrWhiteSpace($tag)) {
            throw "Could not determine the latest release. Use -Dev to install from main."
        }
    }
    $assetUrl = $null
    if (-not $DesktopClient) {
        Write-Step "Waiting for the prebuilt WebUI for $tag"
        $assetUrl = Wait-WebuiAssetUrl -Tag $tag
    }

    Write-Step "Cloning $RepoUrl ($tag) into $InstallDir"
    git clone --depth 1 --branch $tag $RepoUrl $InstallDir
    if ($LASTEXITCODE -ne 0) {
        throw "git clone failed."
    }
    Write-ManagedRootMarker -InstallDir $InstallDir

    if (-not $DesktopClient) {
        Write-Step "Fetching prebuilt WebUI for $tag"
        $webuiDir = Join-Path $InstallDir "webui"
        New-Item -ItemType Directory -Path $webuiDir -Force | Out-Null
        $archive = Join-Path $InstallDir "webui-dist.tar.gz"
        Invoke-WebRequest -Uri $assetUrl -OutFile $archive -Headers $ApiHeaders
        try {
            Expand-WebuiArchive -Archive $archive -Destination $webuiDir
        }
        finally {
            Remove-Item $archive -Force
        }
        if (-not (Test-Path (Join-Path $webuiDir "dist\index.html"))) {
            throw "Prebuilt WebUI did not unpack to webui/dist."
        }
    }
}

Write-Step "Creating virtual environment at $InstallDir\.venv"
$venvDir = Join-Path $InstallDir ".venv"
& python -m venv $venvDir
if ($LASTEXITCODE -ne 0) {
    throw "Creating the virtual environment failed."
}
# Put the venv first on PATH so the installer installs into it (mirrors `source activate`).
$env:VIRTUAL_ENV = $venvDir
$env:PATH = "$(Join-Path $venvDir 'Scripts')$([System.IO.Path]::PathSeparator)$env:PATH"

$setup = Join-Path $InstallDir "scripts\setup.ps1"
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) {
    $legacySetup = Join-Path $InstallDir "scripts\install.ps1"
    $legacySetupContent = if (Test-Path -LiteralPath $legacySetup -PathType Leaf) { Get-Content -Raw -LiteralPath $legacySetup } else { "" }
    if (-not $useExistingCheckout -and $legacySetupContent -match 'function Install-PythonPackage') {
        $setup = $legacySetup
        Write-Step "Using the checkout installer contract from release $tag"
    }
    else {
        throw "The selected checkout has no usable internal setup script."
    }
}
$setupArgList = @("-SkipPathUpdate")
if ($Dev) {
    $setupArgList += "-Dev"
}
elseif (-not $useExistingCheckout) {
    $setupArgList += "-SkipWebuiBuild"
}
if ($SkipWebuiBuild -and $setupArgList -notcontains "-SkipWebuiBuild") {
    $setupArgList += "-SkipWebuiBuild"
}
if ($PSBoundParameters.ContainsKey("DataDir")) {
    $setupArgList += @("-DataDir", $DataDir)
}
if ($PSBoundParameters.ContainsKey("HostName")) {
    $setupArgList += @("-HostName", $HostName)
}
if ($PSBoundParameters.ContainsKey("Port")) {
    $setupArgList += @("-Port", "$Port")
}
if ($Desktop) {
    $setupArgList += "-Desktop"
}
if ($DesktopClient) {
    $setupArgList += "-DesktopClient"
}
if ($NoAutostart) {
    $setupArgList += "-NoAutostart"
}
if ($PSBoundParameters.ContainsKey("TaskName")) {
    $setupArgList += @("-TaskName", $TaskName)
}

$setupLabel = "scripts\$(Split-Path -Leaf $setup)"
Write-Step "Configuring checkout: $setupLabel $($setupArgList -join ' ')"
$powerShellExecutable = if ($PSVersionTable.PSEdition -eq "Core") {
    Join-Path $PSHOME "pwsh.exe"
}
else {
    Join-Path $PSHOME "powershell.exe"
}
# Array splatting into another PowerShell script binds entries positionally, so
# option names such as -SkipPathUpdate can become values for unrelated parameters.
# A child PowerShell process parses the forwarded tokens as real named arguments.
& $powerShellExecutable -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $setup @setupArgList
if ($LASTEXITCODE -ne 0) {
    throw "The vBot checkout setup failed with exit code $LASTEXITCODE."
}

Add-VbotShim -InstallDir $InstallDir -VenvDir $venvDir

Write-Step "vBot installation complete"
Write-Host "Installed at: $InstallDir (virtual environment in .venv)"
Write-Host "The installer output above shows the configured data directory (Desktop Client has none)."
if ($DesktopClient) {
    Write-Host "Open a new terminal, then run: vbot desktop"
}
else {
    Write-Host "Open a new terminal, then run: vbot server status"
}
