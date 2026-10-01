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
from typing import Any

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


def _run_windows_powershell(tmp_path: Path, script: str, *arguments: str) -> object:
    """Run a harness in Windows PowerShell 5.1 and return its JSON result."""
    harness = tmp_path / "harness.ps1"
    harness.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(harness), *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_windows_installer_recognizes_current_and_legacy_webui_archive_layouts() -> None:
    script = (PROJECT_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

    assert "has_current_layout" in script
    assert "has_legacy_layout" in script
    assert "resources/extensions/" in script
    assert "destination / 'webui'" in script
    assert "unsafe path in WebUI archive" in script
    assert "unexpected path in WebUI archive" in script


@pytest.mark.parametrize("script_name", ["setup.sh", "setup.ps1"])
def test_server_setup_delegates_canonical_layout_and_has_no_env_template_body(
    script_name: str,
) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    assert "storage/layout.py" in script.replace("\\", "/")
    assert "OPENAI_API_KEY" not in script
    assert "OPENROUTER_API_KEY" not in script
    assert "ANTHROPIC_API_KEY" not in script


@pytest.mark.parametrize("script_name", ["setup.sh", "setup.ps1"])
def test_server_setup_initializes_layout_before_writing_fresh_settings(
    script_name: str,
) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    layout_reference = (
        "core\\storage\\layout.py" if script_name.endswith(".ps1") else "core/storage/layout.py"
    )
    fresh_settings_branch = (
        "if ($settingsWasMissing) {"
        if script_name.endswith(".ps1")
        else 'if [ "$settings_was_missing" -eq 1 ]; then'
    )

    assert script.index(layout_reference) < script.index(fresh_settings_branch)


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda path: path.name)
def test_shell_lifecycle_scripts_parse(script: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")

    # One script per invocation: `bash -n a b` parses only `a` and treats the
    # remaining arguments as positional parameters.
    result = subprocess.run(
        [bash, "-n", script.relative_to(PROJECT_ROOT).as_posix()],
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
    assert '$DesktopIconPath = Join-Path $ProjectRoot "desktop\\icon.ico"' in setup
    assert '$shortcut.IconLocation = "$DesktopIconPath,0"' in setup
    assert '$shortcut.Arguments = "desktop"' not in setup
    assert '[string]$DesktopShortcutTarget = ""' in setup


def test_linux_install_manifest_records_selected_environment_interpreter() -> None:
    script = (PROJECT_ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")

    assert 'PYTHON_EXECUTABLE="$(command -v "$PYTHON")"' in script


@pytest.mark.parametrize("script_name", ["setup.sh", "setup.ps1"])
def test_fresh_server_install_seeds_thinking_effort_only(script_name: str) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    if script_name.endswith(".sh"):
        assert 'DEFAULT_AGENT_THINKING_EFFORT="high"' in script
        assert "DEFAULT_AGENT_TEMPERATURE" not in script
        creation = script[
            script.index('if [ ! -f "$SETTINGS_PATH" ]') : script.index(
                'elif [ "$PORT_PROVIDED" -eq 1 ]'
            )
        ]
        assert '"defaults"' in creation
        assert '"temperature"' not in creation
        assert '"thinking_effort": "%s"' in creation
        assert '"$DEFAULT_AGENT_THINKING_EFFORT"' in creation
    else:
        assert '$DefaultAgentThinkingEffort = "high"' in script
        assert "DefaultAgentTemperature" not in script
        creation = script[
            script.index("if ($settingsWasMissing) {") : script.index(
                "elseif ($SyncPortIntoSettings)"
            )
        ]
        assert "defaults = [ordered]@{" in creation
        assert "temperature" not in creation
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
def test_existing_settings_port_update_is_locked_and_atomic(script_name: str) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    assert "os.O_CREAT | os.O_EXCL | os.O_WRONLY" in script
    assert "handle.flush()" in script
    assert "os.fsync(handle.fileno())" in script
    assert "os.replace(temp_path, path)" in script
    assert "temp_path.unlink(missing_ok=True)" in script
    assert "lock_path.unlink(missing_ok=True)" in script


@pytest.mark.parametrize(
    ("script_name", "start", "end"),
    [
        ("setup.sh", 'sync_settings_port() {\n    "$PYTHON" - "$1" "$2" <<\'PYEOF\'\n', "\nPYEOF"),
        ("setup.ps1", "$SyncSettingsPortScript = @'\n", "\n'@"),
    ],
)
def test_existing_settings_port_update_program_runs(
    tmp_path: Path, script_name: str, start: str, end: str
) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
    program_start = script.index(start) + len(start)
    program = script[program_start : script.index(end, program_start)]
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"port": 8420, "name": "kept"}), encoding="utf-8")

    def sync(port: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", program, str(settings_path), str(port)],
            capture_output=True,
            text=True,
            timeout=30,
        )

    updated = sync(9134)
    unchanged = sync(9134)

    assert (updated.returncode, updated.stdout.strip()) == (0, "port"), updated.stderr
    assert (unchanged.returncode, unchanged.stdout.strip()) == (0, ""), unchanged.stderr
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {"port": 9134, "name": "kept"}
    assert sorted(path.name for path in tmp_path.iterdir()) == ["settings.json"]


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


