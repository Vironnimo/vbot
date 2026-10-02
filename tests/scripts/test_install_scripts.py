"""Contract tests for the public installers, without installing anything."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTALL_PS1 = PROJECT_ROOT / "scripts" / "install.ps1"
INSTALL_SH = PROJECT_ROOT / "scripts" / "install.sh"

# PowerShell that dot-sources the install.ps1 functions named in $Functions from
# the script at $Source, so a harness can exercise them without running the
# installer.
_LOAD_INSTALLER_FUNCTIONS = r"""
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$null, [ref]$null)
foreach ($name in $Functions) {
    $node = $ast.FindAll({
        param($item)
        $item -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $item.Name -eq $name
    }, $false) | Select-Object -First 1
    . ([scriptblock]::Create($node.Extent.Text))
}
"""


def _powershell() -> str:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    return powershell


def _run_powershell(tmp_path: Path, script: str, *arguments: str) -> Any:
    """Run a harness and return its JSON result."""
    harness = tmp_path / "harness.ps1"
    harness.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-File", str(harness), *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_public_installers_parse_and_install_sh_help_is_side_effect_free() -> None:
    command = (
        "$errors = $null; [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{INSTALL_PS1}', [ref]$null, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
    )
    parsed = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert parsed.returncode == 0, parsed.stderr
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    script = INSTALL_SH.relative_to(PROJECT_ROOT).as_posix()
    for arguments in (["-n", script], [script, "--help"]):
        result = subprocess.run(
            [bash, *arguments], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr


_NATIVE_MODES = (
    "release",
    "main",
    "version",
    "no-autostart",
    "desktop-client",
    "missing",
    "no-digest",
    "unsafe-version",
    "digest",
    "signature",
)


def test_windows_installer_runs_only_the_verified_installer_of_the_selected_channel(
    tmp_path: Path,
) -> None:
    payloads = _run_powershell(
        tmp_path,
        r"""param($Source, $Root, $Modes)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ReleaseDownloads = "https://github.com/Vironnimo/vbot/releases"
$ReleaseIdentityName = "vbot-release.json"
$HostName = "127.0.0.1"
$Port = 9134
$ProgressPreference = "SilentlyContinue"
$Functions = @(
    "Get-ReleaseDownloadBase", "Save-ReleaseDownload", "Get-ReleaseIdentity",
    "Get-RecordedDigest", "Get-ServerHealthUrl", "Install-NativeRelease"
)
"""
        + _LOAD_INSTALLER_FUNCTIONS
        + r"""
