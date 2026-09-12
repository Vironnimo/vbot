"""Shared fixtures and fakes for usage behavior tests.

Parsing tests use synthetic provider bodies; service tests use a fake runtime
and a fake transport so nothing touches the live network.
"""

from __future__ import annotations

from typing import Any

from core.providers.accounts import ConnectionRef
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig


# Fakes
class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload

    @property
    def text(self) -> str:
        return ""


class FakeTransport:
    """Returns a fixed response and counts calls."""

    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(
        self, url: str, *, headers: Any, timeout: float, params: Any = None
    ) -> FakeResponse:
        self.calls.append((url, dict(headers)))
        return self._response


class FakeCredentials:
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


class FakeProviders:
    def __init__(self, configs: dict[str, ProviderConfig]) -> None:
        self._configs = configs

    def get(self, provider_id: str) -> ProviderConfig:
        return self._configs[provider_id]


class FakeRuntime:
    def __init__(
        self,
        *,
        providers: FakeProviders,
        credentials: FakeCredentials,
        tokens: dict[str, str] | None = None,
        extras: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._providers = providers
        self._credentials = credentials
        self._tokens = tokens or {}
        self._extras = extras or {}

    @property
    def providers(self) -> FakeProviders:
        return self._providers

    @property
    def provider_credentials(self) -> FakeCredentials:
        return self._credentials

    def get_connection_token_getter(self, connection: ConnectionRef) -> Any:
        token = self._tokens.get(connection.connection_id, "access-token")

        async def _getter() -> str:
            return token

        return _getter

    def get_connection_token_extra(self, connection: ConnectionRef) -> dict[str, str]:
        return self._extras.get(
            connection.connection_id,
            self._extras.get(connection.connection_id.removesuffix(":default"), {}),
        )


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


def _openai_runtime(*, usable: bool = True) -> FakeRuntime:
    providers = FakeProviders({"openai": _openai_provider_config()})
    usable_set = {"openai:subscription"} if usable else set()
    return FakeRuntime(
        providers=providers,
        credentials=FakeCredentials(usable_set),
        extras={"openai:subscription": {"chatgpt_account_id": "acct-123"}},
    )


_OPENAI_BODY: dict[str, Any] = {
    "plan_type": "Plus",
    "credits": {"balance": 0},
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

_OLLAMA_BODY: dict[str, Any] = {
    "activity": {
        "cost": "0.00000",
        "models": [],
        "period": {"type": "last_4_weeks"},
    },
    "limits": {
        "session": {
            "models": [
                {"name": "gemma4:31b", "request_count": 6},
                {"name": "minimax-m3", "request_count": 3},
            ],
            "usage": 0.019,
        },
        "weekly": {
            "models": [
                {"name": "gemma4:31b", "request_count": 9},
                {"name": "minimax-m3", "request_count": 5},
            ],
            "usage": 0.007,
        },
    },
}

# MiniMax parsing (openclaw-shaped fixtures)
_MINIMAX_BODY: dict[str, Any] = {
    "plan": "Token Plan",
    "model_remains": [
        {"model_name": "MiniMax-Text-01", "current_interval_total_count": 0},
        {
            "model_name": "MiniMax-M2",
            "current_interval_total_count": 1000,
            "current_interval_remain_count": 250,
            "current_interval_minutes": 1440,
            "current_interval_end": 1_750_600_000,
        },
    ],
}

# OpenRouter parsing and fetch (credits + key spending cap)
_OPENROUTER_CREDITS_BODY: dict[str, Any] = {"data": {"total_credits": 50.0, "total_usage": 12.5}}

_OPENROUTER_KEY_BODY: dict[str, Any] = {
    "data": {
        "limit": 100.0,
        "limit_remaining": 25.0,
        "limit_reset": "2026-08-20T00:00:00Z",
        "usage": 12.5,
        "usage_daily": 1.5,
        "usage_weekly": 8.0,
        "usage_monthly": 12.5,
    }
}
