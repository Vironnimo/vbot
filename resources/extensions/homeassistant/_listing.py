"""Home Assistant result text: entity lines, service lines and close matches.

Pure rendering of Home Assistant REST payloads into the short text the Model
reads, plus the candidate lists that failures name. Nothing here requests,
chooses or acts on an entity.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from typing import Any

from ._arguments import call_text

# Lines per listing before it narrows to an overview or a first page.
LINE_LIMIT = 100
_SELECT_OPTION_LIMIT = 8
_HINT_CHARACTERS = 40
_SENTENCE_CHARACTERS = 160
_CANDIDATE_LIMIT = 5


def _folded(text: Any) -> str:
    """Return text for loose matching: casefolded, separators as single spaces."""
    return re.sub(r"[\s_.\-]+", " ", str(text or "").casefold()).strip()


def _attributes(entry: dict[str, Any]) -> dict[str, Any]:
    attributes = entry.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def friendly_name(entry: dict[str, Any]) -> str:
    attributes = entry.get("attributes")
    if not isinstance(attributes, dict):
        return ""
    name = attributes.get("friendly_name")
    return name.strip() if isinstance(name, str) else ""


def entity_line(entry: dict[str, Any], fields: tuple[str, ...] = ()) -> str:
    """Return ``entity_id: state[ unit] (Friendly Name)`` plus requested attributes."""
    attributes = _attributes(entry)
    line = f"{entry.get('entity_id')}: {entry.get('state')}"
    unit = attributes.get("unit_of_measurement")
    if isinstance(unit, str) and unit:
        line += f" {unit}"
    if name := friendly_name(entry):
        line += f" ({name})"
    shown = [f"{field}={_value_text(attributes[field])}" for field in fields if field in attributes]
    return f"{line} {' '.join(shown)}" if shown else line


def _value_text(value: Any) -> str:
    return value if isinstance(value, str) else call_text(value)


def states(payload: Any) -> list[dict[str, Any]]:
    """Return the well-formed state objects of a ``/api/states`` payload, sorted by id."""
    if not isinstance(payload, list):
        return []
    entries = [
        entry
        for entry in payload
        if isinstance(entry, dict) and isinstance(entry.get("entity_id"), str)
    ]
    return sorted(entries, key=lambda entry: entry["entity_id"])


def matches_area(entry: dict[str, Any], area: str) -> bool:
    """Return whether area text occurs in the entity's name, area attribute or object id."""
    wanted = _folded(area)
    attributes = _attributes(entry)
    object_id = entry["entity_id"].partition(".")[2]
    return any(
        wanted in _folded(text)
        for text in (friendly_name(entry), attributes.get("area"), object_id)
    )


def domain_overview(entries: list[dict[str, Any]]) -> tuple[str, str]:
    """Return per-domain counts and the note that says how to narrow the listing."""
    counts = Counter(entry["entity_id"].partition(".")[0] for entry in entries)
    example = "light" if "light" in counts else counts.most_common(1)[0][0]
    note = (
        f"{len(entries)} entities in {len(counts)} domains are too many to list, so this shows "
        f"the count per domain. List one domain with {call_text({'domain': example})}, or "
        f'match names with {{"area":"kitchen"}}.'
    )
    return "\n".join(f"{domain}: {count}" for domain, count in sorted(counts.items())), note


def area_suggestions(entries: list[dict[str, Any]], area: str) -> list[str]:
    """Return name words close to area text that matched nothing."""
    words: set[str] = set()
    for entry in entries:
        for text in (friendly_name(entry), entry["entity_id"].partition(".")[2]):
            words.update(_folded(text).split())
    return difflib.get_close_matches(_folded(area), sorted(words), n=3, cutoff=0.75)


