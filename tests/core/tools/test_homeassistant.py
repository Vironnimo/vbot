"""Tests for the Home Assistant integration tools."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.tools.homeassistant import (
    HA_CALL_SERVICE_DESCRIPTION,
    HA_CALL_SERVICE_NAME,
    HA_CALL_SERVICE_PARAMETERS,
    HA_GET_STATE_DESCRIPTION,
    HA_GET_STATE_NAME,
    HA_GET_STATE_PARAMETERS,
    HA_LIST_ENTITIES_DESCRIPTION,
    HA_LIST_ENTITIES_NAME,
    HA_LIST_ENTITIES_PARAMETERS,
    HA_LIST_SERVICES_DESCRIPTION,
    HA_LIST_SERVICES_NAME,
    HA_LIST_SERVICES_PARAMETERS,
    register_homeassistant_tools,
)
from core.tools.tools import ToolContext, ToolNotFoundError, ToolRegistry, is_tool_result_envelope

_HASS_URL = "http://homeassistant.local:8123"
_TOKEN = "test-ha-token"


def make_context(tool_name: str = HA_LIST_ENTITIES_NAME) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=Path("/tmp/workspace"),
        app_root=Path("/tmp/app"),
        data_root=Path("/tmp/data"),
    )


def _credential_resolver(key: str) -> str:
    credentials: dict[str, str] = {
        "HASS_TOKEN": _TOKEN,
        "HASS_URL": _HASS_URL,
    }
    return credentials.get(key, "")


def _empty_credential_resolver(key: str) -> str:
    del key
    return ""


def assert_success_envelope(result: dict[str, object]) -> dict[str, Any]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
    return data


def assert_failure_envelope(result: dict[str, object], code: str) -> dict[str, str]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is False
    assert result["data"] is None
    assert result["artifacts"] == []
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    return error  # type: ignore[return-value]


async def _dispatch(
    registry: ToolRegistry,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a tool call through the registry."""
    return await registry.dispatch(make_context(tool_name), arguments)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_no_token() -> None:
    registry = ToolRegistry()

    register_homeassistant_tools(registry, _empty_credential_resolver)

    with pytest.raises(ToolNotFoundError):
        registry.get("ha_list_entities")
    with pytest.raises(ToolNotFoundError):
        registry.get("ha_get_state")
    with pytest.raises(ToolNotFoundError):
        registry.get("ha_list_services")
    with pytest.raises(ToolNotFoundError):
        registry.get("ha_call_service")


def test_register_with_token_schema() -> None:
    registry = ToolRegistry()

    register_homeassistant_tools(registry, _credential_resolver)

    tool = registry.get(HA_LIST_ENTITIES_NAME)
    assert tool is not None
    assert tool.name == HA_LIST_ENTITIES_NAME
    assert tool.description == HA_LIST_ENTITIES_DESCRIPTION
    assert tool.parameters == HA_LIST_ENTITIES_PARAMETERS

    tool = registry.get(HA_GET_STATE_NAME)
    assert tool is not None
    assert tool.name == HA_GET_STATE_NAME
    assert tool.description == HA_GET_STATE_DESCRIPTION
    assert tool.parameters == HA_GET_STATE_PARAMETERS

    tool = registry.get(HA_LIST_SERVICES_NAME)
    assert tool is not None
    assert tool.name == HA_LIST_SERVICES_NAME
    assert tool.description == HA_LIST_SERVICES_DESCRIPTION
    assert tool.parameters == HA_LIST_SERVICES_PARAMETERS

    tool = registry.get(HA_CALL_SERVICE_NAME)
    assert tool is not None
    assert tool.name == HA_CALL_SERVICE_NAME
    assert tool.description == HA_CALL_SERVICE_DESCRIPTION
    assert tool.parameters == HA_CALL_SERVICE_PARAMETERS


