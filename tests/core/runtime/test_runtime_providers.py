"""Integration tests for Runtime loading providers and models from resources.

Verifies that ``Runtime.start()`` loads ``ProviderRegistry`` and
``ModelRegistry`` from the ``resources/`` directory, and that the
registries contain the expected data.
"""

from pathlib import Path

import pytest

from core.models.models import ModelRegistry
from core.providers.providers import ProviderRegistry
from core.runtime.runtime import Runtime
from core.utils.config import Config


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

    # Assert
    assert openai_config.id == "openai"
    assert openai_config.name == "OpenAI"
    assert openai_config.adapter == "openai_compatible"
    assert openai_config.base_url == "https://api.openai.com/v1"
    assert openai_config.auth.env_key == "OPENAI_API_KEY"


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
    assert [tool.name for tool in runtime.tools.list_tools()] == ["read", "read2"]
    assert runtime.skills.list_all() == []
    assert runtime.chat_sessions.sessions_dir("coder") == (
        runtime.storage.data_dir / "agents" / "coder" / "sessions"
    )
