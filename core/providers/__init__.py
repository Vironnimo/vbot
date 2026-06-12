"""core.providers — provider configuration, registry, adapters, and error classes."""

from core.providers.accounts import (
    DEFAULT_ACCOUNT_ID,
    ProviderAccount,
    compose_connection_id,
    derive_credential_key,
    split_connection_id,
    validate_account_id,
)
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
from core.providers.minimax import MiniMaxAdapter
from core.providers.mistral import MistralAdapter
from core.providers.openai import OpenAIAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.providers import (
    AuthConfig,
    ConnectionConfig,
    ProviderConfig,
    ProviderRegistry,
)
from core.providers.reasoning import (
    REASONING_REPLAY_CURRENT_RUN,
    REASONING_REPLAY_FULL_HISTORY,
    REASONING_REPLAY_NONE,
    REASONING_REPLAY_POLICIES,
    ReasoningReplayPolicy,
)

__all__ = [
    "DEFAULT_ACCOUNT_ID",
    "REASONING_REPLAY_CURRENT_RUN",
    "REASONING_REPLAY_FULL_HISTORY",
    "REASONING_REPLAY_NONE",
    "REASONING_REPLAY_POLICIES",
    "AnthropicAdapter",
    "AuthConfig",
    "ConnectionConfig",
    "GitHubCopilotAdapter",
    "MiniMaxAdapter",
    "MistralAdapter",
    "OpenAIAdapter",
    "OpenAICompatibleAdapter",
    "OpenCodeGoAdapter",
    "OpenRouterAdapter",
    "ProviderAccount",
    "ProviderCredentialResolver",
    "ProviderAdapter",
    "ProviderAuthError",
    "ProviderConfig",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderRegistry",
    "ProviderStreamingUnsupportedError",
    "ProviderTimeoutError",
    "ReasoningReplayPolicy",
    "compose_connection_id",
    "derive_credential_key",
    "split_connection_id",
    "validate_account_id",
]
