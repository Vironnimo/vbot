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

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.usage import ProviderUsageService
from server.rpc.errors import RpcError
from server.rpc.methods import build_method_handlers
from server.rpc.provider_usage_methods import (
    _provider_usage,
    _provider_usage_history,
    _provider_usage_history_clear,
)

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

    def is_usable(self, provider_id: str, connection_id: str | None = None) -> bool:
        return self.has_credentials(provider_id, connection_id)

    def resolve_account_id(
        self,
        provider_id: str,
        local_connection_id: str,
        account_id: str | None = None,
    ) -> str:
        connection_id = f"{provider_id}:{local_connection_id}"
        if connection_id not in self._usable:
            raise KeyError(connection_id)
        return account_id or "default"


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
        self.provider_usage: ProviderUsageService | None = None

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
        return self._extras.get(
            connection_id, self._extras.get(connection_id.removesuffix(":default"), {})
        )


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


def _openai_history_state(tmp_path: Path) -> SimpleNamespace:
    state = _openai_state()
    state.usage_service = ProviderUsageService(
        state.runtime,
        transport=_FakeTransport(_OPENAI_BODY),
        data_root=tmp_path,
    )
    return state


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
    assert set(snapshot) == {
        "connection",
        "account",
        "display_name",
        "plan",
        "windows",
        "credits",
        "error",
    }
    assert snapshot["connection"] == "openai:subscription"
    assert snapshot["account"] == "default"
    assert [window["label"] for window in snapshot["windows"]] == ["5h", "Week"]


@pytest.mark.asyncio
async def test_provider_usage_rejects_unknown_fields() -> None:
    with pytest.raises(RpcError) as exc_info:
        await _provider_usage(SimpleNamespace(), {"bogus": 1})
    assert exc_info.value.code == "invalid_request"
    assert "bogus" in exc_info.value.message


@pytest.mark.asyncio
async def test_provider_usage_rejects_malformed_connections_filter() -> None:
    with pytest.raises(RpcError) as exc_info:
        await _provider_usage(SimpleNamespace(), {"connections": "openai:subscription"})
    assert exc_info.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_provider_usage_forwards_connections_filter() -> None:
    service = _CapturingService()
    state = SimpleNamespace(usage_service=service)

    await _provider_usage(state, {"connections": ["openai:subscription"]})

    assert service.connections == ["openai:subscription"]


@pytest.mark.asyncio
async def test_provider_usage_uses_runtime_owned_service() -> None:
    runtime = _FakeRuntime(usable=set())
    service = ProviderUsageService(runtime)
    runtime.provider_usage = service
    state = SimpleNamespace(runtime=runtime)

    result = await _provider_usage(state, {})
    await _provider_usage(state, {})

    assert not hasattr(state, "usage_service")
    assert result["providers"] == []


@pytest.mark.asyncio
async def test_provider_usage_history_returns_only_automatic_samples(tmp_path: Path) -> None:
    state = _openai_history_state(tmp_path)
    await state.usage_service.collect_history_sample()

    result = _provider_usage_history(
        state,
        {"since": "2020-01-01T00:00:00Z", "until": "2099-01-01T00:00:00Z"},
    )

    assert len(result["samples"]) == 1
    assert result["samples"][0]["providers"][0]["account"] == "default"


@pytest.mark.asyncio
async def test_provider_usage_history_clear_is_explicit(tmp_path: Path) -> None:
    state = _openai_history_state(tmp_path)
    await state.usage_service.collect_history_sample()

    result = _provider_usage_history_clear(state, {})

    assert result == {"deleted_samples": 1, "deleted_files": 1}
    assert _provider_usage_history(state, {})["samples"] == []


def test_provider_usage_is_registered() -> None:
    handlers = build_method_handlers()

    assert "provider.usage" in handlers
    assert "provider.usage_history" in handlers
    assert "provider.usage_history.clear" in handlers
