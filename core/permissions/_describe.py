"""What a rule set or a ceiling lets an Agent reach, for the user to read.

Editors preview a rule set while it is edited and a Session shows the limits it
runs under. Both read the same per-layer description: the folders the Agent may
read and change, the commands and hosts it may use (``None`` for no limit) and
the deny rules. Placeholders expand where the caller knows their value; an
unknown one stays as written (``{scratch}``: the conversation's Scratch Area),
and a rule whose placeholder has no value in this context is listed as
inactive, as a Run drops it.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING

from core.permissions._paths import join_lexical, lexical_text, split_lexical
from core.permissions._rules import PermissionRule, PermissionRules

if TYPE_CHECKING:
    from core.permissions.permissions import PermissionLayer

type LayerDescription = dict[str, object]


def describe_permission_rules(
    rules: PermissionRules,
    *,
    values: Mapping[str, str | None] | None = None,
    platform: str = sys.platform,
) -> LayerDescription:
    """Describe an Agent's resolved rule set as one layer with source ``agent``.

    ``values`` maps placeholder names (``workdir``, ``project``, ``scratch``,
    ``workspace``, ``home``) to absolute folders; a name mapped to ``None`` has no
    value here, so rules using it are ``inactive``. Unset bases count as their
    unrestricted defaults.
    """
    windows = platform == "win32"
    known = {
        name: None if value is None else lexical_text(value, windows=windows)
        for name, value in (values or {}).items()
    }
    workdir = known.get("workdir") or "{workdir}"
    scratch = known.get("scratch") or "{scratch}"
    inactive: list[str] = []

    def expanded(listed: tuple[PermissionRule, ...] | None) -> tuple[PermissionRule, ...]:
        result: list[PermissionRule] = []
        for rule in listed or ():
            if rule.kind in ("Read", "Edit"):
                pattern = _expand(rule.pattern, known, windows=windows)
                if pattern is None:
                    inactive.append(rule.text)
                    continue
                rule = PermissionRule(rule.kind, pattern)
            if rule not in result:
                result.append(rule)
        return tuple(result)

    allow = expanded(rules.allow)
    deny = expanded(rules.deny)
    description = _describe(
        source="agent",
        read=rules.read or "anywhere",
        write=rules.write or "anywhere",
        commands=rules.commands or "any",
        web=rules.web or "any",
        workdir=workdir,
        scratch=scratch,
        allow=allow,
        deny=deny,
    )
    description["inactive"] = inactive
    return description


def describe_layer(layer: PermissionLayer) -> LayerDescription:
    """Describe one placeholder-free layer of a boundary or ceiling."""
    return _describe(
        source=layer.source,
        read=layer.read,
        write=layer.write,
        commands=layer.commands,
        web=layer.web,
        workdir=layer.workdir,
        scratch=layer.scratch,
        allow=layer.allow,
        deny=layer.deny,
    )


def _describe(
    *,
    source: str,
    read: str,
    write: str,
    commands: str,
    web: str,
    workdir: str,
    scratch: str | None,
    allow: tuple[PermissionRule, ...],
    deny: tuple[PermissionRule, ...],
) -> LayerDescription:
    work_area = [workdir] if scratch is None else [workdir, scratch]
    edit_allow = [rule.pattern for rule in allow if rule.kind == "Edit"]
    read_allow = [rule.pattern for rule in allow if rule.kind == "Read"]
    readable: list[str] | None = None
    if read != "anywhere":
        readable = _unique([*work_area, *read_allow, *edit_allow])
    writable: list[str] | None = None
    if write == "work_area":
        writable = _unique([*work_area, *edit_allow])
    elif write == "scratch":
        writable = _unique([*([] if scratch is None else [scratch]), *edit_allow])
    return {
        "source": source,
        "bases": {"read": read, "write": write, "commands": commands, "web": web},
        "workdir": workdir,
        "read": readable,
        "write": writable,
        "commands": (
            None if commands == "any" else [r.pattern for r in allow if r.kind == "Shell"]
        ),
        "web": None if web == "any" else [r.pattern for r in allow if r.kind == "Web"],
        "deny": [rule.text for rule in deny],
    }


def _expand(pattern: str, values: Mapping[str, str | None], *, windows: bool) -> str | None:
    """``pattern`` with its leading placeholder or ``~`` expanded; ``None`` when inactive."""
    if pattern.startswith("{"):
        name, _, rest = pattern[1:].partition("}")
        if name not in values:
            return pattern
        value = values[name]
        if value is None:
            return None
        text = value.rstrip("/") + rest
    elif pattern == "~" or pattern.startswith("~/"):
        home = values.get("home")
        if home is None:
            return pattern
        text = home.rstrip("/") + pattern[1:]
    else:
        text = pattern
    lexical = split_lexical(text, windows=windows)
    if lexical.absolute:
        return lexical.text
    workdir = values.get("workdir")
    if workdir is None:
        return "{workdir}/" + lexical.text if lexical.text else "{workdir}"
    return join_lexical(split_lexical(workdir, windows=windows), lexical).text


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
