"""Providers: registry behavior."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from core.providers.providers import (
    ProviderRegistry,
)
from core.utils.errors import ConfigError
from tests.core.providers.providers_helpers import (
    OPENAI_DATA,
)
from tests.core.providers.providers_helpers import (
    _clear_cache as _clear_cache,
)
from tests.core.providers.providers_helpers import (
    providers_dir as providers_dir,
)


# Registry: loading and lookup
class TestProviderRegistryLoad:
    """Tests for ProviderRegistry.load() and provider lookup."""

    def test_tolerant_load_skips_corrupt_provider_and_keeps_valid_sibling(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        providers_dir = tmp_path / "providers"
        providers_dir.mkdir()
        providers_dir.joinpath("healthy.json").write_text(
            json.dumps(OPENAI_DATA),
            encoding="utf-8",
        )
        corrupt_path = providers_dir / "corrupt.json"
        corrupt_path.write_text('{"id":', encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="vbot.providers"):
            registry = ProviderRegistry.load(tmp_path, tolerate_invalid=True)

        assert registry.list_ids() == ["openai"]
        assert str(corrupt_path) in caplog.text

    def test_tolerant_load_survives_provider_directory_scan_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        providers_dir = tmp_path / "providers"
        providers_dir.mkdir()

        def fail_scan(*_args: object, **_kwargs: object) -> list[Path]:
            raise OSError("scan failed")

        monkeypatch.setattr(Path, "glob", fail_scan)

        with caplog.at_level(logging.WARNING, logger="vbot.providers"):
            registry = ProviderRegistry.load(
                tmp_path,
                custom_providers={},
                tolerate_invalid=True,
            )

        assert registry.list_ids() == []
        assert str(providers_dir) in caplog.text

    def test_tolerant_load_does_not_populate_strict_registry_cache(self, tmp_path: Path) -> None:
        providers_dir = tmp_path / "providers"
        providers_dir.mkdir()
        providers_dir.joinpath("corrupt.json").write_text('{"id":', encoding="utf-8")

        assert ProviderRegistry.load(tmp_path, tolerate_invalid=True).list_ids() == []
        with pytest.raises(json.JSONDecodeError):
            ProviderRegistry.load(tmp_path)

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
        with pytest.raises(KeyError):
            registry.get("nonexistent")

    def test_get_missing_provider_error_includes_available_ids(self, providers_dir: Path) -> None:
        """The KeyError message lists available provider IDs."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act / Assert
        with pytest.raises(KeyError, match="anthropic"):
            registry.get("no-such-provider")


# Registry: caching
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


# Registry: duplicate IDs
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
        with pytest.raises(KeyError):
            ProviderRegistry.load(tmp_path)

    def test_duplicate_connection_local_id_raises_key_error(self, tmp_path: Path) -> None:
        """Duplicate connection local IDs within one provider raise KeyError."""
        # Arrange
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENAI_DATA)
        data["connections"] = [
            dict(OPENAI_DATA["connections"][0]),
            dict(OPENAI_DATA["connections"][0]),
        ]
        (prov_dir / "openai.json").write_text(json.dumps(data), encoding="utf-8")

        # Act / Assert
        with pytest.raises(KeyError):
            ProviderRegistry.load(tmp_path)


class TestProviderRegistryRequiredFields:
    """Tests for clear provider config errors on missing required fields."""

    def test_missing_connections_field_raises_config_error(self, tmp_path: Path) -> None:
        """A provider JSON without connections raises a clear ConfigError."""
        # Arrange
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENAI_DATA)
        data.pop("connections")
        (prov_dir / "openai.json").write_text(json.dumps(data), encoding="utf-8")

        # Act / Assert
        with pytest.raises(ConfigError):
            ProviderRegistry.load(tmp_path)


