"""Github copilot responses: stream errors behavior."""

from __future__ import annotations

import pytest

from core.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from tests.core.providers.github_copilot_responses_helpers import (
    _iter_deltas,
    _sse,
)


def test_stream_tolerates_unknown_events() -> None:
    lines = [_sse("response.unrecognized", {"type": "response.unrecognized", "value": 1})]

    assert list(_iter_deltas(lines)) == []


def test_stream_raises_provider_error_for_error_events() -> None:
    lines = [_sse("response.failed", {"error": {"message": "bad request"}})]

    with pytest.raises(ProviderError, match="bad request"):
        list(_iter_deltas(lines))


@pytest.mark.parametrize(
    ("event_name", "event_data", "expected_type", "retryable"),
    [
        pytest.param(
            "response.failed",
            {
                "type": "response.failed",
                "response": {
                    "status": "failed",
                    "error": {"code": "server_error", "message": "OpenAI failed."},
                },
            },
            ProviderError,
            True,
            id="openai-platform-transient-server",
        ),
        pytest.param(
            "response.failed",
            {
                "type": "response.failed",
                "response": {
                    "status": "failed",
                    "error": {"code": "invalid_prompt", "message": "Invalid prompt."},
                },
            },
            ProviderError,
            False,
            id="openai-platform-fatal-prompt",
        ),
        pytest.param(
            "error",
            {
                "type": "error",
                "code": "rate_limit_exceeded",
                "message": "Codex rate limit.",
            },
            ProviderRateLimitError,
            True,
            id="openai-codex-transient-rate-limit",
        ),
        pytest.param(
            "error",
            {
                "type": "error",
                "code": "invalid_api_key",
                "message": "Codex authentication failed.",
            },
            ProviderAuthError,
            False,
            id="openai-codex-fatal-auth",
        ),
        pytest.param(
            "response.failed",
            {
                "type": "response.failed",
                "response": {
                    "status": "failed",
                    "error": {"code": "server_error", "message": "Provider overloaded."},
                    "error_type": "provider_overloaded",
                },
            },
            ProviderError,
            True,
            id="openrouter-transient-overload",
        ),
        pytest.param(
            "response.failed",
            {
                "type": "response.failed",
                "response": {
                    "status": "failed",
                    "error": {"code": "server_error", "message": "Authentication failed."},
                    "error_type": "authentication",
                },
            },
            ProviderAuthError,
            False,
            id="openrouter-fatal-auth-overrides-native-code",
        ),
        pytest.param(
            "response.error",
            {
                "type": "response.error",
                "error": {"code": "timeout", "message": "Copilot timed out."},
            },
            ProviderTimeoutError,
            True,
            id="github-copilot-transient-timeout",
        ),
        pytest.param(
            "response.error",
            {
                "type": "response.error",
                "error": {"code": "invalid_request", "message": "Invalid request."},
            },
            ProviderError,
            False,
            id="github-copilot-fatal-request",
        ),
    ],
)
def test_stream_classifies_route_specific_error_frames(
    event_name,
    event_data,
    expected_type,
    retryable,
) -> None:
    with pytest.raises(ProviderError) as exc_info:
        list(_iter_deltas([_sse(event_name, event_data)]))

    assert type(exc_info.value) is expected_type
    assert exc_info.value.retryable is retryable


@pytest.mark.parametrize(
    ("reason", "expected_outcome"),
    [
        ("max_output_tokens", "output_truncated"),
        ("content_filter", "content_filtered"),
        ("provider_added_reason", "unknown"),
    ],
)
def test_stream_maps_incomplete_responses_to_safe_terminal_outcomes(
    reason,
    expected_outcome,
) -> None:
    lines = [
        _sse(
            "response.incomplete",
            {
                "type": "response.incomplete",
                "response": {
                    "status": "incomplete",
                    "incomplete_details": {"reason": reason},
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call_incomplete",
                            "name": "write",
                            "arguments": '{"path":"partial',
                        }
                    ],
                },
            },
        )
    ]

    deltas = list(_iter_deltas(lines))

    assert deltas[-1] == {"type": "finish", "reason": expected_outcome}


def test_stream_raises_provider_error_for_malformed_json() -> None:
    lines = ["event: response.output_text.delta\ndata: not-json\n\n"]

    with pytest.raises(ProviderError):
        list(_iter_deltas(lines))


def test_stream_raises_provider_error_for_non_object_json() -> None:
    lines = ["event: response.output_text.delta\ndata: []\n\n"]

    with pytest.raises(ProviderError):
        list(_iter_deltas(lines))
