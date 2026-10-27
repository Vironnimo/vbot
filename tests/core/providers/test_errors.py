"""Provider error taxonomy and in-band Provider error classification."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderContextOverflowError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUsageLimitError,
    classify_in_band_provider_error,
    usage_limit_error,
)
from core.utils.errors import ProviderError, VBotError


def test_network_error_is_retryable_without_becoming_a_provider_error() -> None:
    error = NetworkError("network down")

    assert isinstance(error, VBotError)
    assert not isinstance(error, ProviderError)
    assert error.retryable is True


@pytest.mark.parametrize(
    ("payload", "lenient", "expected_type", "retryable", "retry_after"),
    [
        pytest.param(
            {"message": "bad key", "code": 401},
            False,
            ProviderAuthError,
            False,
            None,
            id="auth-numeric-code",
        ),
        pytest.param(
            {"message": "no access", "metadata": {"error_type": "authentication"}},
            True,
            ProviderAuthError,
            False,
            None,
            id="auth-metadata-type-even-lenient",
        ),
        pytest.param(
            {"message": "slow down", "error_type": "rate_limit_exceeded"},
            False,
            ProviderRateLimitError,
            True,
            None,
            id="rate-limit-top-level-type",
        ),
        pytest.param(
            {"message": "throttled", "code": 429, "availability": {"retry_after": 5}},
            False,
            ProviderRateLimitError,
            True,
            5,
            id="rate-limit-with-retry-after",
        ),
        pytest.param(
            {"message": "deadline", "error_type": "timeout"},
            False,
            ProviderTimeoutError,
            True,
            None,
            id="timeout",
        ),
        pytest.param(
            {"message": "gateway gone", "code": 502},
            False,
            ProviderError,
            True,
            None,
            id="transient-server-error",
        ),
        pytest.param(
            {"message": "upstream idle timeout", "type": "server_error"},
            False,
            ProviderError,
            True,
            None,
            id="fallback-server-type",
        ),
        pytest.param(
            {"message": "bad key", "type": "authentication_error"},
            True,
            ProviderAuthError,
            False,
            None,
            id="fallback-auth-type-even-lenient",
        ),
        pytest.param(
            {"message": "bad key", "type": "server_error", "code": 401},
            False,
            ProviderAuthError,
            False,
            None,
            id="numeric-auth-beats-fallback-type",
        ),
        pytest.param(
            {"message": "no quota", "type": "api_error", "code": "insufficient_quota"},
            True,
            ProviderError,
            False,
            None,
            id="specific-code-beats-fallback-type",
        ),
        pytest.param(
            {"message": "no quota", "code": 429, "error_type": "insufficient_quota"},
            True,
            ProviderError,
            False,
            None,
            id="exhausted-quota-beats-a-wait-status",
        ),
        pytest.param(
            {
                "message": "no access",
                "metadata": {"error_type": "authentication"},
                "error_type": "server_error",
                "code": "server_error",
                "type": "api_error",
            },
            False,
            ProviderAuthError,
            False,
            None,
            id="metadata-type-precedence-unchanged",
        ),
        pytest.param(
            {"message": "unknown failure", "type": "new_provider_error"},
            False,
            ProviderError,
            False,
            None,
            id="unknown-fallback-type-stays-fatal",
        ),
        pytest.param(
            {"message": "too long", "error_type": "context_length_exceeded"},
            True,
            ProviderContextOverflowError,
            False,
            None,
            id="fatal-type-even-lenient",
        ),
        pytest.param(
            {
                "message": "prompt is too long: 208310 tokens > 200000 maximum",
                "type": "invalid_request_error",
            },
            False,
            ProviderContextOverflowError,
            False,
            None,
            id="explicit-prompt-overflow",
        ),
        pytest.param(
            {"message": "token limit exceeded", "code": "token_limit_exceeded"},
            True,
            ProviderError,
            False,
            None,
            id="ambiguous-token-limit-is-not-context-overflow",
        ),
        pytest.param(
            {
                "message": "prompt is too long: 208310 tokens > 200000 maximum",
                "code": "content_policy_violation",
            },
            True,
            ProviderError,
            False,
            None,
            id="explicit-refusal-beats-context-wording",
        ),
        pytest.param(
            {"message": "nope", "code": "permission_denied"},
            True,
            ProviderError,
            False,
            None,
            id="fatal-string-code-even-lenient",
        ),
        pytest.param(
            {"message": "all busy", "availability": {"retryable": True, "retry_after": 30}},
            False,
            ProviderError,
            True,
            30,
            id="router-availability-hint",
        ),
        pytest.param(
            {"message": "assistant messages require content", "code": 400},
            False,
            ProviderError,
            False,
            None,
            id="unknown-strict",
        ),
        pytest.param(
            {"message": "assistant messages require content", "code": 400},
            True,
            ProviderError,
            True,
            None,
            id="unknown-lenient",
        ),
    ],
)
def test_in_band_error_maps_to_the_shared_taxonomy_and_keeps_the_raw_body(
    payload: dict[str, Any],
    lenient: bool,
    expected_type: type[ProviderError],
    retryable: bool,
    retry_after: int | None,
) -> None:
    classified = classify_in_band_provider_error(payload, lenient_unknown=lenient)

    assert type(classified) is expected_type
    assert classified.retryable is retryable
    assert classified.retry_after == retry_after
    code = payload.get("code")
    if type(classified) is ProviderError and isinstance(code, int) and 400 <= code <= 599:
        assert classified.status_code == code
    prefix = f"{payload['message']}: "
    text = str(classified)
    assert text.startswith(prefix)
    assert json.loads(text[len(prefix) :]) == payload


def test_in_band_error_without_a_message_is_the_bare_json_body() -> None:
    payload = {"code": 500, "metadata": {"error_type": "server"}}

    assert json.loads(str(classify_in_band_provider_error(payload))) == payload


def test_non_mapping_in_band_error_stays_fatal_plain_text() -> None:
    classified = classify_in_band_provider_error("quota exceeded", lenient_unknown=True)

    assert type(classified) is ProviderError
    assert classified.retryable is False
    assert str(classified) == "quota exceeded"


_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _epoch(delta: timedelta) -> int:
    return int((_NOW + delta).timestamp())


@pytest.mark.parametrize(
    ("error", "reported_wait", "expected_type", "retry_after", "wording"),
    [
        pytest.param(
            {"type": "usage_limit_reached", "resets_at": _epoch(timedelta(hours=3))},
            None,
            ProviderUsageLimitError,
            None,
            "Subscription usage limit reached; it resets at 2026-10-19 17:00 CEST: detail",
            id="distant-reset-stops",
        ),
        pytest.param(
            {"type": "usage_limit_reached", "resets_at": _epoch(timedelta(seconds=45))},
            None,
            ProviderRateLimitError,
            45.0,
            "Subscription usage limit reached; it resets at 2026-10-19 14:00 CEST, "
            "in 45 seconds: detail",
            id="reset-within-the-retry-cap-waits-exactly",
        ),
        pytest.param(
            {
                "code": "usage_limit_reached",
                "resets_at": _epoch(timedelta(hours=3)),
                "resets_in_seconds": 30,
            },
            10.0,
            ProviderUsageLimitError,
            None,
            "it resets at 2026-10-19 17:00 CEST: detail",
            id="reset-time-beats-reset-duration-and-reported-wait",
        ),
        pytest.param(
            {"type": "usage_limit_reached", "resets_in_seconds": 7200},
            10.0,
            ProviderUsageLimitError,
            None,
            "it resets at 2026-10-19 16:00 CEST: detail",
            id="reset-duration-beats-reported-wait",
        ),
        pytest.param(
            {"type": "usage_limit_reached"},
            20.0,
            ProviderRateLimitError,
            20.0,
            "in 20 seconds: detail",
            id="reported-wait-names-the-reset",
        ),
        pytest.param(
            {"type": "usage_limit_reached", "resets_at": "soon"},
            None,
            ProviderUsageLimitError,
            None,
            "Subscription usage limit reached; no reset time was reported: detail",
            id="no-usable-reset-stops",
        ),
    ],
)
def test_usage_limit_classifies_by_its_reported_reset(
    error: dict[str, Any],
    reported_wait: float | None,
    expected_type: type[ProviderError],
    retry_after: float | None,
    wording: str,
) -> None:
    classified = usage_limit_error(
        error,
        "detail",
        reported_wait=reported_wait,
        display_timezone=ZoneInfo("Europe/Berlin"),
        now=_NOW,
    )

    assert type(classified) is expected_type
    assert classified.retryable is (expected_type is ProviderRateLimitError)
    assert classified.retry_after == retry_after
    assert wording in str(classified)


def test_usage_limit_error_keeps_the_reported_reset_and_ignores_other_errors() -> None:
    resets_at = _NOW + timedelta(hours=3)
    classified = usage_limit_error(
        {"type": "usage_limit_reached", "resets_at": resets_at.timestamp()}, "detail", now=_NOW
    )

    assert isinstance(classified, ProviderUsageLimitError)
    assert classified.resets_at == resets_at
    assert "it resets at 2026-10-19 15:00 UTC" in str(classified)
    assert usage_limit_error({"type": "rate_limit_exceeded"}, "detail", now=_NOW) is None
