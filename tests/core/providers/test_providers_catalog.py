"""Providers: catalog behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.providers.providers import (
    ProviderConfig,
    ProviderRegistry,
)
from core.utils.errors import ConfigError
from tests.core.providers.providers_helpers import (
    OPENROUTER_DATA,
)
from tests.core.providers.providers_helpers import (
    _clear_cache as _clear_cache,
)


# models_dev_id — vBot↔models.dev provider-id mapping (Phase 3 consumer)
class TestProviderModelsDevId:
    """The optional ``models_dev_id`` field and its id-defaulting accessor."""

    def test_defaults_to_none_and_accessor_falls_back_to_id(self) -> None:
        """When ``models_dev_id`` is absent, the accessor returns the vBot id."""

        config = ProviderConfig(
            id="opencode-go",
            name="OpenCode Go",
            adapter="opencode_go",
            base_url="https://example.test/v1",
        )

        assert config.models_dev_id is None
        assert config.effective_models_dev_id() == "opencode-go"

    def test_explicit_models_dev_id_is_used_by_accessor(self) -> None:
        config = ProviderConfig(
            id="opencode-go",
            name="OpenCode Go",
            adapter="opencode_go",
            base_url="https://example.test/v1",
            models_dev_id="opencode",
        )

        assert config.effective_models_dev_id() == "opencode"

    def test_registry_parses_models_dev_id_from_json(self, tmp_path: Path) -> None:
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENROUTER_DATA)
        data["models_dev_id"] = "openrouter"
        (prov_dir / "openrouter.json").write_text(json.dumps(data), encoding="utf-8")

        registry = ProviderRegistry.load(tmp_path)

        assert registry.get("openrouter").models_dev_id == "openrouter"

    def test_registry_defaults_models_dev_id_to_none_when_absent(self, tmp_path: Path) -> None:
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        (prov_dir / "openrouter.json").write_text(json.dumps(OPENROUTER_DATA), encoding="utf-8")

        registry = ProviderRegistry.load(tmp_path)
        config = registry.get("openrouter")

        assert config.models_dev_id is None
        assert config.effective_models_dev_id() == "openrouter"

    def test_non_string_models_dev_id_raises_config_error(self, tmp_path: Path) -> None:
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENROUTER_DATA)
        data["models_dev_id"] = ["not", "a", "string"]
        (prov_dir / "openrouter.json").write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(ConfigError):
            ProviderRegistry.load(tmp_path)


# catalog_exclusions — provider listings that advertise unusable wire ids
class TestProviderCatalogExclusions:
    def test_defaults_to_empty(self) -> None:
        config = ProviderConfig(
            id="opencode-go",
            name="OpenCode Go",
            adapter="opencode_go",
            base_url="https://example.test/v1",
        )

        assert config.catalog_exclusions == frozenset()

    def test_registry_parses_exact_model_ids(self, tmp_path: Path) -> None:
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENROUTER_DATA)
        data["catalog_exclusions"] = ["broken-preview", "legacy-model"]
        (prov_dir / "openrouter.json").write_text(json.dumps(data), encoding="utf-8")

        config = ProviderRegistry.load(tmp_path).get("openrouter")

        assert config.catalog_exclusions == frozenset({"broken-preview", "legacy-model"})

    @pytest.mark.parametrize(
        "value",
        ["broken-preview", [""], [123]],
    )
    def test_registry_rejects_invalid_catalog_exclusions(
        self,
        tmp_path: Path,
        value: object,
    ) -> None:
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENROUTER_DATA)
        data["catalog_exclusions"] = value
        (prov_dir / "openrouter.json").write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(ConfigError):
            ProviderRegistry.load(tmp_path)


# Registry: empty directory
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
