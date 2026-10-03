"""Wire profile files: schema, parsing and validation.

A wire profile file (``resources/wire/<provider>.json``, or the ``wire`` block of
a Custom Provider) holds *partial profiles*: nested objects whose keys mirror the
fields of :class:`core.providers.wire_profile.WireProfile`. This module checks
every partial profile against one schema and turns a file or block into an
immutable :class:`WireProfileFile`. Invalid values, rules or Model entries are
reported and omitted individually; an invalid top-level shape rejects the whole
file or block.

Merging semantics live in :mod:`core.providers.wire_profiles`; this
module only decides what is well formed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from core.providers.reasoning import (
    REASONING_REPLAY_FIDELITIES,
    REASONING_REPLAY_POLICIES,
    THINKING_EFFORT_ORDER,
)
from core.providers.wire_profile import (
    ADMISSION_STATES,
    BUDGET_STRATEGIES,
    OFF_RENDERS,
    OUT_OF_RANGE_POLICIES,
    OUTPUT_LIMIT_FIELDS,
    PARAMETER_MODES,
    PROMPT_CACHE_STYLES,
    PROTOCOLS,
    REASONING_DIALECTS,
    SNAP_RULES,
    TOOL_CALL_ID_PROFILES,
    TOOL_SCHEMA_PROFILES,
    UNSET_RENDERS,
    Verification,
)

WIRE_PROFILE_FORMAT_VERSION = 1
WIRE_PROFILE_DIR_NAME = "wire"

WireIssueReport = Callable[[str], None]
"""Receives one human-readable message per ignored file, rule, entry or value."""

PartialProfile = Mapping[str, Any]
"""A validated, frozen nested mapping shaped like a (partial) ``WireProfile``."""

_EMPTY: Mapping[str, Any] = MappingProxyType({})


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
#
# A schema node is one of:
#   _Leaf(check)        - a scalar or list value validated by ``check``
#   dict[str, node]     - an object with known keys (merged key by key)
#   _MapOf(node)        - an object with free keys whose values follow ``node``
#   _Json()             - any JSON value, replaced as a whole when merged


@dataclass(frozen=True)
class _Leaf:
    check: Callable[[Any], bool]
    expected: str


@dataclass(frozen=True)
class _MapOf:
    value: Any
    reserved: Mapping[str, str] = field(default_factory=dict)
    """Keys the map must not carry, each with the reason reported when it does."""


_TRANSPORT_OWNED_BODY_KEYS: Mapping[str, str] = {
    "stream": "the request path decides streaming",
}
"""Request body keys that ``body_defaults`` and ``extra_body`` cannot set."""


@dataclass(frozen=True)
class _Json:
    pass


def _enum(values: tuple[str, ...], *, nullable: bool = False) -> _Leaf:
    def check(value: Any) -> bool:
        return (nullable and value is None) or (isinstance(value, str) and value in values)

    expected = "one of " + ", ".join(values) + (" or null" if nullable else "")
    return _Leaf(check, expected)


def _bool() -> _Leaf:
    return _Leaf(lambda value: isinstance(value, bool), "a boolean")


def _string(*, nullable: bool = False) -> _Leaf:
    return _Leaf(
        lambda value: (nullable and value is None) or (isinstance(value, str) and bool(value)),
        "a non-empty string" + (" or null" if nullable else ""),
    )


def _positive_int(*, nullable: bool = True) -> _Leaf:
    return _Leaf(
        lambda value: (
            (nullable and value is None)
            or (isinstance(value, int) and not isinstance(value, bool) and value > 0)
        ),
        "a positive integer" + (" or null" if nullable else ""),
    )


def _number(*, nullable: bool = True) -> _Leaf:
    return _Leaf(
        lambda value: (
            (nullable and value is None)
            or (isinstance(value, int | float) and not isinstance(value, bool))
        ),
        "a number" + (" or null" if nullable else ""),
    )


def _string_list(*, values: tuple[str, ...] | None = None, nullable: bool = False) -> _Leaf:
    def check(value: Any) -> bool:
        if nullable and value is None:
            return True
        if not isinstance(value, list):
            return False
        return all(
            isinstance(item, str) and bool(item) and (values is None or item in values)
            for item in value
        )

    expected = "a list of strings" if values is None else "a list of " + ", ".join(values)
    return _Leaf(check, expected + (" or null" if nullable else ""))


def _scalar_list() -> _Leaf:
    def check(value: Any) -> bool:
        return (
            isinstance(value, list)
            and bool(value)
            and all(item is None or isinstance(item, str | int | float | bool) for item in value)
        )

    return _Leaf(check, "a non-empty list of JSON scalars")


def _parameter_groups() -> _Leaf:
    def check(value: Any) -> bool:
        return isinstance(value, list) and all(
            isinstance(group, list)
            and len(group) >= 2
            and len(set(group)) == len(group)
            and all(isinstance(name, str) and bool(name) for name in group)
            for group in value
        )

    return _Leaf(check, "a list of groups of two or more distinct parameter names")


_EFFORT_LEVELS = tuple(THINKING_EFFORT_ORDER)


def _off_render() -> _Leaf:
    allowed = OFF_RENDERS + _EFFORT_LEVELS
    return _Leaf(
        lambda value: isinstance(value, str) and value in allowed, "one of " + ", ".join(allowed)
    )


def _unset_render() -> _Leaf:
    allowed = UNSET_RENDERS + _EFFORT_LEVELS
    return _Leaf(
        lambda value: isinstance(value, str) and value in allowed, "one of " + ", ".join(allowed)
    )


_PARAMETER_RULE: dict[str, Any] = {
    "mode": _enum(PARAMETER_MODES),
    "minimum": _number(),
    "maximum": _number(),
    "exclusive_minimum": _bool(),
    "out_of_range": _enum(OUT_OF_RANGE_POLICIES),
    "values": _scalar_list(),
}

PROFILE_SCHEMA: dict[str, Any] = {
    "protocol": _enum(PROTOCOLS),
    "admission": {
        "state": _enum(ADMISSION_STATES),
        "message": _string(nullable=True),
    },
    "request": {
        "output_limit_field": _enum(OUTPUT_LIMIT_FIELDS, nullable=True),
        "output_limit_default": _positive_int(),
        "output_limit_cap": _positive_int(),
        "output_limit_collapse": _bool(),
        "allowed_parameters": _string_list(nullable=True),
        "parameters": _MapOf(_PARAMETER_RULE),
        "exclusive_parameters": _parameter_groups(),
        "body_defaults": _MapOf(_Json(), _TRANSPORT_OWNED_BODY_KEYS),
        "extra_body": _MapOf(_Json(), _TRANSPORT_OWNED_BODY_KEYS),
        "extra_headers": _MapOf(_string()),
        "tool_schema": _enum(TOOL_SCHEMA_PROFILES),
        "tool_call_ids": _enum(TOOL_CALL_ID_PROFILES),
        "list_announced_tools": _bool(),
        "prompt_cache": _enum(PROMPT_CACHE_STYLES),
        "options": _MapOf(_Json()),
    },
    "reasoning": {
        "dialect": _enum(REASONING_DIALECTS),
        "supported": _Leaf(
            lambda value: value is None or isinstance(value, bool), "a boolean or null"
        ),
        "control": _enum(("levels", "on_off", "budget"), nullable=True),
        "levels": _string_list(values=_EFFORT_LEVELS, nullable=True),
        "floor": _string_list(values=_EFFORT_LEVELS),
        "effort_map": _MapOf(
            _Leaf(lambda value: isinstance(value, str) and bool(value), "a wire level")
        ),
        "snap": _enum(SNAP_RULES),
        "off": _off_render(),
        "unset": _unset_render(),
        "mandatory": _bool(),
        "budget_max": _positive_int(),
        "budget": {
            "strategy": _enum(BUDGET_STRATEGIES),
            "minimum": _positive_int(nullable=False),
            "maximum": _positive_int(),
        },
        "options": _MapOf(_Json()),
    },
    "response": {
        "reasoning_fields": _string_list(),
        "options": _MapOf(_Json()),
    },
    "replay": {
        "scope": _enum(REASONING_REPLAY_POLICIES),
        "fidelity": _enum(REASONING_REPLAY_FIDELITIES),
        "history_field": _string(nullable=True),
        "echo_response_field": _bool(),
        "echo_empty_on_tool_calls": _bool(),
        "strip_when_off": _bool(),
    },
    "media": {
        "types": _string_list(),
        "image_max_bytes": _positive_int(),
        "request_max_bytes": _positive_int(),
    },
}

_EFFORT_MAP_KEYS = frozenset(_EFFORT_LEVELS)


def is_opaque(node: Any) -> bool:
    """Whether a merged value at this schema node is replaced as a whole."""

    return isinstance(node, _Leaf | _Json)


def child_node(node: Any, key: str) -> Any:
    """Return the schema node of ``key`` inside an object or map node (``None`` if unknown)."""

    if isinstance(node, _MapOf):
        return node.value
    if isinstance(node, Mapping):
        return node.get(key)
    return None


def validate_partial_profile(
    raw: Any,
    *,
    where: str,
    report: WireIssueReport,
    allow_protocol: bool = True,
    protocols: Sequence[str] | None = None,
) -> PartialProfile:
    """Validate one partial profile, dropping (and reporting) every invalid value.

    ``protocols`` limits a ``protocol`` value to the protocols the Provider's
    Adapter speaks (any known protocol when ``None``).
    """

    if not isinstance(raw, Mapping):
        report(f"{where}: expected an object, ignoring it")
        return _EMPTY
    schema: Mapping[str, Any] = PROFILE_SCHEMA
    if not allow_protocol:
        schema = {k: v for k, v in PROFILE_SCHEMA.items() if k != "protocol"}
    elif protocols is not None:
        spoken = tuple(protocols)
        schema = {
            **PROFILE_SCHEMA,
            "protocol": _Leaf(
                lambda value: isinstance(value, str) and value in spoken,
                "a protocol this Provider's Adapter speaks (" + ", ".join(spoken) + ")",
            ),
        }
    if not allow_protocol and "protocol" in raw:
        report(f"{where}: 'protocol' is not allowed here, ignoring it")
    return _validate_object(raw, schema, where=where, report=report)


def _validate_object(
    raw: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    where: str,
    report: WireIssueReport,
) -> Mapping[str, Any]:
    result: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            report(f"{where}: ignoring non-string key {key!r}")
            continue
        if key.startswith("_"):
            continue  # comments such as "_note"
        node = schema.get(key)
        path = f"{where}.{key}"
        if node is None:
            report(f"{path}: unknown field, ignoring it")
            continue
        validated = _validate_node(value, node, where=path, report=report)
        if validated is not _INVALID:
            result[key] = validated
    return MappingProxyType(result)


_INVALID = object()


def _validate_node(value: Any, node: Any, *, where: str, report: WireIssueReport) -> Any:
    if isinstance(node, _Leaf):
        if not node.check(value):
            report(f"{where}: expected {node.expected}, ignoring {value!r}")
            return _INVALID
        return tuple(value) if isinstance(value, list) else value
    if isinstance(node, _Json):
        return _freeze_json(value)
    if isinstance(node, _MapOf):
        if not isinstance(value, Mapping):
            report(f"{where}: expected an object, ignoring it")
            return _INVALID
        entries: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                report(f"{where}: ignoring invalid key {key!r}")
                continue
            if key.startswith("_"):
                continue
            if key in node.reserved:
                report(f"{where}.{key}: {node.reserved[key]}, ignoring it")
                continue
            if where.endswith(".effort_map") and key not in _EFFORT_MAP_KEYS:
                report(f"{where}.{key}: not an effort level, ignoring it")
                continue
            validated = _validate_node(item, node.value, where=f"{where}.{key}", report=report)
            if validated is not _INVALID:
                entries[key] = validated
        return MappingProxyType(entries)
    if isinstance(node, dict):
        if not isinstance(value, Mapping):
            report(f"{where}: expected an object, ignoring it")
            return _INVALID
        return _validate_object(value, node, where=where, report=report)
    raise TypeError(f"unknown schema node at {where}")  # pragma: no cover - schema bug


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze_json(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


# ---------------------------------------------------------------------------
# Rules and Model entries
# ---------------------------------------------------------------------------

_MATCHER_KEYS = frozenset(
    {"ids", "prefix", "suffix", "family", "npm", "metadata", "unknown", "control", "connections"}
)


@dataclass(frozen=True)
class RuleMatcher:
    """Conditions of one rule; every given condition must hold (AND).

    - ``ids``: the Model id is one of these exact wire ids.
    - ``prefix`` / ``suffix``: the Model id starts / ends with one of these.
    - ``family``: the catalog family is one of these.
    - ``npm``: the catalog protocol hint (models.dev package) is one of these.
    - ``metadata``: provider-reported catalog facts (``Model.metadata[<provider
      key>]``) equal these values; ``{"contains": x}`` matches a list holding x.
    - ``unknown``: the Model id is (``true``) or is not (``false``) in the catalog.
    - ``control``: the catalog reasoning control is one of these (``"none"``
      matches a Model without a control).
    - ``connections``: the profile is resolved for one of these Connections.
    """

    ids: frozenset[str] | None = None
    prefix: tuple[str, ...] | None = None
    suffix: tuple[str, ...] | None = None
    family: frozenset[str] | None = None
    npm: frozenset[str] | None = None
    metadata: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)
    unknown: bool | None = None
    control: frozenset[str] | None = None
    connections: frozenset[str] | None = None


@dataclass(frozen=True)
class WireRule:
    """A partial profile applied to every Model its matcher accepts."""

    index: int
    when: RuleMatcher
    values: PartialProfile
    note: str = ""


@dataclass(frozen=True)
class WireModelEntry:
    """Explicit profile data for one exact Model id."""

    values: PartialProfile = field(default_factory=lambda: _EMPTY)
    connections: Mapping[str, PartialProfile] = field(default_factory=lambda: _EMPTY)
    verification: Verification | None = None
    note: str = ""


@dataclass(frozen=True)
class WireProfileFile:
    """One Provider's validated wire profile data."""

    provider_id: str
    defaults: PartialProfile = field(default_factory=lambda: _EMPTY)
    protocols: Mapping[str, PartialProfile] = field(default_factory=lambda: _EMPTY)
    connections: Mapping[str, PartialProfile] = field(default_factory=lambda: _EMPTY)
    rules: tuple[WireRule, ...] = ()
    models: Mapping[str, WireModelEntry] = field(default_factory=lambda: _EMPTY)


