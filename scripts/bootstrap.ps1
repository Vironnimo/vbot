# vBot one-shot bootstrap for Windows.
#
# Installs prerequisites (Python + git; Node only on the dev track) via winget,
# clones the repo, and hands off to scripts/install.ps1. On the default release
# track it fetches the prebuilt WebUI from the matching GitHub release, so no
# Node is needed. Run it with:
#   irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/bootstrap.ps1 | iex
# To pass options, download and run as a file, or:
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/bootstrap.ps1))) -Dev
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $HOME "vbot"),
    [switch]$Dev,
    [string]$Version = "",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$InstallerArgs = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Dev -and -not [string]::IsNullOrWhiteSpace($Version)) {
    throw "-Version selects a specific release tag and cannot be combined with -Dev."
}
# Accept a bare version (0.1.2) as well as the tag form (v0.1.2).
if (-not [string]::IsNullOrWhiteSpace($Version) -and ($Version -notmatch '^v')) {
    $Version = "v$Version"
}

$RepoOwner = "Vironnimo"
$RepoName = "vbot"
$RepoUrl = "https://github.com/$RepoOwner/$RepoName.git"
$ApiBase = "https://api.github.com/repos/$RepoOwner/$RepoName"
$ApiHeaders = @{ "User-Agent" = "vbot-bootstrap"; "Accept" = "application/vnd.github+json" }

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
    $release = Invoke-RestMethod -Uri "$ApiBase/releases/latest" -Headers $ApiHeaders
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

function Write-BootstrapMarker {
    param([string]$InstallDir)
    # Mark this directory as a self-contained bootstrap install so uninstall.ps1
    # knows it may remove the whole tree (venv + source), not just a pip package.
    $marker = Join-Path $InstallDir ".vbot-bootstrap"
    $lines = @(
        "# vBot bootstrap install marker.",
        "# This directory is a self-contained vBot install created by the bootstrap script",
        "# (it has its own virtual environment in .venv). Running scripts/uninstall.ps1",
        "# (uninstall.sh on Linux) removes this entire directory, the 'vbot' launcher,",
        "# and the autostart task. Your data directory is never touched."
    )
    $content = ($lines -join "`r`n") + "`r`n"
    [System.IO.File]::WriteAllText($marker, $content, (New-Object System.Text.UTF8Encoding($false)))
}

if (Test-Path $InstallDir) {
    throw "$InstallDir already exists. To update an existing install run 'vbot update'; otherwise remove it or pass -InstallDir to choose another location."
}

Confirm-Git
Confirm-Python
if ($Dev) {
    Confirm-Node
}

if ($Dev) {
    Write-Step "Cloning $RepoUrl (main) into $InstallDir"
    git clone --depth 1 $RepoUrl $InstallDir
    if ($LASTEXITCODE -ne 0) {
        throw "git clone failed."
    }
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
    Write-Step "Cloning $RepoUrl ($tag) into $InstallDir"
    git clone --depth 1 --branch $tag $RepoUrl $InstallDir
    if ($LASTEXITCODE -ne 0) {
        throw "git clone failed."
    }

    Write-Step "Fetching prebuilt WebUI for $tag"
    $assetUrl = Get-WebuiAssetUrl -Tag $tag
    if ($null -eq $assetUrl) {
        throw "Release $tag has no webui-dist.tar.gz asset yet. Use -Dev to build locally, or wait for the release workflow to finish."
    }
    $webuiDir = Join-Path $InstallDir "webui"
    New-Item -ItemType Directory -Path $webuiDir -Force | Out-Null
    $archive = Join-Path $InstallDir "webui-dist.tar.gz"
    Invoke-WebRequest -Uri $assetUrl -OutFile $archive -Headers $ApiHeaders
    # Refuse a tarball whose members escape webui/ (mirrors `vbot update`'s data filter).
    $unsafe = & tar -tzf $archive | Where-Object { $_ -match '(^/)|((^|/)\.\.(/|$))' }
    if ($unsafe) {
        Remove-Item $archive -Force
        throw "Refusing to unpack the WebUI archive: it contains unsafe paths."
    }
    tar -xzf $archive -C $webuiDir
    Remove-Item $archive -Force
    if (-not (Test-Path (Join-Path $webuiDir "dist\index.html"))) {
        throw "Prebuilt WebUI did not unpack to webui/dist."
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

$installer = Join-Path $InstallDir "scripts\install.ps1"
$installerArgList = @("-SkipPathUpdate")
if ($Dev) {
    $installerArgList += "-Dev"
}
else {
    $installerArgList += "-SkipWebuiBuild"
}
if ($InstallerArgs) {
    $installerArgList += $InstallerArgs
}

Write-Step "Running installer: install.ps1 $($installerArgList -join ' ')"
& $installer @installerArgList

Add-VbotShim -InstallDir $InstallDir -VenvDir $venvDir
Write-BootstrapMarker -InstallDir $InstallDir

Write-Step "vBot bootstrap complete"
Write-Host "Installed at: $InstallDir (virtual environment in .venv)"
Write-Host "Data dir:     $(Join-Path $HOME '.vbot')"
Write-Host "Open a new terminal, then run: vbot server status"
