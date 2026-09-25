"""Homeassistant: service listing and service calls through production dispatch."""

from __future__ import annotations

from typing import Any

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
    model_text,
    request_body,
)
from tests.resources.extensions.homeassistant_helpers import (
    _clean_extension_modules as _clean_extension_modules,
)

_SERVICES: list[dict[str, Any]] = [
    {
        "domain": "light",
        "services": {
            "turn_on": {
                "name": "Turn on",
                "description": "Turns on one or more lights. Adjusts their properties as well.",
                "fields": {
                    "brightness_pct": {
                        "description": "Brightness in percent.",
                        "selector": {"number": {"min": 0, "max": 100, "unit_of_measurement": "%"}},
                    },
                    "rgb_color": {"example": "[255, 100, 100]", "selector": {"color_rgb": {}}},
                    "advanced_fields": {
                        "collapsed": True,
                        "fields": {
                            "flash": {
                                "selector": {"select": {"options": ["long", "short"]}},
                            },
                        },
                    },
                },
                "target": {"entity": [{"domain": ["light"]}]},
            },
            "turn_off": {"description": "Turns off one or more lights.", "fields": {}},
        },
    },
    {
        "domain": "climate",
        "services": {
            "set_temperature": {
                "description": "Sets the target temperature.",
                "fields": {
                    "temperature": {
                        "required": True,
                        "selector": {"number": {"min": 7, "max": 35}},
                    },
                    "hvac_mode": {
                        "selector": {
                            "select": {
                                "options": [
                                    {"label": "Off", "value": "off"},
                                    {"label": "Heat", "value": "heat"},
                                ]
                            }
                        }
                    },
                },
            },
        },
    },
    {
        "domain": "weather",
        "services": {
            "get_forecasts": {
                "description": "Gets weather forecasts.",
                "fields": {
                    "type": {"required": True, "selector": {"select": {"options": ["daily"]}}}
                },
                "response": {"optional": False},
            },
        },
    },
    {"domain": "shell_command", "services": {"run": {"description": "", "fields": {}}}},
]

_KITCHEN = {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen"}}


def _mock_services() -> respx.Route:
    return respx.get(f"{_HASS_URL}/api/services").mock(
        return_value=httpx.Response(200, json=_SERVICES)
    )


# ha_list_services
@respx.mock
@pytest.mark.asyncio
async def test_list_services_without_domain_names_services_per_domain() -> None:
    _mock_services()
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_LIST_SERVICES_NAME, {})

    data = assert_success_envelope(result)
    assert data["count"] == 4
    assert data["content"].splitlines() == [
        "climate: set_temperature",
        "light: turn_off, turn_on",
        "shell_command: blocked in vBot",
        "weather: get_forecasts",
    ]


@respx.mock
@pytest.mark.asyncio
async def test_list_services_with_domain_shows_compact_fields() -> None:
    _mock_services()
    tools = _tools_with_token()

    light = assert_success_envelope(
        await _dispatch(tools, HA_LIST_SERVICES_NAME, {"domain": " Light "})
    )
    climate = assert_success_envelope(
        await _dispatch(tools, HA_LIST_SERVICES_NAME, {"domain": "climate"})
    )
    weather = assert_success_envelope(
        await _dispatch(tools, HA_LIST_SERVICES_NAME, {"domain": "weather"})
    )

    assert light["count"] == 2
    assert light["content"].splitlines() == [
        "turn_off: Turns off one or more lights.",
        "turn_on: Turns on one or more lights. Fields: brightness_pct (0-100 %), "
        "rgb_color (e.g. [255, 100, 100]), flash (long|short)",
    ]
    assert climate["content"] == (
        "set_temperature: Sets the target temperature. Fields: temperature* (7-35), "
        "hvac_mode (off|heat)"
    )
    assert weather["content"].endswith("Fields: type* (daily) Returns data.")


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [{"domain": "light", "service": "turn_on"}, {"domain": "light.turn_on"}],
)
async def test_list_services_shows_one_service_with_field_descriptions(
    arguments: dict[str, Any],
) -> None:
    _mock_services()
    tools = _tools_with_token()

    data = assert_success_envelope(await _dispatch(tools, HA_LIST_SERVICES_NAME, arguments))

    assert data["content"].splitlines() == [
        "light.turn_on: Turns on one or more lights. Adjusts their properties as well.",
        "- brightness_pct (0-100 %): Brightness in percent.",
        "- rgb_color (e.g. [255, 100, 100])",
        "- flash (long|short)",
    ]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"domain": "lights"},
            "Home Assistant has no domain lights. Close domains: light; list one with "
            'ha_list_services {"domain":"light"}.',
        ),
        (
            {"domain": "light", "service": "turn_onn"},
            "Home Assistant has no service light.turn_onn. Services of light: turn_off, turn_on. "
            'If you mean turn_on, call ha_list_services {"domain":"light","service":"turn_on"}.',
        ),
    ],
)
async def test_list_services_unknown_names_list_close_ones(
    arguments: dict[str, Any], message: str
) -> None:
    _mock_services()
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_LIST_SERVICES_NAME, arguments)

    assert assert_failure_envelope(result, "service_not_found")["message"] == message