_BODY_FIELDS = frozenset({"defaults", "protocols", "connections", "rules", "models"})


@dataclass(frozen=True)
class _Limits:
    """What one Provider offers: its Connection ids and its Adapter's protocols.

    ``None`` accepts any id or known protocol: the Runtime loads bundled files
    without limits, and a test loads them with their Provider's limits.
    """

    connection_ids: frozenset[str] | None = None
    protocols: tuple[str, ...] | None = None

    def unknown_connections(self, connection_ids: Collection[str]) -> list[str]:
        if self.connection_ids is None:
            return []
        return sorted(item for item in connection_ids if item not in self.connection_ids)

    def describe_connections(self) -> str:
        return ", ".join(sorted(self.connection_ids or ()))


_NO_LIMITS = _Limits()


def parse_wire_profile_file(
    provider_id: str,
    raw: Any,
    *,
    source: str,
    report: WireIssueReport,
    connection_ids: Collection[str] | None = None,
    protocols: Sequence[str] | None = None,
) -> WireProfileFile | None:
    """Validate a decoded wire profile document; ``None`` rejects the whole file.

    ``connection_ids`` and ``protocols`` limit the file to its Provider's
    Connections and the protocols its Adapter speaks, as for a block.
    """

    if not isinstance(raw, Mapping):
        report(f"{source}: expected a JSON object, ignoring the file")
        return None
    version = raw.get("format_version")
    if version != WIRE_PROFILE_FORMAT_VERSION:
        report(f"{source}: unsupported format_version {version!r}, ignoring the file")
        return None
    return _parse_body(
        provider_id,
        raw,
        source=source,
        report=report,
        separator=":",
        known=_BODY_FIELDS | {"format_version"},
        limits=_Limits(
            connection_ids=frozenset(connection_ids) if connection_ids is not None else None,
            protocols=tuple(protocols) if protocols is not None else None,
        ),
    )


