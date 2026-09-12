"""Tests for runtime adapter wiring."""

from pathlib import Path

import pytest

from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.providers.accounts import ConnectionRef
from core.providers.anthropic import AnthropicAdapter
from core.providers.credentials import ProviderCredentialResolver
from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.github_copilot_policy import RESPONSES_ENDPOINT
from core.providers.kimi import KIMI_CODING_MODE, KimiAdapter
from core.providers.minimax import MiniMaxAdapter
from core.providers.mistral import MistralAdapter
from core.providers.nous import NousAdapter
from core.providers.openai import CODEX_RESPONSES_MODE, OpenAIAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.opencode_zen import OpenCodeZenAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig, ProviderRegistry
from core.providers.stepfun import STEPFUN_PLAN_MODE, StepFunAdapter
from core.providers.xai import XAIAdapter
from core.runtime.runtime import Runtime
from core.utils.config import Config
from tests.core.runtime.runtime_providers_test_support import (
    runtime as runtime,
)


def test_runtime_get_adapter_selects_opencode_go_adapter_from_provider_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """opencode_go provider adapter keys resolve to OpenCodeGoAdapter at runtime."""
    # Arrange
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "opencode-go-token")
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()

    # Act
    adapter = runtime.get_adapter(ConnectionRef("opencode-go", "opencode-go:api-key"))

    # Assert
    assert runtime.providers.get("opencode-go").adapter == "opencode_go"
    assert isinstance(adapter, OpenCodeGoAdapter)


def test_runtime_get_adapter_selects_opencode_zen_adapter_from_explicit_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-zen-token")
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()

    adapter = runtime.get_adapter(ConnectionRef("opencode-zen", "opencode-zen:api-key"))

    assert runtime.providers.get("opencode-zen").adapter == "opencode_zen"
    assert isinstance(adapter, OpenCodeZenAdapter)
    assert adapter._model_lookup is not None  # type: ignore[attr-defined]


def test_runtime_wires_opencode_go_adapter_with_model_lookup(runtime: Runtime) -> None:
    """OpenCodeGo adapters receive a runtime-backed model lookup."""
    # Arrange
    provider_config = ProviderConfig(
        id="opencode-go",
        name="OpenCode Go",
        adapter="opencode_go",
        base_url="https://api.opencodego.com/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENCODE_GO_API_KEY",
                ),
            )
        ],
    )
    runtime._providers = ProviderRegistry({"opencode-go": provider_config})  # type: ignore[attr-defined]
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        runtime.providers,
        process_env={"OPENCODE_GO_API_KEY": "opencode-go-token"},
    )
    runtime._models = ModelRegistry({})  # type: ignore[attr-defined]

    # Act
    adapter = runtime.get_adapter(ConnectionRef("opencode-go", "opencode-go:api-key"))

    # Assert
    assert isinstance(adapter, OpenCodeGoAdapter)
    assert adapter._model_lookup is not None  # type: ignore[attr-defined]


def test_runtime_wires_openai_compatible_adapter_with_model_lookup(runtime: Runtime) -> None:
    """OpenAI-compatible adapters receive a runtime-backed model lookup."""
    # Arrange
    provider_config = ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai_compatible",
        base_url="https://api.openai.com/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENAI_API_KEY",
                ),
            )
        ],
    )
    runtime._providers = ProviderRegistry({"openai": provider_config})  # type: ignore[attr-defined]
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        runtime.providers,
        process_env={"OPENAI_API_KEY": "openai-token"},
    )
    runtime._models = ModelRegistry({})  # type: ignore[attr-defined]

    # Act
    adapter = runtime.get_adapter(ConnectionRef("openai", "openai:api-key"))

    # Assert
    assert isinstance(adapter, OpenAICompatibleAdapter)
    assert adapter._model_lookup is not None  # type: ignore[attr-defined]


def test_runtime_openai_codex_connection_uses_codex_responses_mode(
    runtime: Runtime,
) -> None:
    """``openai:subscription`` resolves to OpenAIAdapter with codex_responses mode."""
    # Arrange
    provider_config = ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai",
        base_url="https://chatgpt.com/backend-api",
        connections=[
            ConnectionConfig(
                id="subscription",
                type="api_key",
                label="ChatGPT Plus/Pro (test token)",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENAI_CODEX_TEST_TOKEN",
                ),
                mode=CODEX_RESPONSES_MODE,
            )
        ],
    )
    runtime._providers = ProviderRegistry({"openai": provider_config})  # type: ignore[attr-defined]
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        runtime.providers,
        process_env={"OPENAI_CODEX_TEST_TOKEN": "header.payload.signature"},
    )
    runtime._models = ModelRegistry({})  # type: ignore[attr-defined]

    # Act
    adapter = runtime.get_adapter(ConnectionRef("openai", "openai:subscription"))

    # Assert
    assert isinstance(adapter, OpenAIAdapter)
    assert adapter._connection_mode == CODEX_RESPONSES_MODE  # type: ignore[attr-defined]
    assert adapter._model_lookup is not None  # type: ignore[attr-defined]


