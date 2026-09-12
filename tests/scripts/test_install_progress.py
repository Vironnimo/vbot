"""Exercise installer progress functions without installing or changing host state."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("exit_code,delay", [(0, 13), (2, 0), (7, 0)])
def test_windows_setup_progress_preserves_arguments_cwd_logs_and_exit(tmp_path, exit_code, delay):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell is unavailable")
    setup = tmp_path / "child setup's script.ps1"
    setup.write_text(
        """param([string]$Named, [int]$ExitCode, [string]$DataPath, [int]$Delay)
Write-Output "==> test-owned phase"
Start-Sleep -Seconds $Delay
Write-Output "test-owned diagnostic"
[Console]::Error.WriteLine("test-owned stderr")
Write-Output "Warning: test-owned warning"
@{named=$Named; cwd=(Get-Location).ProviderPath} |
    ConvertTo-Json | Set-Content -LiteralPath $DataPath
exit $ExitCode
""",
        encoding="utf-8",
        newline="\n",
    )
    harness = tmp_path / "harness.ps1"
    harness.write_text(
        """param($Source, $Setup, $Log, $Executable, [int]$Code, [int]$Delay)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$InstallLogPath = $Log
$tokens = $null; $errors = $null
$parser = [System.Management.Automation.Language.Parser]
$ast = $parser::ParseFile($Source, [ref]$tokens, [ref]$errors)
$names = @("Write-Status", "Write-Step", "Invoke-SetupWithProgress")
foreach ($function in $ast.FindAll({param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -in $names
}, $false)) { . ([scriptblock]::Create($function.Extent.Text)) }
$arguments = @("-Named", "value with spaces and ' quote", "-ExitCode", "$Code",
               "-DataPath", "relative result.json", "-Delay", "$Delay")
$code = Invoke-SetupWithProgress -Executable $Executable -Setup $Setup -SetupArguments $arguments
if (@(Get-Job).Count -ne 0) { throw "Progress left a job behind" }
exit $code
""",
        encoding="utf-8",
        newline="\n",
    )
    log = tmp_path / "install.log"
    result = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(harness),
            str(ROOT / "scripts/install.ps1"),
            str(setup),
            str(log),
            shell,
            str(exit_code),
            str(delay),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=25,
        env={**os.environ, "NO_COLOR": "1"},
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    saved = json.loads((tmp_path / "relative result.json").read_text(encoding="utf-8-sig"))
    assert saved["named"] == "value with spaces and ' quote"
    assert Path(saved["cwd"]).resolve() == tmp_path.resolve()
    if delay:
        assert result.stdout.count("test-owned phase") >= 2
    assert "test-owned phase" in result.stdout
    assert "test-owned warning" in result.stdout
    assert "test-owned diagnostic" not in result.stdout
    assert "test-owned diagnostic" in log.read_text(encoding="utf-8-sig")
    assert "test-owned stderr" in log.read_text(encoding="utf-8-sig")
    assert "\033[" not in result.stdout


def test_linux_setup_progress_preserves_diagnostics_and_failed_exit(tmp_path):
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("Bash is unavailable")
    source = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
    functions = source[source.index("status_line() {") : source.index('INSTALL_LOG=""')]
    script = tmp_path / "progress.sh"
    script.write_text(
        functions
        + """
INSTALL_LOG="$1"
{
    printf '==> test-owned phase\\n'
    printf 'test-owned '
    sleep 11
    printf 'diagnostic\\n'
    exit 7
} | show_setup_progress
exit "${PIPESTATUS[0]}"
""",
        encoding="utf-8",
        newline="\n",
    )
    log = tmp_path / "install.log"
    result = subprocess.run(
        [shell, script.name, log.name],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "NO_COLOR": "1"},
    )
    assert result.returncode == 7
    assert result.stdout.count("test-owned phase") >= 2
    assert "test-owned phase" in result.stdout
    assert "test-owned diagnostic" not in result.stdout
    assert "test-owned diagnostic" in log.read_text()
    assert "\033[" not in result.stdout
