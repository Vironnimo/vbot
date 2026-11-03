"""Session Grants: allow rules the user adds to one Session while approving a Tool call.

An approval request offers to remember its access for the rest of the Session.
:func:`suggest_session_grants` proposes the rules for that: a sensible scope by
default (the folder, the command with any arguments, the host) and, where it
differs, one that names only this access. A suggestion exists only when the
rules, added to the Agent's own layer, would really let the same call run:
inherited limits are never widened, so a call they refuse gets none.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.permissions._approval_access import ApprovalRequirement
from core.permissions._paths import LexicalPath, has_wildcard, split_lexical
from core.permissions._refusal import HostAccess, PathAccess
from core.permissions._rules import PermissionRule, PermissionRulesError, parse_permission_rule
from core.permissions.commands.commands import (
    CommandWord,
    ShellKind,
    SimpleCommand,
    command_pattern_matches,
)
from core.permissions.permissions import PermissionBoundary

# A Session Grant only widens: deny rules stay with the rule sets the user edits.
GRANT_KINDS = frozenset({"Read", "Edit", "Shell", "Web"})


@dataclass(frozen=True, slots=True)
class GrantSuggestion:
    """The rules that would let later calls make an approval's access without asking.

    ``rules`` is the default scope; ``exact`` names only this access, ``None``
    where it equals ``rules`` or no rule can name it exactly.
    """

    rules: tuple[str, ...]
    exact: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"rules": list(self.rules), "exact": list(self.exact) if self.exact else None}


def normalize_session_grants(
    value: object, *, platform: str = sys.platform
) -> tuple[PermissionRule, ...]:
    """The stored grants as rules; raises ``PermissionRulesError`` for anything else.

    A list of ``Read``, ``Edit``, ``Shell`` and ``Web`` rule strings, duplicates
    removed in order.
    """
    if not isinstance(value, (list, tuple)):
        raise PermissionRulesError("Session Grants are a list of rule strings.")
    rules: list[PermissionRule] = []
    for item in value:
        if not isinstance(item, str):
            raise PermissionRulesError("Session Grants are a list of rule strings.")
        rule = parse_permission_rule(item, platform=platform)
        if rule.kind not in GRANT_KINDS:
            raise PermissionRulesError(f"{item!r} cannot be a Session Grant.")
        if rule not in rules:
            rules.append(rule)
    return tuple(rules)


def suggest_session_grants(
    boundary: PermissionBoundary,
    requirements: Sequence[ApprovalRequirement],
    *,
    is_dir: Callable[[str], bool] | None = None,
) -> GrantSuggestion | None:
    """Rules for the Session that make *requirements* permitted, or ``None``.

    Blocking: paths resolve on the filesystem. ``is_dir`` tells a folder from a
    file for paths on the server (default: the server's filesystem); a path on
    another Computer counts as a file unless its access reaches below it.
    """
    if not requirements or boundary.with_agent_allow(()) is None:
        return None
    windows = (boundary.context.platform if boundary.context else sys.platform) == "win32"
    probe = is_dir or _is_server_dir
    default: list[str] = []
    exact: list[str] = []
    exact_possible = True
    for requirement in requirements:
        proposed = _proposed(requirement, windows=windows, is_dir=probe)
        if proposed is None:
            return None
        scoped, only = proposed
        default.extend(rule for rule in scoped if rule not in default)
        if only is None:
            exact_possible = False
        else:
            exact.extend(rule for rule in only if rule not in exact)
    if not _covers(boundary, default, requirements, windows=windows):
        return None
    narrow: tuple[str, ...] | None = None
    if (
        exact_possible
        and exact != default
        and _covers(boundary, exact, requirements, windows=windows)
    ):
        narrow = tuple(exact)
    return GrantSuggestion(tuple(default), narrow)


def _proposed(
    requirement: ApprovalRequirement, *, windows: bool, is_dir: Callable[[str], bool]
) -> tuple[list[str], list[str] | None] | None:
    """The default and the exact rules for one requirement (exact ``None``: not expressible)."""
    if requirement.kind == "path":
        return _path_rules(requirement, windows=windows, is_dir=is_dir)
    if requirement.kind == "command":
        return _command_rules(requirement)
    if requirement.kind in ("host", "url"):
        host = _host_of(requirement)
        if not host:
            return None
        return [f"Web({host})"], [f"Web({host})"]
    return None


def _path_rules(
    requirement: ApprovalRequirement, *, windows: bool, is_dir: Callable[[str], bool]
) -> tuple[list[str], list[str] | None] | None:
    kind = "Edit" if requirement.access == "write" else "Read"
    shown = requirement.shown or requirement.target
    forms = [shown]
    if requirement.resolved and requirement.resolved != requirement.target:
        forms.append(requirement.resolved)
    default: list[str] = []
    exact: list[str] = []
    for text in forms:
        if has_wildcard(text):
            return None
        path = split_lexical(text, windows=windows)
        if not path.absolute or path.unsafe is not None:
            return None
        whole = (
            requirement.subtree
            or requirement.glob is not None
            or requirement.access == "start_dir"
            or (requirement.computer_id == "server" and is_dir(text))
        )
        folder = path if whole or not path.segments else LexicalPath(path.root, path.segments[:-1])
        if _too_wide(folder, windows=windows):
            folder = path
        default.append(f"{kind}({folder.text})")
        exact.append(f"{kind}({path.text})")
    return default, exact


def _too_wide(folder: LexicalPath, *, windows: bool) -> bool:
    """A drive, the filesystem root or the home folder is no folder to grant whole."""
    if not folder.segments:
        return True
    home = split_lexical(os.fspath(Path.home()), windows=windows)
    if windows:
        return folder.text.casefold() == home.text.casefold()
    return folder.text == home.text


def _command_rules(requirement: ApprovalRequirement) -> tuple[list[str], list[str] | None] | None:
    if not requirement.command_identity:
        return None
    (name, static), *words = requirement.command_identity
    if not static or not _plain(name):
        return None
    scoped = name
    if words and words[0][1] and _plain(words[0][0]) and not words[0][0].startswith("-"):
        scoped = f"{name} {words[0][0]}"
    default = [f"Shell({scoped} *)"]
    if all(word_static and _plain(text) for text, word_static in words):
        exact = [f"Shell({' '.join([name, *(text for text, _ in words)])})"]
        return default, exact
    return default, None


def _plain(word: str) -> bool:
    """Whether a pattern can name *word* literally: no space or wildcard inside."""
    return bool(word) and not any(char.isspace() for char in word) and "*" not in word


def _host_of(requirement: ApprovalRequirement) -> str:
    if requirement.kind == "host":
        return requirement.target
    from urllib.parse import urlsplit

    host = urlsplit(requirement.target).hostname or ""
    return f"[{host}]" if ":" in host else host


def _covers(
    boundary: PermissionBoundary,
    texts: Sequence[str],
    requirements: Sequence[ApprovalRequirement],
    *,
    windows: bool,
) -> bool:
    """Whether the boundary with *texts* in the Agent's layer permits every requirement."""
    platform = "win32" if windows else "linux"
    try:
        rules = [parse_permission_rule(text, platform=platform) for text in texts]
    except PermissionRulesError:
        return False
    candidate = boundary.with_agent_allow(rules)
    if candidate is None:
        return False
    return all(_permits(candidate, requirement, windows=windows) for requirement in requirements)


def _permits(
    boundary: PermissionBoundary, requirement: ApprovalRequirement, *, windows: bool
) -> bool:
    if requirement.kind == "path":
        forms = [requirement.shown or requirement.target]
        if requirement.resolved and requirement.resolved != requirement.target:
            forms.append(requirement.resolved)
        access: PathAccess
        if requirement.access == "write":
            access = "write"
        elif requirement.access == "start_dir":
            access = "start_dir"
        elif requirement.access == "read":
            access = "read"
        else:
            return False
        # A filtered root was checked as a plain read; its entries are filtered one by one.
        subtree = requirement.subtree and not requirement.filtered
        return all(
            boundary.check_path(
                form,
                access,
                subtree=subtree,
                glob=requirement.glob,
                pruned=requirement.pruned,
                git=requirement.git,
            )
            is None
            for form in forms
        )
    if requirement.kind == "host":
        host_access: HostAccess = "connect" if requirement.access == "connect" else "web"
        return boundary.check_host(requirement.target, host_access, by_program=True) is None
    if requirement.kind == "url":
        return boundary.check_url(requirement.target, by_program=True) is None
    if requirement.kind == "command":
        (name, _), *words = requirement.command_identity
        shell: ShellKind = "bash"
        if requirement.shell == "powershell":
            shell = "powershell"
        elif requirement.shell == "cmd":
            shell = "cmd"
        arguments = tuple(CommandWord(text, static) for text, static in words)
        command = SimpleCommand(name, arguments, requirement.target, shell)
        return all(
            any(
                command_pattern_matches(pattern, command, case_insensitive=windows)
                for pattern in layer.shell_allow
            )
            for layer in boundary.layers
            if layer.commands == "listed"
        )
    return False


def _is_server_dir(text: str) -> bool:
    try:
        return Path(text).is_dir()
    except OSError:
        return False


__all__ = [
    "GRANT_KINDS",
    "GrantSuggestion",
    "normalize_session_grants",
    "suggest_session_grants",
]