# ha_call_service
@respx.mock
@pytest.mark.asyncio
async def test_call_service_reports_changed_states_as_lines() -> None:
    route = respx.post(f"{_HASS_URL}/api/services/light/turn_on").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"entity_id": "light.living_room", "state": "off", "attributes": {}},
                {
                    "entity_id": "light.living_room",
                    "state": "on",
                    "attributes": {"friendly_name": "Living Room", "brightness": 128},
                },
            ],
        )
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

    data = assert_success_envelope(result)
    assert request_body(route) == {"brightness": 128, "entity_id": "light.living_room"}
    assert data == {
        "service": "light.turn_on",
        "changed": 1,
        "content": "light.living_room: on (Living Room) brightness=128",
    }
    assert model_text(result) == (
        "service: light.turn_on\nchanged: 1\n\nlight.living_room: on (Living Room) brightness=128"
    )


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "path", "body"),
    [
        # Home Assistant YAML: action, target, data
        (
            {
                "action": "light.turn_on",
                "target": {"entity_id": "light.kitchen", "area_id": "kitchen"},
                "data": {"brightness_pct": 50},
            },
            "/api/services/light/turn_on",
            {"brightness_pct": 50, "area_id": "kitchen", "entity_id": "light.kitchen"},
        ),
        (
            {"domain": "light", "service": "turn_off", "service_data": {"transition": 2}},
            "/api/services/light/turn_off",
            {"transition": 2},
        ),
        # One qualified name in either field
        ({"service": "Light.Turn_On"}, "/api/services/light/turn_on", {}),
        ({"domain": "light.turn_on"}, "/api/services/light/turn_on", {}),
        ({"domain": "light", "service": "light.turn_on"}, "/api/services/light/turn_on", {}),
        # Identifier spelling, JSON-string data, and a service without its domain
        (
            {"domain": " LIGHT ", "service": "Turn On", "entity_id": "Light.Kitchen"},
            "/api/services/light/turn_on",
            {"entity_id": "light.kitchen"},
        ),
        (
            {"domain": "light", "service": "turn_on", "data": '{"brightness_pct": 20}'},
            "/api/services/light/turn_on",
            {"brightness_pct": 20},
        ),
        (
            {"service": "toggle", "entity_id": "light.kitchen"},
            "/api/services/light/toggle",
            {"entity_id": "light.kitchen"},
        ),
        # Several entities as a list, a comma-separated string, or nested data
        (
            {"domain": "light", "service": "turn_off", "entity_id": ["light.a", "light.b"]},
            "/api/services/light/turn_off",
            {"entity_id": ["light.a", "light.b"]},
        ),
        (
            {"domain": "light", "service": "turn_off", "entity_id": "light.a, light.b"},
            "/api/services/light/turn_off",
            {"entity_id": ["light.a", "light.b"]},
        ),
        (
            {
                "domain": "light",
                "service": "turn_on",
                "entity_id": "light.a",
                "data": {"entity_id": "LIGHT.A", "brightness": 1},
            },
            "/api/services/light/turn_on",
            {"brightness": 1, "entity_id": "light.a"},
        ),
    ],
)
async def test_call_service_reads_other_call_shapes_exactly(
    arguments: dict[str, Any], path: str, body: dict[str, Any]
) -> None:
    route = respx.post(url__startswith=f"{_HASS_URL}/api/services/").mock(
        return_value=httpx.Response(200, json=[_KITCHEN])
    )
    respx.get(url__startswith=f"{_HASS_URL}/api/states/").mock(
        return_value=httpx.Response(200, json=_KITCHEN)
    )
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_CALL_SERVICE_NAME, arguments)

    assert result["ok"], result["error"]
    assert route.call_count == 1
    assert route.calls[0].request.url.path == path
    assert request_body(route) == body


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        (
            {"domain": "switch", "service": "light.turn_on"},
            'Send one of {"domain":"light","service":"turn_on"} or '
            '{"domain":"switch","service":"turn_on"}.',
        ),
        (
            {"domain": "light.turn_on", "service": "turn_off"},
            '{"domain":"light","service":"turn_off"} or {"domain":"light","service":"turn_on"}',
        ),
        (
            {
                "domain": "light",
                "service": "turn_on",
                "entity_id": "light.living_room",
                "data": {"entity_id": "light.kitchen"},
            },
            '"entity_id":"light.living_room" or "entity_id":"light.living_room,light.kitchen"',
        ),
        (
            {
                "action": "light.turn_on",
                "entity_id": "light.a",
                "target": {"entity_id": "light.b"},
            },
            'entity_id "light.a" and target.entity_id "light.b" name different entities',
        ),
        (
            {
                "action": "light.turn_on",
                "target": {"area_id": "kitchen"},
                "data": {"area_id": "hall"},
            },
            "target.area_id and data.area_id differ",
        ),
        (
            {"service": "turn_off", "entity_id": "light.a,switch.b"},
            'use "domain":"homeassistant" with service "turn_off"',
        ),
        ({"service": "turn_on"}, "domain is missing"),
        ({"domain": "light"}, "service is missing"),
        ({"domain": "light", "service": "turn_on", "entity_id": ""}, '"entity_id" is empty'),
        ({"domain": "light", "service": "turn_on", "entity_id": []}, '"entity_id" is empty'),
        ({"domain": "light/../x", "service": "turn_on"}, "is not a Home Assistant domain name"),
        ({"domain": "42", "service": "turn_on"}, "is not a Home Assistant domain name"),
        ({"domain": "light", "service": "turn/on"}, "is not a Home Assistant service name"),
        ({"action": "light.turn_on", "target": {"floor": "1"}}, "target cannot hold floor"),
    ],
)
async def test_call_service_refuses_unclear_calls_before_any_request(
    arguments: dict[str, Any], fragment: str
) -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_CALL_SERVICE_NAME, arguments)

    error = assert_failure_envelope(result, "invalid_arguments")
    assert error["message"].startswith("ha_call_service was not run: ")
    assert fragment in error["message"]
    assert not respx.calls


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"domain": "shell_command", "service": "run"},
        {"service": "SHELL_COMMAND.RUN"},
        {"domain": "command_line", "service": "run"},
        {"domain": "python_script", "service": "run"},
        {"domain": "pyscript", "service": "run"},
        {"domain": "hassio", "service": "run"},
        {"domain": "rest_command", "service": "run"},
    ],
)
async def test_call_service_blocked_domain(arguments: dict[str, Any]) -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_CALL_SERVICE_NAME, arguments)

    error = assert_failure_envelope(result, "blocked_domain")
    assert "was not called. Tell the user if they need it." in error["message"]
    assert not respx.calls


