"""Canonical Tool schema compilation and runtime validation."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

JsonObject = dict[str, Any]
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


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
    parallel_safe: bool = False,
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

    canonical_input = _compile_schema(input_schema, label="input")
    canonical_result = (
        _compile_schema(result_schema, label="result") if result_schema is not None else None
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


def _compile_schema(schema: Any, *, label: str) -> JsonObject:
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
    _validate_schema_invariants(canonical, label=label, path=())
    return canonical


def _validate_schema_invariants(
    node: Any,
    *,
    label: str,
    path: tuple[str | int, ...],
) -> None:
    if isinstance(node, dict):
        reference = node.get("$ref")
        if isinstance(reference, str) and not reference.startswith("#/"):
            raise ToolContractError(
                f"Tool {label} schema{_format_path(path)} uses an external $ref"
            )
        if node.get("type") == "object" and "properties" in node:
            if node.get("additionalProperties") is not False:
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
            _validate_schema_invariants(value, label=label, path=(*path, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _validate_schema_invariants(value, label=label, path=(*path, index))


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
    raise ToolContractError(f"{label}{path}: {best.message} [{keyword}]")


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
