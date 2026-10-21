"""Placeholder-free layers of a Permission Boundary and the ceilings that carry them.

A layer is one expanded rule set; a ceiling is the layers a Run inherits from
whoever caused it, in a JSON-safe stored form that Sub-Agent Sessions, Cron jobs
and command checkpoints persist.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Final, cast

from core.permissions._paths import split_lexical
from core.permissions._refusal import LAYER_SOURCES, LayerSource
from core.permissions._rules import (
    CommandsBase,
    PermissionRule,
    PermissionRulesError,
    ReadBase,
    WebBase,
    WriteBase,
    parse_permission_rule,
    validated_base,
    write_within_read,
)

_REQUIRED_FIELDS: Final = frozenset(
    {"source", "read", "write", "commands", "web", "workdir", "allow", "deny"}
)
_LAYER_FIELDS: Final = _REQUIRED_FIELDS | {"scratch", "rule_files"}


@dataclass(frozen=True, slots=True)
class PermissionLayer:
    """One placeholder-free rule set that a Run's accesses must satisfy.

    Bases are always set (defaults filled). ``workdir`` and ``scratch`` are the
    layer's Work Area; path patterns of its rules are absolute. ``rule_files``
    are the files this layer's rules come from (a Team member's repository
    profile and the settings files its translation reads, also ones that do not
    exist yet): no Run under the layer may change them, so a ceiling protects
    its own source too.
    """

    source: LayerSource
    read: ReadBase
    write: WriteBase
    commands: CommandsBase
    web: WebBase
    workdir: str
    scratch: str | None
    allow: tuple[PermissionRule, ...]
    deny: tuple[PermissionRule, ...]
    rule_files: tuple[str, ...] = ()

    @property
    def shell_allow(self) -> tuple[str, ...]:
        return tuple(rule.pattern for rule in self.allow if rule.kind == "Shell")

    @property
    def shell_deny(self) -> tuple[str, ...]:
        return tuple(rule.pattern for rule in self.deny if rule.kind == "Shell")

    def to_dict(self) -> dict[str, object]:
        document: dict[str, object] = {
            "source": self.source,
            "read": self.read,
            "write": self.write,
            "commands": self.commands,
            "web": self.web,
            "workdir": self.workdir,
            "scratch": self.scratch,
            "allow": [rule.text for rule in self.allow],
            "deny": [rule.text for rule in self.deny],
        }
        if self.rule_files:
            document["rule_files"] = list(self.rule_files)
        return document

    @property
    def fingerprint(self) -> str:
        """The layer's rules as one stable text, whoever set them.

        Two layers with the same fingerprint limit the same way, so a record
        of the layers something ran under (a terminal's ``permission_scope``)
        compares fingerprints, not where a layer came from.
        """
        document = self.to_dict()
        del document["source"]
        return json.dumps(document, sort_keys=True)

    def retagged(self, source: LayerSource) -> PermissionLayer:
        return replace(self, source=source)


@dataclass(frozen=True, slots=True)
class PermissionCeiling:
    """Layers inherited from whoever caused a Run, in their JSON-safe stored form."""

    layers: tuple[PermissionLayer, ...]

    def to_ceiling_dict(self) -> dict[str, object]:
        return {"layers": [layer.to_dict() for layer in self.layers]}

    def joined(self, other: PermissionCeiling | None) -> PermissionCeiling:
        """This ceiling narrowed by ``other``: every layer of both, each once.

        Joining never widens: a Run must satisfy every layer, so an added layer
        can only refuse more.
        """
        if other is None:
            return self
        return PermissionCeiling(tuple(dict.fromkeys((*self.layers, *other.layers))))

    @classmethod
    def from_ceiling_dict(cls, value: object, *, platform: str = sys.platform) -> PermissionCeiling:
        """Read a stored ceiling strictly; raises :class:`PermissionRulesError`."""
        if not isinstance(value, Mapping) or set(value) != {"layers"}:
            raise PermissionRulesError(
                'A permission ceiling must be an object with exactly the field "layers".'
            )
        layers = value["layers"]
        if not isinstance(layers, list) or not layers:
            raise PermissionRulesError("permission_ceiling.layers must be a non-empty list.")
        windows = platform == "win32"
        return cls(
            tuple(
                _layer_from_dict(item, f"permission_ceiling.layers[{index}]", windows=windows)
                for index, item in enumerate(layers)
            )
        )


def join_ceilings(
    stored: PermissionCeiling | None, added: PermissionCeiling | None
) -> PermissionCeiling | None:
    """``stored`` narrowed by ``added``; ``None`` only when neither is set."""
    if stored is None:
        return added
    return stored.joined(added)


def _layer_from_dict(value: object, where: str, *, windows: bool) -> PermissionLayer:
    if not isinstance(value, Mapping):
        raise PermissionRulesError(f"{where} must be an object.")
    unknown = sorted(str(key) for key in value if key not in _LAYER_FIELDS)
    if unknown:
        raise PermissionRulesError(f"{where} has no field {unknown[0]!r}.")
    missing = sorted(_REQUIRED_FIELDS - set(value))
    if missing:
        raise PermissionRulesError(f"{where} needs the field {missing[0]!r}.")
    source = value["source"]
    if source not in LAYER_SOURCES:
        raise PermissionRulesError(
            f"{where}.source must be one of {', '.join(LAYER_SOURCES)}; received {source!r}."
        )
    try:
        bases = {
            part: validated_base(part, value[part]) for part in ("read", "write", "commands", "web")
        }
    except PermissionRulesError as error:
        raise PermissionRulesError(f"{where}.{error}") from None
    workdir = _absolute_path(value["workdir"], f"{where}.workdir", windows=windows)
    scratch_value = value.get("scratch")
    scratch = (
        None
        if scratch_value is None
        else _absolute_path(scratch_value, f"{where}.scratch", windows=windows)
    )
    rule_files_value = value.get("rule_files", [])
    if not isinstance(rule_files_value, list):
        raise PermissionRulesError(f"{where}.rule_files must be a list of absolute paths.")
    rule_files = tuple(
        _absolute_path(item, f"{where}.rule_files[{index}]", windows=windows)
        for index, item in enumerate(rule_files_value)
    )
    platform = "win32" if windows else "linux"
    rules: dict[str, tuple[PermissionRule, ...]] = {}
    for part in ("allow", "deny"):
        listed = value[part]
        if not isinstance(listed, list):
            raise PermissionRulesError(f"{where}.{part} must be a list of rule strings.")
        parsed: list[PermissionRule] = []
        for index, text in enumerate(listed):
            if not isinstance(text, str):
                raise PermissionRulesError(f"{where}.{part}[{index}] must be a rule string.")
            rule = parse_permission_rule(text, platform=platform)
            if rule.kind in ("Read", "Edit") and (
                rule.pattern.startswith(("{", "~"))
                or not split_lexical(rule.pattern, windows=windows).absolute
            ):
                raise PermissionRulesError(
                    f"{where}.{part}[{index}] {rule.text} must name an absolute path "
                    "without placeholders."
                )
            parsed.append(rule)
        rules[part] = tuple(parsed)
    return PermissionLayer(
        source=cast(LayerSource, source),
        read=cast(ReadBase, bases["read"]),
        write=write_within_read(cast(ReadBase, bases["read"]), cast(WriteBase, bases["write"])),
        commands=cast(CommandsBase, bases["commands"]),
        web=cast(WebBase, bases["web"]),
        workdir=workdir,
        scratch=scratch,
        allow=rules["allow"],
        deny=rules["deny"],
        rule_files=rule_files,
    )


def _absolute_path(value: object, where: str, *, windows: bool) -> str:
    if not isinstance(value, str):
        raise PermissionRulesError(f"{where} must be an absolute path.")
    lexical = split_lexical(value, windows=windows)
    if not lexical.absolute or lexical.unsafe is not None:
        raise PermissionRulesError(f"{where} must be an absolute path; received {value!r}.")
    return lexical.text


__all__ = ["PermissionCeiling", "PermissionLayer", "join_ceilings"]
