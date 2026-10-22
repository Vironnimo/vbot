"""How Tool contracts read JSON Schemas: keyword positions, older drafts, object fields."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

JsonObject = dict[str, Any]

# Keywords whose values are instance data, never schemas: schema rules do not
# apply inside them, even where the data looks like a schema.
DATA_KEYWORDS = frozenset({"const", "default", "enum", "examples"})
# Keywords whose values map names to schemas; the map itself is no schema.
SCHEMA_MAPS = frozenset(
    {"$defs", "definitions", "dependentSchemas", "patternProperties", "properties"}
)
# Keywords whose value is a schema or an array of schemas, in every draft.
_SUBSCHEMA_KEYWORDS = frozenset(
    {
        "additionalItems",
        "additionalProperties",
        "allOf",
        "anyOf",
        "contains",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "oneOf",
        "prefixItems",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
# Draft-04 boolean exclusive bounds and the bound each one makes exclusive.
_DRAFT4_EXCLUSIVE_BOUNDS = {"exclusiveMinimum": "minimum", "exclusiveMaximum": "maximum"}


def current_dialect_schema(schema: Any) -> Any:
    """Return a copy of *schema* with older JSON Schema spellings in their 2020-12 form.

    Tool contracts are JSON Schema 2020-12. Schemas from outside vBot, such as
    MCP servers' Tool schemas, are often written for draft-04 to 2019-09; two
    spellings of those drafts are invalid in 2020-12 and mean one thing there,
    so they are converted wherever a schema holds another:

    - a tuple ``items`` (an array of schemas) becomes ``prefixItems``, and an
      ``additionalItems`` beside it becomes ``items``;
    - a draft-04 boolean ``exclusiveMinimum``/``exclusiveMaximum`` becomes the
      numeric bound: ``true`` takes over the value of ``minimum``/``maximum``,
      ``false`` is dropped.

    Everything else, ``$schema`` included, stays as written, so a schema
    without these spellings comes back equal.
    """
    if not isinstance(schema, dict):
        return copy.deepcopy(schema)
    converted: JsonObject = {}
    for key, value in schema.items():
        if key in SCHEMA_MAPS or key == "dependencies":
            # ``dependencies`` (before 2019-09) maps names to schemas or name arrays.
            converted[key] = (
                {name: current_dialect_schema(item) for name, item in value.items()}
                if isinstance(value, dict)
                else copy.deepcopy(value)
            )
        elif key in _SUBSCHEMA_KEYWORDS:
            converted[key] = (
                [current_dialect_schema(item) for item in value]
                if isinstance(value, list)
                else current_dialect_schema(value)
            )
        else:
            converted[key] = copy.deepcopy(value)
    return _current_dialect_keywords(converted)


def _current_dialect_keywords(schema: JsonObject) -> JsonObject:
    """Spell the older keywords of one schema, not of the schemas inside it, the 2020-12 way."""
    tuple_items = isinstance(schema.get("items"), list) and "prefixItems" not in schema
    exclusive = {
        bound: schema[keyword]
        for keyword, bound in _DRAFT4_EXCLUSIVE_BOUNDS.items()
        if isinstance(schema.get(keyword), bool)
    }
    converted: JsonObject = {}
    for key, value in schema.items():
        if tuple_items and key == "items":
            converted["prefixItems"] = value
        elif tuple_items and key == "additionalItems":
            converted["items"] = value
        elif key in _DRAFT4_EXCLUSIVE_BOUNDS and isinstance(value, bool):
            bound = schema.get(_DRAFT4_EXCLUSIVE_BOUNDS[key])
            if value and _is_number(bound):
                converted[key] = bound
        elif exclusive.get(key) is True and _is_number(value):
            continue  # Its exclusive keyword carries the value now.
        else:
            converted[key] = value
    return converted


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class ObjectFields:
    """The fields an object schema declares, merged with those of its branches."""

    properties: dict[str, Any]
    required: tuple[str, ...]
    # Whether a ``properties`` keyword declared them at all.
    declared: bool
    # Whether an explicit ``additionalProperties`` other than false, or
    # ``patternProperties``, accepts undeclared names.
    open: bool


def object_fields(
    schema: Mapping[str, Any], root: Any, seen: frozenset[int] = frozenset()
) -> ObjectFields | None:
    """Return the fields *schema* declares itself and through ``allOf`` and ``$ref``.

    Every branch of ``allOf`` and a reference beside the keywords apply to the
    same instance, so their properties and required names are the object's own;
    the first declaration of a name wins. A reference resolves inside *root*.
    ``None`` when a branch cannot be read (a reference to an anchor or outside
    the schema): the names it declares would be missing.
    """
    properties = schema.get("properties")
    fields: dict[str, Any] = dict(properties) if isinstance(properties, dict) else {}
    listed = schema.get("required")
    required = (
        [name for name in listed if isinstance(name, str)] if isinstance(listed, list) else []
    )
    declared = isinstance(properties, dict)
    is_open = schema.get("additionalProperties", False) is not False or (
        "patternProperties" in schema
    )
    branches = schema.get("allOf")
    branches = list(branches) if isinstance(branches, list) else []
    reference = schema.get("$ref")
    if reference is not None:
        target = _local_reference(reference, root)
        if target is None:
            return None
        branches.append(target)
    visited = seen | {id(schema)}
    for branch in branches:
        if isinstance(branch, bool):
            continue
        if not isinstance(branch, Mapping):
            return None
        if id(branch) in visited:
            continue
        nested = object_fields(branch, root, visited)
        if nested is None:
            return None
        for name, value in nested.properties.items():
            fields.setdefault(name, value)
        required.extend(name for name in nested.required if name not in required)
        declared = declared or nested.declared
        is_open = is_open or nested.open
    return ObjectFields(fields, tuple(required), declared, is_open)


def _local_reference(reference: Any, root: Any) -> Any:
    """Return the schema a ``#`` or ``#/...`` reference names inside *root*, else ``None``."""
    if reference == "#":
        return root
    if not isinstance(reference, str) or not reference.startswith("#/"):
        return None
    node = root
    for part in reference[2:].split("/"):
        key = unquote(part).replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping) and key in node:
            node = node[key]
        elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            return None
    return node
