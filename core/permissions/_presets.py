"""Permission Rules presets: named starting points that fill the bases of a rule set.

Presets are shortcuts of the editors (WebUI, CLI), not a stored form: applying
one writes ordinary bases, and an editor names the preset a stored rule set
matches. This module is their one definition; the RPC hands the definitions to
the WebUI, and the CLI applies them here.
"""

from __future__ import annotations

from typing import Final, Literal

from core.permissions._rules import PermissionRules, PermissionRulesError

type PresetName = Literal["full_access", "work_dir", "work_dir_read_only"]
type PresetMatch = Literal["full_access", "work_dir", "work_dir_read_only", "custom"]

# Full access is no rule set at all; the other presets set ``read`` and ``write``
# and leave ``commands`` and ``web`` at their unrestricted defaults.
PERMISSION_PRESETS: Final[dict[PresetName, PermissionRules | None]] = {
    "full_access": None,
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


def apply_preset(name: PresetName, rules: PermissionRules | None) -> PermissionRules | None:
    """``rules`` with the bases of preset ``name``; its allow and deny rules stay.

    ``full_access`` removes the rule set, rules included. The other presets set
    ``read`` and ``write`` and unset ``commands`` and ``web``, so the result
    matches the preset.
    """
    preset = PERMISSION_PRESETS[name]
    if preset is None:
        return None
    return PermissionRules(
        read=preset.read,
        write=preset.write,
        allow=rules.allow if rules is not None else None,
        deny=rules.deny if rules is not None else None,
    )


def matching_preset(rules: PermissionRules | None) -> PresetMatch:
    """The preset whose bases ``rules`` has, else ``custom``; allow and deny rules do not count."""
    if rules is None:
        return "full_access"
    bases = (rules.read, rules.write, rules.commands or "any", rules.web or "any")
    for name, preset in PERMISSION_PRESETS.items():
        if preset is not None and bases == (preset.read, preset.write, "any", "any"):
            return name
    return CUSTOM_PRESET


def preset_definitions() -> dict[str, dict[str, object] | None]:
    """Each preset's stored bases (``None`` for full access), for an editor to apply."""
    return {
        name: None if preset is None else {"read": preset.read, "write": preset.write}
        for name, preset in PERMISSION_PRESETS.items()
    }
