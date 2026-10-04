"""The shell Tool on Windows: the PowerShell wrapper's exit status, errors, filters and text.

Each real-PowerShell case starts a ``pwsh`` process in a ConPTY terminal (about
half a second), so a case exists only for a distinct path through the wrapper;
cases that share a path share a process.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

import core.tools.shell as shell_module
from core.storage.temp_files import TemporaryFileManager
from core.tools._powershell import POWERSHELL_ERROR_NOTE, powershell_command
from core.tools.shell import SHELL_TOOL_NAME, register_shell_tool
from core.tools.terminal_manager import TerminalManager
from core.tools.tools import JsonObject, ToolContext, ToolRegistry
from tests.core.tools.terminal_manager_helpers import PendingTriggerService
from tests.core.tools.tools_test_support import dispatch_as_executor

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("pwsh") is None, reason="Real PowerShell 7"
)


def test_command_travels_as_a_literal_parsed_after_the_setup() -> None:
    text = powershell_command("it's ‘x’")

    assert text.startswith(
        "[Console]::OutputEncoding = [Console]::InputEncoding = "
        "[System.Text.UTF8Encoding]::new($false); "
    )
    assert "\n$__vbotCommand = 'it''s ‘‘x’’'\n" in text


# --- Line filters PowerShell lacks -----------------------------------------


def _pwsh_without_unix_tools(command: str, cwd: Path) -> tuple[int, str, str]:
    """Run the wrapped command on a PATH without Git's Unix commands."""
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
    stdout, stderr = (
        stream.decode("utf-8", "replace").replace("\r\n", "\n")
        for stream in (completed.stdout, completed.stderr)
    )
    return completed.returncode, stdout, stderr


def _stops_with(command: str, message: str) -> tuple[str, str]:
    # The catch prints the message and shows that nothing after the filter ran.
    return f"try {{ {command}; 'after' }} catch {{ $_.Exception.Message }}", f"{message}\n"


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
    # Other options stop the script with a message naming the equivalent.
    _stops_with(
        "head -c 5 a.txt",
        "head: -c is not supported here; use head -n N or Select-Object -First N.",
    ),
    _stops_with(
        "tail -f a.txt",
        "tail: -f is not supported here; use tail -n N or Select-Object -Last N.",
    ),
    _stops_with(
        "wc -w a.txt",
        "wc: -w is not supported here; use wc -l, or Measure-Object -Word or -Character.",
    ),
    _stops_with(
        "wc a.txt",
        "wc: only wc -l is supported here; use Measure-Object -Word or -Character for other "
        "counts.",
    ),
]


