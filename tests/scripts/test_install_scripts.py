"""Non-mutating contract tests for the cross-platform lifecycle scripts."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
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


@pytest.mark.parametrize("script_name", ["setup.sh", "setup.ps1"])
def test_server_setup_delegates_canonical_layout_and_has_no_env_template_body(
    script_name: str,
) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    assert "storage/layout.py" in script.replace("\\", "/")
    assert "OPENAI_API_KEY" not in script
    assert "OPENROUTER_API_KEY" not in script
    assert "ANTHROPIC_API_KEY" not in script


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
def test_fresh_server_install_seeds_global_agent_defaults(script_name: str) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    if script_name.endswith(".sh"):
        assert 'DEFAULT_AGENT_TEMPERATURE="0.1"' in script
        assert 'DEFAULT_AGENT_THINKING_EFFORT="high"' in script
        creation = script[
            script.index('if [ ! -f "$SETTINGS_PATH" ]') : script.index(
                'elif [ "$PORT_PROVIDED" -eq 1 ]'
            )
        ]
        assert '"defaults"' in creation
        assert '"temperature": %s' in creation
        assert '"thinking_effort": "%s"' in creation
        assert '"$DEFAULT_AGENT_TEMPERATURE"' in creation
        assert '"$DEFAULT_AGENT_THINKING_EFFORT"' in creation
    else:
        assert "$DefaultAgentTemperature = 0.1" in script
        assert '$DefaultAgentThinkingEffort = "high"' in script
        creation = script[
            script.index("if (-not (Test-Path $settingsPath))") : script.index(
                "elseif ($SyncPortIntoSettings)"
            )
        ]
        assert "defaults = [ordered]@{" in creation
        assert "temperature = $DefaultAgentTemperature" in creation
        assert "thinking_effort = $DefaultAgentThinkingEffort" in creation


@pytest.mark.parametrize("script_name", ["setup.sh", "setup.ps1"])
def test_existing_settings_do_not_receive_fresh_install_agent_defaults(
    script_name: str,
) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    if script_name.endswith(".sh"):
        existing_settings_path = script[
            script.index('elif [ "$PORT_PROVIDED" -eq 1 ]') : script.index(
                '"$PYTHON" "${PROJECT_ROOT}/core/storage/layout.py"'
            )
        ]
        assert "DEFAULT_AGENT_TEMPERATURE" not in existing_settings_path
        assert "DEFAULT_AGENT_THINKING_EFFORT" not in existing_settings_path
    else:
        existing_settings_path = script[
            script.index("elseif ($SyncPortIntoSettings)") : script.index(
                "Invoke-External $Python @("
            )
        ]
        assert "DefaultAgentTemperature" not in existing_settings_path
        assert "DefaultAgentThinkingEffort" not in existing_settings_path


@pytest.mark.parametrize("script_name", ["setup.sh", "setup.ps1"])
def test_server_install_manifest_records_lifecycle_target(script_name: str) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    assert "server-host" in script
    assert "server-port" in script
    assert "server-data-directory" in script


@pytest.mark.parametrize("script_name", ["uninstall.sh", "uninstall.ps1"])
def test_managed_uninstaller_uses_recorded_lifecycle_target(script_name: str) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    assert "python_executable" in script
    assert "server_host" in script
    assert "server_port" in script
    assert "server_data_directory" in script


@pytest.mark.parametrize("script_name", ["uninstall.sh", "uninstall.ps1"])
def test_uninstaller_guards_desktop_artifact_removal_by_install_shape(
    script_name: str,
) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    assert "install_shape" in script
    assert "server-desktop" in script
    assert "desktop-client" in script
    if script_name.endswith(".sh"):
        assert "if install_owns_desktop_entry; then" in script
        assert 'if [ "$remove_desktop" -eq 1 ]; then' in script
    else:
        assert "$removeDesktopShortcut = Test-InstallOwnsDesktopShortcut" in script
        assert "if ($removeDesktopShortcut)" in script


@pytest.mark.parametrize("script_name", ["uninstall.sh", "uninstall.ps1"])
def test_application_uninstaller_treats_server_stop_as_mandatory(script_name: str) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    assert "best_effort" not in script.lower()
    assert "besteffort" not in script.lower()
    if script_name.endswith(".sh"):
        stop_body = script[script.index("stop_vbot_server()") : script.index("managed_cleanup()")]
        assert 'if [ "$stop_status" -ne 0 ]' in stop_body
        assert '&& [ "$REMOVE_DATA" -eq 1 ]' not in stop_body
    else:
        stop_body = script[
            script.index("function Stop-VbotServer") : script.index(
                "function Invoke-ManagedUninstall"
            )
        ]
        assert "if ($stopExitCode -ne 0)" in stop_body
        assert "if ($RemoveData)" not in stop_body


@pytest.mark.parametrize("script_name", ["uninstall.sh", "uninstall.ps1"])
def test_desktop_client_uninstaller_skips_unowned_server_stop(script_name: str) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    assert "desktop-client" in script
    if script_name.endswith(".sh"):
        assert "install_is_desktop_client()" in script
        assert 'if install_is_desktop_client && [ "$REMOVE_DATA" -eq 0 ]; then' in script
        assert "validate_desktop_client_data_target" in script
        assert "DATA_DIR_EXPLICIT" in script
    else:
        assert "function Test-InstallIsDesktopClient" in script
        assert "if ((Test-InstallIsDesktopClient) -and -not $RemoveData)" in script
        assert '$PSBoundParameters.ContainsKey("DataDirectory")' in script
        assert '$PSBoundParameters.ContainsKey("ServerHost")' in script
        assert '$PSBoundParameters.ContainsKey("ServerPort")' in script


def test_linux_managed_uninstaller_preserves_app_when_server_stop_fails(
    tmp_path: Path,
) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    install_root = tmp_path / "install"
    scripts_dir = install_root / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "scripts" / "uninstall.sh", scripts_dir / "uninstall.sh")
    (install_root / ".vbot-install-root").write_text("managed\n", encoding="utf-8")
    vbot_path = install_root / ".venv" / "bin" / "vbot"
    vbot_path.parent.mkdir(parents=True)
    vbot_path.write_text(
        "#!/usr/bin/env bash\nexit 19\n",
        encoding="utf-8",
    )
    vbot_path.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    environment = {**os.environ, "HOME": "home"}

    result = subprocess.run(
        [
            bash,
            "install/scripts/uninstall.sh",
            "--host",
            "127.0.0.1",
            "--port",
            "8420",
            "--data-dir",
            "data",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert install_root.is_dir()


def test_linux_desktop_client_uninstall_does_not_call_default_server(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("POSIX launcher integration requires a POSIX host")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")

    install_root = tmp_path / "install"
    scripts_dir = install_root / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "scripts" / "uninstall.sh", scripts_dir / "uninstall.sh")
    (install_root / ".vbot-install-root").write_text("managed\n", encoding="utf-8")
    vbot_path = install_root / ".venv" / "bin" / "vbot"
    vbot_path.parent.mkdir(parents=True)
    stop_log = tmp_path / "unexpected-server-stop.txt"
    vbot_path.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > {shlex.quote(str(stop_log))}\n",
        encoding="utf-8",
    )
    vbot_path.chmod(0o755)
    (install_root / ".vbot-install.json").write_text(
        json.dumps(
            {
                "install_shape": "desktop-client",
                "python_executable": sys.executable,
            }
        ),
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    default_data = home / ".vbot"
    default_data.mkdir()
    (default_data / "must-survive").write_text("data\n", encoding="utf-8")
    environment = {**os.environ, "HOME": str(home)}

    unsafe_data_removal = subprocess.run(
        [bash, str(scripts_dir / "uninstall.sh"), "--remove-data"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    result = subprocess.run(
        [bash, str(scripts_dir / "uninstall.sh")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert unsafe_data_removal.returncode != 0
    assert default_data.is_dir()
    assert result.returncode == 0, result.stderr
    assert not stop_log.exists()
    assert not install_root.exists()


def test_linux_uninstaller_resolves_vbot_from_recorded_python_environment(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("POSIX launcher integration requires a POSIX host")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")

    install_root = tmp_path / "install"
    scripts_dir = install_root / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "scripts" / "uninstall.sh", scripts_dir / "uninstall.sh")
    (install_root / ".vbot-install-venv").write_text("managed\n", encoding="utf-8")

    recorded_bin = tmp_path / "recorded-environment" / "bin"
    recorded_bin.mkdir(parents=True)
    recorded_python = recorded_bin / "python"
    recorded_python.write_text(
        f'#!/usr/bin/env bash\nexec {shlex.quote(sys.executable)} "$@"\n',
        encoding="utf-8",
    )
    recorded_python.chmod(0o755)
    stop_log = tmp_path / "server-stop.txt"
    recorded_vbot = recorded_bin / "vbot"
    recorded_vbot.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > {shlex.quote(str(stop_log))}\n",
        encoding="utf-8",
    )
    recorded_vbot.chmod(0o755)

    data_dir = tmp_path / "data"
    manifest = {
        "python_executable": str(recorded_python),
        "server_host": "127.0.0.1",
        "server_port": 8420,
        "server_data_directory": str(data_dir),
    }
    (install_root / ".vbot-install.json").write_text(json.dumps(manifest), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    result = subprocess.run(
        [bash, str(scripts_dir / "uninstall.sh")],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert stop_log.read_text(encoding="utf-8").strip() == (
        f"server stop --host 127.0.0.1 --port 8420 --data-dir {data_dir}"
    )
    assert not (install_root / ".vbot-install.json").exists()
    assert install_root.is_dir()


@pytest.mark.parametrize("script_name", ["uninstall.sh", "uninstall.ps1"])
def test_uninstaller_supports_explicit_data_removal_with_path_guards(script_name: str) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    if script_name.endswith(".sh"):
        assert "--remove-data" in script
        assert "--data-dir" in script
    else:
        assert "RemoveData" in script
        assert "DataDirectory" in script
    if script_name.endswith(".sh"):
        assert "remove_data_directory()" in script
    else:
        assert "function Remove-VbotDataDirectory" in script


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
    assert not install_dir.exists()


def test_windows_installer_forwards_setup_options_through_powershell() -> None:
    script = (PROJECT_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

    assert "-File $setup @setupArgList" in script
    assert "& $setup @setupArgList" not in script


def test_windows_installer_shim_does_not_lock_the_pip_package_launcher() -> None:
    script = (PROJECT_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    shim_start = script.index("function Add-VbotShim")
    shim_end = script.index("function Write-ManagedRootMarker", shim_start)
    shim = script[shim_start:shim_end]

    assert 'Scripts\\python.exe"' in shim
    assert '" -m cli.main %*' in shim
    assert "Scripts\\vbot.exe" not in shim


def test_windows_installer_refuses_accidental_elevation_before_install_mutation() -> None:
    script = (PROJECT_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

    guard = script.index("if ((Test-IsElevated) -and -not $AllowElevatedInstall)")
    assert guard < script.index("Confirm-Git", guard)
    assert guard < script.index("$cloneOutput = @(git clone", guard)


def test_windows_public_installer_ends_with_verified_lifecycle_summary() -> None:
    script = (PROJECT_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

    summary_start = script.index("$summaryHost = $HostName")
    summary = script[summary_start:]

    assert ".vbot-install.json" in summary
    assert "server status --host $summaryHost --port $summaryPort" in summary
    assert "autostart status --host $summaryHost --port $summaryPort" in summary
    assert "$setupReportedProblems" in summary
    ready_guard = summary.index("if ($problems.Count -eq 0 -and $serverRunning)")
    assert summary.index("server status --host $summaryHost --port $summaryPort") < ready_guard
    assert summary.index("autostart status --host $summaryHost --port $summaryPort") < ready_guard
    assert ready_guard < summary.index("http://${summaryHost}:$summaryPort/")


@pytest.mark.parametrize("script_name", ["install.sh", "install.ps1"])
def test_public_installer_routes_setup_details_to_a_failure_log(
    script_name: str,
) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    if script_name.endswith(".sh"):
        assert "--verbose" in script
        assert "read -p" not in script
        assert '>> "$INSTALL_LOG" 2>&1' in script
        assert 'tee -a "$INSTALL_LOG"' in script
    else:
        assert "Read-Host" not in script
        assert '$VerbosePreference -eq "Continue"' in script
        assert "Add-Content -LiteralPath $InstallLogPath" in script
        assert "Write-Host $line" in script


def test_linux_public_installer_verifies_server_and_autostart_before_ready() -> None:
    script = (PROJECT_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    summary_start = script.index("finish_with_summary()")
    summary = script[summary_start:]

    assert 'capture_command "$vbot_path" server status' in summary
    assert 'capture_command "$vbot_path" autostart status' in summary
    ready_guard = summary.index('if [ "$server_running" -eq 1 ]')
    assert summary.index('capture_command "$vbot_path" server status') < ready_guard
    assert summary.index('capture_command "$vbot_path" autostart status') < ready_guard
    assert ready_guard < summary.index("http://${summary_host}:${summary_port}/", ready_guard)


def test_windows_checkout_setup_does_not_claim_an_unverified_server_url() -> None:
    script = (PROJECT_ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")

    summary_start = script.index("$statusOutput = @(& $vbotPath server status")
    summary = script[summary_start:]

    assert "server status --host $HostName --port $effectivePort" in summary
    assert "exit $RecoverableProblemExitCode" in summary
    assert summary.index("if ($serverRunning)") < summary.index("http://${HostName}:$effectivePort")


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

    assert ".vbot-bootstrap" in script
    if script_name.endswith(".sh"):
        assert "editable pip install" in script
    else:
        assert "function Install-PythonPackage" in script
