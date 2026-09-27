"""Provider error taxonomy and in-band Provider error classification."""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    classify_in_band_provider_error,
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
            {"message": "too long", "error_type": "context_length_exceeded"},
            True,
            ProviderError,
            False,
            None,
            id="fatal-type-even-lenient",
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
