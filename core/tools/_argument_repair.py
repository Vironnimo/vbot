"""Shared argument repair: read Model-sent argument encodings as their schema intends.

``ToolContract.normalize_arguments`` runs this on a copy of every call's
arguments: numbers and booleans sent as text, JSON containers sent as text
(also with unescaped quotes or raw line breaks inside the text of declared
fields), a single value where an array is expected. A value with more than
one valid reading raises ``ToolContractError`` instead of choosing one. The
rules are in ``tools.md`` -> Shared argument repair and Quote repair of JSON
text.
"""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from core.tools._contract_error import ToolContractError
from core.utils.json_text import escape_inner_quotes

JsonObject = dict[str, Any]
_JSON_NUMBER_PATTERN = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")
_JSON_WHITESPACE = re.compile(r"[ \t\n\r]*")
# What can follow a complete value read from argument text: whitespace and
# closing brackets left over from the call's own nesting, which close nothing.
_STRAY_CLOSERS = re.compile(r"[ \t\n\r\]}]*")
_MAX_FLOAT_DECIMAL_EXPONENT = 308
_MIN_FLOAT_DECIMAL_EXPONENT = -324


def normalize_schema_value(
    value: Any,
    schema: Any,
    *,
    root_schema: JsonObject,
    root_validator: Draft202012Validator,
) -> Any:
    """Repair equivalent encodings, rejecting distinct valid interpretations."""
    if not isinstance(schema, dict):
        return value

    validator = root_validator.evolve(schema=schema)
    resolved = _resolve_schema_reference(schema, root_schema)
    # Repair declared value encodings without reinterpreting object keys.
    # Open application payloads retain their supplied field names.
    if isinstance(value, dict):
        value = _normalize_object_value(
            value, resolved, root_schema=root_schema, root_validator=root_validator
        )
    elif isinstance(value, list) and "array" in _declared_json_types(resolved):
        value = _normalize_array_value(
            value, resolved, root_schema=root_schema, root_validator=root_validator
        )
    if (
        "integer" in _declared_json_types(resolved)
        and isinstance(value, float)
        and math.isfinite(value)
        and value.is_integer()
    ):
        value = int(value)
    if validator.is_valid(value):
        return value

    candidates: list[Any] = []
    if resolved is not schema:
        candidates.append(
            normalize_schema_value(
                value,
                resolved,
                root_schema=root_schema,
                root_validator=root_validator,
            )
        )

    candidates.extend(
        _direct_normalization_candidates(
            value,
            resolved,
            root_schema=root_schema,
            root_validator=root_validator,
        )
    )

    for keyword in ("oneOf", "anyOf"):
        branches = resolved.get(keyword)
        if not isinstance(branches, list):
            continue
        for branch in branches:
            candidates.append(
                normalize_schema_value(
                    value,
                    branch,
                    root_schema=root_schema,
                    root_validator=root_validator,
                )
            )

    all_of = resolved.get("allOf")
    if isinstance(all_of, list):
        combined = value
        for branch in all_of:
            combined = normalize_schema_value(
                combined,
                branch,
                root_schema=root_schema,
                root_validator=root_validator,
            )
        candidates.append(combined)

    candidates.append(value)
    valid = [candidate for candidate in candidates if validator.is_valid(candidate)]
    if valid:
        first = valid[0]
        if any(not same_json_value(first, candidate) for candidate in valid[1:]):
            raise ToolContractError("Ambiguous argument encoding; provide the intended JSON value.")
        return first
    return min(candidates, key=lambda candidate: _validation_score(validator, candidate))


