"""Failure hints: one short next action for well-known failure output of the shell Tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.tools import shell_hints
from core.tools.shell_hints import annotate_failure

_NOT_RECOGNIZED = (
    "{name}: The term '{name}' is not recognized as a name of a cmdlet, function, script "
    "file, or executable program."
)
# pwsh on a German host, with its own quote characters.
_NOT_RECOGNIZED_DE = (
    "{name}: Der Begriff \u201e{name}\u201c wird nicht als Name eines Cmdlets, einer "
    "Funktion, einer Skriptdatei oder eines ausf\u00fchrbaren Programms erkannt."
)
_STORE_PYTHON = (
    "Python was not found; run without arguments to install from the Microsoft Store, or "
    "disable this shortcut from Settings > Apps > Advanced app settings > App execution aliases."
)
_BASH_SYNTAX = (
    "This is bash syntax: in PowerShell write `if (cond) { ... }`, "
    "`foreach ($f in Get-ChildItem *.md) { ... }`, and `Test-Path` for file tests."
)


@pytest.mark.parametrize(
    ("command", "exit_code", "output", "expected"),
    [
        pytest.param("echo hi", 0, "hi", None, id="success_is_never_annotated"),
        pytest.param("true", 7, "some arbitrary failure text", None, id="unknown_failure"),
        ("python setup.py", 127, "bash: python: command not found", ["python3"]),
        ("pip install x", 127, "sh: pip: command not found", ["pip3"]),
        (
            "make build",
            127,
            "bash: line 1: make: command not found",
            ["`make`", "`command -v make`"],
        ),
        (
            "cargo build",
            1,
            "The term 'cargo' is not recognized as a name of a cmdlet, "
            "function, script file, or executable program.",
            ["`cargo`", "Check with `Get-Command cargo`"],
        ),
        (
            "git branch feature",
            1,
            "fatal: a branch named 'feature' already exists",
            ["'feature' already exists"],
        ),
        (
            "python -m http.server 8000",
            1,
            "OSError: [Errno 98] Address already in use",
            ["The requested port is already in use"],
        ),
        (
            "uvicorn app:app",
            1,
            "ERROR: [Errno 98] error while attempting to bind on address ('127.0.0.1', 8421)",
            ["Port 8421 is already in use"],
        ),
        ("git push", 1, "error: API rate limit exceeded for user", ["rate limit"]),
        (
            "gh pr list --json bogus",
            1,
            'gh: Unknown JSON field: "bogus"\nValid fields are: author, body',
            ["no JSON field 'bogus'"],
        ),
        pytest.param(
            "python",
            1,
            "bash: python: command not found\nfatal: a branch named 'x' already exists",
            ["python3"],
            id="first_match_wins",
        ),
        pytest.param(
            "python",
            127,
            "ok\n" * 5000 + "bash: python: command not found",
            None,
            id="scan_window_is_bounded",
        ),
        (
            "Select-String -Pattern todo -Recurse",
            1,
            "Select-String: A parameter cannot be found that matches parameter name 'Recurse'.",
            ["`Get-ChildItem -Recurse -File | Select-String 'text'`"],
        ),
        pytest.param(
            "Select-String todo " * 40_000 + "| Select-String -Pattern todo -Recurse",
            1,
            "Select-String: A parameter cannot be found that matches parameter name 'Recurse'.",
            ["`Get-ChildItem -Recurse -File | Select-String 'text'`"],
            id="long_command_is_scanned_once",
        ),
        # Windows' app execution alias stands in for python3, not for python.
        (
            "python3 app.py",
            9009,
            _STORE_PYTHON,
            ["`python3` is the Microsoft Store placeholder on this machine: run `python` instead."],
        ),
        pytest.param("python app.py", 9009, _STORE_PYTHON, None, id="store_python_named_python"),
    ],
)
def test_failure_output_gets_the_matching_hint(
    command: str, exit_code: int, output: str, expected: list[str] | None
) -> None:
    hint = annotate_failure(command, exit_code, output)

    if expected is None:
        assert hint is None
    else:
        assert hint is not None
        for fragment in expected:
            assert fragment in hint


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git merge feature", "Git stopped the merge at a conflict"),
        ("git pull", "Git stopped the merge at a conflict"),
        ("git pull --rebase origin main", "run `git rebase --continue`; `git rebase --abort`"),
        ("git rebase main", "run `git rebase --continue`; `git rebase --abort` undoes"),
        ("git cherry-pick abc123", "run `git cherry-pick --continue`"),
        ("git revert HEAD", "run `git revert --continue`"),
        ("git stash pop", "the stash stays in the stash list until `git stash drop`"),
        ("git am fix.patch", "run the stopped git command with --continue, or with --abort"),
    ],
)
def test_merge_conflict_names_the_stopped_git_operation(command: str, expected: str) -> None:
    output = (
        "CONFLICT (content): Merge conflict in src/a.py\n"
        "Automatic merge failed; fix conflicts and then commit the result."
    )

    hint = annotate_failure(command, 1, output)

    assert hint is not None
    assert expected in hint
    assert "Resolve them and `git add` them" in hint


@pytest.mark.parametrize("powershell", [False, True])
@pytest.mark.parametrize(
    ("exit_code", "output", "posix_expected"),
    [
        (126, "", "`chmod +x`"),
        (137, "", "SIGKILL"),
        pytest.param(137, "bash: python: command not found", "python3", id="output_beats_code"),
        (1, "bash: ./deploy.sh: Permission denied", "Check its owner and permissions"),
    ],
)
def test_platform_specific_hints_follow_the_host_shell(
    monkeypatch: pytest.MonkeyPatch,
    powershell: bool,
    exit_code: int,
    output: str,
    posix_expected: str,
) -> None:
    monkeypatch.setattr(shell_hints, "_POWERSHELL", powershell)

    hint = annotate_failure("run", exit_code, output)

    if "Permission denied" in output:
        # Neither variant suggests sudo; elevation is the user's decision.
        assert hint is not None and "ask the user to run the command" in hint
        assert "sudo" not in hint
        assert ("holds the file open" in hint) is powershell
        assert (posix_expected in hint) is not powershell
    elif powershell and not output:
        # PowerShell reports its own exit codes, so their POSIX meanings do not apply.
        assert hint is None
    else:
        assert hint is not None and posix_expected in hint


@pytest.mark.parametrize(
    ("command", "output", "expected"),
    [
        (
            "git log | Out-File /dev/null",
            "Out-File: Could not find a part of the path 'C:\\dev\\null'.",
            "PowerShell has no /dev/null file: discard output with `| Out-Null` instead of "
            "passing /dev/null as a path.",
        ),
        (
            "ls -la src",
            "Get-ChildItem: A parameter cannot be found that matches parameter name 'la'.",
            "`ls -la` uses bash flags, but in PowerShell `ls` is Get-ChildItem: use "
            "`Get-ChildItem -Force` to include hidden files and `-Recurse` for subdirectories.",
        ),
        (
            "ls -la src",
            "Get-ChildItem: Es wurde kein Parameter gefunden, der dem Parameternamen "
            "\u201ela\u201c entspricht.",
            "`ls -la` uses bash flags",
        ),
        # The flag that failed binding is not the one written after ls.
        (
            "ls -la src",
            "Get-ChildItem: A parameter cannot be found that matches parameter name 'x'.",
            None,
        ),
        (
            "ls -la src",
            "Get-ChildItem: Cannot find path 'C:\\nope' because it does not exist.",
            None,
        ),
    ],
)
def test_bash_habits_in_powershell_name_the_powershell_form(
    command: str, output: str, expected: str | None
) -> None:
    hint = annotate_failure(command, 1, output)

    if expected is None:
        assert hint is None
    else:
        assert hint is not None and hint.startswith(expected)


@pytest.mark.parametrize(
    ("command", "name", "expected", "template"),
    [
        ("which node", "which", "(Get-Command name).Source", _NOT_RECOGNIZED),
        ("export A=1", "export", "$env:NAME = 'value'", _NOT_RECOGNIZED),
        ("make || true", "true", "drop `|| true`", _NOT_RECOGNIZED),
        ("python3 app.py", "python3", "`python` (or `py`)", _NOT_RECOGNIZED),
        ("DEBUG=1 npm test", "DEBUG=1", "`$env:DEBUG = '1'` on its own line", _NOT_RECOGNIZED),
        ("done", "done", _BASH_SYNTAX, _NOT_RECOGNIZED),
        # Bash commands are not missing installs.
        (
            "test -f x && echo yes",
            "test",
            "`test` and `[ ]` are bash: in PowerShell use `Test-Path path` (add `-PathType Leaf` "
            "or `-PathType Container` for -f or -d) inside `if (...) { ... }`.",
            _NOT_RECOGNIZED,
        ),
        (
            "env | grep PATH",
            "env",
            "`env` is bash: list environment variables with `Get-ChildItem env:` and read one "
            "with `$env:NAME`.",
            _NOT_RECOGNIZED,
        ),
        (
            "chmod +x run.sh",
            "chmod",
            "`chmod` has no PowerShell equivalent and Windows needs none: run the script "
            "directly, such as `./run.ps1` or `python script.py`.",
            _NOT_RECOGNIZED,
        ),
        ("cargo build", "cargo", "Check with `Get-Command cargo`", _NOT_RECOGNIZED_DE),
    ],
)
def test_missing_powershell_command_names_the_equivalent(
    command: str, name: str, expected: str, template: str
) -> None:
    hint = annotate_failure(command, 1, template.format(name=name))

    assert hint is not None
    assert expected in hint


_WINDOWS_FIND = (
    "`find` here is the Windows text-search program, not Unix find: list files with "
    "`Get-ChildItem -Recurse -Filter '*.md'`"
)


@pytest.mark.parametrize(
    ("command", "output", "offered", "expected"),
    [
        (
            "git log | grep fix",
            _NOT_RECOGNIZED_DE.format(name="grep"),
            set(),
            "filter command output with `| Select-String 'text'` (add -CaseSensitive to match "
            "grep, -NotMatch for -v), and search files with "
            "`Get-ChildItem -Recurse -File | Select-String 'text'`.",
        ),
        (
            "grep -r todo .",
            _NOT_RECOGNIZED_DE.format(name="grep"),
            {"search_files"},
            "and search files with search_files.",
        ),
        (
            "Select-String -Pattern todo -Recurse",
            "Select-String: A parameter cannot be found that matches parameter name 'Recurse'.",
            {"search_files"},
            "Select-String 'text'`, or search with search_files.",
        ),
        (
            "sed -i 's/a/b/' f",
            _NOT_RECOGNIZED_DE.format(name="sed"),
            set(),
            "`sed` does not exist in PowerShell: replace text in a file with "
            "`(Get-Content f) -replace 'old', 'new' | Set-Content f`, and print a line range "
            "with `Get-Content f | Select-Object -Skip 9 -First 10`.",
        ),
        (
            "sed -n '10,20p' f",
            _NOT_RECOGNIZED_DE.format(name="sed"),
            {"apply_patch", "edit", "read"},
            "`sed` does not exist in PowerShell: change files with apply_patch, replace text in "
            "command output with `-replace 'old', 'new'`, and read a line range with read.",
        ),
        (
            "sed -i 's/a/b/' f",
            _NOT_RECOGNIZED_DE.format(name="sed"),
            {"edit"},
            "change files with edit,",
        ),
        # find.exe's messages follow the Windows display language.
        ("find . -name '*.md'", "FIND: Parameter format not correct", set(), f"{_WINDOWS_FIND}."),
        (
            "find src -type f | Select-Object -First 5",
            "FIND: Parameterformat falsch",
            {"search_files"},
            f"{_WINDOWS_FIND}, or use search_files.",
        ),
    ],
)
def test_hints_name_only_the_tools_the_agent_is_offered(
    command: str, output: str, offered: set[str], expected: str
) -> None:
    hint = annotate_failure(command, 1, output, offers=offered.__contains__)

    assert hint is not None
    assert expected in hint
    for tool in {"search_files", "read", "apply_patch", "edit"} - offered:
        assert f" {tool}." not in hint and f" {tool}," not in hint


@pytest.mark.parametrize(
    ("command", "output", "expected"),
    [
        (
            "python - <<'EOF'\nprint(1)\nEOF",
            "Missing file specification after redirection operator.",
            "PowerShell has no heredoc (<<).",
        ),
        (
            'python -c \\"print(1)\\"',
            "Missing file specification after redirection operator.",
            'In PowerShell, \\" does not escape a quote.',
        ),
        ("Write-Output (", "Missing file specification after redirection operator.", None),
        # Bash control flow and tests, wherever a statement starts.
        (
            "for f in *.md; do echo $f; done",
            "Missing opening '(' after keyword 'for'.",
            _BASH_SYNTAX,
        ),
        ("[ -f README.md ] && echo yes", "Missing type name after '['.", _BASH_SYNTAX),
        ("if [ -f x ]; then echo y; fi", "Missing '(' after 'if' in if statement.", _BASH_SYNTAX),
        # PowerShell's own do loop and type literals are not bash.
        ("do { $i++ } while ($i -lt 3", "Missing closing ')' after expression.", None),
        ("[int]$n = (", "You must provide a value expression.", None),
        pytest.param(
            "\n" * 40_000 + "[ -f x ] && echo yes",
            "Missing type name after '['.",
            _BASH_SYNTAX,
            id="blank_lines_are_scanned_once",
        ),
        # PowerShell ends the string at \", so Python reads a cut-off line.
        (
            'python -c "import json; print(json.dumps({\\"a\\": 1}))"',
            "SyntaxError: '{' was never closed",
            'In PowerShell, \\" does not escape a quote.',
        ),
        (
            'python -c @"\nimport json\nprint(json.dumps({\\"a\\": 1}))\n"@',
            "SyntaxError: unexpected character after line continuation character",
            'Inside a @"..."@ here-string, \\" stays a backslash and a quote.',
        ),
        (
            "python -c @'\nprint(''hello'')\n'@",
            "SyntaxError: invalid syntax. Is this intended to be part of the string?",
            "Inside a @'...'@ here-string, '' stays two quotes.",
        ),
        pytest.param(
            '@"\n' * 40_000 + "python -c @'\nprint(''hello'')\n'@",
            "SyntaxError: invalid syntax. Is this intended to be part of the string?",
            "Inside a @'...'@ here-string, '' stays two quotes.",
            id="unclosed_here_strings_are_scanned_once",
        ),
        # Quotes a here-string keeps literal and empty strings explain nothing.
        ("@'\nprint(''.join(parts))\nprint(\"a\\\"b\"\n'@ | python -", "SyntaxError: x", None),
    ],
)
def test_bash_syntax_and_quoting_that_break_powershell_are_named(
    monkeypatch: pytest.MonkeyPatch, command: str, output: str, expected: str | None
) -> None:
    if output.startswith("SyntaxError"):
        output = f'  File "<string>", line 1\n    code\n    ^\n{output}'
    else:
        output = f"ParserError:\nLine |\n   1 |  x\n     |  ~\n     | {output}"
    monkeypatch.setattr(shell_hints, "_POWERSHELL", True)

    hint = annotate_failure(command, 1, output)

    if expected is None:
        assert hint is None
    else:
        assert hint is not None and hint.startswith(expected)
    if output.lstrip().startswith("File"):
        # In bash, \" is an escape, so a SyntaxError there is the code's own.
        monkeypatch.setattr(shell_hints, "_POWERSHELL", False)
        assert annotate_failure(command, 1, output) is None


def _project(tmp_path: Path) -> Path:
    """A working directory holding the package `shop` and a `tests` folder."""
    (tmp_path / "shop").mkdir()
    (tmp_path / "tests").mkdir()
    return tmp_path


def _import_traceback(script: Path, module: str) -> str:
    return (
        "Traceback (most recent call last):\n"
        f'  File "{script}", line 13, in <module>\n'
        f"    from {module}.cart import Cart\n"
        f"ModuleNotFoundError: No module named '{module}'\n"
    )


_RUN_AS_MODULE = (
    "Python cannot import '{module}' because running `tests/test_cart.py` by path puts its "
    "directory `tests` on the import path instead of the working directory, where "
    "'{module}' is. Run it as a module from the working directory instead: "
    "`python -m tests.test_cart`."
)


@pytest.mark.parametrize(
    ("command", "module"),
    [
        ("python tests/test_cart.py", "shop"),
        ("python tests\\test_cart.py 2>&1 | Select-Object -Last 5", "tests"),
        ('& "C:/venv/Scripts/python.exe" -u tests/test_cart.py', "shop"),
        ("python {script}", "shop"),
    ],
)
def test_module_not_found_from_script_run_by_path_names_module_run(
    tmp_path: Path, command: str, module: str
) -> None:
    workdir = _project(tmp_path)
    script = workdir / "tests" / "test_cart.py"

    hint = annotate_failure(
        command.format(script=f'"{script}"'), 1, _import_traceback(script, module), workdir=workdir
    )

    assert hint == _RUN_AS_MODULE.format(module=module)


def test_module_not_found_picks_the_script_the_traceback_names(tmp_path: Path) -> None:
    workdir = _project(tmp_path)
    command = (
        "python tests/test_orders.py 2>&1 | Select-Object -Last 20; "
        "python tests/test_cart.py 2>&1 | Select-Object -Last 3; "
        "python tests/test_stock.py 2>&1 | Select-Object -Last 3"
    )
    # The first script passed; the tails of the other two tracebacks survived.
    output = "OK: 64 checks passed\n\n" + "\n".join(
        _import_traceback(workdir / "tests" / name, "shop").removeprefix(
            "Traceback (most recent call last):\n"
        )
        for name in ("test_cart.py", "test_stock.py")
    )

    hint = annotate_failure(command, 1, output, workdir=workdir)

    assert hint == _RUN_AS_MODULE.format(module="shop")


@pytest.mark.parametrize(
    ("command", "output", "workdir_known", "expected"),
    [
        (
            "pytest tests/test_cart.py",
            "E   ModuleNotFoundError: No module named 'shop'",
            True,
            "'shop' is in the working directory, but the working directory is not on Python's "
            "import path for this command. Run Python from the working directory with -m, "
            "such as `python -m pytest`, or add the working directory to PYTHONPATH.",
        ),
        *(
            (
                "python -m flask run",
                "Traceback (most recent call last):\nModuleNotFoundError: No module named 'flask'",
                known,
                "Python cannot import 'flask': this interpreter finds no package or module of "
                "that name. Check the spelling; if the project has a virtual environment, run "
                "its interpreter, otherwise install the package.",
            )
            for known in (True, False)
        ),
        *(
            (
                'python -c "import shop.api"',
                output,
                True,
                "Python imported the package 'shop', but it has no module 'api'. List the files "
                "of 'shop' to check the name, or create the module if it is still missing.",
            )
            for output in (
                "ModuleNotFoundError: No module named 'shop.api'\n",
                # A program that reports the failed import itself.
                "the engine is not usable yet (ModuleNotFoundError: No module named 'shop.api')\n",
            )
        ),
        (
            "python app.py",
            "ModuleNotFoundError: No module named 'shop.api'; 'shop' is not a package",
            False,
            "Python cannot import 'shop.api': 'shop' is a single module, not a package, so it "
            "has no submodules. Import 'api' from the module that defines it.",
        ),
    ],
)
def test_import_failures_name_where_python_looked(
    tmp_path: Path, command: str, output: str, workdir_known: bool, expected: str
) -> None:
    workdir = _project(tmp_path) if workdir_known else None

    assert annotate_failure(command, 1, output, workdir=workdir) == expected
