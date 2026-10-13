"""Windows commands: the PowerShell bootstrap's exit status, errors, filters and text.

Each real-PowerShell case starts ``pwsh -NoProfile -NonInteractive -Command`` with
the bootstrap on pipes through ``CommandManager`` and sends the command as its
request, as the shell Tool does when no shell waits for a command (about a third
of a second), so a case exists only for a distinct path through the bootstrap;
cases that share a path share a process.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from core.sessions import SessionAddress
from core.tools._command_launch import PipeLauncher
from core.tools._powershell import (
    OUTPUT_COLUMNS,
    POWERSHELL_BOOTSTRAP,
    POWERSHELL_ERROR_NOTE,
    POWERSHELL_SILENCED_ERROR_NOTE,
    powershell_request,
)
from core.tools.command_manager import CommandManager
from core.tools.shell_environment import RunIdentity, command_environment

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("pwsh") is None, reason="Real PowerShell 7"
)


# --- Line filters PowerShell lacks -----------------------------------------


async def _run(
    cwd: Path,
    command: str,
    *,
    path: str | None = None,
    prefix: str = "",
    workdir: Path | None = None,
    sent: Callable[[bytes], bytes] | None = None,
) -> tuple[int | None, str]:
    """Run *command* as the shell Tool does; return its exit code and output.

    *path* replaces PATH in the shell's environment; *prefix* runs before the bootstrap.
    *workdir* is the request's workdir instead of *cwd*, the shell's start directory;
    *sent* turns the request into what reaches the shell.
    """
    layers = command_environment(RunIdentity("agent-a", "session-a", None))
    environment = layers.base
    if path is not None:
        environment = {
            **{name: value for name, value in environment.items() if name.upper() != "PATH"},
            "PATH": path,
        }
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    request = powershell_request(command, str(workdir or cwd), layers.changes)
    manager = CommandManager(launcher=PipeLauncher(warm=False))
    try:
        handle = await manager.launch(
            SessionAddress(None, "agent-a", "session-a"),
            [pwsh, "-NoProfile", "-NonInteractive", "-Command", prefix + POWERSHELL_BOOTSTRAP],
            command=command,
            description=None,
            cwd=cwd,
            env=environment,
            origin_run_id="run-a",
            formatter=str,
            request=request if sent is None else sent(request),
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
    # The caught stops leave error records, which the bootstrap notes on the error stream.
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
        # The command sees none of the bootstrap's variables.
        (
            f"& '{sys.executable}' -c 'raise SystemExit(5)'\n"
            "'after, variables: ' + @(Get-Variable __vbot*).Count",
            0,
            False,
            ("after, variables: 0",),
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
        # A last statement that failed with an error the command silenced exits with 1,
        # and the error stream names that error; a native program's own code needs no note.
        (
            "Get-ChildItem hidden-sentinel -ErrorAction SilentlyContinue | Select-Object Name",
            1,
            False,
            (POWERSHELL_SILENCED_ERROR_NOTE, "hidden-sentinel"),
        ),
        (
            f"Remove-Item silenced-sentinel -ea 0\n& '{sys.executable}' -c 'raise SystemExit(1)'",
            1,
            False,
            (),
        ),
        # Different output shapes get their own table, including output from
        # control flow. Captures keep objects until they reach the output boundary.
        (
            "function Records { [pscustomobject]@{Count=3} }; $rows = Records; "
            "$rows | Select-Object Count; "
            "if ($true) { [pscustomobject]@{Name='name-sentinel'} }; "
            "'capture-type: ' + $rows.Count.GetType().Name; "
            "[pscustomobject]@{Name='file-sentinel'; Length=9} | Select-Object Name,Length; "
            "if ($true) { 'match-sentinel' | Select-String match | Select-Object -First 1 }",
            0,
            False,
            (
                "Count",
                "3",
                "name-sentinel",
                "capture-type: Int32",
                "file-sentinel",
                "match-sentinel",
            ),
        ),
        # A shape compares property names directly, not an ambiguous joined key.
        # Empty type-name lists and an explicit output formatter also remain valid.
        (
            "Set-StrictMode -Version Latest; "
            "[pscustomobject]@{'a,b'=11;c=22}; [pscustomobject]@{a=33;'b,c'=44}; "
            "$o = [pscustomobject]@{Name='empty-types-sentinel'}; "
            "$o.PSObject.TypeNames.Clear(); $o; "
            "[pscustomobject]@{Custom='list-sentinel'} | Format-List; "
            "function Out-Default { process { 'hijacked-sentinel' } }; "
            "[pscustomobject]@{Name='private-formatter-sentinel'}",
            0,
            False,
            (
                "11",
                "22",
                "33",
                "44",
                "empty-types-sentinel",
                "Custom : list-sentinel",
                "private-formatter-sentinel",
            ),
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
    ],
    ids=[
        "param-block-native-exit",
        "later-statement",
        "requires",
        "cmdlet-error",
        "parse-error",
        "unknown-pipeline-command",
        "shown-and-silenced",
        "silenced-last-statement",
        "silenced-then-native-exit",
        "mixed-output-and-captures",
        "formatting-boundaries",
        "exit-0",
        "cleared",
        "printed-text-and-quotes",
        "named-blocks",
        "named-blocks-exit",
        "formatted-objects-and-native-program-without-tty",
        "wide-table",
    ],
)
async def test_exit_status_and_error_records_follow_the_last_statement(
    tmp_path: Path, command: str, exit_code: int, error_record: bool, shows: tuple[str, ...]
) -> None:
    code, output = await _run(tmp_path, command)

    assert code == exit_code, output
    assert output.count(POWERSHELL_ERROR_NOTE) == (1 if error_record else 0)
    silenced = POWERSHELL_SILENCED_ERROR_NOTE in shows
    assert output.count(POWERSHELL_SILENCED_ERROR_NOTE) == (1 if silenced else 0)
    for text in shows:
        assert text in output
    assert "silenced-sentinel" not in output
    # The bootstrap's own statements never fail visibly.
    assert "__vbot" not in output


def _without_terminator(request: bytes) -> bytes:
    return request[: request.rindex(b".\n")]


def _cut_in_command(characters: int) -> Callable[[bytes], bytes]:
    def cut(request: bytes) -> bytes:
        command = request.splitlines()[2]
        return request[: request.index(command) + characters]

    return cut


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sent", "workdir"),
    [
        (_without_terminator, None),
        (_cut_in_command(6), None),
        # Base64 that decodes, cut where a quantum ends.
        (_cut_in_command(8), None),
        (lambda _request: b"", None),
        # A shell that waited elsewhere enters a workdir removed since the call was checked.
        (None, "gone"),
    ],
    ids=["no-terminator", "cut-in-command", "cut-at-base64-boundary", "empty", "workdir-gone"],
)
async def test_a_request_the_shell_cannot_read_or_enter_runs_nothing(
    tmp_path: Path, sent: Callable[[bytes], bytes] | None, workdir: str | None
) -> None:
    marker = tmp_path / "ran.txt"
    command = f"New-Item -ItemType File -Path '{marker}'"

    code, output = await _run(
        tmp_path, command, workdir=tmp_path / workdir if workdir else None, sent=sent
    )

    assert code == 1, output
    assert not marker.exists(), output


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
    # The request's second line switches the rewrite on.
    switch = powershell_request(command, "C:/work", {}).splitlines()[1]
    assert switch == (b"1" if rewritten else b"0")


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
    # Errors a profile left behind fill $Error to its capacity before the command.
    profile_errors = (
        "$Error.Clear(); 1..300 | ForEach-Object { "
        "Write-Error 'profile-sentinel' -ErrorAction SilentlyContinue }; "
    )
    command = "try { throw 'new-sentinel' } catch {}; 'finished'" if new_error else "'finished'"

    code, output = await _run(tmp_path, command, prefix=profile_errors)

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


@pytest.mark.asyncio
async def test_final_formatter_keeps_native_bytes_and_partial_output(tmp_path: Path) -> None:
    """No native decoding or newline buffering, even after dynamic command resolution."""
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    gate = tmp_path / "continue"
    source = (
        "import pathlib,sys,time; "
        "sys.stdout.buffer.write(bytes([65,13,66,255])); sys.stdout.buffer.flush(); "
        f"gate=pathlib.Path({str(gate)!r}); "
        "deadline=time.monotonic()+8; "
        'exec("while not gate.exists() and time.monotonic()<deadline: time.sleep(.01)")'
    )
    # Single-quoted PowerShell source preserves the Python quotes and backslashes.
    literal = "'" + source.replace("'", "''") + "'"
    python = "'" + sys.executable.replace("'", "''") + "'"
    command = (
        f"& {python} -c {literal}; "
        f"Set-Alias echo {python} -Option AllScope; echo -c {literal}; "
        f"function Emit-Bytes {{ & {python} -c {literal} }}; Emit-Bytes"
    )
    process = await asyncio.create_subprocess_exec(
        pwsh,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        POWERSHELL_BOOTSTRAP,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=tmp_path,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(powershell_request(command, str(tmp_path), {}))
        await process.stdin.drain()
        process.stdin.close()
        # The producer waits for the test: bytes must arrive before it exits or
        # emits a newline. A formatter that captures native output fails here.
        prefix = await asyncio.wait_for(process.stdout.readexactly(4), timeout=5)
        assert prefix == b"A\rB\xff"
        gate.touch()
        output, errors = await asyncio.wait_for(process.communicate(), timeout=5)
        assert process.returncode == 0, errors
        assert not errors
        assert prefix + output == b"A\rB\xff" * 3
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
