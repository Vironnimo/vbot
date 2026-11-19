"""Named Permission Profiles and the editor's inline rule presets.

An Agent references a profile by name. Inline presets copy rule parts into the
edited rule set. The CLI and WebUI share these definitions and the base choices.
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
    normalize_permission_rules,
    permission_rules_to_dict,
    resolve_rule_set,
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


# Named profiles are complete policies. The inline editor presets above keep
# their original patch semantics for stored rules and existing callers.
_RESTRICTED: Final = PermissionRules(
    read="anywhere", write="work_area", commands="any", web="listed", allow=(), deny=()
)
BUILTIN_PERMISSION_PROFILES: Final[Mapping[str, PermissionRules]] = MappingProxyType(
    {"full_access": PERMISSION_PRESETS["full_access"], "restricted": _RESTRICTED}
)


def normalize_permission_profile_name(value: object) -> str:
    """Validate the exact name shared by a profile and the Agents selecting it."""
    if not isinstance(value, str) or not value or value != value.strip() or not value.isprintable():
        raise PermissionRulesError(
            "A permission profile name must be non-empty printable text "
            "without surrounding whitespace."
        )
    return value


def complete_effective_permission_rules(value: object) -> PermissionRules | None:
    """Fill omitted effective parts with their runtime defaults; None is Full Access."""
    if value is None:
        return None
    return resolve_rule_set(normalize_permission_rules(value), PERMISSION_PRESETS["full_access"])


def custom_permission_profile_name(value: object) -> str:
    name = normalize_permission_profile_name(value)
    if name.casefold().replace("-", "_").replace(" ", "_") in BUILTIN_PERMISSION_PROFILES:
        raise PermissionRulesError(f"Permission profile {name!r} is reserved for a built-in.")
    return name


def normalize_custom_permission_profile(name: str, value: object) -> PermissionRules:
    """Fill omitted parts of a custom profile from the Restricted profile."""
    custom_permission_profile_name(name)
    try:
        rules = normalize_permission_rules(value)
    except PermissionRulesError as error:
        raise PermissionRulesError(f"Permission profile {name!r}: {error}") from error
    return PermissionRules(
        read=rules.read or _RESTRICTED.read,
        write=rules.write or _RESTRICTED.write,
        commands=rules.commands or _RESTRICTED.commands,
        web=rules.web or _RESTRICTED.web,
        allow=rules.allow or (),
        deny=rules.deny or (),
    )


def normalize_permission_profiles(profiles: object) -> dict[str, dict[str, object]]:
    """Normalize a Settings catalog of custom profiles, rejecting invalid entries."""
    if not isinstance(profiles, Mapping):
        raise PermissionRulesError("permission_profiles must be an object of named rule sets.")
    result: dict[str, dict[str, object]] = {}
    for name, value in profiles.items():
        result[name] = permission_rules_to_dict(normalize_custom_permission_profile(name, value))
    return result


def resolve_permission_profile(name: str, profiles: Mapping[str, object]) -> PermissionRules:
    """Return one complete profile, refusing an unavailable selection."""
    name = normalize_permission_profile_name(name)
    if name in BUILTIN_PERMISSION_PROFILES:
        return BUILTIN_PERMISSION_PROFILES[name]
    if name not in profiles:
        raise PermissionRulesError(f"Unknown permission profile {name!r}.")
    return normalize_custom_permission_profile(name, profiles[name])


def permission_profile_catalog(profiles: Mapping[str, object]) -> dict[str, dict[str, object]]:
    """List the two built-ins and saved profiles as complete, independent rule sets."""
    return {
        **{
            name: permission_rules_to_dict(rules)
            for name, rules in BUILTIN_PERMISSION_PROFILES.items()
        },
        **normalize_permission_profiles(profiles),
    }


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