def _direct_normalization_candidates(
    value: Any,
    schema: JsonObject,
    *,
    root_schema: JsonObject,
    root_validator: Draft202012Validator,
) -> list[Any]:
    declared_types = _declared_json_types(schema)
    candidates: list[Any] = []

    if isinstance(value, str):
        text = value.strip()
        if "null" in declared_types and text.lower() == "null":
            candidates.append(None)
        if "integer" in declared_types:
            integer = _coerce_numeric_string(text, integer_only=True)
            if integer is not None:
                candidates.append(integer)
        if "number" in declared_types:
            number = _coerce_numeric_string(text, integer_only=False)
            if number is not None:
                candidates.append(number)
        if "boolean" in declared_types:
            boolean = _coerce_boolean_string(text)
            if boolean is not None:
                candidates.append(boolean)
        if "array" in declared_types:
            candidates.append(
                _normalize_array_value(
                    value,
                    schema,
                    root_schema=root_schema,
                    root_validator=root_validator,
                )
            )
        if "object" in declared_types:
            object_value = _normalize_object_string(
                value,
                schema,
                root_schema=root_schema,
                root_validator=root_validator,
            )
            if object_value is not None:
                candidates.append(object_value)

    if "boolean" in declared_types and isinstance(value, (int, float)) and value in (0, 1):
        candidates.append(bool(value))
    if "string" in declared_types and isinstance(value, int) and not isinstance(value, bool):
        candidates.append(str(value))

    if "array" in declared_types and isinstance(value, list):
        candidates.append(
            _normalize_array_value(
                value,
                schema,
                root_schema=root_schema,
                root_validator=root_validator,
            )
        )
    if "array" in declared_types and value is not None and not isinstance(value, list):
        item_schema = schema.get("items")
        # XML-shaped lists of text have one `item` field. Never unwrap business
        # objects or arrays whose items could themselves be objects.
        if (
            isinstance(value, dict)
            and set(value) == {"item"}
            and isinstance(item_schema, dict)
            and _declared_json_types(_resolve_schema_reference(item_schema, root_schema))
            == ("string",)
            and "prefixItems" not in schema
        ):
            items = value["item"]
            if isinstance(items, str) or (
                isinstance(items, list) and all(isinstance(item, str) for item in items)
            ):
                candidates.append(
                    _normalize_array_value(
                        [items] if isinstance(items, str) else items,
                        schema,
                        root_schema=root_schema,
                        root_validator=root_validator,
                    )
                )
        candidates.append(
            _normalize_array_value(
                value,
                schema,
                root_schema=root_schema,
                root_validator=root_validator,
            )
        )
    if "object" in declared_types and isinstance(value, dict):
        candidates.append(
            _normalize_object_value(
                value,
                schema,
                root_schema=root_schema,
                root_validator=root_validator,
            )
        )

    return candidates


def _declared_json_types(schema: JsonObject) -> tuple[str, ...]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return (declared,)
    if isinstance(declared, list):
        return tuple(item for item in declared if isinstance(item, str))
    if isinstance(schema.get("properties"), dict):
        return ("object",)
    if "items" in schema or "prefixItems" in schema:
        return ("array",)
    return ()


def _coerce_numeric_string(text: str, *, integer_only: bool) -> int | float | None:
    if _JSON_NUMBER_PATTERN.fullmatch(text) is None:
        return None
    try:
        decimal = Decimal(text)
    except InvalidOperation:
        return None
    if not decimal.is_finite():
        return None
    if "e" in text.lower() and not (
        _MIN_FLOAT_DECIMAL_EXPONENT <= decimal.adjusted() <= _MAX_FLOAT_DECIMAL_EXPONENT
    ):
        return None

    if integer_only:
        if decimal != decimal.to_integral_value():
            return None
        try:
            return int(decimal)
        except OverflowError, ValueError:
            return None

    if decimal == decimal.to_integral_value():
        try:
            return int(decimal)
        except OverflowError, ValueError:
            return None
    if not (_MIN_FLOAT_DECIMAL_EXPONENT <= decimal.adjusted() <= _MAX_FLOAT_DECIMAL_EXPONENT):
        return None
    number = float(decimal)
    if not math.isfinite(number) or (number == 0.0 and decimal != 0):
        return None
    return number


def _coerce_boolean_string(text: str) -> bool | None:
    normalized = text.lower()
    if normalized in {"true", "yes", "1", "on"}:
        return True
    if normalized in {"false", "no", "0", "off"}:
        return False
    return None


def _normalize_array_value(
    value: Any,
    schema: JsonObject,
    *,
    root_schema: JsonObject,
    root_validator: Draft202012Validator,
) -> list[Any]:
    parsed = value
    if isinstance(value, str):
        try:
            decoded = load_json_value(value)
        except ToolContractError:
            raise
        except json.JSONDecodeError:
            repaired = _repaired_json_text(
                value, schema, root_schema=root_schema, root_validator=root_validator
            )
            if isinstance(repaired, list):
                return repaired
            decoded = None
        except TypeError, ValueError, RecursionError:
            decoded = None
        if isinstance(decoded, list):
            parsed = decoded
    values = parsed if isinstance(parsed, list) else [parsed]

    prefix_items = schema.get("prefixItems")
    item_schema = schema.get("items")
    normalized: list[Any] = []
    for index, item in enumerate(values):
        schema_for_item: Any = item_schema
        if isinstance(prefix_items, list) and index < len(prefix_items):
            schema_for_item = prefix_items[index]
        normalized.append(
            normalize_schema_value(
                item,
                schema_for_item,
                root_schema=root_schema,
                root_validator=root_validator,
            )
            if isinstance(schema_for_item, dict)
            else item
        )
    return normalized


