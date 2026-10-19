"""Python import and script invocation diagnostics."""

from __future__ import annotations

import re
from itertools import islice
from pathlib import Path

from core.tools._path_suggestions import similar_entries

from . import _shell_hint_common as _common
from ._shell_hint_common import _Failure

_MISSING_MODULE = re.compile(
    r"(?:ModuleNotFoundError|ImportError): No module named '?([\w.]+)'?"
    r"(; '[\w.]+' is not a package)?"
)
# A Python script run by its path, such as `python tests/test_x.py` or `py -u tools\run.py`:
# the interpreter as written, its options (after a closing quote of the
# interpreter's path; -X and -W with their value, as in `-X utf8`), and the script.
_PYTHON_SCRIPT = re.compile(
    r"(?:^|[\s;&|('\"])(?P<interpreter>(?:[^\s;&|()'\"]*[/\\])?(?:python[\d.]*|py)(?:\.exe)?)"
    r"(?P<options>['\"]?(?:\s+-(?:[XW][ \t]*+[^\s;&|<>'\"-][^\s;&|<>'\"]*+|[A-Za-z]\w*))*)"
    r"\s+['\"]?(?P<script>[^\s;&|<>'\"]+\.py)\b",
    re.I | re.M,
)


# Python run with -m, which puts the working directory on the import path.
_PYTHON_MODULE_RUN = re.compile(
    r"(?:^|[\s;&|('\"])(?:[^\s;&|()'\"]*[/\\])?(?:python[\d.]*|py)(?:\.exe)?['\"]?\s+"
    r"(?:-[A-Za-z]\w*\s+)*?-m(?=\s)",
    re.I | re.M,
)
# A traceback frame; <string> and <stdin> are code passed with -c or on stdin.
_TRACEBACK_FILE = re.compile(r'^[ \t]*File "([^"\n]*)"', re.M)


def _hint_module_not_found(failure: _Failure) -> str | None:
    match = _MISSING_MODULE.search(failure.output, failure.error_start)
    if not match:
        return None
    name = match.group(1)
    if "." in name:
        parent, _, leaf = name.rpartition(".")
        if match.group(2):
            return (
                f"Python cannot import '{name}': '{parent}' is a single module, not a "
                f"package, so it has no submodules. Import '{leaf}' from the module "
                "that defines it."
            )
        return _missing_submodule(name, failure.workdir)
    workdir = failure.workdir
    if workdir is None or not _in_workdir(name, workdir):
        below = [] if workdir is None else _in_subdirectories(name, workdir)
        if below:
            directory = below[0][1]
            setting = (
                f"$env:PYTHONPATH = (Resolve-Path {directory}).Path;"
                if _common._POWERSHELL
                else f"PYTHONPATH={directory}"
            )
            shown = " and ".join(path for path, _ in below)
            fix = (
                f"Put `{setting}` before the python call to import it from {directory}/."
                if len(below) == 1
                else "Put the directory of the one you mean on PYTHONPATH before the python "
                f"call, such as `{setting}`."
            )
            where = "a subdirectory" if len(below) == 1 else "subdirectories"
            return (
                f"Python cannot import '{name}': the working directory has it only in {where}, "
                f"as {shown}, and Python does not search subdirectories; it "
                "imports from the directory of the script it runs (the working directory for "
                f"-c, - and -m) and from PYTHONPATH. {fix}"
            )
        return (
            f"Python cannot import '{name}': this interpreter finds no package or module "
            "of that name. Check the spelling; if the project has a virtual environment, "
            "run its interpreter, otherwise install the package."
        )
    found = _script_in_subdirectory(failure.command, failure.output, workdir)
    if found is None and _imported_elsewhere(failure, match.start()):
        return (
            f"'{name}' is in the working directory, but the Python process that could not "
            "import it runs with another working directory or import path, such as a process "
            "the command started. Start that process in the working directory, or add the "
            "working directory to its PYTHONPATH."
        )
    if found is None:
        return (
            f"'{name}' is in the working directory, but the working directory is not on "
            "Python's import path for this command. Run Python from the working directory "
            "with -m, such as `python -m pytest`, or add the working directory to PYTHONPATH."
        )
    script, run = found
    cause = (
        f"Python cannot import '{name}' because running `{script.as_posix()}` by path puts "
        f"its directory `{script.parent.as_posix()}` on the import path instead of the "
        f"working directory, where '{name}' is."
    )
    if all(part.isidentifier() for part in script.with_suffix("").parts):
        return (
            f"{cause} Run it as a module from the working directory instead: "
            f"{_run_as_module(failure.command, script, run)}."
        )
    # A directory such as .scratch is no module name, so -m cannot run the script.
    return (
        f"{cause} Put the working directory on the import path for this run: "
        f"{_run_with_working_directory(failure.command, run)}."
    )


