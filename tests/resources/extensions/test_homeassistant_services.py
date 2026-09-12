"""Homeassistant: services behavior."""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.resources.extensions.homeassistant_helpers import (
    _HASS_URL,
    HA_CALL_SERVICE_NAME,
    HA_LIST_SERVICES_NAME,
    _dispatch,
    _tools_with_token,
    assert_failure_envelope,
    assert_success_envelope,
)
from tests.resources.extensions.homeassistant_helpers import (
    _clean_extension_modules as _clean_extension_modules,
)


# ha_list_services
@respx.mock
@pytest.mark.asyncio
async def test_list_services_success() -> None:
    route = respx.get(f"{_HASS_URL}/api/services").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "domain": "light",
                    "services": {
                        "turn_on": {
                            "description": "Turn on a light",
                            "fields": {"brightness": {"description": "Brightness level"}},
                        },
                        "turn_off": {"description": "Turn off a light", "fields": {}},
                    },
                },
                {
                    "domain": "climate",
                    "services": {
                        "set_temperature": {
                            "description": "Set target temperature",
                            "fields": {},
                        },
                    },
                },
            ],
        )
    )
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_LIST_SERVICES_NAME, {})

    assert route.called is True
    data = assert_success_envelope(result)
    assert data["count"] == 2
    domains = data["domains"]
    assert domains[0]["domain"] == "light"
    assert "turn_on" in domains[0]["services"]
    assert "turn_off" in domains[0]["services"]


@respx.mock
@pytest.mark.asyncio
async def test_list_services_domain_filter() -> None:
    route = respx.get(f"{_HASS_URL}/api/services").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "domain": "light",
                    "services": {"turn_on": {"description": "Turn on", "fields": {}}},
                },
                {
                    "domain": "climate",
                    "services": {"set_temperature": {"description": "Set temp", "fields": {}}},
                },
            ],
        )
    )
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_LIST_SERVICES_NAME, {"domain": "climate"})

    assert route.called is True
    data = assert_success_envelope(result)
    assert data["count"] == 1
    assert data["domains"][0]["domain"] == "climate"


@respx.mock
@pytest.mark.asyncio
async def test_call_service_success() -> None:
    route = respx.post(f"{_HASS_URL}/api/services/light/turn_on").mock(
        return_value=httpx.Response(200, json=[{"entity_id": "light.living_room", "state": "on"}])
    )
    tools = _tools_with_token()

    result = await _dispatch(
        tools,
        HA_CALL_SERVICE_NAME,
        {
            "domain": "light",
            "service": "turn_on",
            "entity_id": "light.living_room",
            "data": {"brightness": 128},
        },
    )

    assert route.called is True
    data = assert_success_envelope(result)
    assert isinstance(data["result"], list)
    assert data["result"][0]["entity_id"] == "light.living_room"


@pytest.mark.asyncio
async def test_call_service_missing_domain() -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_CALL_SERVICE_NAME, {"service": "turn_on"})

    assert_failure_envelope(result, "invalid_arguments")


@pytest.mark.asyncio
async def test_call_service_missing_service() -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_CALL_SERVICE_NAME, {"domain": "light"})

    assert_failure_envelope(result, "invalid_arguments")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "domain",
    ["shell_command", "command_line", "python_script", "pyscript", "hassio", "rest_command"],
)
async def test_call_service_blocked_domain(domain: str) -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_CALL_SERVICE_NAME, {"domain": domain, "service": "run"})

    assert_failure_envelope(result, "blocked_domain")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "domain",
    [
        "",
        "invalid domain",
        "domain/slash",
        "has space",
    ],
)
async def test_call_service_invalid_domain(domain: str) -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_CALL_SERVICE_NAME, {"domain": domain, "service": "turn_on"})

    assert_failure_envelope(result, "invalid_arguments")


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity_id",
    [
        "invalid",
        "light.",
        ".living_room",
        "light/../sensor",
        "light..living_room",
        "Light.Living_Room",
    ],
)
async def test_call_service_invalid_entity_id(entity_id: str) -> None:
    route = respx.post(f"{_HASS_URL}/api/services/light/turn_on").mock(
        return_value=httpx.Response(200, json=[])
    )
    tools = _tools_with_token()

    result = await _dispatch(
        tools,
        HA_CALL_SERVICE_NAME,
        {"domain": "light", "service": "turn_on", "entity_id": entity_id},
    )

    assert_failure_envelope(result, "invalid_arguments")
    assert route.called is False


@respx.mock
@pytest.mark.asyncio
async def test_call_service_rejects_entity_id_in_data() -> None:
    route = respx.post(f"{_HASS_URL}/api/services/light/turn_on").mock(
        return_value=httpx.Response(200, json=[])
    )
    tools = _tools_with_token()

    result = await _dispatch(
        tools,
        HA_CALL_SERVICE_NAME,
        {
            "domain": "light",
            "service": "turn_on",
            "data": {"entity_id": "light/../sensor"},
        },
    )

    assert_failure_envelope(result, "validation_error")
    assert route.called is False


@respx.mock
@pytest.mark.asyncio
async def test_call_service_with_entity_and_data() -> None:
    route = respx.post(f"{_HASS_URL}/api/services/climate/set_temperature").mock(
        return_value=httpx.Response(200, json=[])
    )
    tools = _tools_with_token()

    result = await _dispatch(
        tools,
        HA_CALL_SERVICE_NAME,
        {
            "domain": "climate",
            "service": "set_temperature",
            "entity_id": "climate.living_room",
            "data": {"temperature": 22.5, "hvac_mode": "heat"},
        },
    )

    assert route.called is True
    assert_success_envelope(result)
    request_body = route.calls[0].request.content
    body = httpx.Response(200, content=request_body).json()
    assert body["entity_id"] == "climate.living_room"
    assert body["temperature"] == 22.5
