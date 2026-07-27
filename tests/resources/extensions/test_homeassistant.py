"""Tests for the shipped Home Assistant bundled extension.

These load the **real** extension out of ``resources/extensions/homeassistant``
through ``ExtensionRegistry.load`` (with a bundled root) and apply its declared
tools into a fresh ``ToolRegistry`` — so they double as proof that the bundled
root ships a loadable extension. Credential and config are supplied through
mutable stubs so the live per-call reads (token, URL, readiness) can change
between calls exactly as they would through the settings UI.

The HTTP mocking approach mirrors the retired ``tests/core/tools/`` suite
(``respx`` + ``httpx``), and every behavior assertion from it is ported here.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Awaitable, Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import respx

from core.extensions import ExtensionRegistry
from core.tools.contracts import ToolContractError
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope, tool_failure

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUNDLED_EXTENSIONS_DIR = _REPO_ROOT / "resources" / "extensions"

HA_LIST_ENTITIES_NAME = "ha_list_entities"
HA_GET_STATE_NAME = "ha_get_state"
HA_LIST_SERVICES_NAME = "ha_list_services"
HA_CALL_SERVICE_NAME = "ha_call_service"

_HA_TOOL_NAMES = (
    HA_LIST_ENTITIES_NAME,
    HA_GET_STATE_NAME,
    HA_LIST_SERVICES_NAME,
    HA_CALL_SERVICE_NAME,
)

_HASS_URL = "http://homeassistant.local:8123"
_TOKEN = "test-ha-token"
_EXTENSION_NAME = "homeassistant"
_EXTENSION_MODULE = "vbot_ext.homeassistant"


class _State:
    """Mutable credential + config store backing the live per-call reads."""

    def __init__(self) -> None:
        self.credentials: dict[str, str] = {}
        self.config: dict[str, Any] = {}

    def resolve_credential(self, key: str) -> str:
        return self.credentials.get(key, "")

    def config_for(self, name: str) -> dict[str, Any]:
        del name
        return dict(self.config)


@pytest.fixture(autouse=True)
def _clean_extension_modules() -> Iterator[None]:
    """Drop the synthetic ``vbot_ext`` namespace after each test."""
    yield
    for module_name in list(sys.modules):
        if module_name == "vbot_ext" or module_name.startswith("vbot_ext."):
            del sys.modules[module_name]


def _load_registry(state: _State) -> tuple[ExtensionRegistry, ToolRegistry]:
    """Load the shipped extension and apply its tools into a fresh registry."""
    extensions = ExtensionRegistry.load(
        _REPO_ROOT / "does-not-exist-data-extensions",
        bundled_dir=_BUNDLED_EXTENSIONS_DIR,
        credential_resolver=state.resolve_credential,
        config_provider=state.config_for,
    )
    tools = ToolRegistry()
    extensions.apply_tools(tools)
    return extensions, tools


def _tools_with_token() -> ToolRegistry:
    state = _State()
    state.credentials["HASS_TOKEN"] = _TOKEN
    _, tools = _load_registry(state)
    return tools


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Patch the loaded extension's backoff sleep; return the attempts list."""
    module = sys.modules[_EXTENSION_MODULE]
    sleep_attempts: list[int] = []

    async def _fake_sleep(attempt: int) -> None:
        sleep_attempts.append(attempt)

    monkeypatch.setattr(module, "_sleep_for_retry", _fake_sleep)
    return sleep_attempts


def make_context(tool_name: str = HA_LIST_ENTITIES_NAME) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=Path("/tmp/workspace"),
        vbot_root=Path("/tmp/app"),
        data_root=Path("/tmp/data"),
    )


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
    """Dispatch a call with the same validation envelope as ``ToolExecutor``."""
    try:
        return await registry.dispatch(make_context(tool_name), arguments)
    except ToolContractError as error:
        return tool_failure("invalid_arguments", str(error), retryable=False)


# ---------------------------------------------------------------------------
# Loading, readiness, and the always-registered contract
# ---------------------------------------------------------------------------


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


def test_provider_schemas_reject_unknowns_and_describe_string_formats() -> None:
    tools = _tools_with_token()

    for name in _HA_TOOL_NAMES:
        assert tools.get(name).parameters["additionalProperties"] is False

    entity_id = tools.get(HA_GET_STATE_NAME).parameters["properties"]["entity_id"]
    assert entity_id["minLength"] == 1
    assert entity_id["pattern"]
    call_properties = tools.get(HA_CALL_SERVICE_NAME).parameters["properties"]
    assert call_properties["domain"]["pattern"]
    assert call_properties["service"]["pattern"]
    assert call_properties["data"]["type"] == "object"


# ---------------------------------------------------------------------------
# Handler guard: token removed between prompt build and call
# ---------------------------------------------------------------------------


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

    error = assert_failure_envelope(result, "home_assistant_error")
    assert "HASS_TOKEN" in error["message"]
    assert route.called is False


