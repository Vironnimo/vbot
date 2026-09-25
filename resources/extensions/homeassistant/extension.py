"""Home Assistant bundled extension — 4 LLM-callable REST-API tools.

Thin HTTP wrappers around the built-in Home Assistant REST API
(``{url}/api/``). The tools are always registered; a readiness predicate on
each keeps them invisible in the prompt, the provider definitions, and the tool
pickers until ``HASS_TOKEN`` resolves to a non-empty string. The token
(``.env`` key ``HASS_TOKEN``) and the server URL (extension config field
``url``) are read **live** on every call, so setting either through Settings →
Extensions takes effect without a restart.

``_arguments`` repairs call shapes before validation; ``_listing`` renders the
REST payloads as short text lines. Failures name the exact next call.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    tool_failure,
    tool_success,
)
from core.utils.http_status import is_retryable_status
from core.utils.tls import shared_ssl_context

from ._arguments import (
    ENTITY_ID_RE,
    call_service_normalizer,
    call_text,
    get_state_normalizer,
    list_entities_normalizer,
    list_services_normalizer,
    split_entity_ids,
)
from ._listing import (
    LINE_LIMIT,
    area_suggestions,
    candidate_text,
    close_names,
    domain_overview,
    entity_candidates,
    entity_line,
    matches_area,
    service_detail,
    service_line,
    states,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_HASS_URL = "http://homeassistant.local:8123"

_RETRY_MAX_RETRIES = 2
_RETRY_INITIAL_DELAY_SECONDS = 1.0
_RETRY_BACKOFF_FACTOR = 2
_RETRY_JITTER_FACTOR = 0.5

_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=15.0)

# Transport errors raised before the request left vBot; only these may repeat a POST.
_UNSENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# Statuses that refuse a service call before it runs, so a POST may repeat them.
_REFUSED_STATUSES = frozenset({429, 503})

# Unchanged service targets whose current state a service result reports.
_TARGET_CHECK_LIMIT = 10
_RESPONSE_CHARACTERS = 16000

# Values that could be a mistyped name, worth looking up for candidates.
_NAME_LIKE_RE = re.compile(r"^[\w .'()-]+$")

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
    "List Home Assistant entities as lines of entity_id, state and name. Filter by domain or by "
    "area text matched against names and ids; a long unfiltered list returns counts per domain."
)
HA_LIST_ENTITIES_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "description": "Entity domain, such as light or sensor. Omit for all.",
        },
        "area": {
            "type": "string",
            "description": "Text to find in entity names and ids, such as kitchen. Omit for all.",
        },
    },
    "required": [],
}

HA_GET_STATE_NAME = "ha_get_state"
HA_GET_STATE_DESCRIPTION = "Get one Home Assistant entity's state and attributes."
HA_GET_STATE_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "minLength": 1,
            "description": "Entity id, such as light.kitchen.",
        },
    },
    "required": ["entity_id"],
}

HA_LIST_SERVICES_NAME = "ha_list_services"
HA_LIST_SERVICES_DESCRIPTION = (
    "List Home Assistant services (actions). Without domain: service names per domain. With "
    "domain: each service's fields (* required). Use before ha_call_service."
)
HA_LIST_SERVICES_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "description": "Domain, such as light. Omit for all domains.",
        },
    },
    "required": [],
}

HA_CALL_SERVICE_NAME = "ha_call_service"
HA_CALL_SERVICE_DESCRIPTION = (
    "Call a Home Assistant service (action) such as light.turn_on on entity_id, with optional "
    "data fields from ha_list_services. Returns the entity states it changed."
)
HA_TOOL_FAMILY = "home_assistant"
HA_CALL_SERVICE_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "description": "Service domain, such as light.",
        },
        "service": {
            "type": "string",
            "description": "Service name, such as turn_on.",
        },
        "entity_id": {
            "type": "string",
            "description": (
                "Target entity id, such as light.kitchen; several separated by commas. "
                "Omit for services without a target."
            ),
        },
        "data": {
            "type": "object",
            "description": 'Service fields, such as {"brightness_pct": 50}.',
        },
    },
    "required": ["domain", "service"],
}

_LIST_SERVICES_UNADVERTISED: JsonObject = {"service": {"type": "string"}}
_CALL_SERVICE_UNADVERTISED: JsonObject = {"return_response": {"type": "boolean"}}

# ---------------------------------------------------------------------------
# Requests and their failures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Failure:
    """A Home Assistant request that returned no usable payload."""

    status: int | None = None  # HTTP status; None for transport and format problems
    detail: str = ""
    retryable: bool = False
    attempts: int = 1
    sent: bool = True  # whether the request may have reached Home Assistant
    invalid_json: bool = False


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
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()

    # Home Assistant answers some refusals with a bare "400: Bad Request" body.
    fallback = response.text.strip().removeprefix(f"{response.status_code}: ")
    if fallback:
        return fallback[:300]
    return response.reason_phrase or "request failed"


def _error_text(error: Exception) -> str:
    text = str(error).strip()
    return (f"{type(error).__name__}: {text}" if text else type(error).__name__)[:200]


def _status_retryable(status: int, idempotent: bool) -> bool:
    if idempotent:
        return is_retryable_status(status, idempotent=True)
    return status in _REFUSED_STATUSES


async def _ha_request(
    method: str,
    url: str,
    token: str,
    logger: Any,
    json_body: JsonObject | None = None,
) -> tuple[Any, _Failure | None]:
    """Make one Home Assistant REST request, retrying only what is safe to repeat.

    A GET repeats on transport errors and transient statuses. A POST service
    call repeats only when Home Assistant cannot have run it: the connection
    was never made, or the status refuses the call (429, 503). The token is
    never logged.

    Returns:
        Tuple of (payload, failure); exactly one is None.
    """
    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    idempotent = method == "GET"

    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, verify=shared_ssl_context()) as client:
        for attempt in range(_RETRY_MAX_RETRIES + 1):
            try:
                if idempotent:
                    response = await client.get(url, headers=headers)
                else:
                    response = await client.post(url, headers=headers, json=json_body)
            except httpx.RequestError as error:
                sent = not isinstance(error, _UNSENT_ERRORS)
                repeatable = idempotent or not sent
                if repeatable and attempt < _RETRY_MAX_RETRIES:
                    await _sleep_for_retry(attempt)
                    continue
                logger.warning("Home Assistant request failed: %s", _error_text(error))
                return None, _Failure(
                    detail=_error_text(error),
                    retryable=repeatable,
                    attempts=attempt + 1,
                    sent=sent,
                )

            if response.status_code >= 400:
                retryable = _status_retryable(response.status_code, idempotent)
                if retryable and attempt < _RETRY_MAX_RETRIES:
                    await _sleep_for_retry(attempt)
                    continue
                detail = _extract_error_detail(response)
                logger.warning(
                    "Home Assistant request failed: HTTP %s: %s",
                    response.status_code,
                    detail,
                )
                return None, _Failure(
                    status=response.status_code,
                    detail=detail,
                    retryable=retryable,
                    attempts=attempt + 1,
                )

            try:
                return response.json(), None
            except ValueError:
                return None, _Failure(detail="not JSON", invalid_json=True)

    return None, _Failure(detail="request failed")


def _attempts(failure: _Failure) -> str:
    return f" after {failure.attempts} attempts" if failure.attempts > 1 else ""


def _failure_result(
    failure: _Failure,
    hass_url: str,
    *,
    effect: str | None = None,
    check: str | None = None,
) -> JsonObject:
    """Return a failed request's result; ``effect`` names a service call that may have run."""
    status, detail = failure.status, failure.detail
    settings = "the server URL in Settings -> Extensions"
    if status in (401, 403):
        message = (
            f"Home Assistant rejected the access token (HTTP {status}). Tell the user to check "
            "the Home Assistant token in Settings -> Extensions."
        )
    elif failure.invalid_json:
        message = (
            f"Home Assistant at {hass_url} answered with something other than JSON. Check that "
            f"{settings} points to Home Assistant."
        )
    elif status is None and effect is not None and failure.sent:
        message = (
            f"The connection to Home Assistant failed after {effect} was sent ({detail}), so it "
            f"may or may not have run. Check with {check} before calling it again."
        )
    elif status is None:
        skipped = f"; {effect} was not called" if effect is not None else ""
        message = (
            f"Could not reach Home Assistant at {hass_url} ({detail}){_attempts(failure)}"
            f"{skipped}. Check that Home Assistant is running and that {settings} is right, "
            "then try again later."
        )
    elif effect is not None and status >= 500 and status not in _REFUSED_STATUSES:
        message = (
            f"Home Assistant failed while running {effect} (HTTP {status}: {detail}); it may "
            f"have partly run. Check with {check} before calling it again."
        )
    elif status >= 500 or status in _REFUSED_STATUSES:
        message = (
            f"Home Assistant is busy or unavailable (HTTP {status}: {detail}){_attempts(failure)}. "
            "Try again later."
        )
    else:
        message = f"Home Assistant answered HTTP {status}: {detail}."
        if status == 404:
            message += f" Check that {settings} points to Home Assistant."
    return tool_failure(
        "home_assistant_error",
        message,
        retryable=failure.retryable,
        attempts_made=failure.attempts if failure.retryable and failure.attempts > 1 else None,
    )