# Python's report of a script it cannot open, with the path in repr form.
_SCRIPT_NOT_FOUND = re.compile(r"can't open file '([^'\n]++)': \[Errno 2\]")


def _hint_script_not_found(failure: _Failure) -> str | None:
    """Name the existing script that a missing script path most likely means.

    Agents wrote a script's path as a module name: ``python tools.quote_check.py``
    for tools/quote_check.py failed eight times in one run without a hint. As
    read's not-found error does, the hint names existing paths: the dotted name
    as directories, the same name in a subdirectory, and similar names beside it.
    """
    workdir = failure.workdir
    match = _SCRIPT_NOT_FOUND.search(failure.error)
    if workdir is None or match is None:
        return None
    missing = workdir / match[1].replace("\\\\", "\\")
    name = missing.name
    parts = name.removesuffix(".py").split(".")
    dotted = missing.parent.joinpath(*parts[:-1], f"{parts[-1]}.py")
    if name.endswith(".py") and len(parts) > 1 and dotted.is_file():
        found, dots = [dotted], True
    else:
        found = [
            directory / name
            for directory in _subdirectories(missing.parent)
            if (directory / name).is_file()
        ] or [
            path
            for path in similar_entries(missing, kind="files")
            if path.suffix.casefold() == missing.suffix.casefold()
        ]
        found, dots = found[:_SHOWN_PATHS], False
    if not found:
        return None
    shown = [_shown_path(path, workdir) for path in found]
    call = _call_with_script(failure.command, missing, workdir, shown[0])
    if dots:
        return (
            f"Python found no file {_shown_path(missing, workdir)}: a path separates "
            f"directories with /, not dots, so the script is {shown[0]}. "
            f"Run {call or 'it by that path'}."
        )
    return (
        f"Python found no file {_shown_path(missing, workdir)}, but similar files exist: "
        f"{', '.join(shown)}. Run the one you meant by that path"
        f"{f', such as {call}' if call else ''}."
    )


def _shown_path(path: Path, workdir: Path) -> str:
    """*path* relative to *workdir* where it is below it, with forward slashes."""
    try:
        return path.relative_to(workdir).as_posix()
    except ValueError:
        return path.as_posix()


def _call_with_script(command: str, missing: Path, workdir: Path, script: str) -> str | None:
    """The Python call that ran *missing*, with *script* in its place.

    None when the command does not show that call, such as ``& $python x.py``.
    """
    for run in _PYTHON_SCRIPT.finditer(command):
        written = workdir / run["script"]
        if str(written).casefold() != str(missing).casefold():
            continue
        start, _script_end, rest = _script_call(command, run)
        return _shown_call(f"{command[start : run.end('options')]} {script}", rest)
    return None


def _missing_submodule(name: str, workdir: Path | None) -> str:
    """The hint for a package that has no module of the imported name.

    "Create the module" fired when tests imported a teammate's module that had
    not landed yet, inviting Agents to write someone else's file. When the
    module is in the working directory, Python imported another package of the
    same name: a ``tests`` folder without ``__init__.py`` lost to an installed
    ``tests`` package.
    """
    parent, _, leaf = name.rpartition(".")
    parts = name.split(".")
    top = parts[0]
    package = Path(*parts[:-1])
    if workdir is not None:
        module = next(
            (
                path
                for path in (package / f"{leaf}.py", package / leaf)
                if (workdir / path).is_file() or (path.name == leaf and (workdir / path).is_dir())
            ),
            None,
        )
        if module is not None:
            shown = module.as_posix()
            if not (workdir / top / "__init__.py").is_file():
                return (
                    f"{shown} exists in the working directory, but Python imported another "
                    f"package named '{top}': the working directory's {top}/ has no __init__.py, "
                    f"so a '{top}' package elsewhere on the import path shadows it. Add an empty "
                    f"{top}/__init__.py."
                )
            return (
                f"{shown} exists in the working directory, but Python did not find it on the "
                f"active import path. Check sys.path and the loaded {parent} package's __file__ "
                "and __path__; correct import-path entries that select another package."
            )
        if (workdir / package).is_dir():
            return (
                f"Python imported the package '{parent}', but it has no module '{leaf}', and "
                f"the working directory has no {package.as_posix()}/{leaf}.py. Check the name "
                f"against the files in {package.as_posix()}/; if it is right, that module is "
                "not written yet."
            )
    return (
        f"Python imported the package '{parent}', but it has no module '{leaf}'. Check the "
        f"name against the files of '{parent}'."
    )


def _imported_elsewhere(failure: _Failure, error_at: int) -> bool:
    """Whether the failed import ran in a Python process outside the working directory.

    Python run with -m, or code run with -c or from stdin, has the directory it
    starts in on its import path. When such an import of a module in the working
    directory fails, the process ran elsewhere: often a process the command
    started, such as a test's subprocess. "Run it with -m" was then wrong advice.
    """
    if _PYTHON_MODULE_RUN.search(failure.command):
        return True
    frames = _TRACEBACK_FILE.findall(failure.output, 0, error_at)
    return bool(frames) and frames[-1] in ("<string>", "<stdin>")


