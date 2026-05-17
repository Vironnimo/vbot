"""Tests for provider error classes."""

from core.providers.errors import NetworkError
from core.utils.errors import ProviderError, VBotError


def test_network_error_is_vbot_error_not_provider_error_and_retryable() -> None:
    """NetworkError must not participate in provider fallback classification."""
    error = NetworkError("network down")

    assert isinstance(error, VBotError)
    assert not isinstance(error, ProviderError)
    assert error.retryable is True