@respx.mock
@pytest.mark.asyncio
async def test_call_service_refuses_unsafe_entity_ids_without_any_request() -> None:
    tools = _tools_with_token()

    result = await _dispatch(
        tools,
        HA_CALL_SERVICE_NAME,
        {"domain": "light", "service": "turn_on", "data": {"entity_id": "light/../sensor"}},
    )

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "Find ids with ha_list_entities {}." in error["message"]
    assert not respx.calls


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_id", "fragment"),
    [
        (
            "Kitchen",
            'if you mean it, call ha_call_service {"domain":"light","service":"turn_on",'
            '"entity_id":"light.kitchen"}.',
        ),
        (
            "light.a,Kitchen",
            '{"domain":"light","service":"turn_on","entity_id":"light.a,light.kitchen"}',
        ),
        ("light.", 'Find the id with ha_list_entities {"domain":"light"}.'),
        ("garage door", 'Find the id with ha_list_entities {"area":"garage door"}.'),
    ],
)
async def test_call_service_names_candidates_instead_of_acting_on_a_name(
    entity_id: str, fragment: str
) -> None:
    post = respx.post(url__startswith=f"{_HASS_URL}/api/services/")
    respx.get(f"{_HASS_URL}/api/states").mock(return_value=httpx.Response(200, json=[_KITCHEN]))
    tools = _tools_with_token()

    result = await _dispatch(
        tools,
        HA_CALL_SERVICE_NAME,
        {"domain": "light", "service": "turn_on", "entity_id": entity_id},
    )

    error = assert_failure_envelope(result, "invalid_arguments")
    assert fragment in error["message"]
    assert post.called is False


