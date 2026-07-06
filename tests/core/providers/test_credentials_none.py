"""Tests for credential resolution on keyless ``none`` connections."""

from __future__ import annotations

from core.providers.credentials import ProviderCredentialResolver
from core.providers.providers import (
    AuthConfig,
    ConnectionConfig,
    ProviderConfig,
    ProviderRegistry,
)


def _registry() -> ProviderRegistry:
    provider_config = ProviderConfig(
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
    return ProviderRegistry({"ollama": provider_config})


class TestNoneConnectionAccounts:
    def test_list_accounts_returns_single_always_usable_default(self) -> None:
        """A keyless connection has exactly one implicit, always-usable account."""
        # Arrange
        resolver = ProviderCredentialResolver(_registry(), process_env={})

        # Act
        accounts = resolver.list_accounts("ollama", "local")

        # Assert
        assert len(accounts) == 1
        assert accounts[0].id == "default"
        assert accounts[0].usable is True
        assert accounts[0].source == "none"
        assert accounts[0].credential_key == ""

    def test_has_credentials_is_true_without_any_environment(self) -> None:
        """``none`` connections always report credentials configured."""
        # Arrange
        resolver = ProviderCredentialResolver(_registry(), process_env={})

        # Act / Assert
        assert resolver.has_credentials("ollama", "ollama:local") is True
        assert resolver.has_credentials("ollama") is True

    def test_get_credentials_returns_empty_string(self) -> None:
        """The resolved credential value for a keyless connection is empty."""
        # Arrange
        resolver = ProviderCredentialResolver(_registry(), process_env={})

        # Act / Assert
        assert resolver.get_credentials("ollama", "ollama:local") == ""

    def test_get_credentials_with_explicit_account_returns_empty_string(self) -> None:
        # Arrange
        resolver = ProviderCredentialResolver(_registry(), process_env={})

        # Act / Assert
        assert resolver.get_credentials("ollama", "ollama:local:default") == ""

    def test_resolve_account_id_returns_default(self) -> None:
        # Arrange
        resolver = ProviderCredentialResolver(_registry(), process_env={})

        # Act / Assert
        assert resolver.resolve_account_id("ollama", "local") == "default"

    def test_sibling_api_key_connection_still_requires_credential(self) -> None:
        """The keyless branch must not leak onto sibling api_key connections."""
        # Arrange
        resolver = ProviderCredentialResolver(_registry(), process_env={})

        # Act / Assert
        assert resolver.has_credentials("ollama", "ollama:cloud") is False

    def test_sibling_api_key_connection_resolves_with_credential(self) -> None:
        # Arrange
        resolver = ProviderCredentialResolver(
            _registry(), process_env={"OLLAMA_API_KEY": "sk-cloud"}
        )

        # Act / Assert
        assert resolver.has_credentials("ollama", "ollama:cloud") is True
        assert resolver.get_credentials("ollama", "ollama:cloud") == "sk-cloud"