function Write-Status { param($State, $Message) $script:statuses += "${State}:$Message" }
function Write-Step { param($Message) }
$bytes = [System.Text.Encoding]::UTF8.GetBytes("verified package")
$hasher = New-Object System.Security.Cryptography.SHA256Managed
$sha = [BitConverter]::ToString($hasher.ComputeHash($bytes)).Replace("-", "").ToLowerInvariant()
$hasher.Dispose()
function Invoke-RestMethod {
    param($Uri, $Headers, $TimeoutSec)
    if ($Uri -notlike "*/health") { throw "unexpected request $Uri" }
    $script:healthCalls++
    return [pscustomobject]@{status="ok"}
}
# Serves the release downloads: the identity and the installer it names.
function Invoke-WebRequest {
    param($Uri, $OutFile, [switch]$UseBasicParsing)
    $script:downloads += $Uri
    if ($Uri -like "*/vbot-release.json") {
        if ($Mode -eq "missing") { throw "404 Not Found" }
        $assets = @{"vbot-windows-x86_64-$Shape.zip" = $sha}
        if ($Mode -ne "no-digest") { $assets["vBot-1.2.3-windows-x86_64-$Shape.exe"] = $sha }
        $version = if ($Mode -eq "unsafe-version") { "1.2.3-x" } else { "1.2.3" }
        $identity = @{schema_version=1; version=$version; assets=$assets}
        [IO.File]::WriteAllText($OutFile, (ConvertTo-Json -InputObject $identity -Depth 3))
        return
    }
    [IO.File]::WriteAllBytes($OutFile, $bytes)
}
function Get-AuthenticodeSignature {
    param($LiteralPath)
    $status = if ($Mode -eq "signature") { "HashMismatch" } else { "NotSigned" }
    return [pscustomobject]@{Status=$status}
}
function Get-FileHash {
    param($LiteralPath, $Algorithm)
    $value = if ($Mode -eq "digest") { "0" * 64 } else { $sha }
    return [pscustomobject]@{Hash=$value.ToUpperInvariant()}
}
function Start-Process {
    param($FilePath, $ArgumentList, $WindowStyle, [switch]$Wait, [switch]$PassThru)
    if ($WindowStyle -ne "Hidden") { throw "window was not hidden" }
    if ($FilePath -eq (Join-Path $InstallDir "vBot.exe")) {
        if ($null -ne $ArgumentList -or $Wait) { throw "the tray must start detached" }
        $script:trayStarts++
        return
    }
    $script:calls = @($ArgumentList)
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    @{schema_version=1; install_shape=$Shape; server_host=$HostName; server_port=$Port} |
        ConvertTo-Json | Set-Content (Join-Path $InstallDir "application.json")
    Set-Content (Join-Path $InstallDir "active-version") "rel_test"
    return [pscustomobject]@{ExitCode=0}
}
# One process runs every mode against its own installation directory.
$results = @{}
foreach ($Mode in ($Modes -split ",")) {
    $InstallDir = Join-Path $Root $Mode
    $DataDir = Join-Path $InstallDir "data"
    $NoAutostart = $Mode -eq "no-autostart"
    $Shape = if ($Mode -eq "desktop-client") { "desktop-client" } else { "server" }
    $calls = @(); $statuses = @(); $healthCalls = 0; $trayStarts = 0; $downloads = @()
    $failure = $null
    $tag = if ($Mode -eq "version") { "v1.2.3" } else { "" }
    try { Install-NativeRelease -Tag $tag -Shape $Shape -MainBuild ($Mode -eq "main") }
    catch { $failure = $_.Exception.Message }
    $results[$Mode] = @{ok=($null -eq $failure); error=$failure; calls=$calls
        statuses=$statuses; healthCalls=$healthCalls; trayStarts=$trayStarts
        downloads=$downloads}
}
$results | ConvertTo-Json -Compress -Depth 5
""",
        str(INSTALL_PS1),
        str(tmp_path / "install with spaces"),
        ",".join(_NATIVE_MODES),
    )

    assert set(payloads) == set(_NATIVE_MODES)
    # Only release downloads, never the GitHub API. The latest release's installer
    # comes from the tag its identity names, so a release published meanwhile
    # cannot mix in.
    downloads = "https://github.com/Vironnimo/vbot/releases"
    tagged = f"{downloads}/download/v1.2.3"
    bases = {"main": (f"{downloads}/download/main-build",) * 2, "version": (tagged, tagged)}
    for mode in ("release", "main", "version", "no-autostart", "desktop-client"):
        payload = payloads[mode]
        install_dir = tmp_path / "install with spaces" / mode
        assert payload["ok"] is True, mode
        identity_base, installer_base = bases.get(mode, (f"{downloads}/latest/download", tagged))
        shape = "desktop-client" if mode == "desktop-client" else "server"
        assert payload["downloads"] == [
            f"{identity_base}/vbot-release.json",
            f"{installer_base}/vBot-1.2.3-windows-x86_64-{shape}.exe",
        ], mode
        starts = mode in {"release", "main", "version"}
        assert payload["calls"] == [
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            f'/DIR="{install_dir}"',
            f'/VBOTDATA="{install_dir / "data"}"',
            '/VBOTHOST="127.0.0.1"',
            "/VBOTPORT=9134",
            "/TASKS=startup" if starts else "/TASKS=",
        ], mode
        # Autostart also starts the tray now, and only then is its server awaited.
        assert payload["trayStarts"] == (1 if starts else 0), mode
        assert payload["healthCalls"] == (1 if starts else 0), mode
        (status,) = payload["statuses"]
        assert status.startswith("OK:"), mode
        assert ("main builds" if mode == "main" else "releases") in status, mode
    assert "vbot-release.json" in payloads["missing"]["error"]
    assert "records no valid SHA-256 digest" in payloads["no-digest"]["error"]
    # The version becomes the tag of the installer's download URL.
    assert "names no valid release version" in payloads["unsafe-version"]["error"]
    assert "does not match the SHA-256 digest" in payloads["digest"]["error"]
    assert "invalid Authenticode signature" in payloads["signature"]["error"]
    # Without a verified digest the installer is never downloaded or run.
    for mode in ("missing", "no-digest", "unsafe-version"):
        assert len(payloads[mode]["downloads"]) == 1, mode
    for mode in ("missing", "no-digest", "unsafe-version", "digest", "signature"):
        assert payloads[mode]["calls"] == [], mode


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell installer")
def test_windows_installer_probes_health_like_the_cli(tmp_path: Path) -> None:
    from cli._server_target import build_server_base_url

    hosts = ["127.0.0.1", "0.0.0.0", "", "*", "::", "::1", "[::1]", "localhost", "192.0.2.5"]
    payload = _run_powershell(
        tmp_path,
        r"""param($Source, $Hosts)
$ErrorActionPreference = "Stop"
$Functions = @("Get-ServerHealthUrl")
"""
        + _LOAD_INSTALLER_FUNCTIONS
        + r"""
$urls = @(($Hosts | ConvertFrom-Json) | ForEach-Object {
    Get-ServerHealthUrl -ServerHost $_ -ServerPort 8420
})
ConvertTo-Json -InputObject $urls -Compress
""",
        str(INSTALL_PS1),
        json.dumps(hosts),
    )

    assert payload == [f"{build_server_base_url(host, 8420)}/health" for host in hosts]


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell installer")
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ("-Desktop -DesktopClient", "-Desktop and -DesktopClient are mutually exclusive."),
        (
            "-Main -Version 1.2.3",
            "-Version selects a specific release and cannot be combined with -Main.",
        ),
        # The tag becomes part of the release download URL.
        ("-Version 1.2.3/../x", "-Version needs a release version such as 0.2.0 or v0.2.0."),
    ],
)
def test_windows_installer_failure_returns_to_a_script_block_caller(
    tmp_path: Path, arguments: str, message: str
) -> None:
    command = (
        "try { & ([scriptblock]::Create((Get-Content -Raw -Encoding UTF8 "
        f"-LiteralPath $env:VBOT_TEST_INSTALLER))) {arguments} }} "
        "catch { 'caught: ' + $_.Exception.Message }; 'caller still running'"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "TEMP": str(tmp_path),
            "TMP": str(tmp_path),
            "VBOT_TEST_INSTALLER": str(INSTALL_PS1),
        },
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f"caught: {message}", "caller still running"]
    assert f"[ERROR] vBot installation failed: {message}" in result.stderr
    (log,) = tmp_path.glob("vbot-install-*.log")
    assert f"Error: {message}" in log.read_text(encoding="utf-8")


@pytest.mark.parametrize("doc_name", ["README.md", "USAGE.md"])
def test_public_docs_install_only_through_install_files(doc_name: str) -> None:
    document = (PROJECT_ROOT / doc_name).read_text(encoding="utf-8")

    assert "scripts/install.sh" in document
    assert "scripts/install.ps1" in document