def _unexpected(hass_url: str, path: str) -> JsonObject:
    return tool_failure(
        "home_assistant_error",
        f"Home Assistant at {hass_url} answered {path} in an unexpected format. Check that the "
        "server URL in Settings -> Extensions points to Home Assistant.",
        retryable=False,
    )


async def _all_states(hass_url: str, token: str, logger: Any) -> list[dict[str, Any]]:
    """Return every entity state for candidate lists; empty when the read fails."""
    payload, failure = await _ha_request("GET", f"{hass_url}/api/states", token, logger)
    return [] if failure is not None else states(payload)


def _candidates_sentence(
    candidates: list[dict[str, Any]],
    repeat: Callable[[str], dict[str, Any]],
    tool: str,
    fallback: dict[str, Any],
) -> str:
    """Say which entities a failed reference may mean and how to continue."""
    if len(candidates) == 1:
        call = repeat(candidates[0]["entity_id"])
        return (
            f" Home Assistant has {candidate_text(candidates)}; if you mean it, call {tool} "
            f"{call_text(call)}."
        )
    if candidates:
        return (
            f" Entities that may match: {candidate_text(candidates)}. Repeat the call with the "
            "entity_id you mean."
        )
    return f" Find the id with ha_list_entities {call_text(fallback)}."


