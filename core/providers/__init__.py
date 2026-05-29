"""core.providers — provider configuration, registry, adapters, and error classes."""

from core.providers.adapter import ProviderAdapter
from core.providers.anthropic import AnthropicAdapter
from core.providers.credentials import ProviderCredentialResolver
from core.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderStreamingUnsupportedError,
    ProviderTimeoutError,
)
from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.mistral import MistralAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.providers import (
    AuthConfig,
    ConnectionConfig,
    ProviderConfig,
    ProviderRegistry,
)

__all__ = [
    "AnthropicAdapter",
    "AuthConfig",
    "ConnectionConfig",
    "GitHubCopilotAdapter",
    "MistralAdapter",
    "OpenAICompatibleAdapter",
    "OpenCodeGoAdapter",
    "OpenRouterAdapter",
    "ProviderCredentialResolver",
    "ProviderAdapter",
    "ProviderAuthError",
    "ProviderConfig",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderRegistry",
    "ProviderStreamingUnsupportedError",
    "ProviderTimeoutError",
]
