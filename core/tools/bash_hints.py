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
* Hints state the *next action*, not a diagnosis essay. One or two
  sentences.
* Pure functions, no I/O — trivially unit-testable. The single public
  entry point is ``annotate_failure``.
"""

from __future__ import annotations

import re
from collections.abc import Callable

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


def _hint_command_not_found(command: str, output: str) -> str | None:
    # POSIX: bash/sh report "<name>: command not found".
    m = re.search(r"(?:bash: line \d+: |bash: |sh: \d*:? ?)?([\w.+-]+): command not found", output)
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


def _hint_powershell_command_not_found(command: str, output: str) -> str | None:
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


def _hint_powershell_alias_flags(command: str, output: str) -> str | None:
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


def _hint_select_string_recurse(command: str, output: str) -> str | None:
    if not re.search(r"Select-String\b[^|;\n]*-Recurse", command, re.I):
        return None
    if "Select-String" not in output or not _quoted_in("Recurse", output):
        return None
    return (
        "Select-String has no -Recurse: pipe files in with "
        "`Get-ChildItem -Recurse -File | Select-String 'text'`, or use search_files when "
        "available."
    )


def _hint_powershell_parser_error(command: str, output: str) -> str | None:
    if "ParserError" not in output:
        return None
    if re.search(r"<<-?\s*['\"]?[A-Za-z_]\w*['\"]?", command):
        return (
            "PowerShell has no heredoc (<<). Pipe a here-string instead: @' on its own line, "
            "the text, then '@ at the start of a line, followed by `| python -` or "
            "`| Set-Content file`."
        )
    if '\\"' in command:
        return (
            'In PowerShell, \\" does not escape a quote. Put the argument in single quotes '
            "(double quotes inside stay literal), or pass longer code through a here-string "
            "piped to the program, such as `@'...'@ | python -`."
        )
    return None


def _hint_dev_null(command: str, output: str) -> str | None:
    if "\\dev\\null" not in output:
        return None
    return "PowerShell has no /dev/null: discard output with `2>$null`, `>$null`, or `| Out-Null`."


def _hint_module_not_found(command: str, output: str) -> str | None:
    m = re.search(r"(?:ModuleNotFoundError|ImportError): No module named '?([\w.]+)", output)
    if not m:
        return None
    return (
        f"Python cannot import '{m.group(1)}'. Most often the wrong "
        "interpreter is running: activate the project venv or invoke its "
        "python directly. Only pip install if the package is genuinely "
        "absent from that venv."
    )


def _hint_merge_conflict(command: str, output: str) -> str | None:
    if not re.search(r"^CONFLICT |Automatic merge failed|needs merge", output, re.M):
        return None
    return (
        "Git merge conflict. Do not retry this command. Resolve the "
        "conflicted files listed above (edit, then `git add`), then continue "
        "(`git rebase --continue` / commit the merge) — or abort with "
        "`--abort`."
    )


def _hint_already_exists(command: str, output: str) -> str | None:
    m = re.search(r"(?:fatal|error):.*?'([^']+)' already exists", output)
    if not m:
        return None
    return (
        f"'{m.group(1)}' already exists — retrying unchanged will keep "
        "failing. Reuse it, choose another name, or delete it first if it is "
        "genuinely stale."
    )


def _hint_port_in_use(command: str, output: str) -> str | None:
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


def _hint_permission_denied(command: str, output: str) -> str | None:
    if "Permission denied" not in output and "EACCES" not in output:
        return None
    return (
        "Permission denied. Check ownership/mode of the target path; prefer "
        "a user-writable location. Only escalate to sudo if the task "
        "genuinely requires it."
    )


def _hint_rate_limit(command: str, output: str) -> str | None:
    if "API rate limit" not in output and "was submitted too quickly" not in output:
        return None
    return (
        "API rate limit hit — immediate retries will keep failing. Continue "
        "with other work and retry this operation later."
    )


def _hint_gh_unknown_json_field(command: str, output: str) -> str | None:
    m = re.search(r"Unknown JSON field: \"?(\w+)", output)
    if not m:
        return None
    return (
        f"The installed gh does not support the JSON field '{m.group(1)}' — "
        "use only fields from the valid field list printed in the output above."
    )


# Ordered by how often each failure shape wastes a retry turn — first match wins.
_OUTPUT_HINTS: list[Callable[[str, str], str | None]] = [
    _hint_gh_unknown_json_field,
    _hint_merge_conflict,
    _hint_command_not_found,
    _hint_powershell_command_not_found,
    _hint_powershell_parser_error,
    _hint_powershell_alias_flags,
    _hint_select_string_recurse,
    _hint_dev_null,
    _hint_module_not_found,
    _hint_already_exists,
    _hint_port_in_use,
    _hint_permission_denied,
    _hint_rate_limit,
]


def annotate_failure(command: str, exit_code: int | None, output: str) -> str | None:
    """Return one short recovery hint for a failed command, or None.

    Args:
        command: The command string that ran.
        exit_code: Its exit code (non-zero for failures; None when the process
            never produced one, e.g. a kill before exit).
        output: Combined stdout/stderr as returned to the model.

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
                hint = fn(command or "", window)
            except Exception:
                continue
            if hint:
                return hint
    return _EXIT_CODE_HINTS.get(exit_code)


__all__ = ["annotate_failure"]
