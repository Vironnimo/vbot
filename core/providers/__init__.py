"""core.providers — provider configuration, registry, adapters, and error classes.

Exports load on first access, so importing one provider module does not import every adapter.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.providers.accounts import (
        DEFAULT_ACCOUNT_ID,
        ConnectionRef,
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
    from core.providers.ollama import OllamaAdapter, OllamaCloudAdapter
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
        DEFAULT_REASONING_REPLAY_POLICY,
        REASONING_REPLAY_CURRENT_RUN,
        REASONING_REPLAY_FULL_HISTORY,
        REASONING_REPLAY_NONE,
        REASONING_REPLAY_POLICIES,
        REASONING_REPLAY_TOOL_TURNS,
        ReasoningReplayPolicy,
    )
    from core.providers.stepfun import StepFunAdapter
    from core.providers.xai import XAIAdapter


_EXPORT_MODULES = {
    "AnthropicAdapter": "core.providers.anthropic",
    "AnthropicCompatibleAdapter": "core.providers.anthropic_compatible",
    "AuthConfig": "core.providers.providers",
    "ConnectionConfig": "core.providers.providers",
    "ConnectionRef": "core.providers.accounts",
    "DEFAULT_ACCOUNT_ID": "core.providers.accounts",
    "DEFAULT_REASONING_REPLAY_POLICY": "core.providers.reasoning",
    "GLOBAL_CONTEXT_WINDOW_FLOOR": "core.providers.providers",
    "GitHubCopilotAdapter": "core.providers.github_copilot",
    "KimiAdapter": "core.providers.kimi",
    "LMStudioAdapter": "core.providers.lmstudio",
    "LOCAL_CONTEXT_DEFAULT_CAP": "core.providers.providers",
    "MiniMaxAdapter": "core.providers.minimax",
    "MistralAdapter": "core.providers.mistral",
    "NousAdapter": "core.providers.nous",
    "OllamaAdapter": "core.providers.ollama",
    "OllamaCloudAdapter": "core.providers.ollama",
    "OpenAIAdapter": "core.providers.openai",
    "OpenAICompatibleAdapter": "core.providers.openai_compatible",
    "OpenCodeGoAdapter": "core.providers.opencode_go",
    "OpenCodeZenAdapter": "core.providers.opencode_zen",
    "OpenRouterAdapter": "core.providers.openrouter",
    "ProviderAccount": "core.providers.accounts",
    "ProviderAdapter": "core.providers.adapter",
    "ProviderAuthError": "core.providers.errors",
    "ProviderConfig": "core.providers.providers",
    "ProviderCredentialResolver": "core.providers.credentials",
    "ProviderError": "core.providers.errors",
    "ProviderOutcomeUnknownError": "core.providers.errors",
    "ProviderRateLimitError": "core.providers.errors",
    "ProviderRegistry": "core.providers.providers",
    "ProviderStreamingUnsupportedError": "core.providers.errors",
    "ProviderTimeoutError": "core.providers.errors",
    "REASONING_REPLAY_CURRENT_RUN": "core.providers.reasoning",
    "REASONING_REPLAY_FULL_HISTORY": "core.providers.reasoning",
    "REASONING_REPLAY_NONE": "core.providers.reasoning",
    "REASONING_REPLAY_POLICIES": "core.providers.reasoning",
    "REASONING_REPLAY_TOOL_TURNS": "core.providers.reasoning",
    "ReasoningReplayPolicy": "core.providers.reasoning",
    "StepFunAdapter": "core.providers.stepfun",
    "XAIAdapter": "core.providers.xai",
    "compose_connection_id": "core.providers.accounts",
    "connection_default_enabled": "core.providers.providers",
    "derive_credential_key": "core.providers.accounts",
    "model_is_local": "core.providers.providers",
    "resolve_context_window": "core.providers.providers",
    "resolve_effective_context_window": "core.providers.providers",
    "split_connection_id": "core.providers.accounts",
    "validate_account_id": "core.providers.accounts",
}


def __getattr__(name: str) -> Any:
    """Load public provider exports only when a caller requests them."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "DEFAULT_ACCOUNT_ID",
    "DEFAULT_REASONING_REPLAY_POLICY",
    "GLOBAL_CONTEXT_WINDOW_FLOOR",
    "LOCAL_CONTEXT_DEFAULT_CAP",
    "REASONING_REPLAY_CURRENT_RUN",
    "REASONING_REPLAY_FULL_HISTORY",
    "REASONING_REPLAY_NONE",
    "REASONING_REPLAY_POLICIES",
    "REASONING_REPLAY_TOOL_TURNS",
    "ConnectionRef",
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
    "OllamaCloudAdapter",
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