def parse_wire_profile_block(
    provider_id: str,
    raw: Any,
    *,
    source: str,
    report: WireIssueReport,
    connection_ids: Collection[str] | None = None,
    protocols: Sequence[str] | None = None,
) -> WireProfileFile | None:
    """Validate a wire block embedded in another document; ``None`` rejects it.

    A block has the body of a wire profile file without ``format_version``:
    the enclosing document (``settings.json`` for a Custom Provider) carries
    the version. ``connection_ids`` and ``protocols`` limit the block to the
    Provider's Connections and the protocols its Adapter speaks; values naming
    anything else are reported and omitted like every other invalid value.
    Messages name a field as ``<source>.<path>`` (``wire.defaults.reasoning``).
    """

    if not isinstance(raw, Mapping):
        report(f"{source}: expected a JSON object, ignoring the block")
        return None
    if "format_version" in raw:
        report(
            f"{source}.format_version: a wire block has no format_version (only wire profile "
            "files do), ignoring it"
        )
        raw = {key: value for key, value in raw.items() if key != "format_version"}
    return _parse_body(
        provider_id,
        raw,
        source=source,
        report=report,
        separator=".",
        known=_BODY_FIELDS,
        limits=_Limits(
            connection_ids=frozenset(connection_ids) if connection_ids is not None else None,
            protocols=tuple(protocols) if protocols is not None else None,
        ),
    )


