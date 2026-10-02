"""``provider.usage``, ``provider.usage_history`` and ``.clear`` RPCs.

A fake transport keeps every test off the live network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from core.database import write_bootstrap_marker
from core.providers.accounts import ConnectionRef
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.usage import ProviderUsageService
from tests.server.rpc_test_support import JsonObject, rpc_error, rpc_result

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

    def get_connection_token_getter(self, connection: ConnectionRef) -> Any:
        async def _getter() -> str:
            return "access-token"

        return _getter

    def get_connection_token_extra(self, connection: ConnectionRef) -> dict[str, str]:
        return self._extras.get(
            connection.connection_id,
            self._extras.get(connection.connection_id.removesuffix(":default"), {}),
        )


class _RecordingService:
    """Records every usage request the RPCs forward to the service."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def report(self, connections: list[str] | None = None) -> Any:
        self.calls.append(("report", connections))
        return SimpleNamespace(to_dict=lambda: {"generated_at": "t", "providers": []})

    async def history_report(self, **window: Any) -> Any:
        self.calls.append(("history_report", window))
        return SimpleNamespace(to_dict=lambda: {"generated_at": "t", "samples": []})


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


@pytest_asyncio.fixture
async def history_state(tmp_path: Path) -> AsyncIterator[SimpleNamespace]:
    write_bootstrap_marker(tmp_path)
    state = _openai_state()
    state.usage_service = ProviderUsageService(
        state.runtime,
        transport=_FakeTransport(_OPENAI_BODY),
        data_root=tmp_path,
    )
    try:
        yield state
    finally:
        await state.usage_service.aclose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_usage_returns_report_shape() -> None:
    state = _openai_state()

    result = await rpc_result(state, "provider.usage")

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
async def test_provider_usage_forwards_connections_filter() -> None:
    service = _RecordingService()
    state = SimpleNamespace(usage_service=service)

    await rpc_result(state, "provider.usage", connections=["openai:subscription"])

    assert service.calls == [("report", ["openai:subscription"])]


@pytest.mark.asyncio
async def test_provider_usage_uses_runtime_owned_service() -> None:
    runtime = _FakeRuntime(usable=set())
    runtime.provider_usage = ProviderUsageService(runtime)
    state = SimpleNamespace(runtime=runtime)

    result = await rpc_result(state, "provider.usage")
    await rpc_result(state, "provider.usage")

    assert not hasattr(state, "usage_service")
    assert result["providers"] == []


@pytest.mark.asyncio
async def test_provider_usage_history_returns_automatic_samples_within_the_window(
    history_state: SimpleNamespace,
) -> None:
    # A live report is not an automatic sample; only the collected one is history.
    await rpc_result(history_state, "provider.usage")
    await history_state.usage_service.collect_history_sample()

    result = await rpc_result(
        history_state,
        "provider.usage_history",
        since="2020-01-01T00:00:00Z",
        until="2099-01-01T00:00:00Z",
    )
    before = await rpc_result(history_state, "provider.usage_history", until="2020-01-01T00:00:00Z")

    assert set(result) == {"generated_at", "samples"}
    assert len(result["samples"]) == 1
    assert set(result["samples"][0]) == {"sampled_at", "providers"}
    assert result["samples"][0]["providers"][0]["account"] == "default"
    assert before["samples"] == []


@pytest.mark.asyncio
async def test_provider_usage_history_clear_is_explicit(history_state: SimpleNamespace) -> None:
    await history_state.usage_service.collect_history_sample()

    result = await rpc_result(history_state, "provider.usage_history.clear")
    repeated = await rpc_result(history_state, "provider.usage_history.clear")

    assert result == {"deleted_samples": 1}
    assert repeated == {"deleted_samples": 0}
    assert (await rpc_result(history_state, "provider.usage_history"))["samples"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "named"),
    [
        ("provider.usage", {"bogus": 1}, "bogus"),
        (
            "provider.usage",
            {"connections": "openai:subscription"},
            "params.connections must be a list of connection id strings",
        ),
        (
            "provider.usage_history",
            {"since": "2026-08-02T00:00:00Z", "until": "2026-08-01T00:00:00Z"},
            "params.since must not be after params.until",
        ),
        (
            "provider.usage_history",
            {"since": "yesterday"},
            "params.since must be an ISO 8601 timestamp string",
        ),
        (
            "provider.usage_history",
            {"since": "2026-08-01Z"},
            "params.since must be an ISO 8601 timestamp string",
        ),
    ],
)
async def test_malformed_provider_usage_requests_reach_no_service(
    method: str, params: JsonObject, named: str
) -> None:
    service = _RecordingService()
    state = SimpleNamespace(usage_service=service)

    error = await rpc_error(state, method, **params)

    assert error["code"] == "invalid_request"
    assert named in error["message"]
    assert service.calls == []
