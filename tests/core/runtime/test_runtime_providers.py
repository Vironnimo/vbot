"""Integration tests for Runtime loading providers and models from resources.

Verifies that ``Runtime.start()`` loads ``ProviderRegistry`` and
``ModelRegistry`` from the ``resources/`` directory, and that the
registries contain the expected data.
"""

from pathlib import Path

import pytest

from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.providers.anthropic import AnthropicAdapter
from core.providers.credentials import ProviderCredentialResolver
from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.github_copilot_policy import RESPONSES_ENDPOINT
from core.providers.mistral import MistralAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig, ProviderRegistry
from core.runtime.runtime import Runtime
from core.utils.config import Config
from core.utils.errors import ConfigError


@pytest.fixture
def runtime(tmp_path: Path) -> Runtime:
    """Provide a started Runtime instance loaded from resources."""
    config = Config(data_dir=tmp_path / "data")
    runtime = Runtime(config)
    runtime.start()
    return runtime


# ------------------------------------------------------------------
# Provider registry loading
# ------------------------------------------------------------------


def test_runtime_loads_providers(runtime: Runtime) -> None:
    """Runtime.start() loads a ProviderRegistry from resources/."""
    # Assert
    assert runtime.providers is not None
    assert isinstance(runtime.providers, ProviderRegistry)


def test_runtime_providers_populated(runtime: Runtime) -> None:
    """The provider registry contains the expected provider IDs."""
    # Assert
    ids = runtime.providers.list_ids()
    assert "openai" in ids
    assert "anthropic" in ids
    assert "openrouter" in ids


def test_runtime_provider_config_fields(runtime: Runtime) -> None:
    """Provider configs have the expected field values."""
    # Act
    openai_config = runtime.providers.get("openai")
    openrouter_config = runtime.providers.get("openrouter")
    github_copilot_config = runtime.providers.get("github-copilot")

    # Assert
    assert openai_config.id == "openai"
    assert openai_config.name == "OpenAI"
    assert openai_config.adapter == "openai_compatible"
    assert openai_config.base_url == "https://api.openai.com/v1"
    assert [connection.id for connection in openai_config.connections] == [
        "oauth",
        "api-key",
    ]
    assert openai_config.get_connection("api-key").auth.credential_key == "OPENAI_API_KEY"
    assert openrouter_config.adapter == "openrouter"
    assert github_copilot_config.adapter == "github_copilot"


def test_provider_credential_resolver_has_credentials_for_connection(
    tmp_path: Path,
) -> None:
    """Per-connection credential checks use the connection auth config."""
    # Arrange
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()

    # Act / Assert
    assert runtime.provider_credentials.has_credentials("openai", "openai:api-key") is False
    resolver = ProviderCredentialResolver(
        runtime.providers,
        process_env={"OPENAI_API_KEY": "sk-test"},
    )
    assert resolver.has_credentials("openai", "openai:api-key") is True


def test_provider_credential_resolver_get_credentials_for_connection(
    tmp_path: Path,
) -> None:
    """Per-connection credential lookup returns the matching credential value."""
    # Arrange
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()
    resolver = ProviderCredentialResolver(
        runtime.providers,
        process_env={"OPENAI_API_KEY": "sk-test"},
    )

    # Act
    credential = resolver.get_credentials("openai", "openai:api-key")

    # Assert
    assert credential == "sk-test"


def test_provider_credential_resolver_get_connection_missing_credentials(
    tmp_path: Path,
) -> None:
    """Per-connection credential lookup raises ConfigError when missing."""
    # Arrange
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()
    resolver = ProviderCredentialResolver(runtime.providers, process_env={})

    # Act / Assert
    with pytest.raises(ConfigError, match="Provider credentials not found"):
        resolver.get_credentials("openai", "openai:api-key")


def test_provider_credential_resolver_connection_missing_from_env_and_fallback(
    tmp_path: Path,
) -> None:
    """A credential absent from process env and fallback is not usable."""
    # Arrange
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()
    resolver = ProviderCredentialResolver(
        runtime.providers,
        process_env={},
        fallback_credentials={"OTHER_KEY": "other-value"},
    )

    # Act / Assert
    assert resolver.has_credentials("openai", "openai:api-key") is False
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        resolver.get_credentials("openai", "openai:api-key")


def test_provider_credential_resolver_provider_level_delegates_to_first_usable(
    tmp_path: Path,
) -> None:
    """Provider-level lookups return the first usable connection in config order."""
    # Arrange
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()
    resolver = ProviderCredentialResolver(
        runtime.providers,
        process_env={
            "OPENAI_OAUTH_TOKEN": "oauth-token",
            "OPENAI_API_KEY": "api-key",
        },
    )

    # Act / Assert
    assert resolver.has_credentials("openai") is True
    assert resolver.get_credentials("openai") == "oauth-token"


def test_provider_credential_resolver_provider_level_skips_unusable_connection(
    tmp_path: Path,
) -> None:
    """Provider-level lookup skips missing credentials and uses the next usable connection."""
    # Arrange
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()
    resolver = ProviderCredentialResolver(
        runtime.providers,
        process_env={"OPENAI_API_KEY": "api-key"},
    )

    # Act / Assert
    assert resolver.has_credentials("openai") is True
    assert resolver.get_credentials("openai") == "api-key"


