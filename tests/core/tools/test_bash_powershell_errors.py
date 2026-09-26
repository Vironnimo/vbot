"""PowerShell error records remain visible after a successful final statement."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys

import pytest

import core.tools._bash_environment as bash_environment
import core.tools.bash as bash_module
from core.tools._powershell import POWERSHELL_ERROR_NOTE, powershell_command
from core.tools.bash import register_bash_tool
from core.tools.tools import ToolRegistry
from tests.core.tools.bash_helpers import AGENT_ID, make_context
from tests.core.tools.bash_helpers import manager as manager
from tests.core.tools.bash_helpers import shell_env_cache as shell_env_cache

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        sys.platform != "win32" or shutil.which("pwsh") is None,
        reason="Real PowerShell error records",
    ),
]


@pytest.fixture(autouse=True)
def real_shell_environment(monkeypatch):
    monkeypatch.setattr(bash_environment, "_cached_shell_env", dict(os.environ))


async def _dispatch(manager, tmp_path, command, *, mode="foreground"):
    registry = ToolRegistry()
    register_bash_tool(registry, manager)
    return await asyncio.wait_for(
        registry.dispatch(make_context(tmp_path), {"command": command, "mode": mode}), 30
    )


@pytest.mark.parametrize("mode", ["foreground", "background"])
async def test_successful_write_after_method_error_reports_record_without_changing_exit(
    manager, tmp_path, mode
):
    (tmp_path / "input.txt").write_text("source text", encoding="utf-8")
    result = await _dispatch(
        manager,
        tmp_path,
        "$text = Get-Content -Raw input.txt; $piece = $text.Substring(4, -1); "
        "Set-Content -LiteralPath result.txt -Value ('written-after-error:' + $piece)",
        mode=mode,
    )
    assert result["ok"] is True
    if mode == "background":
        tracked = manager.get_process(result["data"]["process_id"], AGENT_ID)
        await asyncio.wait_for(asyncio.shield(tracked.wait_task), 30)
        data = await manager.snapshot(tracked.process_id, AGENT_ID)
    else:
        data = result["data"]
    assert data["exit_code"] == 0
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "written-after-error:\n"
    assert data["output"].count(POWERSHELL_ERROR_NOTE) == 1
    assert "Substring" in data["output"]


@pytest.mark.parametrize(
    "command",
    [
        "try { throw 'recorded-sentinel' } catch {}; 'finished'",
        "Write-Error 'recorded-sentinel' -ErrorAction SilentlyContinue; 'finished'",
    ],
)
async def test_handled_or_suppressed_errors_include_the_actual_record(manager, tmp_path, command):
    result = await _dispatch(manager, tmp_path, command)
    assert result["ok"] is True
    assert result["data"]["exit_code"] == 0
    assert result["data"]["output"].count(POWERSHELL_ERROR_NOTE) == 1
    assert "recorded-sentinel" in result["data"]["output"]


@pytest.mark.parametrize(
    "command",
    [
        "Write-Output 'MethodInvocationException: printed test failure'",
        "[Console]::Error.WriteLine('Write-Error: printed diagnostic')",
        f"& '{sys.executable}' -c 'import sys; sys.stderr.write(\"native stderr sentinel\\n\")'",
        "Write-Warning 'test warning'",
    ],
)
async def test_printed_error_text_and_stderr_do_not_claim_error_records(manager, tmp_path, command):
    result = await _dispatch(manager, tmp_path, command)
    assert result["ok"] is True
    assert result["data"]["exit_code"] == 0
    assert POWERSHELL_ERROR_NOTE not in result["data"]["output"]
    assert result["data"]["output"].strip()


@pytest.mark.parametrize("new_error", [False, True])
async def test_full_existing_error_collection_uses_identity_instead_of_count(
    manager, tmp_path, monkeypatch, new_error
):
    def with_profile_errors(command):
        return [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$Error.Clear(); 1..300 | ForEach-Object { "
            "Write-Error 'profile-sentinel' -ErrorAction SilentlyContinue }; "
            + powershell_command(command),
        ]

    monkeypatch.setattr(bash_module, "_shell_argv", with_profile_errors)
    command = (
        "Write-Error 'new-sentinel' -ErrorAction SilentlyContinue; 'finished'"
        if new_error
        else "'finished'"
    )
    result = await _dispatch(manager, tmp_path, command)
    assert result["data"]["exit_code"] == 0
    assert (POWERSHELL_ERROR_NOTE in result["data"]["output"]) is new_error
    assert ("new-sentinel" in result["data"]["output"]) is new_error


async def test_large_error_record_has_bounded_completion_evidence(manager, tmp_path):
    result = await _dispatch(
        manager,
        tmp_path,
        "Write-Error ('recorded-sentinel:' + ('x' * 10000)) -ErrorAction SilentlyContinue; "
        "'finished'",
    )
    assert result["data"]["exit_code"] == 0
    output = result["data"]["output"]
    assert POWERSHELL_ERROR_NOTE in output and "recorded-sentinel:" in output
    assert len(output) < 1000


@pytest.mark.parametrize(
    "command, expected",
    [
        ("begin { 'begin-sentinel' } end { 'end-sentinel' }", "begin-sentinel\nend-sentinel"),
        ("param($value = 'param-sentinel') $value", "param-sentinel"),
        (
            "using namespace System.Text\n[StringBuilder]::new('using-sentinel').ToString()",
            "using-sentinel",
        ),
    ],
)
async def test_leading_powershell_constructs_keep_their_execution_semantics(
    manager, tmp_path, command, expected
):
    result = await _dispatch(manager, tmp_path, command)
    assert result["ok"] is True
    assert result["data"]["exit_code"] == 0
    assert result["data"]["output"].strip().replace("\r\n", "\n") == expected


@pytest.mark.parametrize(
    "command, exit_code",
    [
        ("Write-Error 'recorded-sentinel'; exit 0", 0),
        ("Write-Error 'recorded-sentinel'; $Error.Clear(); 'finished'", 0),
        ("Write-Error 'recorded-sentinel'", 1),
        ("Write-Error 'recorded-sentinel'; exit 7", 7),
    ],
)
async def test_explicit_control_flow_and_failure_status_remain_intact(
    manager, tmp_path, command, exit_code
):
    result = await _dispatch(manager, tmp_path, command)
    assert result["ok"] is True
    assert result["data"]["exit_code"] == exit_code
    assert POWERSHELL_ERROR_NOTE not in result["data"]["output"]
