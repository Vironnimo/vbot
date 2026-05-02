"""core.providers — provider configuration, registry, adapters, and error classes."""

from core.providers.adapter import ProviderAdapter
from core.providers.anthropic import AnthropicAdapter
from core.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ProviderConfig, ProviderRegistry

__all__ = [
    "AnthropicAdapter",
    "AuthConfig",
    "OpenAICompatibleAdapter",
    "ProviderAdapter",
    "ProviderAuthError",
    "ProviderConfig",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderRegistry",
    "ProviderTimeoutError",
]
