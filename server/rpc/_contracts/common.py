"""Small JSON Schema constructors shared by the explicit service declarations."""

from __future__ import annotations

from typing import Any

from core.extensions.services import ServiceKind

type JsonObject = dict[str, Any]
type ServiceDefinition = tuple[str, ServiceKind, JsonObject, tuple[str, ...]]


def obj(
    properties: JsonObject | None = None,
    required: tuple[str, ...] = (),
    *,
    additional: bool | JsonObject = False,
) -> JsonObject:
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": additional,
    }


def params(properties: JsonObject | None = None, required: tuple[str, ...] = ()) -> JsonObject:
    return obj(properties, required)


def string(description: str = "") -> JsonObject:
    result: JsonObject = {"type": "string"}
    if description:
        result["description"] = description
    return result


def boolean(description: str = "") -> JsonObject:
    result: JsonObject = {"type": "boolean"}
    if description:
        result["description"] = description
    return result


def integer(minimum: int | None = None, maximum: int | None = None) -> JsonObject:
    result: JsonObject = {"type": "integer"}
    if minimum is not None:
        result["minimum"] = minimum
    if maximum is not None:
        result["maximum"] = maximum
    return result


def array(items: JsonObject) -> JsonObject:
    return {"type": "array", "items": items}


def nullable(schema: JsonObject) -> JsonObject:
    return {"anyOf": [schema, {"type": "null"}]}


def enum(*values: Any) -> JsonObject:
    return {"enum": list(values)}


def service(
    name: str,
    kind: ServiceKind,
    parameters: JsonObject,
    resources: tuple[str, ...] = (),
    description: str = "",
) -> ServiceDefinition:
    return name, kind, {**parameters, "description": description}, resources
