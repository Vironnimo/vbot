"""Tests for ProviderConfig dataclass and ProviderRegistry.

Verifies loading from JSON fixtures, lookup by provider ID, immutability,
missing-provider errors, caching behaviour, and correct parsing of auth
fields, extra_headers, defaults, and models_endpoint.
"""

import json
from collections.abc import Generator
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from core.providers.providers import (
    AuthConfig,
    ProviderConfig,
    ProviderRegistry,
    _registry_cache,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

OPENAI_DATA = {
    "id": "openai",
    "name": "OpenAI",
    "adapter": "openai_compatible",
    "base_url": "https://api.openai.com/v1",
    "auth": {
        "header": "Authorization",
        "prefix": "Bearer ",
        "credential_key": "OPENAI_API_KEY",
    },
    "defaults": {"max_tokens": 4096, "temperature": 0.7},
}

OPENROUTER_DATA = {
    "id": "openrouter",
    "name": "OpenRouter",
    "adapter": "openai_compatible",
    "base_url": "https://openrouter.ai/api/v1",
    "auth": {
        "header": "Authorization",
        "prefix": "Bearer ",
        "credential_key": "OPENROUTER_API_KEY",
    },
    "defaults": {"max_tokens": 4096, "temperature": 0.7},
    "extra_headers": {"HTTP-Referer": "https://vbot.app", "X-Title": "vBot"},
    "models_endpoint": "/models",
}

ANTHROPIC_DATA = {
    "id": "anthropic",
    "name": "Anthropic",
    "adapter": "anthropic",
    "base_url": "https://api.anthropic.com/v1",
    "auth": {
        "header": "x-api-key",
        "prefix": "",
        "credential_key": "ANTHROPIC_API_KEY",
    },
    "defaults": {"max_tokens": 4096, "temperature": 0.7},
}


@pytest.fixture()
def providers_dir(tmp_path: Path) -> Path:
    """Create a temporary resources directory with provider JSON files."""
    prov_dir = tmp_path / "providers"
    prov_dir.mkdir()
    for name, data in [
        ("openai.json", OPENAI_DATA),
        ("openrouter.json", OPENROUTER_DATA),
        ("anthropic.json", ANTHROPIC_DATA),
    ]:
        (prov_dir / name).write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_cache() -> Generator[None, None, None]:
    """Clear the module-level registry cache before and after each test."""
    _registry_cache.clear()
    yield
    _registry_cache.clear()


# ---------------------------------------------------------------------------
# ProviderConfig dataclass
# ---------------------------------------------------------------------------


class TestProviderConfig:
    """Tests for the ProviderConfig frozen dataclass."""

    def test_frozen_raises_on_attribute_assignment(self) -> None:
        """Assigning to a field on a frozen ProviderConfig raises FrozenInstanceError."""
        # Arrange
        config = ProviderConfig(
            id="test",
            name="Test",
            adapter="openai_compatible",
            base_url="https://example.com/v1",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
                credential_key="TEST_KEY",
            ),
        )

        # Act / Assert
        with pytest.raises(FrozenInstanceError):
            config.id = "changed"  # type: ignore[misc]

    def test_frozen_raises_on_nested_auth_assignment(self) -> None:
        """Assigning to a field on the nested AuthConfig also raises FrozenInstanceError."""
        # Arrange
        auth = AuthConfig(
            header="Authorization",
            prefix="Bearer ",
            credential_key="TEST_KEY",
        )

        # Act / Assert
        with pytest.raises(FrozenInstanceError):
            auth.credential_key = "CHANGED"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Auth parsing
# ---------------------------------------------------------------------------


