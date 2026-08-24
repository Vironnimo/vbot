"""Tests for provider error classes and in-band error classification."""

import pytest

from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    classify_in_band_provider_error,
)
from core.utils.errors import ProviderError, VBotError


def test_network_error_is_vbot_error_not_provider_error_and_retryable() -> None:
    """NetworkError must not participate in provider fallback classification."""
    error = NetworkError("network down")

    assert isinstance(error, VBotError)
    assert not isinstance(error, ProviderError)
    assert error.retryable is True


# ---------------------------------------------------------------------------
# classify_in_band_provider_error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        ({"message": "bad key", "code": 401}, ProviderAuthError),
        (
            {"message": "no access", "metadata": {"error_type": "authentication"}},
            ProviderAuthError,
        ),
        (
            {"message": "slow down", "error_type": "rate_limit_exceeded"},
            ProviderRateLimitError,
        ),
        ({"message": "throttled", "code": 429}, ProviderRateLimitError),
        ({"message": "deadline", "error_type": "timeout"}, ProviderTimeoutError),
        ({"message": "gateway gone", "code": 502}, ProviderError),
        (
            {"message": "overloaded", "metadata": {"error_type": "provider_overloaded"}},
            ProviderError,
        ),
    ],
)
def test_classifies_known_codes_into_shared_taxonomy(payload, expected_type) -> None:
    classified = classify_in_band_provider_error(payload)

    assert isinstance(classified, expected_type)


def test_transient_server_error_is_retryable() -> None:
    classified = classify_in_band_provider_error(
        {"message": "boom", "metadata": {"error_type": "server"}}
    )

    assert isinstance(classified, ProviderError)
    assert classified.retryable is True


def test_availability_retryable_hint_upgrades_unknown_error() -> None:
    payload = {
        "message": "all providers busy",
        "availability": {"retryable": True, "retry_after": 30},
    }

    classified = classify_in_band_provider_error(payload)

    assert isinstance(classified, ProviderError)
    assert classified.retryable is True
    assert classified.retry_after == 30


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "too long", "error_type": "context_length_exceeded"},
        {"message": "capped", "error_type": "max_tokens_exceeded"},
        {"message": "blocked", "metadata": {"error_type": "content_policy_violation"}},
        {"message": "no funds", "code": 402, "error_type": "payment_required"},
    ],
)
def test_fatal_classes_stay_non_retryable_even_lenient(payload) -> None:
    strict = classify_in_band_provider_error(payload)
    lenient = classify_in_band_provider_error(payload, lenient_unknown=True)

    for classified in (strict, lenient):
        assert isinstance(classified, ProviderError)
        assert classified.retryable is False


def test_unknown_error_is_fatal_by_default_and_retryable_when_lenient() -> None:
    payload = {
        "message": "assistant messages require content, reasoning, reasoning_meta, or tool_calls",
        "code": 400,
    }

    strict = classify_in_band_provider_error(payload)
    lenient = classify_in_band_provider_error(payload, lenient_unknown=True)

    assert isinstance(strict, ProviderError)
    assert strict.retryable is False
    assert isinstance(lenient, ProviderError)
    assert lenient.retryable is True


def test_string_code_falls_back_to_classifier() -> None:
    classified = classify_in_band_provider_error(
        {"message": "nope", "code": "permission_denied"},
        lenient_unknown=True,
    )

    assert isinstance(classified, ProviderError)
    assert classified.retryable is False


def test_non_mapping_payload_stays_fatal() -> None:
    classified = classify_in_band_provider_error("quota exceeded")

    assert isinstance(classified, ProviderError)
    assert classified.retryable is False
