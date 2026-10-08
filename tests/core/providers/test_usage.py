"""Usage: the live Provider usage report (fetch, parse, cache, fail-open)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from core.providers._usage_types import _PRIMARY_FALLBACK_LABEL, _SECONDARY_FALLBACK_LABEL
from core.providers.errors import ProviderError
from core.providers.openai import CODEX_EXTRA_HEADERS
from core.providers.token_getter import COPILOT_EDITOR_VERSION, COPILOT_INTEGRATION_ID
from core.providers.usage import (
    COPILOT_USAGE_URL,
    ProviderUsageService,
    ProviderUsageSnapshot,
    UsageCredits,
    UsageReport,
    UsageTransport,
    UsageWindow,
    clamp_percent,
)
from tests.core.providers.usage_test_support import (
    OLLAMA_BODY,
    OPENAI_BODY,
    OPENROUTER_CREDITS_BODY,
    OPENROUTER_KEY_BODY,
    USAGE_RESPONSES,
    FakeResponse,
    FakeTransport,
    usage_runtime,
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


def _iso(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, UTC).isoformat()


async def _report(
    transport: UsageTransport, *provider_ids: str, **service_options: Any
) -> UsageReport:
    service = ProviderUsageService(
        usage_runtime(*provider_ids), transport=transport, **service_options
    )
    try:
        return await service.report()
    finally:
        await service.aclose()


_OPENROUTER_CAP_WINDOW = UsageWindow(
    label="API key spending cap",
    used_percent=75.0,
    reset_at="2026-08-20T00:00:00+00:00",
    used_units=75.0,
    remaining_units=25.0,
    total_units=100.0,
    unit="USD",
)
_OPENROUTER_CREDITS = UsageCredits(enabled=True, balance=37.5, unit="USD")


@pytest.mark.parametrize(
    ("provider_id", "expected_calls", "expected"),
    [
        pytest.param(
            "openai",
            [
                (
                    "https://chatgpt.com/backend-api/wham/usage",
                    {
                        "Authorization": "Bearer access-token",
                        "chatgpt-account-id": "acct-123",
                        **CODEX_EXTRA_HEADERS,
                    },
                )
            ],
            ProviderUsageSnapshot(
                connection="openai:subscription",
                account="default",
                display_name="OpenAI",
                plan="Plus",
                windows=[
                    UsageWindow(
                        label="5h",
                        used_percent=42.5,
                        reset_at=_iso(1_750_000_000),
                        window_seconds=18_000,
                    ),
                    UsageWindow(
                        label="Week",
                        used_percent=12.0,
                        reset_at=_iso(1_750_600_000),
                        window_seconds=604_800,
                    ),
                ],
            ),
            id="openai",
        ),
        # The Copilot probe authenticates with the stored GitHub OAuth token.
        pytest.param(
            "github-copilot",
            [
                (
                    COPILOT_USAGE_URL,
                    {
                        "Authorization": "token gho_example",
                        "Accept": "application/json",
                        "Copilot-Integration-Id": COPILOT_INTEGRATION_ID,
                        "Editor-Version": COPILOT_EDITOR_VERSION,
                    },
                )
            ],
            ProviderUsageSnapshot(
                connection="github-copilot:oauth",
                account="default",
                display_name="GitHub Copilot",
                plan="individual",
                windows=[
                    UsageWindow(
                        label="Premium",
                        used_percent=25.0,
                        reset_at="2026-07-01T00:00:00+00:00",
                        used_units=75.0,
                        remaining_units=225.0,
                        total_units=300.0,
                        unit="interactions",
                        unlimited=False,
                    ),
                    UsageWindow(
                        label="Chat",
                        used_percent=0.0,
                        reset_at="2026-07-01T00:00:00+00:00",
                        unlimited=True,
                    ),
                ],
            ),
            id="github-copilot",
        ),
        pytest.param(
            "ollama-cloud",
            [
                (
                    "https://ollama.com/api/balance",
                    {"Authorization": "Bearer ollama-secret", "Accept": "application/json"},
                )
            ],
            ProviderUsageSnapshot(
                connection="ollama-cloud:api-key",
                account="default",
                display_name="Ollama Cloud",
                windows=[
                    UsageWindow(
                        label="5h",
                        used_percent=0.53,
                        reset_at="2026-10-08T14:00:00+00:00",
                        window_seconds=18_000,
                    ),
                    UsageWindow(
                        label="Week",
                        used_percent=5.62,
                        reset_at="2026-10-12T00:00:00+00:00",
                        window_seconds=604_800,
                    ),
                ],
                credits=UsageCredits(enabled=False, balance=0.0, unit="USD"),
            ),
            id="ollama-cloud",
        ),
        # Picks the MiniMax-M chat model with a non-zero total: (1000 - 250) / 1000.
        pytest.param(
            "minimax",
            [
                (
                    "https://api.minimaxi.com/v1/token_plan/remains",
                    {"Authorization": "Bearer access-token"},
                )
            ],
            ProviderUsageSnapshot(
                connection="minimax:api-key",
                account="default",
                display_name="MiniMax",
                plan="Token Plan",
                windows=[
                    UsageWindow(
                        label="24h",
                        used_percent=75.0,
                        reset_at=_iso(1_750_600_000),
                        window_seconds=86_400,
                        used_units=750.0,
                        remaining_units=250.0,
                        total_units=1000.0,
                        unit="requests",
                    )
                ],
            ),
            id="minimax",
        ),
        # Cap used = (100 - 25) / 100; balance = 50 - 12.5.
        pytest.param(
            "openrouter",
            [
                ("https://openrouter.ai/api/v1/credits", {"Authorization": "Bearer or-secret"}),
                ("https://openrouter.ai/api/v1/key", {"Authorization": "Bearer or-secret"}),
            ],
            ProviderUsageSnapshot(
                connection="openrouter:api-key",
                account="default",
                display_name="OpenRouter",
                windows=[_OPENROUTER_CAP_WINDOW],
                credits=_OPENROUTER_CREDITS,
            ),
            id="openrouter",
        ),
        pytest.param(
            "opencode-go",
            [
                (
                    "https://opencode.ai/zen/go/v1/usage",
                    {
                        "User-Agent": "vBot",
                        "Authorization": "Bearer go-secret",
                        "Accept": "application/json",
                    },
                )
            ],
            ProviderUsageSnapshot(
                connection="opencode-go:api-key",
                account="default",
                display_name="OpenCode Go",
                windows=[
                    UsageWindow(
                        label="5h",
                        used_percent=0.0,
                        reset_at="2026-10-08T13:30:41+00:00",
                        window_seconds=18_000,
                    ),
                    UsageWindow(
                        label="Week",
                        used_percent=1.0,
                        reset_at="2026-10-12T00:00:00+00:00",
                        window_seconds=604_800,
                    ),
                    UsageWindow(
                        label="Month", used_percent=41.0, reset_at="2026-10-30T17:56:11+00:00"
                    ),
                ],
            ),
            id="opencode-go",
        ),
    ],
)
@pytest.mark.asyncio
async def test_report_fetches_and_normalizes_each_supported_connection(
    provider_id: str,
    expected_calls: list[tuple[str, dict[str, str]]],
    expected: ProviderUsageSnapshot,
) -> None:
    transport = FakeTransport(USAGE_RESPONSES)

    report = await _report(transport, provider_id)

    assert report.providers == [expected]
    assert transport.calls == expected_calls


def _openrouter_key(**changes: Any) -> dict[str, FakeResponse]:
    key_body = {"data": {**OPENROUTER_KEY_BODY["data"], **changes}}
    return {
        "/credits": FakeResponse(payload=OPENROUTER_CREDITS_BODY),
        "/key": FakeResponse(payload=key_body),
    }


_UNSUPPORTED_SHAPE = {"windows": [], "error": "Unsupported response shape"}


@pytest.mark.parametrize(
    ("provider_id", "responses", "expected"),
    [
        pytest.param(
            "openai",
            FakeResponse(
                payload={
                    "rate_limit": {
                        "primary_window": {"used_percent": 150, "reset_at": 1_750_000_000_000},
                        "secondary_window": {
                            "used_percent": -5,
                            "limit_window_seconds": 86_400,
                            "reset_at": "nope",
                        },
                    }
                }
            ),
            {
                "windows": [
                    UsageWindow(
                        label=_PRIMARY_FALLBACK_LABEL,
                        used_percent=100.0,
                        reset_at=_iso(1_750_000_000),
                    ),
                    UsageWindow(label="Day", used_percent=0.0, window_seconds=86_400),
                ]
            },
            id="openai-clamped-millisecond-reset-and-day-window",
        ),
        pytest.param(
            "openai",
            FakeResponse(
                payload={
                    "rate_limit": {
                        "primary_window": {"used_percent": 3, "limit_window_seconds": 18_000}
                    }
                }
            ),
            {"windows": [UsageWindow(label="5h", used_percent=3.0, window_seconds=18_000)]},
            id="openai-primary-window-only",
        ),
        # The live body reports the balance as a string gated by ``has_credits``.
        pytest.param(
            "openai",
            FakeResponse(
                payload={
                    "credits": {"has_credits": True, "balance": "1234"},
                    "rate_limit": {
                        "secondary_window": {"used_percent": 1, "limit_window_seconds": 18_000}
                    },
                }
            ),
            {
                "windows": [UsageWindow(label="5h", used_percent=1.0, window_seconds=18_000)],
                "credits": UsageCredits(enabled=True, balance=1234.0),
            },
            id="openai-string-credit-balance-and-sub-day-window",
        ),
        pytest.param(
            "openai",
            FakeResponse(
                payload={
                    "credits": {"has_credits": False, "balance": "0"},
                    "rate_limit": {"secondary_window": {"used_percent": 1}},
                }
            ),
            {
                "windows": [UsageWindow(label=_SECONDARY_FALLBACK_LABEL, used_percent=1.0)],
                "credits": UsageCredits(enabled=False, balance=0.0),
            },
            id="openai-disabled-credits-and-unlabelled-window",
        ),
        # Neither windows, enabled credits nor an error: omitted from the report.
        pytest.param(
            "openai", FakeResponse(payload={"plan_type": "Plus"}), None, id="openai-no-windows"
        ),
        pytest.param(
            "github-copilot",
            FakeResponse(payload={"copilot_plan": "business"}),
            None,
            id="copilot-no-quota-snapshots",
        ),
        # The activity-history shape ``/api/usage`` returns since 2026-10-07.
        pytest.param(
            "ollama-cloud",
            FakeResponse(payload={"range": "7d", "totals": {"request_count": 3}, "buckets": []}),
            _UNSUPPORTED_SHAPE,
            id="ollama-no-included-object",
        ),
        pytest.param(
            "ollama-cloud",
            FakeResponse(
                payload={"included": {"session": {"remaining_percent": "75", "resets_at": None}}}
            ),
            _UNSUPPORTED_SHAPE,
            id="ollama-non-numeric-remaining",
        ),
        # Documented monthly-credit plans; not live-verified.
        pytest.param(
            "ollama-cloud",
            FakeResponse(
                payload={
                    "included": {
                        "balance_usd": 15.0,
                        "allowance_usd": 20.0,
                        "period": {
                            "from": "2026-10-01T00:00:00Z",
                            "until": "2026-11-01T00:00:00Z",
                        },
                    },
                    "purchased": {"balance_usd": 4.5},
                }
            ),
            {
                "windows": [
                    UsageWindow(
                        label="Month",
                        used_percent=25.0,
                        reset_at="2026-11-01T00:00:00+00:00",
                        used_units=5.0,
                        remaining_units=15.0,
                        total_units=20.0,
                        unit="USD",
                    )
                ],
                "credits": UsageCredits(enabled=True, balance=4.5, unit="USD"),
            },
            id="ollama-monthly-credits-and-purchased-balance",
        ),
        pytest.param(
            "ollama-cloud",
            FakeResponse(payload={"included": {"balance_usd": 1.0, "allowance_usd": 0}}),
            _UNSUPPORTED_SHAPE,
            id="ollama-credits-without-allowance",
        ),
        pytest.param(
            "opencode-go",
            FakeResponse(
                payload={
                    "usage": {
                        "rolling": {
                            "status": "rate-limited",
                            "percent": 99.5,
                            "resetsAt": "2026-10-08T13:30:41.000Z",
                        }
                    }
                }
            ),
            {
                "windows": [
                    UsageWindow(
                        label="5h",
                        used_percent=100.0,
                        reset_at="2026-10-08T13:30:41+00:00",
                        window_seconds=18_000,
                    )
                ]
            },
            id="opencode-go-rate-limited-window",
        ),
        pytest.param(
            "opencode-go",
            FakeResponse(payload={"usage": {"weekly": {"status": "ok", "percent": 1}}}),
            _UNSUPPORTED_SHAPE,
            id="opencode-go-window-without-reset",
        ),
        # A key without a Go subscription (403 EntitlementError, from source).
        pytest.param(
            "opencode-go",
            FakeResponse(status_code=403),
            {"error": "HTTP 403"},
            id="opencode-go-without-subscription",
        ),
        pytest.param(
            "minimax",
            FakeResponse(payload={"unexpected": True}),
            _UNSUPPORTED_SHAPE,
            id="minimax-no-model-remains",
        ),
        pytest.param(
            "minimax",
            FakeResponse(
                payload={
                    "model_remains": [
                        {"model_name": "MiniMax-Text-01", "current_interval_total_count": 5}
                    ]
                }
            ),
            _UNSUPPORTED_SHAPE,
            id="minimax-no-chat-model",
        ),
        pytest.param(
            "openrouter",
            _openrouter_key(limit=None),
            {"windows": [], "credits": _OPENROUTER_CREDITS},
            id="openrouter-no-cap-limit",
        ),
        # remaining > limit means the cap rolled over; the ratio is meaningless.
        pytest.param(
            "openrouter",
            _openrouter_key(limit_remaining=120.0),
            {"windows": [], "credits": _OPENROUTER_CREDITS},
            id="openrouter-rolled-over-cap",
        ),
        pytest.param(
            "openrouter",
            _openrouter_key(limit_reset="not-a-date"),
            {"windows": [UsageWindow(**{**_OPENROUTER_CAP_WINDOW.to_dict(), "reset_at": None})]},
            id="openrouter-non-iso-reset",
        ),
        pytest.param(
            "openrouter",
            {
                "/credits": FakeResponse(payload=OPENROUTER_CREDITS_BODY),
                "/key": FakeResponse(status_code=500),
            },
            {"windows": [], "credits": _OPENROUTER_CREDITS, "error": None},
            id="openrouter-key-failure-degrades-to-credits",
        ),
        pytest.param(
            "openrouter",
            {
                "/credits": FakeResponse(status_code=500),
                "/key": FakeResponse(payload=OPENROUTER_KEY_BODY),
            },
            {"error": "HTTP 500"},
            id="openrouter-credits-failure",
        ),
    ],
)
@pytest.mark.asyncio
async def test_report_projects_upstream_body_variants(
    provider_id: str,
    responses: FakeResponse | dict[str, FakeResponse],
    expected: dict[str, Any] | None,
) -> None:
    report = await _report(FakeTransport(responses), provider_id)

    if expected is None:
        assert report.providers == []
        return
    [snapshot] = report.providers
    assert {field: getattr(snapshot, field) for field in expected} == expected


def test_clamp_percent_bounds_and_rejects_non_numbers() -> None:
    assert clamp_percent(150) == 100.0
    assert clamp_percent(-5) == 0.0
    assert clamp_percent(42.5) == 42.5
    assert clamp_percent("nope") == 0.0
    assert clamp_percent(True) == 0.0


@pytest.mark.parametrize(
    ("provider_id", "transport", "error"),
    [
        pytest.param("ollama-cloud", HangingTransport(), "Timeout", id="timeout"),
        pytest.param(
            "ollama-cloud",
            FakeTransport(FakeResponse(status_code=503)),
            "HTTP 503",
            id="http-status",
        ),
        pytest.param(
            "ollama-cloud", FakeTransport(FakeResponse()), "Invalid response", id="invalid-json"
        ),
        pytest.param("ollama-cloud", RaisingTransport(), "Unavailable", id="unexpected-error"),
        # The OAuth Connection's token getter cannot refresh, so the 401 stands.
        pytest.param(
            "openai",
            FakeTransport(FakeResponse(status_code=401)),
            "HTTP 401",
            id="oauth-401-without-refresh",
        ),
    ],
)
@pytest.mark.asyncio
async def test_report_fails_open_into_an_error_snapshot(
    provider_id: str, transport: UsageTransport, error: str
) -> None:
    report = await _report(transport, provider_id, timeout=0.01)

    assert [snapshot.error for snapshot in report.providers] == [error]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        ("success", None),
        ("rejected_again", "HTTP 401"),
        ("forbidden", "HTTP 403"),
        ("refresh_failure", "Unavailable"),
    ],
)
async def test_usage_recovers_oauth_once_and_rebuilds_account_headers(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    error: str | None,
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
            return FakeResponse(401 if outcome == "rejected_again" else 200, OPENAI_BODY)

    runtime = usage_runtime("openai")
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
    finally:
        await service.aclose()

    assert [snapshot.error for snapshot in report.providers] == [error]
    assert getter.refreshes == (0 if outcome == "forbidden" else 1)
    assert len(transport.calls) == (2 if outcome in {"success", "rejected_again"} else 1)
    if len(transport.calls) == 2:
        assert transport.calls[1]["Authorization"] == "Bearer new-test-token"
        assert transport.calls[1]["chatgpt-account-id"] == "new-test-account"


@pytest.mark.asyncio
async def test_report_never_probes_an_unusable_connection() -> None:
    transport = FakeTransport(USAGE_RESPONSES)
    service = ProviderUsageService(usage_runtime("openai", usable=False), transport=transport)

    report = await service.report()

    assert report.providers == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_report_probes_only_the_requested_connections() -> None:
    transport = FakeTransport(USAGE_RESPONSES)
    service = ProviderUsageService(usage_runtime("openai", "minimax"), transport=transport)

    report = await service.report(connections=["minimax:api-key"])

    assert [snapshot.connection for snapshot in report.providers] == ["minimax:api-key"]
    assert [url for url, _headers in transport.calls] == [
        "https://api.minimaxi.com/v1/token_plan/remains"
    ]


@pytest.mark.parametrize(
    ("provider_id", "response", "ttl"),
    [
        pytest.param(
            "openai", FakeResponse(payload=OPENAI_BODY), 10.0, id="success-for-ten-seconds"
        ),
        # Ollama asks for at most one usage request per minute.
        pytest.param(
            "ollama-cloud",
            FakeResponse(payload=OLLAMA_BODY),
            60.0,
            id="ollama-success-for-sixty-seconds",
        ),
        pytest.param("openai", FakeResponse(status_code=429), 60.0, id="error-for-sixty-seconds"),
    ],
)
@pytest.mark.asyncio
async def test_report_caches_each_snapshot_until_its_ttl(
    provider_id: str, response: FakeResponse, ttl: float
) -> None:
    transport = FakeTransport(response)
    now = 1000.0
    service = ProviderUsageService(
        usage_runtime(provider_id), transport=transport, monotonic=lambda: now
    )

    first = await service.report()
    now = 1000.0 + ttl - 0.1
    cached = await service.report()
    now = 1000.0 + ttl
    await service.report()

    assert cached.providers == first.providers
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_report_coalesces_concurrent_fetches_per_connection() -> None:
    transport = BlockingTransport(FakeResponse(payload=OPENAI_BODY))
    service = ProviderUsageService(usage_runtime("openai"), transport=transport)

    first = asyncio.create_task(service.report())
    await transport.started.wait()
    second = asyncio.create_task(service.report())
    await asyncio.sleep(0)
    transport.release.set()
    first_report, second_report = await asyncio.gather(first, second)

    assert transport.calls == 1
    assert first_report.providers[0].windows == second_report.providers[0].windows


@pytest.mark.asyncio
async def test_report_fans_out_across_providers_failing_open() -> None:
    # OpenAI succeeds, Copilot returns 401, MiniMax succeeds.
    transport = FakeTransport(
        {**USAGE_RESPONSES, "copilot_internal/user": FakeResponse(status_code=401)}
    )

    report = await _report(transport, "openai", "github-copilot", "minimax")

    by_connection = {snapshot.connection: snapshot for snapshot in report.providers}
    assert set(by_connection) == {
        "openai:subscription",
        "github-copilot:oauth",
        "minimax:api-key",
    }
    assert by_connection["openai:subscription"].error is None
    assert by_connection["github-copilot:oauth"].error == "HTTP 401"
    assert by_connection["minimax:api-key"].windows[0].used_percent == 75.0