class TestAuthParsing:
    """Tests for correct parsing of auth fields from JSON data."""

    def test_openai_auth_fields(self, providers_dir: Path) -> None:
        """OpenAI auth fields parse correctly from JSON."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        config = registry.get("openai")

        # Assert
        assert config.auth.header == "Authorization"
        assert config.auth.prefix == "Bearer "
        assert config.auth.credential_key == "OPENAI_API_KEY"

    def test_anthropic_auth_fields(self, providers_dir: Path) -> None:
        """Anthropic x-api-key auth fields parse correctly from JSON."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        config = registry.get("anthropic")

        # Assert
        assert config.auth.header == "x-api-key"
        assert config.auth.prefix == ""
        assert config.auth.credential_key == "ANTHROPIC_API_KEY"

    def test_openrouter_auth_fields(self, providers_dir: Path) -> None:
        """OpenRouter auth fields parse correctly from JSON."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        config = registry.get("openrouter")

        # Assert
        assert config.auth.header == "Authorization"
        assert config.auth.prefix == "Bearer "
        assert config.auth.credential_key == "OPENROUTER_API_KEY"


# ---------------------------------------------------------------------------
# Defaults, extra_headers, models_endpoint
# ---------------------------------------------------------------------------


class TestOptionalFields:
    """Tests for optional fields: defaults, extra_headers, models_endpoint."""

    def test_openai_defaults(self, providers_dir: Path) -> None:
        """OpenAI defaults parse correctly from JSON."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        config = registry.get("openai")

        # Assert
        assert config.defaults is not None
        assert config.defaults["max_tokens"] == 4096
        assert config.defaults["temperature"] == 0.7

    def test_openai_no_extra_headers(self, providers_dir: Path) -> None:
        """OpenAI has no extra_headers (field is None)."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        config = registry.get("openai")

        # Assert
        assert config.extra_headers is None

    def test_openai_no_models_endpoint(self, providers_dir: Path) -> None:
        """OpenAI has no models_endpoint (field is None)."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        config = registry.get("openai")

        # Assert
        assert config.models_endpoint is None

    def test_openrouter_extra_headers(self, providers_dir: Path) -> None:
        """OpenRouter extra_headers parse correctly from JSON."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        config = registry.get("openrouter")

        # Assert
        assert config.extra_headers is not None
        assert config.extra_headers["HTTP-Referer"] == "https://vbot.app"
        assert config.extra_headers["X-Title"] == "vBot"

    def test_openrouter_models_endpoint(self, providers_dir: Path) -> None:
        """OpenRouter models_endpoint parses correctly from JSON."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        config = registry.get("openrouter")

        # Assert
        assert config.models_endpoint == "/models"

    def test_anthropic_no_extra_headers(self, providers_dir: Path) -> None:
        """Anthropic has no extra_headers (field is None)."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        config = registry.get("anthropic")

        # Assert
        assert config.extra_headers is None

    def test_anthropic_no_models_endpoint(self, providers_dir: Path) -> None:
        """Anthropic has no models_endpoint (field is None)."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        config = registry.get("anthropic")

        # Assert
        assert config.models_endpoint is None


# ---------------------------------------------------------------------------
# Registry: loading and lookup
# ---------------------------------------------------------------------------


class TestProviderRegistryLoad:
    """Tests for ProviderRegistry.load() and provider lookup."""

    def test_load_creates_registry_with_all_providers(self, providers_dir: Path) -> None:
        """Loading populates the registry with all JSON provider files."""
        # Arrange / Act
        registry = ProviderRegistry.load(providers_dir)

        # Assert
        assert len(registry._configs) == 3

    def test_get_returns_correct_provider_config(self, providers_dir: Path) -> None:
        """get() returns the ProviderConfig matching the requested ID."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        config = registry.get("openai")

        # Assert
        assert config.id == "openai"
        assert config.name == "OpenAI"
        assert config.adapter == "openai_compatible"
        assert config.base_url == "https://api.openai.com/v1"

    def test_get_anthropic_provider(self, providers_dir: Path) -> None:
        """get() returns the Anthropic provider config correctly."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        config = registry.get("anthropic")

        # Assert
        assert config.id == "anthropic"
        assert config.adapter == "anthropic"
        assert config.base_url == "https://api.anthropic.com/v1"

    def test_get_openrouter_provider(self, providers_dir: Path) -> None:
        """get() returns the OpenRouter provider config correctly."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        config = registry.get("openrouter")

        # Assert
        assert config.id == "openrouter"
        assert config.base_url == "https://openrouter.ai/api/v1"


class TestProviderRegistryListIds:
    """Tests for ProviderRegistry.list_ids()."""

    def test_list_ids_returns_sorted_provider_ids(self, providers_dir: Path) -> None:
        """list_ids() returns a sorted list of all registered provider IDs."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        ids = registry.list_ids()

        # Assert
        assert ids == ["anthropic", "openai", "openrouter"]