def test_line_filters_run_where_powershell_has_none(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("".join(f"line {n}\n" for n in range(1, 21)))
    (tmp_path / "b.txt").write_text("x\ny\n")
    # One PowerShell process runs every filter; a marker line separates them.
    script = "\n'--'\n".join(command for command, _output in _LINE_FILTERS)

    exit_code, output, errors = _pwsh_without_unix_tools(script, tmp_path)

    assert exit_code == 0, errors
    assert output.split("--\n") == [expected for _command, expected in _LINE_FILTERS], errors


# --- Exit status, errors and text through the shell Tool -------------------


async def _run(tmp_path: Path, command: str) -> JsonObject:
    """Run *command* through the shell Tool in a real terminal; returns the result data."""
    trigger = PendingTriggerService()
    manager = TerminalManager(
        trigger,
        temporary_files=TemporaryFileManager(
            tmp_path / "temp", retention={"commands": timedelta(hours=1)}
        ),
    )
    registry = ToolRegistry()
    register_shell_tool(registry, manager)
    context = ToolContext(
        agent_id="agent-a",
        session_id="session-a",
        run_id="run-a",
        tool_call_id="call-a",
        tool_name=SHELL_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
        cwd=tmp_path,
    )
    try:
        result = await dispatch_as_executor(registry, context, {"command": command, "timeout": 60})
    finally:
        trigger.release.set()
        await manager.aclose()
    assert result["ok"] is True, result
    data: JsonObject = result["data"]
    assert data["status"] == "exited", data
    return data


_PRINTED_ERROR_TEXT = "; ".join(
    [
        "Write-Output 'MethodInvocationException: printed test failure'",
        "[Console]::Error.WriteLine('Write-Error: printed diagnostic')",
        f"& '{sys.executable}' -c 'import sys; sys.stderr.write(\"native stderr sentinel\\n\")'",
        "Write-Warning 'test warning'",
    ]
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "exit_code", "error_record", "shows"),
    [
        # A param block keeps its meaning, and the last program's exit status becomes
        # the command's exit code.
        (
            f"param($code = 3) & '{sys.executable}' -c \"raise SystemExit($code)\"",
            3,
            False,
            (),
        ),
        (
            f"& '{sys.executable}' -c 'raise SystemExit(5)'; Write-Output after",
            0,
            False,
            ("after",),
        ),
        # #requires keeps its meaning at the start of the command.
        ("#requires -Version 7\n'requires-sentinel'", 0, False, ("requires-sentinel",)),
        # PowerShell's messages are English whatever the display language.
        ("Get-Item does-not-exist", 1, False, ("Cannot find path", "does-not-exist")),
        # A syntax error names the command's own line, in UTF-8, and runs nothing.
        (
            "'not-run'\nWrite-Output ('ä' + )",
            1,
            False,
            (
                "ParserError",
                "2 |  Write-Output ('ä' + )",
                "You must provide a value expression following the '+' operator.",
            ),
        ),
        # An unknown pipeline command exits instead of waiting for input; a missing
        # command other than head, tail and wc fails as not found.
        (
            "Get-ChildItem . | vbot_missing_pipeline_command",
            1,
            False,
            ("vbot_missing_pipeline_command", "is not recognized"),
        ),
        # A shown error record stays visible after a successful end. Errors the command's
        # own text silenced claim none, also under strict mode: the note names the
        # shown error although the silenced ones are newer.
        (
            "Set-StrictMode -Version Latest\n"
            "Get-Item shown-sentinel\n"
            "Remove-Item silenced-sentinel.txt -ErrorAction SilentlyContinue\n"
            "Get-Item silenced-sentinel 2>$null; rm silenced-sentinel -ea 0\n"
            "& { Get-Item silenced-sentinel } *>$null; 'finished'",
            0,
            True,
            ("shown-sentinel", "finished"),
        ),
        # Explicit control flow and failure status keep their meaning.
        ("Write-Error 'recorded-sentinel'; exit 0", 0, False, ()),
        ("Write-Error 'recorded-sentinel'; $Error.Clear(); 'finished'", 0, False, ()),
        # Printed error text, warnings, and native stderr claim no error records. Quotes
        # reach PowerShell unchanged, including typographic ones.
        (
            _PRINTED_ERROR_TEXT + "\n$text = @'\nit's ‘typographic’\n'@\n$text",
            0,
            False,
            (
                "MethodInvocationException: printed test failure",
                "Write-Error: printed diagnostic",
                "native stderr sentinel",
                "test warning",
                "it's ‘typographic’",
            ),
        ),
        # Named blocks keep their execution semantics, and the last one its exit status.
        (
            "begin { 'begin-sentinel' } end { 'end-sentinel' }",
            0,
            False,
            ("begin-sentinel\nend-sentinel",),
        ),
        ("begin { 'begin-sentinel' } process { cmd /c exit 3 }", 3, False, ("begin-sentinel",)),
    ],
    ids=[
        "param-block-native-exit",
        "later-statement",
        "requires",
        "cmdlet-error",
        "parse-error",
        "unknown-pipeline-command",
        "shown-and-silenced",
        "exit-0",
        "cleared",
        "printed-text-and-quotes",
        "named-blocks",
        "named-blocks-exit",
    ],
)
async def test_exit_status_and_error_records_follow_the_last_statement(
    tmp_path: Path, command: str, exit_code: int, error_record: bool, shows: tuple[str, ...]
) -> None:
    data = await _run(tmp_path, command)

    assert data["exit_code"] == exit_code
    output = data["output"]
    assert output.count(POWERSHELL_ERROR_NOTE) == (1 if error_record else 0)
    for text in shows:
        assert text in output
    assert "silenced-sentinel" not in output
    # The wrapper's own statements never fail visibly.
    assert "__vbot" not in output


