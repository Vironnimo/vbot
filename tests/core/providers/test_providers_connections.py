"""Providers: connections behavior."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.providers.providers import (
    AuthConfig,
    ConnectionConfig,
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


# Connection mode and models_endpoint (per-connection wire variant)
class TestConnectionModeAndModelsEndpoint:
    """Tests for the per-connection ``mode`` and ``models_endpoint`` fields."""

    def test_connection_config_defaults_to_none_for_mode_and_models_endpoint(self) -> None:
        """A ConnectionConfig built without the new fields exposes them as None."""
        # Arrange / Act
        connection = ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
                credential_key="TEST_KEY",
            ),
        )

        # Assert
        assert connection.mode is None
        assert connection.models_endpoint is None

    def test_connection_config_accepts_mode_and_models_endpoint(self) -> None:
        """A ConnectionConfig built with both fields stores them as provided."""
        # Arrange / Act
        connection = ConnectionConfig(
            id="subscription",
            type="oauth",
            label="ChatGPT Plus/Pro",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
            ),
            mode="codex_responses",
            models_endpoint="/codex/models",
        )

        # Assert
        assert connection.mode == "codex_responses"
        assert connection.models_endpoint == "/codex/models"

    def test_subscription_connection_parses_mode_and_models_endpoint(
        self,
        tmp_path: Path,
    ) -> None:
        """A connection carrying mode + models_endpoint parses both onto the dataclass."""
        # Arrange
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = {
            "id": "openai",
            "name": "OpenAI",
            "adapter": "openai",
            "base_url": "https://chatgpt.com/backend-api",
            "connections": [
                {
                    "id": "subscription",
                    "type": "oauth",
                    "label": "ChatGPT Plus/Pro",
                    "auth": {"header": "Authorization", "prefix": "Bearer "},
                    "mode": "codex_responses",
                    "models_endpoint": "/codex/models",
                }
            ],
        }
        (prov_dir / "openai.json").write_text(json.dumps(data), encoding="utf-8")

        # Act
        registry = ProviderRegistry.load(tmp_path)
        connection = registry.get("openai").get_connection("subscription")

        # Assert
        assert connection.mode == "codex_responses"
        assert connection.models_endpoint == "/codex/models"

    def test_connection_without_mode_or_models_endpoint_remains_none(
        self,
        providers_dir: Path,
    ) -> None:
        """Connections without mode/models_endpoint keep both fields as None."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        config = registry.get("openai")

        # Assert
        assert config.get_connection("api-key").mode is None
        assert config.get_connection("api-key").models_endpoint is None
        assert config.get_connection("oauth").mode is None
        assert config.get_connection("oauth").models_endpoint is None

    def test_provider_level_models_endpoint_is_independent_of_connection_field(
        self,
        tmp_path: Path,
    ) -> None:
        """Per-connection models_endpoint does not affect provider-level models_endpoint."""
        # Arrange
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = {
            "id": "openai",
            "name": "OpenAI",
            "adapter": "openai",
            "base_url": "https://chatgpt.com/backend-api",
            "models_endpoint": "/provider/models",
            "connections": [
                {
                    "id": "api-key",
                    "type": "api_key",
                    "label": "API Key",
                    "auth": {
                        "header": "Authorization",
                        "prefix": "Bearer ",
                        "credential_key": "OPENAI_API_KEY",
                    },
                },
                {
                    "id": "subscription",
                    "type": "oauth",
                    "label": "ChatGPT Plus/Pro",
                    "auth": {"header": "Authorization", "prefix": "Bearer "},
                    "models_endpoint": "/codex/models",
                },
            ],
        }
        (prov_dir / "openai.json").write_text(json.dumps(data), encoding="utf-8")

        # Act
        registry = ProviderRegistry.load(tmp_path)
        config = registry.get("openai")

        # Assert
        assert config.models_endpoint == "/provider/models"
        assert config.get_connection("api-key").models_endpoint is None
        assert config.get_connection("subscription").models_endpoint == "/codex/models"

    def test_non_string_mode_raises_config_error(self, tmp_path: Path) -> None:
        """A non-string ``mode`` value raises ConfigError with field context."""
        # Arrange
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENAI_DATA)
        connection = dict(OPENAI_DATA["connections"][1])
        connection["mode"] = 42
        data["connections"] = [connection]
        (prov_dir / "openai.json").write_text(json.dumps(data), encoding="utf-8")

        # Act / Assert
        with pytest.raises(ConfigError):
            ProviderRegistry.load(tmp_path)

    def test_non_string_connection_models_endpoint_raises_config_error(
        self, tmp_path: Path
    ) -> None:
        """A non-string connection-level ``models_endpoint`` raises ConfigError."""
        # Arrange
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENAI_DATA)
        connection = dict(OPENAI_DATA["connections"][1])
        connection["models_endpoint"] = ["not", "a", "string"]
        data["connections"] = [connection]
        (prov_dir / "openai.json").write_text(json.dumps(data), encoding="utf-8")

        # Act / Assert
        with pytest.raises(ConfigError):
            ProviderRegistry.load(tmp_path)