@respx.mock
@pytest.mark.asyncio
async def test_call_service_without_change_reports_the_current_state() -> None:
    respx.post(f"{_HASS_URL}/api/services/light/turn_on").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(f"{_HASS_URL}/api/states/light.kitchen").mock(
        return_value=httpx.Response(200, json=_KITCHEN)
    )
    tools = _tools_with_token()

    result = await _dispatch(
        tools,
        HA_CALL_SERVICE_NAME,
        {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"},
    )

    assert assert_success_envelope(result) == {
        "service": "light.turn_on",
        "changed": 0,
        "note": "No entity state changed.",
        "content": "light.kitchen: on (Kitchen) [unchanged]",
    }


@respx.mock
@pytest.mark.asyncio
async def test_call_service_on_a_missing_entity_names_close_entities() -> None:
    respx.post(f"{_HASS_URL}/api/services/light/turn_on").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(f"{_HASS_URL}/api/states/light.kitchn").mock(
        return_value=httpx.Response(404, json={"message": "Entity not found."})
    )
    respx.get(f"{_HASS_URL}/api/states").mock(return_value=httpx.Response(200, json=[_KITCHEN]))
    tools = _tools_with_token()

    result = await _dispatch(
        tools,
        HA_CALL_SERVICE_NAME,
        {"domain": "light", "service": "turn_on", "entity_id": "light.kitchn"},
    )

    error = assert_failure_envelope(result, "entity_not_found")
    assert error["message"] == (
        "Home Assistant has no entity light.kitchn, so light.turn_on changed nothing. Home "
        "Assistant has light.kitchen (Kitchen); if you mean it, call ha_call_service "
        '{"domain":"light","service":"turn_on","entity_id":"light.kitchen"}.'
    )


@respx.mock
@pytest.mark.asyncio
async def test_call_service_with_one_missing_entity_of_several_reports_both() -> None:
    respx.post(f"{_HASS_URL}/api/services/light/turn_off").mock(
        return_value=httpx.Response(200, json=[{**_KITCHEN, "state": "off"}])
    )
    respx.get(f"{_HASS_URL}/api/states/light.hall").mock(
        return_value=httpx.Response(404, json={"message": "Entity not found."})
    )
    tools = _tools_with_token()

    result = await _dispatch(
        tools,
        HA_CALL_SERVICE_NAME,
        {"domain": "light", "service": "turn_off", "entity_id": "light.kitchen,light.hall"},
    )

    data = assert_success_envelope(result)
    assert data["changed"] == 1 and data["content"] == "light.kitchen: off (Kitchen)"
    assert data["note"] == (
        "Home Assistant has no entity light.hall, so light.turn_off did nothing for it. Find the "
        'id with ha_list_entities {"domain":"light"}.'
    )


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "code", "message"),
    [
        (
            {"domain": "light", "service": "turn_onn", "entity_id": "light.kitchen"},
            "service_not_found",
            "Home Assistant has no service light.turn_onn, so light.turn_onn was not called. "
            "Services of light: turn_off, turn_on. If you mean turn_on, call ha_call_service "
            '{"domain":"light","service":"turn_on","entity_id":"light.kitchen"}.',
        ),
        (
            {"domain": "lights", "service": "turn_on"},
            "service_not_found",
            "Home Assistant has no domain lights, so lights.turn_on was not called. Close "
            'domains: light; list one with ha_list_services {"domain":"light"}.',
        ),
        (
            {"domain": "light", "service": "turn_on", "data": {"brightness": "max"}},
            "home_assistant_error",
            "Home Assistant rejected light.turn_on (HTTP 400: expected int). Check its fields "
            'with ha_list_services {"domain":"light","service":"turn_on"}.',
        ),
    ],
)
async def test_rejected_service_calls_name_the_next_call(
    arguments: dict[str, Any], code: str, message: str
) -> None:
    respx.post(url__startswith=f"{_HASS_URL}/api/services/").mock(
        return_value=httpx.Response(400, json={"message": "expected int"})
    )
    _mock_services()
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_CALL_SERVICE_NAME, arguments)

    error = assert_failure_envelope(result, code)
    assert error["message"] == message
    assert error["retryable"] is False


