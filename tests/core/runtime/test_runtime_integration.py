"""Integration tests for Runtime adapter creation and model lookup.

Verifies that ``Runtime.get_adapter()`` returns the correct adapter type
with proper wiring, that ``Runtime.get_model()`` returns the correct model
data, and that appropriate errors are raised for invalid lookups.
"""

import os

import pytest

from core.embeddings import EmbeddingService
from core.providers.anthropic import AnthropicAdapter
from core.providers.credentials import ProviderCredentialResolver
from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig, ProviderRegistry
from core.providers.token_store import OAuthToken
from core.runtime.runtime import Runtime
from core.utils.config import Config
from core.utils.errors import ConfigError


@pytest.fixture
def config(tmp_path) -> Config:
    """Provide isolated runtime config."""
    return Config(data_dir=tmp_path / "data")


@pytest.fixture
def runtime(config: Config) -> Runtime:
    """Provide a started Runtime instance loaded from resources."""
    runtime = Runtime(config)
    runtime.start()
    return runtime


@pytest.fixture
def runtime_with_openai_key(monkeypatch: pytest.MonkeyPatch, config: Config) -> Runtime:
    """Provide a started Runtime with a fake OpenAI API key."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key-12345")
    runtime = Runtime(config)
    runtime.start()
    return runtime


@pytest.fixture
def runtime_with_anthropic_key(monkeypatch: pytest.MonkeyPatch, config: Config) -> Runtime:
    """Provide a started Runtime with a fake Anthropic API key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake-key-12345")
    runtime = Runtime(config)
    runtime.start()
    return runtime


