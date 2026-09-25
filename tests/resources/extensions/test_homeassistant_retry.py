"""Homeassistant: retry behavior and request failure messages."""

from __future__ import annotations

import logging
import sys

import httpx
import pytest
import respx

from tests.resources.extensions.homeassistant_helpers import (
    _EXTENSION_MODULE,
    _HASS_URL,
    HA_CALL_SERVICE_NAME,
    HA_GET_STATE_NAME,
    HA_LIST_ENTITIES_NAME,
    _dispatch,
    _load_registry,
    _no_sleep,
    _State,
    _tools_with_token,
    assert_failure_envelope,
    assert_success_envelope,
)
from tests.resources.extensions.homeassistant_helpers import (
    _clean_extension_modules as _clean_extension_modules,
)

_TURN_ON = {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"}


def _raise(error_type: type[httpx.TransportError]):
    def side_effect(request: httpx.Request) -> httpx.Response:
        raise error_type("fixture transport failure", request=request)

    return side_effect


# Network / retry
@respx.mock
@pytest.mark.asyncio
async def test_network_error_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    route = respx.get(f"{_HASS_URL}/api/states").mock(side_effect=_raise(httpx.ConnectError))
    tools = _tools_with_token()
    sleep_attempts = _no_sleep(monkeypatch)

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    error = assert_failure_envelope(result, "home_assistant_error")
    assert len(route.calls) == 3  # 1 initial + 2 retries
    assert sleep_attempts == [0, 1]
    assert error["message"] == (
        f"Could not reach Home Assistant at {_HASS_URL} (ConnectError: fixture transport "
        "failure) after 3 attempts. Check that Home Assistant is running and that the server "
        "URL in Settings -> Extensions is right, then try again later."
    )


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
@pytest.mark.parametrize("status_code", [401, 403])
async def test_rejected_token_is_not_retried_and_goes_to_the_user(status_code: int) -> None:
    route = respx.get(f"{_HASS_URL}/api/states").mock(
        return_value=httpx.Response(status_code, json={"message": "Unauthorized"})
    )
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    error = assert_failure_envelope(result, "home_assistant_error")
    assert len(route.calls) == 1
    assert error["message"] == (
        f"Home Assistant rejected the access token (HTTP {status_code}). Tell the user to "
        "check the Home Assistant token in Settings -> Extensions."
    )


@respx.mock
@pytest.mark.asyncio
async def test_missing_entity_is_not_retried_or_marked_retryable() -> None:
    route = respx.get(f"{_HASS_URL}/api/states/light.missing").mock(
        return_value=httpx.Response(404, json={"message": "Not found"})
    )
    respx.get(f"{_HASS_URL}/api/states").mock(return_value=httpx.Response(200, json=[]))
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_GET_STATE_NAME, {"entity_id": "light.missing"})

    error = assert_failure_envelope(result, "entity_not_found")
    assert len(route.calls) == 1  # no retry on 404
    assert error["retryable"] is False
    assert "attempts_made" not in error
    assert error["message"] == (
        "Home Assistant has no entity light.missing. Find the id with ha_list_entities "
        '{"area":"missing"}.'
    )


# Retry signalling in the failure envelope
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
    respx.get(f"{_HASS_URL}/api/states").mock(side_effect=_raise(httpx.ConnectError))
    tools = _tools_with_token()
    _no_sleep(monkeypatch)
    module = sys.modules[_EXTENSION_MODULE]

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    error = assert_failure_envelope(result, "home_assistant_error")
    assert error["retryable"] is True
    assert error["attempts_made"] == module._RETRY_MAX_RETRIES + 1


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [500, 502, 504])
async def test_service_call_failing_while_running_is_not_repeated(status_code: int) -> None:
    # A POST service call may already have acted, so these statuses never repeat it.
    route = respx.post(f"{_HASS_URL}/api/services/light/turn_on").mock(
        return_value=httpx.Response(status_code, json={"message": "boom"})
    )
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_CALL_SERVICE_NAME, _TURN_ON)

    error = assert_failure_envelope(result, "home_assistant_error")
    assert error["retryable"] is False
    assert "attempts_made" not in error
    assert len(route.calls) == 1
    assert error["message"] == (
        f"Home Assistant failed while running light.turn_on (HTTP {status_code}: boom); it may "
        'have partly run. Check with ha_get_state {"entity_id":"light.kitchen"} before calling '
        "it again."
    )


@respx.mock
@pytest.mark.asyncio
async def test_service_call_refused_as_busy_is_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    route = respx.post(f"{_HASS_URL}/api/services/light/turn_on").mock(
        side_effect=[
            httpx.Response(503, json={"message": "busy"}),
            httpx.Response(200, json=[{"entity_id": "light.kitchen", "state": "on"}]),
        ]
    )
    tools = _tools_with_token()
    _no_sleep(monkeypatch)

    result = await _dispatch(tools, HA_CALL_SERVICE_NAME, _TURN_ON)

    assert assert_success_envelope(result)["changed"] == 1
    assert len(route.calls) == 2


@respx.mock
@pytest.mark.asyncio
async def test_service_call_repeats_only_connections_that_never_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unreachable = respx.post(f"{_HASS_URL}/api/services/light/turn_on").mock(
        side_effect=_raise(httpx.ConnectError)
    )
    tools = _tools_with_token()
    _no_sleep(monkeypatch)

    refused = assert_failure_envelope(
        await _dispatch(tools, HA_CALL_SERVICE_NAME, _TURN_ON), "home_assistant_error"
    )

    assert len(unreachable.calls) == 3
    assert "; light.turn_on was not called." in refused["message"]
    assert refused["retryable"] is True

    broken = respx.post(f"{_HASS_URL}/api/services/light/turn_off").mock(
        side_effect=_raise(httpx.ReadTimeout)
    )
    uncertain = assert_failure_envelope(
        await _dispatch(tools, HA_CALL_SERVICE_NAME, {**_TURN_ON, "service": "turn_off"}),
        "home_assistant_error",
    )

    assert len(broken.calls) == 1
    assert uncertain["retryable"] is False
    assert uncertain["message"] == (
        "The connection to Home Assistant failed after light.turn_off was sent (ReadTimeout: "
        "fixture transport failure), so it may or may not have run. Check with ha_get_state "
        '{"entity_id":"light.kitchen"} before calling it again.'
    )


@respx.mock
@pytest.mark.asyncio
async def test_invalid_entity_id_signals_not_retryable() -> None:
    respx.get(f"{_HASS_URL}/api/states").mock(return_value=httpx.Response(200, json=[]))
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_GET_STATE_NAME, {"entity_id": "not a valid id"})

    error = assert_failure_envelope(result, "invalid_arguments")
    assert error["retryable"] is False


@respx.mock
@pytest.mark.asyncio
async def test_non_json_answer_points_at_the_server_url() -> None:
    respx.get(f"{_HASS_URL}/api/states").mock(return_value=httpx.Response(200, text="<html>"))
    tools = _tools_with_token()

    result = await _dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    error = assert_failure_envelope(result, "home_assistant_error")
    assert "points to Home Assistant" in error["message"]


# The token value is never logged
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
