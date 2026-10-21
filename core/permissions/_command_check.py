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
import re
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

from core.permissions._addresses import canonical_host
from core.permissions._paths import is_inside, path_key, split_lexical
from core.permissions._refusal import PathAccess, PermissionRefusal
from core.permissions.permissions import PermissionBoundary, is_own_host

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
        if refused is None and refuse_command is not None:
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
        """The first refusal in the decision order; blocking (resolves links), off the loop."""
        refused = boundary.check_path(cwd, "start_dir")
        if refused is not None:
            return refused
        context = boundary.context
        windows = (context.platform if context is not None else sys.platform) == "win32"
        for check in (
            self._deny_rules,
            self._listed,
            self._vbot_cli,
            self._vbot_server,
        ):
            refused = check(boundary, plan, cwd, windows)
            if refused is not None:
                return refused
        for url in plan.urls:
            refused = boundary.check_url(url)
            if refused is not None:
                return refused
        for host in plan.hosts:
            refused = boundary.check_host(canonical_host(host), "connect")
            if refused is not None:
                return refused
        for access in plan.paths:
            kind: PathAccess = access.kind
            refused = boundary.check_path(
                access.path,
                kind,
                subtree=access.subtree,
                glob=access.glob,
                pruned=access.pruned,
            )
            if refused is not None:
                if refused.below is not None and kind == "read":
                    refused = replace(refused, hint=_DENIED_BELOW_HINT)
                return refused
        for mention in plan.mentions:
            refused = boundary.check_path(mention.path, "read", glob=mention.glob, deny_only=True)
            if refused is not None:
                return refused
        for element in plan.unresolved:
            return _unresolved_refusal(boundary, element, text, workdir_parameter)
        return None

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

    def _listed(
        self, boundary: PermissionBoundary, plan: CommandPlan, cwd: Path, windows: bool
    ) -> PermissionRefusal | None:
        from core.permissions.commands.commands import command_pattern_matches

        for layer in boundary.layers:
            if layer.commands != "listed":
                continue
            patterns = layer.shell_allow
            for command in plan.commands:
                if not any(
                    command_pattern_matches(pattern, command, case_insensitive=windows)
                    for pattern in patterns
                ):
                    return boundary.refusal(
                        "command",
                        _shown(command.source),
                        "not_permitted",
                        source=layer.source,
                        base="listed",
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
        return None

    def _starts_vbot(
        self,
        command: SimpleCommand,
        folders: Sequence[Path],
        scripts: tuple[str, ...],
        in_checkout: bool,
        windows: bool,
    ) -> bool:
        words = [word.text for word in command.words if word.static]
        for index, word in enumerate(words):
            if word == "-m" and index + 1 < len(words):
                if in_checkout and _VBOT_MODULE.match(words[index + 1]):
                    return True
                continue
            if word.startswith("-m") and in_checkout and _VBOT_MODULE.match(word[2:]):
                return True
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
                    if match.group(2) == wanted and is_own_host(match.group(1).casefold()):
                        return _server_refusal(boundary, command)
                value = text.rpartition("=")[2]
                hosts = hosts or is_own_host(value.casefold())
                ports = ports or value.rpartition(":")[2] == wanted
            if hosts and ports:
                return _server_refusal(boundary, command)
        return None

    @staticmethod
    def _key(path: Path, windows: bool) -> str:
        return path_key(split_lexical(str(path), windows=windows).text, windows=windows)


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


def _shown(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _TARGET_CHARS else text[: _TARGET_CHARS - 3] + "..."


__all__ = ["CommandChecker"]
