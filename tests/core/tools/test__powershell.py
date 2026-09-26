"""PowerShell setup placement and the head/tail/wc fallbacks."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core.tools._powershell import (
    EXIT_STATUS_STATEMENT,
    SETUP_STATEMENT,
    powershell_command,
    with_setup,
)

SETUP = SETUP_STATEMENT


@pytest.mark.parametrize(
    "command, expected",
    [
        ("Write-Output hi", f"{SETUP}\nWrite-Output hi"),
        ("[Console]::WriteLine('x')", f"{SETUP}\n[Console]::WriteLine('x')"),
        # using statements must stay first, including comments before them.
        (
            "using namespace System.Text; [StringBuilder]::new('ok')",
            f"using namespace System.Text;\n{SETUP}\n [StringBuilder]::new('ok')",
        ),
        (
            "# note\n<# block #> USING namespace System.IO\nusing module @{ModuleName='A;B'}\n1",
            f"# note\n<# block #> USING namespace System.IO\nusing module @{{ModuleName='A;B'}}\n"
            f"{SETUP}\n1",
        ),
        ("using namespace System.Text", f"using namespace System.Text\n{SETUP}\n"),
        # A script param block, optionally with attributes, must stay first too.
        ("param($x = 'a;b') $x", f"param($x = 'a;b')\n{SETUP}\n $x"),
        (
            '[CmdletBinding()]\nparam([string]$Name = "x)")\n$Name',
            f'[CmdletBinding()]\nparam([string]$Name = "x)")\n{SETUP}\n\n$Name',
        ),
        # Named blocks must contain every statement.
        ("begin { 'b' } end { 'e' }", f"begin {{{SETUP}\n 'b' }} end {{ 'e' }}"),
        ("param($x) process { $x }", f"param($x) process {{{SETUP}\n $x }}"),
    ],
)
def test_setup_statement_precedes_ordinary_statements(command: str, expected: str) -> None:
    assert with_setup(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "using-thing arg",  # a command name, not the using keyword
        "endless-loop",
        "param",
        "[string]$value = 'x'",
    ],
)
def test_ordinary_leading_statements_get_setup_first(command: str) -> None:
    assert with_setup(command) == f"{SETUP}\n{command}"


def test_unterminated_leading_construct_keeps_setup_before_it() -> None:
    # PowerShell reports the original syntax error; the setup adds no new one.
    assert with_setup("param('unterminated") == f"{SETUP}\nparam('unterminated"
    assert with_setup("using namespace A\nparam(") == f"using namespace A\n{SETUP}\nparam("


@pytest.mark.parametrize(
    "command",
    ["python -c 'raise SystemExit(5)'", "param($x) $x", "using namespace System.Text\n1"],
)
def test_command_ends_with_the_exit_status_statement(command: str) -> None:
    wrapped = powershell_command(command)

    assert wrapped == f"{with_setup(command)}\n{EXIT_STATUS_STATEMENT}"


@pytest.mark.parametrize("command", ["begin { 'b' } end { 'e' }", "param($x) process { $x }"])
def test_named_block_script_keeps_powershell_exit_status(command: str) -> None:
    # A script of named blocks admits no statement after its blocks.
    assert powershell_command(command) == with_setup(command)


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


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("pwsh") is None, reason="Real PowerShell 7"
)
@pytest.mark.parametrize(
    ("command", "expected"),
    [
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
    ],
)
def test_line_filters_run_where_powershell_has_none(tmp_path, command, expected) -> None:
    (tmp_path / "a.txt").write_text("".join(f"line {n}\n" for n in range(1, 21)))
    (tmp_path / "b.txt").write_text("x\ny\n")

    assert _pwsh_without_unix_tools(command, tmp_path) == (0, expected)


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("pwsh") is None, reason="Real PowerShell 7"
)
@pytest.mark.parametrize(
    ("command", "message"),
    [
        (
            "head -c 5 a.txt; 'after'",
            "head: -c is not supported here; use head -n N or Select-Object -First N.",
        ),
        (
            "tail -f a.txt",
            "tail: -f is not supported here; use tail -n N or Select-Object -Last N.",
        ),
        (
            "wc -w a.txt",
            "wc: -w is not supported here; use wc -l, or Measure-Object -Word or -Character.",
        ),
        (
            "wc a.txt",
            "wc: only wc -l is supported here; use Measure-Object -Word or -Character for other "
            "counts.",
        ),
    ],
)
def test_unsupported_line_filter_options_stop_with_the_equivalent(
    tmp_path, command, message
) -> None:
    (tmp_path / "a.txt").write_text("text\n")

    exit_code, output = _pwsh_without_unix_tools(command, tmp_path)

    assert exit_code == 1
    assert message in output
    assert "after" not in output


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("pwsh") is None, reason="Real PowerShell 7"
)
def test_other_missing_commands_still_fail_as_not_found(tmp_path) -> None:
    exit_code, output = _pwsh_without_unix_tools("vbot-no-such-tool --flag", tmp_path)

    assert exit_code == 1
    assert "vbot-no-such-tool" in output
    assert "not supported here" not in output
