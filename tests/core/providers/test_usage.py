"""Tests for the live provider usage probe (``core.providers.usage``).

Parsing tests use synthetic provider bodies; service tests use a fake runtime
and a fake transport so nothing touches the live network.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.usage import (
    ProviderUsageService,
    UsageFetchError,
    UsageWindow,
    _epoch_to_iso,
    _parse_copilot_usage,
    _parse_minimax_usage,
    _parse_openai_usage,
    _secondary_window_label,
    clamp_percent,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


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


class HangingTransport:
    async def get(
        self, url: str, *, headers: Any, timeout: float, params: Any = None
    ) -> FakeResponse:
        await asyncio.sleep(10)
        return FakeResponse()  # pragma: no cover


class RaisingTransport:
    async def get(
        self, url: str, *, headers: Any, timeout: float, params: Any = None
    ) -> FakeResponse:
        raise RuntimeError("boom")


class FakeCredentials:
    def __init__(self, usable: set[str]) -> None:
        self._usable = usable

    def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool:
        return connection_id in self._usable


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

    def get_connection_token_getter(self, provider_id: str, connection_id: str) -> Any:
        token = self._tokens.get(connection_id, "access-token")

        async def _getter() -> str:
            return token

        return _getter

    def get_connection_token_extra(self, provider_id: str, connection_id: str) -> dict[str, str]:
        return self._extras.get(connection_id, {})


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


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_clamp_percent_bounds_and_rejects_non_numbers() -> None:
    assert clamp_percent(150) == 100.0
    assert clamp_percent(-5) == 0.0
    assert clamp_percent(42.5) == 42.5
    assert clamp_percent("nope") == 0.0
    assert clamp_percent(True) == 0.0


def test_epoch_to_iso_converts_seconds_and_milliseconds() -> None:
    expected = datetime.fromtimestamp(1_750_000_000, UTC).isoformat()
    assert _epoch_to_iso(1_750_000_000) == expected
    # A millisecond epoch normalizes to the same instant.
    assert _epoch_to_iso(1_750_000_000_000) == expected
    assert _epoch_to_iso(None) is None
    assert _epoch_to_iso("nope") is None


def test_secondary_window_label_cadence() -> None:
    assert _secondary_window_label(604_800) == "Week"
    assert _secondary_window_label(86_400) == "Day"
    assert _secondary_window_label(18_000) == "5h"
    assert _secondary_window_label(None) == "Weekly"


def test_parse_openai_usage_primary_and_secondary() -> None:
    # Act
    snapshot = _parse_openai_usage("openai:subscription", "OpenAI", _OPENAI_BODY)

    # Assert
    assert snapshot.connection == "openai:subscription"
    assert snapshot.display_name == "OpenAI"
    assert snapshot.plan == "Plus"
    assert snapshot.error is None
    assert snapshot.windows == [
        UsageWindow(
            label="5h",
            used_percent=42.5,
            reset_at=datetime.fromtimestamp(1_750_000_000, UTC).isoformat(),
        ),
        UsageWindow(
            label="Week",
            used_percent=12.0,
            reset_at=datetime.fromtimestamp(1_750_600_000, UTC).isoformat(),
        ),
    ]


def test_parse_openai_usage_appends_credit_balance_to_plan() -> None:
    # The live body reports balance as a string gated by `has_credits`.
    body = {**_OPENAI_BODY, "credits": {"has_credits": True, "balance": "1234"}}
    snapshot = _parse_openai_usage("openai:subscription", "OpenAI", body)
    assert snapshot.plan == "Plus · 1234 credits"


def test_parse_openai_usage_omits_zero_or_disabled_credits() -> None:
    # The real Plus account: has_credits false, balance "0" → plan only.
    body = {**_OPENAI_BODY, "credits": {"has_credits": False, "balance": "0"}}
    snapshot = _parse_openai_usage("openai:subscription", "OpenAI", body)
    assert snapshot.plan == "Plus"


def test_parse_openai_usage_clamps_out_of_range_percent() -> None:
    body = {"rate_limit": {"primary_window": {"used_percent": 150, "limit_window_seconds": 18000}}}
    snapshot = _parse_openai_usage("openai:subscription", "OpenAI", body)
    assert snapshot.windows[0].used_percent == 100.0


def test_parse_openai_usage_missing_windows_yields_no_windows() -> None:
    snapshot = _parse_openai_usage("openai:subscription", "OpenAI", {"plan_type": "Plus"})
    assert snapshot.windows == []
    assert snapshot.error is None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_returns_openai_snapshot_with_windows() -> None:
    # Arrange
    transport = FakeTransport(FakeResponse(payload=_OPENAI_BODY))
    service = ProviderUsageService(_openai_runtime(), transport=transport)

    # Act
    report = await service.report()

    # Assert
    assert len(report.providers) == 1
    snapshot = report.providers[0]
    assert snapshot.connection == "openai:subscription"
    assert [window.label for window in snapshot.windows] == ["5h", "Week"]
    # The request carries the account header + Codex beta/originator headers.
    _, headers = transport.calls[0]
    assert headers["chatgpt-account-id"] == "acct-123"
    assert headers["OpenAI-Beta"] == "responses=experimental"
    assert headers["originator"] == "vbot"


@pytest.mark.asyncio
async def test_report_skips_unusable_connections() -> None:
    transport = FakeTransport(FakeResponse(payload=_OPENAI_BODY))
    service = ProviderUsageService(_openai_runtime(usable=False), transport=transport)

    report = await service.report()

    assert report.providers == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_report_times_out_into_error_snapshot() -> None:
    service = ProviderUsageService(_openai_runtime(), transport=HangingTransport(), timeout=0.01)

    report = await service.report()

    assert len(report.providers) == 1
    assert report.providers[0].error == "Timeout"


@pytest.mark.asyncio
async def test_report_fails_open_on_http_error() -> None:
    transport = FakeTransport(FakeResponse(status_code=401))
    service = ProviderUsageService(_openai_runtime(), transport=transport)

    report = await service.report()

    assert len(report.providers) == 1
    assert report.providers[0].error == "HTTP 401"


@pytest.mark.asyncio
async def test_report_fails_open_on_unexpected_error() -> None:
    service = ProviderUsageService(_openai_runtime(), transport=RaisingTransport())

    report = await service.report()

    assert len(report.providers) == 1
    assert report.providers[0].error == "Unavailable"


@pytest.mark.asyncio
async def test_report_uses_ttl_cache_on_repeated_calls() -> None:
    transport = FakeTransport(FakeResponse(payload=_OPENAI_BODY))
    # A constant clock keeps every cache entry fresh.
    service = ProviderUsageService(_openai_runtime(), transport=transport, monotonic=lambda: 1000.0)

    first = await service.report()
    second = await service.report()

    assert len(transport.calls) == 1
    assert first.providers[0].windows == second.providers[0].windows


@pytest.mark.asyncio
async def test_report_filters_to_requested_connections() -> None:
    transport = FakeTransport(FakeResponse(payload=_OPENAI_BODY))
    service = ProviderUsageService(_openai_runtime(), transport=transport)

    report = await service.report(connections=["minimax:api-key"])

    assert report.providers == []
    assert transport.calls == []


# ---------------------------------------------------------------------------
# GitHub Copilot parsing (openclaw-shaped fixtures)
# ---------------------------------------------------------------------------


_COPILOT_BODY: dict[str, Any] = {
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


def test_parse_copilot_usage_premium_and_chat() -> None:
    # Act
    snapshot = _parse_copilot_usage("github-copilot:oauth", "GitHub Copilot", _COPILOT_BODY)

    # Assert
    assert snapshot.plan == "individual"
    assert snapshot.error is None
    assert [(window.label, window.used_percent) for window in snapshot.windows] == [
        ("Premium", 25.0),
        ("Chat", 0.0),
    ]
    # Each window carries the shared quota reset date.
    assert all(window.reset_at is not None for window in snapshot.windows)


def test_parse_copilot_usage_missing_snapshots_is_graceful() -> None:
    snapshot = _parse_copilot_usage(
        "github-copilot:oauth", "GitHub Copilot", {"copilot_plan": "business"}
    )
    assert snapshot.windows == []
    assert snapshot.plan == "business"
    assert snapshot.error is None


# ---------------------------------------------------------------------------
# MiniMax parsing (openclaw-shaped fixtures)
# ---------------------------------------------------------------------------


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


def test_parse_minimax_usage_picks_chat_model_and_derives_percent() -> None:
    # Act
    snapshot = _parse_minimax_usage("minimax:api-key", "MiniMax", _MINIMAX_BODY)

    # Assert — picks MiniMax-M2 (non-zero total), used = (1000-250)/1000 = 75%.
    assert snapshot.plan == "Token Plan"
    assert len(snapshot.windows) == 1
    window = snapshot.windows[0]
    assert window.used_percent == 75.0
    assert window.label == "24h"
    assert window.reset_at is not None


def test_parse_minimax_usage_malformed_raises_fetch_error() -> None:
    with pytest.raises(UsageFetchError, match="Unsupported response shape"):
        _parse_minimax_usage("minimax:api-key", "MiniMax", {"unexpected": True})


def test_parse_minimax_usage_no_chat_model_raises_fetch_error() -> None:
    body = {"model_remains": [{"model_name": "MiniMax-Text-01", "current_interval_total_count": 5}]}
    with pytest.raises(UsageFetchError, match="Unsupported response shape"):
        _parse_minimax_usage("minimax:api-key", "MiniMax", body)


# ---------------------------------------------------------------------------
# Service fan-out across providers
# ---------------------------------------------------------------------------


class RoutingTransport:
    """Returns a different response per URL substring; records failures."""

    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self._responses = responses

    async def get(
        self, url: str, *, headers: Any, timeout: float, params: Any = None
    ) -> FakeResponse:
        for marker, response in self._responses.items():
            if marker in url:
                return response
        raise RuntimeError(f"no fake response for {url}")


def _multi_provider_runtime() -> FakeRuntime:
    providers = FakeProviders(
        {
            "openai": _openai_provider_config(),
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
            "minimax": ProviderConfig(
                id="minimax",
                name="MiniMax",
                adapter="minimax",
                base_url="https://api.minimaxi.com/v1",
                connections=[
                    ConnectionConfig(
                        id="api-key",
                        type="api_key",
                        label="API / Token Plan Key",
                        auth=AuthConfig(
                            header="Authorization",
                            prefix="Bearer ",
                            credential_key="MINIMAX_API_KEY",
                        ),
                    )
                ],
            ),
        }
    )
    return FakeRuntime(
        providers=providers,
        credentials=FakeCredentials(
            {"openai:subscription", "github-copilot:oauth", "minimax:api-key"}
        ),
        extras={
            "openai:subscription": {"chatgpt_account_id": "acct-123"},
            "github-copilot:oauth": {"github_oauth_token": "gho_example"},
        },
    )


@pytest.mark.asyncio
async def test_report_fans_out_across_providers_failing_open() -> None:
    # Arrange — OpenAI succeeds, Copilot returns 401, MiniMax succeeds.
    transport = RoutingTransport(
        {
            "wham/usage": FakeResponse(payload=_OPENAI_BODY),
            "copilot_internal/user": FakeResponse(status_code=401),
            "token_plan/remains": FakeResponse(payload=_MINIMAX_BODY),
        }
    )
    service = ProviderUsageService(_multi_provider_runtime(), transport=transport)

    # Act
    report = await service.report()

    # Assert — all three present; Copilot is an error snapshot, siblings parsed.
    by_connection = {snapshot.connection: snapshot for snapshot in report.providers}
    assert set(by_connection) == {
        "openai:subscription",
        "github-copilot:oauth",
        "minimax:api-key",
    }
    assert by_connection["openai:subscription"].error is None
    assert by_connection["github-copilot:oauth"].error == "HTTP 401"
    assert by_connection["minimax:api-key"].windows[0].used_percent == 75.0
