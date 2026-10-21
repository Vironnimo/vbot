"""What known commands do with their arguments: the tier A path specs.

PowerShell cmdlets are described by bound parameter name, POSIX programs and
cmd built-ins by a per-command option and operand spec. Commands not named here
get tier B (only clearly-path words count, as reads; ``_analysis.py``).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

type AccessKind = Literal["read", "write"]


@dataclass(frozen=True, slots=True)
class CmdletParameter:
    """One path parameter: the access, whether wildcards expand, what makes it reach a subtree.

    ``subtree`` is True when the access always covers what lies below the
    path, or the names of the parameters that make it do so.
    """

    kind: AccessKind
    literal: bool = False
    subtree: bool | tuple[str, ...] = False


def _parameters(
    kind: AccessKind, *names: str, subtree: bool | tuple[str, ...] = False
) -> dict[str, CmdletParameter]:
    return {
        name: CmdletParameter(kind, literal=name == "LiteralPath", subtree=subtree)
        for name in names
    }


_PATH = ("Path", "LiteralPath")

CMDLETS: Mapping[str, Mapping[str, CmdletParameter]] = {
    # Writes.
    "set-content": _parameters("write", *_PATH),
    "add-content": _parameters("write", *_PATH),
    "clear-content": _parameters("write", *_PATH),
    "out-file": _parameters("write", "FilePath", "LiteralPath"),
    "tee-object": _parameters("write", "FilePath", "LiteralPath"),
    "new-item": _parameters("write", *_PATH),
    "remove-item": _parameters("write", *_PATH, subtree=True),
    "move-item": {
        **_parameters("write", *_PATH, subtree=True),
        **_parameters("write", "Destination"),
    },
    "rename-item": _parameters("write", *_PATH, subtree=True),
    "copy-item": {
        **_parameters("read", *_PATH, subtree=("Recurse",)),
        **_parameters("write", "Destination", subtree=("Recurse",)),
    },
    "set-item": _parameters("write", *_PATH),
    "clear-item": _parameters("write", *_PATH),
    "set-itemproperty": _parameters("write", *_PATH),
    "new-itemproperty": _parameters("write", *_PATH),
    "clear-itemproperty": _parameters("write", *_PATH),
    "remove-itemproperty": _parameters("write", *_PATH),
    "rename-itemproperty": _parameters("write", *_PATH),
    "set-acl": _parameters("write", *_PATH),
    "unblock-file": _parameters("write", *_PATH),
    "compress-archive": {
        **_parameters("read", *_PATH, subtree=True),
        **_parameters("write", "DestinationPath"),
    },
    "expand-archive": {
        **_parameters("read", *_PATH),
        **_parameters("write", "DestinationPath", subtree=True),
    },
    "invoke-webrequest": {**_parameters("write", "OutFile"), **_parameters("read", "InFile")},
    "invoke-restmethod": {**_parameters("write", "OutFile"), **_parameters("read", "InFile")},
    "start-transcript": _parameters("write", *_PATH, "OutputDirectory"),
    "mkdir": _parameters("write", *_PATH),
    # Reads.
    "get-content": _parameters("read", *_PATH),
    "get-childitem": _parameters("read", *_PATH, subtree=("Recurse", "Depth")),
    "get-item": _parameters("read", *_PATH),
    "get-itemproperty": _parameters("read", *_PATH),
    "get-itempropertyvalue": _parameters("read", *_PATH),
    "test-path": _parameters("read", *_PATH),
    "resolve-path": _parameters("read", *_PATH),
    "convert-path": _parameters("read", *_PATH),
    "select-string": _parameters("read", *_PATH),
    "get-filehash": _parameters("read", *_PATH),
    "get-acl": _parameters("read", *_PATH),
    "format-hex": _parameters("read", *_PATH),
    "get-authenticodesignature": _parameters("read", "FilePath", "LiteralPath"),
    "invoke-item": _parameters("read", *_PATH),
    "invoke-command": _parameters("read", "FilePath"),
    "start-job": _parameters("read", "FilePath"),
    "import-module": _parameters("read", "Name"),
}
# Parameters that name a module or a file: only a value that looks like a file is a path.
NAME_OR_PATH_PARAMETERS = frozenset({("import-module", "Name")})
MODULE_FILE = re.compile(r"[\\/]|\.(?:psm1|psd1|ps1|dll|cdxml|xaml)$", re.IGNORECASE)

# Verbs whose cmdlets change what their path parameters name, for cmdlets the
# table does not list (Export-Csv, Save-Help, ...).
_WRITE_VERBS = frozenset(
    {
        "add", "clear", "compress", "copy", "expand", "export", "install", "move", "new",
        "out", "publish", "register", "remove", "rename", "save", "set", "uninstall",
        "unregister", "update", "write",
    }
)  # fmt: skip
_GENERIC_PATH_PARAMETERS = (
    "Path", "LiteralPath", "FilePath", "Destination", "DestinationPath", "OutFile",
    "OutputPath", "OutputDirectory", "InFile",
)  # fmt: skip

# Cmdlets that change the location (cwd tracking) or take part in pipelines.
LOCATION_CMDLETS = frozenset({"set-location", "push-location", "pop-location"})
# Their output names the paths they list (``gci ..\x | Remove-Item``).
LISTING_CMDLETS = frozenset(
    {"get-childitem", "get-item", "resolve-path", "convert-path", "select-string"}
)
# They pass pipeline objects through unchanged in kind.
FILTER_CMDLETS = frozenset(
    {"where-object", "select-object", "sort-object", "get-unique", "tee-object"}
)
# PowerShell's built-in location functions, read as Set-Location.
LOCATION_FUNCTIONS = {"cd..": "..", "cd\\": "\\", "cd/": "/", "cd~": "~"}


def cmdlet_parameters(name: str) -> Mapping[str, CmdletParameter] | None:
    """The path parameters of a cmdlet or function (lower-case name), else None."""
    known = CMDLETS.get(name)
    if known is not None:
        return known
    verb, dash, noun = name.partition("-")
    if not dash or not noun:
        return None
    kind: AccessKind = "write" if verb in _WRITE_VERBS else "read"
    return _parameters(kind, *_GENERIC_PATH_PARAMETERS)


@dataclass(frozen=True, slots=True)
class ProgramSpec:
    """How a program's options and operands touch paths.

    ``operands`` is the access of each operand, ``first`` overrides it for the
    first (``zip out.zip files``), ``last`` for the last of at least two
    (``cp a b``), ``skip`` operands lead that name no path
    (``grep PATTERN``, ``chmod MODE``) unless an option of ``skip_cancel``
    supplies them. ``values`` lists options that take a value, with that
    value's access or None. Options of ``subtree_flags`` (or ``subtree``)
    make every access reach what lies below its path; options of
    ``write_flags`` make operands writes (``sed -i``). ``default`` is the
    access on the working directory without any operand. ``listing``: the
    output names paths below the operands. ``filter``: passes pipeline input
    through. ``slash``: options start with ``/`` (Windows programs). Options of
    ``search_flags`` apply each operand's name at every depth below its folder
    (``del /s *.tmp`` deletes ``*.tmp`` in every subfolder).
    """

    operands: AccessKind | None = None
    first: AccessKind | None = None
    last: AccessKind | None = None
    skip: int = 0
    skip_cancel: frozenset[str] = frozenset()
    values: Mapping[str, AccessKind | None] = field(default_factory=dict)
    subtree_flags: frozenset[str] = frozenset()
    subtree: bool = False
    default: AccessKind | None = None
    write_flags: frozenset[str] = frozenset()
    listing: bool = False
    filter: bool = False
    slash: bool = False
    search_flags: frozenset[str] = frozenset()


_SLASH_S = frozenset({"/s", "/S"})

_RECURSIVE = frozenset({"-r", "-R", "--recursive"})
_READ_TEXT = ProgramSpec(
    operands="read", values={"-n": None, "-c": None, "--lines": None, "--bytes": None}
)
_HASH = ProgramSpec(operands="read")

PROGRAMS: Mapping[str, ProgramSpec] = {
    "rm": ProgramSpec(operands="write", subtree_flags=_RECURSIVE),
    "unlink": ProgramSpec(operands="write"),
    "rmdir": ProgramSpec(operands="write"),
    "shred": ProgramSpec(operands="write", values={"-n": None, "-s": None}),
    "mv": ProgramSpec(
        operands="write",
        subtree=True,
        values={"-t": "write", "--target-directory": "write", "-S": None},
    ),
    "cp": ProgramSpec(
        operands="read",
        last="write",
        values={"-t": "write", "--target-directory": "write", "-S": None},
        subtree_flags=frozenset({"-r", "-R", "-a", "--recursive", "--archive"}),
    ),
    "install": ProgramSpec(
        operands="read", last="write", values={"-t": "write", "-m": None, "-o": None, "-g": None}
    ),
    "rsync": ProgramSpec(
        operands="read", last="write", subtree=True, values={"-e": None, "--rsh": None}
    ),
    "scp": ProgramSpec(
        operands="read",
        last="write",
        subtree_flags=frozenset({"-r"}),
        values={
            "-i": "read",
            "-F": "read",
            "-P": None,
            "-o": None,
            "-J": None,
            "-l": None,
            "-c": None,
            "-S": None,
            "-D": None,
            "-X": None,
        },
    ),  # fmt: skip
    "mkdir": ProgramSpec(operands="write", values={"-m": None, "--mode": None}),
    "touch": ProgramSpec(
        operands="write",
        values={"-d": None, "-t": None, "-r": "read", "--reference": "read", "--date": None},
    ),
    "chmod": ProgramSpec(
        operands="write", skip=1, skip_cancel=frozenset({"--reference"}), subtree_flags=_RECURSIVE
    ),
    "chown": ProgramSpec(
        operands="write", skip=1, skip_cancel=frozenset({"--reference"}), subtree_flags=_RECURSIVE
    ),
    "chgrp": ProgramSpec(
        operands="write", skip=1, skip_cancel=frozenset({"--reference"}), subtree_flags=_RECURSIVE
    ),
    "truncate": ProgramSpec(
        operands="write", values={"-s": None, "--size": None, "-r": "read", "--reference": "read"}
    ),
    "tee": ProgramSpec(operands="write", filter=True),
    "sed": ProgramSpec(
        operands="read",
        skip=1,
        skip_cancel=frozenset({"-e", "-f", "--expression", "--file"}),
        values={"-e": None, "--expression": None, "-f": "read", "--file": "read", "-l": None},
        write_flags=frozenset({"-i", "--in-place"}),
    ),
    "sort": ProgramSpec(
        operands="read",
        values={
            "-o": "write",
            "--output": "write",
            "-k": None,
            "-t": None,
            "-S": None,
            "-T": "write",
            "--key": None,
        },
        filter=True,
    ),
    "cat": ProgramSpec(operands="read"),
    "tac": ProgramSpec(operands="read", filter=True),
    "head": ProgramSpec(
        operands="read",
        values={"-n": None, "-c": None, "--lines": None, "--bytes": None},
        filter=True,
    ),
    "tail": ProgramSpec(
        operands="read",
        values={"-n": None, "-c": None, "--lines": None, "--bytes": None, "-s": None},
        filter=True,
    ),
    "less": _READ_TEXT,
    "more": _READ_TEXT,
    "nl": _READ_TEXT,
    "wc": ProgramSpec(operands="read"),
    "uniq": ProgramSpec(
        operands="read", last="write", filter=True, values={"-f": None, "-s": None, "-w": None}
    ),
    "cut": ProgramSpec(operands="read", values={"-d": None, "-f": None, "-b": None, "-c": None}),
    "file": ProgramSpec(operands="read"),
    "stat": ProgramSpec(operands="read", values={"-c": None, "--format": None}),
    "strings": ProgramSpec(operands="read"),
    "xxd": ProgramSpec(operands="read", last="write"),
    "od": ProgramSpec(operands="read"),
    "hexdump": ProgramSpec(operands="read"),
    "base64": ProgramSpec(operands="read"),
    "md5sum": _HASH,
    "sha1sum": _HASH,
    "sha256sum": _HASH,
    "sha512sum": _HASH,
    "readlink": _HASH,
    "realpath": _HASH,
    "diff": ProgramSpec(operands="read", subtree_flags=_RECURSIVE),
    "cmp": ProgramSpec(operands="read"),
    "comm": ProgramSpec(operands="read"),
    "source": ProgramSpec(operands="read"),
    ".": ProgramSpec(operands="read"),
    "ls": ProgramSpec(
        operands="read",
        default="read",
        subtree_flags=frozenset({"-R", "--recursive"}),
        listing=True,
    ),
    "dir": ProgramSpec(operands="read", default="read", listing=True),
    "tree": ProgramSpec(
        operands="read", default="read", subtree=True, values={"-L": None, "-I": None, "-P": None}
    ),
    "du": ProgramSpec(
        operands="read", default="read", subtree=True, values={"-d": None, "--max-depth": None}
    ),
    "grep": ProgramSpec(
        operands="read",
        skip=1,
        skip_cancel=frozenset({"-e", "-f", "--regexp", "--file"}),
        values={
            "-e": None,
            "--regexp": None,
            "-f": "read",
            "--file": "read",
            "-m": None,
            "--max-count": None,
            "-A": None,
            "-B": None,
            "-C": None,
            "--include": None,
            "--exclude": None,
            "--exclude-dir": None,
            "-d": None,
            "-D": None,
        },
        subtree_flags=frozenset({"-r", "-R", "--recursive", "--dereference-recursive"}),
        filter=True,
        listing=True,
    ),  # fmt: skip
    "rg": ProgramSpec(
        operands="read",
        skip=1,
        skip_cancel=frozenset({"-e", "-f", "--regexp", "--file", "--files"}),
        values={
            "-e": None,
            "--regexp": None,
            "-f": "read",
            "--file": "read",
            "-g": None,
            "--glob": None,
            "--iglob": None,
            "-t": None,
            "-T": None,
            "--type": None,
            "-m": None,
            "-A": None,
            "-B": None,
            "-C": None,
            "-j": None,
            "-M": None,
            "--max-depth": None,
            "--ignore-file": "read",
            "-r": None,
            "--replace": None,
        },
        subtree=True,
        default="read",
        filter=True,
        listing=True,
    ),  # fmt: skip
    "fd": ProgramSpec(
        operands="read",
        skip=1,
        subtree=True,
        default="read",
        listing=True,
        values={
            "-e": None,
            "-t": None,
            "-E": None,
            "-d": None,
            "--max-depth": None,
            "-x": None,
            "-X": None,
        },
    ),  # fmt: skip
    "zip": ProgramSpec(
        operands="read", first="write", subtree_flags=frozenset({"-r", "--recurse-paths"})
    ),
    "virtualenv": ProgramSpec(
        operands="write", subtree=True, values={"-p": None, "--python": None}
    ),
    "gzip": ProgramSpec(operands="write"),
    "gunzip": ProgramSpec(operands="write"),
    "bzip2": ProgramSpec(operands="write"),
    "xz": ProgramSpec(operands="write"),
    # Programs that open no file their words name: no mentions (CommandPlan.mentions).
    **dict.fromkeys(
        (
            "echo",
            "printf",
            "sleep",
            "true",
            "false",
            "test",
            "[",
            "basename",
            "dirname",
            "seq",
            "expr",
            "which",
            "whoami",
            "date",
            "export",
            "unset",
            "exit",
            "set",
            "shift",
            # cmd internals
            "assoc",
            "break",
            "cls",
            "color",
            "endlocal",
            "ftype",
            "goto",
            "path",
            "pause",
            "prompt",
            "rem",
            "setlocal",
            "time",
            "title",
            "ver",
            "verify",
            "vol",
        ),
        ProgramSpec(),
    ),  # fmt: skip
    # Windows programs with switches.
    "xcopy": ProgramSpec(
        operands="read",
        last="write",
        subtree=True,
        slash=True,
        search_flags=frozenset({"/s", "/S", "/e", "/E"}),
    ),
    "robocopy": ProgramSpec(operands="read", last="write", subtree=True, slash=True),
    "takeown": ProgramSpec(operands="write", slash=True, values={"/f": "write", "/F": "write"}),
    "findstr": ProgramSpec(
        operands="read",
        skip=1,
        slash=True,
        filter=True,
        subtree_flags=_SLASH_S,
        search_flags=_SLASH_S,
    ),
    # where /r folder names: searches the whole folder; without /r it searches PATH.
    "where": ProgramSpec(
        slash=True, values={"/r": "read", "/R": "read"}, subtree_flags=frozenset({"/r", "/R"})
    ),
    "certutil": ProgramSpec(operands="read", slash=True),
}

# cmd built-ins (``cmd /c``): slash options, the same operand model.
CMD_BUILTINS: Mapping[str, ProgramSpec] = {
    "del": ProgramSpec(operands="write", subtree_flags=_SLASH_S, search_flags=_SLASH_S, slash=True),
    "erase": ProgramSpec(
        operands="write", subtree_flags=_SLASH_S, search_flags=_SLASH_S, slash=True
    ),
    "rd": ProgramSpec(operands="write", subtree=True, slash=True),
    "rmdir": ProgramSpec(operands="write", subtree=True, slash=True),
    "md": ProgramSpec(operands="write", slash=True),
    "mkdir": ProgramSpec(operands="write", slash=True),
    "copy": ProgramSpec(operands="read", last="write", default="write", slash=True),
    "move": ProgramSpec(operands="write", subtree=True, slash=True),
    "type": ProgramSpec(operands="read", slash=True),
    "dir": ProgramSpec(
        operands="read",
        default="read",
        subtree_flags=_SLASH_S,
        search_flags=_SLASH_S,
        slash=True,
        listing=True,
    ),
    "mklink": ProgramSpec(operands="read", skip=0, slash=True),
}

# Programs whose output names the files below the folders they list.
LISTING_PROGRAMS = frozenset({"find", "ls", "dir", "fd", "fdfind", "grep", "rg"})
# Options that leave a folder out at every depth of a recursive search, by program:
# option -> (the value names it after "!", the match ignores case).
PRUNE_OPTIONS: Mapping[str, Mapping[str, tuple[bool, bool]]] = {
    "rg": {"-g": (True, False), "--glob": (True, False), "--iglob": (True, True)},
    "grep": {"--exclude-dir": (False, False)},
}

# Wrappers that only change how the command after them runs (Claude Code's
# list); the inner command is analyzed instead of the wrapper.
TRANSPARENT_WRAPPERS: Mapping[str, Mapping[str, bool]] = {
    # option -> whether it takes a value
    "timeout": {"-s": True, "--signal": True, "-k": True, "--kill-after": True},
    "time": {"-f": True, "--format": True, "-o": True, "--output": True},
    "nice": {"-n": True, "--adjustment": True},
    "nohup": {},
    "stdbuf": {
        "-i": True, "-o": True, "-e": True, "--input": True, "--output": True, "--error": True,
    },
    "command": {},
    "builtin": {},
    "exec": {"-a": True},
    "ionice": {"-c": True, "-n": True, "-p": True, "--class": True, "--classdata": True},
    "env": {
        "-u": True, "--unset": True, "-C": True, "--chdir": True, "-S": True,
        "--split-string": True,
    },
    "xargs": {
        "-I": True, "-n": True, "-L": True, "-P": True, "-s": True, "-d": True, "-E": True,
        "-a": True, "--max-args": True, "--max-lines": True, "--max-procs": True,
        "--delimiter": True, "--arg-file": True, "--replace": True, "--eof": True,
    },
    "call": {},
}  # fmt: skip
# Wrappers that run the command after them with other rights: both the wrapper
# and the inner command are commands of the plan.
ELEVATING_WRAPPERS: Mapping[str, Mapping[str, bool]] = {
    "sudo": {
        "-u": True,
        "-g": True,
        "-C": True,
        "-D": True,
        "-h": True,
        "-p": True,
        "-U": True,
        "-r": True,
        "-t": True,
    },
    "doas": {"-u": True, "-C": True},
}


@dataclass(frozen=True, slots=True)
class RunnerSpec:
    """A program that runs the command after its subcommand (``uv run``, ``pipx run``).

    ``subcommands``: the words that make it run a command (none: it always
    does, ``uvx``); options may stand before and after them. ``values``:
    options that take a value; ``directories``: options whose value is the
    folder the command runs in. ``environment_prefix``: the command may start
    with ``<environment>:`` (``hatch run test:pytest``).
    """

    subcommands: tuple[tuple[str, ...], ...] = ()
    values: frozenset[str] = frozenset()
    directories: frozenset[str] = frozenset()
    environment_prefix: bool = False


_UV_VALUES = frozenset(
    {
        "--directory", "--project", "--with", "--with-editable", "--with-requirements",
        "--python", "-p", "--package", "--extra", "--group", "--only-group", "--no-group",
        "--env-file", "--index", "--index-url", "--default-index", "--extra-index-url",
        "--find-links", "-f", "--config-file", "--cache-dir", "--color", "--from", "--script",
        "--python-preference", "--index-strategy", "--keyring-provider", "--resolution",
        "--prerelease", "--exclude-newer", "--link-mode", "--refresh-package",
        "--reinstall-package", "--upgrade-package", "-P",
    }
)  # fmt: skip
_CONDA_VALUES = frozenset({"-n", "--name", "-p", "--prefix", "--cwd"})

# Runners: both the runner and the command it runs are commands of the plan.
RUNNERS: Mapping[str, RunnerSpec] = {
    "uv": RunnerSpec(
        subcommands=(("run",), ("tool", "run")),
        values=_UV_VALUES,
        directories=frozenset({"--directory"}),
    ),
    "uvx": RunnerSpec(values=_UV_VALUES, directories=frozenset({"--directory"})),
    "pipx": RunnerSpec(
        subcommands=(("run",),),
        values=frozenset({"--spec", "--python", "--pip-args", "--index-url"}),
    ),
    "poetry": RunnerSpec(
        subcommands=(("run",),),
        values=frozenset({"--directory", "-C", "--project", "-P"}),
        directories=frozenset({"--directory", "-C"}),
    ),
    "pdm": RunnerSpec(subcommands=(("run",),), values=frozenset({"-p", "--project"})),
    "hatch": RunnerSpec(
        subcommands=(("run",),),
        values=frozenset({"-e", "--env", "--data-dir", "--cache-dir", "--config"}),
        environment_prefix=True,
    ),
    "pixi": RunnerSpec(
        subcommands=(("run",),),
        values=frozenset({"-e", "--environment", "--manifest-path"}),
    ),
    "rye": RunnerSpec(subcommands=(("run",),), values=frozenset({"--pyproject"})),
    "conda": RunnerSpec(
        subcommands=(("run",),), values=_CONDA_VALUES, directories=frozenset({"--cwd"})
    ),
    "mamba": RunnerSpec(
        subcommands=(("run",),), values=_CONDA_VALUES, directories=frozenset({"--cwd"})
    ),
    "micromamba": RunnerSpec(
        subcommands=(("run",),), values=_CONDA_VALUES, directories=frozenset({"--cwd"})
    ),
}


@dataclass(frozen=True, slots=True)
class GlobalOptions:
    """Options a program takes before its subcommand (``git -C dir push``).

    ``values`` take a value (next word or ``=``; attached for short options
    in ``attached``); every other leading ``-x`` is a flag only when listed
    in ``flags``, so an unknown option ends the stripping. ``directories``
    run the subcommand in their folder, ``writes`` and ``reads`` are paths.
    """

    values: frozenset[str] = frozenset()
    flags: frozenset[str] = frozenset()
    attached: frozenset[str] = frozenset()
    directories: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    reads: frozenset[str] = frozenset()


# Their global options are removed before Shell(...) patterns match, as wrappers are.
GLOBAL_OPTIONS: Mapping[str, GlobalOptions] = {
    "git": GlobalOptions(
        values=frozenset(
            {
                "-C",
                "-c",
                "--git-dir",
                "--work-tree",
                "--namespace",
                "--super-prefix",
                "--config-env",
                "--exec-path",
                "--list-cmds",
                "--attr-source",
            }
        ),
        flags=frozenset(
            {
                "-p",
                "-P",
                "--paginate",
                "--no-pager",
                "--no-replace-objects",
                "--bare",
                "--literal-pathspecs",
                "--glob-pathspecs",
                "--noglob-pathspecs",
                "--icase-pathspecs",
                "--no-optional-locks",
                "--no-advice",
                "--html-path",
                "--man-path",
                "--info-path",
                "--version",
                "--help",
            }
        ),
        attached=frozenset({"-C", "-c"}),
        directories=frozenset({"-C"}),
        writes=frozenset({"--git-dir", "--work-tree"}),
    ),  # fmt: skip
    "docker": GlobalOptions(
        values=frozenset(
            {
                "--config",
                "-c",
                "--context",
                "-H",
                "--host",
                "-l",
                "--log-level",
                "--tlscacert",
                "--tlscert",
                "--tlskey",
            }
        ),
        flags=frozenset({"-D", "--debug", "--tls", "--tlsverify"}),
        reads=frozenset({"--config", "--tlscacert", "--tlscert", "--tlskey"}),
    ),  # fmt: skip
    "kubectl": GlobalOptions(
        values=frozenset(
            {
                "--context",
                "-n",
                "--namespace",
                "--kubeconfig",
                "-s",
                "--server",
                "--cluster",
                "--user",
                "--token",
                "--as",
                "--as-group",
                "--request-timeout",
                "-v",
                "--cache-dir",
                "--certificate-authority",
                "--client-certificate",
                "--client-key",
            }
        ),
        flags=frozenset({"--insecure-skip-tls-verify"}),
        reads=frozenset(
            {"--kubeconfig", "--certificate-authority", "--client-certificate", "--client-key"}
        ),
    ),  # fmt: skip
    "npm": GlobalOptions(
        values=frozenset({"--prefix", "-w", "--workspace", "--userconfig", "--cache"}),
        flags=frozenset(
            {"-g", "--global", "-s", "--silent", "-q", "--quiet", "--ws", "--workspaces", "-ws"}
        ),
        writes=frozenset({"--prefix"}),
        reads=frozenset({"--userconfig"}),
    ),  # fmt: skip
    "cargo": GlobalOptions(
        values=frozenset({"-C", "--config", "-Z", "--color"}),
        flags=frozenset(
            {"-v", "-vv", "-q", "--quiet", "--verbose", "--frozen", "--locked", "--offline"}
        ),
        directories=frozenset({"-C"}),
    ),  # fmt: skip
    "dotnet": GlobalOptions(flags=frozenset({"-d", "--diagnostics"})),
}

# Options whose value is a path the program writes, for programs without a spec.
# Only where the option means that for every subcommand.
OUTPUT_OPTIONS: Mapping[str, frozenset[str]] = {
    "pip": frozenset(
        {"--target", "-t", "--prefix", "--root", "-d", "--dest", "-w", "--wheel-dir", "--src",
         "--log", "--report"}
    ),
    "dotnet": frozenset({"-o", "--output"}),
    "npm": frozenset({"--prefix"}),
    "pnpm": frozenset({"-C", "--dir"}),
    "yarn": frozenset({"--cwd"}),
    "cargo": frozenset({"--target-dir", "--artifact-dir", "--out-dir"}),
    "go": frozenset({"-o"}),
    "gcc": frozenset({"-o"}),
    "g++": frozenset({"-o"}),
    "cc": frozenset({"-o"}),
    "clang": frozenset({"-o"}),
    "clang++": frozenset({"-o"}),
    "rustc": frozenset({"-o", "--out-dir"}),
    "javac": frozenset({"-d"}),
    "tsc": frozenset({"--outDir", "--outFile", "--declarationDir"}),
    "esbuild": frozenset({"--outfile", "--outdir"}),
    "webpack": frozenset({"-o", "--output-path"}),
    "pandoc": frozenset({"-o", "--output"}),
    "pyinstaller": frozenset({"--distpath", "--workpath", "--specpath"}),
    "mkdocs": frozenset({"-d", "--site-dir"}),
    "hugo": frozenset({"-d", "--destination"}),
    "7z": frozenset({"-o"}),
    "uv": frozenset({"--target", "--prefix", "-o", "--output-file"}),
}  # fmt: skip
# Of those, the short options that also take their value attached (``-o<dir>``).
ATTACHED_OUTPUT_OPTIONS = frozenset({"-o", "-d", "-t", "-w", "-C"})
# Python modules that create the folders their operands name.
VENV_MODULES = frozenset({"venv", "virtualenv"})

# cmd's internal commands; cmd ends their name at the first of . \ / : = ; , + (
# (``cd..`` is ``cd ..``).
CMD_INTERNALS = frozenset(
    {
        "assoc", "break", "call", "cd", "chdir", "cls", "color", "copy", "date", "del", "dir",
        "echo", "endlocal", "erase", "exit", "for", "ftype", "goto", "if", "md", "mkdir",
        "mklink", "move", "path", "pause", "popd", "prompt", "pushd", "rd", "rem", "ren",
        "rename", "rmdir", "set", "setlocal", "shift", "start", "time", "title", "type", "ver",
        "verify", "vol",
    }
)  # fmt: skip

POSIX_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ksh", "ash", "mksh"})
POWERSHELLS = frozenset({"pwsh", "powershell", "pwsh-preview"})

# PowerShell's default aliases (``Get-Alias`` in a pwsh 7 session without
# profile), for command patterns: ``Shell(rm *)`` also names ``Remove-Item``.
# Windows only for the names that are programs on Linux and macOS.
_POSIX_PROGRAM_ALIASES = frozenset(
    {
        "cat",
        "clear",
        "cp",
        "diff",
        "kill",
        "ls",
        "man",
        "mount",
        "mv",
        "ps",
        "rm",
        "rmdir",
        "sleep",
        "sort",
        "tee",
        "write",
    }
)
_ALIASES = {
    "?": "Where-Object", "%": "ForEach-Object", "ac": "Add-Content", "cat": "Get-Content",
    "cd": "Set-Location", "chdir": "Set-Location", "clc": "Clear-Content", "clear": "Clear-Host",
    "clhy": "Clear-History", "cli": "Clear-Item", "clp": "Clear-ItemProperty", "cls": "Clear-Host",
    "clv": "Clear-Variable", "cnsn": "Connect-PSSession", "compare": "Compare-Object",
    "copy": "Copy-Item", "cp": "Copy-Item", "cpi": "Copy-Item", "cpp": "Copy-ItemProperty",
    "cvpa": "Convert-Path", "dbp": "Disable-PSBreakpoint", "del": "Remove-Item",
    "diff": "Compare-Object", "dir": "Get-ChildItem", "dnsn": "Disconnect-PSSession",
    "ebp": "Enable-PSBreakpoint", "echo": "Write-Output", "epal": "Export-Alias",
    "epcsv": "Export-Csv", "erase": "Remove-Item", "etsn": "Enter-PSSession",
    "exsn": "Exit-PSSession", "fc": "Format-Custom", "fhx": "Format-Hex", "fl": "Format-List",
    "foreach": "ForEach-Object", "ft": "Format-Table", "fw": "Format-Wide", "gal": "Get-Alias",
    "gbp": "Get-PSBreakpoint", "gc": "Get-Content", "gcb": "Get-Clipboard", "gci": "Get-ChildItem",
    "gcm": "Get-Command", "gcs": "Get-PSCallStack", "gdr": "Get-PSDrive", "gerr": "Get-Error",
    "ghy": "Get-History", "gi": "Get-Item", "gin": "Get-ComputerInfo", "gjb": "Get-Job",
    "gl": "Get-Location", "gm": "Get-Member", "gmo": "Get-Module", "gp": "Get-ItemProperty",
    "gps": "Get-Process", "gpv": "Get-ItemPropertyValue", "group": "Group-Object",
    "gsn": "Get-PSSession", "gsv": "Get-Service", "gtz": "Get-TimeZone", "gu": "Get-Unique",
    "gv": "Get-Variable", "h": "Get-History", "history": "Get-History", "icm": "Invoke-Command",
    "iex": "Invoke-Expression", "ihy": "Invoke-History", "ii": "Invoke-Item",
    "ipal": "Import-Alias",
    "ipcsv": "Import-Csv", "ipmo": "Import-Module", "irm": "Invoke-RestMethod",
    "iwr": "Invoke-WebRequest", "kill": "Stop-Process", "ls": "Get-ChildItem", "man": "help",
    "md": "mkdir", "measure": "Measure-Object", "mi": "Move-Item", "mount": "New-PSDrive",
    "move": "Move-Item", "mp": "Move-ItemProperty", "mv": "Move-Item", "nal": "New-Alias",
    "ndr": "New-PSDrive", "ni": "New-Item", "nmo": "New-Module", "nsn": "New-PSSession",
    "nv": "New-Variable", "ogv": "Out-GridView", "oh": "Out-Host", "popd": "Pop-Location",
    "ps": "Get-Process", "pushd": "Push-Location", "pwd": "Get-Location", "r": "Invoke-History",
    "rbp": "Remove-PSBreakpoint", "rcjb": "Receive-Job", "rcsn": "Receive-PSSession",
    "rd": "Remove-Item", "rdr": "Remove-PSDrive", "ren": "Rename-Item", "ri": "Remove-Item",
    "rjb": "Remove-Job", "rm": "Remove-Item", "rmdir": "Remove-Item", "rmo": "Remove-Module",
    "rni": "Rename-Item", "rnp": "Rename-ItemProperty", "rp": "Remove-ItemProperty",
    "rsn": "Remove-PSSession", "rv": "Remove-Variable", "rvpa": "Resolve-Path",
    "sajb": "Start-Job", "sal": "Set-Alias", "saps": "Start-Process", "sasv": "Start-Service",
    "sbp": "Set-PSBreakpoint", "scb": "Set-Clipboard", "select": "Select-Object",
    "set": "Set-Variable", "shcm": "Show-Command", "si": "Set-Item", "sl": "Set-Location",
    "sleep": "Start-Sleep", "sls": "Select-String", "sort": "Sort-Object", "sp": "Set-ItemProperty",
    "spjb": "Stop-Job", "spps": "Stop-Process", "spsv": "Stop-Service", "start": "Start-Process",
    "stz": "Set-TimeZone", "sv": "Set-Variable", "tee": "Tee-Object", "type": "Get-Content",
    "where": "Where-Object", "wjb": "Wait-Job", "write": "Write-Output",
}  # fmt: skip
POWERSHELL_ALIASES: Mapping[str, str] = (
    _ALIASES
    if sys.platform == "win32"
    else {name: target for name, target in _ALIASES.items() if name not in _POSIX_PROGRAM_ALIASES}
)
