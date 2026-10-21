"""A fake command analyzer for Tools that check commands against Permission Rules.

Tools get a real ``CommandChecker`` over ``FakeAnalyzer``, which returns the
plan a test sets instead of parsing, so a Tool test decides what a command line
runs and touches without PowerShell or tree-sitter.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from core.permissions import (
    BoundaryContext,
    CommandChecker,
    PermissionBoundary,
    normalize_permission_rules,
)
from core.permissions.commands.commands import (
    CommandAnalysisUnavailable,
    CommandPlan,
    CommandWord,
    PlannedPathAccess,
    SimpleCommand,
    UnresolvedElement,
)

# The vBot server port of ``restricted`` boundaries.
PORT = 22333


def restricted(workdir: Path, **rules: object) -> PermissionBoundary:
    """A boundary whose Work Area is *workdir*, with vBot's data folder beside it."""
    return PermissionBoundary.build(
        normalize_permission_rules({"read": "work_area", "write": "work_area", **rules}),
        BoundaryContext(
            workdir=workdir,
            home=workdir / "home",
            data_dir=workdir.parent / f"{workdir.name}-vbot-data",
            loopback_port=PORT,
        ),
    )


def plan(
    *commands: str,
    reads: Sequence[Path] = (),
    writes: Sequence[Path] = (),
    subtree_reads: Sequence[Path] = (),
    subtree_writes: Sequence[Path] = (),
    urls: Sequence[str] = (),
    hosts: Sequence[str] = (),
    unresolved: Sequence[UnresolvedElement] = (),
) -> CommandPlan:
    """A plan of the simple *commands* (``"git push origin"``), with static words."""
    simple = tuple(
        SimpleCommand(
            text.split()[0],
            tuple(CommandWord(word, True) for word in text.split()[1:]),
            text,
            "bash",
        )
        for text in commands
    )
    paths = (
        *(PlannedPathAccess("read", path, str(path)) for path in reads),
        *(PlannedPathAccess("write", path, str(path)) for path in writes),
        *(PlannedPathAccess("read", path, str(path), subtree=True) for path in subtree_reads),
        *(PlannedPathAccess("write", path, str(path), subtree=True) for path in subtree_writes),
    )
    return CommandPlan(simple, paths, tuple(unresolved), tuple(urls), tuple(hosts))


class FakeAnalyzer:
    """Answers every analysis with ``result``; records what it was asked."""

    def __init__(self, result: CommandPlan | None = None) -> None:
        self.result: CommandPlan | Exception = result or plan()
        self.lines: list[tuple[str, str, Path, Mapping[str, str]]] = []
        self.argvs: list[tuple[tuple[str, ...], Path]] = []
        self.closed = False

    async def analyze(
        self,
        command_line: str,
        *,
        shell: str,
        cwd: Path,
        env: Mapping[str, str],
        home: Path,
    ) -> CommandPlan:
        self.lines.append((command_line, shell, cwd, dict(env)))
        return self._answer()

    async def analyze_argv(
        self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], home: Path
    ) -> CommandPlan:
        self.argvs.append((tuple(argv), cwd))
        return self._answer()

    async def aclose(self) -> None:
        self.closed = True

    def _answer(self) -> CommandPlan:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def fake_checker(analyzer: FakeAnalyzer, *, vbot_root: Path | None = None) -> CommandChecker:
    """A real ``CommandChecker`` over *analyzer*."""
    if vbot_root is None:
        return CommandChecker(lambda: analyzer)  # type: ignore[arg-type, return-value]
    return CommandChecker(lambda: analyzer, vbot_root=vbot_root)  # type: ignore[arg-type, return-value]


UNAVAILABLE = CommandAnalysisUnavailable("the PowerShell helper did not answer")

__all__ = ["UNAVAILABLE", "FakeAnalyzer", "fake_checker", "plan"]