@pytest.fixture
def runtime_with_openrouter_key(monkeypatch: pytest.MonkeyPatch, config: Config) -> Runtime:
    """Provide a started Runtime with a fake OpenRouter API key."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-fake-key-12345")
    runtime = Runtime(config)
    runtime.start()
    return runtime


# ------------------------------------------------------------------
# Adapter creation: happy paths
# ------------------------------------------------------------------


def test_get_adapter_openai_returns_wired_adapter(
    runtime_with_openai_key: Runtime,
) -> None:
    """Runtime.get_adapter('openai', 'openai:api-key') returns a wired adapter."""
    # Act
    adapter = runtime_with_openai_key.get_adapter("openai", "openai:api-key")

    # Assert — type check + provider config wiring
    assert isinstance(adapter, OpenAICompatibleAdapter)
    assert adapter._config.id == "openai"  # type: ignore[attr-defined]
    assert adapter._config.base_url == "https://api.openai.com/v1"  # type: ignore[attr-defined]


def test_get_adapter_anthropic_returns_wired_adapter(
    runtime_with_anthropic_key: Runtime,
) -> None:
    """Runtime.get_adapter('anthropic', 'anthropic:api-key') returns a wired adapter."""
    # Act
    adapter = runtime_with_anthropic_key.get_adapter("anthropic", "anthropic:api-key")

    # Assert — type check + provider config wiring
    assert isinstance(adapter, AnthropicAdapter)
    assert adapter._config.id == "anthropic"  # type: ignore[attr-defined]
    assert adapter._config.base_url == "https://api.anthropic.com/v1"  # type: ignore[attr-defined]


def test_get_adapter_openrouter_returns_wired_adapter(
    runtime_with_openrouter_key: Runtime,
) -> None:
    """Runtime.get_adapter('openrouter', 'openrouter:api-key') returns a wired adapter."""
    # Act
    adapter = runtime_with_openrouter_key.get_adapter("openrouter", "openrouter:api-key")

    # Assert — type check + extra_headers wiring
    assert isinstance(adapter, OpenRouterAdapter)
    assert adapter._config.extra_headers is not None  # type: ignore[attr-defined]
    assert "HTTP-Referer" in adapter._config.extra_headers  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_get_adapter_github_copilot_returns_wired_adapter(runtime: Runtime) -> None:
    """Runtime.get_adapter() returns the GitHub Copilot-specific adapter."""

    runtime.token_store.save(
        "github-copilot",
        "oauth",
        OAuthToken(access_token="copilot-test-token"),
    )

    adapter = runtime.get_adapter("github-copilot", "github-copilot:oauth")

    assert isinstance(adapter, GitHubCopilotAdapter)
    assert adapter._config.id == "github-copilot"  # type: ignore[attr-defined]
    assert await adapter._token_getter() == "copilot-test-token"  # type: ignore[attr-defined]


def test_get_adapter_connection_base_url_override_uses_override(
    runtime: Runtime,
) -> None:
    """Runtime.get_adapter() passes connection base_url override into the adapter."""
    # Arrange
    provider_config = ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai_compatible",
        base_url="https://api.openai.com/v1",
        connections=[
            ConnectionConfig(
                id="enterprise",
                type="api_key",
                label="Enterprise",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENAI_ENTERPRISE_KEY",
                ),
                base_url="https://enterprise.example.com/v1",
            )
        ],
    )
    registry = ProviderRegistry({"openai": provider_config})
    runtime._providers = registry  # type: ignore[attr-defined]
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        registry,
        process_env={"OPENAI_ENTERPRISE_KEY": "sk-enterprise"},
    )

    # Act
    adapter = runtime.get_adapter("openai", "openai:enterprise")

    # Assert
    assert isinstance(adapter, OpenAICompatibleAdapter)
    assert str(adapter._client.base_url) == "https://enterprise.example.com/v1/"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_get_adapter_passes_selected_connection_auth_metadata(runtime: Runtime) -> None:
    """Runtime passes the requested connection's auth metadata to the adapter."""
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
            ),
            ConnectionConfig(
                id="service-account",
                type="api_key",
                label="Service Account",
                auth=AuthConfig(
                    header="x-service-token",
                    prefix="Token ",
                    credential_key="OPENAI_SERVICE_TOKEN",
                ),
            ),
        ],
    )
    registry = ProviderRegistry({"openai": provider_config})
    runtime._providers = registry  # type: ignore[attr-defined]
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        registry,
        process_env={"OPENAI_SERVICE_TOKEN": "service-token"},
    )

    # Act
    adapter = runtime.get_adapter("openai", "openai:service-account")

    # Assert
    assert isinstance(adapter, OpenAICompatibleAdapter)
    assert await adapter._token_getter() == "service-token"  # type: ignore[attr-defined]
    assert adapter._auth_config.header == "x-service-token"  # type: ignore[attr-defined]
    assert adapter._auth_config.prefix == "Token "  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_runtime_start_loads_data_dir_env_for_provider_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Runtime resolves provider credentials from the active data-directory .env."""
    # Arrange
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_dir.joinpath(".env").write_text(
        "OPENROUTER_API_KEY=sk-or-from-data-dir\n",
        encoding="utf-8",
    )
    runtime = Runtime(Config(data_dir=data_dir))
    runtime.start()

    # Act
    adapter = runtime.get_adapter("openrouter", "openrouter:api-key")

    # Assert
    assert runtime.has_provider_credentials("openrouter") is True
    assert runtime.get_provider_credentials("openrouter") == "sk-or-from-data-dir"
    assert await adapter._token_getter() == "sk-or-from-data-dir"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_runtime_start_does_not_overwrite_existing_provider_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Process env vars remain authoritative over data-directory .env values."""
    # Arrange
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-process")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_dir.joinpath(".env").write_text(
        "OPENROUTER_API_KEY=sk-or-from-data-dir\n",
        encoding="utf-8",
    )
    runtime = Runtime(Config(data_dir=data_dir))
    runtime.start()

    # Act
    adapter = runtime.get_adapter("openrouter", "openrouter:api-key")

    # Assert
    assert runtime.get_provider_credentials("openrouter") == "sk-or-from-process"
    assert await adapter._token_getter() == "sk-or-from-process"  # type: ignore[attr-defined]