class TestProviderRegistryMissing:
    """Tests for error handling on missing providers."""

    def test_get_missing_provider_raises_key_error(self, providers_dir: Path) -> None:
        """get() raises KeyError for a provider ID that does not exist."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act / Assert
        with pytest.raises(KeyError, match="nonexistent"):
            registry.get("nonexistent")

    def test_get_missing_provider_error_includes_available_ids(self, providers_dir: Path) -> None:
        """The KeyError message lists available provider IDs."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act / Assert
        with pytest.raises(KeyError, match="anthropic"):
            registry.get("no-such-provider")


# ---------------------------------------------------------------------------
# Registry: caching
# ---------------------------------------------------------------------------


class TestProviderRegistryCaching:
    """Tests for ProviderRegistry caching behaviour."""

    def test_second_load_returns_same_instance(self, providers_dir: Path) -> None:
        """Calling load() twice returns the exact same registry instance."""
        # Arrange — first load
        first = ProviderRegistry.load(providers_dir)

        # Act — second load
        second = ProviderRegistry.load(providers_dir)

        # Assert — same object, not a new instance
        assert first is second

    def test_cache_prevents_re_reading_files(self, providers_dir: Path) -> None:
        """After caching, deleting a JSON file does not affect the registry."""
        # Arrange — load to populate cache
        registry = ProviderRegistry.load(providers_dir)
        original_ids = registry.list_ids()

        # Act — delete one of the JSON files
        (providers_dir / "providers" / "anthropic.json").unlink()

        # Second load should still return cached registry with all 3 providers
        cached_registry = ProviderRegistry.load(providers_dir)

        # Assert
        assert cached_registry.list_ids() == original_ids

    def test_different_dirs_return_different_instances(
        self, providers_dir: Path, tmp_path: Path
    ) -> None:
        """Two different resource directories yield two different registry instances."""
        # Arrange
        other_dir = tmp_path / "other_resources"
        other_dir.mkdir()
        other_providers = other_dir / "providers"
        other_providers.mkdir()

        # Act
        first = ProviderRegistry.load(providers_dir)
        second = ProviderRegistry.load(other_dir)

        # Assert
        assert first is not second


# ---------------------------------------------------------------------------
# Registry: duplicate IDs
# ---------------------------------------------------------------------------


class TestProviderRegistryDuplicates:
    """Tests for duplicate provider ID detection."""

    def test_duplicate_id_raises_key_error(self, tmp_path: Path) -> None:
        """Two provider configs with the same 'id' raise KeyError on load."""
        # Arrange
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data_a = dict(OPENAI_DATA)
        data_b = dict(OPENAI_DATA)  # same id: "openai"
        (prov_dir / "a.json").write_text(json.dumps(data_a), encoding="utf-8")
        (prov_dir / "b.json").write_text(json.dumps(data_b), encoding="utf-8")

        # Act / Assert
        with pytest.raises(KeyError, match="Duplicate provider id"):
            ProviderRegistry.load(tmp_path)


# ---------------------------------------------------------------------------
# Registry: empty directory
# ---------------------------------------------------------------------------


class TestProviderRegistryEmpty:
    """Tests for loading from a directory with no provider JSON files."""

    def test_load_empty_providers_dir(self, tmp_path: Path) -> None:
        """Loading from an empty providers directory yields an empty registry."""
        # Arrange
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()

        # Act
        registry = ProviderRegistry.load(tmp_path)

        # Assert
        assert registry.list_ids() == []

    def test_load_missing_providers_dir(self, tmp_path: Path) -> None:
        """Loading when the providers directory does not exist yields an empty registry."""
        # Arrange — no "providers" subdirectory

        # Act
        registry = ProviderRegistry.load(tmp_path)

        # Assert
        assert registry.list_ids() == []
