"""Usage: service behavior."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.providers.errors import ProviderError
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.usage import (
    ProviderUsageService,
    UsageCredits,
)
from tests.core.providers.usage_helpers import (
    _MINIMAX_BODY,
    _OLLAMA_BODY,
    _OPENAI_BODY,
    _OPENROUTER_CREDITS_BODY,
    _OPENROUTER_KEY_BODY,
    FakeCredentials,
    FakeProviders,
    FakeResponse,
    FakeRuntime,
    FakeTransport,
    _openai_provider_config,
    _openai_runtime,
)


class HangingTransport:
    async def get(
        self, url: str, *, headers: Any, timeout: float, params: Any = None
    ) -> FakeResponse:
        await asyncio.sleep(10)
        return FakeResponse()  # pragma: no cover


class BlockingTransport:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def get(
        self, url: str, *, headers: Any, timeout: float, params: Any = None
    ) -> FakeResponse:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return self._response


class RaisingTransport:
    async def get(
        self, url: str, *, headers: Any, timeout: float, params: Any = None
    ) -> FakeResponse:
        raise RuntimeError("boom")


def _ollama_cloud_provider_config() -> ProviderConfig:
    return ProviderConfig(
        id="ollama-cloud",
        name="Ollama Cloud",
        adapter="ollama",
        base_url="https://ollama.com",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OLLAMA_API_KEY",
                ),
                mode="cloud",
            )
        ],
    )


def _ollama_cloud_runtime() -> FakeRuntime:
    return FakeRuntime(
        providers=FakeProviders({"ollama-cloud": _ollama_cloud_provider_config()}),
        credentials=FakeCredentials({"ollama-cloud:api-key"}),
        tokens={"ollama-cloud:api-key:default": "ollama-secret"},
    )


def _openrouter_provider_config() -> ProviderConfig:
    return ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        adapter="openrouter",
        base_url="https://openrouter.ai/api/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENROUTER_API_KEY",
                ),
            )
        ],
    )


def _openrouter_runtime() -> FakeRuntime:
    return FakeRuntime(
        providers=FakeProviders({"openrouter": _openrouter_provider_config()}),
        credentials=FakeCredentials({"openrouter:api-key"}),
        tokens={"openrouter:api-key:default": "or-secret"},
    )


# Service fan-out across providers
class RoutingTransport:
    """Returns a different response per URL substring; records calls."""

    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(
        self, url: str, *, headers: Any, timeout: float, params: Any = None
    ) -> FakeResponse:
        self.calls.append((url, dict(headers)))
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
@pytest.mark.parametrize("outcome", ["success", "rejected_again", "forbidden", "refresh_failure"])
async def test_usage_recovers_oauth_once_and_rebuilds_account_headers(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    class Getter:
        token = "old-test-token"
        account = "old-test-account"
        refreshes = 0

        async def __call__(self) -> str:
            return self.token

        async def refresh_after_rejection(
            self, rejected: str, *, status_code: int, response_body: str
        ) -> str | None:
            if status_code != 401:
                return None
            assert rejected == "old-test-token"
            self.refreshes += 1
            if outcome == "refresh_failure":
                raise ProviderError("test refresh unavailable", retryable=True)
            self.token = "new-test-token"
            self.account = "new-test-account"
            return self.token

    class Transport:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        async def get(
            self, url: str, *, headers: Any, timeout: float, params: Any = None
        ) -> FakeResponse:
            self.calls.append(dict(headers))
            if len(self.calls) == 1:
                return FakeResponse(403 if outcome == "forbidden" else 401)
            return FakeResponse(401 if outcome == "rejected_again" else 200, _OPENAI_BODY)

    runtime = _openai_runtime()
    getter = Getter()
    monkeypatch.setattr(runtime, "get_connection_token_getter", lambda _connection: getter)
    monkeypatch.setattr(
        runtime,
        "get_connection_token_extra",
        lambda _connection: {"chatgpt_account_id": getter.account},
    )
    transport = Transport()
    service = ProviderUsageService(runtime, transport=transport)
    try:
        report = await service.report()
        assert len(report.providers) == 1
        assert (report.providers[0].error is None) == (outcome == "success")
        assert getter.refreshes == (0 if outcome == "forbidden" else 1)
        assert len(transport.calls) == (2 if outcome in {"success", "rejected_again"} else 1)
        if len(transport.calls) == 2:
            assert transport.calls[1]["Authorization"] == "Bearer new-test-token"
            assert transport.calls[1]["chatgpt-account-id"] == "new-test-account"
    finally:
        await service.aclose()


# Service
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
    assert snapshot.account == "default"
    assert [window.label for window in snapshot.windows] == ["5h", "Week"]
    # The request carries the account header + Codex beta/originator headers.
    _, headers = transport.calls[0]
    assert headers["chatgpt-account-id"] == "acct-123"
    assert headers["OpenAI-Beta"] == "responses=experimental"
    assert headers["originator"] == "vbot"


@pytest.mark.asyncio
async def test_report_fetches_ollama_cloud_usage_with_connection_auth() -> None:
    transport = FakeTransport(FakeResponse(payload=_OLLAMA_BODY))
    service = ProviderUsageService(_ollama_cloud_runtime(), transport=transport)

    report = await service.report()

    assert len(report.providers) == 1
    snapshot = report.providers[0]
    assert snapshot.connection == "ollama-cloud:api-key"
    assert snapshot.account == "default"
    assert [window.used_percent for window in snapshot.windows] == [1.9, 0.7]
    assert transport.calls == [
        (
            "https://ollama.com/api/usage",
            {"Authorization": "Bearer ollama-secret"},
        )
    ]


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
async def test_report_refreshes_successful_snapshot_after_ten_seconds() -> None:
    transport = FakeTransport(FakeResponse(payload=_OPENAI_BODY))
    now = 1000.0
    service = ProviderUsageService(_openai_runtime(), transport=transport, monotonic=lambda: now)

    await service.report()
    now += 10.0
    await service.report()

    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_report_backs_off_error_snapshot_for_sixty_seconds() -> None:
    transport = FakeTransport(FakeResponse(status_code=429))
    now = 1000.0
    service = ProviderUsageService(_openai_runtime(), transport=transport, monotonic=lambda: now)

    await service.report()
    now += 10.1
    cached = await service.report()
    now += 50.0
    await service.report()

    assert cached.providers[0].error == "HTTP 429"
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_report_coalesces_concurrent_fetches_per_connection() -> None:
    transport = BlockingTransport(FakeResponse(payload=_OPENAI_BODY))
    service = ProviderUsageService(_openai_runtime(), transport=transport)

    first = asyncio.create_task(service.report())
    await transport.started.wait()
    second = asyncio.create_task(service.report())
    await asyncio.sleep(0)
    transport.release.set()
    first_report, second_report = await asyncio.gather(first, second)

    assert transport.calls == 1
    assert first_report.providers[0].windows == second_report.providers[0].windows


@pytest.mark.asyncio
async def test_report_filters_to_requested_connections() -> None:
    transport = FakeTransport(FakeResponse(payload=_OPENAI_BODY))
    service = ProviderUsageService(_openai_runtime(), transport=transport)

    report = await service.report(connections=["minimax:api-key"])

    assert report.providers == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_report_fetches_openrouter_credits_and_key_cap() -> None:
    # Arrange
    transport = RoutingTransport(
        {
            "/credits": FakeResponse(payload=_OPENROUTER_CREDITS_BODY),
            "/key": FakeResponse(payload=_OPENROUTER_KEY_BODY),
        }
    )
    service = ProviderUsageService(_openrouter_runtime(), transport=transport)

    # Act
    report = await service.report(connections=["openrouter:api-key"])

    # Assert
    assert len(report.providers) == 1
    snapshot = report.providers[0]
    assert snapshot.connection == "openrouter:api-key"
    assert snapshot.account == "default"
    assert snapshot.windows[0].used_percent == 75.0
    assert snapshot.credits == UsageCredits(enabled=True, balance=37.5)
    assert [(url, headers) for url, headers in transport.calls] == [
        ("https://openrouter.ai/api/v1/credits", {"Authorization": "Bearer or-secret"}),
        ("https://openrouter.ai/api/v1/key", {"Authorization": "Bearer or-secret"}),
    ]


@pytest.mark.asyncio
async def test_report_openrouter_degrades_to_credits_when_key_endpoint_fails() -> None:
    transport = RoutingTransport(
        {
            "/credits": FakeResponse(payload=_OPENROUTER_CREDITS_BODY),
            "/key": FakeResponse(status_code=500),
        }
    )
    service = ProviderUsageService(_openrouter_runtime(), transport=transport)

    report = await service.report()

    assert len(report.providers) == 1
    snapshot = report.providers[0]
    assert snapshot.error is None
    assert snapshot.windows == []
    assert snapshot.credits == UsageCredits(enabled=True, balance=37.5)


@pytest.mark.asyncio
async def test_report_openrouter_credits_failure_is_error_snapshot() -> None:
    transport = RoutingTransport(
        {
            "/credits": FakeResponse(status_code=500),
            "/key": FakeResponse(payload=_OPENROUTER_KEY_BODY),
        }
    )
    service = ProviderUsageService(_openrouter_runtime(), transport=transport)

    report = await service.report()

    assert len(report.providers) == 1
    assert report.providers[0].error == "HTTP 500"


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
