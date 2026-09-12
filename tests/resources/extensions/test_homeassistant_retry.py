"""Homeassistant: retry behavior."""

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


# Network / retry
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
