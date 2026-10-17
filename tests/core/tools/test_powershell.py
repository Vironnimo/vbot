"""Windows commands: the PowerShell bootstrap runs the command as written.

Each real-PowerShell case starts ``pwsh -NoProfile -NonInteractive -Command`` with
the bootstrap on pipes through ``CommandManager`` and sends the command as its
request, as the shell Tool does when no shell waits for a command (about one
second), so a case exists only for a distinct path through the bootstrap;
cases that share a path share a process.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from core.sessions import SessionAddress
from core.tools import _powershell_host
from core.tools._command_launch import PipeLauncher
from core.tools._powershell import (
    OUTPUT_COLUMNS,
    REQUEST_UNREADABLE_EXIT_CODE,
    PowerShellCulture,
    powershell_bootstrap,
    powershell_culture,
    powershell_literal,
    powershell_request,
)
from core.tools.command_manager import CommandManager, CommandReport, NotRunReason
from core.tools.shell_environment import RunIdentity, command_environment

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("pwsh") is None, reason="Real PowerShell 7"
)


async def _run(
    cwd: Path, command: str, *, prefix: str = "", cache_dir: Path | None = None
) -> tuple[int | None, str]:
    """Run *command* as the shell Tool does; return its exit code and output.

    *prefix* runs before the bootstrap.
    """
    report = await _report(cwd, command, prefix=prefix, cache_dir=cache_dir)
    transcript = report.transcript
    assert transcript.omitted_lines == 0
    return report.exit_code, "".join(f"{line}\n" for line in (*transcript.head, *transcript.tail))


async def _report(
    cwd: Path,
    command: str,
    *,
    prefix: str = "",
    cache_dir: Path | None = None,
    workdir: Path | None = None,
    sent: Callable[[bytes], bytes] | None = None,
) -> CommandReport:
    """Run *command* as the shell Tool does; return the report of its end.

    *workdir* is the request's workdir instead of *cwd*, the shell's start
    directory; *sent* turns the request into what reaches the shell.
    """
    layers = command_environment(RunIdentity("agent-a", "session-a", None))
    # Share compiled code across this run's cases without touching the user's cache.
    if cache_dir is None:
        cache_dir = cwd.parent / "powershell-host-cache"
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    request = powershell_request(command, str(workdir or cwd), layers.changes)
    manager = CommandManager(launcher=PipeLauncher(warm=False))
    try:
        handle = await manager.launch(
            SessionAddress(None, "agent-a", "session-a"),
            [
                pwsh,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                prefix + powershell_bootstrap(cache_dir),
            ],
            command=command,
            description=None,
            cwd=cwd,
            env=layers.base,
            origin_run_id="run-a",
            formatter=str,
            request=request.data if sent is None else sent(request.data),
            not_run_codes=request.not_run_codes,
        )
        assert await manager.wait(handle.command_id, seconds=60) == "ended"
        return manager.report(handle.command_id)
    finally:
        await manager.aclose()


def _watch_host_compilation(markers: Path, *, gate: Path | None = None) -> str:
    """Observe real compiler calls without changing the command runspace."""
    directory = powershell_literal(str(markers))
    pause = ""
    if gate is not None:
        pause = (
            "$deadline = [DateTime]::UtcNow.AddSeconds(10); "
            f"while (-not [IO.File]::Exists({powershell_literal(str(gate))})) {{ "
            "if ([DateTime]::UtcNow -gt $deadline) { throw 'compiler gate timed out' }; "
            "[Threading.Thread]::Sleep(10) }; "
        )
    return (
        f"[IO.File]::WriteAllText([IO.Path]::Combine({directory}, \"$PID.ready\"), 'ready'); "
        "function Add-Type { [CmdletBinding()] param([string]$TypeDefinition, "
        "[string]$OutputAssembly) "
        f'[IO.File]::AppendAllText([IO.Path]::Combine({directory}, "$PID.compile"), '
        '$(if ($OutputAssembly) { "disk`n" } else { "memory`n" })); '
        + pause
        + "Microsoft.PowerShell.Utility\\Add-Type @PSBoundParameters }; "
    )


def _compilation_attempts(markers: Path) -> list[str]:
    return [line for path in markers.glob("*.compile") for line in path.read_text().splitlines()]


@pytest.mark.asyncio
async def test_host_cache_reuses_valid_code_and_rebuilds_invalid_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "host-cache"
    markers = tmp_path / "compiler"
    markers.mkdir()
    prefix = _watch_host_compilation(markers)
    command = "'cache-output'; cmd /c exit 5"
    expected = (5, "cache-output\n")

    assert await _run(tmp_path, command, prefix=prefix, cache_dir=cache) == expected
    assert _compilation_attempts(markers) == ["disk"]
    (entry,) = cache.glob("*.bin")
    original = entry.read_bytes()

    # A fresh process must load the entry without invoking any compiler path.
    forbidden = "function Add-Type { throw 'compiler must not run on a hit' }; "
    assert await _run(tmp_path, command, prefix=forbidden, cache_dir=cache) == expected
    assert entry.read_bytes() == original

    corrupt = bytearray(original)
    corrupt[-1] ^= 1
    for attempts, invalid in enumerate((bytes(corrupt), b"\0" * (1024 * 1024 + 1)), start=2):
        entry.write_bytes(invalid)
        assert await _run(tmp_path, command, prefix=prefix, cache_dir=cache) == expected
        assert _compilation_attempts(markers) == ["disk"] * attempts
        assert 32 < entry.stat().st_size < 1024 * 1024

    # Changing the actual C# input, even without a behavioral change, changes the
    # cache identity. The old entry is neither loaded nor overwritten.
    monkeypatch.setattr(
        _powershell_host, "HOST_SOURCE", _powershell_host.HOST_SOURCE + "\n// new\n"
    )
    assert await _run(tmp_path, command, prefix=prefix, cache_dir=cache) == expected
    assert _compilation_attempts(markers) == ["disk"] * 4
    assert len(list(cache.glob("*.bin"))) == 2
    assert all(path.suffix in {".bin", ".lock"} for path in cache.iterdir())


@pytest.mark.asyncio
async def test_host_cache_failure_preserves_command_output_and_exit(tmp_path: Path) -> None:
    # A file where a cache directory is required is never a usable cache. Its
    # contents must survive while the command uses in-memory compilation.
    cache = tmp_path / "not-a-directory"
    cache.write_text("keep this file")
    markers = tmp_path / "compiler"
    markers.mkdir()

    result = await _run(
        tmp_path,
        "[Console]::Error.WriteLine('cache-output'); $Host.SetShouldExit(7)",
        prefix=_watch_host_compilation(markers),
        cache_dir=cache,
    )

    assert result == (7, "cache-output\n")
    assert _compilation_attempts(markers) == ["memory"]
    assert cache.read_text() == "keep this file"


@pytest.mark.asyncio
async def test_concurrent_host_cache_misses_publish_one_complete_entry(tmp_path: Path) -> None:
    cache = tmp_path / "host-cache"
    markers = tmp_path / "compiler"
    markers.mkdir()
    gate = tmp_path / "compile-now"
    prefix = _watch_host_compilation(markers, gate=gate)
    async with asyncio.TaskGroup() as group:
        calls = [
            group.create_task(_run(tmp_path, "'cache-output'", prefix=prefix, cache_dir=cache))
            for _ in range(3)
        ]
        try:
            async with asyncio.timeout(10):
                while len(list(markers.glob("*.ready"))) != 3:
                    await asyncio.sleep(0.01)
        finally:
            gate.touch()

    assert [call.result() for call in calls] == [(0, "cache-output\n")] * 3
    assert _compilation_attempts(markers) == ["disk"]
    assert len(list(cache.glob("*.bin"))) == 1
    assert all(path.suffix in {".bin", ".lock"} for path in cache.iterdir())
    forbidden = "function Add-Type { throw 'compiler must not run on a hit' }; "
    assert await _run(tmp_path, "'cache-output'", prefix=forbidden, cache_dir=cache) == (
        0,
        "cache-output\n",
    )


# --- Exit status, errors and text --------------------------------------------


# The user's format, which the shell Tool's description names; read without a shell.
_CULTURE = powershell_culture() or PowerShellCulture("unread", "unread", "unread")


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
    ("command", "exit_code", "shows"),
    [
        # A param block keeps its meaning, and the last program's exit status becomes
        # the command's exit code.
        (
            f"param($code = 3) & '{sys.executable}' -c \"raise SystemExit($code)\"",
            3,
            (),
        ),
        # A later statement's success decides.
        (f"& '{sys.executable}' -c 'raise SystemExit(5)'\n'after'", 0, ("after",)),
        # A call reports its last statement's native exit code, as bash reports a
        # function's status, through functions, script blocks and scripts; a later
        # statement inside the call decides instead.
        (
            "Set-Content fail.ps1 'cmd /c exit 6'\nfunction f { & { & .\\fail.ps1 } }\nf",
            6,
            (),
        ),
        (
            "Set-Content fail.ps1 'exit 5'; Set-Alias s .\\fail.ps1; function f { s }; f",
            5,
            (),
        ),
        ("function f { cmd /c exit 5; 'after-call' }\nf", 0, ("after-call",)),
        # What ran decides, not the text: through conditions and try blocks, never
        # a program that did not run, and an error after a program leaves $?.
        ("function f { if ($true) { try { cmd /c exit 5 } finally {} } }\nf", 5, ()),
        (
            "cmd /c exit 7\nfunction f { return 'returned-sentinel'; cmd /c exit 0 }\nf",
            0,
            ("returned-sentinel",),
        ),
        ("cmd /c exit 5; Get-Item missing-sentinel", 1, ("missing-sentinel",)),
        # Actual native starts include resolved CommandInfo, aliases and commands
        # hidden from the script debugger. A manually written variable is no exit.
        ("$command = Get-Command cmd; & $command /c exit 7", 7, ()),
        ("Set-Alias c cmd; c /c exit 5", 5, ()),
        ("function f { [System.Diagnostics.DebuggerHidden()] param(); cmd /c exit 7 }; f", 7, ()),
        (
            "function f { Set-Variable LASTEXITCODE 7 -Scope Global }; f; "
            "function g { Set-Item Variable:global:LASTEXITCODE 9 }; g",
            0,
            (),
        ),
        ("cmd /c exit 7; Get-Item silenced-sentinel -ErrorAction Ignore", 1, ()),
        (
            "$PSNativeCommandUseErrorActionPreference = $true; function f { cmd /c exit 5 }; f",
            5,
            ("NativeCommandExitException",),
        ),
        (
            "$PSNativeCommandUseErrorActionPreference = $true; "
            "cmd /c exit 5; Get-Item silenced-sentinel -ErrorAction Ignore",
            1,
            ("NativeCommandExitException",),
        ),
        # #requires keeps its meaning at the start of the command.
        ("#requires -Version 7\n'requires-sentinel'", 0, ("requires-sentinel",)),
        # PowerShell's messages are English whatever the display language; numbers
        # and dates keep the user's own regional format, the one vBot read.
        (
            "(Get-UICulture).Name\n(Get-Culture).Name\n3.14\n"
            "[datetime]::new(2026, 10, 13).ToString('d')\nGet-Item does-not-exist",
            1,
            (
                f"en-US\n{_CULTURE.name}\n{_CULTURE.number}\n{_CULTURE.date}\n",
                "Cannot find path",
                "does-not-exist",
            ),
        ),
        # A syntax error names the command's own line, in UTF-8, and runs nothing.
        (
            "'not-run'\nWrite-Output ('ä' + )",
            1,
            (
                "ParserError",
                "2 |  Write-Output ('ä' + )",
                "You must provide a value expression following the '+' operator.",
            ),
        ),
        # An unknown pipeline command exits instead of waiting for input.
        (
            "Get-ChildItem . | vbot_missing_pipeline_command",
            1,
            ("vbot_missing_pipeline_command", "is not recognized"),
        ),
        # An error behind a successful end shows as PowerShell printed it, and a
        # silenced one stays silent, also when it fails the last statement.
        ("Get-Item shown-sentinel\n'finished'", 0, ("shown-sentinel", "finished")),
        # An error shows between the rows printed before and after it.
        (
            "1..3 | ForEach-Object { if ($_ -eq 2) { Write-Error 'between-sentinel' } "
            'else { [pscustomobject]@{ N = "row-$_" } } }',
            0,
            ("row-1\nWrite-Error: between-sentinel\nrow-3",),
        ),
        (
            "Get-ChildItem silenced-sentinel -ErrorAction SilentlyContinue | Select-Object Name",
            1,
            (),
        ),
        (
            f"Remove-Item silenced-sentinel -ea 0\n& '{sys.executable}' -c 'raise SystemExit(1)'",
            1,
            (),
        ),
        # Explicit control flow keeps its meaning; break and continue outside a loop
        # end the command with the status plain pwsh reports.
        ("Write-Error 'recorded-sentinel'; exit 0", 0, ()),
        ("Write-Error 'break-sentinel'; break\n'not-run'", 1, ("break-sentinel",)),
        ("$Host.SetShouldExit(7); cmd /c exit 3", 7, ()),
        ("$Host.SetShouldExit(7); exit 3", 3, ()),
        ("$Host.SetShouldExit(7); throw 'host-sentinel'", 7, ("host-sentinel",)),
        # The command can change its language mode without disabling the runner
        # which reports its actual status, or causing bootstrap errors to leak.
        (
            "$ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage'; cmd /c exit 5",
            5,
            (),
        ),
        (
            "$ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage'; "
            "Get-Item silenced-sentinel -ErrorAction Ignore",
            1,
            (),
        ),
        ("$ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage'; exit 3", 3, ()),
        # Printed error text, warnings, and native stderr pass unchanged. Quotes
        # reach PowerShell unchanged, including typographic ones.
        (
            _PRINTED_ERROR_TEXT + "\n$text = @'\nit's ‘typographic’\n'@\n$text",
            0,
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
            ("begin-sentinel\nend-sentinel",),
        ),
        ("begin { 'begin-sentinel' } process { cmd /c exit 3 }", 3, ("begin-sentinel",)),
        # A clean block runs last and decides, wherever it stands in the text; its
        # errors are dropped as in plain pwsh.
        (
            "end { 'end-sentinel' } clean { Write-Error 'silenced-sentinel' }",
            1,
            ("end-sentinel",),
        ),
        ("clean { cmd /c exit 7 } end { 'end-sentinel' }", 7, ("end-sentinel",)),
        ("end { Write-Error 'end-sentinel' } clean { exit 7 }", 1, ("end-sentinel",)),
        (
            "end { function global:Write-Error { 'silenced-sentinel' }; "
            "$PSDefaultParameterValues = @{'Write-Error:OutVariable' = 'global:helperSideEffect'}; "
            "$PSModuleAutoLoadingPreference = 'None'; cmd /c exit 5 } "
            "clean { [Console]::WriteLine('clean: ' + $?); "
            "[Console]::WriteLine('exists: ' + "
            "[bool]$ExecutionContext.SessionState.PSVariable.Get('helperSideEffect')); "
            "[Console]::WriteLine('defaults: ' + $PSDefaultParameterValues.Count) }",
            0,
            ("clean: False\nexists: False\ndefaults: 1",),
        ),
        # A top-level return before the end keeps the last statement's status.
        ("return (Write-Error 'return-sentinel')\n'not-run'", 1, ("Write-Error: return-sentinel",)),
        ("return (cmd /c exit 7)", 7, ()),
        ("return @(cmd /c exit 7)", 7, ()),
        ("return @(cmd /c exit 7; 'after-call')", 0, ("after-call",)),
        # Objects the table formatter holds still print before the exit; a native
        # program has no terminal, for input or output.
        (
            "Get-Location\n[pscustomobject]@{ Name = 'table-sentinel' } | Select-Object Name\n"
            f"& '{sys.executable}' -c "
            "\"import sys; print('tty', sys.stdin.isatty(), sys.stdout.isatty())\"",
            0,
            ("table-sentinel", "tty False False"),
        ),
        # A formatted row keeps the commands' width instead of a pipe's 120 columns.
        (
            "[pscustomobject]@{ Name = 'wide-sentinel'; Value = 'v' * 300 } | Format-Table",
            0,
            ("wide-sentinel " + "v" * (OUTPUT_COLUMNS - len("wide-sentinel ") - 2) + "…",),
        ),
    ],
    ids=[
        "param-block-native-exit",
        "later-statement",
        "called-native-exit",
        "called-script-alias-exit",
        "called-later-statement",
        "native-in-blocks",
        "native-that-never-ran",
        "error-after-native",
        "resolved-native-command",
        "native-alias",
        "debugger-hidden-native",
        "written-exit-variable-is-no-program",
        "ignored-error-after-native",
        "native-error-preference",
        "native-error-then-ignored-error",
        "requires",
        "english-messages-own-culture",
        "parse-error",
        "unknown-pipeline-command",
        "shown-error",
        "error-between-rows",
        "silenced-last-statement",
        "silenced-then-native-exit",
        "exit-0",
        "break-after-error",
        "host-exit-after-native",
        "explicit-exit-after-host-exit",
        "host-exit-and-throw",
        "language-mode-native",
        "language-mode-error",
        "language-mode-exit",
        "printed-text-and-quotes",
        "named-blocks",
        "named-blocks-exit",
        "clean-block",
        "clean-block-first",
        "clean-exit-keeps-failure",
        "clean-observes-failure",
        "top-level-return",
        "returned-native-exit",
        "returned-native-array",
        "returned-array-later-output",
        "formatted-objects-and-native-program-without-tty",
        "wide-table",
    ],
)
async def test_exit_status_follows_the_last_statement_and_output_is_the_commands(
    tmp_path: Path, command: str, exit_code: int, shows: tuple[str, ...]
) -> None:
    code, output = await _run(tmp_path, command)

    assert code == exit_code, output
    for text in shows:
        assert text in output
    assert "silenced-sentinel" not in output
    # vBot prints nothing of its own, and its statements never fail visibly. The
    # command's own paths may name vBot: tests run under <repo>-data/.tmp.
    own_output = output.replace(str(tmp_path), "")
    assert "vBot" not in own_output and "__vbot" not in own_output


@pytest.mark.asyncio
async def test_the_command_starts_like_a_new_shell(tmp_path: Path) -> None:
    # Errors recorded before the command, as the bootstrap's own can be, are gone.
    prefix = "Write-Error 'before-sentinel' -ErrorAction SilentlyContinue; "
    names = "(Get-Variable).Name -join ','\n(Get-ChildItem function:).Name -join ','"
    command = (
        f"{names}\n'errors: ' + $Error.Count + ', last exit code: ' + ($null -eq $LASTEXITCODE)\n"
        # Traces and error positions show the command's own lines and none of vBot's.
        "Set-PSDebug -Trace 1\nfunction f { cmd /c exit 4 }\nf\n"
        "$ErrorView = 'NormalView'; Write-Error 'stopped' -ErrorAction Stop"
    )
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    plain = await asyncio.create_subprocess_exec(
        pwsh,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        names,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        cwd=tmp_path,
    )
    new_shell, _ = await plain.communicate()

    code, output = await _run(tmp_path, command, prefix=prefix)

    assert code == 1
    lines = output.splitlines()
    # The same variables and functions as a new pwsh: none of the bootstrap's.
    assert lines[:2] == new_shell.decode().splitlines()
    assert lines[2] == "errors: 0, last exit code: True"
    assert [line for line in lines if line.startswith("DEBUG:")] == [
        "DEBUG:    6+  >>>> f",
        "DEBUG:    5+ function f  >>>> { cmd /c exit 4 }",
        "DEBUG:    5+ function f {  >>>> cmd /c exit 4 }",
        "DEBUG:    5+ function f { cmd /c exit 4  >>>> }",
        "DEBUG:    7+  >>>> $ErrorView = 'NormalView'; Write-Error 'stopped' -ErrorAction Stop",
        "DEBUG:    7+ $ErrorView = 'NormalView';  >>>> Write-Error 'stopped' -ErrorAction Stop",
    ]
    assert "At line:7 char:28\n" in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("begin", "runs"),
    [
        ("", True),
        ("$null = @($input)", False),
        ("if ($false) { $global:input }", False),
        ("function unused { $input }", True),
    ],
    ids=["standalone", "empty-input", "lexical-input", "nested-input"],
)
async def test_empty_console_input_preserves_process_block_effects(
    tmp_path: Path, begin: str, runs: bool
) -> None:
    # ConsoleHost treats a root reference to $input as an input stream. With
    # closed stdin that stream has no objects, so process must never run.
    marker = tmp_path / "process.txt"
    command = (
        f"begin {{ {begin} }} process {{ Add-Content -LiteralPath '{marker}' -Value 'executed' }} "
        "end { 'end-sentinel' }"
    )

    code, output = await _run(tmp_path, command)

    assert (code, output) == (0, "end-sentinel\n")
    assert marker.exists() is runs
    if runs:
        assert marker.read_text() == "executed\n"


@pytest.mark.asyncio
async def test_an_initial_language_policy_runs_nothing_and_reports_why(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"

    report = await _report(
        tmp_path,
        f"New-Item -ItemType File -Path '{marker}'",
        prefix="$ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage'; ",
    )

    assert report.not_run == "language_mode_unavailable"
    assert report.transcript.total_lines == 0
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["default", "custom", "failed", "exit-nested", "host-exit"])
async def test_nested_prompt_uses_the_commands_scope_and_returns_at_eof(
    tmp_path: Path, kind: str
) -> None:
    marker = tmp_path / "prompt.txt"
    command = ""
    if kind in {"custom", "host-exit"}:
        command = (
            f"function prompt {{ Add-Content -LiteralPath '{marker}' -Value $NestedPromptLevel; "
            "'custom:' + $NestedPromptLevel + '>' }; "
        )
    elif kind == "failed":
        command = "function prompt { throw 'suppressed' }; "
    elif kind == "exit-nested":
        command = "function prompt { $Host.ExitNestedPrompt(); 'not-printed' }; "
    if kind == "host-exit":
        command += "$Host.SetShouldExit(7); "
    command += "$Host.EnterNestedPrompt(); $Host.EnterNestedPrompt(); 'after'"
    if kind == "failed":
        command += "; $Error.Count; $?"
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    plain = await asyncio.create_subprocess_exec(
        pwsh,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=tmp_path,
    )
    direct, _ = await plain.communicate()

    code, output = await _run(tmp_path, command)

    assert code == plain.returncode
    assert output == direct.decode().replace("\r\n", "\n")
    if kind == "custom":
        # One invocation per shell: the second nested prompt sees the same EOF.
        assert marker.read_text() == "1\n1\n"
    else:
        assert not marker.exists()


def _without_terminator(request: bytes) -> bytes:
    return request[: request.rindex(b".\n")]


def _cut_in_command(characters: int) -> Callable[[bytes], bytes]:
    def cut(request: bytes) -> bytes:
        command = request.splitlines()[2]
        return request[: request.index(command) + characters]

    return cut


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sent", "workdir", "not_run"),
    [
        (_without_terminator, None, "request_unreadable"),
        (_cut_in_command(6), None, "request_unreadable"),
        # Base64 that decodes, cut where a quantum ends.
        (_cut_in_command(8), None, "request_unreadable"),
        # A shell that waited elsewhere enters a workdir removed since the call was checked.
        (None, "gone", "workdir_unavailable"),
    ],
    ids=["no-terminator", "cut-in-command", "cut-at-base64-boundary", "workdir-gone"],
)
async def test_a_request_the_shell_cannot_read_or_enter_runs_nothing_and_says_so(
    tmp_path: Path,
    sent: Callable[[bytes], bytes] | None,
    workdir: str | None,
    not_run: NotRunReason,
) -> None:
    marker = tmp_path / "ran.txt"
    command = f"New-Item -ItemType File -Path '{marker}'"

    report = await _report(
        tmp_path, command, workdir=tmp_path / workdir if workdir else None, sent=sent
    )

    assert report.not_run == not_run
    assert report.transcript.total_lines == 0
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sent", [None, lambda _request: b""], ids=["command-exits-with-it", "request-without-code"]
)
async def test_an_exit_code_alone_never_says_that_nothing_ran(
    tmp_path: Path, sent: Callable[[bytes], bytes] | None
) -> None:
    # Only the code drawn for the request says so: a command that ran can exit with
    # any other, also with the code a request too short to carry its own ends with.
    marker = tmp_path / "ran.txt"
    command = (
        f"$null = New-Item -ItemType File -Path '{marker}'; exit {REQUEST_UNREADABLE_EXIT_CODE}"
    )

    report = await _report(tmp_path, command, sent=sent)

    assert (report.exit_code, report.not_run) == (REQUEST_UNREADABLE_EXIT_CODE, None)
    assert report.transcript.total_lines == 0
    assert marker.exists() is (sent is None)


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
async def test_native_output_keeps_its_bytes_and_timing(tmp_path: Path) -> None:
    """No decoding or newline buffering, also through an alias or a function.

    stderr passes as bytes too: merging the command's errors into its output
    does not capture a native program's stderr.
    """
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    gate = tmp_path / "continue"
    source = (
        "import pathlib,sys,time; "
        "sys.stderr.buffer.write(bytes([67,13,68,254])); sys.stderr.buffer.flush(); "
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
        powershell_bootstrap(tmp_path.parent / "powershell-host-cache"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=tmp_path,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(powershell_request(command, str(tmp_path), {}).data)
        await process.stdin.drain()
        process.stdin.close()
        # The producer waits for the test: bytes must arrive before it exits or
        # emits a newline. An output stage that captures native output fails here.
        prefix = await asyncio.wait_for(process.stdout.readexactly(4), timeout=5)
        assert prefix == b"A\rB\xff"
        gate.touch()
        output, errors = await asyncio.wait_for(process.communicate(), timeout=5)
        assert process.returncode == 0, errors
        assert errors == b"C\rD\xfe" * 3
        assert prefix + output == b"A\rB\xff" * 3
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