def entity_candidates(entries: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Return entities that a mistyped id or a name may mean, best first."""
    wanted = _folded(text)
    if not wanted:
        return []
    by_id = {entry["entity_id"]: entry for entry in entries}
    ranked: list[str] = [
        entity for entity, entry in by_id.items() if _folded(friendly_name(entry)) == wanted
    ]
    ranked += difflib.get_close_matches(text, list(by_id), n=_CANDIDATE_LIMIT, cutoff=0.75)
    wanted_object = _folded(text.partition(".")[2] if "." in text else text)
    ranked += [
        entity
        for entity, entry in by_id.items()
        if wanted_object
        and (
            wanted_object in _folded(friendly_name(entry))
            or wanted_object in _folded(entity.partition(".")[2])
        )
    ]
    return [by_id[entity] for entity in dict.fromkeys(ranked)][:_CANDIDATE_LIMIT]


def candidate_text(candidates: list[dict[str, Any]]) -> str:
    return ", ".join(
        f"{entry['entity_id']} ({name})" if (name := friendly_name(entry)) else entry["entity_id"]
        for entry in candidates
    )


def close_names(name: str, names: list[str]) -> list[str]:
    return difflib.get_close_matches(name, names, n=3, cutoff=0.6)


def _first_sentence(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    sentence = re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0].strip()
    if len(sentence) > _SENTENCE_CHARACTERS:
        sentence = sentence[: _SENTENCE_CHARACTERS - 3].rstrip() + "..."
    return sentence


def service_fields(definition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return a service's fields with Home Assistant's collapsible sections flattened."""
    fields: dict[str, dict[str, Any]] = {}
    raw = definition.get("fields")
    if not isinstance(raw, dict):
        return fields
    for name, field in raw.items():
        if not isinstance(field, dict):
            continue
        section = field.get("fields")
        if isinstance(section, dict) and "selector" not in field:
            fields.update(
                (inner, value) for inner, value in section.items() if isinstance(value, dict)
            )
        else:
            fields[name] = field
    return fields


def _field_hint(field: dict[str, Any]) -> str:
    selector = field.get("selector")
    kind, config = ("", None)
    if isinstance(selector, dict) and selector:
        kind, config = next(iter(selector.items()))
    config = config if isinstance(config, dict) else {}
    if kind == "number" and "min" in config and "max" in config:
        unit = config.get("unit_of_measurement")
        return f"{config['min']}-{config['max']}" + (f" {unit}" if unit else "")
    if kind == "select":
        options = [
            option.get("value") if isinstance(option, dict) else option
            for option in config.get("options") or []
        ]
        if 0 < len(options) <= _SELECT_OPTION_LIMIT:
            return "|".join(str(option) for option in options)
    if kind == "boolean":
        return "true|false"
    example = field.get("example")
    if example not in (None, ""):
        text = example if isinstance(example, str) else call_text(example)
        if len(text) <= _HINT_CHARACTERS:
            return f"e.g. {text}"
    return kind


def _field_label(name: str, field: dict[str, Any]) -> str:
    hint = _field_hint(field)
    return f"{name}{'*' if field.get('required') else ''}" + (f" ({hint})" if hint else "")


def service_line(name: str, definition: dict[str, Any]) -> str:
    """Return one service as ``name: first sentence. Fields: a*, b (0-100 %)``."""
    line = name
    if sentence := _first_sentence(definition.get("description")):
        line += f": {sentence}"
    fields = service_fields(definition)
    if fields:
        line += " Fields: " + ", ".join(_field_label(key, value) for key, value in fields.items())
    return line + _response_text(definition)


def service_detail(domain: str, name: str, definition: dict[str, Any]) -> str:
    """Return one service with a line per field, including field descriptions."""
    lines = [f"{domain}.{name}: {str(definition.get('description') or '').strip()}".rstrip(": ")]
    for key, field in service_fields(definition).items():
        description = str(field.get("description") or "").strip()
        lines.append(f"- {_field_label(key, field)}" + (f": {description}" if description else ""))
    if response := _response_text(definition):
        lines.append(response.strip())
    return "\n".join(lines)


def _response_text(definition: dict[str, Any]) -> str:
    response = definition.get("response")
    if not isinstance(response, dict):
        return ""
    if response.get("optional"):
        return ' Returns data when called with "return_response":true.'
    return " Returns data."
