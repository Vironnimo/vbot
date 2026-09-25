"""Homeassistant: entity listing and state reads through production dispatch."""

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
    model_text,
)
from tests.resources.extensions.homeassistant_helpers import (
    _clean_extension_modules as _clean_extension_modules,
)

_STATES: list[dict[str, Any]] = [
    {
        "entity_id": "light.living_room",
        "state": "on",
        "attributes": {"friendly_name": "Living Room Spot"},
    },
    {
        "entity_id": "sensor.temperature",
        "state": "22.5",
        "attributes": {"friendly_name": "Hall Temperature", "unit_of_measurement": "°C"},
    },
    {
        "entity_id": "light.kitchen",
        "state": "off",
        "attributes": {"friendly_name": "Kitchen Light"},
    },
    {
        "entity_id": "climate.upstairs",
        "state": "heat",
        "attributes": {"friendly_name": "Thermostat", "area": "Upstairs"},
    },
]


def _mock_states(states: list[dict[str, Any]] | None = None) -> respx.Route:
    return respx.get(f"{_HASS_URL}/api/states").mock(
        return_value=httpx.Response(200, json=_STATES if states is None else states)
    )


@respx.mock
@pytest.mark.asyncio
async def test_list_entities_returns_one_sorted_line_per_entity() -> None:
    route = _mock_states()
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    assert route.called is True
    data = assert_success_envelope(result)
    assert data["count"] == 4
    assert data["content"].splitlines() == [
        "climate.upstairs: heat (Thermostat)",
        "light.kitchen: off (Kitchen Light)",
        "light.living_room: on (Living Room Spot)",
        "sensor.temperature: 22.5 °C (Hall Temperature)",
    ]
    assert model_text(result).startswith("count: 4\n\nclimate.upstairs: heat")


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"domain": "light"}, ["light.kitchen", "light.living_room"]),
        ({"domain": " LIGHT "}, ["light.kitchen", "light.living_room"]),
        # An empty area is a placeholder, not a filter.
        ({"domain": "light", "area": ""}, ["light.kitchen", "light.living_room"]),
        ({"area": "living room"}, ["light.living_room"]),
        ({"area": "Living_Room"}, ["light.living_room"]),
        ({"area": "kitchen"}, ["light.kitchen"]),
        ({"area": "upstairs"}, ["climate.upstairs"]),
        ({"room": "Kitchen"}, ["light.kitchen"]),
        ({"domain": "sensor", "area": "hall"}, ["sensor.temperature"]),
    ],
)
async def test_list_entities_filters_by_domain_and_area_text(
    arguments: dict[str, Any], expected: list[str]
) -> None:
    _mock_states()
    tools = _tools_with_token()

    data = assert_success_envelope(await _dispatch(tools, HA_LIST_ENTITIES_NAME, arguments))

    assert [line.split(":")[0] for line in data["content"].splitlines()] == expected
    assert data["count"] == len(expected)


@respx.mock
@pytest.mark.asyncio
async def test_long_unfiltered_list_returns_counts_per_domain_and_a_page_per_domain() -> None:
    many = [
        {"entity_id": f"{domain}.item_{index:03d}", "state": "on", "attributes": {}}
        for domain in ("light", "sensor")
        for index in range(60)
    ]
    _mock_states(many)
    tools = _tools_with_token()

    overview = assert_success_envelope(await _dispatch(tools, HA_LIST_ENTITIES_NAME, {}))
    assert overview["count"] == 120
    assert overview["content"] == "light: 60\nsensor: 60"
    assert '{"domain":"light"}' in overview["note"] and '{"area":"kitchen"}' in overview["note"]

    _mock_states(many + [{"entity_id": f"light.extra_{i:02d}", "state": "off"} for i in range(50)])
    page = assert_success_envelope(
        await _dispatch(tools, HA_LIST_ENTITIES_NAME, {"domain": "light"})
    )
    assert page["count"] == 110 and len(page["content"].splitlines()) == 100
    assert page["note"] == (
        "Showing the first 100 of 110 entities. Narrow with area text, such as "
        '{"domain":"light","area":"kitchen"}.'
    )


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        ({"domain": "lights"}, 'The closest is light: {"domain":"light"}.'),
        ({"area": "kitchn"}, 'Similar name words: kitchen; try {"area":"kitchen"}.'),
        ({"domain": "light", "area": "garage"}, 'List the domain with {"domain":"light"}.'),
    ],
)
async def test_no_match_notes_name_the_next_call(arguments: dict[str, Any], fragment: str) -> None:
    _mock_states()
    tools = _tools_with_token()

    data = assert_success_envelope(await _dispatch(tools, HA_LIST_ENTITIES_NAME, arguments))

    assert data["count"] == 0 and "content" not in data
    assert fragment in data["note"]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        (
            {"domain": "Light.Kitchen"},
            'Read one entity with ha_get_state {"entity_id":"light.kitchen"}',
        ),
        ({"domain": "light/../sensor"}, "is not a Home Assistant domain name"),
        ({"domain": 42}, "is not a Home Assistant domain name"),
        ({"area": "kitchen", "room": "hall"}, "Conflicting values for area"),
    ],
)
async def test_list_entities_refuses_unclear_filters_before_any_request(
    arguments: dict[str, Any], fragment: str
) -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, arguments)

    error = assert_failure_envelope(result, "invalid_arguments")
    assert fragment in error["message"]
    assert not respx.calls


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

    error = assert_failure_envelope(result, "home_assistant_error")
    assert error["message"] == (
        "Home Assistant is busy or unavailable (HTTP 500: internal error) after 3 attempts. "
        "Try again later."
    )
    assert any(
        record.levelno == logging.WARNING and record.name == f"vbot.extensions.{_EXTENSION_NAME}"
        for record in caplog.records
    )


