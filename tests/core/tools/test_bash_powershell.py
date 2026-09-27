"""The shell Tool on Windows: PowerShell wrapping, exit status, error records, and text."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import core.tools._bash_environment as bash_environment
import core.tools.bash as bash_module
from core.tools._powershell import (
    EXIT_STATUS_STATEMENT,
    POWERSHELL_ERROR_NOTE,
    SETUP_STATEMENT,
    powershell_command,
)
from core.tools.bash import register_bash_tool
from core.tools.process_manager import ProcessManager
from core.tools.tools import ToolRegistry
from tests.core.tools.bash_test_support import AGENT_ID, make_context
from tests.core.tools.bash_test_support import manager as manager
from tests.core.tools.bash_test_support import shell_env_cache as shell_env_cache

SETUP = SETUP_STATEMENT
EXIT = EXIT_STATUS_STATEMENT

real_powershell = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("pwsh") is None, reason="Real PowerShell 7"
)


@pytest.fixture(autouse=True)
def real_shell_environment(shell_env_cache: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # Real commands need the host PATH instead of the shared placeholder.
    monkeypatch.setattr(bash_environment, "_cached_shell_env", dict(os.environ))


def test_shell_detection_uses_native_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bash_module.sys, "platform", "win32")

    assert bash_module._shell_argv("Write-Output hello") == [
        "pwsh",
        "-NonInteractive",
        "-Command",
        powershell_command("Write-Output hello"),
    ]
    assert SETUP_STATEMENT.startswith(
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    )

    monkeypatch.setattr(bash_module.sys, "platform", "linux")

    assert bash_module._shell_argv("echo hello") == ["bash", "-c", "echo hello"]


@pytest.mark.parametrize(
    "command, expected",
    [
        ("Write-Output hi", f"{SETUP}\nWrite-Output hi\n{EXIT}"),
        # Ordinary leading statements, even ones that look like keywords, get setup first.
        ("using-thing arg", f"{SETUP}\nusing-thing arg\n{EXIT}"),
        ("endless-loop", f"{SETUP}\nendless-loop\n{EXIT}"),
        ("param", f"{SETUP}\nparam\n{EXIT}"),
        ("[string]$value = 'x'", f"{SETUP}\n[string]$value = 'x'\n{EXIT}"),
        # using statements must stay first, including comments before them.
        (
            "using namespace System.Text; [StringBuilder]::new('ok')",
            f"using namespace System.Text;\n{SETUP}\n [StringBuilder]::new('ok')\n{EXIT}",
        ),
        (
            "# note\n<# block #> USING namespace System.IO\nusing module @{ModuleName='A;B'}\n1",
            f"# note\n<# block #> USING namespace System.IO\nusing module @{{ModuleName='A;B'}}\n"
            f"{SETUP}\n1\n{EXIT}",
        ),
        ("using namespace System.Text", f"using namespace System.Text\n{SETUP}\n\n{EXIT}"),
        # A script param block, optionally with attributes, must stay first too.
        ("param($x = 'a;b') $x", f"param($x = 'a;b')\n{SETUP}\n $x\n{EXIT}"),
        (
            '[CmdletBinding()]\nparam([string]$Name = "x)")\n$Name',
            f'[CmdletBinding()]\nparam([string]$Name = "x)")\n{SETUP}\n\n$Name\n{EXIT}',
        ),
        # An unterminated construct keeps setup before it: PowerShell reports the
        # original syntax error and the setup adds no new one.
        ("param('unterminated", f"{SETUP}\nparam('unterminated\n{EXIT}"),
        ("using namespace A\nparam(", f"using namespace A\n{SETUP}\nparam(\n{EXIT}"),
        # Named blocks must contain every statement, and admit none after them,
        # so PowerShell keeps its own exit status.
        ("begin { 'b' } end { 'e' }", f"begin {{{SETUP}\n 'b' }} end {{ 'e' }}"),
        ("param($x) process { $x }", f"param($x) process {{{SETUP}\n $x }}"),
    ],
)
def test_powershell_command_places_setup_and_exit_status_where_allowed(
    command: str, expected: str
) -> None:
    assert powershell_command(command) == expected


# --- Line filters PowerShell lacks -----------------------------------------


def _pwsh_without_unix_tools(command: str, cwd: Path) -> tuple[int, str]:
    """Run like the shell Tool does, on a PATH without Git's Unix commands."""
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    path = os.pathsep.join(
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if shutil.which("head", path=entry) is None and shutil.which("wc", path=entry) is None
    )
    completed = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", powershell_command(command)],
        capture_output=True,
        cwd=cwd,
        env={**os.environ, "PATH": path, "NO_COLOR": "1"},
        timeout=60,
    )
    output = (completed.stdout + completed.stderr).decode("utf-8", "replace")
    return completed.returncode, output.replace("\r\n", "\n")


