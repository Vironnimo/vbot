"""Check shell command lines against a Permission Boundary before they run.

One ``CommandChecker`` per Runtime serves every shell Tool call, terminal
start and terminal line, and Cron watch command of a restricted Session or job.
It owns the ``CommandAnalyzer``, creates it on the first check of a restricted
boundary and closes it at shutdown; full access never creates it. The decision
order: the start directory, then every simple command against deny rules and
``commands: listed`` allow rules, vBot's own CLI, the URLs and host names that
reach the vBot server, every URL and host, every path access, the mentions
(words of commands without a path spec, against deny rules and protected
locations only), and last whatever the analysis could not resolve. The
analysis and the decision run on bounded worker pools (the decision resolves
links on disk), never on the Event Loop.
Contract: ``.vorch/domain-maps/permissions.md``.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

from core.permissions._addresses import canonical_host
from core.permissions._approval_access import ApprovalRequirement
from core.permissions._paths import is_inside, path_key, split_lexical
from core.permissions._refusal import PathAccess, PermissionRefusal
from core.permissions.permissions import PermissionBoundary

if TYPE_CHECKING:
    from core.permissions.commands.commands import (
        CommandAnalyzer,
        CommandPlan,
        ShellKind,
        SimpleCommand,
        UnresolvedElement,
    )
    from core.utils.workers import BoundedWorkerPool

# Refuses a simple command a caller cannot let run: ``(detail, hint)`` or None.
type CommandRefusal = Callable[[SimpleCommand], tuple[str, str] | None]

# Longest command text a refusal quotes.
_TARGET_CHARS: Final = 300
_PYTHONS: Final = re.compile(r"^(?:python[\d.]*w?|py|pythonw|uv)$", re.IGNORECASE)
_VBOT_MODULE: Final = re.compile(r"^(?:cli|server)(?:\.|$)")
# ``host:port`` inside a word, as in ``localhost:8765/api`` or ``ws://[::1]:8765``.
_HOST_PORT: Final = re.compile(r"(\[[0-9A-Za-z:.%]+\]|[A-Za-z0-9._-]+):(\d{1,5})(?!\d)")
_UNRESOLVED: Final[dict[str, tuple[str, str]]] = {
    "path_read": (
        "so the check cannot tell what it reads",
        "Write each path literally in the command.",
    ),
    "path_write": (
        "so the check cannot tell what it changes",
        "Write each path literally in the command.",
    ),
    "cwd": (
        "so the check cannot tell which folder it works in",
        "Change into folders by their literal path.",
    ),
    "command": (
        "so the check cannot tell what it runs",
        "Write each command and its arguments literally.",
    ),
    "url": (
        "so the check cannot tell which address it reaches",
        "Write each address literally in the command.",
    ),
}
# For callers whose Tool takes the folder to run in (the shell Tool).
_CWD_HINT_WITH_WORKDIR: Final = (
    "Change into folders by their literal path, or pass the folder as workdir."
)
_UNAVAILABLE_HINT: Final = "Run it again; if this repeats, tell the user."
# A recursive read over a denied folder: what reads the same files and works.
_DENIED_BELOW_HINT: Final = (
    "Use search_files, which leaves denied folders out, or name only the folders the command needs."
)


@dataclass(frozen=True, slots=True)
class CommandDecision:
    """Refused accesses paired with their exact approval identity, plus path freshness."""

    refused: tuple[tuple[PermissionRefusal, ApprovalRequirement | None], ...] = ()
    path_identities: tuple[ApprovalRequirement, ...] = ()

    @property
    def refusals(self) -> tuple[PermissionRefusal, ...]:
        return tuple(refusal for refusal, _ in self.refused)


class CommandChecker:
    """Decides whether a restricted Session's command line may run; one per Runtime.

    ``analyzer_factory`` creates the ``CommandAnalyzer`` on the first check of
    a restricted boundary. ``vbot_root`` is vBot's checkout or installation,
    whose CLI and server modules a restricted command cannot start.
    """

    def __init__(
        self,
        analyzer_factory: Callable[[], CommandAnalyzer],
        *,
        vbot_root: Path | None = None,
    ) -> None:
        if vbot_root is None:
            from core.utils.config import VBOT_ROOT

            vbot_root = VBOT_ROOT
        self._factory = analyzer_factory
        self._analyzer: CommandAnalyzer | None = None
        self._closed = False
        self._lock = asyncio.Lock()
        self._vbot_root = vbot_root

    @property
    def analyzer_started(self) -> bool:
        """Whether a check created the analyzer (never for full access)."""
        return self._analyzer is not None

    async def check_line(
        self,
        boundary: PermissionBoundary,
        command_line: str,
        *,
        shell: ShellKind,
        cwd: Path,
        env: Mapping[str, str],
        home: Path,
        refuse_command: CommandRefusal | None = None,
        workdir_parameter: bool = False,
    ) -> PermissionRefusal | None:
        """The refusal of running *command_line* with *shell* in *cwd*, or None to run it.

        *refuse_command* refuses more: given a simple command, it returns the
        ``(detail, hint)`` of an ``unanalyzable`` refusal, or None.
        *workdir_parameter*: the caller's Tool takes the folder to run in as
        ``workdir``, which a refusal of an unknown folder then suggests.
        """
        if not boundary.restricted:
            return None
        plan = await self._analyze(
            boundary,
            command_line,
            lambda analyzer: analyzer.analyze(
                command_line, shell=shell, cwd=cwd, env=env, home=home
            ),
        )
        if isinstance(plan, PermissionRefusal):
            return plan
        refused = await self._decision(boundary, plan, cwd, command_line, workdir_parameter)
        if (refused is None or refused.reason == "not_permitted") and refuse_command is not None:
            for command in plan.commands:
                reason = refuse_command(command)
                if reason is not None:
                    detail, hint = reason
                    return boundary.refusal(
                        "command", _shown(command.source), "unanalyzable", detail=detail, hint=hint
                    )
        return refused

    async def check_argv(
        self,
        boundary: PermissionBoundary,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        home: Path,
    ) -> PermissionRefusal | None:
        """The refusal of starting the program *argv* directly in *cwd*, or None."""
        if not boundary.restricted:
            return None
        text = " ".join(argv)
        plan = await self._analyze(
            boundary,
            text,
            lambda analyzer: analyzer.analyze_argv(argv, cwd=cwd, env=env, home=home),
        )
        if isinstance(plan, PermissionRefusal):
            return plan
        return await self._decision(boundary, plan, cwd, text, False)

    async def aclose(self) -> None:
        """Stop the analyzer; later checks of restricted boundaries refuse."""
        async with self._lock:
            self._closed = True
            analyzer, self._analyzer = self._analyzer, None
        if analyzer is not None:
            await analyzer.aclose()

    async def _analyze(
        self,
        boundary: PermissionBoundary,
        text: str,
        run: Callable[[CommandAnalyzer], Awaitable[CommandPlan]],
    ) -> CommandPlan | PermissionRefusal:
        from core.permissions.commands.commands import CommandAnalysisUnavailable

        try:
            analyzer = await self._analyzer_instance()
            plan = await run(analyzer)
        except CommandAnalysisUnavailable as error:
            refusal = boundary.refusal(
                "command",
                _shown(text),
                "unanalyzable",
                detail=f"vBot could not analyze it to check it ({str(error).rstrip('.')})",
                hint=_UNAVAILABLE_HINT,
            )
            return replace(refusal, retryable=True)
        return plan

    async def _analyzer_instance(self) -> CommandAnalyzer:
        from core.permissions.commands.commands import CommandAnalysisUnavailable

        async with self._lock:
            if self._closed:
                raise CommandAnalysisUnavailable("vBot is shutting down")
            if self._analyzer is None:
                self._analyzer = self._factory()
            return self._analyzer

    async def _decision(
        self,
        boundary: PermissionBoundary,
        plan: CommandPlan,
        cwd: Path,
        text: str,
        workdir_parameter: bool,
    ) -> PermissionRefusal | None:
        return await _decision_pool().run(
            self._decide, boundary, plan, cwd, text, workdir_parameter
        )

    def _decide(
        self,
        boundary: PermissionBoundary,
        plan: CommandPlan,
        cwd: Path,
        text: str,
        workdir_parameter: bool,
    ) -> PermissionRefusal | None:
        refusals = self._decide_all(boundary, plan, cwd, text, workdir_parameter).refusals
        return next(
            (item for item in refusals if item.reason != "not_permitted"),
            refusals[0] if refusals else None,
        )

    def _decide_all(
        self,
        boundary: PermissionBoundary,
        plan: CommandPlan,
        cwd: Path,
        text: str,
        workdir_parameter: bool,
        computer_id: str | None = None,
        shell: str = "argv",
    ) -> CommandDecision:
        """Decide each access once and prepare its exact grant when requested."""
        from core.permissions.commands.commands import command_pattern_matches

        found: list[tuple[PermissionRefusal, ApprovalRequirement | None]] = []
        identities: list[ApprovalRequirement] = []

        def add(
            refusal: PermissionRefusal | None, requirement: ApprovalRequirement | None = None
        ) -> None:
            item = (refusal, requirement)
            if refusal is not None and item not in found:
                found.append((refusal, requirement))

        root_identity = (
            ApprovalRequirement.path(boundary, cwd, "start_dir", computer_id=computer_id)
            if computer_id is not None
            else None
        )
        if root_identity is not None:
            identities.append(root_identity)
        add(boundary.check_path(cwd, "start_dir"), root_identity)
        context = boundary.context
        windows = (context.platform if context is not None else sys.platform) == "win32"
        for check in (self._deny_rules, self._vbot_cli, self._vbot_server):
            add(check(boundary, plan, cwd, windows))
        for layer in boundary.layers:
            if layer.commands == "listed":
                for command in plan.commands:
                    if not any(
                        command_pattern_matches(pattern, command, case_insensitive=windows)
                        for pattern in layer.shell_allow
                    ):
                        add(
                            boundary.refusal(
                                "command",
                                _shown(command.source),
                                "not_permitted",
                                source=layer.source,
                                base="listed",
                            ),
                            ApprovalRequirement.command(
                                text,
                                shell=shell,
                                cwd=cwd,
                                computer_id=computer_id,
                                identity=(
                                    (command.name.casefold() if windows else command.name, True),
                                    *((word.text, word.static) for word in command.words),
                                ),
                            )
                            if computer_id is not None
                            else None,
                        )
        for url in plan.urls:
            refusal = boundary.check_url(url, by_program=True)
            add(
                refusal,
                ApprovalRequirement.url(url, computer_id=computer_id)
                if computer_id is not None
                and refusal is not None
                and refusal.reason == "not_permitted"
                else None,
            )
        for host in plan.hosts:
            refusal = boundary.check_host(canonical_host(host), "connect", by_program=True)
            add(
                refusal,
                ApprovalRequirement.host(host, "connect", computer_id=computer_id)
                if computer_id is not None
                and refusal is not None
                and refusal.reason == "not_permitted"
                else None,
            )
        for access in plan.paths:
            kind: PathAccess = access.kind
            path = access.path
            if access.work_tree:
                path, resolved = _work_tree_top(boundary, path)
                if not resolved:
                    add(
                        boundary.refusal(
                            "command",
                            _shown(text),
                            "unanalyzable",
                            detail=(
                                f"the Git repository root for {access.path.as_posix()} cannot "
                                "be determined within the folders you can read"
                            ),
                            hint=(
                                "If this command creates the repository, run git init or git clone "
                                "in a separate call first, then run the remaining Git commands. "
                                "Otherwise use a repository whose root you can read."
                            ),
                        )
                    )
                    continue
            identity = (
                ApprovalRequirement.path(
                    boundary,
                    path,
                    kind,
                    computer_id=computer_id,
                    subtree=access.subtree,
                    glob=access.glob,
                    pruned=access.pruned,
                    git=access.git,
                )
                if computer_id is not None
                else None
            )
            if identity is not None:
                identities.append(identity)
            refused = boundary.check_path(
                path,
                kind,
                subtree=access.subtree,
                glob=access.glob,
                pruned=access.pruned,
                git=access.git,
            )
            if (
                refused is not None
                and refused.below is not None
                and kind == "read"
                and not access.work_tree
            ):
                refused = replace(refused, hint=_DENIED_BELOW_HINT)
            add(refused, identity)
        for mention in plan.mentions:
            add(boundary.check_path(mention.path, "read", glob=mention.glob, deny_only=True))
        for element in plan.unresolved:
            add(_unresolved_refusal(boundary, element, text, workdir_parameter))
        return CommandDecision(tuple(found), tuple(identities))

    async def prepare_line(
        self,
        boundary: PermissionBoundary,
        command_line: str,
        *,
        shell: ShellKind,
        cwd: Path,
        env: Mapping[str, str],
        home: Path,
        workdir_parameter: bool = False,
        computer_id: str = "server",
    ) -> tuple[CommandPlan | None, CommandDecision]:
        """Return the exact plan, refusals and paired grants for Tool preflight."""
        if not boundary.restricted:
            return None, CommandDecision()
        plan = await self._analyze(
            boundary,
            command_line,
            lambda analyzer: analyzer.analyze(
                command_line, shell=shell, cwd=cwd, env=env, home=home
            ),
        )
        if isinstance(plan, PermissionRefusal):
            return None, CommandDecision(((plan, None),))
        decision = await _decision_pool().run(
            self._decide_all,
            boundary,
            plan,
            cwd,
            command_line,
            workdir_parameter,
            computer_id,
            shell,
        )
        return plan, decision

    async def prepare_argv(
        self,
        boundary: PermissionBoundary,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        home: Path,
        computer_id: str = "server",
    ) -> tuple[CommandPlan | None, CommandDecision]:
        if not boundary.restricted:
            return None, CommandDecision()
        text = " ".join(argv)
        plan = await self._analyze(
            boundary,
            text,
            lambda analyzer: analyzer.analyze_argv(argv, cwd=cwd, env=env, home=home),
        )
        if isinstance(plan, PermissionRefusal):
            return None, CommandDecision(((plan, None),))
        decision = await _decision_pool().run(
            self._decide_all, boundary, plan, cwd, text, False, computer_id
        )
        return plan, decision

    async def check_line_all(
        self,
        boundary: PermissionBoundary,
        command_line: str,
        *,
        shell: ShellKind,
        cwd: Path,
        env: Mapping[str, str],
        home: Path,
        workdir_parameter: bool = False,
    ) -> tuple[PermissionRefusal, ...]:
        if not boundary.restricted:
            return ()
        plan = await self._analyze(
            boundary,
            command_line,
            lambda analyzer: analyzer.analyze(
                command_line, shell=shell, cwd=cwd, env=env, home=home
            ),
        )
        if isinstance(plan, PermissionRefusal):
            return (plan,)
        decision = await _decision_pool().run(
            self._decide_all, boundary, plan, cwd, command_line, workdir_parameter
        )
        return decision.refusals

    async def check_argv_all(
        self,
        boundary: PermissionBoundary,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        home: Path,
    ) -> tuple[PermissionRefusal, ...]:
        if not boundary.restricted:
            return ()
        text = " ".join(argv)
        plan = await self._analyze(
            boundary,
            text,
            lambda analyzer: analyzer.analyze_argv(argv, cwd=cwd, env=env, home=home),
        )
        if isinstance(plan, PermissionRefusal):
            return (plan,)
        decision = await _decision_pool().run(self._decide_all, boundary, plan, cwd, text, False)
        return decision.refusals

    def _deny_rules(
        self, boundary: PermissionBoundary, plan: CommandPlan, cwd: Path, windows: bool
    ) -> PermissionRefusal | None:
        from core.permissions.commands.commands import command_pattern_may_match

        for command in plan.commands:
            for layer in boundary.layers:
                for rule in layer.deny:
                    if rule.kind == "Shell" and command_pattern_may_match(
                        rule.pattern, command, case_insensitive=windows
                    ):
                        return boundary.refusal(
                            "command",
                            _shown(command.source),
                            "deny_rule",
                            rule=rule.text,
                            source=layer.source,
                        )
        return None

    def _vbot_cli(
        self, boundary: PermissionBoundary, plan: CommandPlan, cwd: Path, windows: bool
    ) -> PermissionRefusal | None:
        root = self._key(self._vbot_root, windows)
        scripts = (
            self._key(self._vbot_root / "cli", windows),
            self._key(self._vbot_root / "server" / "main.py", windows),
        )
        folders = [cwd, *(access.path for access in plan.paths if access.kind == "start_dir")]
        in_checkout = any(is_inside(self._key(folder, windows), root) for folder in folders)
        for command in plan.commands:
            name = command.name.casefold().removesuffix(".ps1")
            if name == "vbot" or (
                _PYTHONS.match(command.name) is not None
                and self._starts_vbot(command, folders, scripts, in_checkout, windows)
            ):
                return boundary.refusal(
                    "command", _shown(command.source), "protected_location", location="vbot_cli"
                )
            if (
                _PYTHONS.match(command.name) is not None
                and command.python_path is not None
                and not command.python_path.static
                and _vbot_module(command)
            ):
                return boundary.refusal(
                    "command",
                    _shown(command.source),
                    "unanalyzable",
                    detail="the check cannot resolve PYTHONPATH to identify the Python module",
                    hint=(
                        "Run the module without changing PYTHONPATH, "
                        "or name its script by a literal path."
                    ),
                )
        return None

    def _starts_vbot(
        self,
        command: SimpleCommand,
        folders: Sequence[Path],
        scripts: tuple[str, ...],
        in_checkout: bool,
        windows: bool,
    ) -> bool:
        root = self._key(self._vbot_root, windows)
        module_folders = list(folders) if in_checkout else []
        if command.executable and any(separator in command.executable for separator in ("/", "\\")):
            module_folders.extend(folder / command.executable for folder in folders)
        if command.python_path is not None and command.python_path.static:
            separator = ";" if windows else ":"
            module_folders.extend(
                folder / entry
                for entry in command.python_path.text.split(separator)
                for folder in folders
            )
        if _vbot_module(command) and any(
            is_inside(self._key(folder, windows), root) for folder in module_folders
        ):
            return True
        words = [word.text for word in command.words if word.static]
        for word in words:
            if word.startswith("-") or not word.lower().endswith(".py"):
                continue
            for folder in folders:
                key = self._key(folder / word, windows)
                if any(is_inside(key, script) for script in scripts):
                    return True
        return False

    def _vbot_server(
        self, boundary: PermissionBoundary, plan: CommandPlan, cwd: Path, windows: bool
    ) -> PermissionRefusal | None:
        port = boundary.loopback_port
        if port is None:
            return None
        wanted = str(port)
        for command in plan.commands:
            texts = [word.text for word in command.words if word.static]
            hosts = False
            ports = False
            for text in texts:
                for match in _HOST_PORT.finditer(text):
                    if match.group(2) == wanted and boundary.is_server_host(match.group(1)):
                        return _server_refusal(boundary, command)
                value = text.rpartition("=")[2]
                hosts = hosts or boundary.is_server_host(value)
                ports = ports or value.rpartition(":")[2] == wanted
            if hosts and ports:
                return _server_refusal(boundary, command)
        return None

    @staticmethod
    def _key(path: Path, windows: bool) -> str:
        return path_key(split_lexical(str(path), windows=windows).text, windows=windows)


def _vbot_module(command: SimpleCommand) -> bool:
    words = command.words
    for index, word in enumerate(words):
        if not word.static:
            continue
        if word.text == "-m" and index + 1 < len(words):
            module = words[index + 1]
            return module.static and _VBOT_MODULE.match(module.text) is not None
        if word.text.startswith("-m") and _VBOT_MODULE.match(word.text[2:]):
            return True
    return False


def _work_tree_top(boundary: PermissionBoundary, folder: Path) -> tuple[Path, bool]:
    """The repository's top folder and whether its lookup completed.

    The nearest folder at or above *folder* that holds ``.git`` (a folder, or a
    file in a linked worktree or submodule); *folder* itself when there is none.
    The search looks only where the boundary lets the Agent read: it ends at a
    folder it may not look into. That is an unresolved repository, not evidence
    that the command changes that ancestor; the check refuses it as unanalyzable.
    """
    current = folder
    while True:
        marker = current / ".git"
        if boundary.check_path(current, "read") is not None or (
            boundary.check_path(marker, "read") is not None
        ):
            return current, False
        if os.path.lexists(marker):
            return current, True
        if current.parent == current:
            return folder, True
        current = current.parent


def _server_refusal(boundary: PermissionBoundary, command: SimpleCommand) -> PermissionRefusal:
    return boundary.refusal(
        "command", _shown(command.source), "protected_location", location="vbot_server"
    )


def _unresolved_refusal(
    boundary: PermissionBoundary, element: UnresolvedElement, text: str, workdir_parameter: bool
) -> PermissionRefusal:
    consequence, hint = _UNRESOLVED[element.kind]
    if element.kind == "cwd" and workdir_parameter:
        hint = _CWD_HINT_WITH_WORKDIR
    reason = element.reason.rstrip(".")
    source = _shown(element.source)
    where = "" if source == _shown(text) else f" ({source})"
    return boundary.refusal(
        "command",
        _shown(text),
        "unanalyzable",
        detail=f"{reason}{where}, {consequence}",
        hint=hint,
    )


_POOL: BoundedWorkerPool | None = None


def _decision_pool() -> BoundedWorkerPool:
    """The pool decisions run on, created on first use."""
    global _POOL
    if _POOL is None:
        from core.utils.workers import BoundedWorkerPool

        _POOL = BoundedWorkerPool(name="command-check", max_workers=2)
    return _POOL


async def run_permission_decision[T](operation: Callable[[], T]) -> T:
    """Run blocking path and command decisions on the shared bounded pool."""
    return await _decision_pool().run(operation)


def _shown(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _TARGET_CHARS else text[: _TARGET_CHARS - 3] + "..."


__all__ = ["CommandChecker"]
