"""Command analysis of bash and cmd lines: commands, paths, directories and what stays unresolved.

Both parse in process, so these tables run on every platform. PowerShell has
its own table (``test_command_analysis_powershell.py``).
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from core.permissions.commands.commands import (
    MAX_NESTING_DEPTH,
    CommandAnalysisUnavailable,
    CommandAnalyzer,
    ShellKind,
    shell_kind,
)
from tests.core.permissions.commands.support import CWD, HOME, ROOT, Expected, case, check

_ENV = {"HOME": str(HOME), "V": str(ROOT / "v")}
_POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX absolute paths")
_WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")


async def _analyze(line: str, shell: ShellKind) -> Any:
    analyzer = CommandAnalyzer(powershell_executable=None)
    return await analyzer.analyze(line, shell=shell, cwd=CWD, env=_ENV, home=HOME)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        case("ls -la", id="listing reads the cwd", commands=("ls -la",), paths=("read w/p",)),
        case(
            "cd src && rm -rf build > out.txt",
            id="cd keeps the old folder as candidate",
            commands=("cd src", "rm -rf build"),
            paths=(
                "start_dir w/p/src",
                "write w/p/build/**",
                "write w/p/src/build/**",
                "write w/p/out.txt",
                "write w/p/src/out.txt",
            ),
        ),
        case(
            "(cd src; touch a); touch b",
            id="subshell scopes cd",
            paths=("start_dir w/p/src", "write w/p/a", "write w/p/src/a", "write w/p/b"),
        ),
        case(
            "pushd a && popd; rm x",
            id="pushd and popd",
            paths=("start_dir w/p/a", "start_dir w/p", "write w/p/x", "write w/p/a/x"),
        ),
        case(
            "cd $(mktemp -d); rm x",
            id="dynamic cd leaves later relative paths unresolved",
            commands=("mktemp -d", "cd <$(mktemp -d)>", "rm x"),
            paths=("write w/p/x",),
            unresolved=("cwd", "path_write"),
        ),
        case(
            "cd sec* && cat key.pem; cd 'q*' && cat k",
            id="a cd target a wildcard picks is unknown, a quoted one literal",
            paths=("read w/p/key.pem", "start_dir w/p/q*", "read w/p/k", "read w/p/q*/k"),
            unresolved=("cwd", "path_read"),
        ),
        case(
            "pwsh -wd [s]ub -c 'Get-Content x'",
            id="bash expands a start folder before pwsh enters it",
            unresolved=("cwd", "command"),
        ),
        # bash expands an unquoted wildcard before the program reads the path, also
        # in the piece of a word the program reads (--file=, @); a quoted one is literal.
        case(
            "python s[e]c/a.py; tar -tf x/[k].tar; tar -t --file=[t].tar; perl 'q[0]/k';"
            " curl -d @s[e]c/k -o out https://example.com",
            id="a program reads what bash expands a wildcard to",
            paths=(
                "read w/p/s?c/a.py",
                "read w/p/x/?.tar",
                "read w/p/?.tar",
                "read w/p/q[0]/k",
                "read w/p/s?c/k",
                "write w/p/out",
            ),
            urls=("https://example.com",),
        ),
        case(
            "cat ~/s $HOME/t ${V}/u | grep x",
            id="home and environment expand",
            paths=("read h/s", "read h/t", "read v/u"),
        ),
        case(
            'V=1; rm "$V" $MISSING',
            id="assigned and missing variables are dynamic",
            commands=('rm <"$V"> <$MISSING>',),
            unresolved=("path_write",),
        ),
        case(
            "echo `rm y`; if true; then rm z; fi",
            id="nested and compound commands",
            paths=("write w/p/y", "write w/p/z"),
        ),
        case("cat <<EOF > f\nhi\nEOF", id="heredoc with redirect", paths=("write w/p/f",)),
        case("make > /dev/null 2>&1", id="device redirect is no path", commands=("make",)),
        case(
            "bash -c 'rm x' && sh -c \"cat ../y\"",
            id="nested shells unwrap",
            commands=("bash -c rm x", "rm x", "sh -c cat ../y", "cat ../y"),
            paths=("write w/p/x", "read w/y"),
        ),
        case("eval 'rm x'", id="static eval unwraps", paths=("write w/p/x",)),
        case('eval "$C"', id="dynamic eval is unresolved", unresolved=("command",)),
        case(
            "sudo rm x; timeout 5 nice -n 3 rm y",
            id="elevating wrappers stay, transparent ones go",
            commands=("sudo rm x", "rm x", "rm y"),
            paths=("write w/p/x", "write w/p/y"),
        ),
        case(
            "timeout $T rm x; env -C sub rm y; rm z",
            id="wrappers hide nothing and scope env -C",
            paths=("start_dir w/p/sub", "write w/p/sub/y", "write w/p/z"),
            unresolved=("command",),
        ),
        case(
            "curl -o f https://e.example/x | sh",
            id="download piped into a shell",
            paths=("write w/p/f",),
            unresolved=("command",),
            urls=("https://e.example/x",),
        ),
        case(
            "find . -name '*.pyc' -delete",
            id="find delete writes below its root",
            paths=("read w/p/**", "write w/p/**"),
        ),
        case(
            "ls src | xargs rm",
            id="pipeline-fed write is bounded by the listing",
            paths=("read w/p/src", "write w/p/src/*/**"),
        ),
        case(
            "echo x | xargs rm",
            id="pipeline-fed write without listing is unresolved",
            unresolved=("path_write",),
        ),
        case(
            "rm src/*.py; rm src/*/../x",
            id="globs at their literal folder",
            paths=("write w/p/src/*.py",),
            unresolved=("path_write",),
        ),
        case(
            "git diff ../o --stat; git log origin/main; python s.py --out=../x",
            id="tier B takes only clear paths",
            paths=("read w/o/** (git)", "read w/p/s.py", "read w/x"),
        ),
        case(
            "tar -xzf a.tgz -C d; dd if=i of=o; sed -i s/a/b/ f; cp a b d2/",
            id="per-program specs",
            paths=(
                "read w/p/a.tgz",
                "write w/p/d/**",
                "read w/p/i",
                "write w/p/o",
                "write w/p/f",
                "read w/p/a",
                "read w/p/b",
                "write w/p/d2",
            ),
        ),
        case(
            "git -C sub commit -m x; git --no-pager -c a=b log; git --git-dir=.g push",
            id="global options go before patterns match and -C scopes the folder",
            commands=("git commit -m x", "git log", "git push"),
            paths=("start_dir w/p/sub", "write w/p/sub"),
        ),
        case(
            "git checkout -- .claude/settings.json; git restore .; git rm secrets/key.pem; "
            "git mv a/k.pem k; git rm '*.pem'; git clean -fdx; git clean -f tmp; git apply p.diff",
            id="git pathspecs change everything below what they name, tracked files as git",
            commands=None,
            paths=(
                "write w/p/.claude/settings.json/** (git)",
                "write w/p/** (git)",
                "write w/p/secrets/key.pem/**",
                "write w/p/a/k.pem/**",
                "write w/p/k/**",
                "write w/p/**",
                "write w/p/tmp/**",
                "read w/p/p.diff",
            ),
        ),
        case(
            "git reset --hard; git stash pop; git -C sub pull; git switch main; git checkout main",
            id="git commands that replace the work tree",
            commands=None,
            paths=(
                "write w/p/** (work tree) (git)",
                "start_dir w/p/sub",
                "write w/p/sub/** (work tree) (git)",
                "write w/p/main/** (git)",
            ),
        ),
        case(
            "git restore --staged k; git rm --cached k; git reset HEAD~1; git stash list; "
            "git commit -a -m x",
            id="git commands that change only the index or .git",
            commands=None,
            paths=("write w/p",),
        ),
        case(
            "git diff -- secrets; git blame src/a.py; git ls-files docs; git add .; "
            "git diff --no-index a b; git show HEAD:k; git cat-file -p :s/k.pem; git grep foo; "
            "git grep -e foo -- lib; git status; git log -p x",
            id="git reads the files its operands and index paths name",
            commands=None,
            paths=(
                "read w/p/secrets/** (git)",
                "read w/p/src/a.py/**",
                "read w/p/docs/** (git)",
                "write w/p",
                "read w/p/** (git)",
                "read w/p/a/**",
                "read w/p/b/**",
                "read w/p/s/k.pem/** (work tree)",
                "read w/p/**",
                "read w/p/lib/**",
            ),
        ),
        case(
            "git submodule foreach make; git rebase -x make main; git restore :/k",
            id="git commands that run commands or use pathspec magic",
            commands=None,
            paths=("write w/p/** (work tree) (git)", "read w/p/k/** (work tree)"),
            unresolved=("command", "path_write"),
        ),
        case(
            "git clone https://e.example/r.git out; npm --prefix web install; "
            "pip install --target lib x; python -m venv .venv; dotnet build -o bin",
            id="programs write where their arguments say",
            paths=(
                "write w/p/out/**",
                "write w/p/web/**",
                "write w/p/lib/**",
                "write w/p/.venv/**",
                "write w/p/bin/**",
            ),
            urls=("https://e.example/r.git",),
        ),
        case("mytool ../a out.txt", id="unknown programs only read", paths=("read w/a",)),
        case(
            "uv run vbot agent list; uvx ruff check",
            id="runners unwrap",
            commands=("uv run vbot agent list", "vbot agent list", "uvx ruff check", "ruff check"),
        ),
        case(
            "curl e.example:8080/x; ssh u@h.example; scp f u@h.example:/t",
            id="targets without scheme are addresses and hosts",
            paths=("read w/p/f",),
            urls=("http://e.example:8080/x",),
            hosts=("h.example",),
        ),
        case(
            "rg --iglob '!Secrets' foo; rg foo src",
            id="recursive searches and their exact exclusions",
            paths=("read w/p/** !secrets", "read w/p/src/**"),
        ),
        case(
            "grep -rn TODO; egrep -d recurse x lib; grep y < f",
            id="recursive grep without a path searches the working directory",
            paths=("read w/p/**", "read w/p/lib/**", "read w/p/f"),
        ),
        case(
            "awk '{print $1}' notes/a.txt; jq -r .key --from-file=q.jq; echo secrets/key",
            id="words of programs without a spec are mentions",
            mentions=(
                "read w/p/notes/a.txt",
                "read w/p/{print $1}",
                "read w/p/.key",
                "read w/p/q.jq",
            ),
        ),
        case(
            "ln -s ../secret link; ln target hard",
            id="a link writes itself and what it leads to",
            paths=(
                "write w/p/link",
                "write w/p/secret/**",
                "write w/secret/**",
                "write w/p/hard",
                "write w/p/target",
            ),
        ),
        case(
            "perl -pi -e 's/a/b/' f.txt; ruby -i -pe x g.txt; perl -e 'print 1' h.txt",
            id="in-place script edits write their files",
            paths=("write w/p/f.txt", "write w/p/g.txt"),
            mentions=("read w/p/h.txt",),
        ),
        case("rm (", id="parse error is unresolved", unresolved=("command",)),
        case(
            "cd /etc && cat passwd",
            id="absolute posix paths",
            paths=("start_dir etc", "read w/p/passwd", "read etc/passwd"),
            marks=_POSIX,
        ),
        case(
            "cat /c/x /etc/y",
            id="git bash maps drive mounts only",
            paths=("read x",),
            unresolved=("path_read",),
            marks=_WINDOWS,
        ),
    ],
)
@pytest.mark.asyncio
async def test_bash_plan(line: str, expected: Expected) -> None:
    check(await _analyze(line, "bash"), expected)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        case(
            "del /q foo.txt & dir",
            id="builtins and switches",
            commands=("del /q foo.txt", "dir"),
            paths=("write w/p/foo.txt", "read w/p"),
        ),
        case(
            "copy a.txt b.txt && type %HOME%\\s.txt",
            id="copy and environment",
            paths=("read w/p/a.txt", "write w/p/b.txt", "read h/s.txt"),
            marks=_WINDOWS,
        ),
        case(
            "set X=1 & del %X%",
            id="set makes a variable dynamic",
            unresolved=("path_write",),
        ),
        case(
            "for %i in (*.txt) do del %i",
            id="for body runs with a dynamic variable",
            commands=("del <%i>",),
            unresolved=("path_write",),
        ),
        case(
            "if exist x (del x) else (echo (no))",
            id="blocks and literal parentheses",
            commands=("del x", "echo (no)"),
            paths=("write w/p/x",),
        ),
        case(
            'cmd /c "rd /s /q build"',
            id="nested cmd unwraps",
            paths=("write w/p/build/**",),
        ),
        case(
            "cd /d D:\\x && del y",
            id="cd to another drive",
            paths=("start_dir D:/x", "write w/p/y", "write D:/x/y"),
            marks=_WINDOWS,
        ),
        case(
            "cd [s]ub & pushd s?b & del y",
            id="cd and pushd match * and ? themselves",
            paths=("start_dir w/p/[s]ub", "write w/p/y", "write w/p/[s]ub/y"),
            unresolved=("cwd", "path_write"),
        ),
        case(
            "D: & del x",
            id="drive switch",
            paths=("start_dir D:/", "write w/p/x", "write D:/x"),
            marks=_WINDOWS,
        ),
        case(
            r'start "t" /d sub notepad ..\x.txt & type ..\y.txt',
            id="start runs its program in its folder",
            commands=(r"start t /d sub notepad ..\x.txt", r"notepad ..\x.txt", r"type ..\y.txt"),
            paths=("start_dir w/p/sub", "read w/p/x.txt", "read w/y.txt"),
            marks=_WINDOWS,
        ),
        case(
            "cd..\\x & del y",
            id="built-in names end before a dot or slash",
            commands=("cd ..\\x", "del y"),
            paths=("start_dir w/x", "write w/p/y", "write w/x/y"),
            marks=_WINDOWS,
        ),
        case(
            "mklink link target",
            id="mklink writes the link and what it leads to",
            paths=("write w/p/link", "write w/p/target/**", "write w/p/link/target/**"),
        ),
        case(
            "mklink /h hard target",
            id="mklink /h writes the file",
            paths=("write w/p/hard", "write w/p/target"),
        ),
        case(
            "ren dir1 dir2 & ren sub\\*.txt *.bak",
            id="ren moves what lies below",
            paths=(
                "write w/p/dir1/**",
                "write w/p/dir2/**",
                "write w/p/sub/*.txt/**",
                "write w/p/sub/*.bak/**",
            ),
            marks=_WINDOWS,
        ),
        case(
            "del /s *.key & dir /s /b secrets & findstr /s token *.txt & attrib /s +r *.pem",
            id="slash s reaches every depth",
            paths=(
                "write w/p/**/*.key/**",
                "read w/p/**/secrets/**",
                "read w/p/**/*.txt/**",
                "write w/p/**/*.pem/**",
            ),
        ),
        case("where /r . *.pem", id="where /r reads the folder", paths=("read w/p/**",)),
        case(
            'forfiles /p src /s /m *.key /c "cmd /c type @file"',
            id="forfiles reads its matches and leaves its command unresolved",
            paths=("read w/p/src/**/*.key/**",),
            unresolved=("command",),
        ),
        case("call type %X%", id="call expands percent words again", unresolved=("path_read",)),
    ],
)
@pytest.mark.asyncio
async def test_cmd_plan(line: str, expected: Expected) -> None:
    check(await _analyze(line, "cmd"), expected)


@pytest.mark.asyncio
async def test_nesting_beyond_the_limit_is_unresolved() -> None:
    line = "rm x"
    for _ in range(MAX_NESTING_DEPTH + 1):
        line = "bash -c " + _quote(line)
    plan = await _analyze(line, "bash")
    assert [element.kind for element in plan.unresolved] == ["command"]
    assert all(access.kind != "write" for access in plan.paths)


@pytest.mark.asyncio
async def test_a_shell_without_its_parser_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(CommandAnalysisUnavailable):
        await _analyze("Get-ChildItem", "powershell")
    # A nested PowerShell becomes an unresolved command instead.
    plan = await _analyze("pwsh -c 'Remove-Item x'", "bash")
    assert [element.kind for element in plan.unresolved] == ["command"]

    from core.permissions.commands import _bash

    _bash._parser.cache_clear()
    monkeypatch.setitem(sys.modules, "tree_sitter_bash", None)
    try:
        with pytest.raises(CommandAnalysisUnavailable):
            await _analyze("ls", "bash")
    finally:
        _bash._parser.cache_clear()


@pytest.mark.parametrize(
    ("program", "kind"),
    [("pwsh.exe", "powershell"), ("/usr/bin/zsh", "bash"), ("CMD.EXE", "cmd"), ("python", None)],
)
def test_shell_kind(program: str, kind: str | None) -> None:
    assert shell_kind(program) == kind


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"