async def _entity_reference_failure(
    code: str,
    message: str,
    entity: str,
    repeat: Callable[[str], dict[str, Any]],
    tool: str,
    hass_url: str,
    token: str,
    logger: Any,
) -> JsonObject:
    """Fail an unusable entity reference, naming the entities it may mean.

    ``repeat`` returns the corrected call for one candidate entity id.
    """
    domain, dot, object_id = entity.partition(".")
    if dot and not object_id.strip("."):
        fallback = {"domain": domain}
    else:
        fallback = {"area": (object_id.strip(".") or entity).replace("_", " ")}
    if _NAME_LIKE_RE.match(entity):
        candidates = entity_candidates(await _all_states(hass_url, token, logger), entity)
        message += _candidates_sentence(candidates, repeat, tool, fallback)
    else:
        message += " Find ids with ha_list_entities {}."
    return tool_failure(code, message, retryable=False)


def _repeat_call(arguments: JsonObject, entity: str) -> Callable[[str], dict[str, Any]]:
    """Return the call that repeats ``arguments`` with one entity id replaced."""
    ids = split_entity_ids(str(arguments.get("entity_id") or ""))

    def repeat(candidate: str) -> dict[str, Any]:
        replaced = [candidate if item == entity else item for item in ids] or [candidate]
        return {**arguments, "entity_id": ",".join(dict.fromkeys(replaced))}

    return repeat


