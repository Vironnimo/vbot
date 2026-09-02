"""Canonical Tool schema compilation and runtime validation."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

JsonObject = dict[str, Any]
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_JSON_NUMBER_PATTERN = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")
_MAX_FLOAT_DECIMAL_EXPONENT = 308
_MIN_FLOAT_DECIMAL_EXPONENT = -324


class ToolContractError(ValueError):
    """A Tool definition or invocation violates its canonical contract."""


@dataclass(frozen=True)
class ToolContract:
    """One compiled, Provider-neutral Tool contract."""

    name: str
    input_schema: JsonObject
    input_validator: Draft202012Validator
    result_schema: JsonObject | None
    result_validator: Draft202012Validator | None
    parallel_safe: bool
    schema_fingerprint: str

    def normalize_arguments(self, arguments: Any) -> Any:
        """Repair common unambiguous model encodings in a copied argument value."""
        copied_arguments = copy.deepcopy(arguments)
        _omit_optional_empty_strings(
            copied_arguments,
            self.input_schema,
            root_schema=self.input_schema,
        )
        return _normalize_schema_value(
            copied_arguments,
            self.input_schema,
            root_schema=self.input_schema,
            root_validator=self.input_validator,
        )

    def validate_arguments(self, arguments: Any) -> None:
        """Raise an actionable error when *arguments* violate the input schema."""
        _validate_instance(self.input_validator, arguments, label="arguments")

    def validate_success_data(self, data: Any) -> None:
        """Raise an actionable error when successful Tool data violates its schema."""
        if self.result_validator is None:
            if not isinstance(data, dict):
                raise ToolContractError("data must be an object")
            return
        _validate_instance(self.result_validator, data, label="data")


def compile_tool_contract(
    *,
    name: str,
    input_schema: JsonObject,
    result_schema: JsonObject | None = None,
    parallel_safe: bool = True,
    require_closed_input: bool = True,
) -> ToolContract:
    """Validate, copy, compile, and fingerprint one canonical Tool contract."""
    if not isinstance(name, str) or not name:
        raise ToolContractError("Tool name is required")
    if _TOOL_NAME_PATTERN.fullmatch(name) is None:
        raise ToolContractError(
            "Tool name must start with a letter and contain at most 64 letters, digits, "
            "or underscores"
        )
    if not isinstance(parallel_safe, bool):
        raise ToolContractError("Tool parallel_safe must be a boolean")

    canonical_input = _compile_schema(
        input_schema,
        label="input",
        require_closed_objects=require_closed_input,
    )
    canonical_result = (
        _compile_schema(result_schema, label="result", require_closed_objects=True)
        if result_schema is not None
        else None
    )
    fingerprint_payload = {
        "name": name,
        "input_schema": canonical_input,
        "result_schema": canonical_result,
        "parallel_safe": parallel_safe,
    }
    encoded = json.dumps(
        fingerprint_payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return ToolContract(
        name=name,
        input_schema=canonical_input,
        input_validator=Draft202012Validator(canonical_input),
        result_schema=canonical_result,
        result_validator=(
            Draft202012Validator(canonical_result) if canonical_result is not None else None
        ),
        parallel_safe=parallel_safe,
        schema_fingerprint=hashlib.sha256(encoded).hexdigest(),
    )


def _compile_schema(
    schema: Any,
    *,
    label: str,
    require_closed_objects: bool,
) -> JsonObject:
    if not isinstance(schema, dict):
        raise ToolContractError(f"Tool {label} schema must be a JSON Schema object")
    canonical = copy.deepcopy(schema)
    try:
        Draft202012Validator.check_schema(canonical)
    except SchemaError as error:
        path = _format_path(error.absolute_schema_path)
        raise ToolContractError(f"Tool {label} schema{path}: {error.message}") from None

    if canonical.get("type") != "object":
        raise ToolContractError(f"Tool {label} schema root must have type object")
    _validate_schema_invariants(
        canonical,
        label=label,
        path=(),
        require_closed_objects=require_closed_objects,
    )
    return canonical


def _validate_schema_invariants(
    node: Any,
    *,
    label: str,
    path: tuple[str | int, ...],
    require_closed_objects: bool,
) -> None:
    if isinstance(node, dict):
        reference = node.get("$ref")
        if isinstance(reference, str) and not reference.startswith("#/"):
            raise ToolContractError(
                f"Tool {label} schema{_format_path(path)} uses an external $ref"
            )
        if node.get("type") == "object" and "properties" in node:
            if require_closed_objects and node.get("additionalProperties") is not False:
                raise ToolContractError(
                    f"Tool {label} schema{_format_path(path)} must set "
                    "additionalProperties to false"
                )
            properties = node.get("properties")
            if not isinstance(properties, dict):
                raise ToolContractError(
                    f"Tool {label} schema{_format_path(path)} properties must be an object"
                )
            required = node.get("required", [])
            if not isinstance(required, list):
                raise ToolContractError(
                    f"Tool {label} schema{_format_path(path)} required must be an array"
                )
            unknown_required = sorted(set(required).difference(properties))
            if unknown_required:
                names = ", ".join(unknown_required)
                raise ToolContractError(
                    f"Tool {label} schema{_format_path(path)} requires unknown properties: {names}"
                )
        for key, value in node.items():
            _validate_schema_invariants(
                value,
                label=label,
                path=(*path, key),
                require_closed_objects=require_closed_objects,
            )
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _validate_schema_invariants(
                value,
                label=label,
                path=(*path, index),
                require_closed_objects=require_closed_objects,
            )


def _normalize_schema_value(
    value: Any,
    schema: Any,
    *,
    root_schema: JsonObject,
    root_validator: Draft202012Validator,
) -> Any:
    """Return the schema-guided repair candidate closest to valid JSON input."""
    if not isinstance(schema, dict):
        return value

    validator = root_validator.evolve(schema=schema)
    if validator.is_valid(value):
        return value

    candidates: list[Any] = []
    resolved = _resolve_schema_reference(schema, root_schema)
    if resolved is not schema:
        candidates.append(
            _normalize_schema_value(
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
                _normalize_schema_value(
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
            combined = _normalize_schema_value(
                combined,
                branch,
                root_schema=root_schema,
                root_validator=root_validator,
            )
        candidates.append(combined)

    candidates.append(value)
    return min(candidates, key=lambda candidate: _validation_score(validator, candidate))


def _omit_optional_empty_strings(
    value: Any,
    schema: Any,
    *,
    root_schema: JsonObject,
) -> None:
    """Treat an exact empty optional string property as an omitted property.

    Empty strings in required properties and array items remain untouched. A Tool
    can therefore use an empty optional field as harmless omission without
    weakening the schema's required-value or collection-item constraints.
    """
    if not isinstance(schema, dict):
        return

    resolved = _resolve_schema_reference(schema, root_schema)
    if isinstance(value, dict):
        properties, required = _object_schema_parts(resolved, root_schema=root_schema)
        additional_schema = resolved.get("additionalProperties")
        for key in list(value):
            item = value[key]
            item_schema = properties.get(key)
            if item_schema is None and isinstance(additional_schema, dict):
                item_schema = additional_schema
            if (
                key not in required
                and item == ""
                and _schema_has_string_type(item_schema, root_schema=root_schema)
            ):
                del value[key]
                continue
            _omit_optional_empty_strings(item, item_schema, root_schema=root_schema)
        return

    if isinstance(value, list):
        item_schema = resolved.get("items")
        for item in value:
            _omit_optional_empty_strings(item, item_schema, root_schema=root_schema)


def _object_schema_parts(
    schema: JsonObject,
    *,
    root_schema: JsonObject,
) -> tuple[dict[str, Any], set[str]]:
    """Collect object properties and required names through local schema branches."""
    resolved = _resolve_schema_reference(schema, root_schema)
    properties = dict(resolved.get("properties", {})) if isinstance(resolved, dict) else {}
    required = {name for name in resolved.get("required", []) if isinstance(name, str)}
    for keyword in ("allOf", "oneOf", "anyOf"):
        branches = resolved.get(keyword)
        if not isinstance(branches, list):
            continue
        for branch in branches:
            if not isinstance(branch, dict):
                continue
            branch_properties, branch_required = _object_schema_parts(
                branch,
                root_schema=root_schema,
            )
            properties.update(branch_properties)
            required.update(branch_required)
    return properties, required


def _schema_has_string_type(schema: Any, *, root_schema: JsonObject) -> bool:
    """Return whether a property schema accepts strings, excluding explicit empty enums."""
    if not isinstance(schema, dict):
        return False
    resolved = _resolve_schema_reference(schema, root_schema)
    if resolved.get("const") == "" or "" in resolved.get("enum", ()):
        return False
    if "string" in _declared_json_types(resolved):
        return True
    for keyword in ("oneOf", "anyOf", "allOf"):
        branches = resolved.get(keyword)
        if isinstance(branches, list) and any(
            _schema_has_string_type(branch, root_schema=root_schema) for branch in branches
        ):
            return True
    return False


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
        except (OverflowError, ValueError):
            return None

    if decimal == decimal.to_integral_value():
        try:
            return int(decimal)
        except (OverflowError, ValueError):
            return None
    if not (_MIN_FLOAT_DECIMAL_EXPONENT <= decimal.adjusted() <= _MAX_FLOAT_DECIMAL_EXPONENT):
        return None
    number = float(decimal)
    if not math.isfinite(number) or (number == 0.0 and decimal != 0):
        return None
    return number


def _coerce_boolean_string(text: str) -> bool | None:
    normalized = text.lower()
    if normalized == "true":
        return True
    if normalized == "false":
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
            decoded = _load_json_value(value)
        except (TypeError, ValueError):
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
            _normalize_schema_value(
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
        parsed = _load_json_value(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return _normalize_object_value(
        parsed,
        schema,
        root_schema=root_schema,
        root_validator=root_validator,
    )


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
        item_schema = property_schemas.get(key)
        if not isinstance(item_schema, dict) and isinstance(additional_schema, dict):
            item_schema = additional_schema
        normalized[key] = (
            _normalize_schema_value(
                item,
                item_schema,
                root_schema=root_schema,
                root_validator=root_validator,
            )
            if isinstance(item_schema, dict)
            else item
        )
    return normalized


def _load_json_value(value: str) -> Any:
    return json.loads(value, parse_constant=_reject_non_json_constant)


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


def _validate_instance(
    validator: Draft202012Validator,
    instance: Any,
    *,
    label: str,
) -> None:
    error = next(iter(validator.iter_errors(instance)), None)
    if error is None:
        return
    best = _best_validation_error(error)
    path = _format_path(best.absolute_path)
    keyword = best.validator if isinstance(best.validator, str) else "schema"
    message = (
        _format_type_validation_error(validator.schema, best)
        if best.validator == "type"
        else best.message
    )
    raise ToolContractError(f"{label}{path}: {message} [{keyword}]")


def _format_type_validation_error(root_schema: Any, error: ValidationError) -> str:
    expected = error.validator_value
    if isinstance(expected, str):
        expected_types = [expected]
    elif isinstance(expected, list):
        expected_types = [str(item) for item in expected]
    else:
        expected_types = [str(expected)]
    expected_text = " or ".join(f"JSON {item}" for item in expected_types)
    received_text = _describe_json_value(error.instance)
    default_hint = _optional_property_default_hint(root_schema, error)
    return f"expected {expected_text}, received {received_text}{default_hint}"


def _describe_json_value(value: Any) -> str:
    if value is None:
        return "JSON null"
    if isinstance(value, bool):
        return f"JSON boolean {json.dumps(value)}"
    if isinstance(value, str):
        return f"JSON string {json.dumps(value, ensure_ascii=False)}"
    if isinstance(value, int):
        return f"JSON integer {value}"
    if isinstance(value, float):
        return f"JSON number {value}"
    if isinstance(value, list):
        return "JSON array"
    if isinstance(value, dict):
        return "JSON object"
    return type(value).__name__


def _optional_property_default_hint(root_schema: Any, error: ValidationError) -> str:
    if not isinstance(error.schema, dict) or "default" not in error.schema:
        return ""

    schema_path = tuple(error.absolute_schema_path)
    for index in range(len(schema_path) - 2, -1, -1):
        if schema_path[index] != "properties":
            continue
        property_name = schema_path[index + 1]
        parent_schema = _schema_node_at_path(root_schema, schema_path[:index])
        if not isinstance(parent_schema, dict):
            continue
        required = parent_schema.get("required", [])
        if isinstance(required, list) and property_name not in required:
            default = json.dumps(error.schema["default"], ensure_ascii=False)
            return f"; omit this optional field to use its default {default}"
    return ""


def _schema_node_at_path(root_schema: Any, path: tuple[Any, ...]) -> Any:
    node = root_schema
    for segment in path:
        if isinstance(node, dict):
            if segment not in node:
                return None
            node = node[segment]
            continue
        if isinstance(node, list) and isinstance(segment, int) and 0 <= segment < len(node):
            node = node[segment]
            continue
        return None
    return node


def _best_validation_error(error: ValidationError) -> ValidationError:
    if not error.context:
        return error
    leaves: list[ValidationError] = []
    pending = list(error.context)
    while pending:
        candidate = pending.pop(0)
        if candidate.context:
            pending.extend(candidate.context)
        else:
            leaves.append(candidate)
    if not leaves:
        return error
    # ``anyOf``/``oneOf`` reports every branch. Discard complete alternatives
    # whose discriminator enum does not match the supplied value; otherwise an
    # error from an unrelated operation can hide the actionable error in the
    # selected branch.
    mismatched_alternatives = {
        prefix
        for leaf in leaves
        if leaf.validator == "enum"
        and isinstance(leaf.validator_value, list)
        and leaf.instance not in leaf.validator_value
        for prefix in _alternative_prefixes(tuple(leaf.absolute_schema_path))
    }
    matching_leaves = [
        leaf
        for leaf in leaves
        if not any(
            tuple(leaf.absolute_schema_path)[: len(prefix)] == prefix
            for prefix in mismatched_alternatives
        )
    ]
    if matching_leaves:
        leaves = matching_leaves
    elif mismatched_alternatives:
        return error
    return sorted(
        leaves,
        key=lambda item: (
            -len(item.absolute_path),
            _format_path(item.absolute_path),
            str(item.validator),
            item.message,
        ),
    )[0]


def _alternative_prefixes(path: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    prefixes: list[tuple[Any, ...]] = []
    for index, segment in enumerate(path[:-1]):
        if segment in {"anyOf", "oneOf"} and isinstance(path[index + 1], int):
            prefixes.append(path[: index + 2])
    return prefixes


def _format_path(path: Any) -> str:
    parts: list[str] = []
    for segment in path:
        if isinstance(segment, int):
            parts.append(f"[{segment}]")
        else:
            escaped = str(segment).replace("~", "~0").replace("/", "~1")
            parts.append(f"/{escaped}")
    return "".join(parts)


__all__ = [
    "ToolContract",
    "ToolContractError",
    "compile_tool_contract",
]
