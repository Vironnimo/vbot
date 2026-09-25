"""Home Assistant call shapes: harness dialects and identifier spelling.

Pure argument repair, registered as each Tool's ``argument_normalizer`` so it
runs before contract validation and before any request. It reads the shapes
other harnesses and Home Assistant itself use (YAML ``action``/``target``,
``service_data``, ``light.turn_on`` in one field, entity id lists) and Home
Assistant identifier spelling (case, spaces, hyphens). When two fields
disagree it refuses with the corrected calls; it never chooses an entity.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable
from typing import Any

from core.tools._argument_repair import normalize_call_arguments
from core.tools.contracts import ToolContractError, compile_tool_contract

# Identifier grammar; it also keeps request paths free of traversal.
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")

# Service targets other than entities travel as service data.
_TARGET_FIELDS = ("area_id", "device_id", "floor_id", "label_id")

_LIST_ENTITIES_ALIASES = {
    "room": "area",
    "name": "area",
    "query": "area",
    "search": "area",
    "text": "area",
}
_ENTITY_ALIASES = {"entity": "entity_id", "entities": "entity_id", "entity_ids": "entity_id"}
_CALL_SERVICE_ALIASES = {
    **_ENTITY_ALIASES,
    "action": "service",
    "service_name": "service",
    "service_data": "data",
}


def call_text(call: dict[str, Any]) -> str:
    """Return a call's arguments as compact JSON for messages."""
    return json.dumps(call, ensure_ascii=False, separators=(",", ":"))


def identifier(text: str) -> str:
    """Return Home Assistant's spelling of a typed identifier (``Light.Living Room``)."""
    text = re.sub(r"\s*\.\s*", ".", text.strip().lower())
    return re.sub(r"[\s-]+", "_", text)


def split_entity_ids(value: str) -> list[str]:
    """Return the entity ids of a comma-separated ``entity_id`` value."""
    return [item for item in (part.strip() for part in value.split(",")) if item]


def _entity_text(tool: str, field: str, value: Any) -> str:
    """Return one or several entity ids as one comma-separated field value."""
    items = value if isinstance(value, list) else [value]
    if any(isinstance(item, (dict, list)) for item in items):
        raise ToolContractError(
            f'{tool} was not run: "{field}" takes entity ids such as "light.kitchen", '
            "or several in a list or separated by commas."
        )
    ids = [identifier(part) for item in items for part in split_entity_ids(str(item))]
    if not ids:
        raise ToolContractError(
            f'{tool} was not run: "{field}" is empty. Name the entity to act on, such as '
            '"entity_id":"light.kitchen", or omit entity_id for a service without a target.'
        )
    return ",".join(dict.fromkeys(ids))


def _domain(tool: str, value: str, example: dict[str, Any]) -> str:
    domain = identifier(value)
    if not IDENTIFIER_RE.match(domain):
        raise ToolContractError(
            f'{tool} was not run: domain "{value}" is not a Home Assistant domain name '
            f"(lowercase letters, digits and underscores). Send it like {call_text(example)}; "
            "find domains with ha_list_services {}."
        )
    return domain


def _service(tool: str, value: str, example: dict[str, Any]) -> str:
    service = identifier(value)
    if not IDENTIFIER_RE.match(service):
        raise ToolContractError(
            f'{tool} was not run: service "{value}" is not a Home Assistant service name '
            f"(lowercase letters, digits and underscores). Send it like {call_text(example)}; "
            'find services with ha_list_services {"domain":"light"}.'
        )
    return service


