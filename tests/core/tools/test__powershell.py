"""PowerShell UTF-8 output setup placement."""

from __future__ import annotations

import pytest

from core.tools._powershell import (
    EXIT_STATUS_STATEMENT,
    UTF8_OUTPUT_STATEMENT,
    powershell_command,
    with_utf8_output,
)

SETUP = UTF8_OUTPUT_STATEMENT


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
    assert with_utf8_output(command) == expected


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
    assert with_utf8_output(command) == f"{SETUP}\n{command}"


def test_unterminated_leading_construct_keeps_setup_before_it() -> None:
    # PowerShell reports the original syntax error; the setup adds no new one.
    assert with_utf8_output("param('unterminated") == f"{SETUP}\nparam('unterminated"
    assert with_utf8_output("using namespace A\nparam(") == f"using namespace A\n{SETUP}\nparam("


@pytest.mark.parametrize(
    "command",
    ["python -c 'raise SystemExit(5)'", "param($x) $x", "using namespace System.Text\n1"],
)
def test_command_ends_with_the_exit_status_statement(command: str) -> None:
    wrapped = powershell_command(command)

    assert wrapped == f"{with_utf8_output(command)}\n{EXIT_STATUS_STATEMENT}"


@pytest.mark.parametrize("command", ["begin { 'b' } end { 'e' }", "param($x) process { $x }"])
def test_named_block_script_keeps_powershell_exit_status(command: str) -> None:
    # A script of named blocks admits no statement after its blocks.
    assert powershell_command(command) == with_utf8_output(command)