# ---------------------------------------------------------------------------
# 1. ha_list_entities
# ---------------------------------------------------------------------------


def _no_match_note(entries: list[dict[str, Any]], domain: str, area: str) -> str:
    if not entries:
        return "Home Assistant has no entities."
    domains = sorted({entry["entity_id"].partition(".")[0] for entry in entries})
    if domain and domain not in domains:
        note = f'No entities in domain "{domain}". Domains with entities: {", ".join(domains)}.'
        if close := close_names(domain, domains):
            call = {"domain": close[0], **({"area": area} if area else {})}
            note += f" The closest is {close[0]}: {call_text(call)}."
        return note
    scope = [
        entry for entry in entries if not domain or entry["entity_id"].startswith(f"{domain}.")
    ]
    where = f"domain {domain}" if domain else "any domain"
    note = f'No entity name or id in {where} contains "{area}".'
    if words := area_suggestions(scope, area):
        call = {**({"domain": domain} if domain else {}), "area": words[0]}
        return note + f" Similar name words: {', '.join(words)}; try {call_text(call)}."
    listing = call_text({"domain": domain}) if domain else "{}"
    return note + f" List {'the domain' if domain else 'counts per domain'} with {listing}."


def _page_note(matched: list[dict[str, Any]], domain: str, area: str) -> str:
    note = f"Showing the first {LINE_LIMIT} of {len(matched)} entities."
    if not area:
        return note + (
            f" Narrow with area text, such as {call_text({'domain': domain, 'area': 'kitchen'})}."
        )
    if not domain:
        busiest = max(
            {entry["entity_id"].partition(".")[0] for entry in matched},
            key=lambda name: sum(entry["entity_id"].startswith(f"{name}.") for entry in matched),
        )
        call = {"domain": busiest, "area": area}
        return note + f" Narrow with a domain, such as {call_text(call)}."
    return note + " Use more specific area text."


async def _handle_list_entities(
    context: ToolContext,
    arguments: JsonObject,
    hass_url: str,
    token: str,
    logger: Any,
) -> JsonObject:
    del context
    domain = str(arguments.get("domain") or "")
    area = str(arguments.get("area") or "")

    payload, failure = await _ha_request("GET", f"{hass_url}/api/states", token, logger)
    if failure is not None:
        return _failure_result(failure, hass_url)
    if not isinstance(payload, list):
        return _unexpected(hass_url, "/api/states")

    entries = states(payload)
    matched = [
        entry
        for entry in entries
        if (not domain or entry["entity_id"].startswith(f"{domain}."))
        and (not area or matches_area(entry, area))
    ]
    if not matched:
        return tool_success({"count": 0, "note": _no_match_note(entries, domain, area)})
    if not domain and not area and len(matched) > LINE_LIMIT:
        content, note = domain_overview(matched)
        return tool_success({"count": len(matched), "note": note, "content": content})
    data: JsonObject = {"count": len(matched)}
    if len(matched) > LINE_LIMIT:
        data["note"] = _page_note(matched, domain, area)
    data["content"] = "\n".join(entity_line(entry) for entry in matched[:LINE_LIMIT])
    return tool_success(data)


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
    entity_id = str(arguments["entity_id"])
    if not ENTITY_ID_RE.match(entity_id):
        return await _entity_reference_failure(
            "invalid_arguments",
            f'ha_get_state was not run: "{entity_id}" is not an entity id; ids look like '
            "light.kitchen.",
            entity_id,
            _repeat_call(arguments, entity_id),
            HA_GET_STATE_NAME,
            hass_url,
            token,
            logger,
        )

    payload, failure = await _ha_request("GET", f"{hass_url}/api/states/{entity_id}", token, logger)
    if failure is not None and failure.status == 404:
        return await _entity_reference_failure(
            "entity_not_found",
            f"Home Assistant has no entity {entity_id}.",
            entity_id,
            _repeat_call(arguments, entity_id),
            HA_GET_STATE_NAME,
            hass_url,
            token,
            logger,
        )
    if failure is not None:
        return _failure_result(failure, hass_url)
    if not isinstance(payload, dict):
        return _unexpected(hass_url, f"/api/states/{entity_id}")

    data: JsonObject = {
        "entity_id": payload.get("entity_id", entity_id),
        "state": payload.get("state"),
        "attributes": payload.get("attributes") or {},
        "last_changed": payload.get("last_changed"),
    }
    if payload.get("last_updated") != payload.get("last_changed"):
        data["last_updated"] = payload.get("last_updated")
    return tool_success(data)