def test_windows_installer_shim_does_not_lock_the_pip_package_launcher() -> None:
    script = (PROJECT_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    shim_start = script.index("function Add-VbotShim")
    shim_end = script.index("function Write-ManagedRootMarker", shim_start)
    shim = script[shim_start:shim_end]

    assert 'Scripts\\python.exe"' in shim
    assert '" -P -m cli.main %*' in shim
    assert "Scripts\\vbot.exe" not in shim


def test_windows_installer_refuses_accidental_elevation_before_install_mutation() -> None:
    script = (PROJECT_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

    guard = script.index("if ((Test-IsElevated) -and -not $AllowElevatedInstall)")
    assert guard < script.index("Confirm-Git", guard)
    assert guard < script.index('$cloneOutput = @(Invoke-CapturedNative -FilePath "git"', guard)


def test_windows_installer_keeps_explicit_directory_and_existing_target_guard() -> None:
    script = (PROJECT_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

    assert '$installDirWasProvided = $PSBoundParameters.ContainsKey("InstallDir")' in script
    existing_target_guard = script.index("elseif (Test-Path -LiteralPath $InstallDir)")
    existing_target_section = script[existing_target_guard:]
    assert "already exists" in existing_target_section
    assert "pass -InstallDir to choose another location" in existing_target_section


def test_windows_dev_installer_routes_fresh_installs_to_native_main() -> None:
    script = (PROJECT_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

    existing_checkout = script[
        script.index("$useExistingCheckout = (") : script.index(
            "$useNativeInstaller =", script.index("$useExistingCheckout = (")
        )
    ]
    assert "-not $Dev" in existing_checkout
    assert "$useNativeInstaller = -not $useExistingCheckout -and -not $SourceCheckout" in script
    assert "if ($DesktopClient -and $Dev)" not in script
    assert "-Dev selects the native main installation" in script


def test_windows_dev_installer_warns_about_missing_launcher_build_tools(tmp_path: Path) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    # (tools on PATH, LLVM installed but not on PATH, expected warning remedy)
    cases = [
        ("clang-cl,llvm-rc", False, None),
        ("clang-cl", False, "llvm-rc not found"),
        ("", True, "add {llvm_bin} to PATH"),
        ("", False, "install LLVM (winget install LLVM.LLVM)"),
    ]
    program_files = {False: tmp_path / "without" / "Program Files"}
    program_files[True] = tmp_path / "with" / "Program Files"
    llvm_bin = program_files[True] / "LLVM" / "bin"
    llvm_bin.mkdir(parents=True)
    (llvm_bin / "clang-cl.exe").write_bytes(b"")
    harness = tmp_path / "build-tools-harness.ps1"
    harness.write_text(
        r"""param($Source, $Cases)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $Source, [ref]$tokens, [ref]$errors
)
$node = $ast.FindAll({
    param($item)
    $item -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $item.Name -eq "Confirm-NativeBuildTools"
}, $false) | Select-Object -First 1
. ([scriptblock]::Create($node.Extent.Text))
function Test-Have { param($Name) return ($Available -split ",") -contains $Name }
function Write-Status { param($State, $Message) $script:statuses += "${State}:$Message" }
$results = @{}
$index = 0
foreach ($case in @($Cases | ConvertFrom-Json)) {
    $env:ProgramFiles = $case.program_files
    $Available = $case.available
    $statuses = @()
    Confirm-NativeBuildTools
    $results["$index"] = @($statuses)
    $index++
}
$results | ConvertTo-Json -Compress -Depth 3
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(harness),
            str(PROJECT_ROOT / "scripts" / "install.ps1"),
            json.dumps(
                [
                    {"available": available, "program_files": str(program_files[installed])}
                    for available, installed, _expected in cases
                ]
            ),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    statuses = json.loads(result.stdout)
    for index, (available, _installed, expected) in enumerate(cases):
        warnings = statuses[str(index)]
        if expected is None:
            assert warnings == [], available
        else:
            assert len(warnings) == 1, available
            assert warnings[0].startswith("WARN:"), available
            assert expected.format(llvm_bin=llvm_bin) in warnings[0], available
            assert "Visual Studio C++ build tools with the Windows SDK" in warnings[0], available


def test_windows_dev_installer_checks_launcher_build_tools_for_every_shape() -> None:
    script = (PROJECT_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

    requirements = script[
        script.index('Write-Step "Checking main build requirements"') : script.index(
            "Install-NativeRelease -Tag $Version -Shape $shape"
        )
    ]
    assert "\n        Confirm-NativeBuildTools\n" in requirements


def test_windows_public_installer_ends_with_verified_lifecycle_summary() -> None:
    script = (PROJECT_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

    summary_start = script.index("$summaryHost = $HostName")
    summary = script[summary_start:]
    server_status = '"server", "status", "--host", $summaryHost, "--port", $summaryPort'
    autostart_status = '"autostart", "status", "--host", $summaryHost, "--port", $summaryPort'

    assert ".vbot-install.json" in summary
    assert server_status in summary
    assert autostart_status in summary
    assert "$setupReportedProblems" in summary
    ready_guard = summary.index("if ($problems.Count -eq 0 -and $serverRunning)")
    assert summary.index(server_status) < ready_guard
    assert summary.index(autostart_status) < ready_guard
    assert ready_guard < summary.index("http://${summaryHost}:$summaryPort/")


_NATIVE_ROUTING_MODES = (
    "success",
    "missing",
    "digest",
    "signature",
    "dev-success",
    "dev-source-failure",
    "dev-update-failure",
    "dev-no-autostart",
    "dev-desktop-client",
)


def test_windows_native_installer_release_routing_is_verified(tmp_path: Path) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    harness = tmp_path / "native-harness.ps1"
    harness.write_text(
        r"""param($Source, $Root, $Modes)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ApiBase = "https://api.github.com/repos/Vironnimo/vbot"
$ApiHeaders = @{}
$HostName = "127.0.0.1"
$Port = 9134
$ProgressPreference = "SilentlyContinue"
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $Source, [ref]$tokens, [ref]$errors
)
$functions = @(
    "Write-Status",
    "Write-Step",
    "Get-OfficialRelease",
    "Get-ServerHealthUrl",
    "Invoke-NativeCommand",
    "Install-NativeRelease"
)
foreach ($name in $functions) {
    $node = $ast.FindAll({
        param($item)
        $item -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $item.Name -eq $name
    }, $false) | Select-Object -First 1
    . ([scriptblock]::Create($node.Extent.Text))
}
function Write-Status {
    param($State, $Message)
    $script:statuses += "${State}:$Message"
}
function Write-Step { param($Message) }
function Invoke-NativeCommand {
    param([string[]]$Arguments)
    $command = $Arguments -join " "
    $script:nativeCommands += $command
    if (
        ($Mode -eq "dev-source-failure" -and $command -eq "application source main") -or
        ($Mode -eq "dev-update-failure" -and $command -eq "update")
    ) {
        throw (
            "The native application command failed. The installation and logs were retained; " +
            "inspect the error before retrying vbot update."
        )
    }
}
$bytes = [System.Text.Encoding]::UTF8.GetBytes("verified package")
$hasher = New-Object System.Security.Cryptography.SHA256Managed
$sha = [BitConverter]::ToString($hasher.ComputeHash($bytes)).Replace("-", "").ToLowerInvariant()
$hasher.Dispose()
function Invoke-RestMethod {
    param($Uri, $Headers, $TimeoutSec)
    if ($Uri -like "*/health") {
        $script:healthCalls++
        return [pscustomobject]@{status="ok"}
    }
    $assets = if ($Mode -eq "missing") { @() } else { @([pscustomobject]@{
        name=("vBot-1.2.3-windows-x86_64-{0}.exe" -f $Shape)
        digest="sha256:$sha"
        browser_download_url=(
            "https://github.com/Vironnimo/vbot/releases/download/v1.2.3/vbot.exe"
        )
    }) }
    return [pscustomobject]@{tag_name="v1.2.3"; assets=$assets}
}
function Invoke-WebRequest {
    param($Uri, $OutFile, $Headers)
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
    $script:calls = @($ArgumentList)
    if ($WindowStyle -ne "Hidden") { throw "installer window was not hidden" }
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    $state = @{
        schema_version=1
        install_shape=$Shape
        server_host=$HostName
        server_port=$Port
        server_data_directory=$DataDir
    }
    $state | ConvertTo-Json | Set-Content (Join-Path $InstallDir "application.json")
    Set-Content (Join-Path $InstallDir "active-version") "rel_test"
    return [pscustomobject]@{ExitCode=0}
}
# One process runs every mode against its own installation directory.
$results = @{}
foreach ($Mode in ($Modes -split ",")) {
    $InstallDir = Join-Path $Root $Mode
    $DataDir = Join-Path $InstallDir "data"
    $Dev = $Mode -like "dev-*"
    $NoAutostart = $Mode -eq "dev-no-autostart"
    $Shape = if ($Mode -eq "dev-desktop-client") { "desktop-client" } else { "server" }
    $calls = @()
    $nativeCommands = @()
    $statuses = @()
    $healthCalls = 0
    $failure = $null
    try { Install-NativeRelease -Tag "v1.2.3" -Shape $Shape }
    catch { $failure = $_.Exception.Message }
    $results[$Mode] = @{
        ok = $null -eq $failure
        error = $failure
        calls = $calls
        nativeCommands = $nativeCommands
        statuses = $statuses
        healthCalls = $healthCalls
        installationRetained = (Test-Path (Join-Path $InstallDir "application.json")) -and
            (Test-Path (Join-Path $InstallDir "active-version"))
    }
}
$results | ConvertTo-Json -Compress -Depth 5
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(harness),
            str(PROJECT_ROOT / "scripts/install.ps1"),
            str(tmp_path / "install with spaces"),
            ",".join(_NATIVE_ROUTING_MODES),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payloads = json.loads(result.stdout.strip().splitlines()[-1])
    assert set(payloads) == set(_NATIVE_ROUTING_MODES)
    for mode in _NATIVE_ROUTING_MODES:
        _assert_native_routing(mode, payloads[mode], tmp_path / "install with spaces" / mode)


def _assert_native_routing(mode: str, payload: dict[str, Any], install_dir: Path) -> None:
    if mode == "success":
        assert payload["ok"] is True, mode
        assert payload["calls"] == [
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            f'/DIR="{install_dir}"',
            f'/VBOTDATA="{install_dir / "data"}"',
            '/VBOTHOST="127.0.0.1"',
            "/VBOTPORT=9134",
            "/TASKS=startup",
        ], mode
        assert payload["nativeCommands"] == [], mode
        assert payload["healthCalls"] == 1, mode
    elif mode == "dev-success":
        assert payload["ok"] is True, mode
        assert payload["calls"][-1] == "/TASKS=", mode
        assert payload["nativeCommands"] == [
            "application source main",
            "update",
            "autostart enable",
        ], mode
        assert payload["statuses"] == ["OK:vBot is installed and follows main."], mode
        assert payload["healthCalls"] == 1, mode
    elif mode in {"dev-source-failure", "dev-update-failure"}:
        assert payload["ok"] is False, mode
        assert "installation and logs were retained" in payload["error"], mode
        assert payload["installationRetained"] is True, mode
        assert payload["statuses"] == [], mode
        assert payload["healthCalls"] == 0, mode
        expected = ["application source main"]
        if mode == "dev-update-failure":
            expected.append("update")
        assert payload["nativeCommands"] == expected, mode
    elif mode == "dev-no-autostart" or mode == "dev-desktop-client":
        assert payload["ok"] is True, mode
        assert payload["calls"][-1] == "/TASKS=", mode
        assert payload["nativeCommands"] == ["application source main", "update"], mode
        assert payload["healthCalls"] == 0, mode
    elif mode == "missing":
        assert "not yet published" in payload["error"], mode
        assert "-SourceCheckout" in payload["error"], mode
    elif mode == "digest":
        assert "digest does not match" in payload["error"], mode
    else:
        assert "invalid Authenticode signature" in payload["error"], mode


@pytest.mark.parametrize("script_name", ["install.ps1"])
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


def test_windows_checkout_setup_does_not_claim_an_unverified_server_url() -> None:
    script = (PROJECT_ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")

    summary_start = script.index("$statusOutput = @(& {")
    summary = script[summary_start:]

    assert "server status --host $HostName --port $effectivePort" in summary
    assert "exit $RecoverableProblemExitCode" in summary
    assert summary.index("if ($serverRunning)") < summary.index("http://${HostName}:$effectivePort")


def test_public_installers_are_the_only_fresh_install_entrypoints() -> None:
    assert (PROJECT_ROOT / "scripts" / "install.sh").is_file()
    assert (PROJECT_ROOT / "scripts" / "install.ps1").is_file()
    assert not (PROJECT_ROOT / "scripts" / "bootstrap.sh").exists()
    assert not (PROJECT_ROOT / "scripts" / "bootstrap.ps1").exists()


def test_linux_installer_leaves_optional_browser_setup_to_the_skill() -> None:
    script = (PROJECT_ROOT / "scripts/install.sh").read_text(encoding="utf-8")
    assert "ensure_browser" not in script
    assert "apt_install chromium" not in script


@pytest.mark.parametrize("doc_name", ["README.md", "USAGE.md"])
def test_public_docs_install_only_through_install_files(doc_name: str) -> None:
    document = (PROJECT_ROOT / doc_name).read_text(encoding="utf-8")

    assert "scripts/install.sh" in document
    assert "scripts/install.ps1" in document
    assert "bootstrap.sh" not in document
    assert "bootstrap.ps1" not in document


@pytest.mark.parametrize("script_name", ["install.ps1"])
def test_public_installer_owns_fresh_install_and_calls_internal_setup(script_name: str) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
    setup_reference = "scripts/setup.sh" if script_name.endswith(".sh") else "scripts\\setup.ps1"
    clone = (
        "git clone"
        if script_name.endswith(".sh")
        else '-FilePath "git" -ArgumentList @(\n        "clone"'
    )

    assert clone in script
    assert ".venv" in script
    assert setup_reference in script
    assert ".vbot-install-root" in script


@pytest.mark.parametrize("script_name", ["uninstall.sh", "uninstall.ps1"])
def test_uninstaller_accepts_new_and_legacy_managed_install_markers(script_name: str) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    assert ".vbot-install-root" in script
    assert ".vbot-install-venv" in script
    assert ".vbot-bootstrap" in script


@pytest.mark.parametrize("script_name", ["install.ps1"])
def test_public_installer_can_configure_releases_from_before_setup_rename(
    script_name: str,
) -> None:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    assert ".vbot-bootstrap" in script
    if script_name.endswith(".sh"):
        assert "editable pip install" in script
    else:
        assert "function Install-PythonPackage" in script


@pytest.mark.skipif(os.name != "nt", reason="PowerShell installer")
def test_native_installer_preserves_progress_labels_and_failure_log(tmp_path):
    exit_codes = (0, 7)
    payloads = _run_windows_powershell(
        tmp_path,
        r"""param($Source, $ExitCodes, $Root)
$ErrorActionPreference = "Stop"
$Functions = @("Invoke-CapturedNative", "Invoke-NativeCommand")
"""
        + _LOAD_INSTALLER_FUNCTIONS
        + r"""
function Write-Status { param($State, $Message) $script:statuses += "${State}:$Message" }
# One process runs every exit code against its own installation directory.
$results = @{}
foreach ($ExitCode in ($ExitCodes -split ",")) {
    $InstallDir = Join-Path $Root $ExitCode
    $null = New-Item -ItemType Directory -Force -Path $InstallDir
    $InstallLogPath = Join-Path $InstallDir "output.log"
    $script:statuses = @()
    # A function shadows only the executable in this disposable harness.
    $application = Join-Path $InstallDir "vBot.exe"
    Set-Item -LiteralPath "Function:$application" -Value {
        "[WORK] prepare-sentinel"
        # The CLI reports command summaries on stderr.
        cmd /c "1>&2 echo [OK] stderr-sentinel"
        "[WARN] warning-sentinel"
        "[ERROR] diagnostic-sentinel"
        "private-diagnostic-sentinel"
        $global:LASTEXITCODE = [int]$ExitCode
    }
    try { Invoke-NativeCommand -Arguments @("autostart", "enable"); $ok = $true; $detail = "" }
    catch { $ok = $false; $detail = $_.Exception.Message }
    $results[$ExitCode] = @{ok=$ok; detail=$detail; statuses=$script:statuses;
        log=[string](Get-Content -Raw $InstallLogPath)}
}
$results | ConvertTo-Json -Compress -Depth 3
""",
        str(PROJECT_ROOT / "scripts/install.ps1"),
        ",".join(map(str, exit_codes)),
        str(tmp_path),
    )
    assert isinstance(payloads, dict)
    assert set(payloads) == {str(code) for code in exit_codes}
    for exit_code in exit_codes:
        payload = payloads[str(exit_code)]
        assert payload["statuses"] == [
            "WORK:prepare-sentinel",
            "OK:stderr-sentinel",
            "WARN:warning-sentinel",
            "ERROR:diagnostic-sentinel",
        ], exit_code
        assert "private-diagnostic-sentinel" in payload["log"], exit_code
        assert payload["ok"] is (exit_code == 0)
        if exit_code:
            assert "autostart enable" in payload["detail"]
            assert str(tmp_path / str(exit_code) / "output.log") in payload["detail"]
            assert "retrying vbot update" not in payload["detail"]


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell installer")
def test_windows_python_probe_reads_the_exit_code_despite_stderr_output(tmp_path: Path) -> None:
    payload = _run_windows_powershell(
        tmp_path,
        r"""param($Source)
$ErrorActionPreference = "Stop"
$Functions = @("Invoke-CapturedNative", "Test-Have", "Test-PythonOk")
"""
        + _LOAD_INSTALLER_FUNCTIONS
        + r"""
# Stands in for the Microsoft Store alias, which reports on stderr.
function python { cmd /c "1>&2 echo Python was not found& exit $ExitCode" }
$results = @{}
foreach ($ExitCode in @(0, 9009)) { $results["$ExitCode"] = (Test-PythonOk) }
$results | ConvertTo-Json -Compress
""",
        str(PROJECT_ROOT / "scripts/install.ps1"),
    )

    assert payload == {"0": True, "9009": False}


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell installer")
def test_windows_native_install_probes_health_like_the_cli(tmp_path: Path) -> None:
    from cli._server_target import build_server_base_url

    hosts = ["127.0.0.1", "0.0.0.0", "", "*", "::", "::1", "[::1]", "localhost", "192.0.2.5"]
    payload = _run_windows_powershell(
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
        str(PROJECT_ROOT / "scripts/install.ps1"),
        json.dumps(hosts),
    )

    assert payload == [f"{build_server_base_url(host, 8420)}/health" for host in hosts]


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell installer")
def test_windows_installer_failure_returns_to_a_script_block_caller(tmp_path: Path) -> None:
    command = (
        "try { & ([scriptblock]::Create((Get-Content -Raw -Encoding UTF8 "
        "-LiteralPath $env:VBOT_TEST_INSTALLER))) -Desktop -DesktopClient } "
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
            "VBOT_TEST_INSTALLER": str(PROJECT_ROOT / "scripts/install.ps1"),
        },
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    message = "-Desktop and -DesktopClient are mutually exclusive."
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f"caught: {message}", "caller still running"]
    assert f"[ERROR] vBot installation failed: {message}" in result.stderr
    (log,) = tmp_path.glob("vbot-install-*.log")
    assert f"Error: {message}" in log.read_text(encoding="utf-8")
