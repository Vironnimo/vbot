"""Home Assistant: repeat safety and request failure messages."""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
import pytest
import respx

from tests.resources.extensions.homeassistant.homeassistant_test_support import (
    EXTENSION_NAME,
    HA_CALL_SERVICE_NAME,
    HA_LIST_ENTITIES_NAME,
    HASS_URL,
    LiveSettings,
    assert_failure_envelope,
    assert_success_envelope,
    dispatch,
    load,
    no_sleep,
    tools_with_token,
)

_TURN_ON = {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"}


def _raise(error_type: type[httpx.TransportError]) -> Callable[[httpx.Request], httpx.Response]:
    def side_effect(request: httpx.Request) -> httpx.Response:
        raise error_type("fixture transport failure", request=request)

    return side_effect


# Reads repeat transient failures
@respx.mock
@pytest.mark.asyncio
async def test_unreachable_server_is_retried_then_reported_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = respx.get(f"{HASS_URL}/api/states").mock(side_effect=_raise(httpx.ConnectError))
    tools = tools_with_token()
    sleep_attempts = no_sleep(monkeypatch)

    result = await dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    error = assert_failure_envelope(result, "home_assistant_error")
    assert len(route.calls) == 3  # 1 initial + 2 retries
    assert sleep_attempts == [0, 1]
    assert (error["retryable"], error["attempts_made"]) == (True, 3)
    assert error["message"] == (
        f"Could not reach Home Assistant at {HASS_URL} (ConnectError: fixture transport "
        "failure) after 3 attempts. Check that Home Assistant is running and that the server "
        "URL in Settings -> Extensions is right, then try again later."
    )


# 429 is refused for any method; 500 is retried only because the read is idempotent.
@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 500])
async def test_transient_status_on_a_read_is_retried(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    route = respx.get(f"{HASS_URL}/api/states").mock(
        side_effect=[
            httpx.Response(status_code, json={"message": "temporary failure"}),
            httpx.Response(
                200,
                json=[{"entity_id": "light.kitchen", "state": "off", "attributes": {}}],
            ),
        ]
    )
    tools = tools_with_token()
    sleep_attempts = no_sleep(monkeypatch)

    result = await dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    assert assert_success_envelope(result)["count"] == 1
    assert len(route.calls) == 2
    assert sleep_attempts == [0]


@respx.mock
@pytest.mark.asyncio
async def test_read_failing_on_every_attempt_is_logged_and_reported_as_retryable(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = respx.get(f"{HASS_URL}/api/states").mock(
        return_value=httpx.Response(500, json={"message": "internal error"})
    )
    tools = tools_with_token()
    no_sleep(monkeypatch)
    logger_name = f"vbot.extensions.{EXTENSION_NAME}"

    with caplog.at_level(logging.WARNING, logger=logger_name):
        result = await dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    error = assert_failure_envelope(result, "home_assistant_error")
    assert error["message"] == (
        "Home Assistant is busy or unavailable (HTTP 500: internal error) after 3 attempts. "
        "Try again later."
    )
    assert (error["retryable"], error["attempts_made"]) == (True, 3)
    assert len(route.calls) == 3
    assert any(
        record.levelno == logging.WARNING and record.name == logger_name
        for record in caplog.records
    )


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_rejected_token_is_not_retried_and_goes_to_the_user(status_code: int) -> None:
    route = respx.get(f"{HASS_URL}/api/states").mock(
        return_value=httpx.Response(status_code, json={"message": "Unauthorized"})
    )

    result = await dispatch(tools_with_token(), HA_LIST_ENTITIES_NAME, {})

    error = assert_failure_envelope(result, "home_assistant_error")
    assert len(route.calls) == 1
    assert error["message"] == (
        f"Home Assistant rejected the access token (HTTP {status_code}). Tell the user to "
        "check the Home Assistant token in Settings -> Extensions."
    )


@respx.mock
@pytest.mark.asyncio
async def test_non_json_answer_points_at_the_server_url() -> None:
    respx.get(f"{HASS_URL}/api/states").mock(return_value=httpx.Response(200, text="<html>"))

    result = await dispatch(tools_with_token(), HA_LIST_ENTITIES_NAME, {})

    error = assert_failure_envelope(result, "home_assistant_error")
    assert "points to Home Assistant" in error["message"]


# Service calls repeat only requests that cannot have acted
@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [500, 502])
async def test_service_call_failing_while_running_is_not_repeated(status_code: int) -> None:
    # A POST service call may already have acted, so these statuses never repeat it.
    route = respx.post(f"{HASS_URL}/api/services/light/turn_on").mock(
        return_value=httpx.Response(status_code, json={"message": "boom"})
    )

    result = await dispatch(tools_with_token(), HA_CALL_SERVICE_NAME, _TURN_ON)

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
    route = respx.post(f"{HASS_URL}/api/services/light/turn_on").mock(
        side_effect=[
            httpx.Response(503, json={"message": "busy"}),
            httpx.Response(200, json=[{"entity_id": "light.kitchen", "state": "on"}]),
        ]
    )
    tools = tools_with_token()
    no_sleep(monkeypatch)

    result = await dispatch(tools, HA_CALL_SERVICE_NAME, _TURN_ON)

    assert assert_success_envelope(result)["changed"] == 1
    assert len(route.calls) == 2


@respx.mock
@pytest.mark.asyncio
async def test_service_call_repeats_only_connections_that_never_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unreachable = respx.post(f"{HASS_URL}/api/services/light/turn_on").mock(
        side_effect=_raise(httpx.ConnectError)
    )
    tools = tools_with_token()
    no_sleep(monkeypatch)

    refused = assert_failure_envelope(
        await dispatch(tools, HA_CALL_SERVICE_NAME, _TURN_ON), "home_assistant_error"
    )

    assert len(unreachable.calls) == 3
    assert "; light.turn_on was not called." in refused["message"]
    assert refused["retryable"] is True

    broken = respx.post(f"{HASS_URL}/api/services/light/turn_off").mock(
        side_effect=_raise(httpx.ReadTimeout)
    )
    uncertain = assert_failure_envelope(
        await dispatch(tools, HA_CALL_SERVICE_NAME, {**_TURN_ON, "service": "turn_off"}),
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
async def test_token_never_appears_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    respx.get(f"{HASS_URL}/api/states").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )
    secret_token = "super-secret-ha-token-value"
    _, tools = load(LiveSettings(token=secret_token))

    with caplog.at_level(logging.DEBUG):
        await dispatch(tools, HA_LIST_ENTITIES_NAME, {})

    for record in caplog.records:
        assert secret_token not in record.getMessage()
