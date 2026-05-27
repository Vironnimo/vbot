"""vBot-specific skill requirement metadata parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias, cast

RequirementCheckKind: TypeAlias = Literal["binary", "env", "skill"]
RequirementGroupKind: TypeAlias = Literal["all", "any"]
SkillAvailabilityState: TypeAlias = Literal["available", "unavailable", "invalid"]

REQUIREMENTS_METADATA_KEY = "vbot"


class RequirementParseError(ValueError):
    """Raised when ``metadata.vbot.requirements`` is malformed."""


@dataclass(frozen=True)
class RequirementCheck:
    """One primitive machine-checkable skill requirement."""

    kind: RequirementCheckKind
    name: str

    def describe(self) -> str:
        if self.kind == "binary":
            return f"binary '{self.name}'"
        if self.kind == "env":
            return f"environment variable '{self.name}'"
        return f"skill '{self.name}'"


@dataclass(frozen=True)
class RequirementGroup:
    """A boolean requirement group."""

    operator: RequirementGroupKind
    children: tuple[RequirementNode, ...]

    def describe(self) -> str:
        joiner = " and " if self.operator == "all" else " or "
        return f"{self.operator}({joiner.join(child.describe() for child in self.children)})"


RequirementNode: TypeAlias = RequirementCheck | RequirementGroup


@dataclass(frozen=True)
class SkillRequirements:
    """Parsed vBot skill requirements."""

    required: RequirementNode | None = None
    optional: tuple[RequirementNode, ...] = field(default_factory=tuple)

    @property
    def empty(self) -> bool:
        return self.required is None and not self.optional


@dataclass(frozen=True)
class RequirementEvaluation:
    """Runtime result of evaluating one requirement node."""

    satisfied: bool
    missing: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SkillAvailability:
    """Runtime availability state for a loadable skill."""

    state: SkillAvailabilityState
    missing: tuple[str, ...] = field(default_factory=tuple)
    optional_missing: tuple[str, ...] = field(default_factory=tuple)


AVAILABLE = SkillAvailability("available")


def parse_vbot_requirements(metadata: dict[str, Any]) -> SkillRequirements:
    """Parse ``metadata.vbot.requirements`` into a typed requirement tree."""

    raw_vbot = metadata.get(REQUIREMENTS_METADATA_KEY)
    if raw_vbot is None:
        return SkillRequirements()
    if not isinstance(raw_vbot, dict):
        raise RequirementParseError("metadata.vbot must be a mapping")

    raw_requirements = raw_vbot.get("requirements")
    if raw_requirements is None:
        return SkillRequirements()
    if not isinstance(raw_requirements, dict):
        raise RequirementParseError("metadata.vbot.requirements must be a mapping")

    unknown_keys = set(raw_requirements) - {"all", "any", "binary", "env", "skill", "optional"}
    if unknown_keys:
        names = ", ".join(sorted(unknown_keys))
        raise RequirementParseError(f"metadata.vbot.requirements has unknown key(s): {names}")

    required_keys = [
        key for key in ("all", "any", "binary", "env", "skill") if key in raw_requirements
    ]
    if len(required_keys) > 1:
        names = ", ".join(required_keys)
        raise RequirementParseError(
            f"metadata.vbot.requirements must define only one required root, got: {names}"
        )

    required = None
    if required_keys:
        required_key = required_keys[0]
        required = _parse_requirement_value(
            required_key,
            raw_requirements[required_key],
            path=f"metadata.vbot.requirements.{required_key}",
        )

    optional = _parse_optional_requirements(raw_requirements.get("optional"))
    return SkillRequirements(required=required, optional=optional)


def _parse_optional_requirements(raw_optional: Any) -> tuple[RequirementNode, ...]:
    if raw_optional is None:
        return ()
    if not isinstance(raw_optional, list):
        raise RequirementParseError("metadata.vbot.requirements.optional must be a list")
    return tuple(
        _parse_requirement_node(item, path=f"metadata.vbot.requirements.optional[{index}]")
        for index, item in enumerate(raw_optional)
    )


def _parse_requirement_node(raw_node: Any, *, path: str) -> RequirementNode:
    if not isinstance(raw_node, dict):
        raise RequirementParseError(f"{path} must be a mapping")

    unknown_keys = set(raw_node) - {"all", "any", "binary", "env", "skill"}
    if unknown_keys:
        names = ", ".join(sorted(unknown_keys))
        raise RequirementParseError(f"{path} has unknown key(s): {names}")

    present_keys = [key for key in ("all", "any", "binary", "env", "skill") if key in raw_node]
    if len(present_keys) != 1:
        raise RequirementParseError(f"{path} must define exactly one requirement")

    key = present_keys[0]
    return _parse_requirement_value(key, raw_node[key], path=f"{path}.{key}")


def _parse_requirement_value(key: str, raw_value: Any, *, path: str) -> RequirementNode:
    if key in {"all", "any"}:
        return _parse_requirement_group(key, raw_value, path=path)
    if key in {"binary", "env", "skill"}:
        return _parse_requirement_check(key, raw_value, path=path)
    raise RequirementParseError(f"{path} has unsupported requirement key: {key}")


def _parse_requirement_group(key: str, raw_children: Any, *, path: str) -> RequirementGroup:
    if key not in {"all", "any"}:
        raise RequirementParseError(f"{path} has unsupported requirement group: {key}")
    if not isinstance(raw_children, list):
        raise RequirementParseError(f"{path} must be a list")
    if not raw_children:
        raise RequirementParseError(f"{path} must not be empty")
    operator = cast("RequirementGroupKind", key)
    return RequirementGroup(
        operator=operator,
        children=tuple(
            _parse_requirement_node(child, path=f"{path}[{index}]")
            for index, child in enumerate(raw_children)
        ),
    )


def _parse_requirement_check(key: str, raw_name: Any, *, path: str) -> RequirementCheck:
    if key not in {"binary", "env", "skill"}:
        raise RequirementParseError(f"{path} has unsupported requirement check: {key}")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise RequirementParseError(f"{path} must be a non-empty string")
    kind = cast("RequirementCheckKind", key)
    return RequirementCheck(kind=kind, name=raw_name.strip())
