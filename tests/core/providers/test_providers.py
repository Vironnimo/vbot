"""Tests for ProviderConfig dataclass and ProviderRegistry.

Verifies loading from JSON fixtures, lookup by provider ID, immutability,
missing-provider errors, caching behaviour, and correct parsing of connection
fields, extra_headers, defaults, and models_endpoint.
"""

import json
from collections.abc import Generator
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import Any

import pytest

from core.providers.providers import (
    GLOBAL_CONTEXT_WINDOW_FLOOR,
    AuthConfig,
    ConnectionConfig,
    OAuthConfig,
    ProviderConfig,
    ProviderRegistry,
    _registry_cache,
    resolve_context_window,
)
from core.utils.errors import ConfigError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

OPENAI_DATA: dict[str, Any] = {
    "id": "openai",
    "name": "OpenAI",
    "adapter": "openai_compatible",
    "base_url": "https://api.openai.com/v1",
    "connections": [
        {
            "id": "oauth",
            "type": "oauth",
            "label": "OAuth",
            "auth": {
                "header": "Authorization",
                "prefix": "Bearer ",
                "credential_key": "OPENAI_OAUTH_TOKEN",
            },
        },
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
    ],
    "defaults": {"max_tokens": 4096, "temperature": 0.7},
}

OPENROUTER_DATA: dict[str, Any] = {
    "id": "openrouter",
    "name": "OpenRouter",
    "adapter": "openai_compatible",
    "base_url": "https://openrouter.ai/api/v1",
    "connections": [
        {
            "id": "api-key",
            "type": "api_key",
            "label": "API Key",
            "auth": {
                "header": "Authorization",
                "prefix": "Bearer ",
                "credential_key": "OPENROUTER_API_KEY",
            },
        }
    ],
    "defaults": {"max_tokens": 4096, "temperature": 0.7},
    "extra_headers": {"HTTP-Referer": "https://vbot.app", "X-Title": "vBot"},
    "models_endpoint": "/models",
}

ANTHROPIC_DATA: dict[str, Any] = {
    "id": "anthropic",
    "name": "Anthropic",
    "adapter": "anthropic",
    "base_url": "https://api.anthropic.com/v1",
    "connections": [
        {
            "id": "api-key",
            "type": "api_key",
            "label": "API Key",
            "auth": {
                "header": "x-api-key",
                "prefix": "",
                "credential_key": "ANTHROPIC_API_KEY",
            },
        }
    ],
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


# ---------------------------------------------------------------------------
# Connection parsing
# ---------------------------------------------------------------------------


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
        with pytest.raises(KeyError, match="missing"):
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
        with pytest.raises(KeyError, match="Duplicate connection id"):
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
        with pytest.raises(ConfigError, match="missing required field 'connections'"):
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
        with pytest.raises(ConfigError, match="Unknown connection type"):
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
        with pytest.raises(ConfigError, match="requires 'credential_key'"):
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
        with pytest.raises(ConfigError, match="must not contain '--' or ':'"):
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
        with pytest.raises(ConfigError, match="must not contain ':'"):
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
        with pytest.raises(ConfigError, match="Unknown OAuth flow"):
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
        with pytest.raises(ConfigError, match="Unknown OAuth device_flow"):
            ProviderRegistry.load(tmp_path)


# ---------------------------------------------------------------------------
# Connection mode and models_endpoint (per-connection wire variant)
# ---------------------------------------------------------------------------


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
        with pytest.raises(ConfigError, match="mode must be a string"):
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
        with pytest.raises(ConfigError, match="models_endpoint must be a string"):
            ProviderRegistry.load(tmp_path)


# ---------------------------------------------------------------------------
# models_dev_id — vBot↔models.dev provider-id mapping (Phase 3 consumer)
# ---------------------------------------------------------------------------


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

        with pytest.raises(ConfigError, match="models_dev_id must be a string"):
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


# ---------------------------------------------------------------------------
# context_window — per-provider read-side default + global floor (Phase 6)
# ---------------------------------------------------------------------------


class TestProviderContextWindowDefault:
    """The optional per-provider ``context_window`` read-side default field."""

    def test_defaults_to_none(self) -> None:
        config = ProviderConfig(
            id="p",
            name="P",
            adapter="openai_compatible",
            base_url="https://example.test/v1",
        )

        assert config.context_window is None

    def test_registry_parses_context_window_from_json(self, tmp_path: Path) -> None:
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENROUTER_DATA)
        data["context_window"] = 128000
        (prov_dir / "openrouter.json").write_text(json.dumps(data), encoding="utf-8")

        registry = ProviderRegistry.load(tmp_path)

        assert registry.get("openrouter").context_window == 128000

    def test_registry_defaults_context_window_to_none_when_absent(self, tmp_path: Path) -> None:
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        (prov_dir / "openrouter.json").write_text(json.dumps(OPENROUTER_DATA), encoding="utf-8")

        assert ProviderRegistry.load(tmp_path).get("openrouter").context_window is None

    @pytest.mark.parametrize("bad_value", [0, -1, "128000", 1.5, True])
    def test_non_positive_or_non_int_context_window_raises(
        self, tmp_path: Path, bad_value: Any
    ) -> None:
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENROUTER_DATA)
        data["context_window"] = bad_value
        (prov_dir / "openrouter.json").write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(ConfigError, match="context_window must be a positive integer"):
            ProviderRegistry.load(tmp_path)


class TestResolveContextWindow:
    """The shared read-side resolution chain: model → provider default → floor."""

    def _provider(self, context_window: int | None) -> ProviderConfig:
        return ProviderConfig(
            id="p",
            name="P",
            adapter="openai_compatible",
            base_url="https://example.test/v1",
            context_window=context_window,
        )

    def test_model_window_wins(self) -> None:
        assert resolve_context_window(262144, self._provider(50000)) == 262144

    def test_provider_default_used_when_model_window_is_none(self) -> None:
        assert resolve_context_window(None, self._provider(50000)) == 50000

    def test_global_floor_when_neither_supplies_one(self) -> None:
        assert resolve_context_window(None, self._provider(None)) == GLOBAL_CONTEXT_WINDOW_FLOOR

    def test_global_floor_when_provider_config_is_none(self) -> None:
        assert resolve_context_window(None, None) == GLOBAL_CONTEXT_WINDOW_FLOOR

    def test_stray_zero_model_window_treated_as_unknown(self) -> None:
        # A fake 0 from an old catalog must never reach a caller as a budget.
        assert resolve_context_window(0, self._provider(50000)) == 50000
        assert resolve_context_window(0, None) == GLOBAL_CONTEXT_WINDOW_FLOOR

    def test_return_is_always_positive(self) -> None:
        assert resolve_context_window(None, None) > 0
