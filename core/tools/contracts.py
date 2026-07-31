"""Canonical Tool schema compilation and runtime validation."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

JsonObject = dict[str, Any]
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class ToolContractError(ValueError):
    """A Tool definition or invocation violates its canonical contract."""


def discriminated_union_schema(
    discriminator: str,
    variants: Mapping[str, JsonObject],
    *,
    description: str,
    discriminator_description: str,
) -> JsonObject:
    """Build a flat union whose discriminator selects one closed argument object."""
    if not isinstance(discriminator, str) or not discriminator:
        raise ToolContractError("Discriminated union property must be a non-empty string")
    if not variants:
        raise ToolContractError("Discriminated union requires at least one variant")

    branches: list[JsonObject] = []
    for variant, raw_branch in variants.items():
        if not isinstance(variant, str) or not variant:
            raise ToolContractError("Discriminated union variant names must be non-empty strings")
        if not isinstance(raw_branch, dict) or raw_branch.get("type") != "object":
            raise ToolContractError(f"Discriminated union branch {variant!r} must have type object")

        branch = copy.deepcopy(raw_branch)
        properties = branch.get("properties")
        if not isinstance(properties, dict):
            raise ToolContractError(
                f"Discriminated union branch {variant!r} properties must be an object"
            )
        if discriminator in properties:
            raise ToolContractError(
                f"Discriminated union branch {variant!r} must not declare "
                f"the {discriminator} property"
            )
        required = branch.get("required", [])
        if not isinstance(required, list):
            raise ToolContractError(
                f"Discriminated union branch {variant!r} required must be an array"
            )

        branch["properties"] = {
            discriminator: {
                "type": "string",
                "enum": [variant],
                "description": discriminator_description,
            },
            **properties,
        }
        branch["required"] = [discriminator, *required]
        branch["additionalProperties"] = False
        branches.append(branch)

    return {
        "type": "object",
        "description": description,
        "oneOf": branches,
    }


def action_schema(
    actions: Mapping[str, JsonObject],
    *,
    description: str,
    action_description: str = "Action to perform.",
) -> JsonObject:
    """Build a flat union whose action selects one closed argument object."""
    return discriminated_union_schema(
        "action",
        actions,
        description=description,
        discriminator_description=action_description,
    )


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
    "action_schema",
    "compile_tool_contract",
]