# ---------------------------------------------------------------------------
# 3. ha_list_services
# ---------------------------------------------------------------------------


def _catalog(payload: list[Any]) -> dict[str, dict[str, Any]]:
    return {
        entry["domain"]: entry["services"]
        for entry in payload
        if isinstance(entry, dict)
        and isinstance(entry.get("domain"), str)
        and isinstance(entry.get("services"), dict)
    }


def _unknown_domain(domain: str, catalog: dict[str, dict[str, Any]], effect: str) -> JsonObject:
    message = f"Home Assistant has no domain {domain}{effect}."
    if close := close_names(domain, list(catalog)):
        message += (
            f" Close domains: {', '.join(close)}; list one with ha_list_services "
            f"{call_text({'domain': close[0]})}."
        )
    else:
        message += " List all domains with ha_list_services {}."
    return tool_failure("service_not_found", message, retryable=False)


def _unknown_service(
    domain: str,
    service: str,
    services: dict[str, Any],
    effect: str,
    repeat: tuple[str, dict[str, Any]],
) -> JsonObject:
    message = (
        f"Home Assistant has no service {domain}.{service}{effect}. Services of {domain}: "
        f"{', '.join(sorted(services))}."
    )
    if close := close_names(service, list(services)):
        tool, call = repeat
        corrected = {**call, "service": close[0]}
        message += f" If you mean {close[0]}, call {tool} {call_text(corrected)}."
    return tool_failure("service_not_found", message, retryable=False)


async def _handle_list_services(
    context: ToolContext,
    arguments: JsonObject,
    hass_url: str,
    token: str,
    logger: Any,
) -> JsonObject:
    del context
    domain = str(arguments.get("domain") or "")
    service = str(arguments.get("service") or "")

    payload, failure = await _ha_request("GET", f"{hass_url}/api/services", token, logger)
    if failure is not None:
        return _failure_result(failure, hass_url)
    if not isinstance(payload, list):
        return _unexpected(hass_url, "/api/services")

    catalog = _catalog(payload)
    if not domain:
        lines = [
            f"{name}: "
            + ("blocked in vBot" if name in _BLOCKED_DOMAINS else ", ".join(sorted(services)))
            for name, services in sorted(catalog.items())
        ]
        return tool_success({"count": len(catalog), "content": "\n".join(lines)})
    services = catalog.get(domain)
    if services is None:
        return _unknown_domain(domain, catalog, "")
    data: JsonObject = {}
    if domain in _BLOCKED_DOMAINS:
        data["note"] = f"vBot blocks calls to {domain} services."
    if service:
        definition = services.get(service)
        if not isinstance(definition, dict):
            return _unknown_service(
                domain, service, services, "", (HA_LIST_SERVICES_NAME, {"domain": domain})
            )
        return tool_success(
            {"count": 1, **data, "content": service_detail(domain, service, definition)}
        )
    lines = [
        service_line(name, definition)
        for name, definition in sorted(services.items())
        if isinstance(definition, dict)
    ]
    return tool_success({"count": len(lines), **data, "content": "\n".join(lines)})


# ---------------------------------------------------------------------------
# 4. ha_call_service
# ---------------------------------------------------------------------------


def _check_call(domain: str, entities: list[str]) -> str:
    if len(entities) == 1:
        return f"{HA_GET_STATE_NAME} {call_text({'entity_id': entities[0]})}"
    return f"{HA_LIST_ENTITIES_NAME} {call_text({'domain': domain})}"


