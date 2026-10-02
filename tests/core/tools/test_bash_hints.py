"""Failure hints: one short next action for well-known failure output of the shell Tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.tools import bash_hints
from core.tools.bash_hints import annotate_failure

_NOT_RECOGNIZED = (
    "{name}: The term '{name}' is not recognized as a name of a cmdlet, function, script "
    "file, or executable program."
)
# pwsh on a German host, with its own quote characters.
_NOT_RECOGNIZED_DE = (
    "{name}: Der Begriff \u201e{name}\u201c wird nicht als Name eines Cmdlets, einer "
    "Funktion, einer Skriptdatei oder eines ausf\u00fchrbaren Programms erkannt."
)


@pytest.mark.parametrize(
    ("command", "exit_code", "output", "expected"),
    [
        pytest.param("echo hi", 0, "hi", None, id="success_is_never_annotated"),
        pytest.param("true", 7, "some arbitrary failure text", None, id="unknown_failure"),
        ("python setup.py", 127, "bash: python: command not found", ["python3"]),
        ("pip install x", 127, "sh: pip: command not found", ["pip3"]),
        ("make build", 127, "bash: line 1: make: command not found", ["`make`", "`which make`"]),
        (
            "cargo build",
            1,
            "The term 'cargo' is not recognized as a name of a cmdlet, "
            "function, script file, or executable program.",
            ["`cargo`", "Get-Command cargo"],
        ),
        (
            "git merge feature",
            1,
            "CONFLICT (content): Merge conflict in src/a.py\n"
            "Automatic merge failed; fix conflicts and then commit the result.",
            ["Do not retry", "git add"],
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
            ["port"],
        ),
        (
            "uvicorn app:app",
            1,
            "ERROR: [Errno 98] error while attempting to bind on address ('127.0.0.1', 8421)",
            ["8421"],
        ),
        ("./deploy.sh", 1, "bash: ./deploy.sh: Permission denied", ["Permission denied"]),
        ("git push", 1, "error: API rate limit exceeded for user", ["rate limit"]),
        (
            "gh pr list --json bogus",
            1,
            'gh: Unknown JSON field: "bogus"\nValid fields are: author, body',
            ["bogus"],
        ),
        pytest.param(
            "python",
            1,
            "bash: python: command not found\nfatal: a branch named 'x' already exists",
            ["python3"],
            id="first_match_wins",
        ),
        ("run.sh", 126, "", ["chmod +x"]),
        ("stress", 137, "", ["SIGKILL"]),
        ("pytest", 124, "", ["background"]),
        pytest.param(
            "python",
            124,
            "bash: python: command not found",
            ["python3"],
            id="output_pattern_beats_exit_code",
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
            assert fragment.lower() in hint.lower()


@pytest.mark.parametrize(
    ("command", "output", "expected"),
    [
        (
            "git fetch 2>/dev/null",
            "Out-File: Could not find a part of the path 'C:\\dev\\null'.",
            "PowerShell has no /dev/null: discard output with `2>$null`, `>$null`, or "
            "`| Out-Null`.",
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
            "„la“ entspricht.",
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
        ("git log | grep fix", "grep", "`| Select-String 'text'`", _NOT_RECOGNIZED),
        ("sed -i 's/a/b/' f", "sed", "-replace 'old', 'new'", _NOT_RECOGNIZED_DE),
        ("which node", "which", "(Get-Command name).Source", _NOT_RECOGNIZED),
        ("export A=1", "export", "$env:NAME = 'value'", _NOT_RECOGNIZED),
        ("make || true", "true", "drop `|| true`", _NOT_RECOGNIZED),
        ("python3 app.py", "python3", "`python` (or `py`)", _NOT_RECOGNIZED),
        ("DEBUG=1 npm test", "DEBUG=1", "`$env:DEBUG = '1'` on its own line", _NOT_RECOGNIZED),
        ("if [ -f x ]; then echo y; fi", "if", "`if (Test-Path x) { ... }`", _NOT_RECOGNIZED),
        ("cargo build", "cargo", "Verify with `Get-Command cargo`", _NOT_RECOGNIZED_DE),
    ],
)
def test_missing_powershell_command_names_the_equivalent(command, name, expected, template):
    hint = annotate_failure(command, 1, template.format(name=name))

    assert hint is not None
    assert expected in hint


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
def test_bash_quoting_that_breaks_code_in_powershell_is_named(
    monkeypatch, command, output, expected
):
    if output.startswith("SyntaxError"):
        output = f'  File "<string>", line 1\n    code\n    ^\n{output}'
    else:
        output = f"ParserError:\nLine |\n   1 |  x\n     |  ~\n     | {output}"
    monkeypatch.setattr(bash_hints, "_POWERSHELL", True)

    hint = annotate_failure(command, 1, output)

    if expected is None:
        assert hint is None
    else:
        assert hint is not None and hint.startswith(expected)
    if output.lstrip().startswith("File"):
        # In bash, \" is an escape, so a SyntaxError there is the code's own.
        monkeypatch.setattr(bash_hints, "_POWERSHELL", False)
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