def _qualified(tool: str, arguments: dict[str, Any], call: dict[str, Any]) -> None:
    """Split ``domain.service`` written in either field; refuse disagreeing domains."""
    domain = arguments.get("domain")
    service = arguments.get("service")
    parts: dict[str, tuple[str, str]] = {}
    for field, value in (("domain", domain), ("service", service)):
        if not isinstance(value, str):
            continue
        first, dot, second = identifier(value).partition(".")
        if dot and IDENTIFIER_RE.match(first) and IDENTIFIER_RE.match(second):
            parts[field] = (first, second)
    if not parts:
        return
    domains = {first for first, _ in parts.values()}
    if isinstance(domain, str) and "domain" not in parts:
        domains.add(identifier(domain))
    services = {second for _, second in parts.values()}
    if isinstance(service, str) and "service" not in parts:
        services.add(identifier(service))
    if len(domains) > 1 or len(services) > 1:
        options = " or ".join(
            call_text({**call, "domain": first, "service": second})
            for first in sorted(domains)
            for second in sorted(services)
        )
        raise ToolContractError(
            f"{tool} was not run: domain and service name different services. "
            f"Send one of {options}."
        )
    arguments["domain"], arguments["service"] = domains.pop(), services.pop()


def _lift_target(arguments: dict[str, Any]) -> None:
    """Read Home Assistant's ``target`` object: entities to entity_id, the rest to data."""
    target = arguments.pop("target", None)
    if target is None or target == "" or target == {}:
        return
    if isinstance(target, str) and target.strip().startswith("{"):
        with contextlib.suppress(ValueError):
            target = json.loads(target)
    if not isinstance(target, dict):
        target = {"entity_id": target}
    unknown = sorted(set(target) - {"entity_id", *_TARGET_FIELDS})
    if unknown:
        raise ToolContractError(
            f"ha_call_service was not run: target cannot hold {', '.join(unknown)}. "
            f"A target holds entity_id, {', '.join(_TARGET_FIELDS)}."
        )
    if "entity_id" in target:
        _merge_entities(arguments, target["entity_id"], "target.entity_id")
    others = {field: target[field] for field in _TARGET_FIELDS if field in target}
    if not others:
        return
    if arguments.get("data") in (None, ""):
        arguments["data"] = {}
    data = arguments["data"]
    if not isinstance(data, dict):
        return  # Contract validation refuses the malformed data.
    for field, value in others.items():
        if field in data and data[field] != value:
            raise ToolContractError(
                f"ha_call_service was not run: target.{field} and data.{field} differ. "
                f"Send {field} once, in target or in data."
            )
        data[field] = value


def _merge_entities(arguments: dict[str, Any], value: Any, source: str) -> None:
    lifted = _entity_text("ha_call_service", source, value)
    current = arguments.get("entity_id")
    if current is None:
        arguments["entity_id"] = lifted
        return
    current = _entity_text("ha_call_service", "entity_id", current)
    if set(split_entity_ids(current)) != set(split_entity_ids(lifted)):
        raise ToolContractError(
            f'ha_call_service was not run: entity_id "{current}" and {source} "{lifted}" '
            "name different entities. Put the entities to act on in entity_id only, "
            f'such as "entity_id":"{current}" or "entity_id":"{current},{lifted}".'
        )
    arguments["entity_id"] = current


def _repair_list_entities(arguments: dict[str, Any]) -> dict[str, Any]:
    domain = arguments.get("domain")
    if isinstance(domain, str):
        entity = identifier(domain)
        if ENTITY_ID_RE.match(entity):
            raise ToolContractError(
                f'ha_list_entities was not run: domain takes a domain such as "light", not '
                f'"{domain}". Read one entity with ha_get_state {call_text({"entity_id": entity})}'
                f", or list its domain with {call_text({'domain': entity.partition('.')[0]})}."
            )
        arguments["domain"] = _domain("ha_list_entities", domain, {"domain": "light"})
    if isinstance(arguments.get("area"), str):
        arguments["area"] = arguments["area"].strip()
    return arguments


def _repair_get_state(arguments: dict[str, Any]) -> dict[str, Any]:
    value = arguments.get("entity_id")
    if isinstance(value, (str, list)) and value:
        ids = split_entity_ids(_entity_text("ha_get_state", "entity_id", value))
        if len(ids) > 1:
            raise ToolContractError(
                f"ha_get_state was not run: it reads one entity per call. Call it once for "
                f"each of {', '.join(ids)}; the calls can run in parallel."
            )
        arguments["entity_id"] = ids[0]
    return arguments


