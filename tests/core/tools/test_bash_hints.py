"""Tests for the bash output-pattern failure hints."""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_success_never_annotated() -> None:
    assert annotate_failure("echo hi", 0, "hi") is None


def test_unknown_failure_returns_none() -> None:
    assert annotate_failure("true", 7, "some arbitrary failure text") is None


def test_bare_python_not_found_gets_specific_hint() -> None:
    hint = annotate_failure("python setup.py", 127, "bash: python: command not found")
    assert hint is not None
    assert "python3" in hint


def test_bare_pip_not_found_gets_specific_hint() -> None:
    hint = annotate_failure("pip install x", 127, "sh: pip: command not found")
    assert hint is not None
    assert "pip3" in hint


def test_other_command_not_found_names_the_binary() -> None:
    hint = annotate_failure("make build", 127, "bash: line 1: make: command not found")
    assert hint is not None
    assert "`make`" in hint
    assert "`which make`" in hint


def test_powershell_command_not_found_hint() -> None:
    hint = annotate_failure(
        "cargo build",
        1,
        "The term 'cargo' is not recognized as a name of a cmdlet, "
        "function, script file, or executable program.",
    )
    assert hint is not None
    assert "`cargo`" in hint
    assert "Get-Command cargo" in hint


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
    "command",
    [
        "python tests/test_cart.py",
        "python tests\\test_cart.py 2>&1 | Select-Object -Last 5",
        '& "C:/venv/Scripts/python.exe" -u tests/test_cart.py',
    ],
)
@pytest.mark.parametrize("module", ["shop", "tests"])
def test_module_not_found_from_script_run_by_path_names_module_run(
    tmp_path: Path, command: str, module: str
) -> None:
    workdir = _project(tmp_path)
    output = _import_traceback(workdir / "tests" / "test_cart.py", module)

    hint = annotate_failure(command, 1, output, workdir=workdir)

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


def test_module_not_found_from_absolute_script_path(tmp_path: Path) -> None:
    workdir = _project(tmp_path)
    script = workdir / "tests" / "test_cart.py"

    hint = annotate_failure(
        f'python "{script}"', 1, _import_traceback(script, "shop"), workdir=workdir
    )

    assert hint == _RUN_AS_MODULE.format(module="shop")


def test_module_in_workdir_without_script_names_import_path(tmp_path: Path) -> None:
    workdir = _project(tmp_path)

    hint = annotate_failure(
        "pytest tests/test_cart.py",
        1,
        "E   ModuleNotFoundError: No module named 'shop'",
        workdir=workdir,
    )

    assert hint == (
        "'shop' is in the working directory, but the working directory is not on Python's "
        "import path for this command. Run Python from the working directory with -m, "
        "such as `python -m pytest`, or add the working directory to PYTHONPATH."
    )


@pytest.mark.parametrize("workdir_known", [True, False])
def test_module_missing_everywhere_names_install(tmp_path: Path, workdir_known: bool) -> None:
    hint = annotate_failure(
        "python -m flask run",
        1,
        "Traceback (most recent call last):\nModuleNotFoundError: No module named 'flask'",
        workdir=_project(tmp_path) if workdir_known else None,
    )

    assert hint == (
        "Python cannot import 'flask': this interpreter finds no package or module of that "
        "name. Check the spelling; if the project has a virtual environment, run its "
        "interpreter, otherwise install the package."
    )


@pytest.mark.parametrize(
    "output",
    [
        "ModuleNotFoundError: No module named 'shop.api'\n",
        # A program that reports the failed import itself.
        "the engine is not usable yet (ModuleNotFoundError: No module named 'shop.api')\n",
    ],
)
def test_missing_submodule_names_the_package_not_the_interpreter(
    tmp_path: Path, output: str
) -> None:
    hint = annotate_failure('python -c "import shop.api"', 1, output, workdir=_project(tmp_path))

    assert hint == (
        "Python imported the package 'shop', but it has no module 'api'. List the files "
        "of 'shop' to check the name, or create the module if it is still missing."
    )


def test_submodule_of_a_plain_module_names_the_module() -> None:
    hint = annotate_failure(
        "python app.py",
        1,
        "ModuleNotFoundError: No module named 'shop.api'; 'shop' is not a package",
    )

    assert hint == (
        "Python cannot import 'shop.api': 'shop' is a single module, not a package, so it "
        "has no submodules. Import 'api' from the module that defines it."
    )


def test_merge_conflict_hint_says_do_not_retry() -> None:
    hint = annotate_failure(
        "git merge feature",
        1,
        "CONFLICT (content): Merge conflict in src/a.py\n"
        "Automatic merge failed; fix conflicts and then commit the result.",
    )
    assert hint is not None
    assert "Do not retry" in hint
    assert "git add" in hint


def test_already_exists_hint() -> None:
    hint = annotate_failure(
        "git branch feature", 1, "fatal: a branch named 'feature' already exists"
    )
    assert hint is not None
    assert "'feature' already exists" in hint


def test_port_in_use_hint_with_port() -> None:
    hint = annotate_failure(
        "python -m http.server 8000", 1, "OSError: [Errno 98] Address already in use"
    )
    assert hint is not None
    assert "port" in hint