# ---------------------------------------------------------------------------
# ha_list_entities
# ---------------------------------------------------------------------------


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

    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(registry, HA_LIST_ENTITIES_NAME, {})

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

    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(registry, HA_LIST_ENTITIES_NAME, {"domain": "light"})

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

    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(registry, HA_LIST_ENTITIES_NAME, {"area": "kitchen"})

    assert route.called is True
    data = assert_success_envelope(result)
    assert data["count"] == 1
    assert data["entities"][0]["entity_id"] == "light.kitchen"


@respx.mock
@pytest.mark.asyncio
async def test_list_entities_http_error(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 500 on an idempotent GET is now retryable; stub the backoff sleep so the
    # exhausted-retries path still fails fast.
    async def _fake_sleep(attempt: int) -> None:
        del attempt

    monkeypatch.setattr("core.tools.homeassistant._sleep_for_retry", _fake_sleep)
    respx.get(f"{_HASS_URL}/api/states").mock(
        return_value=httpx.Response(500, json={"message": "internal error"})
    )

    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    with caplog.at_level(logging.WARNING, logger="vbot.tools.homeassistant"):
        result = await _dispatch(registry, HA_LIST_ENTITIES_NAME, {})

    assert_failure_envelope(result, "home_assistant_error")
    assert any(
        record.levelno == logging.WARNING
        and "Home Assistant request failed" in record.getMessage()
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# ha_get_state
# ---------------------------------------------------------------------------


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

    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(
        registry,
        HA_GET_STATE_NAME,
        {"entity_id": "light.living_room"},
    )

    assert route.called is True
    data = assert_success_envelope(result)
    assert data["entity_id"] == "light.living_room"
    assert data["state"] == "on"
    assert data["attributes"] == {"brightness": 255}


@respx.mock
@pytest.mark.asyncio
async def test_get_state_missing_entity_id() -> None:
    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(registry, HA_GET_STATE_NAME, {})

    assert_failure_envelope(result, "validation_error")


@respx.mock
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
    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(
        registry,
        HA_GET_STATE_NAME,
        {"entity_id": entity_id},
    )

    assert_failure_envelope(result, "validation_error")


@respx.mock
@pytest.mark.asyncio
async def test_get_state_not_found() -> None:
    respx.get(f"{_HASS_URL}/api/states/light.missing").mock(
        return_value=httpx.Response(404, json={"message": "Entity not found"})
    )

    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(
        registry,
        HA_GET_STATE_NAME,
        {"entity_id": "light.missing"},
    )

    assert_failure_envelope(result, "home_assistant_error")


# ---------------------------------------------------------------------------
# ha_list_services
# ---------------------------------------------------------------------------


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

    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(registry, HA_LIST_SERVICES_NAME, {})

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

    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(registry, HA_LIST_SERVICES_NAME, {"domain": "climate"})

    assert route.called is True
    data = assert_success_envelope(result)
    assert data["count"] == 1
    assert data["domains"][0]["domain"] == "climate"


# ---------------------------------------------------------------------------
# ha_call_service
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_call_service_success() -> None:
    route = respx.post(f"{_HASS_URL}/api/services/light/turn_on").mock(
        return_value=httpx.Response(200, json=[{"entity_id": "light.living_room", "state": "on"}])
    )

    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(
        registry,
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


@respx.mock
@pytest.mark.asyncio
async def test_call_service_missing_domain() -> None:
    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(
        registry,
        HA_CALL_SERVICE_NAME,
        {"service": "turn_on"},
    )

    assert_failure_envelope(result, "validation_error")


@respx.mock
@pytest.mark.asyncio
async def test_call_service_missing_service() -> None:
    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(
        registry,
        HA_CALL_SERVICE_NAME,
        {"domain": "light"},
    )

    assert_failure_envelope(result, "validation_error")


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "domain",
    ["shell_command", "command_line", "python_script", "pyscript", "hassio", "rest_command"],
)
async def test_call_service_blocked_domain(domain: str) -> None:
    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(
        registry,
        HA_CALL_SERVICE_NAME,
        {"domain": domain, "service": "run"},
    )

    assert_failure_envelope(result, "blocked_domain")


@respx.mock
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
    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(
        registry,
        HA_CALL_SERVICE_NAME,
        {"domain": domain, "service": "turn_on"},
    )

    assert_failure_envelope(result, "validation_error")


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

    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(
        registry,
        HA_CALL_SERVICE_NAME,
        {"domain": "light", "service": "turn_on", "entity_id": entity_id},
    )

    assert_failure_envelope(result, "validation_error")
    assert route.called is False


@respx.mock
@pytest.mark.asyncio
async def test_call_service_rejects_entity_id_in_data() -> None:
    route = respx.post(f"{_HASS_URL}/api/services/light/turn_on").mock(
        return_value=httpx.Response(200, json=[])
    )

    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(
        registry,
        HA_CALL_SERVICE_NAME,
        {
            "domain": "light",
            "service": "turn_on",
            "data": {"entity_id": "light/../sensor"},
        },
    )

    assert_failure_envelope(result, "validation_error")
    assert route.called is False


# ---------------------------------------------------------------------------
# Network / retry
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_network_error_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_attempts: list[int] = []

    async def _fake_sleep(attempt: int) -> None:
        sleep_attempts.append(attempt)

    monkeypatch.setattr("core.tools.homeassistant._sleep_for_retry", _fake_sleep)

    def _raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    route = respx.get(f"{_HASS_URL}/api/states").mock(side_effect=_raise_connect_error)

    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(registry, HA_LIST_ENTITIES_NAME, {})

    assert_failure_envelope(result, "home_assistant_error")
    assert len(route.calls) == 3  # 1 initial + 2 retries
    assert sleep_attempts == [0, 1]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
async def test_retry_transient_http_status(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    # ha_list_entities issues a GET (idempotent), so 500 and 504 retry too.
    sleep_attempts: list[int] = []

    async def _fake_sleep(attempt: int) -> None:
        sleep_attempts.append(attempt)

    monkeypatch.setattr("core.tools.homeassistant._sleep_for_retry", _fake_sleep)

    route = respx.get(f"{_HASS_URL}/api/states").mock(
        side_effect=[
            httpx.Response(status_code, json={"message": "temporary failure"}),
            httpx.Response(
                200,
                json=[{"entity_id": "light.kitchen", "state": "off", "attributes": {}}],
            ),
        ]
    )

    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(registry, HA_LIST_ENTITIES_NAME, {})

    data = assert_success_envelope(result)
    assert data["count"] == 1
    assert len(route.calls) == 2
    assert sleep_attempts == [0]


@respx.mock
@pytest.mark.asyncio
async def test_no_retry_on_401() -> None:
    route = respx.get(f"{_HASS_URL}/api/states").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )

    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(registry, HA_LIST_ENTITIES_NAME, {})

    assert_failure_envelope(result, "home_assistant_error")
    assert len(route.calls) == 1  # no retry on 401


@respx.mock
@pytest.mark.asyncio
async def test_no_retry_on_404() -> None:
    route = respx.get(f"{_HASS_URL}/api/states/light.missing").mock(
        return_value=httpx.Response(404, json={"message": "Not found"})
    )

    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(
        registry,
        HA_GET_STATE_NAME,
        {"entity_id": "light.missing"},
    )

    assert_failure_envelope(result, "home_assistant_error")
    assert len(route.calls) == 1  # no retry on 404


# ---------------------------------------------------------------------------
# Default Hass URL fallback
# ---------------------------------------------------------------------------


def test_uses_default_hass_url() -> None:
    def _token_only_resolver(key: str) -> str:
        if key == "HASS_TOKEN":
            return _TOKEN
        return ""

    registry = ToolRegistry()
    register_homeassistant_tools(registry, _token_only_resolver)
    tool = registry.get(HA_LIST_ENTITIES_NAME)
    assert tool is not None


@respx.mock
@pytest.mark.asyncio
async def test_call_service_with_entity_and_data() -> None:
    route = respx.post(f"{_HASS_URL}/api/services/climate/set_temperature").mock(
        return_value=httpx.Response(200, json=[])
    )

    registry = ToolRegistry()
    register_homeassistant_tools(registry, _credential_resolver)

    result = await _dispatch(
        registry,
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
