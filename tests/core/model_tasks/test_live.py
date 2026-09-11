"""Offline verification against the documented Live WebRTC wire contract."""

import json

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator

from core.model_tasks.live import BACKEND_MODEL, LIVE_MODEL, LiveClient, live_tools
from core.providers.errors import ProviderAuthError, ProviderOutcomeUnknownError
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig


def client():
    return LiveClient(
        provider=ProviderConfig(
            id="openai",
            name="OpenAI",
            adapter="openai",
            base_url="https://api.openai.com/v1",
            connections=[],
        ),
        connection=ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(
                header="Authorization", prefix="Bearer ", credential_key="OPENAI_API_KEY"
            ),
        ),
        credential="test-secret",
        model_id=LIVE_MODEL,
    )


@pytest.mark.asyncio
@respx.mock
async def test_live_create_keeps_credentials_server_side_and_uses_responses_tools():
    route = respx.post("https://api.openai.com/v1/live/sessions").respond(
        201,
        json={
            "session": {"id": "opaque-live-id", "private": "hidden"},
            "transport": {"type": "webrtc", "sdp": "answer"},
            "secret": "hidden",
        },
    )
    result = await client().create_session("v=0\r\nfixture")
    body = json.loads(route.calls[0].request.content)
    assert body["transport"] == {"type": "webrtc", "sdp": "v=0\r\nfixture"}
    assert body["session"]["model"] == LIVE_MODEL
    backend = body["session"]["delegation"]
    assert backend["type"] == "responses"
    assert backend["responses"]["model"] == BACKEND_MODEL
    assert backend["responses"]["parallel_tool_calls"] is False
    assert all(tool["strict"] is False for tool in backend["responses"]["tools"])
    assert route.calls[0].request.headers["Authorization"] == "Bearer test-secret"
    assert result == {
        "session": {"id": "opaque-live-id"},
        "transport": {"type": "webrtc", "sdp": "answer"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sdp", ["", "not-sdp", "v=0" + "x" * 65536], ids=["empty", "invalid", "oversized"]
)
@respx.mock
async def test_bad_offer_never_reaches_provider(sdp):
    with pytest.raises(ValueError):
        await client().create_session(sdp)
    assert not respx.calls


@pytest.mark.asyncio
@respx.mock
async def test_live_creation_never_replays_ambiguous_failure():
    route = respx.post("https://api.openai.com/v1/live/sessions").mock(
        side_effect=httpx.ReadTimeout("fixture")
    )
    with pytest.raises(ProviderOutcomeUnknownError):
        await client().create_session("v=0\r\nfixture")
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_denied_access_is_not_retried():
    route = respx.post("https://api.openai.com/v1/live/sessions").respond(
        403, json={"error": {"message": "access denied"}}
    )
    with pytest.raises(ProviderAuthError):
        await client().create_session("v=0\r\nfixture")
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_malformed_creation_is_outcome_unknown_not_retryable():
    route = respx.post("https://api.openai.com/v1/live/sessions").respond(
        201, json={"session": {"id": "opaque"}}
    )
    with pytest.raises(ProviderOutcomeUnknownError):
        await client().create_session("v=0\r\nfixture")
    assert route.call_count == 1


def test_real_tool_schemas_accept_intended_calls_and_reject_unsupported_program():
    app, terminal = live_tools()
    for tool in (app, terminal):
        Draft202012Validator.check_schema(tool["parameters"])
    Draft202012Validator(app["parameters"]).validate(
        {"action": "send", "agent_id": "joel", "session_id": "session-a", "text": "continue"}
    )
    validator = Draft202012Validator(terminal["parameters"])
    validator.validate({"action": "start", "program": "codex", "count": 4, "workdir": "/workspace"})
    assert list(validator.iter_errors({"action": "start", "program": "bash"}))
    for count in (5, 12, 33):
        validator.validate({"action": "start", "program": "codex", "count": count})
    assert "maximum" not in terminal["parameters"]["properties"]["count"]
    for count in (0, -1, 1.5, "5"):
        assert list(validator.iter_errors({"action": "start", "program": "codex", "count": count}))
    for call in (
        {"action": "close", "terminal_id": "term-a"},
        {"action": "show_group", "group_id": "group-a"},
        {"action": "create_group", "name": "Review"},
        {"action": "rename_group", "group_id": "group-a", "name": "Review"},
        {"action": "delete_group", "group_id": "group-a"},
    ):
        validator.validate(call)