def test_port_in_use_hint_named_port() -> None:
    hint = annotate_failure(
        "uvicorn app:app",
        1,
        "ERROR: [Errno 98] error while attempting to bind on address ('127.0.0.1', 8421)",
    )
    assert hint is not None
    assert "8421" in hint


def test_permission_denied_hint() -> None:
    hint = annotate_failure("./deploy.sh", 1, "bash: ./deploy.sh: Permission denied")
    assert hint is not None
    assert "Permission denied" in hint


def test_rate_limit_hint() -> None:
    hint = annotate_failure("git push", 1, "error: API rate limit exceeded for user")
    assert hint is not None
    assert "rate limit" in hint.lower()


def test_gh_unknown_json_field_hint() -> None:
    hint = annotate_failure(
        "gh pr list --json bogus",
        1,
        'gh: Unknown JSON field: "bogus"\nValid fields are: author, body',
    )
    assert hint is not None
    assert "bogus" in hint


def test_first_match_wins() -> None:
    output = "bash: python: command not found\nfatal: a branch named 'x' already exists"
    hint = annotate_failure("python", 1, output)
    assert hint is not None
    assert "python3" in hint


def test_exit_126_hint() -> None:
    hint = annotate_failure("run.sh", 126, "")
    assert hint is not None
    assert "chmod +x" in hint


def test_exit_137_hint() -> None:
    hint = annotate_failure("stress", 137, "")
    assert hint is not None
    assert "SIGKILL" in hint


def test_exit_124_hint_mentions_background_mode() -> None:
    hint = annotate_failure("pytest", 124, "")
    assert hint is not None
    assert "background" in hint


def test_exit_code_hint_yields_to_output_pattern() -> None:
    hint = annotate_failure("python", 124, "bash: python: command not found")
    assert hint is not None
    assert "python3" in hint


def test_scan_window_is_bounded() -> None:
    output = "ok\n" * 5000 + "bash: python: command not found"
    hint = annotate_failure("python", 127, output)
    assert hint is None


@pytest.mark.parametrize("template", [_NOT_RECOGNIZED, _NOT_RECOGNIZED_DE])
@pytest.mark.parametrize(
    ("command", "name", "expected"),
    [
        ("git log | grep fix", "grep", "`| Select-String 'text'`"),
        ("sed -i 's/a/b/' f", "sed", "-replace 'old', 'new'"),
        ("which node", "which", "(Get-Command name).Source"),
        ("export A=1", "export", "$env:NAME = 'value'"),
        ("make || true", "true", "drop `|| true`"),
        ("python3 app.py", "python3", "`python` (or `py`)"),
        ("DEBUG=1 npm test", "DEBUG=1", "`$env:DEBUG = '1'` on its own line"),
        ("if [ -f x ]; then echo y; fi", "if", "`if (Test-Path x) { ... }`"),
        ("cargo build", "cargo", "Verify with `Get-Command cargo`"),
    ],
)
def test_missing_powershell_command_names_the_equivalent(template, command, name, expected):
    hint = annotate_failure(command, 1, template.format(name=name))
    assert hint is not None
    assert expected in hint


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "Get-ChildItem: A parameter cannot be found that matches parameter name 'la'.",
            "`ls -la` uses bash flags, but in PowerShell `ls` is Get-ChildItem: use "
            "`Get-ChildItem -Force` to include hidden files and `-Recurse` for subdirectories.",
        ),
        (
            "Get-ChildItem: Es wurde kein Parameter gefunden, der dem Parameternamen "
            "\u201ela\u201c entspricht.",
            "`ls -la` uses bash flags",
        ),
        # The flag that failed binding is not the one written after ls.
        ("Get-ChildItem: A parameter cannot be found that matches parameter name 'x'.", None),
        ("Get-ChildItem: Cannot find path 'C:\\nope' because it does not exist.", None),
    ],
)
def test_bash_flags_on_a_powershell_alias(output, expected):
    hint = annotate_failure("ls -la src", 1, output)
    if expected is None:
        assert hint is None
    else:
        assert hint is not None and hint.startswith(expected)


def test_select_string_recurse_suggests_piping_files():
    hint = annotate_failure(
        "Select-String -Pattern todo -Recurse",
        1,
        "Select-String: A parameter cannot be found that matches parameter name 'Recurse'.",
    )
    assert hint is not None
    assert "`Get-ChildItem -Recurse -File | Select-String 'text'`" in hint


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python - <<'EOF'\nprint(1)\nEOF", "PowerShell has no heredoc (<<)."),
        ('python -c \\"print(1)\\"', 'In PowerShell, \\" does not escape a quote.'),
        ("Write-Output (", None),
    ],
)
def test_parser_errors_from_bash_syntax(command, expected):
    output = (
        "ParserError:\nLine |\n   1 |  x\n     |  ~\n     | Missing file specification "
        "after redirection operator."
    )
    hint = annotate_failure(command, 1, output)
    if expected is None:
        assert hint is None
    else:
        assert hint is not None and hint.startswith(expected)


def test_dev_null_on_windows_names_null_redirection():
    hint = annotate_failure(
        "git fetch 2>/dev/null",
        1,
        "Out-File: Could not find a part of the path 'C:\\dev\\null'.",
    )
    assert hint == (
        "PowerShell has no /dev/null: discard output with `2>$null`, `>$null`, or `| Out-Null`."
    )
