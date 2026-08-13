"""Home Assistant bundled extension — 4 LLM-callable REST-API tools.

Thin HTTP wrappers around the built-in Home Assistant REST API
(``{url}/api/``). The tools are always registered; a readiness predicate on
each keeps them invisible in the prompt, the provider definitions, and the tool
pickers until ``HASS_TOKEN`` resolves to a non-empty string. The token
(``.env`` key ``HASS_TOKEN``) and the server URL (extension config field
``url``) are read **live** on every call, so setting either through Settings →
Extensions takes effect without a restart.
"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Any

import httpx

from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    tool_failure,
    tool_success,
)
from core.utils.http_status import HttpRequestFailure, is_retryable_status

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_HASS_URL = "http://homeassistant.local:8123"

_RETRY_MAX_RETRIES = 2
_RETRY_INITIAL_DELAY_SECONDS = 1.0
_RETRY_BACKOFF_FACTOR = 2
_RETRY_JITTER_FACTOR = 0.5

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
    "List Home Assistant entities, optionally filtered by domain or friendly-name area text."
)
HA_LIST_ENTITIES_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "minLength": 1,
            "pattern": "^[a-z][a-z0-9_]*$",
            "description": "Domain to include (e.g. light or climate). Omit for all domains.",
        },
        "area": {
            "type": "string",
            "minLength": 1,
            "description": "Case-insensitive friendly_name text to match. Omit for all areas.",
        },
    },
    "required": [],
}

HA_GET_STATE_NAME = "ha_get_state"
HA_GET_STATE_DESCRIPTION = "Get the full state object for a single Home Assistant entity."
HA_GET_STATE_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "minLength": 1,
            "pattern": r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$",
            "description": "Home Assistant entity id (e.g. light.living_room).",
        },
    },
    "required": ["entity_id"],
}

HA_LIST_SERVICES_NAME = "ha_list_services"
HA_LIST_SERVICES_DESCRIPTION = (
    "List Home Assistant services, optionally by domain. Use before ha_call_service "
    "to discover available actions and parameters."
)
HA_LIST_SERVICES_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "minLength": 1,
            "pattern": "^[a-z][a-z0-9_]*$",
            "description": "Domain to include (e.g. light or climate). Omit for all domains.",
        },
    },
    "required": [],
}

HA_CALL_SERVICE_NAME = "ha_call_service"
HA_CALL_SERVICE_DESCRIPTION = (
    "Call a Home Assistant service. Use ha_list_services to discover names and parameters."
)
HA_TOOL_FAMILY = "home_assistant"
HA_CALL_SERVICE_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "minLength": 1,
            "pattern": "^[a-z][a-z0-9_]*$",
            "description": "Service domain (e.g. light or climate).",
        },
        "service": {
            "type": "string",
            "minLength": 1,
            "pattern": "^[a-z][a-z0-9_]*$",
            "description": "Service name (e.g. turn_on or set_temperature).",
        },
        "entity_id": {
            "type": "string",
            "minLength": 1,
            "pattern": r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$",
            "description": "Target entity id. Omit for service-defined targeting.",
        },
        "data": {
            "type": "object",
            "description": "Service data. Omit when none; put entity_id in the top-level field.",
        },
    },
    "required": ["domain", "service"],
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


def _validate_arguments(arguments: JsonObject, allowed: frozenset[str]) -> JsonObject | None:
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        return tool_failure(
            "validation_error",
            f"Unknown argument(s): {', '.join(unknown)}",
            retryable=False,
        )
    return None


def _optional_text_argument(arguments: JsonObject, name: str) -> tuple[str, JsonObject | None]:
    raw = arguments.get(name)
    if raw is None:
        return "", None
    if not isinstance(raw, str):
        return "", tool_failure(
            "validation_error",
            f"{name} must be a string",
            retryable=False,
        )
    return raw.strip(), None


def _invalid_entity_id_failure(entity_id: str) -> JsonObject:
    return tool_failure(
        "validation_error", f"invalid entity_id format: {entity_id}", retryable=False
    )


def _ha_failure_envelope(failure: HttpRequestFailure) -> JsonObject:
    """Map a classified HA request failure onto the home_assistant_error envelope."""
    return tool_failure(
        "home_assistant_error",
        failure.message,
        retryable=failure.retryable,
        attempts_made=failure.attempts_made,
    )


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
    logger: Any,
    json_body: JsonObject | None = None,
) -> tuple[JsonObject | None, HttpRequestFailure | None]:
    """Make an HTTP request to the Home Assistant REST API with retries.

    Args:
        method: HTTP method (GET, POST).
        url: Full URL to the HA API endpoint.
        token: HA Long-Lived Access Token.
        logger: The extension's logger; the token is never logged.
        json_body: Optional JSON body for POST requests.

    Returns:
        Tuple of (response_json, failure). One is always None; the failure
        carries the retry classification for the result envelope.
    """
    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    idempotent = method == "GET"

    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        for attempt in range(_RETRY_MAX_RETRIES + 1):
            try:
                if method == "GET":
                    response = await client.get(url, headers=headers)
                elif method == "POST":
                    response = await client.post(url, headers=headers, json=json_body)
                else:
                    return None, HttpRequestFailure(f"unsupported HTTP method: {method}")
            except httpx.RequestError as error:
                if attempt >= _RETRY_MAX_RETRIES:
                    logger.warning("Home Assistant request failed: %s", error)
                    return None, HttpRequestFailure(
                        f"request failed: {error}",
                        retryable=True,
                        attempts_made=_RETRY_MAX_RETRIES + 1,
                    )
                await _sleep_for_retry(attempt)
                continue

            if response.status_code >= 400:
                # Only GET reads are idempotent; POST service calls are not.
                if (
                    is_retryable_status(response.status_code, idempotent=idempotent)
                    and attempt < _RETRY_MAX_RETRIES
                ):
                    await _sleep_for_retry(attempt)
                    continue
                detail = _extract_error_detail(response)
                logger.warning(
                    "Home Assistant request failed: HTTP %s: %s",
                    response.status_code,
                    detail,
                )
                # A retryable status only reaches here once retries were exhausted.
                retryable = is_retryable_status(response.status_code, idempotent=idempotent)
                return None, HttpRequestFailure(
                    f"HTTP {response.status_code}: {detail}",
                    retryable=retryable,
                    attempts_made=(_RETRY_MAX_RETRIES + 1) if retryable else None,
                )

            try:
                payload = response.json()
            except ValueError:
                return None, HttpRequestFailure("Home Assistant returned invalid JSON")

            return payload, None

    return None, HttpRequestFailure("request failed")


# ---------------------------------------------------------------------------
# 1. ha_list_entities
# ---------------------------------------------------------------------------


async def _handle_list_entities(
    context: ToolContext,
    arguments: JsonObject,
    hass_url: str,
    token: str,
    logger: Any,
) -> JsonObject:
    del context

    if argument_failure := _validate_arguments(arguments, frozenset({"domain", "area"})):
        return argument_failure
    domain, argument_failure = _optional_text_argument(arguments, "domain")
    if argument_failure is not None:
        return argument_failure
    area, argument_failure = _optional_text_argument(arguments, "area")
    if argument_failure is not None:
        return argument_failure
    domain = domain.lower()
    area = area.lower()
    if domain and not _DOMAIN_SERVICE_RE.match(domain):
        return tool_failure("validation_error", f"invalid domain: {domain}", retryable=False)

    payload, request_failure = await _ha_request("GET", f"{hass_url}/api/states", token, logger)
    if request_failure is not None:
        return _ha_failure_envelope(request_failure)
    if payload is None:
        return tool_failure(
            "home_assistant_error", "no response from Home Assistant", retryable=False
        )

    if not isinstance(payload, list):
        return tool_failure(
            "home_assistant_error",
            "unexpected response format from /api/states",
            retryable=False,
        )

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
    logger: Any,
) -> JsonObject:
    del context

    if argument_failure := _validate_arguments(arguments, frozenset({"entity_id"})):
        return argument_failure
    entity_id, argument_failure = _optional_text_argument(arguments, "entity_id")
    if argument_failure is not None:
        return argument_failure
    if not entity_id:
        return tool_failure("validation_error", "entity_id is required", retryable=False)
    if not _ENTITY_ID_RE.match(entity_id):
        return _invalid_entity_id_failure(entity_id)

    payload, request_failure = await _ha_request(
        "GET",
        f"{hass_url}/api/states/{entity_id}",
        token,
        logger,
    )
    if request_failure is not None:
        return _ha_failure_envelope(request_failure)
    if payload is None:
        return tool_failure(
            "home_assistant_error", f"entity {entity_id} not found", retryable=False
        )

    if not isinstance(payload, dict):
        return tool_failure("home_assistant_error", "unexpected response format", retryable=False)

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
    logger: Any,
) -> JsonObject:
    del context

    if argument_failure := _validate_arguments(arguments, frozenset({"domain"})):
        return argument_failure
    domain_filter, argument_failure = _optional_text_argument(arguments, "domain")
    if argument_failure is not None:
        return argument_failure
    domain_filter = domain_filter.lower()
    if domain_filter and not _DOMAIN_SERVICE_RE.match(domain_filter):
        return tool_failure(
            "validation_error",
            f"invalid domain: {domain_filter}",
            retryable=False,
        )

    payload, request_failure = await _ha_request("GET", f"{hass_url}/api/services", token, logger)
    if request_failure is not None:
        return _ha_failure_envelope(request_failure)
    if payload is None:
        return tool_failure(
            "home_assistant_error", "no response from Home Assistant", retryable=False
        )

    if not isinstance(payload, list):
        return tool_failure(
            "home_assistant_error",
            "unexpected response format from /api/services",
            retryable=False,
        )

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
    logger: Any,
) -> JsonObject:
    del context

    if argument_failure := _validate_arguments(
        arguments,
        frozenset({"domain", "service", "entity_id", "data"}),
    ):
        return argument_failure
    domain, argument_failure = _optional_text_argument(arguments, "domain")
    if argument_failure is not None:
        return argument_failure
    service, argument_failure = _optional_text_argument(arguments, "service")
    if argument_failure is not None:
        return argument_failure
    entity_id, argument_failure = _optional_text_argument(arguments, "entity_id")
    if argument_failure is not None:
        return argument_failure
    raw_data = arguments.get("data")
    if raw_data is not None and not isinstance(raw_data, dict):
        return tool_failure("validation_error", "data must be an object", retryable=False)
    data = _normalize_json_object(raw_data)
    domain = domain.lower()
    service = service.lower()

    if not domain:
        return tool_failure("validation_error", "domain is required", retryable=False)
    if not service:
        return tool_failure("validation_error", "service is required", retryable=False)
    if not _DOMAIN_SERVICE_RE.match(domain):
        return tool_failure("validation_error", f"invalid domain: {domain}", retryable=False)
    if not _DOMAIN_SERVICE_RE.match(service):
        return tool_failure("validation_error", f"invalid service: {service}", retryable=False)
    if entity_id and not _ENTITY_ID_RE.match(entity_id):
        return _invalid_entity_id_failure(entity_id)
    if data is not None and "entity_id" in data:
        return tool_failure(
            "validation_error", "data.entity_id is not allowed; use entity_id", retryable=False
        )

    if domain in _BLOCKED_DOMAINS:
        return tool_failure(
            "blocked_domain",
            f"domain '{domain}' is blocked for security reasons",
            retryable=False,
        )

    body: JsonObject = {}
    if entity_id:
        body["entity_id"] = entity_id
    if data:
        body.update(data)

    payload, request_failure = await _ha_request(
        "POST",
        f"{hass_url}/api/services/{domain}/{service}",
        token,
        logger,
        json_body=body,
    )
    if request_failure is not None:
        return _ha_failure_envelope(request_failure)
    if payload is None:
        return tool_failure(
            "home_assistant_error", "no response from Home Assistant", retryable=False
        )

    return tool_success({"result": payload})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_HASS_TOKEN_ENV_KEY = "HASS_TOKEN"
_URL_CONFIG_KEY = "url"

# Shown by tool.list for the four not-ready HA tools until the token is set.
# Server-delivered English text (plain ASCII), like a tool description — not i18n.
_HASS_READINESS_HINT = (
    "Requires a Home Assistant connection - set the server URL and token in Settings -> Extensions."
)


def _resolve_token(api: Any) -> str:
    """Resolve the HA token live (process env, then data-dir ``.env``)."""
    return str(api.resolve_credential(_HASS_TOKEN_ENV_KEY)).strip()


def _resolve_url(api: Any) -> str:
    """Resolve the HA server URL live from extension config, default otherwise.

    The trailing slash is stripped because the URL is user-typed in the settings
    form; the endpoint paths are always joined with a leading ``/``.
    """
    configured = str(api.get_config().get(_URL_CONFIG_KEY) or "").strip()
    return (configured or _DEFAULT_HASS_URL).rstrip("/")


def _missing_token_failure() -> JsonObject:
    """Defense-in-depth failure for an empty token at call time."""
    return tool_failure("home_assistant_error", "HASS_TOKEN is not configured", retryable=False)


def register(api: Any) -> None:
    """Register the Home Assistant tools and their settings schema.

    All four tools are always registered; a shared readiness predicate on the
    token keeps them out of the model-facing surfaces until it is set. Token and
    URL are resolved live on every call so a settings change applies without a
    restart.
    """
    api.register_settings(
        [
            {
                "key": "url",
                "type": "text",
                "label": "Server URL",
                "description": "Base URL of your Home Assistant instance.",
                "default": _DEFAULT_HASS_URL,
            },
            {
                "key": "token",
                "type": "secret",
                "label": "Access token",
                "description": "Home Assistant long-lived access token.",
                "env_key": _HASS_TOKEN_ENV_KEY,
            },
        ]
    )

    def _is_ready() -> bool:
        return bool(api.resolve_credential(_HASS_TOKEN_ENV_KEY).strip())

    async def list_entities_handler(
        context: ToolContext,
        arguments: JsonObject,
    ) -> JsonObject:
        token = _resolve_token(api)
        if not token:
            return _missing_token_failure()
        return await _handle_list_entities(context, arguments, _resolve_url(api), token, api.logger)

    async def get_state_handler(
        context: ToolContext,
        arguments: JsonObject,
    ) -> JsonObject:
        token = _resolve_token(api)
        if not token:
            return _missing_token_failure()
        return await _handle_get_state(context, arguments, _resolve_url(api), token, api.logger)

    async def list_services_handler(
        context: ToolContext,
        arguments: JsonObject,
    ) -> JsonObject:
        token = _resolve_token(api)
        if not token:
            return _missing_token_failure()
        return await _handle_list_services(context, arguments, _resolve_url(api), token, api.logger)

    async def call_service_handler(
        context: ToolContext,
        arguments: JsonObject,
    ) -> JsonObject:
        token = _resolve_token(api)
        if not token:
            return _missing_token_failure()
        return await _handle_call_service(context, arguments, _resolve_url(api), token, api.logger)

    api.register_tool_family(HA_TOOL_FAMILY, "Home Assistant")

    api.register_tool(
        HA_LIST_ENTITIES_NAME,
        HA_LIST_ENTITIES_DESCRIPTION,
        HA_LIST_ENTITIES_PARAMETERS,
        list_entities_handler,
        result_schema={"type": "object", "required": ["count", "entities"]},
        parallel_safe=True,
        open_input_schema=True,
        ready=_is_ready,
        readiness_hint=_HASS_READINESS_HINT,
        family=HA_TOOL_FAMILY,
    )
    api.register_tool(
        HA_GET_STATE_NAME,
        HA_GET_STATE_DESCRIPTION,
        HA_GET_STATE_PARAMETERS,
        get_state_handler,
        result_schema={
            "type": "object",
            "required": ["entity_id", "state", "attributes", "last_changed", "last_updated"],
        },
        display=ToolDisplay(summary_fields=("entity_id",)),
        parallel_safe=True,
        open_input_schema=True,
        ready=_is_ready,
        readiness_hint=_HASS_READINESS_HINT,
        family=HA_TOOL_FAMILY,
    )
    api.register_tool(
        HA_LIST_SERVICES_NAME,
        HA_LIST_SERVICES_DESCRIPTION,
        HA_LIST_SERVICES_PARAMETERS,
        list_services_handler,
        result_schema={"type": "object", "required": ["count", "domains"]},
        parallel_safe=True,
        open_input_schema=True,
        ready=_is_ready,
        readiness_hint=_HASS_READINESS_HINT,
        family=HA_TOOL_FAMILY,
    )
    api.register_tool(
        HA_CALL_SERVICE_NAME,
        HA_CALL_SERVICE_DESCRIPTION,
        HA_CALL_SERVICE_PARAMETERS,
        call_service_handler,
        result_schema={"type": "object", "required": ["result"]},
        display=ToolDisplay(summary_fields=("domain", "service", "entity_id")),
        open_input_schema=True,
        ready=_is_ready,
        readiness_hint=_HASS_READINESS_HINT,
        family=HA_TOOL_FAMILY,
    )