def _parse_body(
    provider_id: str,
    raw: Mapping[str, Any],
    *,
    source: str,
    report: WireIssueReport,
    separator: str,
    known: frozenset[str],
    limits: _Limits,
) -> WireProfileFile:
    prefix = f"{source}{separator}"
    for key in raw:
        if isinstance(key, str) and not key.startswith("_") and key not in known:
            report(f"{source}: unknown top-level field {key!r}, ignoring it")

    defaults = validate_partial_profile(
        raw.get("defaults", {}),
        where=f"{prefix}defaults",
        report=report,
        protocols=limits.protocols,
    )

    protocols: dict[str, PartialProfile] = {}
    raw_protocols = raw.get("protocols", {})
    if isinstance(raw_protocols, Mapping):
        for name, partial in raw_protocols.items():
            if name not in PROTOCOLS:
                report(f"{prefix}protocols.{name}: unknown protocol, ignoring it")
                continue
            if limits.protocols is not None and name not in limits.protocols:
                report(
                    f"{prefix}protocols.{name}: not spoken by this Provider's Adapter "
                    f"({', '.join(limits.protocols)}), ignoring it"
                )
                continue
            protocols[name] = validate_partial_profile(
                partial, where=f"{prefix}protocols.{name}", report=report, allow_protocol=False
            )
    else:
        report(f"{prefix}protocols: expected an object, ignoring it")

    connections = _parse_connection_map(
        raw.get("connections", {}), where=f"{prefix}connections", report=report, limits=limits
    )

    rules: list[WireRule] = []
    raw_rules = raw.get("rules", [])
    if isinstance(raw_rules, list):
        for index, raw_rule in enumerate(raw_rules):
            rule = _parse_rule(
                index, raw_rule, where=f"{prefix}rules[{index}]", report=report, limits=limits
            )
            if rule is not None:
                rules.append(rule)
    else:
        report(f"{prefix}rules: expected a list, ignoring it")

    models: dict[str, WireModelEntry] = {}
    raw_models = raw.get("models", {})
    if isinstance(raw_models, Mapping):
        for model_id, raw_entry in raw_models.items():
            if not isinstance(model_id, str) or not model_id or model_id.startswith("_"):
                continue
            entry = _parse_model_entry(
                raw_entry, where=f"{prefix}models.{model_id}", report=report, limits=limits
            )
            if entry is not None:
                models[model_id] = entry
    else:
        report(f"{prefix}models: expected an object, ignoring it")

    return WireProfileFile(
        provider_id=provider_id,
        defaults=defaults,
        protocols=MappingProxyType(protocols),
        connections=connections,
        rules=tuple(rules),
        models=MappingProxyType(models),
    )


