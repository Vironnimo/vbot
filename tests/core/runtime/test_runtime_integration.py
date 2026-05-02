"""Integration tests for Runtime adapter creation and model lookup.

Verifies that ``Runtime.get_adapter()`` returns the correct adapter type
with proper wiring, that ``Runtime.get_model()`` returns the correct model
data, and that appropriate errors are raised for invalid lookups.
"""

import pytest

from core.providers.anthropic import AnthropicAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.runtime.runtime import Runtime
from core.utils.config import Config
from core.utils.errors import ConfigError


@pytest.fixture
def runtime() -> Runtime:
    """Provide a started Runtime instance loaded from resources."""
    config = Config()
    runtime = Runtime(config)
    runtime.start()
    return runtime


@pytest.fixture
def runtime_with_openai_key(monkeypatch: pytest.MonkeyPatch) -> Runtime:
    """Provide a started Runtime with a fake OpenAI API key."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key-12345")
    config = Config()
    runtime = Runtime(config)
    runtime.start()
    return runtime


@pytest.fixture
def runtime_with_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> Runtime:
    """Provide a started Runtime with a fake Anthropic API key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake-key-12345")
    config = Config()
    runtime = Runtime(config)
    runtime.start()
    return runtime


@pytest.fixture
def runtime_with_openrouter_key(monkeypatch: pytest.MonkeyPatch) -> Runtime:
    """Provide a started Runtime with a fake OpenRouter API key."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-fake-key-12345")
    config = Config()
    runtime = Runtime(config)
    runtime.start()
    return runtime


# ------------------------------------------------------------------
# Adapter creation: happy paths
# ------------------------------------------------------------------


def test_get_adapter_openai_returns_wired_adapter(
    runtime_with_openai_key: Runtime,
) -> None:
    """Runtime.get_adapter('openai') returns a wired OpenAICompatibleAdapter."""
    # Act
    adapter = runtime_with_openai_key.get_adapter("openai")

    # Assert — type check + provider config wiring
    assert isinstance(adapter, OpenAICompatibleAdapter)
    assert adapter._config.id == "openai"  # type: ignore[attr-defined]
    assert adapter._config.base_url == "https://api.openai.com/v1"  # type: ignore[attr-defined]


def test_get_adapter_anthropic_returns_wired_adapter(
    runtime_with_anthropic_key: Runtime,
) -> None:
    """Runtime.get_adapter('anthropic') returns a wired AnthropicAdapter."""
    # Act
    adapter = runtime_with_anthropic_key.get_adapter("anthropic")

    # Assert — type check + provider config wiring
    assert isinstance(adapter, AnthropicAdapter)
    assert adapter._config.id == "anthropic"  # type: ignore[attr-defined]
    assert adapter._config.base_url == "https://api.anthropic.com/v1"  # type: ignore[attr-defined]


def test_get_adapter_openrouter_returns_wired_adapter(
    runtime_with_openrouter_key: Runtime,
) -> None:
    """Runtime.get_adapter('openrouter') returns a wired OpenAICompatibleAdapter."""
    # Act
    adapter = runtime_with_openrouter_key.get_adapter("openrouter")

    # Assert — type check + extra_headers wiring
    assert isinstance(adapter, OpenAICompatibleAdapter)
    assert adapter._config.extra_headers is not None  # type: ignore[attr-defined]
    assert "HTTP-Referer" in adapter._config.extra_headers  # type: ignore[attr-defined]


# ------------------------------------------------------------------
# Model lookup: happy paths
# ------------------------------------------------------------------


def test_get_model_openrouter_claude_sonnet(runtime: Runtime) -> None:
    """Runtime.get_model('openrouter', 'anthropic/claude-sonnet-4') returns correct data."""
    # Act
    model = runtime.get_model("openrouter", "anthropic/claude-sonnet-4")

    # Assert
    assert model.model_id == "anthropic/claude-sonnet-4"
    assert model.name == "Claude Sonnet 4"
    assert model.context_window == 128000
    assert model.max_output_tokens == 64000
    assert model.capabilities.vision is True
    assert model.capabilities.tools is True
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
        runtime.get_adapter("nonexistent")


def test_get_model_nonexistent_raises_key_error(runtime: Runtime) -> None:
    """Runtime.get_model('nonexistent', 'model') raises KeyError."""
    # Act & Assert
    with pytest.raises(KeyError):
        runtime.get_model("nonexistent", "model")


def test_get_adapter_missing_api_key_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime.get_adapter() raises ConfigError when the API key is not set."""
    # Arrange — ensure OPENAI_API_KEY is absent from the environment
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = Config()
    runtime = Runtime(config)
    runtime.start()

    # Act & Assert
    with pytest.raises(ConfigError, match="API key not found"):
        runtime.get_adapter("openai")


def test_get_adapter_unknown_adapter_type_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime.get_adapter() raises ConfigError for an unknown adapter type.

    This test verifies the adapter factory mapping rejects unknown types
    by temporarily removing the openai_compatible entry.
    """
    # Arrange
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key")
    config = Config()
    runtime = Runtime(config)
    runtime.start()

    # Patch the adapter map to remove the openai_compatible entry
    original_map = runtime.get_adapter.__globals__["_ADAPTER_MAP"].copy()
    runtime.get_adapter.__globals__["_ADAPTER_MAP"].pop("openai_compatible")

    try:
        # Act & Assert
        with pytest.raises(ConfigError, match="Unknown adapter type"):
            runtime.get_adapter("openai")
    finally:
        # Restore the original map
        runtime.get_adapter.__globals__["_ADAPTER_MAP"].update(original_map)


# ------------------------------------------------------------------
# Error cases: methods called before start
# ------------------------------------------------------------------


def test_get_adapter_before_start_raises_runtime_error() -> None:
    """Runtime.get_adapter() before start() raises RuntimeError."""
    # Arrange
    config = Config()
    runtime = Runtime(config)

    # Act & Assert
    with pytest.raises(RuntimeError, match="not started"):
        runtime.get_adapter("openai")


def test_get_model_before_start_raises_runtime_error() -> None:
    """Runtime.get_model() before start() raises RuntimeError."""
    # Arrange
    config = Config()
    runtime = Runtime(config)

    # Act & Assert
    with pytest.raises(RuntimeError, match="not started"):
        runtime.get_model("openai", "gpt-5.2")


# ------------------------------------------------------------------
# Existing behavior preserved
# ------------------------------------------------------------------


def test_runtime_start_idempotent_with_registries() -> None:
    """Calling start() twice is still a no-op after adding registry loading."""
    # Arrange
    config = Config()
    runtime = Runtime(config)
    runtime.start()
    providers_first = runtime.providers
    models_first = runtime.models

    # Act — second call is a no-op
    runtime.start()

    # Assert — same registry instances (cached)
    assert runtime.providers is providers_first
    assert runtime.models is models_first


def test_runtime_stop_then_start_reloads_registries() -> None:
    """After stop() and start(), registries are available again."""
    # Arrange
    config = Config()
    runtime = Runtime(config)
    runtime.start()
    runtime.stop()

    # Act
    runtime.start()

    # Assert
    assert runtime.providers is not None
    assert runtime.models is not None
    assert "openai" in runtime.providers.list_ids()