# ---------------------------------------------------------------------------
# URL resolution (live reads)
# ---------------------------------------------------------------------------


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
        record.levelno == logging.WARNING and "Home Assistant request failed" in record.getMessage()
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
    ("tool_name", "arguments", "message"),
    (
        (HA_LIST_ENTITIES_NAME, {"unknown": True}, "Additional properties are not allowed"),
        (HA_LIST_ENTITIES_NAME, {"domain": 42}, "is not of type 'string'"),
        (HA_GET_STATE_NAME, {"entity_id": 42}, "is not of type 'string'"),
        (HA_LIST_SERVICES_NAME, {"domain": []}, "is not of type 'string'"),
        (
            HA_CALL_SERVICE_NAME,
            {"domain": "light", "service": "turn_on", "data": []},
            "is not of type 'object'",
        ),
    ),
)
async def test_handlers_reject_unknown_or_wrong_typed_arguments(
    tool_name: str,
    arguments: dict[str, Any],
    message: str,
) -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, tool_name, arguments)

    error = assert_failure_envelope(result, "invalid_arguments")
    assert message in error["message"]


@respx.mock
@pytest.mark.asyncio
async def test_get_state_not_found() -> None:
    respx.get(f"{_HASS_URL}/api/states/light.missing").mock(
        return_value=httpx.Response(404, json={"message": "Entity not found"})
    )
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_GET_STATE_NAME, {"entity_id": "light.missing"})

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


# ---------------------------------------------------------------------------
# ha_call_service
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Network / retry
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_network_error_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    route = respx.get(f"{_HASS_URL}/api/states").mock(side_effect=_raise_connect_error)
    tools = _tools_with_token()
    sleep_attempts = _no_sleep(monkeypatch)

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})

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
    route = respx.get(f"{_HASS_URL}/api/states").mock(
        side_effect=[
            httpx.Response(status_code, json={"message": "temporary failure"}),
            httpx.Response(
                200,
                json=[{"entity_id": "light.kitchen", "state": "off", "attributes": {}}],
            ),
        ]
    )
    tools = _tools_with_token()
    sleep_attempts = _no_sleep(monkeypatch)

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})

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
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    assert_failure_envelope(result, "home_assistant_error")
    assert len(route.calls) == 1  # no retry on 401


@respx.mock
@pytest.mark.asyncio
async def test_no_retry_on_404() -> None:
    route = respx.get(f"{_HASS_URL}/api/states/light.missing").mock(
        return_value=httpx.Response(404, json={"message": "Not found"})
    )
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_GET_STATE_NAME, {"entity_id": "light.missing"})

    assert_failure_envelope(result, "home_assistant_error")
    assert len(route.calls) == 1  # no retry on 404


# ---------------------------------------------------------------------------
# Retry signalling in the failure envelope
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_exhausted_transient_status_signals_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = respx.get(f"{_HASS_URL}/api/states").mock(
        return_value=httpx.Response(503, json={"message": "busy"})
    )
    tools = _tools_with_token()
    _no_sleep(monkeypatch)
    module = sys.modules[_EXTENSION_MODULE]

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    error = assert_failure_envelope(result, "home_assistant_error")
    assert error["retryable"] is True
    assert error["attempts_made"] == module._RETRY_MAX_RETRIES + 1
    assert len(route.calls) == module._RETRY_MAX_RETRIES + 1


@respx.mock
@pytest.mark.asyncio
async def test_exhausted_transport_error_signals_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    respx.get(f"{_HASS_URL}/api/states").mock(side_effect=_raise_connect_error)
    tools = _tools_with_token()
    _no_sleep(monkeypatch)
    module = sys.modules[_EXTENSION_MODULE]

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    error = assert_failure_envelope(result, "home_assistant_error")
    assert error["retryable"] is True
    assert error["attempts_made"] == module._RETRY_MAX_RETRIES + 1


@respx.mock
@pytest.mark.asyncio
async def test_non_retryable_status_signals_not_retryable() -> None:
    respx.get(f"{_HASS_URL}/api/states/light.missing").mock(
        return_value=httpx.Response(404, json={"message": "Not found"})
    )
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_GET_STATE_NAME, {"entity_id": "light.missing"})

    error = assert_failure_envelope(result, "home_assistant_error")
    assert error["retryable"] is False
    assert "attempts_made" not in error


@respx.mock
@pytest.mark.asyncio
async def test_non_idempotent_post_500_is_not_retryable() -> None:
    # ha_call_service POSTs, which is not idempotent, so a 500 is fatal (no retry).
    route = respx.post(f"{_HASS_URL}/api/services/light/turn_on").mock(
        return_value=httpx.Response(500, json={"message": "boom"})
    )
    tools = _tools_with_token()

    result = await _dispatch(
        tools,
        HA_CALL_SERVICE_NAME,
        {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"},
    )

    error = assert_failure_envelope(result, "home_assistant_error")
    assert error["retryable"] is False
    assert "attempts_made" not in error
    assert len(route.calls) == 1


@pytest.mark.asyncio
async def test_validation_error_signals_not_retryable() -> None:
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_GET_STATE_NAME, {"entity_id": "not a valid id"})

    error = assert_failure_envelope(result, "invalid_arguments")
    assert error["retryable"] is False


# ---------------------------------------------------------------------------
# The token value is never logged
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_token_never_appears_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    respx.get(f"{_HASS_URL}/api/states").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )
    secret_token = "super-secret-ha-token-value"
    state = _State()
    state.credentials["HASS_TOKEN"] = secret_token
    _, tools = _load_registry(state)

    with caplog.at_level(logging.DEBUG):
        await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    for record in caplog.records:
        assert secret_token not in record.getMessage()