async def _post_service(
    url: str,
    body: JsonObject,
    requested: bool,
    token: str,
    logger: Any,
) -> tuple[Any, _Failure | None, str | None]:
    """POST a service call; returns (payload, failure, response-request problem)."""
    payload, failure = await _ha_request(
        "POST", url + ("?return_response" if requested else ""), token, logger, json_body=body
    )
    if failure is None or failure.status != 400 or "return_response" not in failure.detail:
        return payload, failure, None
    if requested:
        return None, failure, "unsupported"
    # Home Assistant refuses a data-only service before running it unless the
    # call asks for its data, so asking now runs the call exactly once.
    payload, failure = await _ha_request(
        "POST", url + "?return_response", token, logger, json_body=body
    )
    return payload, failure, None


async def _rejected_call(
    arguments: JsonObject,
    failure: _Failure,
    hass_url: str,
    token: str,
    logger: Any,
) -> JsonObject:
    """Explain a service call Home Assistant refused as a bad request."""
    domain, service = str(arguments["domain"]), str(arguments["service"])
    effect = f"{domain}.{service}"
    payload, services_failure = await _ha_request("GET", f"{hass_url}/api/services", token, logger)
    catalog = _catalog(payload) if services_failure is None and isinstance(payload, list) else {}
    if catalog and domain not in catalog:
        return _unknown_domain(domain, catalog, f", so {effect} was not called")
    services = catalog.get(domain) or {}
    if catalog and service not in services:
        return _unknown_service(
            domain,
            service,
            services,
            f", so {effect} was not called",
            (HA_CALL_SERVICE_NAME, dict(arguments)),
        )
    fields = call_text({"domain": domain, "service": service})
    return tool_failure(
        "home_assistant_error",
        f"Home Assistant rejected {effect} (HTTP {failure.status}: {failure.detail}). Check its "
        f"fields with {HA_LIST_SERVICES_NAME} {fields}.",
        retryable=False,
    )


def _latest_states(payload: Any) -> list[dict[str, Any]]:
    """Return the last reported state of each changed entity, sorted by id."""
    # ``states`` sorts stably, so the dict keeps each entity's latest state.
    return list({entry["entity_id"]: entry for entry in states(payload)}.values())


def _response_fields(response: Any) -> JsonObject:
    if response is None:
        return {}
    text = call_text(response)
    if len(text) <= _RESPONSE_CHARACTERS:
        return {"response": response}
    return {
        "response": text[:_RESPONSE_CHARACTERS],
        "response_note": f"The response was cut at {_RESPONSE_CHARACTERS} characters.",
    }


