"""PowerShell command analysis: the plan table against real pwsh, and the helper's lifecycle.

The table needs PowerShell 7 on Windows, the only host where the Shell Tool
runs PowerShell; one helper serves the whole module. The lifecycle tests use a
fake helper and run everywhere.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from core.permissions.commands.commands import (
    CommandAnalysisUnavailable,
    CommandAnalyzer,
    PowerShellHelperLimits,
)
from tests.core.permissions.commands.support import CWD, HOME, Expected, case, check

_PWSH = shutil.which("pwsh")
_ENV = {"USERPROFILE": str(HOME), "HOME": str(HOME), "PATH": ""}


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def analyzer() -> AsyncIterator[CommandAnalyzer]:
    assert _PWSH is not None
    analyzer = CommandAnalyzer(powershell_executable=Path(_PWSH))
    yield analyzer
    await analyzer.aclose()


@pytest.mark.skipif(sys.platform != "win32" or _PWSH is None, reason="PowerShell 7 on Windows")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        case("Get-ChildItem", id="listing reads the cwd", paths=("read w/p",)),
        case(
            "rm -Recurse build; del *.tmp",
            id="aliases resolve and bind",
            commands=("Remove-Item -Recurse build", "Remove-Item *.tmp"),
            paths=("write w/p/build/**", "write w/p/*.tmp/**"),
        ),
        case("rm -rf build", id="binding error is unresolved", unresolved=("path_write",)),
        case(
            "cd src; Remove-Item x",
            id="cd keeps the old folder as candidate",
            paths=("start_dir w/p/src", "write w/p/x/**", "write w/p/src/x/**"),
        ),
        case(
            "Set-Location $env:NOPE; Remove-Item x",
            id="dynamic cd leaves later relative paths unresolved",
            paths=("write w/p/x/**",),
            unresolved=("cwd", "path_write"),
        ),
        case(
            "Set-Location -LiteralPath 'q*'; cd [s]ub; Remove-Item x",
            id="a -Path wildcard picks the folder when it runs",
            paths=("start_dir w/p/q*", "write w/p/x/**", "write w/p/q*/x/**"),
            unresolved=("cwd", "path_write"),
        ),
        case(
            "Start-Process cmd -WorkingDirectory s?b",
            id="start-process matches its folder too",
            unresolved=("cwd",),
        ),
        case(
            "Push-Location C:\\q; Pop-Location; cd..; D:",
            id="location stack, cd.. and drive switch",
            paths=("start_dir q", "start_dir w/p", "start_dir w", "start_dir .", "start_dir D:/"),
        ),
        case(
            "Get-Content ~\\s.txt, $env:USERPROFILE\\t.txt | Set-Content out.txt",
            id="home and environment expand",
            paths=("read h/s.txt", "read h/t.txt", "write w/p/out.txt"),
        ),
        case(
            "$p = 'x'; Remove-Item $p",
            id="assigned variable is dynamic",
            unresolved=("path_write",),
        ),
        case(
            "gci -Recurse | ? { $_.Length -gt 1 } | Remove-Item",
            id="pipeline-fed write through a filter",
            commands=(
                "Get-ChildItem -Recurse",
                "Where-Object <{ $_.Length -gt 1 }>",
                "Remove-Item",
            ),
            paths=("read w/p/**", "write w/p/**"),
        ),
        case(
            "gci | % { $_ } | rm",
            id="pipeline-fed write after a script block is unresolved",
            paths=("read w/p",),
            unresolved=("path_write",),
        ),
        case(
            "iex 'Remove-Item C:\\x'; iex $c; & $tool; . $profile",
            id="invoke-expression and dynamic names",
            paths=("write x/**",),
            unresolved=("command",),
        ),
        case(
            'pwsh -NoProfile -Command "Remove-Item C:\\a"; '
            "pwsh -EncodedCommand UgBlAG0AbwB2AGUALQBJAHQAZQBtACAAQwA6AFwAYgA=; "
            'cmd /c "del C:\\c.txt"; bash -c "cat ~/d"',
            id="nested shells unwrap",
            paths=("write a/**", "write b/**", "write c.txt", "read h/d"),
        ),
        case(
            "icacls --% C:\\x /grant",
            id="stop-parsing is unresolved",
            commands=("icacls --% C:\\x /grant",),
            unresolved=("command",),
        ),
        case(
            "Copy-Item a.txt -Destination D:\\b.txt; New-Item -Path logs -Name today; "
            "Rename-Item c.txt d.txt",
            id="cmdlet parameters",
            paths=(
                "read w/p/a.txt",
                "write D:/b.txt",
                "write w/p/logs/today",
                "write w/p/c.txt/**",
                "write w/p/d.txt/**",
            ),
        ),
        case(
            "Start-Process notepad -ArgumentList 'C:\\s.txt' -WorkingDirectory C:\\w2",
            id="start-process runs its program",
            commands=(
                "Start-Process notepad -ArgumentList C:\\s.txt -WorkingDirectory C:\\w2",
                "notepad C:\\s.txt",
            ),
            paths=("start_dir w2", "read s.txt"),
        ),
        case(
            "echo hi > C:\\o.txt; echo hi > $null",
            id="redirects",
            paths=("write o.txt",),
        ),
        case(
            "git diff ..\\o --stat; icacls.exe C:\\x /grant Everyone:F; "
            "Invoke-WebRequest https://e.example/f -OutFile f.bin",
            id="programs, tier B and URLs",
            paths=("read w/o/** (git)", "write x", "write w/p/f.bin"),
            urls=("https://e.example/f",),
        ),
        case(
            "Remove-Item C:foo, D:bar, \\r, \\\\s\\h\\f, \\\\?\\C:\\l, HKCU:\\x, env:V",
            id="windows path forms",
            paths=("write w/p/foo/**", "write r/**", "write //s/h/f/**", "write l/**"),
            unresolved=("path_write",),
        ),
        case(
            "Set-Alias rm2 Remove-Item; rm2 x; function f { rm y }; f",
            id="redefined names",
            paths=("write w/p/y/**",),
            unresolved=("command",),
        ),
        case(
            "[IO.File]::WriteAllText('a.txt', 'x'); [System.IO.File]::ReadAllText(\"..\\b\"); "
            "[IO.Directory]::Delete('d', $true)",
            id="static .NET file calls",
            commands=(),
            paths=("write w/p/a.txt", "read w/b", "write w/p/d/**"),
        ),
        case(
            "gci *.log | % { $_.Delete() }; (Get-Item a.txt).Delete(); "
            "(New-Object IO.StreamWriter 'w.txt').Close()",
            id="methods of listed items and file objects",
            paths=(
                "read w/p/*.log",
                "write w/p/*.log/**",
                "read w/p/a.txt",
                "write w/p/a.txt/**",
                "write w/p/w.txt",
            ),
        ),
        case(
            "[IO.File]::Delete($f); $p = '..\\c.txt'; $o.Foo('x')",
            id="computed .NET paths and path-like strings",
            commands=(),
            paths=("read w/c.txt",),
            unresolved=("path_write",),
        ),
        case(
            "iwr ('http://' + $h); iwr e.example/x; "
            "(New-Object Net.WebClient).DownloadFile('https://e.example/f', 'f')",
            id="computed and scheme-less addresses and web downloads",
            paths=("write w/p/f",),
            unresolved=("url",),
            urls=("http://e.example/x", "https://e.example/f"),
        ),
        case(
            "Get-ChildItem -Depth 2; Import-Module ..\\m.psm1; . ..\\s.ps1; & ..\\t.ps1",
            id="depth recurses and scripts and modules are read",
            paths=("read w/p/**", "read w/m.psm1", "read w/s.ps1", "read w/t.ps1"),
        ),
        case(
            "Get-ChildItem *.md; Select-String -Path *.md x",
            id="wildcards stay patterns below their folder",
            paths=("read w/p/*.md",),
        ),
        case(
            "${C:\\s\\key.txt}; ${C:\\s\\out.txt} = 'a'; ${C:\\s\\n.txt} += 'b'; "
            "${env:PATH}; ${function:prompt}",
            id="drive variables read and write files",
            paths=("read s/key.txt", "write s/n.txt", "write s/out.txt"),
        ),
        case(
            "[IO.Directory]::GetFiles('src', '*.key', 'TopDirectoryOnly'); "
            "(Get-ChildItem a).GetFiles('*.key', $option)",
            id="a search option is recursive unless it says top directory only",
            paths=("read w/p/src", "read w/p/a", "read w/p/a/*/**"),
        ),
        case(
            "(Get-Item a.txt).IsReadOnly = $false; $x.CopyTo('d.txt', 1); $y.Open()",
            id="item properties write and unfollowable file methods are unresolved",
            paths=("read w/p/a.txt", "write w/p/a.txt/**"),
            unresolved=("path_write",),
        ),
        case(
            "New-Item -ItemType SymbolicLink -Path l -Target t; "
            "New-Item -ItemType HardLink -Path h -Value t2",
            id="new links write themselves and what they lead to",
            paths=(
                "write w/p/l",
                "write w/p/l/t/**",
                "write w/p/t/**",
                "write w/p/h",
                "write w/p/t2",
            ),
        ),
        case(
            "Invoke-Thing secrets/x -Mode fast",
            id="words of unknown cmdlets are mentions",
            mentions=("read w/p/secrets/x", "read w/p/fast"),
        ),
        case("Remove-Item (", id="parse error is unresolved", unresolved=("command",)),
    ],
)
async def test_powershell_plan(analyzer: CommandAnalyzer, line: str, expected: Expected) -> None:
    check(await analyzer.analyze(line, shell="powershell", cwd=CWD, env=_ENV, home=HOME), expected)


# Lifecycle ---------------------------------------------------------------------

_EMPTY: dict[str, list[str]] = {"errors": [], "commands": [], "assigned": [], "functions": []}


class _FakeChannel:
    def __init__(self, helper: _FakeHelper) -> None:
        self._helper = helper
        self._lines: asyncio.Queue[bytes] = asyncio.Queue()
        self._lines.put_nowait(b'{"ready": true}\n')
        self.closed = False

    async def write_line(self, line: bytes) -> None:
        if self.closed:
            raise OSError("closed")
        request = json.loads(line)
        self._helper.requests += 1
        behavior = self._helper.behaviors.pop(0) if self._helper.behaviors else "answer"
        if behavior == "answer":
            answer = {"id": request["id"], "ok": True, "result": _EMPTY}
            self._lines.put_nowait(json.dumps(answer).encode() + b"\n")
        elif behavior == "exit":
            self._lines.put_nowait(b"")
        # "hang": never answers

    async def read_line(self) -> bytes:
        return await self._lines.get()

    def memory_bytes(self) -> int | None:
        return self._helper.memory

    async def close(self) -> None:
        self.closed = True


class _FakeHelper:
    def __init__(self, *, starts: int | None = None) -> None:
        self.channels: list[_FakeChannel] = []
        self.behaviors: list[str] = []
        self.requests = 0
        self.memory: int | None = 1
        self._starts = starts

    async def launch(self) -> Any:
        if self._starts is not None and len(self.channels) >= self._starts:
            raise OSError("cannot start")
        channel = _FakeChannel(self)
        self.channels.append(channel)
        return channel


def _analyzer(helper: _FakeHelper, **limits: Any) -> CommandAnalyzer:
    return CommandAnalyzer(
        powershell_executable=None,
        limits=PowerShellHelperLimits(**{"request_seconds": 0.05, **limits}),
        helper_launcher=helper.launch,
    )


async def _analyze(analyzer: CommandAnalyzer) -> None:
    await analyzer.analyze("x", shell="powershell", cwd=CWD, env={}, home=HOME)


@pytest.mark.asyncio
async def test_helper_starts_once_and_closes() -> None:
    helper = _FakeHelper()
    analyzer = _analyzer(helper)
    await _analyze(analyzer)
    await _analyze(analyzer)
    assert len(helper.channels) == 1
    await analyzer.aclose()
    assert helper.channels[0].closed
    with pytest.raises(CommandAnalysisUnavailable):
        await _analyze(analyzer)


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["hang", "exit"])
async def test_failed_request_retries_once_with_a_fresh_helper(behavior: str) -> None:
    helper = _FakeHelper()
    analyzer = _analyzer(helper)
    helper.behaviors = [behavior]
    await _analyze(analyzer)
    assert len(helper.channels) == 2 and helper.channels[0].closed
    helper.behaviors = [behavior, behavior]
    with pytest.raises(CommandAnalysisUnavailable):
        await _analyze(analyzer)
    await analyzer.aclose()


@pytest.mark.asyncio
async def test_helper_restarts_after_request_and_memory_limits() -> None:
    helper = _FakeHelper()
    analyzer = _analyzer(helper, max_requests=2, max_memory_bytes=100)
    for _ in range(3):
        await _analyze(analyzer)
    assert len(helper.channels) == 2 and helper.channels[0].closed
    helper.memory = 101
    await _analyze(analyzer)
    assert helper.channels[1].closed
    await analyzer.aclose()


@pytest.mark.asyncio
async def test_failed_start_fails_fast_until_the_retry_window_ends() -> None:
    helper = _FakeHelper(starts=0)
    analyzer = _analyzer(helper, retry_after_seconds=60)
    with pytest.raises(CommandAnalysisUnavailable):
        await _analyze(analyzer)
    with pytest.raises(CommandAnalysisUnavailable):
        await _analyze(analyzer)
    assert helper.channels == []
    await analyzer.aclose()


@pytest.mark.asyncio
async def test_idle_helper_stops() -> None:
    helper = _FakeHelper()
    analyzer = _analyzer(helper, idle_seconds=0)
    await _analyze(analyzer)
    for _ in range(5):
        await asyncio.sleep(0)
    assert helper.channels[0].closed
    await _analyze(analyzer)
    assert len(helper.channels) == 2
    await analyzer.aclose()