def _normalize_object_string(
    value: str,
    schema: JsonObject,
    *,
    root_schema: JsonObject,
    root_validator: Draft202012Validator,
) -> JsonObject | None:
    try:
        parsed = load_json_value(value)
    except ToolContractError:
        raise
    except json.JSONDecodeError:
        repaired = _repaired_json_text(
            value, schema, root_schema=root_schema, root_validator=root_validator
        )
        return repaired if isinstance(repaired, dict) else None
    except TypeError, ValueError, RecursionError:
        return None
    if not isinstance(parsed, dict):
        return None
    return _normalize_object_value(
        parsed,
        schema,
        root_schema=root_schema,
        root_validator=root_validator,
    )


def decode_json_argument(text: str, schema: JsonObject) -> Any:
    """Return the value JSON ``text`` encodes for an argument whose schema is ``schema``.

    Some Providers pass an argument they could not decode on as text, most
    often because a ``"`` inside the text of one of its fields is not escaped,
    or a line break in it is not written as an escape. When ``text`` is not
    JSON, raw control characters inside its texts are read as themselves
    (unless the same string also writes line breaks as escapes) and the stray quotes inside
    the text of the schema's fields are escaped
    (``core.utils.json_text.escape_inner_quotes``), and closing brackets
    after the end of the value are left out; that reading counts only
    when it gives one value the schema accepts with no undeclared fields.
    Otherwise a ``json.JSONDecodeError`` is raised: the first break past the
    raw control characters, else the strict decoding error. A repeated field
    raises ``ToolContractError``.
    """
    try:
        return load_json_value(text)
    except json.JSONDecodeError as error:
        repaired = _repaired_json_text(
            text, schema, root_schema=schema, root_validator=Draft202012Validator(schema)
        )
        if repaired is not _NOT_REPAIRED:
            return repaired
        strict_error = error
    try:
        json.loads(text, strict=False)
    except json.JSONDecodeError as error:
        # A raw line break reads as itself: the break past it is the one to report.
        raise error from None
    raise strict_error


_NOT_REPAIRED = object()


def _repaired_json_text(
    text: str,
    schema: JsonObject,
    *,
    root_schema: JsonObject,
    root_validator: Draft202012Validator,
) -> Any:
    """Return the schema-valid value ``text`` encodes once its slips in text are read.

    Inside the text of a value, a raw line break, tab or other control
    character stands for itself (``_schema_reading`` has the exception), and a
    stray ``"`` inside the text of a declared field belongs to the text
    (``escape_inner_quotes``). Only a schema that declares fields is read this
    way. ``_NOT_REPAIRED`` when that reading is not a value of ``schema`` with
    only declared fields, or when the text with and without its quotes escaped
    gives two such values.
    """
    fields = _schema_field_names(schema, root_schema)
    if not fields:
        return _NOT_REPAIRED
    readings: list[Any] = []
    for candidate in (text, escape_inner_quotes(text, fields)):
        if candidate is None:
            continue
        value = _schema_reading(
            candidate, schema, root_schema=root_schema, root_validator=root_validator
        )
        if value is not _NOT_REPAIRED and not any(
            same_json_value(value, other) for other in readings
        ):
            readings.append(value)
    return readings[0] if len(readings) == 1 else _NOT_REPAIRED


def _schema_reading(
    text: str,
    schema: JsonObject,
    *,
    root_schema: JsonObject,
    root_validator: Draft202012Validator,
) -> Any:
    """Return the value of ``schema`` that ``text`` encodes, control characters as text.

    A string that writes line breaks both raw and as escapes has no such value:
    a raw one there can be a slip of the output, such as a break inside a word.
    Different strings can use different representations.
    Closing brackets after the end of the value close nothing it opened and are
    left out; any other text there leaves ``text`` without such a value.
    """
    try:
        decoded, text = _value_and_its_text(text)
        if _has_mixed_line_breaks(text):
            return _NOT_REPAIRED
        value = normalize_schema_value(
            decoded, schema, root_schema=root_schema, root_validator=root_validator
        )
    except ValueError, RecursionError:
        # Includes ToolContractError: a repeated field or an ambiguous encoding.
        return _NOT_REPAIRED
    if not root_validator.evolve(schema=schema).is_valid(value):
        return _NOT_REPAIRED
    if not _declared_fields_only(value, schema, root_schema):
        return _NOT_REPAIRED
    return value


def _value_and_its_text(text: str) -> tuple[Any, str]:
    """Decode ``text`` with control characters as text; return the value and its JSON text.

    Only whitespace and stray closing brackets can follow the value; anything
    else there raises ``json.JSONDecodeError`` as extra data.
    """
    decoder = json.JSONDecoder(
        strict=False,
        parse_constant=_reject_non_json_constant,
        object_pairs_hook=_unique_json_object,
    )
    start = _JSON_WHITESPACE.match(text)
    value, end = decoder.raw_decode(text, start.end() if start else 0)
    if not _STRAY_CLOSERS.fullmatch(text, end):
        raise json.JSONDecodeError("Extra data", text, end)
    return value, text[:end]