def test_provider_credential_resolver_unknown_connection_id_raises_config_error(
    tmp_path: Path,
) -> None:
    """Unknown connection IDs raise ConfigError."""
    # Arrange
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()

    # Act / Assert
    with pytest.raises(ConfigError, match="Unknown connection id"):
        runtime.provider_credentials.has_credentials("openai", "openai:missing")


# ------------------------------------------------------------------
# Model registry loading
# ------------------------------------------------------------------


def test_runtime_loads_models(runtime: Runtime) -> None:
    """Runtime.start() loads a ModelRegistry from resources/."""
    # Assert
    assert runtime.models is not None
    assert isinstance(runtime.models, ModelRegistry)


def test_runtime_models_populated(runtime: Runtime) -> None:
    """The model registry contains models from all providers."""
    # Act
    openai_models = runtime.models.list_for_provider("openai")
    anthropic_models = runtime.models.list_for_provider("anthropic")
    openrouter_models = runtime.models.list_for_provider("openrouter")

    # Assert
    assert len(openai_models) > 0
    assert len(anthropic_models) > 0
    assert len(openrouter_models) > 0


def test_runtime_model_fields(runtime: Runtime) -> None:
    """Model entries have the expected field values."""
    # Act
    model = runtime.models.get("anthropic", "claude-sonnet-4-20250219")

    # Assert
    assert model.model_id == "claude-sonnet-4-20250219"
    assert model.name == "Claude Sonnet 4"
    assert model.context_window == 200000
    assert model.capabilities.vision is True
    assert model.capabilities.reasoning.supported is True


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
    adapter = runtime.get_adapter("opencode-go", "opencode-go:api-key")

    # Assert
    assert runtime.providers.get("opencode-go").adapter == "opencode_go"
    assert isinstance(adapter, OpenCodeGoAdapter)


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
    adapter = runtime.get_adapter("opencode-go", "opencode-go:api-key")

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
    adapter = runtime.get_adapter("openai", "openai:api-key")

    # Assert
    assert isinstance(adapter, OpenAICompatibleAdapter)
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
    adapter = runtime.get_adapter("anthropic", "anthropic:api-key")

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
    adapter = runtime.get_adapter("github-copilot", "github-copilot:api-key")

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
    adapter = runtime.get_adapter("github-copilot", "github-copilot:api-key")

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
    adapter = runtime.get_adapter("mistral", "mistral:api-key")

    # Assert
    assert isinstance(adapter, MistralAdapter)
    payload = adapter._build_payload(
        [{"role": "user", "content": "Hello"}],
        "mistral-medium-latest",
        thinking_effort="high",
    )
    assert "reasoning_effort" not in payload
    assert "prompt_mode" not in payload


# ------------------------------------------------------------------
# Error cases: registries not accessible before start
# ------------------------------------------------------------------


def test_providers_not_accessible_before_start(tmp_path: Path) -> None:
    """Accessing providers before start() raises RuntimeError."""
    # Arrange
    config = Config(data_dir=tmp_path / "data")
    runtime = Runtime(config)

    # Act & Assert
    with pytest.raises(RuntimeError, match="not started"):
        _ = runtime.providers


def test_models_not_accessible_before_start(tmp_path: Path) -> None:
    """Accessing models before start() raises RuntimeError."""
    # Arrange
    config = Config(data_dir=tmp_path / "data")
    runtime = Runtime(config)

    # Act & Assert
    with pytest.raises(RuntimeError, match="not started"):
        _ = runtime.models


def test_phase_two_services_not_accessible_before_start(tmp_path: Path) -> None:
    """Accessing Phase 2 services before start() raises RuntimeError."""
    runtime = Runtime(Config(data_dir=tmp_path / "data"))

    for attribute_name in (
        "storage",
        "agents",
        "tools",
        "skills",
        "chat_sessions",
        "system_prompts",
    ):
        with pytest.raises(RuntimeError, match="not started"):
            getattr(runtime, attribute_name)


def test_runtime_loads_phase_two_services(runtime: Runtime) -> None:
    """Runtime.start() loads Phase 2 services alongside registries."""
    assert runtime.storage.data_dir.exists()
    assert runtime.agents.data_dir == runtime.storage.data_dir
    assert [tool.name for tool in runtime.tools.list_tools()] == [
        "bash",
        "cron",
        "edit",
        "glob",
        "grep",
        "process",
        "read",
        "session_search",
        "status",
        "subagent",
        "subagent_result",
        "web_fetch",
        "web_search",
        "write",
    ]
    assert [skill.name for skill in runtime.skills.list_all()] == [
        "poem-writer",
        "vbot-cli",
        "warning-example",
    ]
    assert runtime.skills.warnings_for("warning-example") == [
        "Skill name 'warning-example' does not match directory name 'warning-name-mismatch'."
    ]
    assert [diagnostic.name for diagnostic in runtime.skills.invalid_diagnostics()] == [
        "broken-skill"
    ]
    assert runtime.chat_sessions.sessions_dir("coder") == (
        runtime.storage.data_dir / "agents" / "coder" / "sessions"
    )