@respx.mock
@pytest.mark.asyncio
async def test_bare_bad_request_names_the_service_fields() -> None:
    respx.post(f"{_HASS_URL}/api/services/light/turn_on").mock(
        return_value=httpx.Response(400, text="400: Bad Request")
    )
    _mock_services()
    tools = _tools_with_token()

    result = await _dispatch(
        tools, HA_CALL_SERVICE_NAME, {"domain": "light", "service": "turn_on", "data": {"x": 1}}
    )

    assert assert_failure_envelope(result, "home_assistant_error")["message"] == (
        "Home Assistant rejected light.turn_on (HTTP 400: Bad Request). Check its fields with "
        'ha_list_services {"domain":"light","service":"turn_on"}.'
    )


@respx.mock
@pytest.mark.asyncio
async def test_data_only_service_is_asked_for_its_response_once() -> None:
    refusal = {
        "message": "Service call requires responses but caller did not ask for responses. "
        "Add ?return_response to query parameters."
    }
    forecast = {"weather.home": {"forecast": [{"condition": "sunny"}]}}
    route = respx.post(url__startswith=f"{_HASS_URL}/api/services/weather/get_forecasts").mock(
        side_effect=[
            httpx.Response(400, json=refusal),
            httpx.Response(200, json={"changed_states": [], "service_response": forecast}),
        ]
    )
    tools = _tools_with_token()

    result = await _dispatch(
        tools,
        HA_CALL_SERVICE_NAME,
        {"action": "weather.get_forecasts", "data": {"type": "daily"}},
    )

    data = assert_success_envelope(result)
    assert [call.request.url.query for call in route.calls] == [b"", b"return_response"]
    assert data == {
        "service": "weather.get_forecasts",
        "changed": 0,
        "note": "No entity state changed.",
        "response": forecast,
    }


@respx.mock
@pytest.mark.asyncio
async def test_return_response_on_a_service_without_data_says_to_drop_it() -> None:
    route = respx.post(url__startswith=f"{_HASS_URL}/api/services/light/turn_on").mock(
        return_value=httpx.Response(
            400,
            json={
                "message": "Service does not support responses. "
                "Remove return_response from request."
            },
        )
    )
    tools = _tools_with_token()

    result = await _dispatch(
        tools,
        HA_CALL_SERVICE_NAME,
        {"domain": "light", "service": "turn_on", "return_response": "true"},
    )

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "Call it again without return_response." in error["message"]
    assert route.call_count == 1 and route.calls[0].request.url.query == b"return_response"