def test_runtime_start_does_not_mutate_process_environment_when_loading_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Runtime.start() keeps data-dir credentials out of the live process env."""

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_dir.joinpath(".env").write_text(
        "OPENROUTER_API_KEY=sk-or-from-data-dir\n",
        encoding="utf-8",
    )

    runtime = Runtime(Config(data_dir=data_dir))

    runtime.start()

    assert "OPENROUTER_API_KEY" not in os.environ


def test_runtime_empty_process_credential_overrides_data_dir_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """An empty process credential still wins over the data-dir fallback value."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_dir.joinpath(".env").write_text(
        "OPENROUTER_API_KEY=sk-or-from-data-dir\n",
        encoding="utf-8",
    )

    runtime = Runtime(Config(data_dir=data_dir))
    runtime.start()

    assert runtime.has_provider_credentials("openrouter") is False
    with pytest.raises(ConfigError, match="Provider credentials not found"):
        runtime.get_adapter("openrouter", "openrouter:api-key")


def test_runtime_provider_credentials_report_missing_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
) -> None:
    """Runtime reports missing provider credentials when neither source has a value."""

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runtime = Runtime(config)
    runtime.start()

    assert runtime.has_provider_credentials("openai") is False


def test_runtime_provider_credentials_work_when_any_connection_is_usable(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
) -> None:
    """Runtime provider credential status is true when one connection is usable."""

    monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key")
    runtime = Runtime(config)
    runtime.start()

    assert runtime.has_provider_credentials("openai") is True


# ------------------------------------------------------------------
# Model lookup: happy paths
# ------------------------------------------------------------------


def test_get_model_openrouter_claude_sonnet(runtime: Runtime) -> None:
    """Runtime.get_model('openrouter', 'anthropic/claude-sonnet-4') returns correct data."""
    # Act
    model = runtime.get_model("openrouter", "anthropic/claude-sonnet-4")

    # Assert
    assert model.model_id == "anthropic/claude-sonnet-4"
    assert model.name == "Anthropic: Claude Sonnet 4"
    assert model.context_window == 1000000
    assert model.max_output_tokens == 64000
    assert model.capabilities.vision is True
    assert model.capabilities.tools is True
    assert model.capabilities.reasoning.supported is True


def test_get_model_openrouter_claude_haiku(runtime: Runtime) -> None:
    """Runtime.get_model('openrouter', 'anthropic/claude-haiku-4.5') returns correct data."""
    # Act
    model = runtime.get_model("openrouter", "anthropic/claude-haiku-4.5")

    # Assert
    assert model.model_id == "anthropic/claude-haiku-4.5"
    assert model.name == "Anthropic: Claude Haiku 4.5"
    assert model.context_window == 200000
    assert model.max_output_tokens == 64000
    assert model.capabilities.vision is True
    assert model.capabilities.tools is True
    assert model.capabilities.json_mode is True
    assert model.capabilities.reasoning.supported is True


def test_get_model_openrouter_gpt55(runtime: Runtime) -> None:
    """Runtime.get_model('openrouter', 'openai/gpt-5.5') returns committed catalog data."""
    # Act
    model = runtime.get_model("openrouter", "openai/gpt-5.5")

    # Assert
    assert model.model_id == "openai/gpt-5.5"
    assert model.name == "OpenAI: GPT-5.5"
    assert model.context_window == 1050000
    assert model.max_output_tokens == 128000
    assert model.capabilities.vision is True
    assert model.capabilities.tools is True
    assert model.capabilities.json_mode is True
    assert model.capabilities.reasoning.supported is True


def test_get_model_openrouter_claude_opus_47(runtime: Runtime) -> None:
    """Runtime.get_model for OpenRouter Opus 4.7 matches committed catalog data."""
    # Act
    model = runtime.get_model("openrouter", "anthropic/claude-opus-4.7")

    # Assert
    assert model.model_id == "anthropic/claude-opus-4.7"
    assert model.name == "Anthropic: Claude Opus 4.7"
    assert model.context_window == 1000000
    assert model.max_output_tokens == 128000
    assert model.capabilities.vision is True
    assert model.capabilities.tools is True
    assert model.capabilities.json_mode is True
    assert model.capabilities.reasoning.supported is True