def _in_workdir(module: str, workdir: Path) -> bool:
    return (workdir / module).is_dir() or (workdir / f"{module}.py").is_file()


# Directory entries of the working directory looked at for a module or script below it.
_LISTED_ENTRIES = 200
_SHOWN_PATHS = 3


def _subdirectories(workdir: Path) -> list[Path]:
    """The working directory's subdirectories, sorted, from a bounded listing."""
    try:
        entries = sorted(islice(workdir.iterdir(), _LISTED_ENTRIES))
    except OSError:
        return []
    found: list[Path] = []
    for entry in entries:
        try:
            if entry.is_dir() and entry.name != "__pycache__":
                found.append(entry)
        except OSError:
            continue
    return found


def _in_subdirectories(module: str, workdir: Path) -> list[tuple[str, str]]:
    """A module's file or package directly in subdirectories of *workdir*.

    Each as (path shown, its subdirectory), relative to *workdir*. A script piped
    into Python imported a module that lives in tools/; the hint sent the Agent
    to install a package of that name.
    """
    found: list[tuple[str, str]] = []
    for directory in _subdirectories(workdir):
        if (directory / f"{module}.py").is_file():
            found.append((f"{directory.name}/{module}.py", directory.name))
        elif (directory / module / "__init__.py").is_file():
            found.append((f"{directory.name}/{module}/", directory.name))
        if len(found) == _SHOWN_PATHS:
            break
    return found


def _script_in_subdirectory(
    command: str, output: str, workdir: Path
) -> tuple[Path, re.Match[str]] | None:
    """The script in a subdirectory of ``workdir`` that the command ran by path,
    with the match of the Python call that ran it.

    With several scripts, the first one a traceback names wins; None when no
    traceback names one.
    """
    scripts: dict[Path, re.Match[str]] = {}
    for match in _PYTHON_SCRIPT.finditer(command):
        path = Path(match["script"].replace("\\", "/"))
        if path.is_absolute():
            try:
                path = path.relative_to(workdir)
            except ValueError:
                continue
        if len(path.parts) > 1 and ".." not in path.parts and path not in scripts:
            scripts[path] = match
    found = list(scripts.items())
    if len(found) > 1:
        traceback = output.replace("\\", "/")
        found = [item for item in found if f'/{item[0].as_posix()}"' in traceback]
    return found[0] if found else None


# Longer arguments are named, not repeated, in a suggested call.
_ARGUMENTS_SHOWN_CHARS = 400
# The arguments after a script's path, up to a redirection or the end of its
# statement: quoted or plain words.
_SCRIPT_ARGUMENTS = re.compile(
    r"""(?:[ \t]++(?![0-9*]*+>)(?:'[^'\n]*+'|"[^"\n]*+"|[^\s;&|<>'"])++)*+"""
)


def _run_as_module(command: str, script: Path, run: re.Match[str]) -> str:
    """The Python call that ran *script* by path, rewritten to run it with -m.

    The interpreter as written, its options and every argument after the script
    stay unchanged: one suggestion dropped `--impl minidb.sqlite_float.format_real`,
    which chose the implementation the script tested.
    """
    start, _script_end, rest = _script_call(command, run)
    call = f"{command[start : run.end('options')]} -m {'.'.join(script.with_suffix('').parts)}"
    return _shown_call(call, rest)


def _run_with_working_directory(command: str, run: re.Match[str]) -> str:
    """The Python call that ran a script by path, with the working directory on PYTHONPATH.

    For a script in a directory that is no module name, such as ``.scratch``,
    where -m cannot run it. The variable is set in the shell's own syntax.
    """
    start, script_end, rest = _script_call(command, run)
    setting = "$env:PYTHONPATH = (Get-Location).Path; " if _common._POWERSHELL else "PYTHONPATH=. "
    return _shown_call(f"{setting}{command[start:script_end]}", rest)


def _script_call(command: str, run: re.Match[str]) -> tuple[int, int, str]:
    """Where the Python call *run* starts and its script ends, and the arguments after it."""
    start = run.start("interpreter")
    if start > 0 and command[start - 1] in "'\"":
        # A quoted interpreter path, run with PowerShell's call operator.
        start -= 1
        before = command[:start].rstrip(" \t")
        if before.endswith("&") and not before.endswith("&&"):
            start = len(before) - 1
    end = run.end("script")
    if end < len(command) and command[end] in "'\"":
        end += 1
    arguments = _SCRIPT_ARGUMENTS.match(command, end)
    return start, end, arguments[0].rstrip() if arguments else ""


def _shown_call(call: str, rest: str) -> str:
    if len(rest) > _ARGUMENTS_SHOWN_CHARS:
        return f"`{call}` followed by the same arguments"
    return f"`{call}{rest}`"
