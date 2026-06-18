"""Tests for the ``provider.usage`` RPC handler.

Coverage:
- returns the report/snapshot shape from a seeded service,
- rejects unknown params and a malformed ``connections`` filter,
- forwards the optional ``connections`` filter to the service,
- lazily builds and caches the service on RPC state,
- the handler is registered in the method table.

A fake transport keeps every test off the live network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.usage import ProviderUsageService
from server.rpc.errors import RpcError
from server.rpc.methods import build_method_handlers
from server.rpc.provider_usage_methods import _provider_usage

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self) -> Any:
        return self._payload

    @property
    def text(self) -> str:
        return ""


class _FakeTransport:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def get(
        self, url: str, *, headers: Any, timeout: float, params: Any = None
    ) -> _FakeResponse:
        return _FakeResponse(self._payload)


class _FakeCredentials:
    def __init__(self, usable: set[str]) -> None:
        self._usable = usable

    def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool:
        return connection_id in self._usable


class _FakeProviders:
    def __init__(self, configs: dict[str, ProviderConfig]) -> None:
        self._configs = configs

    def get(self, provider_id: str) -> ProviderConfig:
        return self._configs[provider_id]


class _FakeRuntime:
    def __init__(
        self, *, usable: set[str], extras: dict[str, dict[str, str]] | None = None
    ) -> None:
        self._providers = _FakeProviders({"openai": _openai_provider_config()})
        self._credentials = _FakeCredentials(usable)
        self._extras = extras or {}

    @property
    def providers(self) -> _FakeProviders:
        return self._providers

    @property
    def provider_credentials(self) -> _FakeCredentials:
        return self._credentials

    def get_connection_token_getter(self, provider_id: str, connection_id: str) -> Any:
        async def _getter() -> str:
            return "access-token"

        return _getter

    def get_connection_token_extra(self, provider_id: str, connection_id: str) -> dict[str, str]:
        return self._extras.get(connection_id, {})


class _CapturingService:
    def __init__(self) -> None:
        self.connections: Any = "unset"

    async def report(self, connections: list[str] | None = None) -> Any:
        self.connections = connections
        return SimpleNamespace(to_dict=lambda: {"generated_at": "t", "providers": []})


def _openai_provider_config() -> ProviderConfig:
    return ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai",
        base_url="https://api.openai.com/v1",
        connections=[
            ConnectionConfig(
                id="subscription",
                type="oauth",
                label="ChatGPT Plus/Pro",
                auth=AuthConfig(header="Authorization", prefix="Bearer "),
                base_url="https://chatgpt.com/backend-api",
                mode="codex_responses",
            )
        ],
    )


_OPENAI_BODY: dict[str, Any] = {
    "plan_type": "Plus",
    "rate_limit": {
        "primary_window": {
            "used_percent": 42.5,
            "limit_window_seconds": 18000,
            "reset_at": 1_750_000_000,
        },
        "secondary_window": {
            "used_percent": 12.0,
            "limit_window_seconds": 604_800,
            "reset_at": 1_750_600_000,
        },
    },
}


def _openai_state() -> SimpleNamespace:
    runtime = _FakeRuntime(
        usable={"openai:subscription"},
        extras={"openai:subscription": {"chatgpt_account_id": "acct-123"}},
    )
    service = ProviderUsageService(runtime, transport=_FakeTransport(_OPENAI_BODY))
    return SimpleNamespace(runtime=runtime, usage_service=service)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_usage_returns_report_shape() -> None:
    state = _openai_state()

    result = await _provider_usage(state, {})

    assert set(result) == {"generated_at", "providers"}
    assert len(result["providers"]) == 1
    snapshot = result["providers"][0]
    assert set(snapshot) == {"connection", "display_name", "plan", "windows", "error"}
    assert snapshot["connection"] == "openai:subscription"
    assert [window["label"] for window in snapshot["windows"]] == ["5h", "Week"]


@pytest.mark.asyncio
async def test_provider_usage_rejects_unknown_fields() -> None:
    with pytest.raises(RpcError, match="unsupported provider.usage fields: bogus"):
        await _provider_usage(SimpleNamespace(), {"bogus": 1})


@pytest.mark.asyncio
async def test_provider_usage_rejects_malformed_connections_filter() -> None:
    with pytest.raises(RpcError, match="params.connections must be a list"):
        await _provider_usage(SimpleNamespace(), {"connections": "openai:subscription"})


@pytest.mark.asyncio
async def test_provider_usage_forwards_connections_filter() -> None:
    service = _CapturingService()
    state = SimpleNamespace(usage_service=service)

    await _provider_usage(state, {"connections": ["openai:subscription"]})

    assert service.connections == ["openai:subscription"]


@pytest.mark.asyncio
async def test_provider_usage_lazily_caches_service_on_state() -> None:
    # No usable connections → no fetch, no network, empty report.
    state = SimpleNamespace(runtime=_FakeRuntime(usable=set()))

    result = await _provider_usage(state, {})
    cached = state.usage_service
    await _provider_usage(state, {})

    assert state.usage_service is cached
    assert result["providers"] == []


def test_provider_usage_is_registered() -> None:
    handlers = build_method_handlers()

    assert "provider.usage" in handlers