class TestProviderRegistryConnectionTypes:
    """Tests for connection type validation."""

    @pytest.mark.parametrize("connection_type", ["bearer", "oidc"])
    def test_unknown_connection_type_raises_config_error(
        self, tmp_path: Path, connection_type: str
    ) -> None:
        """Unknown connection types are rejected at config load time."""
        # Arrange
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENAI_DATA)
        connection = dict(OPENAI_DATA["connections"][0])
        connection["type"] = connection_type
        data["connections"] = [connection]
        (prov_dir / "openai.json").write_text(json.dumps(data), encoding="utf-8")

        # Act / Assert
        with pytest.raises(ConfigError):
            ProviderRegistry.load(tmp_path)

    def test_duplicate_connection_types_are_allowed(self, tmp_path: Path) -> None:
        """Multiple connections with the same type are allowed if local IDs differ."""
        # Arrange
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENAI_DATA)
        first = dict(OPENAI_DATA["connections"][1])
        second = dict(OPENAI_DATA["connections"][1])
        first["id"] = "primary-key"
        second["id"] = "secondary-key"
        data["connections"] = [first, second]
        (prov_dir / "openai.json").write_text(json.dumps(data), encoding="utf-8")

        # Act
        registry = ProviderRegistry.load(tmp_path)
        config = registry.get("openai")

        # Assert
        assert [connection.type for connection in config.connections] == [
            "api_key",
            "api_key",
        ]

    def test_api_key_connection_without_credential_key_raises_config_error(
        self, tmp_path: Path
    ) -> None:
        """API key connections still require a credential_key."""
        # Arrange
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENAI_DATA)
        connection = dict(OPENAI_DATA["connections"][1])
        auth = dict(connection["auth"])
        auth.pop("credential_key")
        connection["auth"] = auth
        data["connections"] = [connection]
        (prov_dir / "openai.json").write_text(json.dumps(data), encoding="utf-8")

        # Act / Assert
        with pytest.raises(ConfigError):
            ProviderRegistry.load(tmp_path)

    @pytest.mark.parametrize("local_id", ["api--key", "api:key"])
    def test_connection_id_with_ambiguous_characters_raises_config_error(
        self, tmp_path: Path, local_id: str
    ) -> None:
        """Connection ids with '--' or ':' would break token filenames and id parsing."""
        # Arrange
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENAI_DATA)
        connection = dict(OPENAI_DATA["connections"][1])
        connection["id"] = local_id
        data["connections"] = [connection]
        (prov_dir / "openai.json").write_text(json.dumps(data), encoding="utf-8")

        # Act / Assert
        with pytest.raises(ConfigError):
            ProviderRegistry.load(tmp_path)

    def test_provider_id_with_colon_raises_config_error(self, tmp_path: Path) -> None:
        """Provider ids with ':' would break the compositional connection id grammar."""
        # Arrange
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENAI_DATA)
        data["id"] = "open:ai"
        (prov_dir / "openai.json").write_text(json.dumps(data), encoding="utf-8")

        # Act / Assert
        with pytest.raises(ConfigError):
            ProviderRegistry.load(tmp_path)

    def test_unknown_oauth_flow_raises_config_error(self, tmp_path: Path) -> None:
        """Only Device Flow OAuth configs are accepted in this phase."""
        # Arrange
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENAI_DATA)
        connection = dict(OPENAI_DATA["connections"][0])
        connection["oauth"] = {
            "flow": "authorization_code",
            "client_id": "client-id",
            "device_auth_url": "https://github.com/login/device/code",
            "token_url": "https://github.com/login/oauth/access_token",
            "scopes": ["copilot"],
        }
        data["connections"] = [connection]
        (prov_dir / "openai.json").write_text(json.dumps(data), encoding="utf-8")

        # Act / Assert
        with pytest.raises(ConfigError):
            ProviderRegistry.load(tmp_path)

    def test_unknown_oauth_device_flow_raises_config_error(self, tmp_path: Path) -> None:
        """OAuth Device Flow variants are validated explicitly."""
        # Arrange
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENAI_DATA)
        connection = dict(OPENAI_DATA["connections"][0])
        connection["oauth"] = {
            "flow": "device",
            "device_flow": "unknown",
            "client_id": "client-id",
            "device_auth_url": "https://github.com/login/device/code",
            "token_url": "https://github.com/login/oauth/access_token",
            "scopes": ["copilot"],
        }
        data["connections"] = [connection]
        (prov_dir / "openai.json").write_text(json.dumps(data), encoding="utf-8")

        # Act / Assert
        with pytest.raises(ConfigError):
            ProviderRegistry.load(tmp_path)


class TestCustomProviderRegistry:
    def test_load_materializes_keyless_custom_provider(self, tmp_path: Path) -> None:
        registry = ProviderRegistry.load(
            tmp_path,
            custom_providers={
                "local-ai": {
                    "name": "Local AI",
                    "adapter": "openai_compatible",
                    "base_url": "http://127.0.0.1:8080/v1",
                    "auth": "none",
                    "models_endpoint": "/models",
                    "defaults": {},
                    "models": {},
                }
            },
        )

        provider = registry.get("local-ai")
        assert provider.custom is True
        assert provider.models_endpoint == "/models"
        assert provider.get_connection("default").type == "none"

    def test_reload_replaces_custom_provider_in_place(self, tmp_path: Path) -> None:
        registry = ProviderRegistry.load(tmp_path, custom_providers={})
        held_reference = registry

        registry.reload(
            tmp_path,
            custom_providers={
                "gateway": {
                    "name": "Gateway",
                    "adapter": "openai_compatible",
                    "base_url": "https://gateway.example/v1",
                    "auth": "api_key",
                    "models_endpoint": None,
                    "defaults": {},
                    "models": {},
                }
            },
        )

        connection = held_reference.get("gateway").get_connection("default")
        assert connection.auth.credential_key == "VBOT_CUSTOM_GATEWAY_API_KEY"
        assert connection.auth.header == "Authorization"
        assert connection.auth.prefix == "Bearer "

    def test_custom_provider_registries_are_isolated_per_runtime_data(
        self,
        tmp_path: Path,
    ) -> None:
        first = ProviderRegistry.load(
            tmp_path,
            custom_providers={
                "first": {
                    "name": "First",
                    "adapter": "openai_compatible",
                    "base_url": "https://first.example/v1",
                    "auth": "none",
                    "models_endpoint": None,
                    "defaults": {},
                    "models": {},
                }
            },
        )
        second = ProviderRegistry.load(
            tmp_path,
            custom_providers={
                "second": {
                    "name": "Second",
                    "adapter": "openai_compatible",
                    "base_url": "https://second.example/v1",
                    "auth": "none",
                    "models_endpoint": None,
                    "defaults": {},
                    "models": {},
                }
            },
        )
        bundled_only = ProviderRegistry.load(tmp_path)

        assert first.list_ids() == ["first"]
        assert second.list_ids() == ["second"]
        assert bundled_only.list_ids() == []

    def test_custom_provider_cannot_shadow_bundled_provider(
        self,
        providers_dir: Path,
    ) -> None:
        with pytest.raises(ConfigError):
            ProviderRegistry.load(
                providers_dir,
                custom_providers={
                    "openai": {
                        "name": "Shadow",
                        "adapter": "openai_compatible",
                        "base_url": "https://shadow.example/v1",
                        "auth": "none",
                        "models_endpoint": None,
                        "defaults": {},
                        "models": {},
                    }
                },
            )