def _repair_list_services(arguments: dict[str, Any]) -> dict[str, Any]:
    example = {"domain": "light", "service": "turn_on"}
    _qualified("ha_list_services", arguments, {})
    if isinstance(arguments.get("domain"), str):
        arguments["domain"] = _domain("ha_list_services", arguments["domain"], {"domain": "light"})
    if isinstance(arguments.get("service"), str):
        if "domain" not in arguments:
            raise ToolContractError(
                "ha_list_services was not run: service needs its domain, such as "
                f"{call_text(example)}."
            )
        arguments["service"] = _service("ha_list_services", arguments["service"], example)
    return arguments


def _repair_call_service(arguments: dict[str, Any]) -> dict[str, Any]:
    _lift_target(arguments)
    data = arguments.get("data")
    if isinstance(data, dict) and "entity_id" in data:
        _merge_entities(arguments, data.pop("entity_id"), "data.entity_id")
    if "entity_id" in arguments:
        arguments["entity_id"] = _entity_text(
            "ha_call_service", "entity_id", arguments["entity_id"]
        )
    entity = arguments.get("entity_id")
    _qualified("ha_call_service", arguments, {"entity_id": entity} if entity else {})
    example = {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"}
    if arguments.get("domain") is None and isinstance(arguments.get("service"), str):
        # A service named without its domain acts on its entities' domain.
        ids = split_entity_ids(entity or "")
        domains = {item.partition(".")[0] for item in ids if ENTITY_ID_RE.match(item)}
        if len(domains) == 1 and all(ENTITY_ID_RE.match(item) for item in ids):
            arguments["domain"] = domains.pop()
        elif len(domains) > 1:
            service = identifier(arguments["service"])
            raise ToolContractError(
                "ha_call_service was not run: the entities belong to the domains "
                f"{', '.join(sorted(domains))}, so the service's domain is unclear. Add the "
                f'domain, or use "domain":"homeassistant" with service "{service}" to act on '
                "entities of several domains."
            )
    for field, read in (("domain", _domain), ("service", _service)):
        value = arguments.get(field)
        if value is None:
            raise ToolContractError(
                f"ha_call_service was not run: {field} is missing. Send the service as domain "
                f"and service, such as {call_text(example)}; find services with "
                "ha_list_services {}."
            )
        if isinstance(value, str):
            arguments[field] = read("ha_call_service", value, example)
    response = arguments.get("return_response")
    if isinstance(response, str) and response.strip().lower() in {"true", "false"}:
        arguments["return_response"] = response.strip().lower() == "true"
    return arguments


def _normalizer(
    name: str,
    schema: dict[str, Any],
    repair: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    aliases: dict[str, str] | None = None,
    omitted: tuple[str, ...] = (),
) -> Callable[[Any], Any]:
    contract = compile_tool_contract(name=name, input_schema=schema, require_closed_input=False)

    def normalize(arguments: Any) -> Any:
        repaired = normalize_call_arguments(
            contract, arguments, field_aliases=aliases, empty_as_omitted=omitted
        )
        return repair(repaired) if isinstance(repaired, dict) else repaired

    return normalize


def list_entities_normalizer(name: str, schema: dict[str, Any]) -> Callable[[Any], Any]:
    return _normalizer(
        name,
        schema,
        _repair_list_entities,
        aliases=_LIST_ENTITIES_ALIASES,
        omitted=("domain", "area"),
    )


def get_state_normalizer(name: str, schema: dict[str, Any]) -> Callable[[Any], Any]:
    return _normalizer(name, schema, _repair_get_state, aliases=_ENTITY_ALIASES)


def list_services_normalizer(name: str, schema: dict[str, Any]) -> Callable[[Any], Any]:
    return _normalizer(name, schema, _repair_list_services, omitted=("domain", "service"))


def call_service_normalizer(name: str, schema: dict[str, Any]) -> Callable[[Any], Any]:
    return _normalizer(
        name,
        schema,
        _repair_call_service,
        aliases=_CALL_SERVICE_ALIASES,
        omitted=("domain", "service", "data"),
    )
