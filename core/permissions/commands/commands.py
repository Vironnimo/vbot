"""Command-line analysis for Permission Rules: what a shell command line runs and touches.

``CommandAnalyzer.analyze`` turns one command line of PowerShell, bash or cmd
into a ``CommandPlan``: the simple commands it runs (canonical names, words
marked static or dynamic), the paths it reads, writes or changes into
(absolute, lexically normalized against the working directory it tracks), the
http(s) URLs and other hosts it reaches, and what it could not resolve. It never runs the
command and touches the filesystem not at all; the Permission Boundary decides
about the plan (link resolution included). Contract and limits:
``.vorch/domain-maps/permissions/commands.md``.

PowerShell is parsed by PowerShell itself in one long-lived helper process
(``_powershell.py``); bash by tree-sitter in process; cmd by a small tokenizer.
Imports stay cheap: tree-sitter loads on the first bash analysis, the helper
starts on the first PowerShell analysis. An analysis runs on the bounded
``command-analysis`` worker pool, so a long command line does not block the
Event Loop; only the helper round trip runs on the loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from core.permissions.commands import _patterns
from core.permissions.commands._powershell import (
    HelperLauncher,
    HelperLimits,
    HelperUnavailable,
    PowerShellHelper,
    process_launcher,
)
from core.permissions.commands._specs import POSIX_SHELLS, POWERSHELLS
from core.utils.errors import VBotError

if TYPE_CHECKING:
    from core.permissions.commands._analysis import Analysis
    from core.utils.workers import BoundedWorkerPool

type ShellKind = Literal["powershell", "bash", "cmd"]
type PathAccessKind = Literal["read", "write", "start_dir"]
type UnresolvedKind = Literal["path_read", "path_write", "cwd", "command", "url"]

PowerShellHelperLimits = HelperLimits

# Longer command lines are not analyzed (one unresolved ``command``).
MAX_COMMAND_LINE_CHARS = 1_000_000
# Nested shells (pwsh -Command, bash -c, cmd /c, iex, eval) unwrapped at most.
MAX_NESTING_DEPTH = 8


@dataclass(frozen=True, slots=True)
class CommandWord:
    """One argument word. ``static`` False: it holds an expansion this analysis could not resolve.

    ``text`` is the resolved value of a static word and the source text of a
    dynamic one.
    """

    text: str
    static: bool


@dataclass(frozen=True, slots=True)
class SimpleCommand:
    """One command the line runs, at any depth (pipelines, subexpressions, nested shells).

    ``name`` is canonical: a PowerShell alias resolved to its cmdlet, a program
    without directory and ``.exe``/``.cmd``/``.bat``/``.com``, wrappers such
    as ``env``, ``timeout`` or ``nohup`` removed. ``words`` are its arguments
    after the name. ``source`` is the original text of the command. ``shell``
    is the language it was written in (PowerShell names compare
    case-insensitively and through their aliases).
    """

    name: str
    words: tuple[CommandWord, ...]
    source: str
    shell: ShellKind


@dataclass(frozen=True, slots=True)
class PlannedPathAccess:
    """A path the command line reads, writes or changes into (``start_dir``).

    ``path`` is absolute and lexically normalized against the tracked working
    directory; links are not resolved. ``glob``: the access reaches the
    entries below ``path`` that this relative pattern names (``*.md`` of
    ``docs/*.md``, ``/`` separators, ``*``, ``?`` and ``**`` as in rules).
    ``subtree``: it may also reach anything below those entries, or below
    ``path`` without ``glob`` (a recursive operation, a pipeline whose targets
    a listing command names). A deny rule that can match what it reaches
    applies too, except below folders named in ``pruned``: names the command
    itself leaves out at every depth (``rg -g '!secret'``), recorded only
    where that exclusion is exact. ``source`` is the text it came from.
    """

    kind: PathAccessKind
    path: Path
    source: str
    subtree: bool = False
    glob: str | None = None
    pruned: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UnresolvedElement:
    """Something the analysis could not resolve, and why.

    ``path_read``/``path_write``: a read or write whose path is computed when
    the command runs or cannot be placed (a variable the script assigns, a
    relative path after a ``cd`` to such a path). ``cwd``: a directory change
    to an unknown folder. ``command``: a command this analysis cannot see
    into (a computed command name, ``Invoke-Expression`` of a computed
    string, ``--%``, a command line that does not parse). ``url``: a web or
    network address computed when it runs (``Invoke-WebRequest $url``).
    """

    kind: UnresolvedKind
    source: str
    reason: str


@dataclass(frozen=True, slots=True)
class CommandPlan:
    """What a command line runs and touches.

    ``urls`` are the http(s) URLs its words name (a web program's target
    without scheme, ``curl example.com``, as ``http://``); ``hosts`` the other
    hosts it connects to (``ssh host``, ``scp host:x .``). ``mentions`` are the
    other static words of programs and cmdlets without a path spec, each
    placed as a ``read`` of the path it would name (``awk 1 secrets/key.pem``):
    whether the program reads them is unknown, so only deny rules and
    protected locations apply to them, never a base.
    """

    commands: tuple[SimpleCommand, ...]
    paths: tuple[PlannedPathAccess, ...]
    unresolved: tuple[UnresolvedElement, ...]
    urls: tuple[str, ...]
    hosts: tuple[str, ...] = ()
    mentions: tuple[PlannedPathAccess, ...] = ()


class CommandAnalysisUnavailable(VBotError):  # noqa: N818 - names the outcome
    """The analysis could not run (the PowerShell helper failed or timed out, or the
    bash parser could not be loaded); fail closed."""


class CommandAnalyzer:
    """Analyzes command lines; owns the PowerShell helper. One per server.

    ``powershell_executable`` is the ``pwsh``/``powershell`` the shell Tool
    runs (``core.tools.shell.powershell_executable``); None where there is
    none, which makes PowerShell analysis unavailable. ``helper_launcher``
    replaces the helper process (tests).
    """

    def __init__(
        self,
        *,
        powershell_executable: Path | None,
        limits: HelperLimits | None = None,
        helper_launcher: HelperLauncher | None = None,
    ) -> None:
        launcher = helper_launcher
        if launcher is None and powershell_executable is not None:
            launcher = process_launcher(powershell_executable)
        self._helper = (
            None if launcher is None else PowerShellHelper(launcher, limits or HelperLimits())
        )

    async def analyze(
        self,
        command_line: str,
        *,
        shell: ShellKind,
        cwd: Path,
        env: Mapping[str, str],
        home: Path,
    ) -> CommandPlan:
        """The plan of *command_line* run by *shell* in *cwd* with *env*.

        Raises ``CommandAnalysisUnavailable`` when PowerShell must be parsed
        and the helper cannot answer. A terminal line is analyzed the same way,
        with the terminal's shell and its reported working directory.
        """
        if shell == "powershell" and self._helper is None:
            raise CommandAnalysisUnavailable("PowerShell is not available to analyze the command")
        return await self._run(
            env, home, lambda analysis: analysis.run(command_line, shell, (cwd,), depth=0)
        )

    async def analyze_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        home: Path,
    ) -> CommandPlan:
        """The plan of starting the program *argv* directly in *cwd*, without a shell.

        For a terminal's program: its words are literal, and a shell program
        with a command (``pwsh -Command ...``) is analyzed like any nested shell.
        """
        arguments = tuple(argv)
        return await self._run(env, home, lambda analysis: analysis.run_argv(arguments, cwd))

    async def _run(
        self, env: Mapping[str, str], home: Path, work: Callable[[Analysis], None]
    ) -> CommandPlan:
        """Run *work* on a fresh ``Analysis`` in the worker pool.

        Its PowerShell parses go back to this Event Loop, where the helper lives.
        """
        from core.permissions.commands._analysis import Analysis

        loop = asyncio.get_running_loop()
        helper = self._helper
        parse: Callable[[str], dict[str, Any]] | None = None
        if helper is not None:

            def parse(text: str) -> dict[str, Any]:
                future = asyncio.run_coroutine_threadsafe(helper.analyze(text), loop)
                try:
                    return future.result()
                except concurrent.futures.CancelledError as error:
                    raise HelperUnavailable("the command analysis was stopped") from error

        def analyze() -> CommandPlan:
            analysis = Analysis(env=env, home=home, powershell=parse)
            work(analysis)
            return analysis.plan()

        try:
            return await _analysis_pool().run(analyze)
        except HelperUnavailable as error:
            raise CommandAnalysisUnavailable(str(error)) from error

    async def aclose(self) -> None:
        """Stop the helper; later PowerShell analyses raise ``CommandAnalysisUnavailable``."""
        if self._helper is not None:
            await self._helper.aclose()


_POOL: BoundedWorkerPool | None = None


def _analysis_pool() -> BoundedWorkerPool:
    """The pool every analysis runs on, created on first use (imports stay cheap)."""
    global _POOL
    if _POOL is None:
        from core.utils.workers import BoundedWorkerPool

        # Analyses are CPU-bound Python; two let a short line pass a long one.
        _POOL = BoundedWorkerPool(name="command-analysis", max_workers=2)
    return _POOL


def shell_kind(program: str) -> ShellKind | None:
    """The analysis language of a shell program (``pwsh.exe`` -> ``powershell``), else None.

    For the terminal: a line typed at the prompt of such a shell can be
    analyzed with ``analyze``; zsh and other POSIX shells are read as bash.
    """
    from core.permissions.commands._paths import program_name

    name = program_name(program).lower()
    if name in POWERSHELLS:
        return "powershell"
    if name == "cmd":
        return "cmd"
    if name in POSIX_SHELLS:
        return "bash"
    return None


def normalize_command_pattern(pattern: str) -> str:
    """The canonical form of a ``Shell(...)`` pattern; raises ``ValueError(reason)``."""
    return _patterns.normalize(pattern)


def command_pattern_matches(
    pattern: str, command: SimpleCommand, *, case_insensitive: bool
) -> bool:
    """Whether *pattern* matches *command* (Claude Code semantics; for allow rules).

    The command is its canonical name and its words joined by single spaces;
    ``*`` matches any text, and a trailing `` *`` also matches the bare
    command. A dynamic word matches only a ``*``. PowerShell commands compare
    case-insensitively, and the pattern's first word through PowerShell's
    aliases.
    """
    return _patterns.matches(
        pattern, command, case_insensitive=case_insensitive, dynamic_matches_any=False
    )


def command_pattern_may_match(
    pattern: str, command: SimpleCommand, *, case_insensitive: bool
) -> bool:
    """Whether *pattern* matches *command* for some value of its dynamic words (for deny rules).

    A deny rule refuses a command it could match through a dynamic word
    (``rm $X`` for ``Shell(rm -rf *)``... only when the static rest fits).
    Equals ``command_pattern_matches`` for a command without dynamic words.
    """
    return _patterns.matches(
        pattern, command, case_insensitive=case_insensitive, dynamic_matches_any=True
    )
