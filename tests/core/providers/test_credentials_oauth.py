"""Tests for OAuth-aware provider credential resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.providers.credentials import ProviderCredentialResolver
from core.providers.providers import (
    AuthConfig,
    ConnectionConfig,
    OAuthConfig,
    ProviderConfig,
    ProviderRegistry,
)
from core.providers.token_store import OAuthToken, TokenStore
from core.utils.errors import ConfigError


def _registry() -> ProviderRegistry:
    provider_config = ProviderConfig(
        id="github-copilot",
        name="GitHub Copilot",
        adapter="openai_compatible",
        base_url="https://api.githubcopilot.com",
        connections=[
            ConnectionConfig(
                id="oauth",
                type="oauth",
                label="Sign in with GitHub",
                auth=AuthConfig(header="Authorization", prefix="Bearer "),
                oauth=OAuthConfig(
                    flow="device",
                    client_id="client-id",
                    device_auth_url="https://github.com/login/device/code",
                    token_url="https://github.com/login/oauth/access_token",
                    scopes=["copilot"],
                    token_exchange_url="https://api.github.com/copilot_internal/v2/token",
                ),
            ),
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="COPILOT_API_KEY",
                ),
            ),
        ],
    )
    return ProviderRegistry({"github-copilot": provider_config})


def test_has_credentials_oauth_with_token_present(tmp_path: Path) -> None:
    """OAuth credential checks are true when a stored token is usable."""
    # Arrange
    token_store = TokenStore(tmp_path)
    token_store.save("github-copilot", "oauth", OAuthToken(access_token="access-secret"))
    resolver = ProviderCredentialResolver(_registry(), process_env={}, token_store=token_store)

    # Act / Assert
    assert resolver.has_credentials("github-copilot", "github-copilot:oauth") is True


def test_has_credentials_oauth_without_token(tmp_path: Path) -> None:
    """OAuth credential checks are false when no token is stored."""
    # Arrange
    token_store = TokenStore(tmp_path)
    resolver = ProviderCredentialResolver(_registry(), process_env={}, token_store=token_store)

    # Act / Assert
    assert resolver.has_credentials("github-copilot", "github-copilot:oauth") is False


def test_has_credentials_api_key_unchanged(tmp_path: Path) -> None:
    """API key credential checks still use process env / fallback credentials."""
    # Arrange
    resolver = ProviderCredentialResolver(
        _registry(),
        process_env={"COPILOT_API_KEY": "api-secret"},
        token_store=TokenStore(tmp_path),
    )

    # Act / Assert
    assert resolver.has_credentials("github-copilot", "github-copilot:api-key") is True


def test_get_credentials_oauth_with_token_returns_access_token(tmp_path: Path) -> None:
    """OAuth credential lookup returns the stored access token."""
    # Arrange
    token_store = TokenStore(tmp_path)
    token_store.save("github-copilot", "oauth", OAuthToken(access_token="access-secret"))
    resolver = ProviderCredentialResolver(_registry(), process_env={}, token_store=token_store)

    # Act
    credential = resolver.get_credentials("github-copilot", "github-copilot:oauth")

    # Assert
    assert credential == "access-secret"


def test_get_credentials_oauth_without_token_raises_config_error(tmp_path: Path) -> None:
    """OAuth credential lookup raises ConfigError when no token is stored."""
    # Arrange
    resolver = ProviderCredentialResolver(
        _registry(),
        process_env={},
        token_store=TokenStore(tmp_path),
    )

    # Act / Assert
    with pytest.raises(ConfigError, match="OAuth token"):
        resolver.get_credentials("github-copilot", "github-copilot:oauth")


def test_get_credentials_api_key_unchanged(tmp_path: Path) -> None:
    """API key credential lookup still returns the configured static credential."""
    # Arrange
    resolver = ProviderCredentialResolver(
        _registry(),
        process_env={"COPILOT_API_KEY": "api-secret"},
        token_store=TokenStore(tmp_path),
    )

    # Act
    credential = resolver.get_credentials("github-copilot", "github-copilot:api-key")

    # Assert
    assert credential == "api-secret"
