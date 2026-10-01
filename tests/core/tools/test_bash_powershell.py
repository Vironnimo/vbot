"""The shell Tool on Windows: PowerShell wrapping, exit status, error records, and text.

Each real-PowerShell case starts a ``pwsh`` process (about half a second), so a case
exists only for a distinct path through the wrapper; cases that share a path share a
process.
"""

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
from core.tools._powershell import POWERSHELL_ERROR_NOTE, SETUP_STATEMENT, powershell_command
from core.tools.bash import register_bash_tool
from core.tools.process_manager import ProcessManager
from core.tools.tools import ToolRegistry
from tests.core.tools.bash_test_support import make_context
from tests.core.tools.bash_test_support import manager as manager
from tests.core.tools.bash_test_support import shell_env_cache as shell_env_cache

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
    # The command travels as a single-quoted literal, parsed after the setup.
    assert "\n$__vbotCommand = 'it''s ‘‘x’’'\n" in powershell_command("it's ‘x’")

    monkeypatch.setattr(bash_module.sys, "platform", "linux")

    assert bash_module._shell_argv("echo hello") == ["bash", "-c", "echo hello"]


# --- Line filters PowerShell lacks -----------------------------------------


def _pwsh_without_unix_tools(command: str, cwd: Path) -> tuple[int, str, str]:
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


@real_powershell
def test_line_filters_run_where_powershell_has_none(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("".join(f"line {n}\n" for n in range(1, 21)))
    (tmp_path / "b.txt").write_text("x\ny\n")
    # One PowerShell process runs every filter; a marker line separates them.
    script = "\n'--'\n".join(command for command, _output in _LINE_FILTERS)

    exit_code, output, errors = _pwsh_without_unix_tools(script, tmp_path)

    assert exit_code == 0, errors
    assert output.split("--\n") == [expected for _command, expected in _LINE_FILTERS], errors


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
        # A param block keeps its meaning, and the last program's exit status becomes
        # the command's exit code.
        ('param($code = 3) python -c "raise SystemExit($code)"', 3, False, ()),
        ("python -c 'raise SystemExit(5)'; Write-Output after", 0, False, ("after",)),
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
    assert "silenced-sentinel" not in output
    # The wrapper's own statements never fail visibly.
    assert "__vbot" not in output


@real_powershell
@pytest.mark.asyncio
async def test_successful_write_after_method_error_reports_record_without_changing_exit(
    manager: ProcessManager, tmp_path: Path
) -> None:
    (tmp_path / "input.txt").write_text("source text", encoding="utf-8")
    result = await _dispatch(
        manager,
        tmp_path,
        "$text = Get-Content -Raw input.txt; $piece = $text.Substring(4, -1); "
        "Set-Content -LiteralPath result.txt -Value ('written-after-error:' + $piece)",
    )
    assert result["ok"] is True
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
    command = "try { throw 'new-sentinel' } catch {}; 'finished'" if new_error else "'finished'"
    result = await _dispatch(manager, tmp_path, command)
    assert result["data"]["exit_code"] == 0
    assert (POWERSHELL_ERROR_NOTE in result["data"]["output"]) is new_error
    assert ("new-sentinel" in result["data"]["output"]) is new_error


@real_powershell
@pytest.mark.asyncio
async def test_handled_error_record_has_bounded_completion_evidence(
    manager: ProcessManager, tmp_path: Path
) -> None:
    # A caught throw names no command, so it counts although nothing showed it.
    result = await _dispatch(
        manager,
        tmp_path,
        "try { throw ('recorded-sentinel:' + ('x' * 10000)) } catch {}; 'finished'",
    )
    assert result["data"]["exit_code"] == 0
    output = result["data"]["output"]
    assert output.count(POWERSHELL_ERROR_NOTE) == 1 and "recorded-sentinel:" in output
    assert "finished" in output
    assert len(output) < 1000


# --- Input and text --------------------------------------------------------


@real_powershell
@pytest.mark.asyncio
async def test_stdin_reaches_its_end(manager: ProcessManager, tmp_path: Path) -> None:
    # Commands that read stdin see its end at once; pipelines into child processes
    # are covered by test_bash.py::test_shell_pipeline_and_script_owned_input_remain_available.
    result = await _dispatch(
        manager,
        tmp_path,
        'Write-Output "x: $input"\n'
        "$input | ForEach-Object { $_ }\n"
        '$read = [Console]::In.ReadToEnd(); Write-Output "read: $read"\n'
        "'alpha','beta' | ForEach-Object { $_.ToUpper() }",
    )

    assert result["ok"] is True
    assert result["data"]["exit_code"] == 0
    assert [line.rstrip() for line in result["data"]["output"].strip().splitlines()] == [
        "x:",
        "read:",
        "ALPHA",
        "BETA",
    ]


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
