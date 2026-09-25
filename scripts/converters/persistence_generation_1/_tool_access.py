"""Retired Tool names in Agent and Project Tool access, rewritten for Generation 1.

Two older Tool access shapes survive in pre-Generation-1 data directories:

- An Identity Agent's root ``allowed_tools`` list, which ``tool_access``
  replaced. ``["*"]`` or a missing list meant every Tool; the runtime-derived
  names ``history``, ``memory`` and ``session_read`` were never configurable.
- The separate ``grep`` and ``glob`` Tools, which ``search_files`` replaced.
  Search stays available only where both old Tools were available, so the
  consolidation never widens a policy: where only one of them was allowed,
  ``search_files`` is not granted and the narrowing is reported.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from core.tools.availability import ToolAccess, normalize_tool_access

_LEGACY_RUNTIME_DERIVED_TOOLS = frozenset({"history", "memory", "session_read"})
RETIRED_SEARCH_TOOLS = frozenset({"grep", "glob"})
SEARCH_TOOL = "search_files"


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


def consolidate_search_policy(value: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Replace ``grep`` and ``glob`` in one Tool access policy with ``search_files``.

    Returns the policy and, when search access narrowed, why. A policy without
    the retired names is returned unchanged. ``ValueError`` means the policy is
    invalid; the application reports it.
    """
    policy = normalize_tool_access(value)
    names = {*policy.allowed, *policy.denied, *policy.granted}
    if not RETIRED_SEARCH_TOOLS.intersection(names):
        return dict(value), None
    if policy.mode == "none":
        effective: set[str] = set()
    else:
        effective = {
            name
            for name in RETIRED_SEARCH_TOOLS
            if (policy.mode == "all" or name in policy.allowed) and name not in policy.denied
        }
    result = policy.to_dict()
    for key in ("allowed", "denied", "granted"):
        if key in result:
            result[key] = [name for name in result[key] if name not in RETIRED_SEARCH_TOOLS]
    search = effective == RETIRED_SEARCH_TOOLS
    if search and policy.mode == "selected":
        result["allowed"] = list(dict.fromkeys([*result["allowed"], SEARCH_TOOL]))
    if (
        not search
        and (policy.mode == "all" or RETIRED_SEARCH_TOOLS.intersection(policy.denied))
        and SEARCH_TOOL not in result.get("allowed", [])
    ):
        result["denied"] = list(dict.fromkeys([*result.get("denied", []), SEARCH_TOOL]))
    converted = normalize_tool_access(result)
    granted = SEARCH_TOOL not in converted.denied and (
        converted.mode == "all" or SEARCH_TOOL in converted.allowed
    )
    if effective and not granted:
        return converted.to_dict(), _narrowed(effective)
    return converted.to_dict(), None


def consolidate_search_ceiling(value: list[Any]) -> tuple[list[Any], str | None]:
    """Replace ``grep`` and ``glob`` in a Project Tool whitelist with ``search_files``."""
    present = RETIRED_SEARCH_TOOLS.intersection(name for name in value if isinstance(name, str))
    if not present:
        return list(value), None
    result = [name for name in value if name not in RETIRED_SEARCH_TOOLS]
    if present == RETIRED_SEARCH_TOOLS:
        result.append(SEARCH_TOOL)
    result = list(dict.fromkeys(result))
    if present != RETIRED_SEARCH_TOOLS and SEARCH_TOOL not in result:
        return result, _narrowed(present)
    return result, None


def _narrowed(available: Collection[str]) -> str:
    [name] = sorted(available)
    return (
        f"only {name} of the retired grep and glob Tools was available, so {SEARCH_TOOL} "
        "was not granted; enable it explicitly if wanted"
    )
