"""Non-mutating contract tests for the cross-platform lifecycle scripts."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHELL_SCRIPTS = (
    PROJECT_ROOT / "scripts" / "bootstrap.sh",
    PROJECT_ROOT / "scripts" / "install.sh",
    PROJECT_ROOT / "scripts" / "uninstall.sh",
)
POWERSHELL_SCRIPTS = (
    PROJECT_ROOT / "scripts" / "bootstrap.ps1",
    PROJECT_ROOT / "scripts" / "install.ps1",
    PROJECT_ROOT / "scripts" / "uninstall.ps1",
)


def test_shell_lifecycle_scripts_parse() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")

    result = subprocess.run(
        [
            bash,
            "-n",
            *(path.relative_to(PROJECT_ROOT).as_posix() for path in SHELL_SCRIPTS),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda path: path.name)
def test_shell_lifecycle_help_is_side_effect_free(script: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")

    result = subprocess.run(
        [bash, script.relative_to(PROJECT_ROOT).as_posix(), "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout


def test_linux_installer_rejects_unsafe_service_name_before_data_dir_creation(
    tmp_path: Path,
) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    data_dir = tmp_path / "must-not-exist"

    result = subprocess.run(
        [
            bash,
            "scripts/install.sh",
            "--service-name",
            "../../outside",
            "--data-dir",
            str(data_dir),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "--service-name must start" in result.stderr
    assert not data_dir.exists()


def test_linux_installer_rejects_option_like_service_name(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    data_dir = tmp_path / "must-not-exist"

    result = subprocess.run(
        [
            bash,
            "scripts/install.sh",
            "--service-name",
            "--system",
            "--data-dir",
            str(data_dir),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "must start with a letter or number" in result.stderr
    assert not data_dir.exists()


def test_powershell_lifecycle_scripts_parse() -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    quoted_paths = ",".join(f"'{path}'" for path in POWERSHELL_SCRIPTS)
    command = (
        f"$failed = $false; foreach ($path in @({quoted_paths})) {{ "
        "$errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, "
        "[ref]$errors) | Out-Null; if ($errors.Count -gt 0) { $failed = $true; "
        "$errors | ForEach-Object { Write-Error $_.Message } } }; if ($failed) { exit 1 }"
    )

    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_windows_install_manifest_function_is_defined_before_main_flow() -> None:
    script = (PROJECT_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

    assert script.index("function Write-InstallManifest") < script.index(
        "Write-InstallManifest -Python"
    )


def test_windows_bootstrap_rejects_dev_with_version_before_install(tmp_path: Path) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    install_dir = tmp_path / "must-not-exist"

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(PROJECT_ROOT / "scripts" / "bootstrap.ps1"),
            "-Dev",
            "-Version",
            "v1.0.0",
            "-InstallDir",
            str(install_dir),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "cannot be combined" in (result.stderr + result.stdout)
    assert not install_dir.exists()
