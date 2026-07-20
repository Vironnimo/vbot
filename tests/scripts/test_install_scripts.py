"""Non-mutating contract tests for the cross-platform lifecycle scripts."""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHELL_SCRIPTS = (
    PROJECT_ROOT / "scripts" / "install.sh",
    PROJECT_ROOT / "scripts" / "setup.sh",
    PROJECT_ROOT / "scripts" / "uninstall.sh",
)
POWERSHELL_SCRIPTS = (
    PROJECT_ROOT / "scripts" / "install.ps1",
    PROJECT_ROOT / "scripts" / "setup.ps1",
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
            "scripts/setup.sh",
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
            "scripts/setup.sh",
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


def test_linux_public_installer_rejects_conflicting_shapes_before_install(
    tmp_path: Path,
) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    install_dir = tmp_path / "must-not-exist"

    result = subprocess.run(
        [
            bash,
            "scripts/install.sh",
            "--dir",
            str(install_dir),
            "--desktop",
            "--desktop-client",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "mutually exclusive" in result.stderr
    assert not install_dir.exists()


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
    script = (PROJECT_ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")

    assert script.index("function Write-InstallManifest") < script.rindex("Write-InstallManifest")


def test_windows_desktop_shortcut_targets_windowless_gui_entrypoint() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    setup = (PROJECT_ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")

    assert project["gui-scripts"]["vbot-desktop"] == "desktop.main:main"
    assert "$desktopPath = Resolve-DesktopCommandPath $scriptsPath" in setup
    assert "New-DesktopShortcut -TargetPath $desktopPath" in setup
    assert '$shortcut.Arguments = "desktop"' not in setup
    assert '[string]$DesktopShortcutTarget = ""' in setup


def test_linux_install_manifest_records_selected_environment_interpreter() -> None:
    script = (PROJECT_ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")

    assert 'PYTHON_EXECUTABLE="$(command -v "$PYTHON")"' in script


@pytest.mark.parametrize("script_name", ["setup.sh", "setup.ps1"])
def test_server_install_manifest_records_lifecycle_target(script_name: str) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    assert "server-host" in script
    assert "server-port" in script
    assert "server-data-directory" in script


@pytest.mark.parametrize("script_name", ["uninstall.sh", "uninstall.ps1"])
def test_managed_uninstaller_uses_recorded_lifecycle_target(script_name: str) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    assert "server_host" in script
    assert "server_port" in script
    assert "server_data_directory" in script


@pytest.mark.parametrize("script_name", ["uninstall.sh", "uninstall.ps1"])
def test_uninstaller_supports_explicit_data_removal_with_path_guards(script_name: str) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    if script_name.endswith(".sh"):
        assert "--remove-data" in script
        assert "--data-dir" in script
    else:
        assert "RemoveData" in script
        assert "DataDirectory" in script
    assert "Refusing to remove" in script
    assert "data directory" in script.lower()


def test_windows_installer_rejects_dev_with_version_before_install(tmp_path: Path) -> None:
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
            str(PROJECT_ROOT / "scripts" / "install.ps1"),
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


def test_windows_installer_forwards_setup_options_through_powershell() -> None:
    script = (PROJECT_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

    assert "-File $setup @setupArgList" in script
    assert "& $setup @setupArgList" not in script


def test_windows_public_installer_ends_with_verified_lifecycle_summary() -> None:
    script = (PROJECT_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

    summary_start = script.index('Write-Step "Final installation summary"')
    summary = script[summary_start:]

    assert ".vbot-install.json" in summary
    assert "server status --host $summaryHost --port $summaryPort" in summary
    assert "autostart status --host $summaryHost --port $summaryPort" in summary
    assert "$setupReportedProblems" in summary
    assert 'Write-Host "Server: running"' in summary
    assert 'Write-Host "Server: NOT RUNNING"' in summary
    assert 'Write-Host "Problems:"' in summary
    assert "Required next step: open PowerShell as Administrator" in summary
    assert 'Write-Host "Server URL: http://${summaryHost}:$summaryPort"' in summary
    assert summary.index("if ($serverRunning)") < summary.index(
        'Write-Host "Server URL: http://${summaryHost}:$summaryPort"'
    )


def test_windows_checkout_setup_does_not_claim_an_unverified_server_url() -> None:
    script = (PROJECT_ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")

    summary_start = script.index('Write-Step "Checkout setup summary"')
    summary = script[summary_start:]

    assert "server status --host $HostName --port $effectivePort" in summary
    assert 'Write-Host "Server: NOT RUNNING"' in summary
    assert "Required next step: open PowerShell as Administrator" in summary
    assert "exit $RecoverableProblemExitCode" in summary
    assert summary.index("if ($serverRunning)") < summary.index(
        'Write-Host "Server URL: http://${HostName}:$effectivePort"'
    )


def test_public_installers_are_the_only_fresh_install_entrypoints() -> None:
    assert (PROJECT_ROOT / "scripts" / "install.sh").is_file()
    assert (PROJECT_ROOT / "scripts" / "install.ps1").is_file()
    assert not (PROJECT_ROOT / "scripts" / "bootstrap.sh").exists()
    assert not (PROJECT_ROOT / "scripts" / "bootstrap.ps1").exists()


@pytest.mark.parametrize("doc_name", ["README.md", "USAGE.md"])
def test_public_docs_install_only_through_install_files(doc_name: str) -> None:
    document = (PROJECT_ROOT / doc_name).read_text(encoding="utf-8")

    assert "scripts/install.sh" in document
    assert "scripts/install.ps1" in document
    assert "bootstrap.sh" not in document
    assert "bootstrap.ps1" not in document


@pytest.mark.parametrize("script_name", ["install.sh", "install.ps1"])
def test_public_installer_owns_fresh_install_and_calls_internal_setup(script_name: str) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
    setup_reference = "scripts/setup.sh" if script_name.endswith(".sh") else "scripts\\setup.ps1"

    assert "git clone" in script
    assert ".venv" in script
    assert setup_reference in script
    assert ".vbot-install-root" in script


@pytest.mark.parametrize("script_name", ["uninstall.sh", "uninstall.ps1"])
def test_uninstaller_accepts_new_and_legacy_managed_install_markers(script_name: str) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    assert ".vbot-install-root" in script
    assert ".vbot-install-venv" in script
    assert ".vbot-bootstrap" in script


@pytest.mark.parametrize("script_name", ["install.sh", "install.ps1"])
def test_public_installer_can_configure_releases_from_before_setup_rename(
    script_name: str,
) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    assert "legacy" in script.lower()
    assert ".vbot-bootstrap" in script
    if script_name.endswith(".sh"):
        assert "editable pip install" in script
    else:
        assert "function Install-PythonPackage" in script
