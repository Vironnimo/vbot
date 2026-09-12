"""Homeassistant: entities behavior."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
import respx

from tests.resources.extensions.homeassistant_helpers import (
    _EXTENSION_NAME,
    _HASS_URL,
    HA_CALL_SERVICE_NAME,
    HA_GET_STATE_NAME,
    HA_LIST_ENTITIES_NAME,
    HA_LIST_SERVICES_NAME,
    _dispatch,
    _no_sleep,
    _tools_with_token,
    assert_failure_envelope,
    assert_success_envelope,
)
from tests.resources.extensions.homeassistant_helpers import (
    _clean_extension_modules as _clean_extension_modules,
)


@respx.mock
@pytest.mark.asyncio
async def test_list_entities_success() -> None:
    route = respx.get(f"{_HASS_URL}/api/states").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "entity_id": "light.living_room",
                    "state": "on",
                    "attributes": {"friendly_name": "Living Room Light"},
                },
                {
                    "entity_id": "sensor.temperature",
                    "state": "22.5",
                    "attributes": {"friendly_name": "Temperature Sensor"},
                },
            ],
        )
    )
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    assert route.called is True
    data = assert_success_envelope(result)
    assert data["count"] == 2
    entities = data["entities"]
    assert len(entities) == 2
    assert entities[0]["entity_id"] == "light.living_room"
    assert entities[0]["state"] == "on"
    assert entities[0]["friendly_name"] == "Living Room Light"


@respx.mock
@pytest.mark.asyncio
async def test_list_entities_domain_filter() -> None:
    route = respx.get(f"{_HASS_URL}/api/states").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"entity_id": "light.living_room", "state": "on", "attributes": {}},
                {"entity_id": "sensor.temperature", "state": "22.5", "attributes": {}},
            ],
        )
    )
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {"domain": "light"})

    assert route.called is True
    data = assert_success_envelope(result)
    assert data["count"] == 1
    assert data["entities"][0]["entity_id"] == "light.living_room"


@respx.mock
@pytest.mark.asyncio
async def test_list_entities_area_filter() -> None:
    route = respx.get(f"{_HASS_URL}/api/states").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "entity_id": "light.kitchen",
                    "state": "off",
                    "attributes": {"friendly_name": "Kitchen Light"},
                },
                {
                    "entity_id": "light.living_room",
                    "state": "on",
                    "attributes": {"friendly_name": "Living Room Spot"},
                },
            ],
        )
    )
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {"area": "kitchen"})

    assert route.called is True
    data = assert_success_envelope(result)
    assert data["count"] == 1
    assert data["entities"][0]["entity_id"] == "light.kitchen"


@respx.mock
@pytest.mark.asyncio
async def test_list_entities_http_error(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 500 on an idempotent GET is retryable; stub the backoff sleep so the
    # exhausted-retries path still fails fast.
    respx.get(f"{_HASS_URL}/api/states").mock(
        return_value=httpx.Response(500, json={"message": "internal error"})
    )
    tools = _tools_with_token()
    _no_sleep(monkeypatch)

    with caplog.at_level(logging.WARNING, logger=f"vbot.extensions.{_EXTENSION_NAME}"):
        result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    assert_failure_envelope(result, "home_assistant_error")
    assert any(
        record.levelno == logging.WARNING and record.name == f"vbot.extensions.{_EXTENSION_NAME}"
        for record in caplog.records
    )


# ha_get_state
@respx.mock
@pytest.mark.asyncio
async def test_get_state_success() -> None:
    route = respx.get(f"{_HASS_URL}/api/states/light.living_room").mock(
        return_value=httpx.Response(
            200,
            json={
                "entity_id": "light.living_room",
                "state": "on",
                "attributes": {"brightness": 255},
                "last_changed": "2025-01-01T00:00:00+00:00",
                "last_updated": "2025-01-01T12:00:00+00:00",
            },
        )
    )
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_GET_STATE_NAME, {"entity_id": "light.living_room"})

    assert route.called is True
    data = assert_success_envelope(result)
    assert data["entity_id"] == "light.living_room"
    assert data["state"] == "on"
    assert data["attributes"] == {"brightness": 255}


@pytest.mark.asyncio
async def test_get_state_missing_entity_id() -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_GET_STATE_NAME, {})

    assert_failure_envelope(result, "invalid_arguments")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity_id",
    [
        "",
        "invalid",
        "light.",
        ".living_room",
        "light/../sensor",
        "light..living_room",
        "Light.Living_Room",
    ],
)
async def test_get_state_invalid_entity_id(entity_id: str) -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_GET_STATE_NAME, {"entity_id": entity_id})

    assert_failure_envelope(result, "invalid_arguments")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    (
        (HA_LIST_ENTITIES_NAME, {"domain": 42}),
        (HA_GET_STATE_NAME, {"entity_id": 42}),
        (HA_LIST_SERVICES_NAME, {"domain": []}),
        (
            HA_CALL_SERVICE_NAME,
            {"domain": "light", "service": "turn_on", "data": []},
        ),
    ),
)
async def test_handlers_reject_unknown_or_wrong_typed_arguments(
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, tool_name, arguments)

    assert_failure_envelope(result, "invalid_arguments")


@pytest.mark.asyncio
async def test_list_entities_handler_rejects_unknown_arguments_after_open_schema() -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {"unknown": True})

    assert_failure_envelope(result, "validation_error")


@pytest.mark.asyncio
async def test_get_state_handler_rejects_unknown_arguments_after_open_schema() -> None:
    tools = _tools_with_token()

    result = await _dispatch(
        tools,
        HA_GET_STATE_NAME,
        {"entity_id": "light.living_room", "unknown": True},
    )

    assert_failure_envelope(result, "validation_error")


@pytest.mark.asyncio
async def test_list_services_handler_rejects_unknown_arguments_after_open_schema() -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_LIST_SERVICES_NAME, {"unknown": True})

    assert_failure_envelope(result, "validation_error")


@pytest.mark.asyncio
async def test_call_service_handler_rejects_unknown_arguments_after_open_schema() -> None:
    tools = _tools_with_token()

    result = await _dispatch(
        tools,
        HA_CALL_SERVICE_NAME,
        {"domain": "light", "service": "turn_on", "unknown": True},
    )

    assert_failure_envelope(result, "validation_error")


@respx.mock
@pytest.mark.asyncio
async def test_get_state_not_found() -> None:
    respx.get(f"{_HASS_URL}/api/states/light.missing").mock(
        return_value=httpx.Response(404, json={"message": "Entity not found"})
    )
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_GET_STATE_NAME, {"entity_id": "light.missing"})

    assert_failure_envelope(result, "home_assistant_error")
