"""Reading a Model's call syntax: spellings, placeholders, and owner-selected repairs.

Tool owners (built-in Tools, Extensions, the Live Tools) use this inside their
``argument_normalizer`` to accept the field names other harnesses use, to
recognize values a Model writes into optional fields it does not mean to use,
and to repair the call object they declared (:func:`normalize_call_arguments`).
Nothing here decides what a value means for a Tool: each owner selects the
fields, aliases, and words it accepts, and payload values stay unchanged.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any

from core.tools.contracts import ToolContract, ToolContractError, _load_json_value, _same_json_value


def spelling(value: str) -> str:
    """Return ``value`` without case, spaces, and punctuation: ``Agent-ID`` -> ``agentid``."""
    return re.sub(r"[\W_]+", "", value.casefold())


class SpellingAliases(Mapping[str, str]):
    """Field aliases that match regardless of case, spaces, and punctuation."""

    def __init__(self, fields: Mapping[str, Iterable[str]]) -> None:
        self._aliases = {
            spelling(alias): field for field, names in fields.items() for alias in names
        }

    def __getitem__(self, key: str) -> str:
        return self._aliases[spelling(key)]

    def __iter__(self) -> Iterator[str]:
        return iter(self._aliases)

    def __len__(self) -> int:
        return len(self._aliases)


# Words Models write into optional fields to mean "not used", compared by spelling.
PLACEHOLDER_WORDS = frozenset(
    {
        "blank",
        "empty",
        "invalidplaceholder",
        "na",
        "nil",
        "none",
        "notapplicable",
        "notset",
        "null",
        "omit",
        "omitted",
        "placeholder",
        "tbd",
        "undefined",
        "unset",
        "unused",
    }
)


def is_placeholder(value: Any, words: Iterable[str] = PLACEHOLDER_WORDS) -> bool:
    """Return whether ``value`` stands for an omitted optional field.

    ``None``, blank or punctuation-only text (``" "``, ``"."``, ``"???"``), and
    the given placeholder words (``"unused"``, ``"<none>"``, ``"__omit__"``)
    qualify. Any other text, including an unknown id, is a real value.
    """
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    text = spelling(value)
    return not text or text in words


def normalize_call_arguments(
    contract: ToolContract,
    arguments: Any,
    *,
    enum_fields: Sequence[str] = (),
    field_aliases: Mapping[str, str] | None = None,
    field_normalizers: Mapping[str, Callable[[Any], Any]] | None = None,
    empty_as_omitted: Sequence[str] = (),
    placeholder_as_omitted: Sequence[str] = (),
    wrapping_fields: Sequence[str] = (),
) -> Any:
    """Repair one owner-declared argument object, preserving every supplied instruction.

    Owners opt into field formatting and known call wrappers at this boundary.
    Field normalizers run on canonical field names before schema conversion and
    alias conflict checks. Enum formatting and empty-as-omission need explicit
    field selection, and so does ignoring a placeholder under one spelling of a
    field that another spelling gives a real value. A field in
    ``wrapping_fields`` whose value is an object of this call's own fields, or
    JSON text of one, wraps those fields; the owner selects only fields whose
    real values can never be such an object. Nested objects remain payloads;
    batch owners call this separately for each item.
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
        if _format_key(key) == "operation" and actions:
            return "action"
        return _formatted_name(key, list(properties)) or key

    def entries(obj: dict[str, Any], depth: int = 0) -> list[tuple[str, str, Any]]:
        """Return (field, key as sent, value) for every supplied value."""
        if depth > 8:
            raise ToolContractError("Too many nested call wrappers; provide one argument object.")
        result: list[tuple[str, str, Any]] = []
        for key, item in obj.items():
            if key not in properties and canonical_field(key) == key:
                action = _formatted_name(key, actions)
                if _format_key(key) in {"request", "arguments"} or action is not None:
                    nested = _load_json_value(item) if isinstance(item, str) else item
                    if isinstance(nested, dict) and (nested or action is not None):
                        result.extend(entries(nested, depth + 1))
                        if action is not None:
                            result.append(("action", key, action))
                        continue
            field = canonical_field(key)
            if field in wrapping_fields:
                wrapped = _argument_object(item)
                if wrapped and all(canonical_field(name) in properties for name in wrapped):
                    result.extend(entries(wrapped, depth + 1))
                    continue
            result.append((field, key, item))
        return result

    supplied = entries(value)
    # A placeholder beside a real value of the same field asks for nothing.
    meant = {
        field
        for field, _, item in supplied
        if field in placeholder_as_omitted and not is_placeholder(item)
    }
    normalized: dict[str, Any] = {}
    sent: dict[str, tuple[str, Any]] = {}
    for field, key, item in supplied:
        if field in meant and is_placeholder(item):
            continue
        given = item
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
            (first_key, first), second = sent[field], (key, given)
            raise ToolContractError(
                f"Conflicting values for {field}: {first_key} is {_shown(first)} and "
                f"{second[0]} is {_shown(second[1])}. Send only the intended one."
            )
        normalized[field] = item
        sent.setdefault(field, (key, given))
    # Check conflicts before omission: an explicit empty alias must not hide
    # contradictory nonempty data from another spelling or wrapper.
    for field in empty_as_omitted:
        if field in normalized and normalized[field] in (None, ""):
            del normalized[field]
    return normalized


def _argument_object(value: Any) -> dict[str, Any] | None:
    """Return ``value`` as an object when it is one or is JSON text of one."""
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            value = _load_json_value(value)
        except (ValueError, ToolContractError):
            return None
    return value if isinstance(value, dict) else None


def _shown(value: Any) -> str:
    """Quote a sent value as JSON, cut short enough to tell two values apart."""
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def _format_key(value: str) -> str:
    """``value`` without case, spaces, ``_`` and ``-``, for field and choice names."""
    return re.sub(r"[\s_-]+", "", value.casefold())


def _formatted_name(value: str, choices: Sequence[Any]) -> str | None:
    if value in choices:
        return value
    matches = [
        name
        for name in choices
        if isinstance(name, str) and _format_key(name) == _format_key(value)
    ]
    return matches[0] if len(matches) == 1 else None


__all__ = [
    "PLACEHOLDER_WORDS",
    "SpellingAliases",
    "is_placeholder",
    "normalize_call_arguments",
    "spelling",
]