_LINE_FILTERS = [
    ("1..100 | head -n 3", "1\n2\n3\n"),
    ("1..100 | head -3", "1\n2\n3\n"),
    ("1..10 | tail -2", "9\n10\n"),
    ("1..10 | tail -n +8", "8\n9\n10\n"),
    ("1..7 | wc -l", "7\n"),
    ("head -n 2 a.txt", "line 1\nline 2\n"),
    ("tail --lines=1 a.txt", "line 20\n"),
    ("wc -l a.txt b.txt", "20 a.txt\n2 b.txt\n22 total\n"),
    ("head -n 1 a.txt b.txt", "==> a.txt <==\nline 1\n==> b.txt <==\nx\n"),
    (f"& '{sys.executable}' -c \"print('a'); print('b')\" | tail -n 1", "b\n"),
]


@real_powershell
def test_line_filters_run_where_powershell_has_none(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("".join(f"line {n}\n" for n in range(1, 21)))
    (tmp_path / "b.txt").write_text("x\ny\n")
    # One PowerShell process runs every filter; a marker line separates them.
    script = "\n'--'\n".join(command for command, _output in _LINE_FILTERS)

    exit_code, output = _pwsh_without_unix_tools(script, tmp_path)

    assert exit_code == 0
    assert output.split("--\n") == [expected for _command, expected in _LINE_FILTERS]


@real_powershell
@pytest.mark.parametrize(
    ("command", "message"),
    [
        (
            "head -c 5 a.txt; 'after'",
            "head: -c is not supported here; use head -n N or Select-Object -First N.",
        ),
        (
            "tail -f a.txt; 'after'",
            "tail: -f is not supported here; use tail -n N or Select-Object -Last N.",
        ),
        (
            "wc -w a.txt; 'after'",
            "wc: -w is not supported here; use wc -l, or Measure-Object -Word or -Character.",
        ),
        (
            "wc a.txt; 'after'",
            "wc: only wc -l is supported here; use Measure-Object -Word or -Character for other "
            "counts.",
        ),
        # Other missing commands still fail as not found.
        ("vbot-no-such-tool --flag", "vbot-no-such-tool"),
    ],
    ids=["head-c", "tail-f", "wc-w", "wc", "other-missing"],
)
def test_unsupported_line_filter_options_stop_with_the_equivalent(
    tmp_path: Path, command: str, message: str
) -> None:
    (tmp_path / "a.txt").write_text("text\n")

    exit_code, output = _pwsh_without_unix_tools(command, tmp_path)

    assert exit_code == 1
    assert message in output
    assert "after" not in output
    assert ("supported here" in output) is ("supported here" in message)


# --- Exit status and error records through the shell Tool ------------------


async def _dispatch(
    manager: ProcessManager, tmp_path: Path, command: str, *, mode: str = "foreground"
) -> dict[str, Any]:
    registry = ToolRegistry()
    register_bash_tool(registry, manager)
    return await asyncio.wait_for(
        registry.dispatch(make_context(tmp_path), {"command": command, "mode": mode}),
        # Bound an interactive hang without making cold PowerShell startup on a
        # shared Windows runner a performance requirement.
        30,
    )


_PRINTED_ERROR_TEXT = "; ".join(
    [
        "Write-Output 'MethodInvocationException: printed test failure'",
        "[Console]::Error.WriteLine('Write-Error: printed diagnostic')",
        f"& '{sys.executable}' -c 'import sys; sys.stderr.write(\"native stderr sentinel\\n\")'",
        "Write-Warning 'test warning'",
    ]
)


@real_powershell
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "exit_code", "error_record", "shows"),
    [
        # The last program's exit status becomes the command's exit code.
        ("python -c 'raise SystemExit(5)'", 5, False, ()),
        ("cmd /c exit 4", 4, False, ()),
        ("python -c 'raise SystemExit(5)'; Write-Output after", 0, False, ("after",)),
        ("Get-Item does-not-exist", 1, False, ("does-not-exist",)),
        ('param($code = 3) python -c "raise SystemExit($code)"', 3, False, ()),
        # An unknown pipeline command exits instead of waiting for input.
        (
            "Get-ChildItem . | __vbot_missing_pipeline_command__",
            1,
            False,
            ("__vbot_missing_pipeline_command__",),
        ),
        # Handled or suppressed error records stay visible after a successful end.
        ("try { throw 'recorded-sentinel' } catch {}; 'finished'", 0, True, ("recorded-sentinel",)),
        (
            "Write-Error 'recorded-sentinel' -ErrorAction SilentlyContinue; 'finished'",
            0,
            True,
            ("recorded-sentinel",),
        ),
        # Explicit control flow and failure status keep their meaning.
        ("Write-Error 'recorded-sentinel'; exit 0", 0, False, ()),
        ("Write-Error 'recorded-sentinel'; $Error.Clear(); 'finished'", 0, False, ()),
        ("Write-Error 'recorded-sentinel'", 1, False, ()),
        ("Write-Error 'recorded-sentinel'; exit 7", 7, False, ()),
        # Printed error text, warnings, and native stderr claim no error records.
        (
            _PRINTED_ERROR_TEXT,
            0,
            False,
            (
                "MethodInvocationException: printed test failure",
                "Write-Error: printed diagnostic",
                "native stderr sentinel",
                "test warning",
            ),
        ),
        # Named blocks keep their execution semantics.
        (
            "begin { 'begin-sentinel' } end { 'end-sentinel' }",
            0,
            False,
            ("begin-sentinel\nend-sentinel",),
        ),
    ],
    ids=[
        "native-exit",
        "cmd-exit",
        "later-statement",
        "cmdlet-error",
        "param-block",
        "unknown-pipeline-command",
        "handled",
        "suppressed",
        "exit-0",
        "cleared",
        "error-exit",
        "exit-7",
        "printed-text",
        "named-blocks",
    ],
)
async def test_exit_status_and_error_records_follow_the_last_statement(
    manager: ProcessManager,
    tmp_path: Path,
    command: str,
    exit_code: int,
    error_record: bool,
    shows: tuple[str, ...],
) -> None:
    result = await _dispatch(manager, tmp_path, command)

    assert result["ok"] is True
    assert result["data"]["exit_code"] == exit_code
    output = result["data"]["output"].replace("\r\n", "\n")
    assert output.count(POWERSHELL_ERROR_NOTE) == (1 if error_record else 0)
    for text in shows:
        assert text in output


