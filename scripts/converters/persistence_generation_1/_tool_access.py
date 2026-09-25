"""Retired Tool names in persisted Tool access, rewritten for Generation 1.

Two kinds of legacy Tool access survive in pre-Generation-1 data directories:

- An Identity Agent's root ``allowed_tools`` list, which ``tool_access``
  replaced. ``["*"]`` or a missing list meant every Tool; the runtime-derived
  names ``history``, ``memory`` and ``session_read`` were never configurable.
- Names of Tools vBot has since retired. The application treats them as
  unknown names that grant and deny nothing, so the converter replaces them in
  every Tool access policy and Project Tool whitelist it stages.

A successor is granted only where the retired Tools that were available
already covered its work, so a replacement never widens a policy beyond what
the retired names granted:

- ``grep`` and ``glob`` become ``search_files``, which needs both: neither
  could do the other's work.
- ``write`` and ``edit`` become ``apply_patch``, which needs ``write``: a
  full-file write could already create, replace and change every file, while
  ``edit`` alone could not create one.
- ``terminal_beta`` becomes ``terminal`` (a rename).
- ``read2`` and ``read_new`` become ``read``; either one covers it.

A policy that denied a retired Tool, or allowed every Tool, denies the
successor when the remaining retired Tools do not cover it. Where retired Tools
were available but do not cover the successor, the narrowing is reported.
Retired names whose work moved into another existing Tool, or out of the
Tools, are dropped without granting or denying anything else.

The successor takes the place of the first retired name it replaces; a list
that already names it keeps it where it is. Every replacement and drop is
returned as a note for the conversion report.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from core.tools.availability import ToolAccess, normalize_tool_access

_LEGACY_RUNTIME_DERIVED_TOOLS = frozenset({"history", "memory", "session_read"})


@dataclass(frozen=True, slots=True)
class _Successor:
    """A current Tool and the retired Tools whose work it took over."""

    name: str
    replaces: tuple[str, ...]
    # Sets of retired Tools that, all available, covered the successor's work.
    covered_by: tuple[frozenset[str], ...]
    requirement: str

    def covered(self, available: Collection[str]) -> bool:
        return any(names <= set(available) for names in self.covered_by)


_SUCCESSORS = (
    _Successor(
        "search_files", ("grep", "glob"), (frozenset({"grep", "glob"}),), "both grep and glob"
    ),
    _Successor("apply_patch", ("write", "edit"), (frozenset({"write"}),), "write"),
    _Successor("terminal", ("terminal_beta",), (frozenset({"terminal_beta"}),), "terminal_beta"),
    _Successor(
        "read",
        ("read2", "read_new"),
        (frozenset({"read2"}), frozenset({"read_new"})),
        "read2 or read_new",
    ),
)
_SUCCESSOR_OF = {retired: successor for successor in _SUCCESSORS for retired in successor.replaces}
# Retired Tools without a successor of their own, and where their work went.
_DROPPED = {
    "subagent_result": "subagent includes its status lookup",
    "skill_list": "skill includes its catalog",
    "session_read": "deep Session reads moved to the vbot-cli Skill",
    "browser": "browser automation moved to the playwright-cli Skill",
    "swarm_decisions": "Swarm decisions were removed",
}
RETIRED_TOOL_NAMES = frozenset({*_SUCCESSOR_OF, *_DROPPED})


class ToolAccessConversionError(Exception):
    """Raised when legacy Tool access cannot be converted without guessing."""


def convert_legacy_allowed_tools(value: Any) -> ToolAccess:
    """Return the ``tool_access`` policy equivalent to a legacy ``allowed_tools`` value."""
    if value is None:
        return normalize_tool_access({"mode": "all"})
    if not isinstance(value, list) or not all(
        isinstance(item, str) and bool(item.strip()) for item in value
    ):
        raise ToolAccessConversionError("allowed_tools must be a list of non-empty strings")
    if "*" in value:
        if value != ["*"]:
            raise ToolAccessConversionError("allowed_tools mixes '*' with explicit names")
        return normalize_tool_access({"mode": "all"})
    allowed = [name for name in value if name not in _LEGACY_RUNTIME_DERIVED_TOOLS]
    return normalize_tool_access({"mode": "selected", "allowed": list(dict.fromkeys(allowed))})


def convert_policy(value: Any) -> tuple[Any, list[str]]:
    """Replace the retired Tool names in one Tool access policy.

    Returns the policy and one report note per retired name or successor group;
    a policy without retired names comes back unchanged with no notes. A value
    that is not a valid policy also comes back unchanged: nothing here can
    repair it, and the application reports it.
    """
    if not isinstance(value, Mapping):
        return value, []
    try:
        policy = normalize_tool_access(value)
    except ValueError:
        return value, []
    named = [*policy.allowed, *policy.denied, *policy.granted]
    if not RETIRED_TOOL_NAMES.intersection(named):
        return value, []
    lists = {
        "allowed": list(policy.allowed),
        "denied": list(policy.denied),
        "granted": list(policy.granted),
    }
    notes = []
    for group in _retired_groups(named):
        if isinstance(group, str):
            for key, names in lists.items():
                lists[key] = [name for name in names if name != group]
            notes.append(f"{group} dropped: {_DROPPED[group]}")
            continue
        successor, retired = group
        available = _available(policy, successor.replaces)
        covered = successor.covered(available)
        grant = policy.mode == "selected" and covered and successor.name not in policy.denied
        lists["allowed"] = _substitute(lists["allowed"], successor, insert=grant)
        deny = (
            not covered
            and (policy.mode == "all" or any(name in policy.denied for name in successor.replaces))
            and successor.name not in lists["allowed"]
        )
        lists["denied"] = _substitute(lists["denied"], successor, insert=deny)
        lists["granted"] = _substitute(lists["granted"], successor, insert=False)
        allowed_after = successor.name not in lists["denied"] and (
            policy.mode == "all" or successor.name in lists["allowed"]
        )
        if successor.name in lists["allowed"] and successor.name not in policy.allowed:
            outcome = f"replaced by {successor.name}"
        elif successor.name in lists["denied"] and successor.name not in policy.denied:
            outcome = f"replaced by a denial of {successor.name}"
        else:
            outcome = f"dropped ({successor.name} {_state(successor.name, policy)})"
        narrowed = available if not covered and not allowed_after else set()
        notes.append(_note(successor, retired, outcome, narrowed))
    converted = normalize_tool_access(
        {
            "mode": policy.mode,
            **({"allowed": lists["allowed"]} if policy.mode == "selected" else {}),
            "denied": lists["denied"],
            "granted": lists["granted"],
        }
    )
    return converted.to_dict(), notes


def convert_whitelist(value: list[Any]) -> tuple[list[Any], list[str]]:
    """Replace the retired Tool names in a Project Tool whitelist.

    Returns the whitelist and its report notes, as :func:`convert_policy` does.
    Entries that are not names are kept for the application to report.
    """
    named = [name for name in value if isinstance(name, str)]
    if not RETIRED_TOOL_NAMES.intersection(named):
        return list(value), []
    result = list(value)
    notes = []
    for group in _retired_groups(named):
        if isinstance(group, str):
            result = [name for name in result if name != group]
            notes.append(f"{group} dropped: {_DROPPED[group]}")
            continue
        successor, retired = group
        covered = successor.covered(retired)
        present = successor.name in named
        result = _substitute(result, successor, insert=covered)
        if covered and not present:
            notes.append(_note(successor, retired, f"replaced by {successor.name}", ()))
        elif present:
            outcome = f"dropped ({successor.name} already allowed)"
            notes.append(_note(successor, retired, outcome, ()))
        else:
            outcome = f"dropped ({successor.name} not granted)"
            notes.append(_note(successor, retired, outcome, retired))
    return result, notes


def _retired_groups(named: Iterable[str]) -> list[str | tuple[_Successor, list[str]]]:
    """The retired names in ``named`` in order of appearance, grouped by successor.

    A dropped name stands alone; replaced names come with their successor.
    """
    groups: list[str | tuple[_Successor, list[str]]] = []
    by_successor: dict[str, list[str]] = {}
    for name in dict.fromkeys(named):
        if name in _DROPPED:
            groups.append(name)
        elif (successor := _SUCCESSOR_OF.get(name)) is not None:
            if successor.name not in by_successor:
                by_successor[successor.name] = []
                groups.append((successor, by_successor[successor.name]))
            by_successor[successor.name].append(name)
    return groups


def _available(policy: ToolAccess, names: Iterable[str]) -> set[str]:
    """The retired ``names`` the policy made available while they existed."""
    if policy.mode == "none":
        return set()
    return {
        name
        for name in names
        if (policy.mode == "all" or name in policy.allowed) and name not in policy.denied
    }


def _substitute(names: list[Any], successor: _Successor, *, insert: bool) -> list[Any]:
    """Remove the successor's retired names; with ``insert``, put it at the first one's place."""
    result = []
    placed = successor.name in names
    for name in names:
        if isinstance(name, str) and name in successor.replaces:
            if insert and not placed:
                result.append(successor.name)
                placed = True
            continue
        result.append(name)
    return result


def _state(name: str, policy: ToolAccess) -> str:
    if name in policy.denied:
        return "already denied"
    if name in policy.allowed:
        return "already allowed"
    if policy.mode == "all":
        return "stays allowed"
    return "not granted"


def _note(
    successor: _Successor, retired: Sequence[str], outcome: str, narrowed: Collection[str]
) -> str:
    note = f"{_join(retired)} {outcome}"
    if narrowed:
        note += (
            f": {successor.name} needs {successor.requirement}, so the access of "
            f"{_join(sorted(narrowed))} is not carried over; enable {successor.name} "
            "explicitly if wanted"
        )
    return note


def _join(names: Sequence[str]) -> str:
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"