def _parse_connection_map(
    raw: Any, *, where: str, report: WireIssueReport, limits: _Limits
) -> Mapping[str, PartialProfile]:
    if not isinstance(raw, Mapping):
        report(f"{where}: expected an object, ignoring it")
        return _EMPTY
    result: dict[str, PartialProfile] = {}
    for connection_id, partial in raw.items():
        if not isinstance(connection_id, str) or not connection_id or connection_id.startswith("_"):
            continue
        if limits.unknown_connections((connection_id,)):
            report(
                f"{where}.{connection_id}: unknown Connection (this Provider has "
                f"{limits.describe_connections()}), ignoring it"
            )
            continue
        result[connection_id] = validate_partial_profile(
            partial, where=f"{where}.{connection_id}", report=report, protocols=limits.protocols
        )
    return MappingProxyType(result)


def _parse_rule(
    index: int, raw: Any, *, where: str, report: WireIssueReport, limits: _Limits
) -> WireRule | None:
    if not isinstance(raw, Mapping):
        report(f"{where}: expected an object, ignoring the rule")
        return None
    matcher = _parse_matcher(raw.get("when"), where=f"{where}.when", report=report)
    if matcher is None:
        return None
    if matcher.connections is not None:
        unknown = limits.unknown_connections(matcher.connections)
        if unknown:
            report(
                f"{where}.when.connections: unknown Connection(s) {unknown} (this Provider "
                f"has {limits.describe_connections()}), ignoring the rule"
            )
            return None
    values = validate_partial_profile(
        raw.get("set", {}), where=f"{where}.set", report=report, protocols=limits.protocols
    )
    note = raw.get("note", "")
    return WireRule(
        index=index, when=matcher, values=values, note=note if isinstance(note, str) else ""
    )


