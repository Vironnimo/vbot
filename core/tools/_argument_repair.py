"""Owner-selected repairs for a Tool's call syntax, never arbitrary application data."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from core.tools.contracts import ToolContract, ToolContractError, _load_json_value, _same_json_value


def normalize_call_arguments(
    contract: ToolContract,
    arguments: Any,
    *,
    enum_fields: Sequence[str] = (),
    field_aliases: Mapping[str, str] | None = None,
    field_normalizers: Mapping[str, Callable[[Any], Any]] | None = None,
    empty_as_omitted: Sequence[str] = (),
) -> Any:
    """Repair one owner-declared argument object, preserving every supplied instruction.

    Owners opt into field formatting and known call wrappers at this boundary.
    Field normalizers run on canonical field names before schema conversion and
    alias conflict checks. Enum formatting and empty-as-omission need explicit
    field selection. Nested objects remain payloads; batch owners call this
    separately for each item.
    No edit-distance matching, identifier correction, or schema-score guessing.
    """
    value = copy.deepcopy(arguments)
    if isinstance(value, str):
        try:
            value = _load_json_value(value)
        except ValueError as error:
            raise ToolContractError("Provide one JSON argument object.") from error
    if not isinstance(value, dict):
        return value
    properties = contract.input_schema.get("properties", {})
    aliases = field_aliases or {}
    actions = properties.get("action", {}).get("enum", []) if "action" in enum_fields else []

    def canonical_field(key: str) -> str:
        if key in properties:
            return key
        if key in aliases:
            return aliases[key]
        if _spelling(key) == "operation" and actions:
            return "action"
        return _formatted_name(key, list(properties)) or key

    def entries(obj: dict[str, Any], depth: int = 0) -> list[tuple[str, Any]]:
        if depth > 8:
            raise ToolContractError("Too many nested call wrappers; provide one argument object.")
        result: list[tuple[str, Any]] = []
        for key, item in obj.items():
            if key not in properties and canonical_field(key) == key:
                action = _formatted_name(key, actions)
                if _spelling(key) in {"request", "arguments"} or action is not None:
                    nested = _load_json_value(item) if isinstance(item, str) else item
                    if isinstance(nested, dict) and (nested or action is not None):
                        result.extend(entries(nested, depth + 1))
                        if action is not None:
                            result.append(("action", action))
                        continue
            result.append((canonical_field(key), item))
        return result

    normalized: dict[str, Any] = {}
    for field, item in entries(value):
        if field_normalizers and field in field_normalizers:
            item = field_normalizers[field](item)
        if field in enum_fields and isinstance(item, str):
            options = properties.get(field, {}).get("enum", [])
            item = _formatted_name(item, options) or item
        repaired = contract.normalize_arguments({field: item})
        if field not in repaired:
            # An empty value under a name that is not a parameter requests nothing.
            continue
        item = repaired[field]
        if field in normalized and not _same_json_value(normalized[field], item):
            raise ToolContractError(f"Conflicting values for {field}; provide one intended value.")
        normalized[field] = item
    # Check conflicts before omission: an explicit empty alias must not hide
    # contradictory nonempty data from another spelling or wrapper.
    for field in empty_as_omitted:
        if field in normalized and normalized[field] in (None, ""):
            del normalized[field]
    return normalized


def _spelling(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value.casefold())


def _formatted_name(value: str, choices: Sequence[Any]) -> str | None:
    if value in choices:
        return value
    matches = [
        name for name in choices if isinstance(name, str) and _spelling(name) == _spelling(value)
    ]
    return matches[0] if len(matches) == 1 else None
