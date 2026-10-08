"""Offline fakes, Provider configs and upstream bodies for Provider usage tests.

Service tests drive ``ProviderUsageService`` through a fake Runtime and a fake
transport, so nothing touches the live network.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.providers.accounts import ConnectionRef
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig


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
    """Answers by URL substring (``""`` matches every URL) and records each call."""

    def __init__(self, responses: FakeResponse | Mapping[str, FakeResponse]) -> None:
        self._responses = (
            {"": responses} if isinstance(responses, FakeResponse) else dict(responses)
        )
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(
        self, url: str, *, headers: Any, timeout: float, params: Any = None
    ) -> FakeResponse:
        self.calls.append((url, dict(headers)))
        for marker, response in self._responses.items():
            if marker in url:
                return response
        raise RuntimeError(f"no fake response for {url}")


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


def _api_key_connection(credential_key: str, *, mode: str | None = None) -> ConnectionConfig:
    return ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API key",
        auth=AuthConfig(header="Authorization", prefix="Bearer ", credential_key=credential_key),
        mode=mode,
    )


PROVIDER_CONFIGS: dict[str, ProviderConfig] = {
    "openai": ProviderConfig(
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
    ),
    "github-copilot": ProviderConfig(
        id="github-copilot",
        name="GitHub Copilot",
        adapter="github_copilot",
        base_url="https://api.githubcopilot.com",
        connections=[
            ConnectionConfig(
                id="oauth",
                type="oauth",
                label="Sign in with GitHub",
                auth=AuthConfig(header="Authorization", prefix="Bearer "),
            )
        ],
    ),
    "ollama-cloud": ProviderConfig(
        id="ollama-cloud",
        name="Ollama Cloud",
        adapter="ollama",
        base_url="https://ollama.com",
        connections=[_api_key_connection("OLLAMA_API_KEY", mode="cloud")],
    ),
    "minimax": ProviderConfig(
        id="minimax",
        name="MiniMax",
        adapter="minimax",
        base_url="https://api.minimaxi.com/v1",
        connections=[_api_key_connection("MINIMAX_API_KEY")],
    ),
    "openrouter": ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        adapter="openrouter",
        base_url="https://openrouter.ai/api/v1",
        connections=[_api_key_connection("OPENROUTER_API_KEY")],
    ),
    "opencode-go": ProviderConfig(
        id="opencode-go",
        name="OpenCode Go",
        adapter="opencode_go",
        base_url="https://opencode.ai/zen/go/v1",
        connections=[_api_key_connection("OPENCODE_API_KEY")],
        extra_headers={"User-Agent": "vBot"},
    ),
}

USAGE_CONNECTIONS = {
    "openai": "openai:subscription",
    "github-copilot": "github-copilot:oauth",
    "ollama-cloud": "ollama-cloud:api-key",
    "minimax": "minimax:api-key",
    "openrouter": "openrouter:api-key",
    "opencode-go": "opencode-go:api-key",
}


def usage_runtime(*provider_ids: str, usable: bool = True) -> FakeRuntime:
    """A Runtime whose named Providers each have one logged-in usage Connection."""

    return FakeRuntime(
        providers=FakeProviders(
            {provider_id: PROVIDER_CONFIGS[provider_id] for provider_id in provider_ids}
        ),
        credentials=FakeCredentials(
            {USAGE_CONNECTIONS[provider_id] for provider_id in provider_ids} if usable else set()
        ),
        tokens={
            "ollama-cloud:api-key:default": "ollama-secret",
            "openrouter:api-key:default": "or-secret",
            "opencode-go:api-key:default": "go-secret",
        },
        extras={
            "openai:subscription": {"chatgpt_account_id": "acct-123"},
            "github-copilot:oauth": {"github_oauth_token": "gho_example"},
        },
    )


OPENAI_BODY: dict[str, Any] = {
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

# GitHub Copilot and MiniMax bodies are openclaw-shaped (inferred upstream shapes).
COPILOT_BODY: dict[str, Any] = {
    "copilot_plan": "individual",
    "quota_reset_date": "2026-07-01",
    "quota_snapshots": {
        "premium_interactions": {
            "percent_remaining": 75.0,
            "remaining": 225,
            "entitlement": 300,
            "unlimited": False,
        },
        "chat": {"unlimited": True, "percent_remaining": 100.0},
        "completions": {"unlimited": True},
    },
}

# Live-verified Ollama Cloud ``/api/balance`` shape of the older session/weekly plans.
OLLAMA_BODY: dict[str, Any] = {
    "included": {
        "session": {"remaining_percent": 99.47, "resets_at": "2026-10-08T14:00:00Z"},
        "weekly": {"remaining_percent": 94.38, "resets_at": "2026-10-12T00:00:00Z"},
    },
    "purchased": {"balance_usd": 0},
}

# Live-verified OpenCode Go ``/usage`` shape (undocumented endpoint).
OPENCODE_GO_BODY: dict[str, Any] = {
    "usage": {
        "rolling": {"status": "ok", "percent": 0, "resetsAt": "2026-10-08T13:30:41.000Z"},
        "weekly": {"status": "ok", "percent": 1, "resetsAt": "2026-10-12T00:00:00.000Z"},
        "monthly": {"status": "ok", "percent": 41, "resetsAt": "2026-10-30T17:56:11.000Z"},
    }
}

MINIMAX_BODY: dict[str, Any] = {
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

# OpenRouter credits plus the API-key spending cap.
OPENROUTER_CREDITS_BODY: dict[str, Any] = {"data": {"total_credits": 50.0, "total_usage": 12.5}}

OPENROUTER_KEY_BODY: dict[str, Any] = {
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

# One successful body per supported Connection, keyed by URL substring.
USAGE_RESPONSES: dict[str, FakeResponse] = {
    "wham/usage": FakeResponse(payload=OPENAI_BODY),
    "copilot_internal/user": FakeResponse(payload=COPILOT_BODY),
    "api/balance": FakeResponse(payload=OLLAMA_BODY),
    "go/v1/usage": FakeResponse(payload=OPENCODE_GO_BODY),
    "token_plan/remains": FakeResponse(payload=MINIMAX_BODY),
    "/credits": FakeResponse(payload=OPENROUTER_CREDITS_BODY),
    "/key": FakeResponse(payload=OPENROUTER_KEY_BODY),
}
