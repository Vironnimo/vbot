"""Permission Rules presets: named starting points that fill the parts of a rule set.

Presets are shortcuts of the editors (WebUI, CLI), not a stored form: applying
one writes ordinary parts, and an editor names the preset a stored rule set
matches. This module is their one definition; the RPC hands the definitions to
the WebUI, and the CLI applies them here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal

from core.permissions._rules import (
    PermissionRules,
    PermissionRulesError,
    permission_rules_to_dict,
)

type PresetName = Literal["full_access", "work_dir", "work_dir_read_only"]
type PresetMatch = Literal["full_access", "work_dir", "work_dir_read_only", "custom"]

# Full access sets every part to its unrestricted value with empty rule lists,
# so a level that chooses it overrides every part of a restrictive default.
# The other presets set ``read`` and ``write``; applying one also unsets
# ``commands`` and ``web`` and keeps the own allow and deny lists.
PERMISSION_PRESETS: Final[dict[PresetName, PermissionRules]] = {
    "full_access": PermissionRules(
        read="anywhere", write="anywhere", commands="any", web="any", allow=(), deny=()
    ),
    "work_dir": PermissionRules(read="work_area", write="work_area"),
    "work_dir_read_only": PermissionRules(read="work_area", write="scratch"),
}
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
        if part not in ("commands", "web") and value is not None
    }
    result.update(preset_definitions()[name])
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
    """Each preset's stored parts, for an editor to apply (see :func:`apply_preset`)."""
    return {name: permission_rules_to_dict(preset) for name, preset in PERMISSION_PRESETS.items()}
