"""Providers: configuration behavior."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from core.providers.providers import (
    AuthConfig,
    ConnectionConfig,
    OAuthConfig,
    ProviderConfig,
    ProviderRegistry,
)
from tests.core.providers.providers_helpers import (
    OPENAI_DATA,
)
from tests.core.providers.providers_helpers import (
    _clear_cache as _clear_cache,
)
from tests.core.providers.providers_helpers import (
    providers_dir as providers_dir,
)


# ProviderConfig dataclass
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
            connections=[
                ConnectionConfig(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=AuthConfig(
                        header="Authorization",
                        prefix="Bearer ",
                        credential_key="TEST_KEY",
                    ),
                )
            ],
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

    def test_auth_config_surface_is_credential_centric_only(self) -> None:
        """AuthConfig exposes only credential-centric fields and no env-key shim."""
        # Arrange
        auth = AuthConfig(
            header="Authorization",
            prefix="Bearer ",
            credential_key="TEST_KEY",
        )

        # Act
        field_names = [field.name for field in fields(AuthConfig)]

        # Assert
        assert field_names == ["header", "prefix", "credential_key"]
        assert not hasattr(auth, "env_key")

    def test_connection_config_creation_and_immutability(self) -> None:
        """ConnectionConfig stores auth metadata and is immutable."""
        # Arrange
        connection = ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
                credential_key="TEST_KEY",
            ),
            base_url="https://enterprise.example.com/v1",
        )

        # Act / Assert
        assert connection.id == "api-key"
        assert connection.type == "api_key"
        assert connection.label == "API Key"
        assert connection.auth.credential_key == "TEST_KEY"
        assert connection.base_url == "https://enterprise.example.com/v1"
        with pytest.raises(FrozenInstanceError):
            connection.label = "Changed"  # type: ignore[misc]


# Connection parsing
class TestConnectionParsing:
    """Tests for correct parsing of connection fields from JSON data."""

    def test_openai_connections_fields(self, providers_dir: Path) -> None:
        """OpenAI connection fields parse correctly from JSON."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        config = registry.get("openai")

        # Assert
        assert [connection.id for connection in config.connections] == ["oauth", "api-key"]
        assert config.connections[0].type == "oauth"
        assert config.connections[0].label == "OAuth"
        assert config.connections[0].auth.credential_key == "OPENAI_OAUTH_TOKEN"
        assert config.connections[1].type == "api_key"
        assert config.connections[1].auth.header == "Authorization"
        assert config.connections[1].auth.prefix == "Bearer "
        assert config.connections[1].auth.credential_key == "OPENAI_API_KEY"

    def test_anthropic_connection_fields(self, providers_dir: Path) -> None:
        """Anthropic x-api-key connection fields parse correctly from JSON."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        config = registry.get("anthropic")

        # Assert
        connection = config.connections[0]
        assert connection.id == "api-key"
        assert connection.type == "api_key"
        assert connection.auth.header == "x-api-key"
        assert connection.auth.prefix == ""
        assert connection.auth.credential_key == "ANTHROPIC_API_KEY"

    def test_openrouter_connection_fields(self, providers_dir: Path) -> None:
        """OpenRouter connection fields parse correctly from JSON."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)

        # Act
        config = registry.get("openrouter")

        # Assert
        connection = config.connections[0]
        assert connection.id == "api-key"
        assert connection.type == "api_key"
        assert connection.auth.header == "Authorization"
        assert connection.auth.prefix == "Bearer "
        assert connection.auth.credential_key == "OPENROUTER_API_KEY"

    def test_get_connection_returns_matching_local_id(self, providers_dir: Path) -> None:
        """ProviderConfig.get_connection() returns the matching local ID."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)
        config = registry.get("openai")

        # Act
        connection = config.get_connection("api-key")

        # Assert
        assert connection.label == "API Key"
        assert connection.auth.credential_key == "OPENAI_API_KEY"

    def test_get_connection_unknown_local_id_raises_key_error(self, providers_dir: Path) -> None:
        """ProviderConfig.get_connection() raises KeyError for an unknown local ID."""
        # Arrange
        registry = ProviderRegistry.load(providers_dir)
        config = registry.get("openai")

        # Act / Assert
        with pytest.raises(KeyError):
            config.get_connection("missing")

    def test_connection_base_url_override_parses_from_json(self, tmp_path: Path) -> None:
        """Connection base_url overrides parse from provider JSON."""
        # Arrange
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENAI_DATA)
        connection = dict(OPENAI_DATA["connections"][1])
        connection["base_url"] = "https://enterprise.example.com/v1"
        data["connections"] = [connection]
        (prov_dir / "openai.json").write_text(json.dumps(data), encoding="utf-8")

        # Act
        registry = ProviderRegistry.load(tmp_path)
        config = registry.get("openai")

        # Assert
        assert config.get_connection("api-key").base_url == "https://enterprise.example.com/v1"

    def test_subscription_connection_oauth_device_flow_fields_parse(
        self,
        tmp_path: Path,
    ) -> None:
        """A subscription connection's Codex Device Flow metadata parses from JSON."""
        # Arrange
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = {
            "id": "openai-subscription",
            "name": "OpenAI Subscription",
            "adapter": "openai_subscription",
            "base_url": "https://chatgpt.com/backend-api",
            "models_endpoint": "/codex/models",
            "connections": [
                {
                    "id": "oauth",
                    "type": "oauth",
                    "label": "ChatGPT Plus/Pro",
                    "auth": {"header": "Authorization", "prefix": "Bearer "},
                    "oauth": {
                        "flow": "device",
                        "device_flow": "openai_codex",
                        "client_id": "client-id",
                        "device_auth_url": "https://auth.openai.com/device/usercode",
                        "token_url": "https://auth.openai.com/oauth/token",
                        "verification_uri": "https://auth.openai.com/codex/device",
                        "redirect_uri": "https://auth.openai.com/deviceauth/callback",
                        "expires_in": 600,
                        "scopes": ["openid"],
                    },
                }
            ],
        }
        (prov_dir / "openai-subscription.json").write_text(
            json.dumps(data),
            encoding="utf-8",
        )

        # Act
        registry = ProviderRegistry.load(tmp_path)
        config = registry.get("openai-subscription")
        oauth = config.get_connection("oauth").oauth

        # Assert
        assert config.models_endpoint == "/codex/models"
        assert oauth == OAuthConfig(
            flow="device",
            client_id="client-id",
            device_auth_url="https://auth.openai.com/device/usercode",
            token_url="https://auth.openai.com/oauth/token",
            scopes=["openid"],
            device_flow="openai_codex",
            verification_uri="https://auth.openai.com/codex/device",
            redirect_uri="https://auth.openai.com/deviceauth/callback",
            expires_in=600,
        )


# Defaults, extra_headers, models_endpoint
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