def test_get_model_anthropic_claude_sonnet(runtime: Runtime) -> None:
    """Runtime.get_model('anthropic', 'claude-sonnet-4-20250219') returns correct data."""
    # Act
    model = runtime.get_model("anthropic", "claude-sonnet-4-20250219")

    # Assert
    assert model.model_id == "claude-sonnet-4-20250219"
    assert model.name == "Claude Sonnet 4"
    assert model.context_window == 200000


def test_get_model_openai_gpt52(runtime: Runtime) -> None:
    """Runtime.get_model('openai', 'gpt-5.2') returns correct data."""
    # Act
    model = runtime.get_model("openai", "gpt-5.2")

    # Assert
    assert model.model_id == "gpt-5.2"
    assert model.name == "GPT-5.2"


# ------------------------------------------------------------------
# Error cases: unknown providers and models
# ------------------------------------------------------------------


def test_get_adapter_nonexistent_raises_key_error(runtime: Runtime) -> None:
    """Runtime.get_adapter('nonexistent') raises KeyError."""
    # Act & Assert
    with pytest.raises(KeyError, match="nonexistent"):
        runtime.get_adapter("nonexistent", "nonexistent:api-key")


def test_get_model_nonexistent_raises_key_error(runtime: Runtime) -> None:
    """Runtime.get_model('nonexistent', 'model') raises KeyError."""
    # Act & Assert
    with pytest.raises(KeyError):
        runtime.get_model("nonexistent", "model")


def test_get_adapter_missing_api_key_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
) -> None:
    """Runtime.get_adapter() raises ConfigError when credentials are not set."""
    # Arrange — ensure OPENAI_API_KEY is absent from the environment
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runtime = Runtime(config)
    runtime.start()

    # Act & Assert
    with pytest.raises(ConfigError, match="Provider credentials not found"):
        runtime.get_adapter("openai", "openai:api-key")


def test_get_adapter_unknown_connection_id_raises_config_error(
    runtime_with_openai_key: Runtime,
) -> None:
    """Runtime.get_adapter() raises ConfigError for an unknown connection ID."""

    with pytest.raises(ConfigError, match="Unknown connection id"):
        runtime_with_openai_key.get_adapter("openai", "openai:missing")


def test_get_adapter_unknown_adapter_type_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
) -> None:
    """Runtime.get_adapter() raises ConfigError for an unknown adapter type.

    This test verifies the adapter factory mapping rejects unknown types
    by temporarily removing the openai_compatible entry.
    """
    # Arrange
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key")
    runtime = Runtime(config)
    runtime.start()

    # Patch the adapter map to remove the openai_compatible entry
    original_map = runtime.get_adapter.__globals__["_ADAPTER_MAP"].copy()
    runtime.get_adapter.__globals__["_ADAPTER_MAP"].pop("openai_compatible")

    try:
        # Act & Assert
        with pytest.raises(ConfigError, match="Unknown adapter type"):
            runtime.get_adapter("openai", "openai:api-key")
    finally:
        # Restore the original map
        runtime.get_adapter.__globals__["_ADAPTER_MAP"].update(original_map)


# ------------------------------------------------------------------
# Error cases: methods called before start
# ------------------------------------------------------------------


def test_get_adapter_before_start_raises_runtime_error(config: Config) -> None:
    """Runtime.get_adapter() before start() raises RuntimeError."""
    # Arrange
    runtime = Runtime(config)

    # Act & Assert
    with pytest.raises(RuntimeError, match="not started"):
        runtime.get_adapter("openai", "openai:api-key")


# ------------------------------------------------------------------
# EmbeddingService wiring
# ------------------------------------------------------------------


def test_runtime_exposes_embedding_service_after_start(
    runtime_with_openrouter_key: Runtime,
) -> None:
    """``runtime.embeddings`` returns an ``EmbeddingService`` instance
    after ``start()``. The service is wired against the same
    ``TaskModelService`` and runtime the rest of the specialized
    execution services use.
    """

    service = runtime_with_openrouter_key.embeddings

    assert isinstance(service, EmbeddingService)


