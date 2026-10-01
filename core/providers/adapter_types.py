"""The Adapter class each Provider ``adapter`` selector names.

Kept apart from :mod:`core.providers.runtime` so wire clients can resolve an
Adapter's class-level facts (such as its OpenAI-compatible base URL) without
importing the runtime's storage and Model DB machinery.
"""

from __future__ import annotations

from core.providers.adapter import ProviderAdapter
from core.providers.anthropic import AnthropicAdapter
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
from core.providers.stepfun import StepFunAdapter
from core.providers.xai import XAIAdapter

ADAPTER_TYPES: dict[str, type[ProviderAdapter]] = {
    "openai_compatible": OpenAICompatibleAdapter,
    "openai": OpenAIAdapter,
    "openrouter": OpenRouterAdapter,
    "kimi": KimiAdapter,
    "minimax": MiniMaxAdapter,
    "mistral": MistralAdapter,
    "nous": NousAdapter,
    "stepfun": StepFunAdapter,
    "opencode_go": OpenCodeGoAdapter,
    "opencode_zen": OpenCodeZenAdapter,
    "github_copilot": GitHubCopilotAdapter,
    "anthropic": AnthropicAdapter,
    "ollama": OllamaAdapter,
    "ollama_cloud": OllamaCloudAdapter,
    "lmstudio": LMStudioAdapter,
    "xai": XAIAdapter,
}


def openai_compatible_base_url(adapter: str, base_url: str) -> str:
    """Return the OpenAI-compatible API base for a Provider's configured base URL.

    An unknown selector keeps the configured base, the convention of every
    OpenAI-compatible Provider.
    """

    adapter_class = ADAPTER_TYPES.get(adapter)
    return adapter_class.openai_compatible_base_url(base_url) if adapter_class else base_url
