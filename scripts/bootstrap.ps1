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
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$InstallerArgs = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

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

if (Test-Path $InstallDir) {
    throw "$InstallDir already exists. Remove it or pass -InstallDir to choose another location."
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
    $tag = Get-LatestTag
    if ([string]::IsNullOrWhiteSpace($tag)) {
        throw "Could not determine the latest release. Use -Dev to install from main."
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
    tar -xzf $archive -C $webuiDir
    Remove-Item $archive -Force
    if (-not (Test-Path (Join-Path $webuiDir "dist\index.html"))) {
        throw "Prebuilt WebUI did not unpack to webui/dist."
    }
}

$installer = Join-Path $InstallDir "scripts\install.ps1"
$installerArgList = @()
if (-not $Dev) {
    $installerArgList += "-SkipWebuiBuild"
}
if ($InstallerArgs) {
    $installerArgList += $InstallerArgs
}

Write-Step "Running installer: install.ps1 $($installerArgList -join ' ')"
& $installer @installerArgList

Write-Step "vBot bootstrap complete"
Write-Host "Installed at: $InstallDir"
Write-Host "Data dir:     $(Join-Path $HOME '.vbot')"