def test_runtime_openai_api_key_connection_uses_default_mode(runtime: Runtime) -> None:
    """``openai:api-key`` resolves to OpenAIAdapter with no connection mode set."""
    # Arrange
    provider_config = ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai",
        base_url="https://api.openai.com/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENAI_API_KEY",
                ),
            )
        ],
    )
    runtime._providers = ProviderRegistry({"openai": provider_config})  # type: ignore[attr-defined]
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        runtime.providers,
        process_env={"OPENAI_API_KEY": "sk-test"},
    )
    runtime._models = ModelRegistry({})  # type: ignore[attr-defined]

    # Act
    adapter = runtime.get_adapter(ConnectionRef("openai", "openai:api-key"))

    # Assert
    assert isinstance(adapter, OpenAIAdapter)
    assert adapter._connection_mode is None  # type: ignore[attr-defined]
    assert adapter._model_lookup is not None  # type: ignore[attr-defined]


def test_runtime_wires_anthropic_adapter_with_model_lookup(runtime: Runtime) -> None:
    """Anthropic adapters get a provider-scoped runtime model lookup."""
    # Arrange
    provider_config = ProviderConfig(
        id="anthropic",
        name="Anthropic",
        adapter="anthropic",
        base_url="https://api.anthropic.com/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="x-api-key",
                    prefix="",
                    credential_key="ANTHROPIC_API_KEY",
                ),
            )
        ],
    )
    runtime._providers = ProviderRegistry({"anthropic": provider_config})  # type: ignore[attr-defined]
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        runtime.providers,
        process_env={"ANTHROPIC_API_KEY": "anthropic-token"},
    )
    anthropic_model = Model(
        model_id="shared-model-id",
        name="Anthropic Shared Model",
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=True),
        ),
        context_window=200000,
        max_output_tokens=8192,
        metadata={},
    )
    runtime._models = ModelRegistry(  # type: ignore[attr-defined]
        {
            ("anthropic", "shared-model-id"): anthropic_model,
            ("openrouter", "shared-model-id"): Model(
                model_id="shared-model-id",
                name="OpenRouter Shared Model",
                capabilities=Capabilities(
                    vision=True,
                    tools=True,
                    json_mode=True,
                    reasoning=ReasoningCapabilities(supported=False),
                ),
                context_window=128000,
                max_output_tokens=4096,
                metadata={},
            ),
            ("openrouter", "openrouter-only-model"): Model(
                model_id="openrouter-only-model",
                name="OpenRouter Only Model",
                capabilities=Capabilities(
                    vision=False,
                    tools=True,
                    json_mode=True,
                    reasoning=ReasoningCapabilities(supported=False),
                ),
                context_window=64000,
                max_output_tokens=4096,
                metadata={},
            ),
        }
    )

    # Act
    adapter = runtime.get_adapter(ConnectionRef("anthropic", "anthropic:api-key"))

    # Assert
    assert isinstance(adapter, AnthropicAdapter)
    lookup = adapter._model_lookup  # type: ignore[attr-defined]
    assert lookup is not None
    assert lookup("shared-model-id") == anthropic_model
    assert lookup("openrouter-only-model") is None


def test_runtime_wires_copilot_adapter_with_model_metadata_lookup(runtime: Runtime) -> None:
    """Copilot adapters receive a narrow runtime metadata lookup."""
    # Arrange
    provider_config = ProviderConfig(
        id="github-copilot",
        name="GitHub Copilot",
        adapter="github_copilot",
        base_url="https://api.githubcopilot.com",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="GITHUB_COPILOT_TOKEN",
                ),
            )
        ],
    )
    runtime._providers = ProviderRegistry({"github-copilot": provider_config})  # type: ignore[attr-defined]
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        runtime.providers,
        process_env={"GITHUB_COPILOT_TOKEN": "copilot-token"},
    )
    runtime._models = ModelRegistry(  # type: ignore[attr-defined]
        {
            ("github-copilot", "gpt-test"): Model(
                model_id="gpt-test",
                name="GPT Test",
                capabilities=Capabilities(
                    vision=False,
                    tools=True,
                    json_mode=True,
                    reasoning=ReasoningCapabilities(supported=True),
                ),
                context_window=128000,
                max_output_tokens=4096,
                metadata={
                    "github_copilot": {
                        "vendor": "OpenAI",
                        "family": "gpt-test",
                        "supported_endpoints": [RESPONSES_ENDPOINT],
                        "reasoning_efforts": ["low", "medium", "high"],
                        "tool_calls": True,
                        "structured_outputs": True,
                    }
                },
            )
        }
    )

    # Act
    adapter = runtime.get_adapter(ConnectionRef("github-copilot", "github-copilot:api-key"))

    # Assert
    assert isinstance(adapter, GitHubCopilotAdapter)
    assert adapter._policy_for_model("gpt-test").endpoint_path == RESPONSES_ENDPOINT  # type: ignore[attr-defined]


