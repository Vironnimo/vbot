"""Model views of MCP payloads: text bodies verbatim, every other field compact.

A Session's Model reads Tool Results as plain text: a ``content`` string renders
verbatim below the other fields. A view turns MCP content blocks, Resource
contents and Prompt messages into that body, drops protocol defaults and a
``structuredContent`` that only repeats the text, and keeps every other field,
so it loses nothing the payload holds. Views also word the argument problems of
discovered targets without echoing supplied values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# Values the SDK writes on every result; anything else stays in the view.
_DEFAULTS: dict[str, Any] = {
    "isError": False,
    "resultType": "complete",
    "ttlMs": 0,
    "cacheScope": "private",
}

_SUMMARY_PROPERTIES = 12
_SUMMARY_CHARACTERS = 400
_REQUIREMENT_CHARACTERS = 160


@dataclass(frozen=True, slots=True)
class Part:
    """One rendered item of a body: a descriptor, then its addressable text."""

    prefix: str
    text: str
    pointer: str | None  # JSON Pointer of ``text`` in the payload.
    item: str  # JSON Pointer of the whole item.

    @property
    def rendered(self) -> str:
        return self.prefix + self.text


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def body_parts(payload: dict[str, Any]) -> tuple[list[Part], str] | None:
    """Return the body parts and the field they render, or ``None`` for other payloads."""
    if isinstance(payload.get("content"), list):
        return [
            _block_part(block, f"/content/{index}")
            for index, block in enumerate(payload["content"])
        ], "content"
    if isinstance(payload.get("contents"), list):
        return [
            _resource_part(resource, f"/contents/{index}", {})
            for index, resource in enumerate(payload["contents"])
        ], "contents"
    if isinstance(payload.get("messages"), list):
        parts: list[Part] = []
        for index, message in enumerate(payload["messages"]):
            parts.extend(_message_parts(message, f"/messages/{index}"))
        return parts, "messages"
    return None


def join_parts(parts: list[Part]) -> str:
    """Join parts on one line each, or with blank lines when any part spans lines."""
    texts = [part.rendered for part in parts]
    separator = "\n\n" if any("\n" in text for text in texts) else "\n"
    return separator.join(texts)


def payload_view(payload: dict[str, Any]) -> dict[str, Any]:
    """Return what the Model reads for one complete MCP payload."""
    rendered = body_parts(payload)
    if rendered is None:
        return dict(payload)
    parts, field = rendered
    view = {
        key: value
        for key, value in payload.items()
        if key != field and not (key in _DEFAULTS and _DEFAULTS[key] == value)
    }
    if "structuredContent" in view and repeats_text(view["structuredContent"], parts):
        del view["structuredContent"]
    view["content"] = join_parts(parts)
    return view


def view_size(view: dict[str, Any]) -> int:
    body = view.get("content")
    rest = {key: value for key, value in view.items() if key != "content"}
    return (len(body) if isinstance(body, str) else len(compact(body))) + len(compact(rest))


def repeats_text(structured: Any, parts: list[Part]) -> bool:
    """Whether structured content only repeats the plain text blocks.

    Servers commonly send a JSON rendering of the same value as text, or wrap a
    plain return value as ``{"result": value}``.
    """
    texts = [part.text for part in parts if part.pointer is not None and not part.prefix]
    if not texts:
        return False
    parsed = [_parsed(text) for text in texts]
    joined = "\n".join(texts)
    candidates = [joined, _parsed(joined), *parsed, parsed]
    if isinstance(structured, dict) and set(structured) == {"result"}:
        structured = structured["result"]
    encoded = _canonical(structured)
    return any(_canonical(candidate) == encoded for candidate in candidates)


def error_text(view: dict[str, Any]) -> str:
    """Return a failed call's own report: its text plus any other fields."""
    lines = [
        f"{key}: {compact(value)}"
        for key, value in view.items()
        if key not in {"content", "isError"}
    ]
    body = view.get("content")
    if isinstance(body, str) and body:
        lines.append(body)
    return "\n".join(lines) or "(the tool returned no error text)"


