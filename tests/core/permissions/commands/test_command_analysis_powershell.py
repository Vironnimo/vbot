"""PowerShell command analysis: the plan table against real pwsh, and the helper's lifecycle.

The table needs PowerShell 7 on Windows, the only host where the Shell Tool
runs PowerShell; one helper serves the whole module. The recorded native-word
parse tests both platforms' wildcard lowering without starting an external
runtime, and a Windows integration check keeps that recording current.
The lifecycle tests use a fake helper and run everywhere.
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

from core.permissions.commands import _powershell
from core.permissions.commands.commands import (
    CommandAnalysisUnavailable,
    CommandAnalyzer,
    PowerShellHelperLimits,
)
from tests.core.permissions.commands.support import CWD, HOME, Expected, case, check

_PWSH = shutil.which("pwsh")
_ENV = {"USERPROFILE": str(HOME), "HOME": str(HOME), "PATH": ""}
_NATIVE_WILDCARDS_LINE = (
    "python s*/a.py; python 's*/b.py'; python ('s*/c.py'); "
    "python 'q*',s*/d; Get-Content -LiteralPath s*/e"
)
_NATIVE_WILDCARDS_PARSE = json.loads(
    Path(__file__).with_name("powershell_native_wildcards.json").read_text(encoding="utf-8")
)


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
            "$env:GIT_DIR = '../../outside/.git'; git reset --hard; "
            "git config --global user.name x",
            id="git powershell environment redirects and global config writes are checked",
            paths=(
                "read outside/.git",
                "write w/p/** (work tree) (git)",
                "write h/.gitconfig",
                "write h/.config/git/config",
            ),
            unresolved=("path_write",),
        ),
        case(
            "foreach ($f in 'src/a.rs','src/b.rs') { Select-String -Path $f -Pattern x }",
            id="bounded loop literal paths are read individually",
            paths=("read w/p/src/a.rs", "read w/p/src/b.rs"),
        ),
        case(
            "foreach ($f in 'src/a.rs','C:/outside/b.rs') { Set-Content -Path $f -Value x }",
            id="bounded loop writes include every literal target outside cwd",
            paths=("read outside/b.rs", "write w/p/src/a.rs/**", "write outside/b.rs/**"),
        ),
        case(
            "Get-ChildItem tests -Recurse -Filter stderr.txt | "
            "ForEach-Object { Get-Content $_.FullName -Raw }",
            id="bounded loop recursive item properties keep the subtree",
            paths=("read w/p/tests/**",),
        ),
        case(
            "foreach ($f in Get-ChildItem src -Filter *.py) { "
            "Get-Content $f.FullName | Select-Object -First 1 }",
            id="bounded loop foreach resolves listing properties",
            paths=("read w/p/src", "read w/p/src/*"),
        ),
        case(
            "foreach ($f in @(Get-Item src/*.py)) { Get-Content $f.FullName }; "
            "foreach ($p in @('docs/a','docs/b')) { Get-Content $p }",
            id="bounded loop array expressions keep literal and listing targets",
            paths=("read w/p/src/*.py", "read w/p/docs/a", "read w/p/docs/b"),
        ),
        case(
            "foreach ($f in Get-Item src/*.py | Where-Object { $_.Length -gt 0 }) { "
            "Remove-Item $f.FullName }",
            id="bounded loop preserving filters retain glob write bounds",
            paths=("read w/p/src/*.py", "write w/p/src/*.py/**"),
        ),
        case(
            "Get-Item src/*.py | ForEach-Object { Set-Content $_.FullName x }; "
            "Resolve-Path docs/*.md | ForEach-Object { Get-Content $_.Path }; "
            "Select-String -Path tests/*.py -Pattern x | ForEach-Object { Get-Content $_.Path }",
            id="bounded loop item path and match objects keep their source paths",
            paths=(
                "read w/p/src/*.py",
                "write w/p/src/*.py/**",
                "read w/p/docs/*.md",
                "read w/p/tests/*.py",
            ),
        ),
        case(
            "Get-ChildItem src | Select-String -Pattern x | ForEach-Object { Get-Content $_.Path }",
            id="bounded loop matches from FileInfo input retain source bounds",
            paths=("read w/p/src", "read w/p/src/*"),
        ),
        case(
            "$files = git diff --name-only; foreach ($f in $files) { Get-Content $f }; "
            "rg x src | ForEach-Object { Get-Content $_ }",
            id="bounded loop arbitrary program output stays unresolved",
            paths=("read w/p/src/**",),
            unresolved=("path_read",),
        ),
        case(
            "Get-ChildItem src | ForEach-Object { $_ = 'elsewhere'; Get-Content $_ }",
            id="bounded loop reassignment invalidates processing item bounds",
            paths=("read w/p/src",),
            unresolved=("path_read",),
        ),
        case(
            "foreach ($f in 'src/a') { Set-Variable f 'elsewhere'; Get-Content $f }",
            id="bounded loop variable cmdlets invalidate literal element bounds",
            unresolved=("path_read",),
        ),
        case(
            "Get-ChildItem docs | ForEach-Object { "
            "Set-Variable PSItem 'elsewhere'; Get-Content $_.FullName }",
            id="bounded loop PSItem alias mutation invalidates underscore bounds",
            paths=("read w/p/docs",),
            unresolved=("path_read",),
        ),
        case(
            "Get-ChildItem src | ForEach-Object { "
            "try { throw 'x' } catch { Get-Content $_ } }; "
            "Get-ChildItem docs | ForEach-Object -Begin { Get-Content $_.FullName } "
            "-Process { $_ }",
            id="bounded loop catch and begin blocks do not inherit processing items",
            paths=("read w/p/src", "read w/p/docs"),
            unresolved=("path_read",),
        ),
        case(
            "Get-ChildItem src | ForEach-Object { Write-Output elsewhere | "
            "ForEach-Object { Get-Content $_ } }; Get-Content $_",
            id="bounded loop nested and out of scope input stays unresolved",
            paths=("read w/p/src",),
            unresolved=("path_read",),
        ),
        case(
            "foreach ($f in 'src/a','src/b') { Get-Content $f.FullName }",
            id="bounded loop literal strings do not acquire file object properties",
            unresolved=("path_read",),
        ),
        case(
            "Get-Item a.txt | % { $_.IsReadOnly = $true }; "
            "foreach ($f in Get-Item b.txt) { $f.Attributes = 'Normal' }",
            id="bounded loop metadata property writes preserve the item identity",
            paths=("read w/p/a.txt", "write w/p/a.txt/**", "read w/p/b.txt", "write w/p/b.txt/**"),
        ),
        case(
            "Select-String -Path a.txt -Pattern x | % { $_.Path = 'elsewhere'; "
            "Get-Content $_.Path }",
            id="bounded loop mutable path properties invalidate the item bounds",
            paths=("read w/p/a.txt",),
            unresolved=("path_read",),
        ),
        case(
            "Get-ChildItem src/*.log | & { process { Get-Content $_.FullName } }; "
            "Get-ChildItem docs/*.md | & { end { Get-Content $_.FullName } }",
            id="bounded loop inline scriptblocks expose input only in process blocks",
            paths=("read w/p/src/*.log", "read w/p/docs/*.md"),
            unresolved=("path_read",),
        ),
        case(
            "Get-ChildItem src -Name | ForEach-Object { Set-Content $_ x }; "
            "Resolve-Path docs -Relative | ForEach-Object { Set-Content $_ x }; "
            "Select-String -Path tests/*.py -Raw x | ForEach-Object { Set-Content $_ x }",
            id="bounded loop relative names and raw matches cannot retain item bounds",
            paths=("read w/p/src", "read w/p/docs", "read w/p/tests/*.py"),
            unresolved=("path_write",),
        ),
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
            id="once-assigned scalar path is resolved",
            paths=("write w/p/x/**",),
        ),
        case(
            '$base = "$env:USERPROFILE/docs"; $p = "$base/file.txt"; '
            "Get-Content -LiteralPath $p -Raw -Encoding UTF8",
            id="constant interpolation and filesystem provider options",
            paths=("read h/docs", "read h/docs/file.txt"),
        ),
        case(
            'Get-ChildItem "$env:USERPROFILE/docs" -Directory',
            id="known environment path binds filesystem listing options",
            paths=("read h/docs",),
        ),
        case(
            "$p = 'x'; $lines = @(Get-Content -LiteralPath $p); "
            "Set-Content -LiteralPath $p -Value 'new' -Encoding UTF8 -NoNewline; "
            "[IO.File]::ReadAllText($p)",
            id="constant targets in eager subexpressions and member calls",
            paths=("read w/p/x", "write w/p/x"),
        ),
        case(
            "$p = 'x'; $p = 'y'; Remove-Item $p",
            id="reassigned variable stays unknown",
            unresolved=("path_write",),
        ),
        case(
            "$p = 'x'; if (Test-Path $p) { Get-Content $p }; "
            "while ($false) { Get-Content $p }; & { Get-Content $p }",
            id="immutable outer paths survive conditional loop and invoked blocks",
            paths=("read w/p/x",),
        ),
        case(
            "$p = 'x'; if ($true) { $p = 'y' }; Get-Content $p",
            id="a conditional reassignment invalidates the outer path",
            unresolved=("path_read",),
        ),
        case(
            "$dir = 'tmp'; New-Item -ItemType Directory -Force -Path $dir | Out-Null; "
            "$out = Join-Path $dir a.txt; 'x' | Set-Content $out",
            id="join-path establishes a constant without invalidating its inputs",
            paths=("read w/p/tmp", "write w/p/tmp", "write w/p/tmp/a.txt"),
        ),
        case(
            "$out = Join-Path $env:USERPROFILE x.txt; 'hi' | Set-Content $out",
            id="join-path resolves an environment parent",
            paths=("read h", "write h/x.txt"),
        ),
        case(
            "$out = Join-Path -Path C:/outside -ChildPath sub -AdditionalChildPath a.txt; "
            "Set-Content $out hi",
            id="join-path outside the work area remains a checked write",
            paths=("read outside", "write outside/sub/a.txt"),
        ),
        case(
            "$out = Join-Path $PWD a.txt; Set-Content $out hi",
            id="join-path does not freeze the mutable location",
            unresolved=("path_read", "path_write"),
        ),
        case(
            "$out = Join-Path src *.txt -Resolve; Get-Content $out",
            id="join-path filesystem expansion stays unresolved",
            paths=("read w/p/src",),
            unresolved=("path_read",),
        ),
        case(
            "$py = 'C:/Python314/python.exe'; & $py --version; & $py --version",
            id="a constant executable resolves without making later invocations dynamic",
            commands=("python --version", "python --version"),
            paths=("read Python314/python.exe",),
            mentions=("read Python314/python.exe",),
        ),
        case(
            "$reader = 'Get-Content'; $p = 'x'; & $reader $p; "
            "$writer = 'rm'; & $writer C:/outside/x",
            id="constant cmdlet names bind parameters and resolve aliases",
            commands=("Get-Content x", "Remove-Item C:/outside/x"),
            paths=("read w/p/x", "write outside/x/**"),
        ),
        case(
            "$script = 'C:/outside/script.ps1'; . $script",
            id="a constant dot-sourced script is a named script read",
            commands=("script.ps1",),
            paths=("read outside/script.ps1",),
        ),
        case(
            "$script = 'setup.ps1'; $p = 'x'; . $script; Get-Content $p",
            id="opaque dot-sourced scripts invalidate later constant uses",
            paths=("read w/p/setup.ps1",),
            unresolved=("path_read",),
        ),
        case(
            "$setter = 'Set-Variable'; $p = 'x'; & $setter p y; Get-Content $p",
            id="constant invocation names still invalidate mutated variables",
            unresolved=("path_read",),
        ),
        case(
            "$setter = 'Set-Item'; $p = 'x'; & $setter variable:p y; Get-Content $p",
            id="constant invocation names still invalidate provider targets",
            unresolved=("path_read",),
        ),
        case(
            "$function = 'f'; $p = 'x'; function f { 'opaque' }; & $function; Get-Content $p",
            id="constant function names do not hide opaque variable mutations",
            unresolved=("command", "path_read"),
        ),
        case(
            "Get-Content $p; $p = 'x'",
            id="constant is unavailable before its assignment",
            unresolved=("path_read",),
        ),
        case(
            "if ($true) { $p = 'x' }; Get-Content $p",
            id="conditional assignments stay unknown",
            unresolved=("path_read",),
        ),
        case(
            "$p = 'x'; [ref]$p; Remove-Item $p",
            id="reference exposure prevents constant propagation",
            unresolved=("path_write",),
        ),
        case(
            "$p = 'x'; [System.Management.Automation.PSReference]$p; Remove-Item $p",
            id="full reference type prevents constant propagation",
            unresolved=("path_write",),
        ),
        case(
            "$p = 'x'; Get-Content $global:p; Get-Content $script:p",
            id="scoped references never borrow a local constant",
            unresolved=("path_read",),
        ),
        case(
            "$p = 'x'; Set-Variable p y; Remove-Item $p",
            id="variable cmdlet mutation prevents constant propagation",
            unresolved=("path_write",),
        ),
        case(
            "$p = 'x'; Set-Item variable:p y; Remove-Item $p",
            id="provider variable mutation prevents constant propagation",
            unresolved=("path_write",),
        ),
        case(
            "$p = 'x'; $e = 'x'; $w = 'x'; $i = 'x'; $v = 'x'; "
            "Write-Output y -ov +global:p -ErrorVariable script:e "
            "-WarningVariable +local:w -InformationVariable i -PipelineVariable v; "
            "Get-Content $p,$e,$w,$i,$v",
            id="common parameter variables invalidate scalar values including scopes and append",
            unresolved=("path_read",),
        ),
        case(
            "$p = 'x'; Write-Output y -OutVariable $unknown; Remove-Item $p; "
            "Set-Item env:USERPROFILE y; Get-Content $env:USERPROFILE",
            id="unknown common parameter variable prevents constant propagation",
            unresolved=("path_write", "path_read"),
        ),
        case(
            "$p = 'x'; function f { [CmdletBinding()]param(); 'y' }; "
            "f -OutVariable p; Set-Content $p z",
            id="script function common parameters cannot preserve scalar values",
            unresolved=("path_write",),
        ),
        case(
            "$p = 'x'; $v = 'variable:p'; Set-Item $v y; Remove-Item $p",
            id="indirect provider target invalidates its variable",
            unresolved=("path_write",),
        ),
        case(
            "$a = 'x'; $b = 'x'; $c = 'x'; $d = 'x'; "
            "Copy-Item variable:source variable:a; Move-Item variable:source variable:b; "
            "New-Item variable: -Name c -Value y; Rename-Item variable:source -NewName d; "
            "Get-Content $a,$b,$c,$d",
            id="provider destination and item name mutations invalidate scalar values",
            unresolved=("path_read",),
        ),
        case(
            '$p = "$env:USERPROFILE/x"; $v = "env:USERPROFILE"; Set-Item $v y; Get-Content $p -Raw',
            id="indirect environment mutation invalidates dependent constants",
            unresolved=("path_read",),
        ),
        case(
            "$p = 'x'; Set-Variable 'p*' y; Remove-Item $p",
            id="wildcard variable mutation prevents constant propagation",
            unresolved=("path_write",),
        ),
        case(
            "$HOME = 'x'; Remove-Item $HOME",
            id="read-only automatic variables never become constants",
            unresolved=("path_write",),
        ),
        case(
            "$LASTEXITCODE = 'x'; cmd /c exit 1; Remove-Item $LASTEXITCODE; "
            "$Matches = 'x'; 'a' -match 'a'; Get-Content $Matches",
            id="implicitly changed automatic variables never become constants",
            unresolved=("path_write", "path_read"),
        ),
        case(
            "$p = 1e3; Get-Content $p",
            id="numeric source spelling is not a string value",
            unresolved=("path_read",),
        ),
        case(
            "$p = 'x'; function f { Get-Content $p }; f",
            id="constants are not propagated into deferred function bodies",
            unresolved=("path_read",),
        ),
        case(
            "$p = $PWD; Get-Content $p",
            id="mutable shell location is not a frozen scalar",
            unresolved=("path_read",),
        ),
        case(
            "$p = 'x' > log; Get-Content $p",
            id="redirected assignment has no known result",
            paths=("write w/p/log",),
            unresolved=("path_read",),
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
            "Invoke-WebRequest ftp://evil.example/x; Invoke-RestMethod intranet "
            "-Proxy http://proxy.example:8080; curl.exe --preproxy socks5://evil.example:1080 https://ok.example",
            id="web cmdlets and native downloads check every network target",
            urls=("http://proxy.example:8080", "http://intranet", "https://ok.example"),
            hosts=("evil.example",),
        ),
        case(
            "Start-BitsTransfer -Source https://ok.example/x -Destination C:/outside/x; "
            "Start-BitsTransfer https://ok.example/y y; "
            "BitsTransfer\\Start-BitsTransfer -Sou https://ok.example/z -Dest:z",
            id="BITS named positional abbreviated and qualified destinations are writes",
            paths=("write outside/x/**", "write w/p/y/**", "write w/p/z/**"),
            urls=("https://ok.example/x", "https://ok.example/y", "https://ok.example/z"),
        ),
        case(
            "Start-BitsTransfer -Source https://ok.example/x,https://ok.example/y "
            "-Destination x,y; Start-BitsTransfer -Source upload.txt -Destination "
            "https://ok.example/upload -TransferType Upload -ProxyList http://proxy.example:8080",
            id="BITS checks every transfer endpoint and proxy",
            paths=("write w/p/x/**", "write w/p/y/**", "read w/p/upload.txt"),
            urls=(
                "https://ok.example/x",
                "https://ok.example/y",
                "https://ok.example/upload",
                "http://proxy.example:8080",
            ),
        ),
        case(
            "Start-BitsTransfer https://ok.example/x; "
            "Start-BitsTransfer -Source https://ok.example/x -Destination $TARGET",
            id="BITS omitted destination writes cwd and computed destination refuses",
            paths=("write w/p/**",),
            urls=("https://ok.example/x",),
            unresolved=("path_write",),
        ),
        case(
            "Import-Csv inputs.csv | Start-BitsTransfer; "
            "Start-BitsTransfer @options; Start-BitsTransfer x y -NotifyCmdLine cmd.exe,'/c del z'",
            id="BITS piped splatted and deferred command effects stay unresolved",
            paths=("read w/p/inputs.csv", "read w/p/x", "write w/p/y/**", "write w/p/**"),
            unresolved=("path_read", "path_write", "command"),
        ),
        case(
            "Get-ChildItem *.md; Select-String -Path *.md x",
            id="wildcards stay patterns below their folder",
            paths=("read w/p/*.md",),
        ),
        case(
            "git status | Select-String changed; cargo --version | Select-String version; "
            "python --version | Select-String Python",
            id="known native output is searched as text",
        ),
        case(
            "& 'C:\\tools\\viewer.exe' --version | Select-String version",
            id="explicit native program is a deny-checked mention and emits text",
            commands=("viewer --version", "Select-String version"),
            mentions=("read tools/viewer.exe",),
        ),
        case(
            "Get-Content notes.txt | Where-Object { $_ } | Select-Object -First 5 | "
            "Select-String note | Select-String again",
            id="text stays text through preserving filters and text matches",
            paths=("read w/p/notes.txt",),
        ),
        case(
            "Get-ChildItem | Out-String | Select-String name",
            id="out-string searches a listing display without reading listed contents",
            paths=("read w/p",),
        ),
        case(
            "Get-ChildItem | Select-String contents",
            id="file objects still read the files they list",
            paths=("read w/p", "read w/p/*"),
        ),
        case(
            "Get-Content paths.txt | Remove-Item",
            id="text consumed as paths still needs known targets",
            paths=("read w/p/paths.txt",),
            unresolved=("path_write",),
        ),
        case(
            "$env:PATH -split ';' | Select-String bin; "
            "$lines = Get-Content notes.txt; $lines | Select-String note",
            id="string expressions and a single text assignment stay text",
            paths=("read w/p/notes.txt",),
        ),
        case(
            "unknown-command | Select-String value; $objects | Select-String value",
            id="unknown output may contain file objects",
            unresolved=("path_read",),
        ),
        case(
            "function git { Get-Item notes.txt }; git | Select-String note; "
            "pwsh -NoProfile -Command 'Get-Item notes.txt' | Select-String note",
            id="functions and PowerShell hosts do not promise native text output",
            paths=("read w/p/notes.txt",),
            unresolved=("path_read",),
        ),
        case(
            "Get-Content paths.txt | Select-Object -ExpandProperty File | Select-String x",
            id="property expansion does not preserve the text-output guarantee",
            paths=("read w/p/paths.txt",),
            unresolved=("path_read",),
        ),
        case(
            "& ..\\items.ps1 | Select-String x; . ..\\setup.ps1",
            id="scripts retain read checks and unknown object output",
            paths=("read w/items.ps1", "read w/setup.ps1"),
            unresolved=("path_read",),
        ),
        case(
            "Start-Process C:\\tools\\viewer.exe -ArgumentList '--version'",
            id="start-process checks native programs against read denies only",
            mentions=("read tools/viewer.exe",),
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
        case(
            "playwright-cli -s=x snapshot 2>&1 | Select-String -Pattern option; "
            "npx playwright test --reporter=line 2>&1 | Select-String 'passed|failed'; "
            "flutter build web --help 2>&1 | Select-String canvaskit; "
            "pytest -q 2>&1 | Select-String passed",
            id="external program output is text without a per-program spec",
        ),
        case(
            "unknown-command | Remove-Item; $objects | Select-String x",
            id="text classification never invents file targets",
            unresolved=("path_write", "path_read"),
        ),
        case(
            "$s = Get-Content -Raw a.txt; $s.Replace('C:/outside/', '') | Set-Content a.txt; "
            "$t = [IO.File]::ReadAllText('b.txt'); $t = $t.Replace('a', 'b'); "
            "[IO.File]::WriteAllText('b.txt', $t.Replace('C:/outside/', ''))",
            id="proven string variables and self replacement preserve file accesses",
            paths=("read w/p/a.txt", "write w/p/a.txt", "read w/p/b.txt", "write w/p/b.txt"),
        ),
        case(
            "(Get-Content a.txt -Raw).Replace('C:/outside/', ''); "
            "(Get-ChildItem | Out-String).Replace('C:/outside/', ''); "
            "('literal').Replace('C:/outside/', ''); ([string]$value).Replace('a', 'b'); "
            "foreach ($piece in 'a', 'b') { $piece.Replace('C:/outside/', '') }",
            id="direct string receivers do not turn replacement text into paths",
            paths=("read w/p/a.txt", "read w/p"),
        ),
        case(
            "Get-ChildItem src -Filter *.py | Select-String x | ForEach-Object { "
            "\"$($_.Path.Replace('C:\\outside\\', '')):$($_.LineNumber)\""
            " }; Get-ChildItem src -Recurse | ForEach-Object { "
            "$_.FullName.Replace('C:/outside/', '') }",
            id="listing and match string properties do not write files",
            paths=("read w/p/src", "read w/p/src/*", "read w/p/src/**"),
        ),
        case(
            "([IO.FileInfo]::new('a.txt')).FullName.Replace('C:/outside/', ''); "
            "(Get-Item b.txt).Replace('C:/target', 'C:/backup'); "
            "$unknown.Replace('C:/target', 'C:/backup')",
            id="file replacement still writes its source destination and backup",
            paths=(
                "read w/p/a.txt",
                "read w/p/b.txt",
                "write w/p/b.txt/**",
                "write target",
                "write backup",
            ),
            unresolved=("path_write",),
        ),
        case(
            "function files { Get-Item a.txt }; (files).Replace('C:/target', 'C:/backup')",
            id="unfollowable command objects cannot claim to be strings",
            paths=("read w/p/a.txt",),
            unresolved=("path_write",),
        ),
        case(
            "$s = 'text'; $s = Get-Item a.txt; $s.Replace('C:/target', 'C:/backup'); "
            "$t = 'text'; Set-Variable t (Get-Item b.txt); $t.Replace('C:/target', 'C:/backup')",
            id="a changed receiver cannot retain string provenance",
            paths=("read w/p/a.txt", "read w/p/b.txt"),
            unresolved=("path_write",),
        ),
        case(
            "foreach ($f in 'a') { Set-Variable f (Get-Item secret); "
            "$f.Replace('C:/target', 'C:/backup') }",
            id="mutated literal-loop variables cannot retain string provenance",
            paths=("read w/p/secret",),
            unresolved=("path_write",),
        ),
        case(
            r"'a b' -split '\s+'; (Get-Content README.md -Raw) -replace '\{(\w+)\}', '$1'; "
            "'x' -match ('C:/pattern'); 'x' -notlike 'C:/pattern'; -split 'C:/pattern'; "
            "(([System.Xml.XmlDocument]::new()).Load('C:/outside/x.xml')) -match 'x'; "
            r"([System.Xml.XmlDocument]::new()).Load('\outside\direct.xml'); "
            r"(([System.Xml.XmlDocument]::new()).Load(('\outside\wrapped.xml'))) -match 'x'",
            id="regex and pattern operands are text even through parentheses",
            paths=(
                "read w/p/README.md",
                "read outside/x.xml",
                "read outside/direct.xml",
                "read outside/wrapped.xml",
            ),
        ),
        case(
            "rg -n '/api/rpc' server; rg '/api/rpc'; rg -e '/api/rpc' server; "
            "rg -f patterns.txt server",
            id="ripgrep positional and option patterns are not file paths",
            paths=("read w/p/server/**", "read w/p/**", "read w/p/patterns.txt/**"),
        ),
        case(
            "gh api /repos/owner/repo/pulls; playwright-cli find --regex '/Ask this agent/'; "
            "'abc'.StartsWith('/api'); '\\tvar n := 1'; @'\n\\tvar n := 2\n'@",
            id="root-relative routes and literal text are not absolute host reads",
            mentions=(
                "read w/p/api",
                "read repos/owner/repo/pulls",
                "read w/p/find",
                "read Ask this agent",
            ),
        ),
        case(
            r"tool C:/outside/file ../parent \\server\share\file ~/home /api/route \root\word",
            id="clear paths stay reads while root-relative words remain deny-checked mentions",
            paths=("read outside/file", "read w/parent", "read //server/share/file", "read h/home"),
            mentions=("read api/route", "read root/word"),
        ),
        case(
            "docker run --rm -v /data:/data alpine ls /data; docker run -v named:/data alpine",
            id="container destinations and named volumes are not Windows host reads",
            mentions=("read data", "read w/p/run", "read w/p/alpine", "read w/p/ls"),
        ),
        case(
            "docker run --rm -v C:/outside:/data alpine; "
            "docker run -itvC:/readonly:/data:ro alpine; "
            "docker run --volume=C:/other:/data:rw alpine; "
            "docker run -v=C:/equal:/data alpine; "
            "docker container create --mount "
            "type=bind,source=C:/bound,target=/data,readonly alpine",
            id="Docker bind sources retain read or write subtree checks",
            paths=(
                "write outside/**",
                "write readonly",
                "read readonly/**",
                "write other/**",
                "write equal/**",
                "read bound/**",
            ),
        ),
        case(
            "docker run --mount type=bind,source=C:/first,src=C:/second,target=/data,"
            "readonly,ro=false alpine; "
            "docker run --mount TYPE=BIND,SRC=C:/zero,target=/data,ro=0 alpine; "
            "docker run --mount type=bind,src=C:/create,target=/data,ro,bind-create-src alpine; "
            "docker run -v C:/labels:/data:ro,z alpine; "
            "docker run --mount type=bind,src=C:/nested,target=/data,readonly,"
            "bind-recursive=writable alpine",
            id="Docker writable mount options override read-only appearances",
            paths=(
                "write second/**",
                "write zero/**",
                "write create",
                "read create/**",
                "write labels/**",
                "write nested/**",
            ),
        ),
        case("Remove-Item (", id="parse error is unresolved", unresolved=("command",)),
    ],
)
async def test_powershell_plan(analyzer: CommandAnalyzer, line: str, expected: Expected) -> None:
    check(await analyzer.analyze(line, shell="powershell", cwd=CWD, env=_ENV, home=HOME), expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("posix", [False, True], ids=["windows", "posix"])
async def test_pwsh_expands_native_program_wildcards_outside_windows(
    monkeypatch: pytest.MonkeyPatch, posix: bool
) -> None:
    """Outside Windows pwsh expands a native program's unquoted wildcards, as bash does."""
    monkeypatch.setattr(_powershell, "NATIVE_GLOBS", posix)
    helper = _FakeHelper()
    helper.result = _NATIVE_WILDCARDS_PARSE
    analyzer = _analyzer(helper)
    try:
        plan = await analyzer.analyze(
            _NATIVE_WILDCARDS_LINE, shell="powershell", cwd=CWD, env=_ENV, home=HOME
        )
    finally:
        await analyzer.aclose()
    globs = {access.glob for access in plan.paths if access.glob is not None}
    assert globs == ({"s*/a.py", "s*/c.py", "q*", "s*/d"} if posix else set())


@pytest.mark.skipif(sys.platform != "win32" or _PWSH is None, reason="PowerShell 7 on Windows")
@pytest.mark.asyncio
async def test_powershell_native_word_recording_matches_the_helper(
    analyzer: CommandAnalyzer,
) -> None:
    # The helper's JSON-lines protocol is the contract consumed by the lowering.
    assert analyzer._helper is not None
    assert await analyzer._helper.analyze(_NATIVE_WILDCARDS_LINE) == _NATIVE_WILDCARDS_PARSE


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
            answer = {"id": request["id"], "ok": True, "result": self._helper.result}
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
        self.result: dict[str, Any] = _EMPTY
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
