"""Output-pattern failure hints for the bash tool.

When a command exits non-zero, the raw output often confuses models into
wasted diagnostic turns (e.g. retrying ``python`` when only ``python3``
exists, re-sending a gh field list the installed gh does not support, or
blindly retrying a merge conflict). This module extends the exit-code fact
with an output-pattern tier: a bounded scan of the command output maps
well-known failure shapes to one short, actionable recovery hint.

Design rules (keep these when adding patterns):

* Only fires on non-zero exit codes — never annotate success.
* At most ONE hint per result, first match wins; patterns are ordered by
  how often their failure shape wastes model turns.
* Scans only the first ``_SCAN_CHARS`` of output — hints must key on
  error headers, not deep context.
* The command is not truncated, so a pattern over it must run in linear
  time: one whose failed attempts each scan on to the end of the command is
  quadratic and can stall the Event Loop.
* Hints state the *next action*, not a diagnosis essay. One or two
  sentences.
* No I/O beyond existence checks in the command's working directory —
  trivially unit-testable. The single public entry point is
  ``annotate_failure``.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path

# The shell Tool runs commands in PowerShell on Windows and in bash elsewhere.
_POWERSHELL = sys.platform == "win32"
# Bounded scan window: error headers appear early; deep output is noise.
_SCAN_CHARS = 4000

# Exit-code-only hints for codes whose meaning is platform-stable.
_EXIT_CODE_HINTS: dict[int, str] = {
    126: "Exit 126: the file was found but is not executable — chmod +x it or "
    "invoke it via its interpreter (e.g. `bash script.sh`).",
    137: "Exit 137: the process was SIGKILLed — usually out of memory or an "
    "external kill. Reduce memory use or check `dmesg | tail` before retrying.",
    124: "Exit 124: the command hit its timeout. Raise `timeout`, or run it "
    "with `mode: background` so vBot monitors it and delivers the result "
    "automatically instead of polling.",
}


# POSIX: bash/sh report "<name>: command not found", after a prefix such as
# "bash: line 1: " or "sh: 1: ". The name is a whole run of name characters; the
# lookbehind keeps a failed attempt from rescanning the rest of the same run.
_COMMAND_NOT_FOUND = re.compile(r"(?<![\w.+-])([\w.+-]++): command not found")


def _hint_command_not_found(command: str, output: str, workdir: Path | None) -> str | None:
    m = _COMMAND_NOT_FOUND.search(output)
    if not m:
        return None
    missing = m.group(1)
    if missing == "python":
        return (
            "This system has no bare `python` — use `python3`, or the "
            "project venv's interpreter (e.g. .venv/bin/python)."
        )
    if missing == "pip":
        return (
            "This system has no bare `pip` — use `pip3`, `python3 -m pip`, "
            "or the project venv's pip (e.g. .venv/bin/pip)."
        )
    return (
        f"`{missing}` is not installed or not on PATH. Verify with "
        f"`which {missing}`; install it or use an absolute path instead of "
        "retrying the same command."
    )


# PowerShell equivalents for bash commands Agents reach for on a Windows host.
_POWERSHELL_EQUIVALENTS: dict[str, str] = {
    "grep": (
        "`grep` does not exist in PowerShell: filter output with `| Select-String 'text'` "
        "(add -CaseSensitive to match grep, -NotMatch for -v), and search files with "
        "search_files when available."
    ),
    "sed": (
        "`sed` does not exist in PowerShell: replace text with "
        "`(Get-Content f) -replace 'old', 'new' | Set-Content f`, and print a line range "
        "with `Get-Content f | Select-Object -Skip 9 -First 10`."
    ),
    "awk": (
        "`awk` does not exist in PowerShell: split fields with "
        "`ForEach-Object { ($_ -split '\\s+')[1] }`."
    ),
    "which": "`which` does not exist in PowerShell: use `(Get-Command name).Source`.",
    "touch": (
        "`touch` does not exist in PowerShell: create a file with `New-Item -ItemType File path`."
    ),
    "export": "PowerShell sets environment variables with `$env:NAME = 'value'`, not export.",
    "source": "PowerShell runs a script in the current scope with `. ./script.ps1`, not source.",
    "true": (
        "`true` is not a PowerShell command: drop `|| true`, since a failing command "
        "does not stop the script."
    ),
    "xargs": "`xargs` does not exist in PowerShell: pipe into `ForEach-Object { command $_ }`.",
    "dirname": "Use `Split-Path path -Parent` in PowerShell instead of dirname.",
    "basename": "Use `Split-Path path -Leaf` in PowerShell instead of basename.",
    "python3": "Windows runs Python as `python` (or `py`), not python3.",
    "pip3": "Windows runs pip as `python -m pip`, not pip3.",
}
for _alias in ("egrep", "fgrep"):
    _POWERSHELL_EQUIVALENTS[_alias] = _POWERSHELL_EQUIVALENTS["grep"]
_BASH_SYNTAX_WORDS = frozenset({"if", "then", "fi", "do", "done", "esac", "elif"})
# PowerShell localizes its messages and their quotes; names appear in any of these.
_QUOTES = "'\"\u2018\u2019\u201c\u201d\u201e"
# English and German hosts.
_POWERSHELL_NOT_RECOGNIZED = re.compile(
    rf"(?:The term|Der Begriff) [{_QUOTES}]([^{_QUOTES}]+)[{_QUOTES}] "
    r"(?:is not recognized|wird nicht)"
)


def _quoted_in(name: str, output: str) -> bool:
    return re.search(rf"[{_QUOTES}]{re.escape(name)}[{_QUOTES}]", output) is not None


def _hint_powershell_command_not_found(
    command: str, output: str, workdir: Path | None
) -> str | None:
    # Windows: pwsh reports "The term 'X' is not recognized as a name of a
    # cmdlet, function, script file, or executable program."
    m = _POWERSHELL_NOT_RECOGNIZED.search(output)
    if not m:
        return None
    missing = m.group(1)
    if missing in _POWERSHELL_EQUIVALENTS:
        return _POWERSHELL_EQUIVALENTS[missing]
    if "=" in missing and not missing.startswith("="):
        name, value = missing.split("=", 1)
        return (
            f"PowerShell sets a variable with `$env:{name} = '{value}'` on its own line "
            f"before the command, not `{missing} command`."
        )
    if missing in _BASH_SYNTAX_WORDS:
        return (
            "This is bash syntax. PowerShell writes `if (Test-Path x) { ... }` and "
            "`foreach ($item in $items) { ... }`."
        )
    return (
        f"`{missing}` is not installed or not on PATH. Verify with "
        f"`Get-Command {missing}`; install it or use an absolute path "
        "instead of retrying the same command."
    )


# PowerShell aliases that look like Unix commands but take cmdlet parameters.
_ALIAS_ADVICE: dict[str, tuple[tuple[str, ...], str]] = {
    "Get-ChildItem": (
        ("ls", "dir", "gci"),
        "use `Get-ChildItem -Force` to include hidden files and `-Recurse` for subdirectories",
    ),
    "Remove-Item": (("rm", "del", "rmdir", "rd"), "use `Remove-Item path -Recurse -Force`"),
    "Copy-Item": (("cp", "copy", "cpi"), "use `Copy-Item source destination -Recurse`"),
    "Get-Process": (("ps",), "use `Get-Process`, or `Get-Process name` to filter"),
    "Stop-Process": (("kill",), "use `Stop-Process -Id <pid> -Force`"),
}


def _hint_powershell_alias_flags(command: str, output: str, workdir: Path | None) -> str | None:
    """A bash flag on a PowerShell alias, such as ``ls -la``, fails parameter binding."""
    for cmdlet, (aliases, advice) in _ALIAS_ADVICE.items():
        if not re.search(rf"^{cmdlet}: ", output, re.M):
            continue
        for alias in aliases:
            m = re.search(rf"(?:^|[\s;|&(]){alias}\s+-([A-Za-z]+)", command, re.M)
            if m and _quoted_in(m.group(1), output):
                return (
                    f"`{alias} -{m.group(1)}` uses bash flags, but in PowerShell `{alias}` is "
                    f"{cmdlet}: {advice}."
                )
    return None


# -Recurse after Select-String in the same pipeline step. An attempt ends at the
# next Select-String, which starts its own, so a long command is scanned once.
_SELECT_STRING_RECURSE = re.compile(r"Select-String\b(?:(?!Select-String\b)[^|;\n])*-Recurse", re.I)


def _hint_select_string_recurse(command: str, output: str, workdir: Path | None) -> str | None:
    if not _SELECT_STRING_RECURSE.search(command):
        return None
    if "Select-String" not in output or not _quoted_in("Recurse", output):
        return None
    return (
        "Select-String has no -Recurse: pipe files in with "
        "`Get-ChildItem -Recurse -File | Select-String 'text'`, or use search_files when "
        "available."
    )


_ESCAPED_QUOTE_HINT = (
    'In PowerShell, \\" does not escape a quote. Put the argument in single quotes '
    "(double quotes inside stay literal), or pass longer code through a here-string "
    "piped to the program, such as `@'...'@ | python -`."
)
# A PowerShell here-string: @" or @' ends its line, and "@ or '@ starts a line.
_HERE_STRING_OPENER = re.compile(r"@(['\"])[ \t]*\r?\n")
# '' before a word is a quote doubled as in a single-quoted string; an empty
# string literal such as ''.join is followed by a sign instead.
_DOUBLED_QUOTE = re.compile(r"''\w")


def _here_strings(command: str) -> tuple[list[tuple[str, str]], str]:
    """Return each here-string's quote and text, and the command outside them.

    A here-string ends at the first line that starts with its quote and @. When
    one never ends, no later one with the same quote can end either, so each
    quote is searched to the end of the command at most once.
    """
    found: list[tuple[str, str]] = []
    outside: list[str] = []
    unclosed: set[str] = set()
    copied = position = 0
    while opener := _HERE_STRING_OPENER.search(command, position):
        quote, start = opener[1], opener.end()
        close = -1 if quote in unclosed else command.find(f"\n{quote}@", start)
        if close < 0:
            unclosed.add(quote)
            position = opener.start() + 1
            continue
        end = close - 1 if close > start and command[close - 1] == "\r" else close
        found.append((quote, command[start:end]))
        outside.append(command[copied : opener.start()])
        copied = position = close + 3
    outside.append(command[copied:])
    return found, "".join(outside)


def _hint_powershell_syntax(command: str, output: str, workdir: Path | None) -> str | None:
    """Explain bash heredocs and quote escapes that break code passed in PowerShell.

    PowerShell itself reports a ParserError. Escapes that PowerShell passes on
    unchanged break the code the called program reads, so a Python or Node
    SyntaxError follows; only the Windows shell is PowerShell.
    """
    here_strings, outside = _here_strings(command)
    if "ParserError" in output:
        if re.search(r"<<-?\s*['\"]?[A-Za-z_]\w*['\"]?", command):
            return (
                "PowerShell has no heredoc (<<). Pipe a here-string instead: @' on its own "
                "line, the text, then '@ at the start of a line, followed by `| python -` or "
                "`| Set-Content file`."
            )
        return _ESCAPED_QUOTE_HINT if '\\"' in outside else None
    if not _POWERSHELL or "SyntaxError" not in output:
        return None
    if any(quote == '"' and '\\"' in text for quote, text in here_strings):
        return (
            'Inside a @"..."@ here-string, \\" stays a backslash and a quote. Write the '
            "quotes there without backslashes, or use @'...'@, which also keeps $ literal."
        )
    if any(quote == "'" and _DOUBLED_QUOTE.search(text) for quote, text in here_strings):
        return "Inside a @'...'@ here-string, '' stays two quotes. Write each quote once there."
    return _ESCAPED_QUOTE_HINT if '\\"' in outside else None


def _hint_dev_null(command: str, output: str, workdir: Path | None) -> str | None:
    if "\\dev\\null" not in output:
        return None
    return "PowerShell has no /dev/null: discard output with `2>$null`, `>$null`, or `| Out-Null`."


_MISSING_MODULE = re.compile(
    r"(?:ModuleNotFoundError|ImportError): No module named '?([\w.]+)'?"
    r"(; '[\w.]+' is not a package)?"
)
# A Python script run by its path, such as `python tests/test_x.py` or `py -u tools\run.py`.
_PYTHON_SCRIPT = re.compile(
    r"(?:^|[\s;&|('\"])(?:[^\s;&|()'\"]*[/\\])?(?:python[\d.]*|py)(?:\.exe)?['\"]?\s+"
    r"(?:-[A-Za-z]\w*\s+)*['\"]?([^\s;&|<>'\"]+\.py)\b",
    re.I | re.M,
)


def _hint_module_not_found(command: str, output: str, workdir: Path | None) -> str | None:
    m = _MISSING_MODULE.search(output)
    if not m:
        return None
    name = m.group(1)
    if "." in name:
        parent, _, leaf = name.rpartition(".")
        if m.group(2):
            return (
                f"Python cannot import '{name}': '{parent}' is a single module, not a "
                f"package, so it has no submodules. Import '{leaf}' from the module "
                "that defines it."
            )
        return (
            f"Python imported the package '{parent}', but it has no module '{leaf}'. "
            f"List the files of '{parent}' to check the name, or create the module if "
            "it is still missing."
        )
    if workdir is None or not _in_workdir(name, workdir):
        return (
            f"Python cannot import '{name}': this interpreter finds no package or module "
            "of that name. Check the spelling; if the project has a virtual environment, "
            "run its interpreter, otherwise install the package."
        )
    script = _script_in_subdirectory(command, output, workdir)
    if script is None:
        return (
            f"'{name}' is in the working directory, but the working directory is not on "
            "Python's import path for this command. Run Python from the working directory "
            "with -m, such as `python -m pytest`, or add the working directory to PYTHONPATH."
        )
    run_as = (
        f"`python -m {'.'.join(script.with_suffix('').parts)}`"
        if all(part.isidentifier() for part in script.with_suffix("").parts)
        else "`python -m` with its dotted module name"
    )
    return (
        f"Python cannot import '{name}' because running `{script.as_posix()}` by path puts "
        f"its directory `{script.parent.as_posix()}` on the import path instead of the "
        f"working directory, where '{name}' is. Run it as a module from the working "
        f"directory instead: {run_as}."
    )


def _in_workdir(module: str, workdir: Path) -> bool:
    return (workdir / module).is_dir() or (workdir / f"{module}.py").is_file()


def _script_in_subdirectory(command: str, output: str, workdir: Path) -> Path | None:
    """The script in a subdirectory of ``workdir`` that the command ran by path.

    With several scripts, the first one a traceback names wins; None when no
    traceback names one.
    """
    scripts: list[Path] = []
    for match in _PYTHON_SCRIPT.finditer(command):
        path = Path(match.group(1).replace("\\", "/"))
        if path.is_absolute():
            try:
                path = path.relative_to(workdir)
            except ValueError:
                continue
        if len(path.parts) > 1 and ".." not in path.parts and path not in scripts:
            scripts.append(path)
    if len(scripts) > 1:
        traceback = output.replace("\\", "/")
        scripts = [script for script in scripts if f'/{script.as_posix()}"' in traceback]
    return scripts[0] if scripts else None


def _hint_merge_conflict(command: str, output: str, workdir: Path | None) -> str | None:
    if not re.search(r"^CONFLICT |Automatic merge failed|needs merge", output, re.M):
        return None
    return (
        "Git merge conflict. Do not retry this command. Resolve the "
        "conflicted files listed above (edit, then `git add`), then continue "
        "(`git rebase --continue` / commit the merge) — or abort with "
        "`--abort`."
    )


def _hint_already_exists(command: str, output: str, workdir: Path | None) -> str | None:
    m = re.search(r"(?:fatal|error):.*?'([^']+)' already exists", output)
    if not m:
        return None
    return (
        f"'{m.group(1)}' already exists — retrying unchanged will keep "
        "failing. Reuse it, choose another name, or delete it first if it is "
        "genuinely stale."
    )


def _hint_port_in_use(command: str, output: str, workdir: Path | None) -> str | None:
    m = re.search(
        r"(?:address already in use|"
        r"port (\d+) (?:is )?already in use|"
        r"bind\(\) to .*? port (\d+)|"
        r"bind on address \([^)]*,\s*(\d+))",
        output,
        re.I,
    )
    if not m:
        return None
    port = next((group for group in m.groups() if group), "")
    if port:
        return (
            f"Port {port} is already in use — the intended service may "
            "already be running. Stop it or pick a different port; retrying "
            "the same command will keep failing."
        )
    return (
        "The requested port is already in use — the intended service may "
        "already be running. Stop it or pick a different port; retrying the "
        "same command will keep failing."
    )


def _hint_permission_denied(command: str, output: str, workdir: Path | None) -> str | None:
    if "Permission denied" not in output and "EACCES" not in output:
        return None
    return (
        "Permission denied. Check ownership/mode of the target path; prefer "
        "a user-writable location. Only escalate to sudo if the task "
        "genuinely requires it."
    )


def _hint_rate_limit(command: str, output: str, workdir: Path | None) -> str | None:
    if "API rate limit" not in output and "was submitted too quickly" not in output:
        return None
    return (
        "API rate limit hit — immediate retries will keep failing. Continue "
        "with other work and retry this operation later."
    )


def _hint_gh_unknown_json_field(command: str, output: str, workdir: Path | None) -> str | None:
    m = re.search(r"Unknown JSON field: \"?(\w+)", output)
    if not m:
        return None
    return (
        f"The installed gh does not support the JSON field '{m.group(1)}' — "
        "use only fields from the valid field list printed in the output above."
    )


# Ordered by how often each failure shape wastes a retry turn — first match wins.
_OUTPUT_HINTS: list[Callable[[str, str, Path | None], str | None]] = [
    _hint_gh_unknown_json_field,
    _hint_merge_conflict,
    _hint_command_not_found,
    _hint_powershell_command_not_found,
    _hint_powershell_syntax,
    _hint_powershell_alias_flags,
    _hint_select_string_recurse,
    _hint_dev_null,
    _hint_module_not_found,
    _hint_already_exists,
    _hint_port_in_use,
    _hint_permission_denied,
    _hint_rate_limit,
]


def annotate_failure(
    command: str, exit_code: int | None, output: str, *, workdir: Path | None = None
) -> str | None:
    """Return one short recovery hint for a failed command, or None.

    Args:
        command: The command string that ran.
        exit_code: Its exit code (non-zero for failures; None when the process
            never produced one, e.g. a kill before exit).
        output: Combined stdout/stderr as returned to the model.
        workdir: The directory the command started in, when known.

    Only the first ``_SCAN_CHARS`` characters of output are examined and at
    most one hint is returned. Returns None for ``exit_code`` 0 or None so
    successful and undetermined runs stay unannotated.
    """
    if not exit_code:
        return None
    window = (output or "")[:_SCAN_CHARS]
    if window:
        for fn in _OUTPUT_HINTS:
            try:
                hint = fn(command or "", window, workdir)
            except Exception:
                continue
            if hint:
                return hint
    return _EXIT_CODE_HINTS.get(exit_code)


__all__ = ["annotate_failure"]
