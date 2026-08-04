"""Tests for connection enablement and usability on the credential resolver."""

from __future__ import annotations

from core.providers.credentials import ProviderCredentialResolver
from core.providers.providers import (
    AuthConfig,
    ConnectionConfig,
    ProviderConfig,
    ProviderRegistry,
    connection_default_enabled,
)


def _registry() -> ProviderRegistry:
    ollama = ProviderConfig(
        id="ollama",
        name="Ollama",
        adapter="ollama",
        base_url="http://localhost:11434",
        connections=[
            ConnectionConfig(
                id="local",
                type="none",
                label="Local",
                auth=AuthConfig(header="", prefix="", credential_key=""),
            ),
            ConnectionConfig(
                id="cloud",
                type="api_key",
                label="Ollama Cloud",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OLLAMA_API_KEY",
                ),
            ),
        ],
    )
    openai = ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai",
        base_url="https://api.openai.com/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENAI_API_KEY",
                ),
            ),
        ],
    )
    return ProviderRegistry({"ollama": ollama, "openai": openai})


def _resolver(
    overrides: dict[str, bool] | None = None,
    process_env: dict[str, str] | None = None,
) -> ProviderCredentialResolver:
    return ProviderCredentialResolver(
        _registry(),
        process_env=process_env or {},
        enabled_overrides_loader=(lambda: overrides) if overrides is not None else None,
    )


class TestConnectionDefaultEnabled:
    def test_keyed_connection_defaults_enabled(self) -> None:
        connection = _registry().get("openai").get_connection("api-key")
        assert connection_default_enabled(connection) is True

    def test_keyless_connection_defaults_disabled(self) -> None:
        connection = _registry().get("ollama").get_connection("local")
        assert connection_default_enabled(connection) is False


class TestIsConnectionEnabled:
    def test_keyless_disabled_without_override(self) -> None:
        """A keyless local connection starts disabled until the user opts in."""
        # Arrange
        resolver = _resolver()

        # Act / Assert
        assert resolver.is_connection_enabled("ollama", "ollama:local") is False

    def test_keyed_enabled_without_override(self) -> None:
        resolver = _resolver()

        assert resolver.is_connection_enabled("openai", "openai:api-key") is True

    def test_override_enables_keyless_connection(self) -> None:
        resolver = _resolver(overrides={"ollama:local": True})

        assert resolver.is_connection_enabled("ollama", "ollama:local") is True

    def test_override_disables_keyed_connection(self) -> None:
        resolver = _resolver(overrides={"openai:api-key": False})

        assert resolver.is_connection_enabled("openai", "openai:api-key") is False

    def test_account_part_is_ignored(self) -> None:
        """Enablement is connection-level; an account suffix changes nothing."""
        resolver = _resolver(overrides={"openai:api-key": False})

        assert resolver.is_connection_enabled("openai", "openai:api-key:work") is False

    def test_provider_level_any_connection_enabled(self) -> None:
        # ollama:cloud is keyed → enabled by default, so the provider counts.
        resolver = _resolver()

        assert resolver.is_connection_enabled("ollama") is True

    def test_provider_level_all_connections_disabled(self) -> None:
        resolver = _resolver(overrides={"ollama:cloud": False})

        assert resolver.is_connection_enabled("ollama") is False


class TestIsConnectionAdded:
    def test_keyless_requires_explicit_override_presence(self) -> None:
        assert _resolver().is_connection_added("ollama", "ollama:local") is False
        assert (
            _resolver(overrides={"ollama:local": True}).is_connection_added(
                "ollama", "ollama:local"
            )
            is True
        )

    def test_disabled_keyless_connection_remains_added(self) -> None:
        resolver = _resolver(overrides={"ollama:local": False})

        assert resolver.is_connection_added("ollama", "ollama:local") is True
        assert resolver.is_connection_enabled("ollama", "ollama:local") is False

    def test_keyed_connection_is_added_when_it_has_credentials(self) -> None:
        resolver = _resolver(process_env={"OPENAI_API_KEY": "sk-test"})

        assert resolver.is_connection_added("openai", "openai:api-key") is True


class TestIsUsable:
    def test_keyless_default_not_usable_despite_credentials(self) -> None:
        """Keyless passes the credential gate but stays unusable while disabled."""
        # Arrange
        resolver = _resolver()

        # Act / Assert
        assert resolver.has_credentials("ollama", "ollama:local") is True
        assert resolver.is_usable("ollama", "ollama:local") is False

    def test_keyless_enabled_becomes_usable(self) -> None:
        resolver = _resolver(overrides={"ollama:local": True})

        assert resolver.is_usable("ollama", "ollama:local") is True

    def test_keyed_with_credential_is_usable(self) -> None:
        resolver = _resolver(process_env={"OPENAI_API_KEY": "sk-test"})

        assert resolver.is_usable("openai", "openai:api-key") is True

    def test_keyed_disabled_with_credential_not_usable(self) -> None:
        """Disabling wins over an ambient environment credential."""
        resolver = _resolver(
            overrides={"openai:api-key": False},
            process_env={"OPENAI_API_KEY": "sk-test"},
        )

        assert resolver.is_usable("openai", "openai:api-key") is False

    def test_keyed_enabled_without_credential_not_usable(self) -> None:
        resolver = _resolver()

        assert resolver.is_usable("openai", "openai:api-key") is False

    def test_provider_level_conjunction_is_per_connection(self) -> None:
        """Enabled-only and credentialed-only connections never combine to usable."""
        # ollama:local — credentialed (keyless) but disabled;
        # ollama:cloud — enabled (keyed default) but no credential.
        resolver = _resolver()

        assert resolver.is_usable("ollama") is False

    def test_provider_level_usable_with_one_full_connection(self) -> None:
        resolver = _resolver(overrides={"ollama:local": True})

        assert resolver.is_usable("ollama") is True
