"""Tool JSON-schema sanitization for the Anthropic Messages wire.

The Anthropic ``/messages`` API validates each tool's ``input_schema`` and
requires the **root** to be an object carrying ``type: object``. A root that is
a union (``oneOf`` / ``anyOf`` / ``allOf`` with no ``type``) is rejected with
HTTP 400 ``tools.N.custom.input_schema.type: Field required`` (verified live
2026-07-09). Such a root is common when a tool schema is generated from a
Pydantic discriminated union / MCP output. This module strips those root union
keywords and guarantees ``type: object`` + a ``properties`` map, turning a hard
request-killing 400 into a permissive object schema.

It also collapses nullable unions — ``{"anyOf": [{"type": "string"}, {"type":
"null"}]}``, the usual Pydantic/MCP encoding for an optional field — to their
single non-null branch. The live Anthropic validator currently *accepts* these,
so this is defensive rather than a 400 fix: it is a semantically-equivalent
normalization (optionality is already carried by the parent ``required`` array)
that also keeps strict schema-to-grammar consumers happy.

vBot's own built-in and bundled-extension tools are hand-written plain object
schemas that never hit either case, but a third-party extension may register a
tool whose schema is generated externally. This module is the one choke point
that keeps such a schema from breaking the request; an already-valid schema
passes through unchanged.
"""

from __future__ import annotations

from typing import Any

from core.utils.logging import get_logger

_LOGGER = get_logger("providers.tool_schema")

# Union keywords Anthropic rejects at the schema root (nested ones are fine).
_TOP_LEVEL_UNION_KEYWORDS = ("oneOf", "anyOf", "allOf")
# Keywords whose ``[X, {type: null}]`` form encodes an optional field.
_NULLABLE_UNION_KEYWORDS = ("anyOf", "oneOf")
# Metadata carried from a collapsed union node onto its surviving variant.
_METADATA_KEYS = ("title", "description", "default", "examples")
_EMPTY_OBJECT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def sanitize_anthropic_tool_input_schema(schema: Any, *, tool_name: str = "") -> dict[str, Any]:
    """Return an Anthropic-valid ``input_schema`` for a tool's parameters.

    Collapses nullable ``anyOf`` / ``oneOf`` unions anywhere in the tree, strips
    root-level union keywords, and guarantees a top-level ``object`` with a
    ``properties`` map. A schema already in that shape passes through unchanged.
    The result is freshly built, so the caller's registry entry is never mutated.
    """

    if not isinstance(schema, dict) or not schema:
        return dict(_EMPTY_OBJECT_SCHEMA)

    normalized = _strip_nullable_unions(schema)
    if not isinstance(normalized, dict):
        return dict(_EMPTY_OBJECT_SCHEMA)

    stripped_keys = [key for key in _TOP_LEVEL_UNION_KEYWORDS if key in normalized]
    if stripped_keys:
        normalized = {
            key: value for key, value in normalized.items() if key not in _TOP_LEVEL_UNION_KEYWORDS
        }
        normalized.setdefault("type", "object")
        _LOGGER.debug(
            "Stripped root-level union keyword(s) %s from tool %r input_schema "
            "(Anthropic rejects them at the schema root)",
            stripped_keys,
            tool_name or "<unknown>",
        )

    if normalized.get("type") == "object" and not isinstance(normalized.get("properties"), dict):
        normalized["properties"] = {}
    return normalized


def _strip_nullable_unions(schema: Any) -> Any:
    """Collapse ``anyOf`` / ``oneOf`` nullable unions to their single non-null branch.

    Recurses through the whole schema. A union collapses only when dropping the
    ``{"type": "null"}`` branch(es) leaves exactly one variant; otherwise the
    union is meaningful and left intact. Metadata on the union node (title,
    description, default, examples) carries onto the surviving variant.
    """

    if isinstance(schema, list):
        return [_strip_nullable_unions(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    stripped = {key: _strip_nullable_unions(value) for key, value in schema.items()}
    for keyword in _NULLABLE_UNION_KEYWORDS:
        variants = stripped.get(keyword)
        if not isinstance(variants, list):
            continue
        non_null = [
            item for item in variants if not (isinstance(item, dict) and item.get("type") == "null")
        ]
        # Collapse only when a null branch was dropped and exactly one variant
        # remains — a genuine multi-branch union stays untouched.
        if len(non_null) == 1 and len(non_null) != len(variants):
            replacement = dict(non_null[0]) if isinstance(non_null[0], dict) else {}
            for meta_key in _METADATA_KEYS:
                if meta_key in stripped and meta_key not in replacement:
                    replacement[meta_key] = stripped[meta_key]
            return _strip_nullable_unions(replacement)
    return stripped