def _string_set(value: Any) -> tuple[str, ...] | None:
    if isinstance(value, str) and value:
        return (value,)
    if isinstance(value, list) and value and all(isinstance(item, str) and item for item in value):
        return tuple(value)
    return None


def _parse_matcher(raw: Any, *, where: str, report: WireIssueReport) -> RuleMatcher | None:
    if not isinstance(raw, Mapping) or not raw:
        report(f"{where}: expected a non-empty object, ignoring the rule")
        return None
    unknown_keys = [key for key in raw if key not in _MATCHER_KEYS]
    if unknown_keys:
        report(f"{where}: unknown condition(s) {sorted(map(str, unknown_keys))}, ignoring the rule")
        return None
    values: dict[str, Any] = {}
    for key in ("ids", "prefix", "suffix", "family", "npm", "control", "connections"):
        if key not in raw:
            continue
        parsed = _string_set(raw[key])
        if parsed is None:
            report(f"{where}.{key}: expected a string or a list of strings, ignoring the rule")
            return None
        values[key] = parsed if key in ("prefix", "suffix") else frozenset(parsed)
    if "metadata" in raw:
        metadata = raw["metadata"]
        if not isinstance(metadata, Mapping) or not metadata:
            report(f"{where}.metadata: expected a non-empty object, ignoring the rule")
            return None
        values["metadata"] = _freeze_json(metadata)
    if "unknown" in raw:
        if not isinstance(raw["unknown"], bool):
            report(f"{where}.unknown: expected a boolean, ignoring the rule")
            return None
        values["unknown"] = raw["unknown"]
    return RuleMatcher(**values)