def schema_summary(schema: dict[str, Any]) -> str:
    """Name a target's arguments with their types, required ones first."""
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return "It takes no named arguments."
    required = [name for name in schema.get("required", []) if name in properties]
    names = required + [name for name in properties if name not in required]
    items = []
    for name in names[:_SUMMARY_PROPERTIES]:
        kind = properties[name].get("type") if isinstance(properties[name], dict) else None
        details = [
            "/".join(kind) if isinstance(kind, list) else kind,
            "required" if name in required else None,
        ]
        detail = ", ".join(item for item in details if item)
        items.append(f"{name} ({detail})" if detail else name)
    text = ", ".join(items)
    if len(names) > _SUMMARY_PROPERTIES:
        text += f", and {len(names) - _SUMMARY_PROPERTIES} more"
    if len(text) > _SUMMARY_CHARACTERS:
        text = text[:_SUMMARY_CHARACTERS].rsplit(", ", 1)[0] + ", ..."
    return f"It takes {text}."


def argument_problem(
    validator: str, requirement: Any, schema: Any, instance: Any, pointer: str
) -> str:
    """Word one JSON Schema failure; it names fields but never echoes supplied values."""
    if validator == "required" and isinstance(instance, dict) and isinstance(requirement, list):
        missing = [str(name) for name in requirement if name not in instance]
        return f"{pointer} is missing {', '.join(missing) or 'a required field'}"
    if validator == "additionalProperties" and isinstance(instance, dict):
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        unknown = [str(name)[:40] for name in instance if name not in properties]
        # The top-level fields follow in the target's argument summary.
        known = ", ".join(properties) if pointer != "/arguments" else ""
        return f"{pointer} has fields the target does not take: {', '.join(unknown)}" + (
            f" (it takes {_bounded(known)})" if known else ""
        )
    if validator == "type":
        kinds = requirement if isinstance(requirement, list) else [requirement]
        return f"{pointer} must be {' or '.join(_article(str(kind)) for kind in kinds)}"
    if validator == "enum" and isinstance(requirement, list):
        options = "|".join(str(option) for option in requirement)
        return f"{pointer} must be one of {_bounded(options)}"
    return f"{pointer} does not meet {validator} {_bounded(compact(requirement))}"


def _article(kind: str) -> str:
    return f"an {kind}" if kind[:1] in "aeiou" else f"a {kind}"


def _bounded(text: str) -> str:
    if len(text) <= _REQUIREMENT_CHARACTERS:
        return text
    return text[: _REQUIREMENT_CHARACTERS - 3] + "..."


def _parsed(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _block_part(block: Any, item: str) -> Part:
    if not isinstance(block, dict):
        return Part(f"[content] {compact(block)}", "", None, item)
    kind = block.get("type")
    if kind == "text" and isinstance(block.get("text"), str):
        rest = {key: value for key, value in block.items() if key not in {"type", "text"}}
        prefix = f"[text] {compact(rest)}\n" if rest else ""
        return Part(prefix, block["text"], f"{item}/text", item)
    if kind == "resource" and isinstance(block.get("resource"), dict):
        rest = {key: value for key, value in block.items() if key not in {"type", "resource"}}
        return _resource_part(block["resource"], f"{item}/resource", rest)
    rest = {key: value for key, value in block.items() if key != "type"}
    return Part(f"[{kind or 'content'}] {compact(rest)}", "", None, item)


def _resource_part(resource: Any, item: str, extra: dict[str, Any]) -> Part:
    if not isinstance(resource, dict):
        return Part(f"[resource] {compact(resource)}", "", None, item)
    header = {key: value for key, value in resource.items() if key != "text"} | extra
    if isinstance(resource.get("text"), str):
        return Part(f"[resource] {compact(header)}\n", resource["text"], f"{item}/text", item)
    return Part(f"[resource] {compact(header)}", "", None, item)


def _message_parts(message: Any, item: str) -> list[Part]:
    if not isinstance(message, dict):
        return [Part(f"[message] {compact(message)}", "", None, item)]
    role = message.get("role", "message")
    content = message.get("content")
    blocks = (
        [(content, f"{item}/content")]
        if not isinstance(content, list)
        else [(block, f"{item}/content/{index}") for index, block in enumerate(content)]
    )
    parts = [_block_part(block, pointer) for block, pointer in blocks]
    rest = {key: value for key, value in message.items() if key not in {"role", "content"}}
    label = f"[{role}] {compact(rest)}\n" if rest else f"[{role}]\n"
    if parts:
        first = parts[0]
        parts[0] = Part(label + first.prefix, first.text, first.pointer, first.item)
    return parts or [Part(label.rstrip("\n"), "", None, item)]
