"""Home Assistant integration — 4 LLM-callable REST-API tools.

Thin HTTP wrappers around the built-in Home Assistant REST API
(``{HASS_URL}/api/``). Tools are only registered when ``HASS_TOKEN``
is configured.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Callable
from typing import Any

import httpx

from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.homeassistant")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_HASS_URL = "http://homeassistant.local:8123"

_RETRY_MAX_RETRIES = 2
_RETRY_INITIAL_DELAY_SECONDS = 1.0
_RETRY_BACKOFF_FACTOR = 2
_RETRY_JITTER_FACTOR = 0.5
_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})

_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=15.0)

# Regex for validating entity IDs, domains, and service names.
# These prevent path traversal in HA REST API URLs.
_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
_DOMAIN_SERVICE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Domains that can execute arbitrary code or enable SSRF.
_BLOCKED_DOMAINS = frozenset(
    {
        "shell_command",
        "command_line",
        "python_script",
        "pyscript",
        "hassio",
        "rest_command",
    }
)

# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

HA_LIST_ENTITIES_NAME = "ha_list_entities"
HA_LIST_ENTITIES_DESCRIPTION = (
    "List all entities registered in Home Assistant, optionally filtered "
    "by domain (e.g. light, climate) and area (matched against friendly_name)."
)
HA_LIST_ENTITIES_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "description": "Optional domain filter (e.g. light, climate, sensor).",
        },
        "area": {
            "type": "string",
            "description": (
                "Optional area filter matched against entity friendly_name "
                "(case-insensitive substring)."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
}

HA_GET_STATE_NAME = "ha_get_state"
HA_GET_STATE_DESCRIPTION = "Get the full state object for a single Home Assistant entity."
HA_GET_STATE_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "Entity id (e.g. light.living_room, sensor.temperature).",
        },
    },
    "required": ["entity_id"],
    "additionalProperties": False,
}

HA_LIST_SERVICES_NAME = "ha_list_services"
HA_LIST_SERVICES_DESCRIPTION = (
    "List all services available in Home Assistant, optionally filtered "
    "by domain (e.g. light, climate). Use this to discover what actions "
    "are available before calling ha_call_service."
)
HA_LIST_SERVICES_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "description": "Optional domain filter (e.g. light, climate).",
        },
    },
    "required": [],
    "additionalProperties": False,
}

HA_CALL_SERVICE_NAME = "ha_call_service"
HA_CALL_SERVICE_DESCRIPTION = (
    "Call a Home Assistant service (action) on a domain. "
    "Use ha_list_services first to discover available services and their parameters."
)
HA_CALL_SERVICE_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "description": "Service domain (e.g. light, climate, switch).",
        },
        "service": {
            "type": "string",
            "description": "Service name (e.g. turn_on, turn_off, set_temperature).",
        },
        "entity_id": {
            "type": "string",
            "description": "Optional target entity id (e.g. light.living_room).",
        },
        "data": {
            "type": "object",
            "description": "Optional service data parameters (e.g. brightness, temperature).",
        },
    },
    "required": ["domain", "service"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_text(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def _normalize_json_object(raw: Any) -> JsonObject | None:
    if isinstance(raw, dict):
        return raw
    return None


def _invalid_entity_id_failure(entity_id: str) -> JsonObject:
    return tool_failure("validation_error", f"invalid entity_id format: {entity_id}")


async def _sleep_for_retry(attempt: int) -> None:
    base_delay = _RETRY_INITIAL_DELAY_SECONDS * (_RETRY_BACKOFF_FACTOR**attempt)
    jitter = random.uniform(0, base_delay * _RETRY_JITTER_FACTOR)
    await asyncio.sleep(base_delay + jitter)


def _extract_error_detail(response: httpx.Response) -> str:
    """Extract a human-readable error message from an HA HTTP response."""
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        message = _normalize_text(payload.get("message", ""))
        if message:
            return message

    fallback = _normalize_text(response.text)
    if fallback:
        return fallback[:300]
    return response.reason_phrase or "request failed"


async def _ha_request(
    method: str,
    url: str,
    token: str,
    json_body: JsonObject | None = None,
) -> tuple[JsonObject | None, str | None]:
    """Make an HTTP request to the Home Assistant REST API with retries.

    Args:
        method: HTTP method (GET, POST).
        url: Full URL to the HA API endpoint.
        token: HA Long-Lived Access Token.
        json_body: Optional JSON body for POST requests.

    Returns:
        Tuple of (response_json, error_string). One is always None.
    """
    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        for attempt in range(_RETRY_MAX_RETRIES + 1):
            try:
                if method == "GET":
                    response = await client.get(url, headers=headers)
                elif method == "POST":
                    response = await client.post(url, headers=headers, json=json_body)
                else:
                    return None, f"unsupported HTTP method: {method}"
            except httpx.RequestError as error:
                if attempt >= _RETRY_MAX_RETRIES:
                    _LOGGER.warning("Home Assistant request failed: %s", error)
                    return None, f"request failed: {error}"
                await _sleep_for_retry(attempt)
                continue

            if response.status_code >= 400:
                if response.status_code in _RETRYABLE_STATUS_CODES and attempt < _RETRY_MAX_RETRIES:
                    await _sleep_for_retry(attempt)
                    continue
                detail = _extract_error_detail(response)
                _LOGGER.warning(
                    "Home Assistant request failed: HTTP %s: %s",
                    response.status_code,
                    detail,
                )
                return None, f"HTTP {response.status_code}: {detail}"

            try:
                payload = response.json()
            except ValueError:
                return None, "Home Assistant returned invalid JSON"

            return payload, None

    return None, "request failed"


# ---------------------------------------------------------------------------
# 1. ha_list_entities
# ---------------------------------------------------------------------------


async def _handle_list_entities(
    context: ToolContext,
    arguments: JsonObject,
    hass_url: str,
    token: str,
) -> JsonObject:
    del context

    domain = _normalize_text(arguments.get("domain", "")).lower()
    area = _normalize_text(arguments.get("area", "")).lower()

    payload, error = await _ha_request("GET", f"{hass_url}/api/states", token)
    if error is not None:
        return tool_failure("home_assistant_error", error)
    if payload is None:
        return tool_failure("home_assistant_error", "no response from Home Assistant")

    if not isinstance(payload, list):
        return tool_failure("home_assistant_error", "unexpected response format from /api/states")

    entities: list[JsonObject] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue

        entity_id = _normalize_text(entry.get("entity_id", ""))
        if not entity_id:
            continue

        if domain and not entity_id.startswith(f"{domain}."):
            continue

        state = entry.get("state")
        friendly_name = ""
        attributes = entry.get("attributes")
        if isinstance(attributes, dict):
            friendly_name = _normalize_text(attributes.get("friendly_name", ""))

        if area and area not in friendly_name.lower():
            continue

        entities.append(
            {
                "entity_id": entity_id,
                "state": state,
                "friendly_name": friendly_name,
            }
        )

    return tool_success({"count": len(entities), "entities": entities})


# ---------------------------------------------------------------------------
# 2. ha_get_state
# ---------------------------------------------------------------------------


async def _handle_get_state(
    context: ToolContext,
    arguments: JsonObject,
    hass_url: str,
    token: str,
) -> JsonObject:
    del context

    entity_id = _normalize_text(arguments.get("entity_id", ""))
    if not entity_id:
        return tool_failure("validation_error", "entity_id is required")
    if not _ENTITY_ID_RE.match(entity_id):
        return _invalid_entity_id_failure(entity_id)

    payload, error = await _ha_request("GET", f"{hass_url}/api/states/{entity_id}", token)
    if error is not None:
        return tool_failure("home_assistant_error", error)
    if payload is None:
        return tool_failure("home_assistant_error", f"entity {entity_id} not found")

    if not isinstance(payload, dict):
        return tool_failure("home_assistant_error", "unexpected response format")

    return tool_success(
        {
            "entity_id": payload.get("entity_id"),
            "state": payload.get("state"),
            "attributes": payload.get("attributes"),
            "last_changed": payload.get("last_changed"),
            "last_updated": payload.get("last_updated"),
        }
    )


# ---------------------------------------------------------------------------
# 3. ha_list_services
# ---------------------------------------------------------------------------


async def _handle_list_services(
    context: ToolContext,
    arguments: JsonObject,
    hass_url: str,
    token: str,
) -> JsonObject:
    del context

    domain_filter = _normalize_text(arguments.get("domain", "")).lower()

    payload, error = await _ha_request("GET", f"{hass_url}/api/services", token)
    if error is not None:
        return tool_failure("home_assistant_error", error)
    if payload is None:
        return tool_failure("home_assistant_error", "no response from Home Assistant")

    if not isinstance(payload, list):
        return tool_failure("home_assistant_error", "unexpected response format from /api/services")

    domains: list[JsonObject] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue

        entry_domain = _normalize_text(entry.get("domain", ""))
        if domain_filter and entry_domain != domain_filter:
            continue

        services_raw = entry.get("services")
        if not isinstance(services_raw, dict):
            continue

        services: dict[str, JsonObject] = {}
        for service_name, service_def in services_raw.items():
            if not isinstance(service_def, dict):
                continue
            services[service_name] = {
                "description": _normalize_text(service_def.get("description", "")),
                "fields": service_def.get("fields", {}),
            }

        domains.append({"domain": entry_domain, "services": services})

    return tool_success({"count": len(domains), "domains": domains})


# ---------------------------------------------------------------------------
# 4. ha_call_service
# ---------------------------------------------------------------------------


async def _handle_call_service(
    context: ToolContext,
    arguments: JsonObject,
    hass_url: str,
    token: str,
) -> JsonObject:
    del context

    domain = _normalize_text(arguments.get("domain", "")).lower()
    service = _normalize_text(arguments.get("service", "")).lower()
    entity_id = _normalize_text(arguments.get("entity_id", ""))
    data = _normalize_json_object(arguments.get("data"))

    if not domain:
        return tool_failure("validation_error", "domain is required")
    if not service:
        return tool_failure("validation_error", "service is required")
    if not _DOMAIN_SERVICE_RE.match(domain):
        return tool_failure("validation_error", f"invalid domain: {domain}")
    if not _DOMAIN_SERVICE_RE.match(service):
        return tool_failure("validation_error", f"invalid service: {service}")
    if entity_id and not _ENTITY_ID_RE.match(entity_id):
        return _invalid_entity_id_failure(entity_id)
    if data is not None and "entity_id" in data:
        return tool_failure("validation_error", "data.entity_id is not allowed; use entity_id")

    if domain in _BLOCKED_DOMAINS:
        return tool_failure(
            "blocked_domain",
            f"domain '{domain}' is blocked for security reasons",
        )

    body: JsonObject = {}
    if entity_id:
        body["entity_id"] = entity_id
    if data:
        body.update(data)

    payload, error = await _ha_request(
        "POST",
        f"{hass_url}/api/services/{domain}/{service}",
        token,
        json_body=body,
    )
    if error is not None:
        return tool_failure("home_assistant_error", error)
    if payload is None:
        return tool_failure("home_assistant_error", "no response from Home Assistant")

    return tool_success({"result": payload})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_homeassistant_tools(
    registry: ToolRegistry,
    credential_resolver: Callable[[str], str],
) -> None:
    """Register Home Assistant tools if HASS_TOKEN is configured.

    Args:
        registry: The tool registry to register with.
        credential_resolver: A callable that resolves credential names
            from the process environment and data-dir ``.env``.
    """
    token = credential_resolver("HASS_TOKEN").strip()
    if not token:
        return

    hass_url = credential_resolver("HASS_URL").strip()
    if not hass_url:
        hass_url = _DEFAULT_HASS_URL

    async def list_entities_handler(
        context: ToolContext,
        arguments: JsonObject,
    ) -> JsonObject:
        return await _handle_list_entities(context, arguments, hass_url, token)

    async def get_state_handler(
        context: ToolContext,
        arguments: JsonObject,
    ) -> JsonObject:
        return await _handle_get_state(context, arguments, hass_url, token)

    async def list_services_handler(
        context: ToolContext,
        arguments: JsonObject,
    ) -> JsonObject:
        return await _handle_list_services(context, arguments, hass_url, token)

    async def call_service_handler(
        context: ToolContext,
        arguments: JsonObject,
    ) -> JsonObject:
        return await _handle_call_service(context, arguments, hass_url, token)

    registry.register(
        HA_LIST_ENTITIES_NAME,
        HA_LIST_ENTITIES_DESCRIPTION,
        HA_LIST_ENTITIES_PARAMETERS,
        list_entities_handler,
    )
    registry.register(
        HA_GET_STATE_NAME,
        HA_GET_STATE_DESCRIPTION,
        HA_GET_STATE_PARAMETERS,
        get_state_handler,
        display=ToolDisplay(summary_fields=("entity_id",)),
    )
    registry.register(
        HA_LIST_SERVICES_NAME,
        HA_LIST_SERVICES_DESCRIPTION,
        HA_LIST_SERVICES_PARAMETERS,
        list_services_handler,
    )
    registry.register(
        HA_CALL_SERVICE_NAME,
        HA_CALL_SERVICE_DESCRIPTION,
        HA_CALL_SERVICE_PARAMETERS,
        call_service_handler,
        display=ToolDisplay(summary_fields=("domain", "service", "entity_id")),
    )


__all__ = [
    "HA_CALL_SERVICE_DESCRIPTION",
    "HA_CALL_SERVICE_NAME",
    "HA_CALL_SERVICE_PARAMETERS",
    "HA_GET_STATE_DESCRIPTION",
    "HA_GET_STATE_NAME",
    "HA_GET_STATE_PARAMETERS",
    "HA_LIST_ENTITIES_DESCRIPTION",
    "HA_LIST_ENTITIES_NAME",
    "HA_LIST_ENTITIES_PARAMETERS",
    "HA_LIST_SERVICES_DESCRIPTION",
    "HA_LIST_SERVICES_NAME",
    "HA_LIST_SERVICES_PARAMETERS",
    "register_homeassistant_tools",
]