@real_powershell
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["foreground", "background"])
async def test_successful_write_after_method_error_reports_record_without_changing_exit(
    manager: ProcessManager, tmp_path: Path, mode: str
) -> None:
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
        assert tracked.wait_task is not None
        await asyncio.wait_for(asyncio.shield(tracked.wait_task), 30)
        data = await manager.snapshot(tracked.process_id, AGENT_ID)
    else:
        data = result["data"]
    assert data["exit_code"] == 0
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "written-after-error:\n"
    output = str(data["output"])
    assert output.count(POWERSHELL_ERROR_NOTE) == 1
    assert "Substring" in output


@real_powershell
@pytest.mark.asyncio
@pytest.mark.parametrize("new_error", [False, True])
async def test_full_existing_error_collection_uses_identity_instead_of_count(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, new_error: bool
) -> None:
    def with_profile_errors(command: str) -> list[str]:
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


@real_powershell
@pytest.mark.asyncio
async def test_large_error_record_has_bounded_completion_evidence(
    manager: ProcessManager, tmp_path: Path
) -> None:
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


# --- Input and text --------------------------------------------------------


@real_powershell
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command, expected",
    [
        # Commands that read stdin see its end at once.
        (
            'Write-Output "x: $input"\n'
            "$input | ForEach-Object { $_ }\n"
            '$read = [Console]::In.ReadToEnd(); Write-Output "read: $read"\n'
            "'alpha','beta' | ForEach-Object { $_.ToUpper() }",
            ["x:", "read:", "ALPHA", "BETA"],
        ),
        (
            "'alpha','beta' | pwsh -NonInteractive -Command "
            "'$input | ForEach-Object { $_.ToUpper() }'",
            ["ALPHA", "BETA"],
        ),
    ],
    ids=["stdin-at-eof", "nested-pipeline"],
)
async def test_stdin_reaches_its_end_and_pipelines_feed_nested_shells(
    manager: ProcessManager, tmp_path: Path, command: str, expected: list[str]
) -> None:
    result = await _dispatch(manager, tmp_path, command)

    assert result["ok"] is True
    assert result["data"]["exit_code"] == 0
    assert [line.rstrip() for line in result["data"]["output"].strip().splitlines()] == expected


@real_powershell
@pytest.mark.asyncio
async def test_output_keeps_non_ascii_text(manager: ProcessManager, tmp_path: Path) -> None:
    (tmp_path / "Übersicht €.txt").write_text("x", encoding="utf-8")
    command = "using namespace System.Text\n" + "; ".join(
        [
            "[StringBuilder]::new('ß').ToString()",
            "Write-Output 'Jürgen €'",
            "Get-ChildItem -Name",
            "cmd /c echo ä€",
            "$line = cmd /c echo ö; Write-Output $line",
            "[Console]::Error.WriteLine('fäil')",
        ]
    )

    result = await _dispatch(manager, tmp_path, command)

    assert result["ok"] is True
    assert result["data"]["exit_code"] == 0
    lines = [line.strip() for line in result["data"]["output"].splitlines() if line.strip()]
    # stderr may interleave with stdout, so only the set of lines is stable.
    assert sorted(lines) == sorted(["ß", "Jürgen €", "Übersicht €.txt", "ä€", "ö", "fäil"])
