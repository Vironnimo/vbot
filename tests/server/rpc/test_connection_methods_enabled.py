"""Tests for the ``connection.set_enabled`` RPC handler."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from server.events import RESOURCE_CHANGED_EVENT, ServerEventBus
from server.rpc.methods import dispatch_rpc


class StubProviderRegistry:
    def __init__(self, provider: ProviderConfig) -> None:
        self._provider = provider

    def get(self, provider_id: str) -> ProviderConfig:
        if provider_id != self._provider.id:
            raise KeyError(provider_id)
        return self._provider

    def list_ids(self) -> list[str]:
        return [self._provider.id]


class StubStorage:
    def __init__(self) -> None:
        self.enabled_writes: list[tuple[str, bool]] = []

    def set_provider_connection_enabled(self, connection_key: str, enabled: bool) -> None:
        self.enabled_writes.append((connection_key, enabled))


class StubCredentials:
    def __init__(self, configured: set[str]) -> None:
        self._configured = configured

    def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool:
        return connection_id in self._configured


def make_ollama_provider() -> ProviderConfig:
    return ProviderConfig(
        id="ollama",
        name="Ollama",
        adapter="ollama",
        base_url="http://localhost:11434",
        models_endpoint="/api/tags",
        connections=[
            ConnectionConfig(
                id="local",
                type="none",
                label="Local",
                auth=AuthConfig(header="", prefix="", credential_key=""),
                auto_refresh=True,
            ),
            ConnectionConfig(
                id="cloud",
                type="api_key",
                label="Ollama Cloud",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OLLAMA_API_KEY",
                ),
            ),
        ],
    )


def make_state(*, reachable: bool | None = None) -> SimpleNamespace:
    storage = StubStorage()
    refresh_calls: list[dict[str, Any]] = []

    async def maybe_refresh_local_catalogs(*, force: bool = False) -> None:
        refresh_calls.append({"force": force})

    runtime = SimpleNamespace(
        providers=StubProviderRegistry(make_ollama_provider()),
        provider_credentials=StubCredentials({"ollama:local"}),
        storage=storage,
        maybe_refresh_local_catalogs=maybe_refresh_local_catalogs,
        connection_reachability=lambda connection_id: reachable,
    )
    state = SimpleNamespace(runtime=runtime, event_bus=ServerEventBus())
    state.refresh_calls = refresh_calls
    return state


@pytest.mark.asyncio
async def test_enable_local_connection_probes_and_reports_reachability() -> None:
    """Enabling an auto-refresh connection forces a probe and reports the outcome."""
    # Arrange
    state = make_state(reachable=True)

    # Act
    response = await dispatch_rpc(
        state,
        {
            "method": "connection.set_enabled",
            "params": {
                "provider_id": "ollama",
                "connection_id": "ollama:local",
                "enabled": True,
            },
        },
    )

    # Assert
    assert response == {
        "ok": True,
        "result": {
            "provider_id": "ollama",
            "connection_id": "ollama:local",
            "enabled": True,
            "configured": True,
            "reachable": True,
        },
    }
    assert state.runtime.storage.enabled_writes == [("ollama:local", True)]
    assert state.refresh_calls == [{"force": True}]


@pytest.mark.asyncio
async def test_enable_sticks_when_endpoint_unreachable() -> None:
    """'Enabled, but not running' is a valid outcome — the enable persists."""
    # Arrange
    state = make_state(reachable=False)

    # Act
    response = await dispatch_rpc(
        state,
        {
            "method": "connection.set_enabled",
            "params": {
                "provider_id": "ollama",
                "connection_id": "ollama:local",
                "enabled": True,
            },
        },
    )

    # Assert
    assert response["ok"] is True
    assert response["result"]["enabled"] is True
    assert response["result"]["reachable"] is False
    assert state.runtime.storage.enabled_writes == [("ollama:local", True)]


@pytest.mark.asyncio
async def test_disable_never_probes() -> None:
    # Arrange
    state = make_state()

    # Act
    response = await dispatch_rpc(
        state,
        {
            "method": "connection.set_enabled",
            "params": {
                "provider_id": "ollama",
                "connection_id": "ollama:local",
                "enabled": False,
            },
        },
    )

    # Assert
    assert response["ok"] is True
    assert response["result"]["enabled"] is False
    assert state.refresh_calls == []
    assert state.runtime.storage.enabled_writes == [("ollama:local", False)]


@pytest.mark.asyncio
async def test_non_auto_refresh_connection_omits_reachability() -> None:
    # Arrange
    state = make_state()

    # Act
    response = await dispatch_rpc(
        state,
        {
            "method": "connection.set_enabled",
            "params": {
                "provider_id": "ollama",
                "connection_id": "ollama:cloud",
                "enabled": False,
            },
        },
    )

    # Assert
    assert response["ok"] is True
    assert "reachable" not in response["result"]
    assert state.refresh_calls == []


@pytest.mark.asyncio
async def test_rejects_non_boolean_enabled() -> None:
    # Arrange
    state = make_state()

    # Act
    response = await dispatch_rpc(
        state,
        {
            "method": "connection.set_enabled",
            "params": {
                "provider_id": "ollama",
                "connection_id": "ollama:local",
                "enabled": "yes",
            },
        },
    )

    # Assert
    assert response["ok"] is False
    assert "boolean" in response["error"]["message"]


@pytest.mark.asyncio
async def test_rejects_account_scoped_connection_id() -> None:
    """Enablement is connection-level; an account suffix is an invalid target."""
    # Arrange
    state = make_state()

    # Act
    response = await dispatch_rpc(
        state,
        {
            "method": "connection.set_enabled",
            "params": {
                "provider_id": "ollama",
                "connection_id": "ollama:cloud:work",
                "enabled": True,
            },
        },
    )

    # Assert
    assert response["ok"] is False
    assert "account" in response["error"]["message"]


@pytest.mark.asyncio
async def test_publishes_provider_resource_changed() -> None:
    # Arrange
    state = make_state()

    # Act
    await dispatch_rpc(
        state,
        {
            "method": "connection.set_enabled",
            "params": {
                "provider_id": "ollama",
                "connection_id": "ollama:local",
                "enabled": True,
            },
        },
    )

    # Assert
    assert [
        event["payload"]
        for event in state.event_bus.events
        if event["type"] == RESOURCE_CHANGED_EVENT
    ] == [{"kind": "providers"}]