@pytest.mark.asyncio
async def test_successful_write_after_method_error_reports_record_without_changing_exit(
    tmp_path: Path,
) -> None:
    (tmp_path / "input.txt").write_text("source text", encoding="utf-8")
    data = await _run(
        tmp_path,
        "$text = Get-Content -Raw input.txt; $piece = $text.Substring(4, -1); "
        "Set-Content -LiteralPath result.txt -Value ('written-after-error:' + $piece)",
    )

    assert data["exit_code"] == 0
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "written-after-error:\n"
    assert data["output"].count(POWERSHELL_ERROR_NOTE) == 1
    assert "Substring" in data["output"]


@pytest.mark.asyncio
@pytest.mark.parametrize("new_error", [False, True])
async def test_full_existing_error_collection_uses_identity_instead_of_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, new_error: bool
) -> None:
    def with_profile_errors(command: str) -> str:
        return (
            "$Error.Clear(); 1..300 | ForEach-Object { "
            "Write-Error 'profile-sentinel' -ErrorAction SilentlyContinue }; "
            + powershell_command(command)
        )

    monkeypatch.setattr(shell_module, "powershell_command", with_profile_errors)
    command = "try { throw 'new-sentinel' } catch {}; 'finished'" if new_error else "'finished'"

    data = await _run(tmp_path, command)

    assert data["exit_code"] == 0
    assert (POWERSHELL_ERROR_NOTE in data["output"]) is new_error
    assert ("new-sentinel" in data["output"]) is new_error


@pytest.mark.asyncio
async def test_handled_error_record_has_bounded_completion_evidence(tmp_path: Path) -> None:
    # A caught throw names no command, so it counts although nothing showed it.
    data = await _run(
        tmp_path, "try { throw ('recorded-sentinel:' + ('x' * 10000)) } catch {}; 'finished'"
    )

    assert data["exit_code"] == 0
    output = data["output"]
    assert output.count(POWERSHELL_ERROR_NOTE) == 1 and "recorded-sentinel:" in output
    assert "finished" in output
    assert len(output) < 1000


@pytest.mark.asyncio
async def test_output_keeps_non_ascii_text(tmp_path: Path) -> None:
    (tmp_path / "Übersicht €.txt").write_text("x", encoding="utf-8")
    command = "using namespace System.Text\n" + "; ".join(
        [
            "[StringBuilder]::new('ß').ToString()",
            "Write-Output 'Jürgen €'",
            "Get-ChildItem -Name -File",
            # A native program's console output, shown and captured.
            "cmd /c echo ä€",
            "$line = cmd /c echo ö; Write-Output $line",
            # A native program receives the text PowerShell pipes to it as UTF-8.
            f"'piped ü' | & '{sys.executable}' -c "
            "\"import sys; print(sys.stdin.buffer.read().decode('utf-8').strip())\"",
            "[Console]::Error.WriteLine('fäil')",
        ]
    )

    data = await _run(tmp_path, command)

    assert data["exit_code"] == 0
    lines = [line.strip() for line in data["output"].splitlines() if line.strip()]
    # stderr can interleave with stdout, so only the set of lines is stable.
    assert sorted(lines) == sorted(
        ["ß", "Jürgen €", "Übersicht €.txt", "ä€", "ö", "piped ü", "fäil"]
    )
