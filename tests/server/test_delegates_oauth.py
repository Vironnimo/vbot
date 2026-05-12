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
        self.polls: list[tuple[str, str, OAuthConfig, str, int, int, Any]] = []
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
        expires_in: int,
        on_complete: Any,
    ) -> None:
        self.polls.append(
            (
                provider_id,
                local_connection_id,
                oauth_config,
                device_code,
                interval,
                expires_in,
                on_complete,
            )
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

    def list_ids(self) -> list[str]:
        return [self._provider.id]


class StubProviderCredentials:
    def __init__(self, usable_connection_ids: set[str]) -> None:
        self._usable_connection_ids = usable_connection_ids
        self.requested_credentials: list[str] = []

    def has_credentials(self, provider_id: str, connection_id: str) -> bool:
        return provider_id == "github-copilot" and connection_id in self._usable_connection_ids

    def get_credentials(self, provider_id: str, connection_id: str) -> str:
        self.requested_credentials.append(connection_id)
        if self.has_credentials(provider_id, connection_id):
            return "api-key-secret"
        raise KeyError(connection_id)


class StubModelRegistry:
    def list_for_provider(self, _provider_id: str) -> list[Any]:
        return []


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
        base_url=None,
    )


def make_refreshable_oauth_provider() -> ProviderConfig:
    provider = make_provider(connection=make_oauth_connection())
    return ProviderConfig(
        id=provider.id,
        name=provider.name,
        adapter=provider.adapter,
        base_url=provider.base_url,
        connections=provider.connections,
        defaults=provider.defaults,
        extra_headers=provider.extra_headers,
        models_endpoint="/models",
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
        base_url=None,
    )


def make_state(tmp_path: Any, provider: ProviderConfig) -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(
            providers=StubProviderRegistry(provider),
            token_store=TokenStore(tmp_path),
            provider_credentials=StubProviderCredentials(
                {f"{provider.id}:{connection.id}" for connection in provider.connections}
            ),
            models=StubModelRegistry(),
            _resolve_resources_path=lambda: tmp_path / "resources",
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
    assert poll[:6] == ("github-copilot", "oauth", oauth_config(), "device-code", 5, 900)


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
    on_complete = engine.polls[0][6]

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


@pytest.mark.asyncio
async def test_model_refresh_db_uses_oauth_token_getter_for_fresh_token(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = make_state(tmp_path, make_refreshable_oauth_provider())
    state.runtime.token_store.save(
        "github-copilot",
        "oauth",
        OAuthToken(access_token="stale-token", extra={"github_oauth_token": "github-secret"}),
    )
    refreshed: dict[str, Any] = {}

    class StubOAuthTokenGetter:
        def __init__(
            self, token_store: Any, provider_id: str, connection_id: str, config: Any
        ) -> None:
            self.args = (token_store, provider_id, connection_id, config)

        async def __aenter__(self) -> StubOAuthTokenGetter:
            refreshed["entered"] = True
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            refreshed["closed"] = True

        async def __call__(self) -> str:
            refreshed["getter_args"] = self.args
            return "fresh-runtime-token"

    async def fake_refresh_models(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        refreshed["credential"] = _args[1]
        refreshed["connection"] = _kwargs["credential_connection"]
        return {
            "provider_id": "github-copilot",
            "model_count": 0,
            "fetched_at": "2026-05-12T00:00:00+00:00",
        }

    monkeypatch.setattr("server.delegates.OAuthTokenGetter", StubOAuthTokenGetter)
    monkeypatch.setattr("server.delegates.refresh_models", fake_refresh_models)

    response = await dispatch_rpc(
        state,
        {
            "method": "model.refresh_db",
            "params": {"provider_id": "github-copilot"},
        },
    )

    assert response["ok"] is True
    assert refreshed["credential"] == "fresh-runtime-token"
    assert refreshed["connection"].id == "oauth"
    assert refreshed["entered"] is True
    assert refreshed["closed"] is True
    assert state.runtime.provider_credentials.requested_credentials == []


@pytest.mark.asyncio
async def test_model_refresh_db_preserves_api_key_credential_path(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = make_provider(connection=make_api_key_connection())
    provider = ProviderConfig(
        id=provider.id,
        name=provider.name,
        adapter=provider.adapter,
        base_url=provider.base_url,
        connections=provider.connections,
        defaults=provider.defaults,
        extra_headers=provider.extra_headers,
        models_endpoint="/models",
    )
    state = make_state(tmp_path, provider)
    refreshed: dict[str, Any] = {}

    async def fake_refresh_models(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        refreshed["credential"] = _args[1]
        return {
            "provider_id": "github-copilot",
            "model_count": 0,
            "fetched_at": "2026-05-12T00:00:00+00:00",
        }

    monkeypatch.setattr("server.delegates.refresh_models", fake_refresh_models)

    response = await dispatch_rpc(
        state,
        {
            "method": "model.refresh_db",
            "params": {"provider_id": "github-copilot"},
        },
    )

    assert response["ok"] is True
    assert refreshed["credential"] == "api-key-secret"
    assert state.runtime.provider_credentials.requested_credentials == ["github-copilot:api-key"]
