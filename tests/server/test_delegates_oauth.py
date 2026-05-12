"""Tests for OAuth provider RPC delegates."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from core.providers.auth_flow import DeviceFlowSession
from core.providers.providers import AuthConfig, ConnectionConfig, OAuthConfig, ProviderConfig
from core.providers.token_store import OAuthToken, TokenStore
from server.delegates import dispatch_rpc
from server.events import PROVIDER_AUTH_COMPLETED_EVENT, ServerEventBus


class StubDeviceFlowEngine:
    def __init__(self) -> None:
        self.started: list[tuple[str, str, OAuthConfig]] = []
        self.polls: list[tuple[str, str, OAuthConfig, str, int, Any]] = []
        self.cancelled: list[tuple[str, str]] = []
        self._active_flows: dict[tuple[str, str], asyncio.Task[None]] = {}

    async def start_device_flow(
        self,
        provider_id: str,
        local_connection_id: str,
        oauth_config: OAuthConfig,
    ) -> DeviceFlowSession:
        self.started.append((provider_id, local_connection_id, oauth_config))
        return DeviceFlowSession(
            device_code="device-code",
            user_code="ABCD-1234",
            verification_uri="https://github.com/login/device",
            expires_in=900,
            interval=5,
        )

    async def _poll_for_token(
        self,
        provider_id: str,
        local_connection_id: str,
        oauth_config: OAuthConfig,
        device_code: str,
        interval: int,
        on_complete: Any,
    ) -> None:
        self.polls.append(
            (provider_id, local_connection_id, oauth_config, device_code, interval, on_complete)
        )

    def cancel_flow(self, provider_id: str, local_connection_id: str) -> None:
        self.cancelled.append((provider_id, local_connection_id))


class StubProviderRegistry:
    def __init__(self, provider: ProviderConfig) -> None:
        self._provider = provider

    def get(self, provider_id: str) -> ProviderConfig:
        if provider_id != self._provider.id:
            raise KeyError(provider_id)
        return self._provider


def oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="client-id",
        device_auth_url="https://github.com/login/device/code",
        token_url="https://github.com/login/oauth/access_token",
        scopes=["copilot"],
        token_exchange_url="https://api.github.com/copilot_internal/v2/token",
    )


def make_provider(*, connection: ConnectionConfig) -> ProviderConfig:
    return ProviderConfig(
        id="github-copilot",
        name="GitHub Copilot",
        adapter="openai_compatible",
        base_url="https://api.githubcopilot.com",
        connections=[connection],
    )


def make_oauth_connection() -> ConnectionConfig:
    return ConnectionConfig(
        id="oauth",
        type="oauth",
        label="Sign in with GitHub",
        auth=AuthConfig(header="Authorization", prefix="Bearer "),
        oauth=oauth_config(),
    )


def make_api_key_connection() -> ConnectionConfig:
    return ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(
            header="Authorization",
            prefix="Bearer ",
            credential_key="GITHUB_COPILOT_API_KEY",
        ),
    )


def make_state(tmp_path: Any, provider: ProviderConfig) -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(
            providers=StubProviderRegistry(provider),
            token_store=TokenStore(tmp_path),
        ),
        event_bus=ServerEventBus(),
    )


@pytest.mark.asyncio
async def test_provider_connect_starts_device_flow_and_polling(tmp_path: Any) -> None:
    state = make_state(tmp_path, make_provider(connection=make_oauth_connection()))
    engine = StubDeviceFlowEngine()
    state.device_flow_engine = engine

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.connect",
            "params": {
                "provider_id": "github-copilot",
                "connection_id": "github-copilot:oauth",
            },
        },
    )
    await asyncio.sleep(0)

    assert response == {
        "ok": True,
        "result": {
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
        },
    }
    assert engine.started == [("github-copilot", "oauth", oauth_config())]
    assert len(engine.polls) == 1
    poll = engine.polls[0]
    assert poll[:5] == ("github-copilot", "oauth", oauth_config(), "device-code", 5)


@pytest.mark.asyncio
async def test_provider_connect_completion_callback_publishes_event(tmp_path: Any) -> None:
    state = make_state(tmp_path, make_provider(connection=make_oauth_connection()))
    engine = StubDeviceFlowEngine()
    state.device_flow_engine = engine

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.connect",
            "params": {
                "provider_id": "github-copilot",
                "connection_id": "github-copilot:oauth",
            },
        },
    )
    await asyncio.sleep(0)
    on_complete = engine.polls[0][5]

    await on_complete(success=True)

    assert response["ok"] is True
    assert state.event_bus.events[-1]["type"] == PROVIDER_AUTH_COMPLETED_EVENT
    assert state.event_bus.events[-1]["payload"] == {
        "provider_id": "github-copilot",
        "connection_id": "github-copilot:oauth",
        "success": True,
    }


@pytest.mark.asyncio
async def test_provider_connect_rejects_non_oauth_connection(tmp_path: Any) -> None:
    state = make_state(tmp_path, make_provider(connection=make_api_key_connection()))

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.connect",
            "params": {
                "provider_id": "github-copilot",
                "connection_id": "github-copilot:api-key",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "oauth_not_supported"


@pytest.mark.asyncio
async def test_provider_disconnect_deletes_token_and_cancels_flow(tmp_path: Any) -> None:
    state = make_state(tmp_path, make_provider(connection=make_oauth_connection()))
    engine = StubDeviceFlowEngine()
    state.device_flow_engine = engine
    state.runtime.token_store.save(
        "github-copilot",
        "oauth",
        OAuthToken(access_token="stored-token"),
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.disconnect",
            "params": {
                "provider_id": "github-copilot",
                "connection_id": "github-copilot:oauth",
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "provider_id": "github-copilot",
            "connection_id": "github-copilot:oauth",
            "status": "disconnected",
        },
    }
    assert state.runtime.token_store.load("github-copilot", "oauth") is None
    assert engine.cancelled == [("github-copilot", "oauth")]


@pytest.mark.asyncio
async def test_provider_connection_status_reports_token_and_active_flow(tmp_path: Any) -> None:
    state = make_state(tmp_path, make_provider(connection=make_oauth_connection()))
    engine = StubDeviceFlowEngine()
    state.device_flow_engine = engine
    state.runtime.token_store.save(
        "github-copilot",
        "oauth",
        OAuthToken(access_token="stored-token"),
    )
    task = asyncio.create_task(asyncio.sleep(60))
    engine._active_flows[("github-copilot", "oauth")] = task

    try:
        response = await dispatch_rpc(
            state,
            {
                "method": "provider.connection_status",
                "params": {
                    "provider_id": "github-copilot",
                    "connection_id": "github-copilot:oauth",
                },
            },
        )
    finally:
        task.cancel()

    assert response == {
        "ok": True,
        "result": {
            "provider_id": "github-copilot",
            "connection_id": "github-copilot:oauth",
            "connected": True,
            "flow_active": True,
        },
    }
