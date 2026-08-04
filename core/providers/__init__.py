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
from core.providers.anthropic_compatible import AnthropicCompatibleAdapter
from core.providers.credentials import ProviderCredentialResolver
from core.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderOutcomeUnknownError,
    ProviderRateLimitError,
    ProviderStreamingUnsupportedError,
    ProviderTimeoutError,
)
from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.kimi import KimiAdapter
from core.providers.lmstudio import LMStudioAdapter
from core.providers.minimax import MiniMaxAdapter
from core.providers.mistral import MistralAdapter
from core.providers.nous import NousAdapter
from core.providers.ollama import OllamaAdapter
from core.providers.openai import OpenAIAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.opencode_zen import OpenCodeZenAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.providers import (
    GLOBAL_CONTEXT_WINDOW_FLOOR,
    LOCAL_CONTEXT_DEFAULT_CAP,
    AuthConfig,
    ConnectionConfig,
    ProviderConfig,
    ProviderRegistry,
    connection_default_enabled,
    model_is_local,
    resolve_context_window,
    resolve_effective_context_window,
)
from core.providers.reasoning import (
    REASONING_REPLAY_CURRENT_RUN,
    REASONING_REPLAY_FULL_HISTORY,
    REASONING_REPLAY_NONE,
    REASONING_REPLAY_POLICIES,
    ReasoningReplayPolicy,
)
from core.providers.stepfun import StepFunAdapter
from core.providers.xai import XAIAdapter

__all__ = [
    "DEFAULT_ACCOUNT_ID",
    "GLOBAL_CONTEXT_WINDOW_FLOOR",
    "LOCAL_CONTEXT_DEFAULT_CAP",
    "REASONING_REPLAY_CURRENT_RUN",
    "REASONING_REPLAY_FULL_HISTORY",
    "REASONING_REPLAY_NONE",
    "REASONING_REPLAY_POLICIES",
    "AnthropicAdapter",
    "AnthropicCompatibleAdapter",
    "AuthConfig",
    "ConnectionConfig",
    "GitHubCopilotAdapter",
    "KimiAdapter",
    "LMStudioAdapter",
    "MiniMaxAdapter",
    "MistralAdapter",
    "NousAdapter",
    "OllamaAdapter",
    "OpenAIAdapter",
    "OpenAICompatibleAdapter",
    "OpenCodeGoAdapter",
    "OpenCodeZenAdapter",
    "OpenRouterAdapter",
    "XAIAdapter",
    "ProviderAccount",
    "ProviderCredentialResolver",
    "ProviderAdapter",
    "ProviderAuthError",
    "ProviderConfig",
    "ProviderError",
    "ProviderOutcomeUnknownError",
    "ProviderRateLimitError",
    "ProviderRegistry",
    "ProviderStreamingUnsupportedError",
    "ProviderTimeoutError",
    "ReasoningReplayPolicy",
    "StepFunAdapter",
    "compose_connection_id",
    "connection_default_enabled",
    "derive_credential_key",
    "model_is_local",
    "resolve_context_window",
    "resolve_effective_context_window",
    "split_connection_id",
    "validate_account_id",
]