# ha_get_state
@respx.mock
@pytest.mark.asyncio
async def test_get_state_success_drops_a_repeated_timestamp() -> None:
    route = respx.get(f"{_HASS_URL}/api/states/light.living_room").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "entity_id": "light.living_room",
                    "state": "on",
                    "attributes": {"brightness": 255},
                    "last_changed": "2025-01-01T00:00:00+00:00",
                    "last_updated": "2025-01-01T00:00:00+00:00",
                    "context": {"id": "fixture"},
                },
            ),
            httpx.Response(
                200,
                json={
                    "entity_id": "light.living_room",
                    "state": "on",
                    "attributes": {"brightness": 128},
                    "last_changed": "2025-01-01T00:00:00+00:00",
                    "last_updated": "2025-01-01T12:00:00+00:00",
                },
            ),
        ]
    )
    tools = _tools_with_token()

    first = assert_success_envelope(
        await _dispatch(tools, HA_GET_STATE_NAME, {"entity_id": "light.living_room"})
    )
    second = assert_success_envelope(
        await _dispatch(tools, HA_GET_STATE_NAME, {"entityId": " Light.Living Room "})
    )

    assert route.call_count == 2
    assert first == {
        "entity_id": "light.living_room",
        "state": "on",
        "attributes": {"brightness": 255},
        "last_changed": "2025-01-01T00:00:00+00:00",
    }
    assert second["last_updated"] == "2025-01-01T12:00:00+00:00"


@pytest.mark.asyncio
async def test_get_state_missing_entity_id() -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_GET_STATE_NAME, {})

    assert_failure_envelope(result, "invalid_arguments")


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("entity_id", ["", "light/../sensor", "light/kitchen"])
async def test_get_state_refuses_unsafe_ids_without_any_request(entity_id: str) -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_GET_STATE_NAME, {"entity_id": entity_id})

    assert_failure_envelope(result, "invalid_arguments")
    assert not respx.calls


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_id", "fragment"),
    [
        (
            "Kitchen Light",
            "Home Assistant has light.kitchen (Kitchen Light); if you mean it, call "
            'ha_get_state {"entity_id":"light.kitchen"}.',
        ),
        ("light.", "Find the id with ha_list_entities"),
        ("invalid", "Find the id with ha_list_entities"),
        ("light..living_room", "light.living_room (Living Room Spot)"),
    ],
)
async def test_get_state_names_candidates_for_a_name_or_malformed_id(
    entity_id: str, fragment: str
) -> None:
    states = _mock_states()
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_GET_STATE_NAME, {"entity_id": entity_id})

    error = assert_failure_envelope(result, "invalid_arguments")
    assert error["message"].startswith("ha_get_state was not run: ")
    assert fragment in error["message"]
    # Only the candidate lookup ran; no state read used a guessed id.
    assert [call.request.url.path for call in respx.calls] == ["/api/states"]
    assert states.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_get_state_reads_one_entity_per_call() -> None:
    tools = _tools_with_token()

    result = await _dispatch(
        tools, HA_GET_STATE_NAME, {"entity_id": ["light.kitchen", "light.living_room"]}
    )

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "Call it once for each of light.kitchen, light.living_room" in error["message"]
    assert not respx.calls
    single = respx.get(f"{_HASS_URL}/api/states/light.kitchen").mock(
        return_value=httpx.Response(200, json={"entity_id": "light.kitchen", "state": "off"})
    )
    assert (await _dispatch(tools, HA_GET_STATE_NAME, {"entity_id": ["light.kitchen"]}))["ok"]
    assert single.called


@respx.mock
@pytest.mark.asyncio
async def test_get_state_not_found_names_close_entities() -> None:
    respx.get(f"{_HASS_URL}/api/states/light.kitchn").mock(
        return_value=httpx.Response(404, json={"message": "Entity not found."})
    )
    _mock_states()
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_GET_STATE_NAME, {"entity_id": "light.kitchn"})

    error = assert_failure_envelope(result, "entity_not_found")
    assert error["message"] == (
        "Home Assistant has no entity light.kitchn. Home Assistant has light.kitchen (Kitchen "
        'Light); if you mean it, call ha_get_state {"entity_id":"light.kitchen"}.'
    )
    assert error["retryable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    (
        (HA_LIST_SERVICES_NAME, {"domain": []}),
        (HA_GET_STATE_NAME, {"entity_id": {"id": "light.kitchen"}}),
        (
            HA_CALL_SERVICE_NAME,
            {"domain": "light", "service": "turn_on", "data": []},
        ),
    ),
)
async def test_wrong_typed_arguments_fail_at_dispatch(
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, tool_name, arguments)

    assert_failure_envelope(result, "invalid_arguments")


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    (
        (HA_LIST_ENTITIES_NAME, {"unknown": True}),
        (HA_GET_STATE_NAME, {"entity_id": "light.living_room", "unknown": True}),
        (HA_LIST_SERVICES_NAME, {"unknown": True}),
        (HA_CALL_SERVICE_NAME, {"domain": "light", "service": "turn_on", "unknown": True}),
    ),
)
async def test_unknown_arguments_fail_at_dispatch_before_any_request(
    tool_name: str, arguments: dict[str, Any]
) -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, tool_name, arguments)

    error = assert_failure_envelope(result, "invalid_arguments")
    assert '"unknown" is not a parameter' in error["message"]
    assert not respx.calls
