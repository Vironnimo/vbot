"""Public Settings path and definition value records, preserving dynamic-key formatting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

JsonObject = dict[str, Any]


APPLICATION_LIVE = "live"


APPLICATION_RESTART = "restart"


_MISSING = object()


class SettingsPathError(ValueError):
    """Raised when a public Settings path or patch operation is invalid."""


@dataclass(frozen=True)
class PathSegment:
    """One parsed path segment, preserving whether bracket quoting was used."""

    value: str
    quoted: bool = False


@dataclass(frozen=True)
class SettingsPath:
    """A parsed public Settings path."""

    segments: tuple[PathSegment, ...]

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(segment.value for segment in self.segments)


@dataclass(frozen=True)
class DynamicSegment:
    """A required bracket-quoted key in a setting-definition pattern."""

    name: str


@dataclass(frozen=True)
class DynamicRemainder:
    """One or more bracket-quoted keys below a dynamic JSON object."""

    name: str


DefinitionSegment = str | DynamicSegment | DynamicRemainder


@dataclass(frozen=True)
class SettingDefinition:
    """Metadata and validation constraints for one public Settings path."""

    pattern: tuple[DefinitionSegment, ...]
    value_type: str
    description: str
    application: str = APPLICATION_LIVE
    default: Any = _MISSING
    allowed_values: tuple[Any, ...] = ()
    nullable: bool = False
    unsettable: bool = True
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    non_empty: bool = False

    @property
    def template(self) -> str:
        return _format_definition_pattern(self.pattern)

    @property
    def has_default(self) -> bool:
        return self.default is not _MISSING


@dataclass(frozen=True)
class ResolvedSetting:
    """A definition matched to a concrete public path."""

    path: SettingsPath
    definition: SettingDefinition
    parameters: dict[str, Any]

    @property
    def canonical_path(self) -> str:
        return _format_resolved_path(self.definition.pattern, self.path)


@dataclass(frozen=True)
class SettingsPatchOperation:
    """One validated set/unset operation in an atomic Settings patch."""

    operation: str
    resolved: ResolvedSetting
    value: Any = None


def _format_definition_pattern(pattern: tuple[DefinitionSegment, ...]) -> str:
    output = ""
    for segment in pattern:
        if isinstance(segment, DynamicSegment):
            output += f'["<{segment.name}>"]'
        elif isinstance(segment, DynamicRemainder):
            output += f'["<{segment.name}>"]...'
        else:
            output += ("." if output else "") + segment
    return output


def _format_resolved_path(
    pattern: tuple[DefinitionSegment, ...],
    path: SettingsPath,
) -> str:
    output = ""
    for index, segment in enumerate(path.segments):
        expected = pattern[min(index, len(pattern) - 1)]
        if isinstance(expected, (DynamicSegment, DynamicRemainder)):
            output += f"[{json.dumps(segment.value, ensure_ascii=False)}]"
        else:
            output += ("." if output else "") + segment.value
    return output
