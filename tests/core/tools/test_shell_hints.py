"""Failure hints: one short next action for well-known failure output of the shell Tool."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.tools import _shell_hint_common as shell_hint_common
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
_FAILED = ("python.exe exited with code 1",)


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
            "ok\n" * 2000 + "bash: python: command not found\n" + "ok\n" * 2000,
            None,
            id="scan_window_skips_the_middle",
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
        pytest.param(
            'Select-String -Path *.py -Pattern "todo|fixme" -Recurse',
            1,
            "Select-String: A parameter cannot be found that matches parameter name 'Recurse'.",
            ["`Get-ChildItem -Recurse -File | Select-String 'text'`"],
            id="pipe_inside_the_quoted_pattern",
        ),
        # Windows' app execution alias stands in for python3, not for python.
        (
            "python3 app.py",
            9009,
            _STORE_PYTHON,
            ["`python3` is the Microsoft Store placeholder on this machine: run `python` instead."],
        ),
        pytest.param("python app.py", 9009, _STORE_PYTHON, None, id="store_python_named_python"),
        pytest.param(
            "pip install -r requirements.txt",
            1,
            "Collecting package "
            + "x" * 90
            + "\n"
            + ("Downloading " + "y" * 100 + "\n") * 120
            + "ModuleNotFoundError: No module named 'setuptools'",
            ["Python cannot import 'setuptools'"],
            id="error_after_long_output",
        ),
        pytest.param(
            "python -m http.server 8000",
            1,
            "OSError: [WinError 10048] Only one usage of each socket address "
            "(protocol/network address/port) is normally permitted",
            ["The requested port is already in use"],
            id="windows_port_in_use",
        ),
        pytest.param(
            "timeout 30 make test",
            124,
            "",
            ["the `timeout` program in the command stopped it at its time limit"],
            id="timeout_program",
        ),
        pytest.param("make test", 124, "", None, id="exit_124_without_timeout_program"),
        # A command gets no input and has no terminal.
        pytest.param(
            "Read-Host 'Name'",
            1,
            "Read-Host: PowerShell is in NonInteractive mode. Read and Prompt functionality is "
            "not available.",
            ["PowerShell cannot prompt in a command: pass the value as a parameter"],
            id="powershell_prompt",
        ),
        pytest.param(
            "Get-Credential",
            1,
            "Get-Credential: PowerShell befindet sich im Nichtinteraktiv-Modus. Lese- und "
            "Promptfunktionen sind nicht verfügbar.",
            ["add -Confirm:$false or -Force to a cmdlet that asks for confirmation."],
            id="powershell_prompt_german",
        ),
        pytest.param(
            "docker run -it ubuntu bash",
            1,
            "the input device is not a TTY",
            ["run the container without -t, and without -i unless you pipe input into it."],
            id="container_tty",
        ),
        pytest.param(
            "python ask.py",
            1,
            'Traceback (most recent call last):\n  File "ask.py", line 1\nEOFError: EOF when '
            "reading a line\nbash: python: command not found",
            ["The program asked for input, and commands get none."],
            id="end_of_input_before_not_installed",
        ),
        pytest.param(
            "npm init vite",
            1,
            "Error: User force closed the prompt with 0 null",
            ["such as -y or --yes, or pipe it into the program within the command."],
            id="prompt_library_closed",
        ),
        # A Python failure is its last traceback's exception, not data printed before it.
        pytest.param(
            "python probe.py",
            1,
            "dir path -> [Errno 13] Permission denied: 'C:\\w\\d'\n"
            "Traceback (most recent call last):\n"
            '  File "probe.py", line 34, in <module>\n'
            "    print('Permission denied')\n"
            "TypeError: expected str, bytes or os.PathLike object, not State",
            None,
            id="data_before_the_traceback_is_not_the_failure",
        ),
        pytest.param(
            "python write.py",
            1,
            "Traceback (most recent call last):\n"
            '  File "write.py", line 3, in <module>\n'
            "PermissionError: [Errno 13] Permission denied: 'out.db'",
            [],
            id="python_failure_is_its_exception",
        ),
        pytest.param(
            "python prepare.py; git branch feature",
            1,
            "Traceback (most recent call last):\n"
            '  File "prepare.py", line 1, in <module>\n'
            "ValueError: no input\n"
            "fatal: a branch named 'feature' already exists",
            ["'feature' already exists"],
            id="output_after_the_traceback_counts",
        ),
        pytest.param(
            "Run the unit tests",
            127,
            "bash: line 1: Run: command not found",
            ["command holds a description"],
            id="description_sent_as_command",
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
        (1, "Remove-Item: Access to the path 'C:\\data\\app.log' is denied.", ""),
    ],
)
def test_platform_specific_hints_follow_the_host_shell(
    monkeypatch: pytest.MonkeyPatch,
    powershell: bool,
    exit_code: int,
    output: str,
    posix_expected: str,
) -> None:
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", powershell)

    hint = annotate_failure("run", exit_code, output)

    if "Access to the path" in output:
        # PowerShell's wording; bash never prints it.
        assert (hint is not None) is powershell
    elif "Permission denied" in output:
        assert hint is not None
        assert "sudo" not in hint
    else:
        # PowerShell passes these codes on from bash, wsl, docker or ssh.
        assert hint is not None and posix_expected in hint


@pytest.mark.parametrize(
    ("command", "output", "expected"),
    [
        # PowerShell and programs read /dev/null as a path on the current drive.
        (
            "git log 2>/dev/null",
            "Out-File: Could not find a part of the path 'C:\\dev\\null'.",
            "Windows has no /dev/null",
        ),
        (
            "git diff --no-index \\dev\\null new.txt",
            "error: Could not access '\\dev\\null'",
            "Windows has no /dev/null",
        ),
        (
            "python export.py --out /dev/null",
            "FileNotFoundError: [Errno 2] No such file or directory: '/dev/null'",
            "Windows has no /dev/null",
        ),
        # Output that names it, from a command that does not.
        ("python export.py", "error: cannot write /dev/null", None),
        # A diff header names /dev/null for an added file; diff exits 1 for a difference.
        (
            "git diff --no-index /dev/null new.txt",
            "diff --git a/new.txt b/new.txt\nnew file mode 100644\nindex 0000000..3e75765\n"
            "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+new",
            None,
        ),
        (
            "diff -u /dev/null f",
            "--- /dev/null\t1970-01-01 00:00:00.000000000 +0000\n"
            "+++ f\t2026-10-13 09:00:00.000000000 +0200\n@@ -0,0 +1 @@\n+new",
            None,
        ),
        (
            "ls -la src",
            "Get-ChildItem: A parameter cannot be found that matches parameter name 'la'.",
            "`ls -la` uses bash flags, but in PowerShell `ls` is Get-ChildItem: use "
            "`Get-ChildItem -Force` to include hidden files and `-Recurse` for subdirectories.",
        ),
        # A failure past the command's first line: the terminal drops the
        # space after the cmdlet in PowerShell's multi-line error header.
        (
            "echo start\nrm -rf build",
            "start\nRemove-Item:\nLine |\n   2 |  rm -rf build\n     |     ~~~\n"
            "     | A parameter cannot be found that matches parameter name 'rf'.",
            "`rm -rf` uses bash flags, but in PowerShell `rm` is Remove-Item",
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
        # A program reports the wildcard it got unexpanded.
        (
            "python -m py_compile minidb/*.py minidb/storage/*.py",
            "[Errno 22] Invalid argument: 'minidb/*.py'",
            "PowerShell passes `minidb/*.py` to programs as written",
        ),
        # A cmdlet expands it and reports the resolved path; git and a quoted
        # pattern match it themselves.
        (
            "Get-Content logs/*.log -Tail 5",
            "Get-Content: Cannot find path 'C:\\work\\logs\\*.log' because it does not exist.",
            None,
        ),
        ("git add src/*.py", "fatal: pathspec 'src/*.py' did not match any files", None),
        ("rg -g '*.py' todo", "rg: *.py: No such file or directory (os error 2)", None),
        # A regular expression in a string is the program's own pattern.
        (
            'python find.py "Melting point.*" data.txt',
            'No line matched "Melting point.*" in data.txt',
            None,
        ),
        # \" ended the string early and a { } block of the code became an argument.
        (
            'python -c "\nfor (sql,) in cases:\n    print(f\'{run(f\\"SELECT {sql}\\")}\')\n"',
            "python.exe: ScriptBlock should only be specified as a value of the Command parameter.",
            'In PowerShell, \\" does not escape a quote.',
        ),
        (
            "node -e { console.log(1) }",
            "node.exe: ScriptBlock should only be specified as a value of the Command parameter.",
            None,
        ),
        # Python code sent as the command, whatever PowerShell reports about it.
        (
            "import sqlite3\ncon = sqlite3.connect(':memory:')\nprint([(1, 'x')])",
            "ParserError:\nLine |\n   3 |  print([(1, 'x')])\n     |         ~\n"
            "     | Missing type name after '['.",
            "The command is Python code, which PowerShell cannot run.",
        ),
        (
            "# probe\n\nfrom pathlib import Path\nprint(Path.cwd())",
            "ParserError:\nLine |\n   3 |  from pathlib import Path\n     |  ~~~~\n"
            "     | The 'from' keyword is not supported in this version of the language.",
            "The command is Python code, which PowerShell cannot run.",
        ),
        (
            "def f(x):\n    return x\nprint(f(1))",
            _NOT_RECOGNIZED.format(name="x"),
            "The command is Python code, which PowerShell cannot run.",
        ),
        # PowerShell's own class, also with a base class, and Import-Module.
        ("class Child : Base {\n}", "ParserError: Unable to find type [Base].", None),
        # Text PowerShell ran as a command is no program to install.
        ('& "my tool" --check', _NOT_RECOGNIZED.format(name="my tool"), None),
        ("& $(Get-Tool)x", _NOT_RECOGNIZED.format(name="$(Get-Tool)x"), None),
        (
            "Import-Module Pester",
            "Import-Module: The specified module 'Pester' was not loaded.",
            None,
        ),
    ],
)
def test_bash_habits_in_powershell_name_the_powershell_form(
    monkeypatch: pytest.MonkeyPatch, command: str, output: str, expected: str | None
) -> None:
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", True)

    hint = annotate_failure(command, 1, output)

    if expected is None:
        assert hint is None
    else:
        assert hint is not None and hint.startswith(expected)
    if "*" in command:
        # Bash expands wildcards before a program starts.
        monkeypatch.setattr(shell_hint_common, "_POWERSHELL", False)
        assert annotate_failure(command, 1, output) is None


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
        # head and tail point to the untrimmed output, not to a trim stage.
        ("git log | head -n 5", "head", "Run the command without it", _NOT_RECOGNIZED),
        ("tail -n 20 app.log", "tail", "`Get-Content path -Tail 20`", _NOT_RECOGNIZED),
        # A missing later stage skipped the stages before it.
        (
            "node -e 1 && node t.js 2>&1 | Sort-Object | tail -5; npm test | tail -1",
            "tail",
            "so `node t.js 2>&1 | Sort-Object` and `npm test` did not run.",
            _NOT_RECOGNIZED,
        ),
        ("wc -l notes.txt", "wc", "`(Get-Content path).Count`", _NOT_RECOGNIZED),
        # A description sent as the command; a missing program keeps its own hint.
        (
            "Check shell files compile and behave",
            "Check",
            "command holds a description, not a command",
            _NOT_RECOGNIZED,
        ),
        (
            "Quell- und Testdateien auflisten",
            "Quell-",
            "command holds a description, not a command",
            _NOT_RECOGNIZED,
        ),
        ("Cargo build --release", "Cargo", "Check with `Get-Command Cargo`", _NOT_RECOGNIZED),
        ("check shell files compile", "check", "Check with `Get-Command check`", _NOT_RECOGNIZED),
    ],
)
def test_missing_powershell_command_names_the_equivalent(
    command: str, name: str, expected: str, template: str
) -> None:
    hint = annotate_failure(command, 1, template.format(name=name))

    assert hint is not None
    assert expected in hint
    assert "Select-Object" not in hint


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
        (
            "head -n 5 notes.txt",
            _NOT_RECOGNIZED.format(name="head"),
            {"read"},
            "with read and limit.",
        ),
        (
            "tail -n 20 app.log",
            _NOT_RECOGNIZED.format(name="tail"),
            {"read"},
            "with read and a negative offset, such as -20.",
        ),
        # find.exe's messages follow the Windows display language.
        ("find . -name '*.md'", "FIND: Parameter format not correct", set(), f"{_WINDOWS_FIND}."),
        (
            "find src -type f | Select-Object -First 5",
            "FIND: Parameterformat falsch",
            {"search_files"},
            f"{_WINDOWS_FIND}, or use search_files.",
        ),
        (
            "python ask.py",
            "EOFError: EOF when reading a line",
            {"terminal"},
            "pipe it into the program within the command. To answer it yourself, run the "
            "program with terminal.",
        ),
        ("python ask.py", "EOFError: EOF when reading a line", set(), "within the command."),
        (
            "docker run -it ubuntu bash",
            "the input device is not a TTY",
            {"terminal"},
            "To work inside the container interactively, run it with terminal.",
        ),
    ],
)
def test_hints_name_only_the_tools_the_agent_is_offered(
    command: str, output: str, offered: set[str], expected: str
) -> None:
    hint = annotate_failure(command, 1, output, offers=offered.__contains__)

    assert hint is not None
    assert expected in hint
    for tool in {"search_files", "read", "apply_patch", "edit", "terminal"} - offered:
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
        (
            'python -m minidb "$work\\data.db" < "$work\\s1.sql"',
            "The '<' operator is reserved for future use.",
            "PowerShell has no < input redirection.",
        ),
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
        # Bash's quote inside single quotes, in PowerShell's own code and in a here-string.
        (
            "Get-Content x | Where-Object { $_ -match 'it'\\''s' }",
            "Unexpected token '\\''' in expression or statement.",
            "PowerShell has no '\\'' or '\"'\"' escape.",
        ),
        (
            "python -c @'\nprint('\"'\"'hi'\"'\"')\n'@",
            "SyntaxError: invalid syntax",
            "Inside a @'...'@ here-string, '\\'' and '\"'\"' stay as written.",
        ),
        ("python -c @'\nprint(\"it's\")\n'@", "SyntaxError: invalid syntax", None),
        # A here-string written on one line, or with its '@ indented.
        (
            "python -c @'import re; print(re.sub('a', 'b', 'abc'))'@",
            "No characters are allowed after a here-string header but before the end of the line.",
            "A here-string's @' must end its line",
        ),
        (
            "@'\nprint(1)\n  '@ | python -",
            "White space is not allowed before the string terminator.",
            "A here-string's @' must end its line",
        ),
    ],
)
def test_bash_syntax_and_quoting_that_break_powershell_are_named(
    monkeypatch: pytest.MonkeyPatch, command: str, output: str, expected: str | None
) -> None:
    if output.startswith("SyntaxError"):
        output = f'  File "<string>", line 1\n    code\n    ^\n{output}'
    else:
        output = f"ParserError:\nLine |\n   1 |  x\n     |  ~\n     | {output}"
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", True)

    hint = annotate_failure(command, 1, output)

    if expected is None:
        assert hint is None
    else:
        assert hint is not None and hint.startswith(expected)
    if output.lstrip().startswith("File"):
        # In bash, \" is an escape, so a SyntaxError there is the code's own.
        monkeypatch.setattr(shell_hint_common, "_POWERSHELL", False)
        assert annotate_failure(command, 1, output) is None


_ESCAPED_QUOTE = 'In PowerShell, \\" does not escape a quote'


@pytest.mark.parametrize(
    ("command", "exit_code", "output", "failed", "named"),
    [
        # A statement written as \"...\" ran as a command named after its text.
        (
            'if ($ok) { \\"ok: $($x -gt 0)\\" }',
            1,
            _NOT_RECOGNIZED.format(name="\\ok: $($x -gt 0)\\"),
            (),
            "`\\ok: $($x -gt 0)\\`",
        ),
        # A quoted title's (English) ran as a command; it is not a missing install.
        (
            'python -c "add = {\\"publisher\\": \\"Wikipedia (English)\\"}"',
            1,
            _NOT_RECOGNIZED.format(name="English"),
            _FAILED,
            "`English`",
        ),
        # A later statement exited with 0 after the early end broke the code.
        (
            'python -c "print(f\\"{x}\\")"; python check.py',
            0,
            "python.exe: ScriptBlock should only be specified as a value of the Command "
            "parameter.\nchecked",
            (),
            "",
        ),
        (
            'python -c "t.replace(\\"a\\", \\"b\\")"; python check.py',
            0,
            '  File "<string>", line 1\n    t.replace(\\\n'
            "SyntaxError: unterminated string literal (detected at line 1)\nchecked",
            _FAILED,
            "",
        ),
        # The rest of a pattern became an argument Select-String has no parameter for.
        (
            'Select-String -Path *.py -Pattern "a|\\"b\\"|c"',
            1,
            "Select-String: A positional parameter cannot be found that accepts argument 'b\\|c'.",
            (),
            "`b\\|c`",
        ),
        (
            'Select-String -Path *.py -Pattern "a|\\"b\\":"; echo done',
            0,
            "Select-String: Es wurde kein Positionsparameter gefunden, der das Argument "
            "„b\\:“ akzeptiert.\ndone",
            (),
            "`b\\:`",
        ),
        # The cut-off pattern itself fails as a regex, rather than an extra argument.
        (
            'Select-String -Pattern "a|\\"b\\""',
            1,
            "Select-String: Invalid pattern 'a|\\' at offset 3. Illegal \\ at end of pattern.",
            (),
            "",
        ),
        (
            'Select-String -Pattern "a|\\"b\\""; echo done',
            0,
            "Select-String: Die Zeichenfolge ist kein gültiger regulärer Ausdruck: "
            "Invalid pattern 'a|\\' at offset 3. Illegal \\ at end of pattern.\ndone",
            (),
            "",
        ),
        # A failed program reported a piece of the split argument: the word after
        # the quote, or the cut text before it.
        (
            'git grep -n "name = \\"value\\"" -- src',
            128,
            "fatal: unable to resolve revision: value\\",
            ("git.exe exited with code 128",),
            "`value\\`",
        ),
        (
            'rg -n "x|\\"y\\"" src; Write-Output done',
            0,
            "rg: regex parse error:\n    (?:x|\\)\n    ^\nerror: unclosed group\ndone",
            ("rg.exe exited with code 2",),
            '`"x|\\"`',
        ),
        # A literal regex error, one from another stage, a valid escaped backslash,
        # or an expandable argument is not evidence that this quote cut the pattern.
        (
            "Select-String -Pattern 'a|\\'",
            1,
            "Select-String: Invalid pattern 'a|\\' at offset 3. Illegal \\ at end of pattern.",
            (),
            None,
        ),
        (
            'Select-String -Pattern "a|\\"b\\""; Select-String -Pattern "other\\"',
            1,
            "Select-String: Invalid pattern 'other\\' at offset 6. Illegal \\ at end of pattern.",
            (),
            None,
        ),
        (
            'Write-Output "a|\\"b\\""; Select-String -Pattern "a|\\"',
            1,
            "Select-String: Invalid pattern 'a|\\' at offset 3. Illegal \\ at end of pattern.",
            (),
            None,
        ),
        (
            'Select-String -Pattern "a|\\\\"b\\""',
            1,
            "Select-String: Invalid pattern 'a|\\' at offset 3. Illegal \\ at end of pattern.",
            (),
            None,
        ),
        (
            'Select-String -Pattern "$prefix|\\"b\\""',
            1,
            "Select-String: Invalid pattern '$prefix|\\' at offset 9. "
            "Illegal \\ at end of pattern.",
            (),
            None,
        ),
        # A SyntaxError printed as data, a \" that ends a path's string, and a
        # missing command from outside the string are not the early end's.
        (
            'python -c "t.replace(\\"a\\", \\"b\\")"; python report.py',
            0,
            "SyntaxError: counted 3 times",
            (),
            None,
        ),
        (
            'python check.py "C:\\data\\" --fast; python lint.py',
            0,
            "SyntaxError: invalid syntax",
            _FAILED,
            None,
        ),
        (
            'cargo build; python -c "print(\\"x\\")"',
            1,
            _NOT_RECOGNIZED.format(name="cargo"),
            (),
            None,
        ),
        (
            'Get-ChildItem a b c; python -c "print(\\"x\\")"',
            1,
            "Get-ChildItem: A positional parameter cannot be found that accepts argument 'c'.",
            (),
            None,
        ),
        # A failure that names no piece, a piece echoed by a cmdlet while another
        # program failed, and \" inside single quotes, which reaches git whole.
        (
            'git grep -n "name = \\"value\\"" -- src',
            128,
            "fatal: not a git repository (or any of the parent directories): .git",
            ("git.exe exited with code 128",),
            None,
        ),
        (
            'Write-Output "Running \\"tests\\" now"; python check.py',
            1,
            "Running \\\ntests\\ now\nFAILED (failures=1)",
            _FAILED,
            None,
        ),
        (
            "git grep -n 'name = \\\"value\\\"' -- src",
            128,
            "fatal: unable to resolve revision: value\\",
            ("git.exe exited with code 128",),
            None,
        ),
    ],
)
def test_an_escaped_quote_that_ended_a_powershell_string_is_named(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    exit_code: int,
    output: str,
    failed: tuple[str, ...],
    named: str | None,
) -> None:
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", True)

    hint = annotate_failure(command, exit_code, output, failed_programs=failed)

    if named is None:
        assert hint is None or not hint.startswith(_ESCAPED_QUOTE)
    else:
        assert hint is not None and hint.startswith(_ESCAPED_QUOTE) and named in hint
    # In bash, \" is an escape.
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", False)
    hint = annotate_failure(command, exit_code, output, failed_programs=failed)
    assert hint is None or not hint.startswith(_ESCAPED_QUOTE)


@pytest.mark.parametrize(
    ("command", "names"),
    [
        ("python -c \"print('$field')\"", ("$field",)),
        ("py -3.14 -I -c \"print('${field} $other $field')\"", ("${field}", "$other")),
        ('& "C:/tools/python.exe" -c "\nprint(\'$field\')\n"', ("$field",)),
        ("python -c \"print('``$field')\"", ("$field",)),
        ("python -c \"print('`$literal $expanded')\"", ("$expanded",)),
        ("python -c \"print('$one $two $three $four $five')\"", ("$one", "$two", "$three")),
        pytest.param(
            "python -c \"print('$" + "x" * 40_000 + "')\"",
            ("$" + "x" * 36 + "...",),
            id="long-variable-name-is-bounded",
        ),
        ("python -c \"print('`$field')\"", ()),
        ("python -c 'print(\"$field\")'", ()),
        ("python -c \"print('$env:VALUE ${env:OTHER} $script:value')\"", ()),
        ('python -c "$code"', ()),
        ('python -c "  ${code}  "', ()),
        ("python -c @'\nprint(\"$field\")\n'@", ()),
        ("python -c @\"\nprint('$field')\n\"@", ()),
        ("python --% -c \"print('$field')\"", ()),
        ("node -c \"print('$field')\"", ()),
        ("python -m check \"print('$field')\"", ()),
        ("# python -c \"print('$field')\"", ()),
        ("<# outer <# inner #> python -c \"print('$field')\" #>", ()),
        ("python -c \"print('$($field)')\"", ()),
        ("python -c \"print('$field')\"suffix", ()),
        ("python -c \"print('$field')", ()),
    ],
)
def test_python_source_expansion_names_only_unescaped_literal_variables(
    monkeypatch: pytest.MonkeyPatch, command: str, names: tuple[str, ...]
) -> None:
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", True)

    hint = annotate_failure(command, 0, "")

    if names:
        assert hint is not None
        for name in names:
            assert f"`{name}`" in hint
        assert "single-quoted here-string" in hint
        assert "$literal" not in hint and "$four" not in hint and "$five" not in hint
    else:
        assert hint is None
    # Failure-specific hints win, and running commands have no outcome yet.
    assert annotate_failure(command, 1, "") is None
    assert annotate_failure(command, None, "") is None
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", False)
    assert annotate_failure(command, 0, "") is None


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize(
    ("command", "names"),
    [
        ('pwsh -NoProfile -Command "$r = Get-Item .; $r.Name"', ("$r",)),
        ('powershell -c "Get-ChildItem | ForEach-Object { $_.Name }"', ("$_",)),
        ('& pwsh.exe -Command "try { $n += 1 } catch { $PSItem }"', ("$n", "$PSItem")),
        # This shell's own variables, escaped ones, single quotes and other programs.
        ('pwsh -Command "cargo test $filter"', ()),
        ('pwsh -Command "`$r = Get-Item .; `$r.Name"', ()),
        ("pwsh -Command '$r = Get-Item .; $r.Name'", ()),
        ('pwsh -Command "$_"', ()),
        ('pwsh -File run.ps1 "$r = 1"', ()),
        ('node -e "$r = 1"', ()),
    ],
)
def test_nested_powershell_code_names_the_variables_this_shell_expanded(
    monkeypatch: pytest.MonkeyPatch, command: str, names: tuple[str, ...], exit_code: int
) -> None:
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", True)

    hint = annotate_failure(command, exit_code, "")

    if names:
        assert hint is not None
        for name in names:
            assert f"`{name}`" in hint
    else:
        assert hint is None
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", False)
    assert annotate_failure(command, exit_code, "") is None


@pytest.mark.parametrize(
    ("command", "piped"),
    [
        ("for ($z in 12,31) { python harvest.py $z }", False),
        ("for ($z in 12,31) { python harvest.py $z } 2>&1 | Select-Object -First 30", True),
    ],
)
def test_a_for_loop_over_values_names_foreach(
    monkeypatch: pytest.MonkeyPatch, command: str, piped: bool
) -> None:
    output = (
        "ParserError:\nLine |\n   1 |  for ($z in 12,31) { python harvest.py $z }\n"
        "     |          ~~\n     | Unexpected token 'in' in expression or statement."
    )
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", True)

    hint = annotate_failure(command, 1, output)

    assert hint is not None and "foreach ($z in 12,31) {" in hint
    # A foreach statement cannot be piped; $( ) around it can.
    assert ("$(foreach ($z in 12,31) { ... }) 2>&1 | Select-Object -First 30" in hint) is piped


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize(
    ("command", "doubled"),
    [
        # Text copied from a web page: PowerShell ends the string at \u201a, so the
        # program gets two arguments, or the rest is a ParserError.
        (
            "python cite.py --quote 'K\u00f6ln (lateinisch \u201aColonia\u2018) liegt am Rhein'",
            "\u201a\u201a",
        ),
        ("$q = 'it\u2019s'; python cite.py --quote $q", "\u2019\u2019"),
        ('python cite.py --quote "er sagte \u201eja\u201c"', "\u201e\u201e"),
        # Doubled, of the other kind, in a here-string or in a comment: literal text.
        ("python cite.py --quote 'lateinisch \u201a\u201aColonia\u2018\u2018'", None),
        ('python cite.py --quote "it\u2019s \u201aja\u2018"', None),
        ("python cite.py --quote '\u201eja\u201c'", None),
        ("python cite.py --quote @'\nit\u2019s \u201aColonia\u2018\n'@", None),
        ("python cite.py  # it\u2019s", None),
    ],
)
def test_typographic_quotes_that_end_a_powershell_string_are_named(
    monkeypatch: pytest.MonkeyPatch, command: str, doubled: str | None, exit_code: int
) -> None:
    output = "ParserError: Unexpected token in expression or statement." if exit_code else ""
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", True)

    hint = annotate_failure(command, exit_code, output)

    if doubled is None:
        assert hint is None
    else:
        # The hint names the fix: the quote written twice.
        assert hint is not None and doubled in hint
    # Bash takes typographic quotes as text.
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", False)
    assert annotate_failure(command, 0, output) is None


_NODE_FAILED = ("node.exe exited with code 1",)
# What Python on Windows prints when it writes to an output that was closed.
_CLOSED_OUTPUT = (
    "Traceback (most recent call last):\n"
    '  File "build.py", line 9, in <module>\n'
    "OSError: [Errno 22] Invalid argument"
)


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize(
    ("command", "output", "failed", "printed", "names"),
    [
        # The closed output's error proves the stop.
        (
            "python build.py | Select-Object -First 8",
            _CLOSED_OUTPUT,
            _FAILED,
            8,
            ("`Select-Object -First 8`", "python", "Errno 22", "-Wait"),
        ),
        # Otherwise the program's own failure, whose error lines the stage received.
        (
            "python check.py --url x 2>&1 | Select-Object -First 3",
            "usage: check.py [-h]",
            ("python.exe exited with code 2",),
            3,
            ("`Select-Object -First 3`", "python", "code 2"),
        ),
        (
            "& 'C:/venv/Scripts/python.exe' build.py *>&1 | select -f:4; 'done'",
            "step",
            _FAILED,
            5,
            ("`select -f:4`", "python"),
        ),
        (
            "cd app; node tests/check.js 2>&1 | Select-Object -Skip 2 -First 20",
            "checked",
            ("node.exe exited with code 1 (tests/check.js)",),
            20,
            ("`Select-Object -Skip 2 -First 20`", "node", "code 1"),
        ),
        # $LASTEXITCODE read after a stage that passed its lines does not hold the
        # program's code; a stage that did not fill waited for the program.
        (
            'python check.py 2>&1 | Select-Object -First 3; "exit=$LASTEXITCODE"',
            "error: bad url",
            _FAILED,
            4,
            ("`Select-Object -First 3`", "$LASTEXITCODE"),
        ),
        (
            'python check.py 2>&1 | Select-Object -First 3; "exit=$LASTEXITCODE"',
            "error: bad url",
            _FAILED,
            3,
            None,
        ),
        # No failed program: it finished its work; its errors bypassed the stage;
        # a run outside such a pipeline may have failed.
        ("python build.py 2>&1 | Select-Object -First 8", "step", (), 8, None),
        # The output already shows why it failed, in any language or as a mark.
        (
            "python check.py --url x 2>&1 | Select-Object -First 3",
            "Traceback (most recent call last):\nValueError: bad url",
            ("python.exe exited with code 2",),
            3,
            None,
        ),
        ("node t.js 2>&1 | Select-Object -First 2", "FEHLER  page.html", _NODE_FAILED, 2, None),
        ("node t.js 2>&1 | Select-Object -First 2", "   \u2717 page.html", _NODE_FAILED, 2, None),
        (
            "node t.js 2>&1 | Select-Object -First 2",
            "AssertionError [ERR_ASSERTION]: 1 == 2",
            _NODE_FAILED,
            2,
            None,
        ),
        ("python build.py | Select-Object -First 8", "ValueError: bad", _FAILED, 8, None),
        (
            "python build.py 2>&1 | Select-Object -First 2; python build.py --check",
            "step",
            _FAILED,
            8,
            None,
        ),
        # Another statement's output makes the stage's lines unknown, and shared
        # output cannot establish which of two calls hit the closed pipe.
        (
            "python check.py 2>&1 | Select-Object -First 2; python report.py",
            "checked",
            ("python.exe exited with code 2 (check.py)",),
            8,
            None,
        ),
        (
            "python check.py 2>&1 | Select-Object -First 2; python report.py",
            _CLOSED_OUTPUT,
            ("python.exe exited with code 2 (check.py)",),
            8,
            None,
        ),
        ("Write-Output 'a'; node t.js 2>&1 | Select-Object -First 3", "a", _NODE_FAILED, 3, None),
        # A stage behind another stage, or inside a block, passed fewer lines than
        # the command printed.
        (
            "node t.js 2>&1 | Select-String Fehler | Select-Object -First 12",
            "page.html",
            _NODE_FAILED,
            12,
            None,
        ),
        (
            "foreach ($u in 'a', 'b') { python check.py $u 2>&1 | Select-Object -First 1 }",
            "hits=0",
            _FAILED * 2,
            2,
            None,
        ),
        # The output never reached 8 lines, -Wait lets it finish, a query loses
        # nothing, and -Last reads to the end.
        ("python build.py 2>&1 | Select-Object -First 20", "step", _FAILED, 8, None),
        ("python build.py 2>&1 | Select-Object -First 8 -Wait", "step", _FAILED, 8, None),
        (
            "git log 2>&1 | Select-Object -First 8",
            "commit",
            ("git.exe exited with code 128",),
            8,
            None,
        ),
        ("python build.py 2>&1 | Select-Object -Last 8", "step", _FAILED, 8, None),
        ("Write-Output '| Select-Object -First 1'; python build.py 2>&1", "step", _FAILED, 8, None),
    ],
)
def test_a_program_that_failed_behind_a_select_first_stage_is_explained(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    output: str,
    failed: tuple[str, ...],
    printed: int,
    names: tuple[str, ...] | None,
    exit_code: int,
) -> None:
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", True)

    hint = annotate_failure(
        command, exit_code, output, failed_programs=failed, output_lines=printed
    )

    if names is None:
        assert hint is None
    else:
        assert hint is not None
        for name in names:
            assert name in hint
        assert "1 lines" not in hint
        # Exit code 0 does not show the failure; the hint says so.
        assert ("code 0" in hint) is (exit_code == 0)
    # Select-Object is PowerShell's.
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", False)
    assert annotate_failure(command, 0, output, failed_programs=failed) is None


@pytest.mark.parametrize(
    ("command", "output", "names"),
    [
        # grep was not found, so the checker before it never ran.
        (
            "python check.py 2>&1 | grep FAIL; Write-Output done",
            f"{_NOT_RECOGNIZED.format(name='grep')}\ndone",
            ("Select-String", "`python check.py 2>&1` did not run", "code 0"),
        ),
        # A program called by its quoted path.
        (
            "& 'C:\\tools\\lint.exe' src; 'done'",
            f"{_NOT_RECOGNIZED.format(name='C:\\tools\\lint.exe')}\ndone",
            ("Get-Command", "did not run", "code 0"),
        ),
        # A redirection to /dev/null kept its statement from running.
        (
            "python check.py 2>/dev/null; 'done'",
            "Out-File: Could not find a part of the path 'C:\\dev\\null'.\ndone",
            ("`2>$null`", "/dev/null did not run", "code 0"),
        ),
        # The error text is a program's output: the command calls no such name.
        ("python check.py", _NOT_RECOGNIZED.format(name="grep"), None),
        ("python check.py --skip 'grep'", _NOT_RECOGNIZED.format(name="grep"), None),
    ],
)
def test_a_statement_powershell_could_not_run_behind_exit_code_0_is_named(
    monkeypatch: pytest.MonkeyPatch, command: str, output: str, names: tuple[str, ...] | None
) -> None:
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", True)

    hint = annotate_failure(command, 0, output)

    if names is None:
        assert hint is None
    else:
        assert hint is not None
        for name in names:
            assert name in hint
    # Only PowerShell runs commands this way.
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", False)
    assert annotate_failure(command, 0, output) is None


@pytest.mark.parametrize(
    ("command", "exit_code", "failed", "names"),
    [
        (
            "python check.py | Select-Object -Last 6 && python report.py",
            1,
            _FAILED,
            ("python", "`python report.py`", ";"),
        ),
        (
            "python check.py 2>&1 | findstr FAIL && git add . && git commit -m x",
            1,
            _FAILED,
            ("`git add .` and what follows it",),
        ),
        # -First stops the pipeline early and lets it succeed.
        ("python check.py | Select-Object -First 6 && python report.py", 1, _FAILED, None),
        # || runs its right side after the failure.
        ("python check.py | Select-Object -Last 6 || Write-Output failed", 1, _FAILED, None),
        # Another run of the program may be the part after && that failed.
        ("python check.py | Select-Object -Last 6 && python report.py", 1, _FAILED * 2, None),
        # The exit code is another program's; a pipe in a string is text.
        ("python check.py | Select-Object -Last 6 && git push", 128, _FAILED, None),
        ("Write-Output 'a | b' && python report.py", 1, _FAILED, None),
    ],
)
def test_a_failed_program_in_a_pipeline_names_what_and_and_skipped(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    exit_code: int,
    failed: tuple[str, ...],
    names: tuple[str, ...] | None,
) -> None:
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", True)

    hint = annotate_failure(command, exit_code, "checked 4 files", failed_programs=failed)

    if names is None:
        assert hint is None
    else:
        assert hint is not None and "&&" in hint
        for name in names:
            assert name in hint
    # In bash, && follows the pipeline's last command.
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", False)
    assert annotate_failure(command, exit_code, "checked", failed_programs=failed) is None


@pytest.mark.parametrize(
    ("command", "hinted"),
    [
        (
            "Start-Sleep -Seconds 90; Get-Content C:\\vbot\\commands\\tmp_k3x9a0b2c4d1.log -Tail 5",
            True,
        ),
        ("sleep 60 && tail -n 5 /home/u/.vbot/commands/tmp_k3x9a0b2c4d1.log", True),
        # Waiting for something else, or reading the log without waiting.
        ("Start-Sleep 5; Get-Content server.log -Tail 5", False),
        ("Get-Content C:\\vbot\\commands\\tmp_k3x9a0b2c4d1.log -Tail 5", False),
    ],
)
def test_waiting_to_read_a_background_command_log_names_the_result_message(
    command: str, hinted: bool
) -> None:
    hint = annotate_failure(command, 0, "step 3/9")

    assert (hint is not None and "new message" in hint) is hinted


def _project(tmp_path: Path) -> Path:
    """A working directory holding the package `shop`, a `tests` folder and tools/source_id.py."""
    (tmp_path / "shop").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "source_id.py").touch()
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


def _suggested_call(hint: str | None) -> str:
    """The call a hint suggests: its last code span."""
    assert hint is not None
    spans: list[str] = re.findall(r"`([^`]+)`", hint)
    return spans[-1]


_PYTHONPATH = {True: "$env:PYTHONPATH = (Get-Location).Path; ", False: "PYTHONPATH=. "}


@pytest.mark.parametrize("powershell", [False, True])
@pytest.mark.parametrize(
    ("command", "module", "call"),
    [
        ("python tests/test_cart.py", "shop", "python -m tests.test_cart"),
        (
            "python tests\\test_cart.py 2>&1 | Select-Object -Last 5",
            "tests",
            "python -m tests.test_cart",
        ),
        (
            '& "C:/venv/Scripts/python.exe" -u tests/test_cart.py',
            "shop",
            '& "C:/venv/Scripts/python.exe" -u -m tests.test_cart',
        ),
        ("python {script}", "shop", "python -m tests.test_cart"),
        # Every argument after the script stays, up to a redirection or the statement's end.
        (
            "python tests/test_cart.py --impl shop.fast.total 20000 'two words' > out.txt; echo",
            "shop",
            "python -m tests.test_cart --impl shop.fast.total 20000 'two words'",
        ),
        # -m cannot run a script from a directory that is no module name.
        (
            "python .scratch/probe.py --offline",
            "shop",
            "{pythonpath}python .scratch/probe.py --offline",
        ),
        (
            "python -X utf8 .scratch/probe.py 2>&1",
            "shop",
            "{pythonpath}python -X utf8 .scratch/probe.py",
        ),
    ],
)
def test_module_not_found_from_script_run_by_path_names_module_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    powershell: bool,
    command: str,
    module: str,
    call: str,
) -> None:
    monkeypatch.setattr(shell_hint_common, "_POWERSHELL", powershell)
    workdir = _project(tmp_path)
    scratch = ".scratch" in command
    script = workdir / (".scratch/probe.py" if scratch else "tests/test_cart.py")

    hint = annotate_failure(
        command.format(script=f'"{script}"'), 1, _import_traceback(script, module), workdir=workdir
    )

    assert hint is not None
    if not scratch:
        assert hint.startswith(_RUN_AS_MODULE.format(module=module).split(" Run it as")[0])
    assert _suggested_call(hint) == call.format(pythonpath=_PYTHONPATH[powershell])


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
                "Python imported the package 'shop', but it has no module 'api', and the "
                "working directory has no shop/api.py.",
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
        # Python run with -m, or code from -c or stdin, has the directory it starts in
        # on its import path: the import failed in a process that ran elsewhere, such
        # as one the command started, and -m is no fix.
        (
            "python -m unittest tests.test_crash",
            "AssertionError: the writer failed; stderr=Traceback (most recent call last):\n"
            '  File "<string>", line 3, in <module>\n'
            "ModuleNotFoundError: No module named 'shop'\n",
            True,
            "runs with another working directory or import path",
        ),
        (
            "cd tests; @'\nimport shop\n'@ | python -",
            "Traceback (most recent call last):\n"
            '  File "<stdin>", line 1, in <module>\n'
            "ModuleNotFoundError: No module named 'shop'\n",
            True,
            "runs with another working directory or import path",
        ),
        # Only a subdirectory has the module; Python does not search there.
        (
            "@'\nfrom source_id import source_id\n'@ | python -",
            "Traceback (most recent call last):\n"
            '  File "<stdin>", line 1, in <module>\n'
            "ModuleNotFoundError: No module named 'source_id'\n",
            True,
            "tools/source_id.py",
        ),
    ],
)
def test_import_failures_name_where_python_looked(
    tmp_path: Path, command: str, output: str, workdir_known: bool, expected: str
) -> None:
    workdir = _project(tmp_path) if workdir_known else None

    hint = annotate_failure(command, 1, output, workdir=workdir)

    assert hint is not None and expected in hint


@pytest.mark.parametrize(
    ("files", "names", "omits"),
    [
        # tests/ without __init__.py loses to a regular `tests` package elsewhere.
        (("tests/test_errors.py",), ("tests/test_errors.py", "tests/__init__.py"), ()),
        (
            ("tests/__init__.py", "tests/test_errors.py"),
            ("tests/test_errors.py", "sys.path", "__file__", "__path__"),
            ("__init__.py", "-m"),
        ),
        # A module that is not there: the hint informs and never asks to write it.
        (("tests/support.py",), ("tests/test_errors.py",), ("create", "Add", "write")),
    ],
)
def test_a_missing_submodule_names_what_the_working_directory_holds(
    tmp_path: Path, files: tuple[str, ...], names: tuple[str, ...], omits: tuple[str, ...]
) -> None:
    for name in files:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).touch()
    output = (
        "Traceback (most recent call last):\n"
        '  File "C:\\Python314\\Lib\\unittest\\loader.py", line 137, in loadTestsFromName\n'
        "ModuleNotFoundError: No module named 'tests.test_errors'\n\n"
        "FAILED (errors=1)"
    )

    hint = annotate_failure("python -m unittest tests.test_errors", 1, output, workdir=tmp_path)

    assert hint is not None
    for text in names:
        assert text in hint
    for text in omits:
        assert text not in hint


@pytest.mark.parametrize(
    ("command", "missing", "call"),
    [
        # A module name written as the script's path.
        (
            "python tools.quote_check.py snippets --find 'a b'; python tools.quote_check.py x",
            "tools.quote_check.py",
            "python tools/quote_check.py snippets --find 'a b'",
        ),
        # The same name in a subdirectory, and a similar name beside it.
        (
            "python -u quote_check.py --limit 1",
            "quote_check.py",
            "python -u tools/quote_check.py --limit 1",
        ),
        ("python tools/qoute_check.py", "tools/qoute_check.py", "python tools/quote_check.py"),
        # An interpreter in a variable: the path without a made-up call.
        ("& $python tools/qoute_check.py", "tools/qoute_check.py", ""),
        ("python report.py", "report.py", None),
    ],
)
def test_a_script_python_cannot_open_names_the_existing_path(
    tmp_path: Path, command: str, missing: str, call: str | None
) -> None:
    """*call* is the suggested call, "" for a hint without one, None for no hint."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "quote_check.py").touch()
    # Python names the absolute path in its repr form.
    output = (
        f"C:\\Python314\\python.exe: can't open file {str(tmp_path / missing)!r}: "
        "[Errno 2] No such file or directory"
    )

    hint = annotate_failure(command, 2, output, workdir=tmp_path)

    if call is None:
        assert hint is None
    else:
        assert hint is not None and "tools/quote_check.py" in hint
        assert re.findall(r"`([^`]+)`", hint)[-1:] == ([call] if call else [])
