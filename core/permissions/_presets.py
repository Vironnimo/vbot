"""What Permission Rules editors need: presets and the bases a level offers.

Presets are shortcuts of the editors (WebUI, CLI), not a stored form: applying
one writes ordinary parts, and an editor names the preset a stored rule set
matches. This module is their one definition, and :func:`editor_bases` says
which base applies to each part of a level and which it cannot choose; the RPC
hands both to the WebUI, so no editor re-implements the rule language.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal, cast

from core.permissions._rules import (
    BASE_CHOICES,
    READ_BASES,
    UNRESTRICTED_BASES,
    WRITE_BASES,
    PermissionRules,
    PermissionRulesError,
    ReadBase,
    WriteBase,
    permission_rules_to_dict,
    write_within_read,
)

type PresetName = Literal["full_access", "work_dir", "work_dir_read_only"]
type PresetMatch = Literal["full_access", "work_dir", "work_dir_read_only", "custom"]
type BaseSource = Literal["own", "inherited", "default"]

# Full access sets every part to its unrestricted value with empty rule lists,
# so a level that chooses it overrides every part of a restrictive default.
# The other presets set ``read`` and ``write``; applying one also unsets
# ``commands`` and ``web`` and keeps the own allow and deny lists. Read-only:
# the rule sets are frozen, and so is the mapping.
PERMISSION_PRESETS: Final[Mapping[PresetName, PermissionRules]] = MappingProxyType(
    {
        "full_access": PermissionRules(
            read="anywhere", write="anywhere", commands="any", web="any", allow=(), deny=()
        ),
        "work_dir": PermissionRules(read="work_area", write="work_area"),
        "work_dir_read_only": PermissionRules(read="work_area", write="scratch"),
    }
)
CUSTOM_PRESET: Final = "custom"


def preset_name(text: str) -> PresetName:
    """The preset ``text`` names (``work-dir``, ``work_dir`` and ``Work dir`` alike).

    Raises :class:`PermissionRulesError` naming the presets.
    """
    key = text.strip().lower().replace("-", "_").replace(" ", "_")
    for name in PERMISSION_PRESETS:
        if key == name:
            return name
    raise PermissionRulesError(
        f'Unknown permission preset "{text.strip()}". '
        "Use full-access, work-dir or work-dir-read-only."
    )


def apply_preset(name: PresetName, rules: Mapping[str, object] | None) -> dict[str, object]:
    """The stored rule set ``rules`` with preset ``name`` applied.

    Applying a preset unsets ``commands`` and ``web`` and then sets every part
    the preset defines; an allow or deny list it does not define stays. So
    ``full_access`` replaces the whole rule set with the explicit full set, and
    the other presets keep the own rules. Works on the stored form, so rule
    strings pass through unchanged for the owner of the rule set to validate.
    """
    result: dict[str, object] = {
        part: list(value) if isinstance(value, list) else value
        for part, value in (rules or {}).items()
        if value is not None
    }
    for part, value in preset_definitions()[name].items():
        if value is None:
            result.pop(part, None)
        else:
            result[part] = value
    return result


def matching_preset(rules: PermissionRules | None) -> PresetMatch:
    """The preset ``rules`` matches, else ``custom``.

    ``full_access`` matches no rule set and every set that restricts nothing.
    The other presets compare the bases only; allow and deny rules do not count.
    """
    if rules is None or not rules.restricts:
        return "full_access"
    bases = (rules.read, rules.write, rules.commands or "any", rules.web or "any")
    for name, preset in PERMISSION_PRESETS.items():
        if name != "full_access" and bases == (preset.read, preset.write, "any", "any"):
            return name
    return CUSTOM_PRESET


def preset_definitions() -> dict[str, dict[str, object]]:
    """Each preset as the change it makes to a stored rule set (see :func:`apply_preset`).

    A part set to ``None`` is removed, every other part is set; parts a preset
    does not name stay. An editor applies a preset by applying this change.
    """
    return {
        name: {"commands": None, "web": None, **permission_rules_to_dict(preset)}
        for name, preset in PERMISSION_PRESETS.items()
    }


def editor_bases(
    own: Mapping[str, object] | None, inherited: PermissionRules | None
) -> dict[str, dict[str, object]]:
    """Each base part of a level as an editor shows it, in the stored order.

    ``own`` is the level's own rule set in stored form, also while it has
    problems (a part that is no valid base counts as not set); ``inherited`` is
    what the level inherits (``None``: nothing). Per part: ``value`` and
    ``source`` (``own``, ``inherited`` or ``default``) say what applies,
    ``inherit`` what applies without an own value, ``choices`` the bases and
    ``excluded`` those the level cannot choose because an Agent can change only
    files it can read (:func:`write_within_read`). ``write`` adds
    ``narrowed_by_read``: for a read base, the own write bases it narrows and
    to what, so an editor that changes ``read`` narrows ``write`` with it.
    """
    stored = own or {}

    def fallback(part: str) -> tuple[str, BaseSource]:
        value = getattr(inherited, part) if inherited is not None else None
        if value is not None:
            return cast(str, value), "inherited"
        return UNRESTRICTED_BASES[part], "default"

    def own_base(part: str) -> str | None:
        value = stored.get(part)
        return value if isinstance(value, str) and value in BASE_CHOICES[part] else None

    read = cast(ReadBase, own_base("read") or fallback("read")[0])
    bases: dict[str, dict[str, object]] = {}
    for part, choices in BASE_CHOICES.items():
        inherit_value, inherit_source = fallback(part)
        excluded: list[str] = []
        if part == "write":
            inherit_value = write_within_read(read, cast(WriteBase, inherit_value))
            excluded = [base for base in WRITE_BASES if write_within_read(read, base) != base]
        value = own_base(part)
        entry: dict[str, object] = {
            "value": value if value is not None else inherit_value,
            "source": "own" if value is not None else inherit_source,
            "inherit": {"value": inherit_value, "source": inherit_source},
            "choices": list(choices),
            "excluded": excluded,
        }
        if part == "write":
            entry["narrowed_by_read"] = _write_narrowing()
        bases[part] = entry
    return bases


def _write_narrowing() -> dict[str, dict[str, str]]:
    narrowing: dict[str, dict[str, str]] = {}
    for read in READ_BASES:
        narrowed: dict[str, str] = {
            write: write_within_read(read, write)
            for write in WRITE_BASES
            if write_within_read(read, write) != write
        }
        if narrowed:
            narrowing[read] = narrowed
    return narrowing