# Connection type "none" — keyless connections (e.g. local Ollama)
class TestNoneConnectionType:
    """Tests for the keyless ``none`` connection type."""

    def _write_provider(self, tmp_path: Path, connection: dict[str, Any]) -> Path:
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = {
            "id": "ollama",
            "name": "Ollama",
            "adapter": "ollama",
            "base_url": "http://localhost:11434",
            "connections": [connection],
        }
        (prov_dir / "ollama.json").write_text(json.dumps(data), encoding="utf-8")
        return tmp_path

    def test_none_connection_without_auth_block_parses(self, tmp_path: Path) -> None:
        """A ``none`` connection needs no auth block and gets an empty AuthConfig."""
        # Arrange
        resources = self._write_provider(
            tmp_path, {"id": "local", "type": "none", "label": "Local"}
        )

        # Act
        registry = ProviderRegistry.load(resources)
        connection = registry.get("ollama").get_connection("local")

        # Assert
        assert connection.type == "none"
        assert connection.auth == AuthConfig(header="", prefix="", credential_key="")

    def test_none_connection_with_auth_block_parses_leniently(self, tmp_path: Path) -> None:
        """An optional auth block on a ``none`` connection parses without required fields."""
        # Arrange
        resources = self._write_provider(
            tmp_path,
            {"id": "local", "type": "none", "label": "Local", "auth": {}},
        )

        # Act
        registry = ProviderRegistry.load(resources)
        connection = registry.get("ollama").get_connection("local")

        # Assert
        assert connection.auth == AuthConfig(header="", prefix="", credential_key="")

    def test_api_key_connection_still_requires_auth_block(self, tmp_path: Path) -> None:
        """Non-keyless connection types keep requiring the auth block."""
        # Arrange
        resources = self._write_provider(
            tmp_path,
            {"id": "cloud", "type": "api_key", "label": "Cloud"},
        )

        # Act / Assert
        with pytest.raises(ConfigError):
            ProviderRegistry.load(resources)

    def test_oauth_connection_still_requires_auth_block(self, tmp_path: Path) -> None:
        """OAuth connections keep requiring the auth block."""
        # Arrange
        resources = self._write_provider(
            tmp_path,
            {"id": "sso", "type": "oauth", "label": "SSO"},
        )

        # Act / Assert
        with pytest.raises(ConfigError):
            ProviderRegistry.load(resources)

    def test_auto_refresh_flag_parses(self, tmp_path: Path) -> None:
        """The per-connection auto_refresh flag parses from JSON."""
        # Arrange
        resources = self._write_provider(
            tmp_path,
            {"id": "local", "type": "none", "label": "Local", "auto_refresh": True},
        )

        # Act
        connection = ProviderRegistry.load(resources).get("ollama").get_connection("local")

        # Assert
        assert connection.auto_refresh is True

    def test_auto_refresh_defaults_to_false(self, tmp_path: Path) -> None:
        # Arrange
        resources = self._write_provider(
            tmp_path, {"id": "local", "type": "none", "label": "Local"}
        )

        # Act / Assert
        assert (
            ProviderRegistry.load(resources).get("ollama").get_connection("local").auto_refresh
            is False
        )

    def test_non_boolean_auto_refresh_raises_config_error(self, tmp_path: Path) -> None:
        # Arrange
        resources = self._write_provider(
            tmp_path,
            {"id": "local", "type": "none", "label": "Local", "auto_refresh": "yes"},
        )

        # Act / Assert
        with pytest.raises(ConfigError):
            ProviderRegistry.load(resources)

    def test_public_catalog_flag_parses(self, tmp_path: Path) -> None:
        resources = self._write_provider(
            tmp_path,
            {
                "id": "cloud",
                "type": "api_key",
                "label": "Cloud",
                "catalog_requires_credentials": False,
                "auth": {
                    "header": "Authorization",
                    "prefix": "Bearer ",
                    "credential_key": "OLLAMA_API_KEY",
                },
            },
        )

        connection = ProviderRegistry.load(resources).get("ollama").get_connection("cloud")

        assert connection.catalog_requires_credentials is False

    def test_catalog_requires_credentials_defaults_to_true(self, tmp_path: Path) -> None:
        resources = self._write_provider(
            tmp_path,
            {"id": "local", "type": "none", "label": "Local"},
        )

        connection = ProviderRegistry.load(resources).get("ollama").get_connection("local")

        assert connection.catalog_requires_credentials is True

    def test_non_boolean_public_catalog_flag_raises_config_error(self, tmp_path: Path) -> None:
        resources = self._write_provider(
            tmp_path,
            {
                "id": "local",
                "type": "none",
                "label": "Local",
                "catalog_requires_credentials": "no",
            },
        )

        with pytest.raises(ConfigError):
            ProviderRegistry.load(resources)
