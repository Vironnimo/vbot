"""Session Grants offered when the operator approves a Tool call.

Rule suggestions widen the Agent layer. Literal read folders can also satisfy
a Parent layer, and stay with the requesting Session and Computer. Every
suggestion must cover the whole prepared access under the resulting boundary.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.permissions._approval_access import ApprovalRequirement
from core.permissions._paths import LexicalPath, has_wildcard, is_inside, path_key, split_lexical
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
    where it equals ``rules`` or no rule can name it exactly. ``folders`` lists literal
    read scopes from nearest to broadest. ``choose_folder`` requires an explicit
    choice when even the nearest common scope is wider than the direct folder.
    """

    rules: tuple[str, ...]
    exact: tuple[str, ...] | None = None
    folders: tuple[str, ...] = ()
    choose_folder: bool = False

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "rules": list(self.rules),
            "exact": list(self.exact) if self.exact else None,
        }
        if self.folders:
            result["folders"] = list(self.folders)
            result["choose_folder"] = self.choose_folder
        return result


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
    file on the filesystem where the boundary actually checked the path.
    """
    if not requirements or not boundary.restricted:
        return None
    windows = (boundary.context.platform if boundary.context else sys.platform) == "win32"
    probe = is_dir or _is_server_dir
    folders, choose_folder = _read_folder_choices(
        boundary, requirements, windows=windows, is_dir=probe
    )
    folder_only = (
        GrantSuggestion((), folders=folders, choose_folder=choose_folder) if folders else None
    )
    if not boundary.layers or boundary.layers[0].source != "agent":
        return folder_only
    default: list[str] = []
    exact: list[str] = []
    exact_possible = True
    for requirement in requirements:
        proposed = _proposed(requirement, windows=windows, is_dir=probe)
        if proposed is None:
            return folder_only
        scoped, only = proposed
        default.extend(rule for rule in scoped if rule not in default)
        if only is None:
            exact_possible = False
        else:
            exact.extend(rule for rule in only if rule not in exact)
    if not _covers(boundary, default, requirements, windows=windows):
        return folder_only
    narrow: tuple[str, ...] | None = None
    if (
        exact_possible
        and exact != default
        and _covers(boundary, exact, requirements, windows=windows)
    ):
        narrow = tuple(exact)
    return GrantSuggestion(tuple(default), narrow, folders, choose_folder)


def normalize_read_folders(
    value: object, *, windows: bool = sys.platform == "win32"
) -> tuple[str, ...]:
    """Canonical absolute, literal folder names in a stored Session exception."""
    if not isinstance(value, (list, tuple)):
        raise PermissionRulesError("Session read folders must be a list.")
    result: dict[str, str] = {}
    for item in value:
        if not isinstance(item, str) or not item or has_wildcard(item) or "\x00" in item:
            raise PermissionRulesError("A Session read folder must be a literal path.")
        folder = split_lexical(item, windows=windows)
        if not folder.absolute or folder.unsafe or folder.text != item:
            raise PermissionRulesError("A Session read folder must be an absolute folder path.")
        result.setdefault(path_key(item, windows=windows), item)
    return tuple(result.values())


def _read_folder_choices(
    boundary: PermissionBoundary,
    requirements: Sequence[ApprovalRequirement],
    *,
    windows: bool,
    is_dir: Callable[[str], bool],
) -> tuple[tuple[str, ...], bool]:
    if not all(
        r.kind == "path" and r.access == "read" and r.computer_id == requirements[0].computer_id
        for r in requirements
    ):
        return (), False
    first = requirements[0]
    shown = first.shown or first.target
    path = split_lexical(shown, windows=windows)
    if path.unsafe or not path.absolute or has_wildcard(shown):
        return (), False
    segments = (
        path.segments
        if (first.subtree and not first.filtered) or first.glob or is_dir(shown)
        else path.segments[:-1]
    )
    choices: list[str] = []
    for length in range(len(segments), -1, -1):
        folder = LexicalPath(path.root, segments[:length])
        key = path_key(folder.text, windows=windows)
        if not all(
            is_inside(r.target, key) and is_inside(r.resolved or r.target, key)
            for r in requirements
        ):
            continue
        # Each offered folder must actually permit every read. A broader one
        # cannot change a protected path, deny or independent ceiling, but in
        # vBot's data folder only a folder inside it counts, so the list ends there.
        if not permitted_folder_reads(boundary.with_read_folder(folder.text), requirements):
            if not choices:
                return (), False
            break
        choices.append(folder.text)
    # A shared ancestor across different directories or a link target needs an explicit choice.
    direct = LexicalPath(path.root, segments)
    return tuple(choices), bool(
        choices and (choices[0] != direct.text or _too_wide(direct, windows=windows))
    )


def permitted_folder_reads(
    boundary: PermissionBoundary, requirements: Sequence[ApprovalRequirement]
) -> bool:
    """Recheck the private prepared accesses of a waiting call against current rules."""
    return bool(requirements) and all(
        item.kind == "path"
        and item.access == "read"
        and _permits(
            boundary, item, windows=bool(boundary.context and boundary.context.platform == "win32")
        )
        for item in requirements
    )


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
    "normalize_read_folders",
    "permitted_folder_reads",
    "suggest_session_grants",
]
