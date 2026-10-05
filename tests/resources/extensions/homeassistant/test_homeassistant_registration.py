"""Home Assistant: registration, readiness, Tool input contracts and live configuration."""

from __future__ import annotations

import json
from collections.abc import Awaitable
from typing import Any, cast

import httpx
import pytest
import respx

from tests.resources.extensions.homeassistant.homeassistant_test_support import (
    EXTENSION_NAME,
    HA_CALL_SERVICE_NAME,
    HA_GET_STATE_NAME,
    HA_LIST_ENTITIES_NAME,
    HA_LIST_SERVICES_NAME,
    HA_TOOL_NAMES,
    HASS_URL,
    TOKEN,
    LiveSettings,
    assert_failure_envelope,
    assert_success_envelope,
    dispatch,
    load,
    make_context,
    tools_with_token,
)

_FAMILY_ID = "extension:homeassistant:home_assistant"


def test_extension_loads_from_the_bundled_root_with_settings_and_one_tool_family() -> None:
    extensions, tools = load(LiveSettings())

    record = next(r for r in extensions.records() if r.name == EXTENSION_NAME)
    assert record.status == "loaded"
    assert record.capability_errors == []
    assert record.manifest is not None
    assert record.manifest.display_name == "Home Assistant"
    schema = record.declarations.settings_schema
    assert schema is not None
    fields = {field.key: field for field in schema}
    assert (fields["url"].type, fields["url"].default) == ("text", HASS_URL)
    assert (fields["token"].type, fields["token"].env_key) == ("secret", "HASS_TOKEN")
    assert fields["token"].default is None

    family = tools.get_family(_FAMILY_ID)
    assert (family.label, family.extension) == ("Home Assistant", EXTENSION_NAME)
    for name in HA_TOOL_NAMES:
        tool = tools.get(name)
        assert (tool.family, tool.family_label) == (_FAMILY_ID, "Home Assistant")
        assert tool.extension == EXTENSION_NAME
        assert tool.readiness_hint == (
            "Requires a Home Assistant connection - set the server URL and token "
            "in Settings -> Extensions."
        )
    assert tools.get(HA_GET_STATE_NAME).display.summary_fields == ("entity_id",)
    assert tools.get(HA_CALL_SERVICE_NAME).display.summary_fields == (
        "domain",
        "service",
        "entity_id",
    )


@pytest.mark.asyncio
async def test_tools_stay_registered_but_unoffered_and_refused_until_a_token_is_set() -> None:
    settings = LiveSettings()
    _, tools = load(settings)

    assert {tools.get(name).name for name in HA_TOOL_NAMES} == set(HA_TOOL_NAMES)
    assert {d["name"] for d in tools.provider_definitions()}.isdisjoint(HA_TOOL_NAMES)
    assert_failure_envelope(await dispatch(tools, HA_LIST_ENTITIES_NAME, {}), "tool_not_ready")

    settings.credentials["HASS_TOKEN"] = TOKEN

    assert set(HA_TOOL_NAMES) <= {d["name"] for d in tools.provider_definitions()}


def test_provider_schemas_are_open_and_leave_identifier_grammar_to_the_tools() -> None:
    _, tools = load(LiveSettings())

    for name in HA_TOOL_NAMES:
        tool = tools.get(name)
        assert tool.open_input_schema is True
        assert "additionalProperties" not in tool.parameters
        # Identifier grammar is checked after spelling repair, with messages naming the
        # next call, so the definitions carry no patterns for Providers to enforce first.
        assert "pattern" not in json.dumps(tool.parameters)
    assert tools.get(HA_GET_STATE_NAME).parameters["properties"]["entity_id"]["minLength"] == 1
    call_parameters = tools.get(HA_CALL_SERVICE_NAME).parameters
    assert call_parameters["required"] == ["domain", "service"]
    assert call_parameters["properties"]["data"]["type"] == "object"


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (HA_LIST_SERVICES_NAME, {"domain": ["light", "switch"]}),
        (HA_GET_STATE_NAME, {"entity_id": {"id": "light.kitchen"}}),
        (
            HA_CALL_SERVICE_NAME,
            {"domain": "light", "service": "turn_on", "data": [{"brightness_pct": 50}]},
        ),
    ],
)
async def test_wrong_typed_arguments_fail_at_dispatch_before_any_request(
    tool_name: str, arguments: dict[str, Any]
) -> None:
    result = await dispatch(tools_with_token(), tool_name, arguments)

    assert_failure_envelope(result, "invalid_arguments")
    assert not respx.calls


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (HA_LIST_ENTITIES_NAME, {"unknown": True}),
        (HA_GET_STATE_NAME, {"entity_id": "light.living_room", "unknown": True}),
        (HA_LIST_SERVICES_NAME, {"unknown": True}),
        (HA_CALL_SERVICE_NAME, {"domain": "light", "service": "turn_on", "unknown": True}),
    ],
)
async def test_unknown_arguments_fail_at_dispatch_before_any_request(
    tool_name: str, arguments: dict[str, Any]
) -> None:
    result = await dispatch(tools_with_token(), tool_name, arguments)

    error = assert_failure_envelope(result, "invalid_arguments")
    assert '"unknown" is not a parameter' in error["message"]
    assert not respx.calls


@respx.mock
@pytest.mark.asyncio
async def test_handler_refuses_without_a_request_when_the_token_is_removed_mid_flight() -> None:
    # Dispatch checks readiness first; the handler repeats the check in case the token
    # is removed between that check and the call, so it is called directly here.
    route = respx.get(f"{HASS_URL}/api/states")
    settings = LiveSettings(token=TOKEN)
    _, tools = load(settings)
    tool = tools.get(HA_LIST_ENTITIES_NAME)
    settings.credentials.pop("HASS_TOKEN")

    result = await cast(
        Awaitable[dict[str, Any]], tool.handler(make_context(HA_LIST_ENTITIES_NAME), {})
    )

    error = assert_failure_envelope(result, "home_assistant_error")
    assert error["message"] == (
        "Home Assistant is not connected: no access token is set. Tell the user to set it in "
        "Settings -> Extensions."
    )
    assert route.called is False


@respx.mock
@pytest.mark.asyncio
async def test_server_url_and_token_are_read_live_on_every_call() -> None:
    routes = [
        respx.get(f"{url}/api/states").mock(return_value=httpx.Response(200, json=[]))
        for url in (HASS_URL, "http://ha-one:8123", "http://ha-two:8123")
    ]
    settings = LiveSettings(token=TOKEN)
    _, tools = load(settings)

    # No configured URL: the default server.
    assert_success_envelope(await dispatch(tools, HA_LIST_ENTITIES_NAME, {}))
    # A configured URL loses its trailing slash.
    settings.config["url"] = "http://ha-one:8123/"
    assert_success_envelope(await dispatch(tools, HA_LIST_ENTITIES_NAME, {}))
    # URL and token changes both apply to the next call.
    settings.config["url"] = "http://ha-two:8123"
    settings.credentials["HASS_TOKEN"] = "token-two"
    assert_success_envelope(await dispatch(tools, HA_LIST_ENTITIES_NAME, {}))

    assert [route.call_count for route in routes] == [1, 1, 1]
    assert [route.calls[0].request.headers["Authorization"] for route in routes] == [
        f"Bearer {TOKEN}",
        f"Bearer {TOKEN}",
        "Bearer token-two",
    ]
