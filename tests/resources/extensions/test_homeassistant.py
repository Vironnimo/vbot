"""Homeassistant: registration behavior."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, cast

import httpx
import pytest
import respx

from tests.resources.extensions.homeassistant_helpers import (
    _EXTENSION_NAME,
    _HASS_URL,
    _TOKEN,
    HA_CALL_SERVICE_NAME,
    HA_GET_STATE_NAME,
    HA_LIST_ENTITIES_NAME,
    HA_LIST_SERVICES_NAME,
    _dispatch,
    _load_registry,
    _State,
    _tools_with_token,
    assert_failure_envelope,
    assert_success_envelope,
    make_context,
)
from tests.resources.extensions.homeassistant_helpers import (
    _clean_extension_modules as _clean_extension_modules,
)

_HA_TOOL_NAMES = (
    HA_LIST_ENTITIES_NAME,
    HA_GET_STATE_NAME,
    HA_LIST_SERVICES_NAME,
    HA_CALL_SERVICE_NAME,
)

_FAMILY_ID = "extension:homeassistant:home_assistant"


def test_homeassistant_tools_share_extension_family() -> None:
    _, tools = _load_registry(_State())

    for tool_name in _HA_TOOL_NAMES:
        tool = tools.get(tool_name)
        assert tool.family == _FAMILY_ID
        assert tool.family_label == "Home Assistant"

    family = tools.get_family(_FAMILY_ID)
    assert family.label == "Home Assistant"
    assert family.extension == _EXTENSION_NAME


# Loading, readiness, and the always-registered contract
def test_extension_loads_from_bundled_root() -> None:
    state = _State()
    extensions, _ = _load_registry(state)

    record = next(r for r in extensions.records() if r.name == _EXTENSION_NAME)
    assert record.status == "loaded"
    assert record.capability_errors == []
    assert record.manifest is not None
    assert record.manifest.display_name == "Home Assistant"


def test_all_four_tools_registered_without_token() -> None:
    state = _State()  # no HASS_TOKEN
    _, tools = _load_registry(state)

    for name in _HA_TOOL_NAMES:
        tool = tools.get(name)
        assert tool.name == name
        assert tool.ready is not None


def test_ha_tools_carry_readiness_hint_and_extension_attribution() -> None:
    # tool.list surfaces these: the four tools are attributed to the extension and
    # carry the concrete hint explaining what makes them ready.
    state = _State()
    _, tools = _load_registry(state)

    for name in _HA_TOOL_NAMES:
        tool = tools.get(name)
        assert tool.extension == _EXTENSION_NAME
        assert tool.readiness_hint == (
            "Requires a Home Assistant connection - set the server URL and token "
            "in Settings -> Extensions."
        )


def test_not_ready_tools_absent_from_provider_definitions_without_token() -> None:
    state = _State()  # no token
    _, tools = _load_registry(state)

    names = {definition["name"] for definition in tools.provider_definitions()}
    assert names.isdisjoint(_HA_TOOL_NAMES)


def test_ready_tools_present_in_provider_definitions_with_token() -> None:
    tools = _tools_with_token()

    names = {definition["name"] for definition in tools.provider_definitions()}
    assert set(_HA_TOOL_NAMES) <= names


@pytest.mark.asyncio
async def test_dispatch_returns_tool_not_ready_without_token() -> None:
    state = _State()  # no token
    _, tools = _load_registry(state)

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    assert_failure_envelope(result, "tool_not_ready")


def test_readiness_flips_live_when_token_appears() -> None:
    state = _State()
    _, tools = _load_registry(state)

    assert {d["name"] for d in tools.provider_definitions()}.isdisjoint(_HA_TOOL_NAMES)

    state.credentials["HASS_TOKEN"] = _TOKEN

    assert set(_HA_TOOL_NAMES) <= {d["name"] for d in tools.provider_definitions()}


def test_settings_schema_declared() -> None:
    state = _State()
    extensions, _ = _load_registry(state)

    record = next(r for r in extensions.records() if r.name == _EXTENSION_NAME)
    schema = record.declarations.settings_schema
    assert schema is not None
    by_key = {field.key: field for field in schema}
    assert by_key["url"].type == "text"
    assert by_key["url"].default == _HASS_URL
    assert by_key["token"].type == "secret"
    assert by_key["token"].env_key == "HASS_TOKEN"
    assert by_key["token"].default is None


def test_display_metadata_preserved() -> None:
    tools = _tools_with_token()

    assert tools.get(HA_GET_STATE_NAME).display.summary_fields == ("entity_id",)
    assert tools.get(HA_CALL_SERVICE_NAME).display.summary_fields == (
        "domain",
        "service",
        "entity_id",
    )


def test_provider_schemas_follow_each_tools_current_migration_state() -> None:
    tools = _tools_with_token()

    list_entities = tools.get(HA_LIST_ENTITIES_NAME)
    assert list_entities.open_input_schema is True
    assert "additionalProperties" not in list_entities.parameters
    get_state = tools.get(HA_GET_STATE_NAME)
    assert get_state.open_input_schema is True
    assert "additionalProperties" not in get_state.parameters
    list_services = tools.get(HA_LIST_SERVICES_NAME)
    assert list_services.open_input_schema is True
    assert "additionalProperties" not in list_services.parameters
    call_service = tools.get(HA_CALL_SERVICE_NAME)
    assert call_service.open_input_schema is True
    assert "additionalProperties" not in call_service.parameters

    entity_id = tools.get(HA_GET_STATE_NAME).parameters["properties"]["entity_id"]
    assert entity_id["minLength"] == 1
    assert entity_id["pattern"]
    call_properties = tools.get(HA_CALL_SERVICE_NAME).parameters["properties"]
    assert call_properties["domain"]["pattern"]
    assert call_properties["service"]["pattern"]
    assert call_properties["data"]["type"] == "object"


# Handler guard: token removed between prompt build and call
@respx.mock
@pytest.mark.asyncio
async def test_handler_guard_when_token_removed_mid_flight() -> None:
    # Build with a token so the tool is ready and dispatch reaches the handler,
    # then remove the token so the handler's own guard fires — no request made.
    route = respx.get(f"{_HASS_URL}/api/states")
    state = _State()
    state.credentials["HASS_TOKEN"] = _TOKEN
    _, tools = _load_registry(state)

    tool = tools.get(HA_LIST_ENTITIES_NAME)
    state.credentials.pop("HASS_TOKEN")

    # Call the handler directly (dispatch would short-circuit on readiness first;
    # the guard is defense in depth behind that check).
    result = await cast(
        Awaitable[dict[str, Any]],
        tool.handler(make_context(HA_LIST_ENTITIES_NAME), {}),
    )

    assert_failure_envelope(result, "home_assistant_error")
    assert route.called is False


# URL resolution (live reads)
@respx.mock
@pytest.mark.asyncio
async def test_uses_default_url_when_config_absent() -> None:
    route = respx.get(f"{_HASS_URL}/api/states").mock(return_value=httpx.Response(200, json=[]))
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    assert route.called is True
    assert_success_envelope(result)


@respx.mock
@pytest.mark.asyncio
async def test_uses_configured_url_with_trailing_slash_stripped() -> None:
    custom = "http://ha.example:8123"
    route = respx.get(f"{custom}/api/states").mock(return_value=httpx.Response(200, json=[]))
    state = _State()
    state.credentials["HASS_TOKEN"] = _TOKEN
    state.config["url"] = f"{custom}/"  # trailing slash must be stripped
    _, tools = _load_registry(state)

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    assert route.called is True
    assert_success_envelope(result)


@respx.mock
@pytest.mark.asyncio
async def test_live_url_and_token_change_between_two_calls() -> None:
    first_url = "http://ha-one:8123"
    second_url = "http://ha-two:8123"
    first = respx.get(f"{first_url}/api/states").mock(return_value=httpx.Response(200, json=[]))
    second = respx.get(f"{second_url}/api/states").mock(return_value=httpx.Response(200, json=[]))

    state = _State()
    state.credentials["HASS_TOKEN"] = "token-one"
    state.config["url"] = first_url
    _, tools = _load_registry(state)

    await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})
    assert first.called is True

    # Change both the URL and the token between calls; the next call reads live.
    state.config["url"] = second_url
    state.credentials["HASS_TOKEN"] = "token-two"

    await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})
    assert second.called is True
    sent_token = second.calls[0].request.headers["Authorization"]
    assert sent_token == "Bearer token-two"
