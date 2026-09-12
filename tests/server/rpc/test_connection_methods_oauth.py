"""Tests for OAuth provider RPC handlers."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from core.providers.accounts import ConnectionRef
from core.providers.auth_flow import DeviceFlowSession
from core.providers.credentials import ProviderCredentialResolver
from core.providers.errors import NetworkError, ProviderAuthError, ProviderError
from core.providers.providers import (
    AuthConfig,
    ConnectionConfig,
    OAuthConfig,
    ProviderConfig,
    ProviderRegistry,
)
from core.providers.token_store import OAuthToken, TokenStore
from core.storage.layout import DataDirectoryLayout
from server.events import (
    PROVIDER_AUTH_COMPLETED_EVENT,
    RESOURCE_CHANGED_EVENT,
    ServerEventBus,
)
from server.rpc import connection_methods
from server.rpc.methods import dispatch_rpc


class StubDeviceFlowEngine:
    def __init__(self) -> None:
        self.started: list[tuple[str, str, OAuthConfig, str]] = []
        self.completions: list[Any] = []
        self.cancelled: list[tuple[str, str, str]] = []
        self.active: set[tuple[str, str, str]] = set()

    async def connect(
        self,
        provider_id: str,
        local_connection_id: str,
        oauth_config: OAuthConfig,
        on_complete: Any,
        *,
        account_id: str = "default",
    ) -> DeviceFlowSession:
        self.started.append((provider_id, local_connection_id, oauth_config, account_id))
        self.completions.append(on_complete)
        self.active.add((provider_id, local_connection_id, account_id))
        return DeviceFlowSession(
            device_code="device-code",
            user_code="ABCD-1234",
            verification_uri="https://github.com/login/device",
            expires_in=900,
            interval=5,
        )

    def is_flow_active(self, provider_id: str, connection_id: str, account_id: str) -> bool:
        return (provider_id, connection_id, account_id) in self.active

    def cancel_flow(
        self,
        provider_id: str,
        local_connection_id: str,
        account_id: str = "default",
    ) -> None:
        self.cancelled.append((provider_id, local_connection_id, account_id))
        self.active.discard((provider_id, local_connection_id, account_id))


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

    def is_connection_enabled(self, provider_id: str, connection_id: str | None = None) -> bool:
        return True

    def is_usable(self, provider_id: str, connection_id: str) -> bool:
        return self.has_credentials(provider_id, connection_id)

    def get_credentials(self, provider_id: str, connection_id: str) -> str:
        self.requested_credentials.append(connection_id)
        if self.has_credentials(provider_id, connection_id):
            return "api-key-secret"
        raise KeyError(connection_id)

    def resolve_account_id(self, provider_id: str, connection_id: str) -> str:
        assert f"{provider_id}:{connection_id}" in self._usable_connection_ids
        return "default"


class StubModelRegistry:
    def list_for_provider(self, _provider_id: str) -> list[Any]:
        return []

    def reload(self, _resources_dir: Any, **_kwargs: Any) -> None:
        # refresh_db reloads the registry in place after writing layer files; the
        # stub has nothing to re-assemble, so this is a no-op.
        pass


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


async def no_models_dev_catalog() -> None:
    return None


def make_state(tmp_path: Any, provider: ProviderConfig) -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(
            providers=StubProviderRegistry(provider),
            token_store=TokenStore(tmp_path),
            provider_credentials=StubProviderCredentials(
                {f"{provider.id}:{connection.id}" for connection in provider.connections}
            ),
            storage=SimpleNamespace(
                data_dir=tmp_path,
                layout=DataDirectoryLayout(tmp_path),
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
            "account": "default",
        },
    }
    assert engine.started == [("github-copilot", "oauth", oauth_config(), "default")]
    assert len(engine.completions) == 1
    assert engine.is_flow_active("github-copilot", "oauth", "default")


@pytest.mark.asyncio
async def test_provider_connect_threads_account_into_device_flow(tmp_path: Any) -> None:
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
                "account": "work",
            },
        },
    )
    await asyncio.sleep(0)

    assert response["ok"] is True
    assert response["result"]["account"] == "work"
    assert engine.started == [("github-copilot", "oauth", oauth_config(), "work")]
    assert engine.is_flow_active("github-copilot", "oauth", "work")


@pytest.mark.asyncio
async def test_provider_connect_rejects_invalid_account_id(tmp_path: Any) -> None:
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
                "account": "Not-Valid",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert engine.started == []


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
    on_complete = engine.completions[0]

    await on_complete(success=True)

    assert response["ok"] is True
    # A successful login also emits a generic resource_changed alongside this
    # event, so locate the targeted auth event rather than assuming it is last.
    auth_events = [
        event for event in state.event_bus.events if event["type"] == PROVIDER_AUTH_COMPLETED_EVENT
    ]
    assert len(auth_events) == 1
    assert auth_events[0]["payload"] == {
        "provider_id": "github-copilot",
        "connection_id": "github-copilot:oauth",
        "account": "default",
        "success": True,
    }


@pytest.mark.asyncio
async def test_provider_connect_completion_event_carries_named_account(tmp_path: Any) -> None:
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
                "account": "work",
            },
        },
    )
    await asyncio.sleep(0)
    on_complete = engine.completions[0]

    await on_complete(success=False)

    assert response["ok"] is True
    assert state.event_bus.events[-1]["type"] == PROVIDER_AUTH_COMPLETED_EVENT
    assert state.event_bus.events[-1]["payload"] == {
        "provider_id": "github-copilot",
        "connection_id": "github-copilot:oauth",
        "account": "work",
        "success": False,
    }


@pytest.mark.asyncio
async def test_provider_connect_start_emits_no_resource_changed_until_login(
    tmp_path: Any,
) -> None:
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

    # Connect-start only begins the device flow — the connection is not yet
    # established, so no reload signal until the login completes.
    assert response["ok"] is True
    assert [e for e in state.event_bus.events if e["type"] == RESOURCE_CHANGED_EVENT] == []


@pytest.mark.asyncio
async def test_provider_connect_completion_publishes_resource_changed_on_success(
    tmp_path: Any,
) -> None:
    state = make_state(tmp_path, make_provider(connection=make_oauth_connection()))
    engine = StubDeviceFlowEngine()
    state.device_flow_engine = engine

    await dispatch_rpc(
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
    on_complete = engine.completions[0]

    await on_complete(success=True)

    # A successful login newly enables the provider's models → reload signal,
    # emitted alongside (not replacing) the targeted auth event.
    assert [
        e["payload"] for e in state.event_bus.events if e["type"] == RESOURCE_CHANGED_EVENT
    ] == [{"kind": "providers"}]
    assert any(e["type"] == PROVIDER_AUTH_COMPLETED_EVENT for e in state.event_bus.events)


@pytest.mark.asyncio
async def test_provider_connect_completion_skips_resource_changed_on_failure(
    tmp_path: Any,
) -> None:
    state = make_state(tmp_path, make_provider(connection=make_oauth_connection()))
    engine = StubDeviceFlowEngine()
    state.device_flow_engine = engine

    await dispatch_rpc(
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
    on_complete = engine.completions[0]

    await on_complete(success=False)

    # A failed login changes nothing about availability — only the targeted
    # auth event fires so the modal can surface the failure.
    assert [e for e in state.event_bus.events if e["type"] == RESOURCE_CHANGED_EVENT] == []
    assert state.event_bus.events[-1]["type"] == PROVIDER_AUTH_COMPLETED_EVENT


@pytest.mark.asyncio
async def test_provider_disconnect_publishes_providers_resource_changed(
    tmp_path: Any,
) -> None:
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

    # Dropping a connection changes which models are selectable → reload signal.
    assert response["ok"] is True
    assert [
        e["payload"] for e in state.event_bus.events if e["type"] == RESOURCE_CHANGED_EVENT
    ] == [{"kind": "providers"}]


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
            "account": "default",
            "status": "disconnected",
        },
    }
    assert state.runtime.token_store.load("github-copilot", "oauth") is None
    assert engine.cancelled == [("github-copilot", "oauth", "default")]


@pytest.mark.asyncio
async def test_provider_disconnect_with_account_deletes_only_that_account_token(
    tmp_path: Any,
) -> None:
    state = make_state(tmp_path, make_provider(connection=make_oauth_connection()))
    engine = StubDeviceFlowEngine()
    state.device_flow_engine = engine
    state.runtime.token_store.save(
        "github-copilot",
        "oauth",
        OAuthToken(access_token="default-token"),
    )
    state.runtime.token_store.save(
        "github-copilot",
        "oauth",
        OAuthToken(access_token="work-token"),
        account_id="work",
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.disconnect",
            "params": {
                "provider_id": "github-copilot",
                "connection_id": "github-copilot:oauth",
                "account": "work",
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "provider_id": "github-copilot",
            "connection_id": "github-copilot:oauth",
            "account": "work",
            "status": "disconnected",
        },
    }
    assert state.runtime.token_store.load("github-copilot", "oauth", account_id="work") is None
    assert state.runtime.token_store.load("github-copilot", "oauth") is not None
    assert engine.cancelled == [("github-copilot", "oauth", "work")]


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
    engine.active.add(("github-copilot", "oauth", "default"))

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

    assert response == {
        "ok": True,
        "result": {
            "provider_id": "github-copilot",
            "connection_id": "github-copilot:oauth",
            "account": "default",
            "connected": True,
            "flow_active": True,
        },
    }


@pytest.mark.asyncio
async def test_provider_connection_status_reports_per_account_state(tmp_path: Any) -> None:
    state = make_state(tmp_path, make_provider(connection=make_oauth_connection()))
    engine = StubDeviceFlowEngine()
    state.device_flow_engine = engine
    state.runtime.token_store.save(
        "github-copilot",
        "oauth",
        OAuthToken(access_token="work-token"),
        account_id="work",
    )
    engine.active.add(("github-copilot", "oauth", "work"))

    work_response = await dispatch_rpc(
        state,
        {
            "method": "provider.connection_status",
            "params": {
                "provider_id": "github-copilot",
                "connection_id": "github-copilot:oauth",
                "account": "work",
            },
        },
    )
    default_response = await dispatch_rpc(
        state,
        {
            "method": "provider.connection_status",
            "params": {
                "provider_id": "github-copilot",
                "connection_id": "github-copilot:oauth",
            },
        },
    )

    assert work_response == {
        "ok": True,
        "result": {
            "provider_id": "github-copilot",
            "connection_id": "github-copilot:oauth",
            "account": "work",
            "connected": True,
            "flow_active": True,
        },
    }
    assert default_response == {
        "ok": True,
        "result": {
            "provider_id": "github-copilot",
            "connection_id": "github-copilot:oauth",
            "account": "default",
            "connected": False,
            "flow_active": False,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("account_id", ["default", "work"])
async def test_model_refresh_db_uses_oauth_token_getter_for_fresh_token(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    account_id: str,
) -> None:
    provider = make_refreshable_oauth_provider()
    state = make_state(tmp_path, provider)
    state.runtime.token_store.save(
        "github-copilot",
        "oauth",
        OAuthToken(access_token="stale-token", extra={"github_oauth_token": "github-secret"}),
        account_id=account_id,
    )
    refreshed: dict[str, Any] = {}
    state.runtime.provider_credentials = ProviderCredentialResolver(
        ProviderRegistry({provider.id: provider}),
        process_env={},
        token_store=state.runtime.token_store,
    )

    def reject_static_credential(*_args: Any) -> str:
        raise AssertionError("OAuth discovery must use the live getter")

    monkeypatch.setattr(
        state.runtime.provider_credentials, "get_credentials", reject_static_credential
    )

    def token_extra(connection: ConnectionRef) -> dict[str, str]:
        assert connection == ConnectionRef("github-copilot", "github-copilot:oauth")
        assert "getter_args" in refreshed
        return {"copilot_api_endpoint": "https://api.enterprise.githubcopilot.com"}

    state.runtime.get_connection_token_extra = token_extra

    class StubOAuthTokenGetter:
        def __init__(
            self,
            token_store: Any,
            provider_id: str,
            connection_id: str,
            config: Any,
            *,
            account_id: str,
        ) -> None:
            self.args = (token_store, provider_id, connection_id, config)
            refreshed["account_id"] = account_id

        async def __aenter__(self) -> StubOAuthTokenGetter:
            refreshed["entered"] = True
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            refreshed["closed"] = True

        async def __call__(self) -> str:
            refreshed["getter_args"] = self.args
            return "fresh-runtime-token"

    async def fake_refresh_models(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        assert not refreshed.get("closed")
        assert isinstance(_args[1], StubOAuthTokenGetter)
        refreshed["credential"] = await _args[1]()
        refreshed["connection"] = _kwargs["credential_connection"]
        return {
            "provider_id": "github-copilot",
            "model_count": 0,
            "fetched_at": "2026-05-12T00:00:00+00:00",
        }

    monkeypatch.setattr("server.rpc.provider_access.OAuthTokenGetter", StubOAuthTokenGetter)
    monkeypatch.setattr("server.rpc.connection_methods.refresh_models", fake_refresh_models)
    monkeypatch.setattr(connection_methods, "fetch_catalog", no_models_dev_catalog)

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
    assert refreshed["account_id"] == account_id
    assert refreshed["connection"].base_url == "https://api.enterprise.githubcopilot.com"


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["global", "provider"])
@pytest.mark.parametrize("error_kind", ["auth", "provider", "network"])
async def test_model_refresh_continues_after_oauth_credential_failure(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    error_kind: str,
) -> None:
    provider = make_refreshable_oauth_provider()
    provider = ProviderConfig(
        id=provider.id,
        name=provider.name,
        adapter=provider.adapter,
        base_url=provider.base_url,
        models_endpoint=provider.models_endpoint,
        connections=[*provider.connections, make_api_key_connection()],
    )
    state = make_state(tmp_path, provider)
    resources_dir = state.runtime._resolve_resources_path()
    models_dir = resources_dir / "models"
    models_dir.mkdir(parents=True)
    old_model = {
        "name": "Old OAuth Model",
        "connections": ["oauth"],
        "capabilities": {"reasoning": {"supported": False}},
    }
    (models_dir / "github-copilot.json").write_text(
        json.dumps({"provider_id": provider.id, "models": {"old-model": old_model}}),
        encoding="utf-8",
    )
    error = {
        "auth": ProviderAuthError("test-auth-failure"),
        "provider": ProviderError("test-provider-failure", retryable=True),
        "network": NetworkError("test-network-failure"),
    }[error_kind]

    async def failing_getter(_self: Any) -> str:
        raise error

    monkeypatch.setattr("core.providers.token_getter.OAuthTokenGetter.__call__", failing_getter)
    monkeypatch.setattr(connection_methods, "fetch_catalog", no_models_dev_catalog)
    catalog_route = respx.get("https://api.githubcopilot.com/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "fresh-model"}]}),
    )
    params = {} if scope == "global" else {"provider_id": provider.id}

    response = await dispatch_rpc(state, {"method": "model.refresh_db", "params": params})

    assert response["ok"] is True, response
    result = response["result"]
    assert result["model_count"] == 2
    assert result["errors"] == [
        {"provider_id": provider.id, "connection_id": "github-copilot:oauth", "error": str(error)}
    ]
    assert catalog_route.call_count == 1
    assert catalog_route.calls[0].request.headers["Authorization"] == "Bearer api-key-secret"
    written = json.loads(
        (state.runtime.storage.layout.models / "github-copilot.json").read_text(encoding="utf-8")
    )
    assert written["models"]["old-model"] == old_model
    assert written["models"]["fresh-model"]["connections"] == ["api-key"]


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

    monkeypatch.setattr("server.rpc.connection_methods.refresh_models", fake_refresh_models)
    monkeypatch.setattr(connection_methods, "fetch_catalog", no_models_dev_catalog)

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