def _parse_model_entry(
    raw: Any, *, where: str, report: WireIssueReport, limits: _Limits
) -> WireModelEntry | None:
    if not isinstance(raw, Mapping):
        report(f"{where}: expected an object, ignoring the entry")
        return None
    known = {"set", "connections", "verified", "note"}
    for key in raw:
        if isinstance(key, str) and not key.startswith("_") and key not in known:
            report(f"{where}: unknown field {key!r}, ignoring it")
    values = validate_partial_profile(
        raw.get("set", {}), where=f"{where}.set", report=report, protocols=limits.protocols
    )
    connections = _parse_connection_map(
        raw.get("connections", {}), where=f"{where}.connections", report=report, limits=limits
    )
    verification = _parse_verification(
        raw.get("verified"), where=f"{where}.verified", report=report, limits=limits
    )
    note = raw.get("note", "")
    return WireModelEntry(
        values=values,
        connections=connections,
        verification=verification,
        note=note if isinstance(note, str) else "",
    )


def _parse_verification(
    raw: Any, *, where: str, report: WireIssueReport, limits: _Limits
) -> Verification | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        report(f"{where}: expected an object, ignoring it")
        return None
    date = raw.get("date")
    if not isinstance(date, str) or len(date) != 10 or date[4] != "-" or date[7] != "-":
        report(f"{where}.date: expected YYYY-MM-DD, ignoring the verification")
        return None
    connections = raw.get("connections", [])
    if not isinstance(connections, list) or not all(
        isinstance(item, str) and item for item in connections
    ):
        report(f"{where}.connections: expected a list of Connection ids, ignoring the verification")
        return None
    unknown = limits.unknown_connections(connections)
    if unknown:
        report(
            f"{where}.connections: unknown Connection(s) {unknown} (this Provider has "
            f"{limits.describe_connections()}), ignoring the verification"
        )
        return None
    evidence = raw.get("evidence", "")
    if not isinstance(evidence, str):
        report(f"{where}.evidence: expected a string, ignoring it")
        evidence = ""
    return Verification(date=date, connections=tuple(connections), evidence=evidence)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_wire_profile_files(
    resources_dir: Path,
    *,
    report: WireIssueReport,
    limits: Callable[[str], tuple[Collection[str] | None, Sequence[str] | None]] | None = None,
) -> dict[str, WireProfileFile]:
    """Load every bundled ``resources/wire/<provider>.json`` file.

    ``limits(provider_id)`` returns the Provider's ``(connection_ids,
    protocols)`` to validate each file against; ``None`` checks neither.
    """

    directory = resources_dir / WIRE_PROFILE_DIR_NAME
    files: dict[str, WireProfileFile] = {}
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError as exc:
        report(f"{directory}: cannot list wire profile files ({exc})")
        return files
    for path in paths:
        provider_id = path.stem
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            report(f"{path.name}: cannot read wire profile file ({exc}), ignoring it")
            continue
        connection_ids, protocols = limits(provider_id) if limits is not None else (None, None)
        parsed = parse_wire_profile_file(
            provider_id,
            raw,
            source=path.name,
            report=report,
            connection_ids=connection_ids,
            protocols=protocols,
        )
        if parsed is not None:
            files[provider_id] = parsed
    return files
