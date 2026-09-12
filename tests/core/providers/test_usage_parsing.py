"""Usage: parsing behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from core.providers._usage_parsers import (
    _epoch_to_iso,
    _parse_copilot_usage,
    _parse_minimax_usage,
    _parse_ollama_usage,
    _parse_openai_usage,
    _parse_openrouter_usage,
    _secondary_window_label,
)
from core.providers.usage import (
    UsageCredits,
    UsageFetchError,
    UsageWindow,
    clamp_percent,
)
from tests.core.providers.usage_helpers import (
    _MINIMAX_BODY,
    _OLLAMA_BODY,
    _OPENAI_BODY,
    _OPENROUTER_CREDITS_BODY,
    _OPENROUTER_KEY_BODY,
)

# GitHub Copilot parsing (openclaw-shaped fixtures)
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


# Pure helpers
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
            window_seconds=18_000,
        ),
        UsageWindow(
            label="Week",
            used_percent=12.0,
            reset_at=datetime.fromtimestamp(1_750_600_000, UTC).isoformat(),
            window_seconds=604_800,
        ),
    ]


def test_parse_openai_usage_keeps_credit_balance_structured() -> None:
    # The live body reports balance as a string gated by `has_credits`.
    body = {**_OPENAI_BODY, "credits": {"has_credits": True, "balance": "1234"}}
    snapshot = _parse_openai_usage("openai:subscription", "OpenAI", body)
    assert snapshot.plan == "Plus"
    assert snapshot.credits == UsageCredits(enabled=True, balance=1234.0)


def test_parse_openai_usage_omits_zero_or_disabled_credits() -> None:
    # The real Plus account: has_credits false, balance "0" → plan only.
    body = {**_OPENAI_BODY, "credits": {"has_credits": False, "balance": "0"}}
    snapshot = _parse_openai_usage("openai:subscription", "OpenAI", body)
    assert snapshot.plan == "Plus"
    assert snapshot.credits == UsageCredits(enabled=False, balance=0.0)


def test_parse_openai_usage_clamps_out_of_range_percent() -> None:
    body = {"rate_limit": {"primary_window": {"used_percent": 150, "limit_window_seconds": 18000}}}
    snapshot = _parse_openai_usage("openai:subscription", "OpenAI", body)
    assert snapshot.windows[0].used_percent == 100.0


def test_parse_openai_usage_missing_windows_yields_no_windows() -> None:
    snapshot = _parse_openai_usage("openai:subscription", "OpenAI", {"plan_type": "Plus"})
    assert snapshot.windows == []
    assert snapshot.error is None


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


# Ollama Cloud parsing (live-verified shape)
def test_parse_ollama_usage_normalizes_limits_and_observed_requests() -> None:
    snapshot = _parse_ollama_usage(
        "ollama-cloud:api-key",
        "Ollama Cloud",
        _OLLAMA_BODY,
    )

    assert snapshot.plan is None
    assert snapshot.error is None
    assert snapshot.windows == [
        UsageWindow(
            label="5h",
            used_percent=1.9,
            window_seconds=18_000,
            used_units=9.0,
            unit="requests",
        ),
        UsageWindow(
            label="Week",
            used_percent=0.7,
            window_seconds=604_800,
            used_units=14.0,
            unit="requests",
        ),
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"unexpected": True},
        {"limits": {}},
        {"limits": {"session": {"usage": "0.1"}}},
    ],
)
def test_parse_ollama_usage_malformed_limits_raise_fetch_error(body: Any) -> None:
    with pytest.raises(UsageFetchError):
        _parse_ollama_usage("ollama-cloud:api-key", "Ollama Cloud", body)


def test_parse_ollama_usage_keeps_percent_when_request_breakdown_changes() -> None:
    body = {"limits": {"session": {"usage": 0.25, "models": {"unexpected": True}}}}

    snapshot = _parse_ollama_usage("ollama-cloud:api-key", "Ollama Cloud", body)

    assert snapshot.windows == [UsageWindow(label="5h", used_percent=25.0, window_seconds=18_000)]


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
    with pytest.raises(UsageFetchError):
        _parse_minimax_usage("minimax:api-key", "MiniMax", {"unexpected": True})


def test_parse_minimax_usage_no_chat_model_raises_fetch_error() -> None:
    body = {"model_remains": [{"model_name": "MiniMax-Text-01", "current_interval_total_count": 5}]}
    with pytest.raises(UsageFetchError):
        _parse_minimax_usage("minimax:api-key", "MiniMax", body)


def test_parse_openrouter_usage_builds_cap_window_and_credits() -> None:
    # Act
    snapshot = _parse_openrouter_usage(
        "openrouter:api-key",
        "OpenRouter",
        _OPENROUTER_CREDITS_BODY,
        _OPENROUTER_KEY_BODY,
    )

    # Assert — used = (100 - 25) / 100 = 75%, balance = 50 - 12.5 = 37.5.
    assert snapshot.plan is None
    assert snapshot.error is None
    assert snapshot.credits == UsageCredits(enabled=True, balance=37.5)
    assert snapshot.windows == [
        UsageWindow(
            label="API key spending cap",
            used_percent=75.0,
            reset_at="2026-08-20T00:00:00+00:00",
            used_units=75.0,
            remaining_units=25.0,
            total_units=100.0,
            unit="USD",
        )
    ]


@pytest.mark.parametrize("limit", [None, 0])
def test_parse_openrouter_usage_without_cap_limit_yields_credits_only(limit: Any) -> None:
    key_body = {"data": {**_OPENROUTER_KEY_BODY["data"], "limit": limit}}

    snapshot = _parse_openrouter_usage(
        "openrouter:api-key", "OpenRouter", _OPENROUTER_CREDITS_BODY, key_body
    )

    assert snapshot.windows == []
    assert snapshot.credits == UsageCredits(enabled=True, balance=37.5)


def test_parse_openrouter_usage_rollover_remaining_skips_window() -> None:
    # remaining > limit means the cap rolled over; the ratio is meaningless.
    key_body = {"data": {**_OPENROUTER_KEY_BODY["data"], "limit_remaining": 120.0}}

    snapshot = _parse_openrouter_usage(
        "openrouter:api-key", "OpenRouter", _OPENROUTER_CREDITS_BODY, key_body
    )

    assert snapshot.windows == []
    assert snapshot.credits == UsageCredits(enabled=True, balance=37.5)


def test_parse_openrouter_usage_non_iso_reset_omits_reset_at() -> None:
    key_body = {"data": {**_OPENROUTER_KEY_BODY["data"], "limit_reset": "not-a-date"}}

    snapshot = _parse_openrouter_usage(
        "openrouter:api-key", "OpenRouter", _OPENROUTER_CREDITS_BODY, key_body
    )

    assert len(snapshot.windows) == 1
    assert snapshot.windows[0].reset_at is None


def test_parse_openrouter_usage_degrades_to_credits_without_key_body() -> None:
    snapshot = _parse_openrouter_usage(
        "openrouter:api-key", "OpenRouter", _OPENROUTER_CREDITS_BODY, None
    )

    assert snapshot.windows == []
    assert snapshot.credits == UsageCredits(enabled=True, balance=37.5)
    assert snapshot.error is None