def test_runtime_copilot_metadata_lookup_falls_back_for_unknown_model(runtime: Runtime) -> None:
    """Unknown Copilot model IDs use conservative policy instead of failing."""
    # Arrange
    provider_config = ProviderConfig(
        id="github-copilot",
        name="GitHub Copilot",
        adapter="github_copilot",
        base_url="https://api.githubcopilot.com",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="GITHUB_COPILOT_TOKEN",
                ),
            )
        ],
    )
    runtime._providers = ProviderRegistry({"github-copilot": provider_config})  # type: ignore[attr-defined]
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        runtime.providers,
        process_env={"GITHUB_COPILOT_TOKEN": "copilot-token"},
    )
    runtime._models = ModelRegistry({})  # type: ignore[attr-defined]

    # Act
    adapter = runtime.get_adapter(ConnectionRef("github-copilot", "github-copilot:api-key"))

    # Assert
    assert isinstance(adapter, GitHubCopilotAdapter)
    unknown_policy = adapter._policy_for_model("unknown-model")  # type: ignore[attr-defined]
    assert unknown_policy.endpoint_path == "/chat/completions"
    assert unknown_policy.supports_tools is False


def test_runtime_wires_mistral_adapter_with_model_lookup_for_reasoning_suppression(
    runtime: Runtime,
) -> None:
    """Mistral reasoning suppression is driven by runtime-backed model lookup."""
    # Arrange
    provider_config = ProviderConfig(
        id="mistral",
        name="Mistral AI",
        adapter="mistral",
        base_url="https://api.mistral.ai/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="MISTRAL_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
    )
    runtime._providers = ProviderRegistry({"mistral": provider_config})  # type: ignore[attr-defined]
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        runtime.providers,
        process_env={"MISTRAL_API_KEY": "mistral-token"},
    )
    runtime._models = ModelRegistry(  # type: ignore[attr-defined]
        {
            ("mistral", "mistral-medium-latest"): Model(
                model_id="mistral-medium-latest",
                name="Mistral Medium",
                capabilities=Capabilities(
                    vision=False,
                    tools=True,
                    json_mode=True,
                    reasoning=ReasoningCapabilities(supported=False),
                ),
                context_window=128000,
                max_output_tokens=8192,
                metadata={},
            )
        }
    )

    # Act
    adapter = runtime.get_adapter(ConnectionRef("mistral", "mistral:api-key"))

    # Assert
    assert isinstance(adapter, MistralAdapter)
    payload = adapter._build_payload(
        [{"role": "user", "content": "Hello"}],
        "mistral-medium-latest",
        thinking_effort="high",
    )
    assert "reasoning_effort" not in payload
    assert "prompt_mode" not in payload


def test_runtime_wires_minimax_adapter(runtime: Runtime) -> None:
    """MiniMax provider configs resolve to the MiniMax adapter."""
    # Arrange
    provider_config = ProviderConfig(
        id="minimax",
        name="MiniMax",
        adapter="minimax",
        base_url="https://api.minimax.io/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API / Token Plan Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="MINIMAX_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
    )
    runtime._providers = ProviderRegistry({"minimax": provider_config})  # type: ignore[attr-defined]
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        runtime.providers,
        process_env={"MINIMAX_API_KEY": "minimax-token"},
    )
    runtime._models = ModelRegistry({})  # type: ignore[attr-defined]

    # Act
    adapter = runtime.get_adapter(ConnectionRef("minimax", "minimax:api-key"))

    # Assert
    assert isinstance(adapter, MiniMaxAdapter)


def test_runtime_wires_xai_adapter(runtime: Runtime) -> None:
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        runtime.providers,
        process_env={"XAI_API_KEY": "xai-token"},
    )

    adapter = runtime.get_adapter(ConnectionRef("xai", "xai:api-key"))

    assert isinstance(adapter, XAIAdapter)


def test_runtime_wires_nous_adapter(runtime: Runtime) -> None:
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        runtime.providers,
        process_env={"NOUS_API_KEY": "nous-token"},
    )

    adapter = runtime.get_adapter(ConnectionRef("nous", "nous:api-key"))

    assert isinstance(adapter, NousAdapter)


def test_runtime_wires_stepfun_adapter_and_explicit_connection_mode(runtime: Runtime) -> None:
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        runtime.providers,
        process_env={"STEPFUN_API_KEY": "step-plan-token"},
    )

    adapter = runtime.get_adapter(ConnectionRef("stepfun", "stepfun:step-plan"))

    assert isinstance(adapter, StepFunAdapter)
    assert adapter._connection_mode == STEPFUN_PLAN_MODE  # type: ignore[attr-defined]
    assert str(adapter._client.base_url) == "https://api.stepfun.com/step_plan/v1/"  # type: ignore[attr-defined]


def test_runtime_wires_kimi_adapter_and_connection_mode(runtime: Runtime) -> None:
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        runtime.providers,
        process_env={"KIMI_CODING_API_KEY": "kimi-token"},
    )

    adapter = runtime.get_adapter(ConnectionRef("kimi", "kimi:coding-plan"))

    assert isinstance(adapter, KimiAdapter)
    assert adapter._connection_mode == KIMI_CODING_MODE  # type: ignore[attr-defined]