async def _handle_call_service(
    context: ToolContext,
    arguments: JsonObject,
    hass_url: str,
    token: str,
    logger: Any,
) -> JsonObject:
    del context
    domain, service = str(arguments["domain"]), str(arguments["service"])
    effect = f"{domain}.{service}"
    entity_text = str(arguments.get("entity_id") or "")
    entities = split_entity_ids(entity_text)
    data = dict(arguments.get("data") or {})

    if domain in _BLOCKED_DOMAINS:
        return tool_failure(
            "blocked_domain",
            f"vBot blocks the Home Assistant domain {domain} because its services can run "
            f"commands or send requests from the server; {effect} was not called. Tell the user "
            "if they need it.",
            retryable=False,
        )
    for entity in entities:
        if not ENTITY_ID_RE.match(entity):
            return await _entity_reference_failure(
                "invalid_arguments",
                f'ha_call_service was not run: "{entity}" is not an entity id; ids look like '
                "light.kitchen.",
                entity,
                _repeat_call(arguments, entity),
                HA_CALL_SERVICE_NAME,
                hass_url,
                token,
                logger,
            )

    body = dict(data)
    if entities:
        body["entity_id"] = entities[0] if len(entities) == 1 else entities
    payload, failure, response_problem = await _post_service(
        f"{hass_url}/api/services/{domain}/{service}",
        body,
        bool(arguments.get("return_response")),
        token,
        logger,
    )
    if response_problem is not None:
        return tool_failure(
            "invalid_arguments",
            f"{effect} returns no data, so Home Assistant refused return_response and did not "
            "run it. Call it again without return_response.",
            retryable=False,
        )
    if failure is not None and failure.status in (400, 404):
        return await _rejected_call(arguments, failure, hass_url, token, logger)
    if failure is not None:
        return _failure_result(
            failure, hass_url, effect=effect, check=_check_call(domain, entities)
        )

    response = payload.get("service_response") if isinstance(payload, dict) else None
    if isinstance(payload, dict):
        payload = payload.get("changed_states")
    changed = _latest_states(payload)
    changed_ids = {entry["entity_id"] for entry in changed}
    attributes = tuple(key for key in data if isinstance(key, str))
    lines = [entity_line(entry, attributes) for entry in changed]
    missing: list[str] = []
    for entity in [entity for entity in entities if entity not in changed_ids][
        :_TARGET_CHECK_LIMIT
    ]:
        current, check_failure = await _ha_request(
            "GET", f"{hass_url}/api/states/{entity}", token, logger
        )
        if check_failure is not None and check_failure.status == 404:
            missing.append(entity)
        elif isinstance(current, dict):
            lines.append(f"{entity_line(current, attributes)} [unchanged]")

    if missing and not changed and len(missing) == len(entities):
        return await _entity_reference_failure(
            "entity_not_found",
            f"Home Assistant has no entity {missing[0]}, so {effect} changed nothing.",
            missing[0],
            _repeat_call(arguments, missing[0]),
            HA_CALL_SERVICE_NAME,
            hass_url,
            token,
            logger,
        )
    result: JsonObject = {"service": effect, "changed": len(changed)}
    if missing:
        listing = call_text({"domain": missing[0].partition(".")[0]})
        result["note"] = (
            f"Home Assistant has no entity {', '.join(missing)}, so {effect} did nothing for it. "
            f"Find the id with {HA_LIST_ENTITIES_NAME} {listing}."
        )
    elif not changed:
        result["note"] = "No entity state changed."
    result.update(_response_fields(response))
    if lines:
        result["content"] = "\n".join(lines)
    return tool_success(result)


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
    return tool_failure(
        "home_assistant_error",
        "Home Assistant is not connected: no access token is set. Tell the user to set it in "
        "Settings -> Extensions.",
        retryable=False,
    )


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
        argument_normalizer=list_entities_normalizer(
            HA_LIST_ENTITIES_NAME, HA_LIST_ENTITIES_PARAMETERS
        ),
        result_schema={"type": "object", "required": ["count"]},
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
        argument_normalizer=get_state_normalizer(HA_GET_STATE_NAME, HA_GET_STATE_PARAMETERS),
        result_schema={"type": "object", "required": ["entity_id", "state"]},
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
        argument_normalizer=list_services_normalizer(
            HA_LIST_SERVICES_NAME, HA_LIST_SERVICES_PARAMETERS
        ),
        result_schema={"type": "object", "required": ["count"]},
        parallel_safe=True,
        open_input_schema=True,
        unadvertised_parameters=_LIST_SERVICES_UNADVERTISED,
        ready=_is_ready,
        readiness_hint=_HASS_READINESS_HINT,
        family=HA_TOOL_FAMILY,
    )
    api.register_tool(
        HA_CALL_SERVICE_NAME,
        HA_CALL_SERVICE_DESCRIPTION,
        HA_CALL_SERVICE_PARAMETERS,
        call_service_handler,
        argument_normalizer=call_service_normalizer(
            HA_CALL_SERVICE_NAME, HA_CALL_SERVICE_PARAMETERS
        ),
        result_schema={"type": "object", "required": ["service", "changed"]},
        display=ToolDisplay(summary_fields=("domain", "service", "entity_id")),
        open_input_schema=True,
        unadvertised_parameters=_CALL_SERVICE_UNADVERTISED,
        ready=_is_ready,
        readiness_hint=_HASS_READINESS_HINT,
        family=HA_TOOL_FAMILY,
    )