def _has_mixed_line_breaks(text: str) -> bool:
    """Whether one string of decoded JSON mixes raw and escaped line breaks."""
    inside = escaped = raw_break = escaped_break = False
    for character in text:
        if not inside:
            if character == '"':
                inside = True
                raw_break = escaped_break = False
        elif escaped:
            escaped_break |= character in "nr"
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == '"':
            inside = False
        else:
            raw_break |= character in "\r\n"
        if raw_break and escaped_break:
            return True
    return False


def _schema_field_names(schema: Any, root_schema: JsonObject, depth: int = 0) -> set[str]:
    """Return every property name declared in ``schema`` and the schemas inside it."""
    if not isinstance(schema, dict) or depth > _FIELD_NAME_DEPTH:
        return set()
    resolved = _resolve_schema_reference(schema, root_schema)
    names: set[str] = set()
    properties = resolved.get("properties")
    nested: list[Any] = []
    if isinstance(properties, dict):
        names.update(properties)
        nested.extend(properties.values())
    for keyword in ("items", "additionalProperties"):
        nested.append(resolved.get(keyword))
    for keyword in ("prefixItems", "anyOf", "oneOf", "allOf"):
        branches = resolved.get(keyword)
        if isinstance(branches, list):
            nested.extend(branches)
    for child in nested:
        names.update(_schema_field_names(child, root_schema, depth + 1))
    return names


_FIELD_NAME_DEPTH = 8


def _declared_fields_only(value: Any, schema: Any, root_schema: JsonObject) -> bool:
    """Whether every object in ``value`` uses only the fields its schema declares.

    An object schema that lists ``properties`` and does not open itself with
    ``additionalProperties`` or ``patternProperties`` counts as closed here,
    as a Tool's root parameters do.
    """
    if not isinstance(schema, dict):
        return True
    resolved = _resolve_schema_reference(schema, root_schema)
    if isinstance(value, dict):
        properties = resolved.get("properties")
        if not isinstance(properties, dict):
            return True
        closed = (
            resolved.get("additionalProperties", False) is False
            and "patternProperties" not in resolved
        )
        if closed and any(key not in properties for key in value):
            return False
        return all(
            _declared_fields_only(item, properties.get(key), root_schema)
            for key, item in value.items()
        )
    if isinstance(value, list):
        items = resolved.get("items")
        return all(_declared_fields_only(item, items, root_schema) for item in value)
    return True


def _normalize_object_value(
    value: JsonObject,
    schema: JsonObject,
    *,
    root_schema: JsonObject,
    root_validator: Draft202012Validator,
) -> JsonObject:
    properties = schema.get("properties")
    property_schemas = properties if isinstance(properties, dict) else {}
    additional_schema = schema.get("additionalProperties")
    normalized: JsonObject = {}
    for key, item in value.items():
        item_schema = property_schemas.get(key, additional_schema)
        normalized[key] = (
            normalize_schema_value(
                item, item_schema, root_schema=root_schema, root_validator=root_validator
            )
            if isinstance(item_schema, dict)
            else item
        )
    return normalized


def same_json_value(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool/number equality shortcut."""
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
        right, sort_keys=True, allow_nan=False
    )


def load_json_value(value: str) -> Any:
    return json.loads(
        value, parse_constant=_reject_non_json_constant, object_pairs_hook=_unique_json_object
    )


def _unique_json_object(pairs: list[tuple[str, Any]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            if not same_json_value(result[key], value):
                raise ToolContractError.conflicting_values(
                    key, (f"{key} (first occurrence)", result[key]), (f"{key} (again)", value)
                )
            raise ToolContractError(f"Duplicate JSON field {key}; provide one intended value.")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _resolve_schema_reference(schema: JsonObject, root_schema: JsonObject) -> JsonObject:
    reference = schema.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/"):
        return schema
    target: Any = root_schema
    for raw_segment in reference[2:].split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or segment not in target:
            return schema
        target = target[segment]
    if not isinstance(target, dict):
        return schema
    siblings = {key: value for key, value in schema.items() if key != "$ref"}
    return {**target, **siblings}


def _validation_score(validator: Any, value: Any) -> int:
    return sum(_validation_error_score(error) for error in validator.iter_errors(value))


def _validation_error_score(error: ValidationError) -> int:
    if not error.context:
        return 1
    return sum(_validation_error_score(child) for child in error.context)


__all__ = [
    "decode_json_argument",
    "load_json_value",
    "normalize_schema_value",
    "same_json_value",
]