def test_runtime_embeddings_is_none_before_start(config: Config) -> None:
    """The embedding service is ``None`` before ``start()`` and raises
    :class:`RuntimeError` on access — same lifecycle as the other
    specialized services.
    """

    runtime = Runtime(config)

    assert runtime._embeddings is None  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="not started"):
        _ = runtime.embeddings


def test_runtime_embeddings_is_none_after_stop(
    runtime_with_openrouter_key: Runtime,
) -> None:
    """Stopping the runtime clears the embedding service reference like
    the other specialized services.
    """

    runtime_with_openrouter_key.stop()

    assert runtime_with_openrouter_key._embeddings is None  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="not started"):
        _ = runtime_with_openrouter_key.embeddings


def test_provider_credential_access_before_start_raises_runtime_error(config: Config) -> None:
    """Runtime provider credential access before start() raises RuntimeError."""

    runtime = Runtime(config)

    with pytest.raises(RuntimeError, match="not started"):
        runtime.has_provider_credentials("openai")

    with pytest.raises(RuntimeError, match="not started"):
        runtime.get_provider_credentials("openai")


def test_get_model_before_start_raises_runtime_error(config: Config) -> None:
    """Runtime.get_model() before start() raises RuntimeError."""
    # Arrange
    runtime = Runtime(config)

    # Act & Assert
    with pytest.raises(RuntimeError, match="not started"):
        runtime.get_model("openai", "gpt-5.2")


# ------------------------------------------------------------------
# Existing behavior preserved
# ------------------------------------------------------------------


def test_runtime_start_idempotent_with_registries(config: Config) -> None:
    """Calling start() twice is still a no-op after adding registry loading."""
    # Arrange
    runtime = Runtime(config)
    runtime.start()
    providers_first = runtime.providers
    models_first = runtime.models
    storage_first = runtime.storage
    agents_first = runtime.agents

    # Act — second call is a no-op
    runtime.start()

    # Assert — same registry instances (cached)
    assert runtime.providers is providers_first
    assert runtime.models is models_first
    assert runtime.storage is storage_first
    assert runtime.agents is agents_first


def test_runtime_start_preserves_existing_agents(config: Config) -> None:
    """Runtime.start() does not add main when persisted agents already exist."""
    runtime = Runtime(config)
    runtime.start()
    runtime.agents.create("coder", "Coder Agent")
    runtime.agents.delete("main")
    runtime.stop()

    runtime.start()

    agents = runtime.agents.list()
    assert [agent.id for agent in agents] == ["coder"]
    assert agents[0].current_session_id
    assert runtime.chat_sessions.get("coder", agents[0].current_session_id).load() == []


def test_runtime_stop_then_start_reloads_registries(config: Config) -> None:
    """After stop() and start(), registries are available again."""
    # Arrange
    runtime = Runtime(config)
    runtime.start()
    runtime.stop()

    # Act
    runtime.start()

    # Assert
    assert runtime.providers is not None
    assert runtime.models is not None
    assert "openai" in runtime.providers.list_ids()
    assert runtime.storage is not None
    assert runtime.agents is not None


def test_runtime_read_provider_definition_is_compact(config: Config) -> None:
    """Runtime startup exposes only model-visible read metadata."""
    runtime = Runtime(config)

    runtime.start()

    definitions = runtime.tools.provider_definitions(["read"])
    assert definitions == [
        {
            "name": "read",
            "description": (
                "Read the contents of a file. Output is truncated to 2000 lines or "
                "50 KB (whichever is hit first). If offset is past EOF, returns an "
                "explicit end-of-file notice. Use offset/limit for large files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Path to the file to read (relative to workspace, or absolute)."
                        ),
                    },
                    "offset": {
                        "type": "number",
                        "description": "Line number to start reading from (1-indexed).",
                    },
                    "limit": {
                        "type": "number",
                        "description": "Maximum number of lines to read.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        }
    ]
    assert set(definitions[0]["parameters"]["properties"]) == {
        "path",
        "offset",
        "limit",
    }
    assert "description" not in definitions[0]["parameters"]["properties"]
