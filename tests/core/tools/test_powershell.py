"""Windows commands: the PowerShell wrapper's exit status, errors, filters and text.

Each real-PowerShell case starts ``pwsh -NoProfile -NonInteractive -Command`` on
pipes through ``CommandManager``, as the shell Tool does (about half a second),
so a case exists only for a distinct path through the wrapper; cases that share
a path share a process.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from core.sessions import SessionAddress
from core.tools._powershell import (
    NULL_DEVICE_STATEMENTS,
    OUTPUT_COLUMNS,
    POWERSHELL_ERROR_NOTE,
    powershell_command,
)
from core.tools.command_manager import CommandManager
from core.tools.shell_environment import RunIdentity, command_environment

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


async def _run(
    cwd: Path,
    command: str,
    *,
    path: str | None = None,
    wrap: Callable[[str], str] = powershell_command,
) -> tuple[int | None, str]:
    """Run the wrapped *command* as the shell Tool does; return its exit code and output.

    *path* replaces PATH in the command environment.
    """
    environment = command_environment(RunIdentity("agent-a", "session-a", None))
    if path is not None:
        environment = {
            **{name: value for name, value in environment.items() if name.upper() != "PATH"},
            "PATH": path,
        }
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    manager = CommandManager()
    try:
        handle = await manager.launch(
            SessionAddress(None, "agent-a", "session-a"),
            [pwsh, "-NoProfile", "-NonInteractive", "-Command", wrap(command)],
            command=command,
            description=None,
            cwd=cwd,
            env=environment,
            origin_run_id="run-a",
            formatter=str,
        )
        assert await manager.wait(handle.command_id, seconds=60) == "ended"
        report = manager.report(handle.command_id)
    finally:
        await manager.aclose()
    transcript = report.transcript
    assert transcript.omitted_lines == 0
    return report.exit_code, "".join(f"{line}\n" for line in (*transcript.head, *transcript.tail))


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


@pytest.mark.asyncio
async def test_line_filters_run_where_powershell_has_none(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("".join(f"line {n}\n" for n in range(1, 21)))
    (tmp_path / "b.txt").write_text("x\ny\n")
    # One PowerShell process runs every filter; a marker line separates them.
    script = "\n'--'\n".join(command for command, _output in _LINE_FILTERS)
    # A PATH without Git's Unix commands.
    path = os.pathsep.join(
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if shutil.which("head", path=entry) is None and shutil.which("wc", path=entry) is None
    )

    exit_code, output = await _run(tmp_path, script, path=path)

    assert exit_code == 0, output
    # The caught stops leave error records, which the wrapper notes on the error stream.
    printed = "".join(
        line
        for line in output.splitlines(keepends=True)
        if not line.startswith(POWERSHELL_ERROR_NOTE)
    )
    assert printed.split("--\n") == [expected for _command, expected in _LINE_FILTERS], output


# --- Exit status, errors and text --------------------------------------------


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
        # PowerShell's messages are English and its numbers take a decimal point
        # without grouping, whatever the display language and regional format; dates
        # keep the user's regional format.
        (
            "$own = [cultureinfo]::new((Get-Culture).Name, $true)\n"
            "'{0:N1} {1} {2}' -f 1234.5, [double]::Parse('3.14'), "
            "((Get-Date '2026-01-02').ToString('d') -ceq "
            "[datetime]::new(2026, 1, 2).ToString('d', $own))\n"
            "Get-Item does-not-exist",
            1,
            False,
            ("1234.5 3.14 True", "Cannot find path", "does-not-exist"),
        ),
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
        # Objects the table formatter holds still print before the exit; a native
        # program has no terminal, for input or output.
        (
            "Get-Location\n[pscustomobject]@{ Name = 'table-sentinel' } | Select-Object Name\n"
            f"& '{sys.executable}' -c "
            "\"import sys; print('tty', sys.stdin.isatty(), sys.stdout.isatty())\"",
            0,
            False,
            ("table-sentinel", "tty False False"),
        ),
        # A formatted row keeps the commands' width instead of a pipe's 120 columns.
        (
            "[pscustomobject]@{ Name = 'wide-sentinel'; Value = 'v' * 300 } | Format-Table",
            0,
            False,
            ("wide-sentinel " + "v" * (OUTPUT_COLUMNS - len("wide-sentinel ") - 2) + "\u2026",),
        ),
        # A prompt fails at once instead of waiting for input that never comes.
        ("Read-Host 'Name'", 1, False, ("NonInteractive mode",)),
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
        "formatted-objects-and-native-program-without-tty",
        "wide-table",
        "read-host",
    ],
)
async def test_exit_status_and_error_records_follow_the_last_statement(
    tmp_path: Path, command: str, exit_code: int, error_record: bool, shows: tuple[str, ...]
) -> None:
    code, output = await _run(tmp_path, command)

    assert code == exit_code, output
    assert output.count(POWERSHELL_ERROR_NOTE) == (1 if error_record else 0)
    for text in shows:
        assert text in output
    assert "silenced-sentinel" not in output
    # The wrapper's own statements never fail visibly.
    assert "__vbot" not in output


@pytest.mark.asyncio
async def test_bash_null_device_discards_and_reaches_native_programs_as_nul(
    tmp_path: Path,
) -> None:
    # A program beside msys-2.0.dll handles /dev/null itself and would create a NUL file.
    msys = tmp_path / "msys"
    msys.mkdir()
    (msys / "msys-2.0.dll").write_bytes(b"")
    (msys / "show.cmd").write_bytes(b"@echo %~1\r\n")
    # The program writes only to arguments that arrived as NUL, so a broken rewrite
    # prints /dev/null and writes nothing.
    program = (
        "import sys; print(sys.argv[1:]); "
        "[open(a, 'w').write('x') for a in sys.argv[1:] if a == 'NUL']"
    )
    command = "\n".join(
        [
            # PowerShell resolves /dev/null on a drive rooted in tmp_path, so a file it
            # writes could only appear there.
            "$null = New-PSDrive vbot FileSystem $PWD.ProviderPath -ErrorAction Stop",
            "Set-Location vbot:\\ -ErrorAction Stop",
            "if ((Get-Location).Drive.Name -ne 'vbot') { exit 99 }",
            "$null = New-Item -ItemType Directory dev",
            # Redirections discard in any case and quoting and keep the columns that
            # show which errors the command silenced.
            "'silenced-sentinel' >/dev/null; Get-Item silenced-sentinel 2> '\\DEV\\NULL'; "
            "Get-Item silenced-sentinel -ea 0",
            f"& '{sys.executable}' -c \"{program}\" /dev/null '\\DEV\\NULL' "
            "--output=/dev/null --log='\\DEV\\NULL' -o:/dev/null",
            "Write-Output /dev/null",
            "& .\\msys\\show.cmd /dev/null",
        ]
    )

    code, output = await _run(tmp_path, command)

    assert code == 0
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    assert sorted(lines) == sorted(
        ["['NUL', 'NUL', '--output=NUL', '--log=NUL', '-o:NUL']", "/dev/null", "/dev/null"]
    )
    assert {path.name for path in tmp_path.iterdir()} == {"dev", "msys"}
    assert not any((tmp_path / "dev").iterdir())
    assert {path.name for path in msys.iterdir()} == {"msys-2.0.dll", "show.cmd"}


@pytest.mark.parametrize(
    ("command", "rewritten"),
    [
        ("python --version", False),
        ("Get-ChildItem -Force | Select-Object -First 3 Name", False),
        ("python x 2>/dev/NULL", True),
        # PowerShell reads these strings as /dev/null without the letters in the text.
        ("python x /dev/nu`ll", True),
        ('python x "/dev/`u{6e}ull"', True),
        ("python x /dev/nu\x01ll", True),
        ("python x /dev/nul­l", True),
    ],
    ids=["native", "cmdlets", "letters", "escape", "unicode-escape", "control", "soft-hyphen"],
)
def test_only_commands_that_can_spell_the_null_device_carry_its_rewrite(
    command: str, rewritten: bool
) -> None:
    assert (NULL_DEVICE_STATEMENTS in powershell_command(command)) is rewritten


@pytest.mark.asyncio
async def test_successful_write_after_method_error_reports_record_without_changing_exit(
    tmp_path: Path,
) -> None:
    (tmp_path / "input.txt").write_text("source text", encoding="utf-8")
    code, output = await _run(
        tmp_path,
        "$text = Get-Content -Raw input.txt; $piece = $text.Substring(4, -1); "
        "Set-Content -LiteralPath result.txt -Value ('written-after-error:' + $piece)",
    )

    assert code == 0
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "written-after-error:\n"
    assert output.count(POWERSHELL_ERROR_NOTE) == 1
    assert "Substring" in output


@pytest.mark.asyncio
@pytest.mark.parametrize("new_error", [False, True])
async def test_full_existing_error_collection_uses_identity_instead_of_count(
    tmp_path: Path, new_error: bool
) -> None:
    def with_profile_errors(command: str) -> str:
        return (
            "$Error.Clear(); 1..300 | ForEach-Object { "
            "Write-Error 'profile-sentinel' -ErrorAction SilentlyContinue }; "
            + powershell_command(command)
        )

    command = "try { throw 'new-sentinel' } catch {}; 'finished'" if new_error else "'finished'"

    code, output = await _run(tmp_path, command, wrap=with_profile_errors)

    assert code == 0
    assert (POWERSHELL_ERROR_NOTE in output) is new_error
    assert ("new-sentinel" in output) is new_error


@pytest.mark.asyncio
async def test_handled_error_record_has_bounded_completion_evidence(tmp_path: Path) -> None:
    # A caught throw names no command, so it counts although nothing showed it.
    code, output = await _run(
        tmp_path, "try { throw ('recorded-sentinel:' + ('x' * 10000)) } catch {}; 'finished'"
    )

    assert code == 0
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

    code, output = await _run(tmp_path, command)

    assert code == 0
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    # stderr can interleave with stdout, so only the set of lines is stable.
    assert sorted(lines) == sorted(
        ["ß", "Jürgen €", "Übersicht €.txt", "ä€", "ö", "piped ü", "fäil"]
    )
