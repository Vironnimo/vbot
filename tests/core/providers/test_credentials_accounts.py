"""Tests for account-aware provider credential resolution."""

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
        id="openai",
        name="OpenAI",
        adapter="openai_compatible",
        base_url="https://api.openai.com/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENAI_API_KEY",
                ),
            ),
            ConnectionConfig(
                id="subscription",
                type="oauth",
                label="Subscription",
                auth=AuthConfig(header="Authorization", prefix="Bearer "),
                oauth=OAuthConfig(
                    flow="device",
                    client_id="client-id",
                    device_auth_url="https://auth.openai.com/device",
                    token_url="https://auth.openai.com/token",
                    scopes=["openid"],
                ),
            ),
            ConnectionConfig(
                id="oauth-stub",
                type="oauth",
                label="OAuth Stub",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENAI_STUB_KEY",
                ),
            ),
        ],
    )
    return ProviderRegistry({"openai": provider_config})


class TestListAccounts:
    def test_orders_default_first_then_alphabetical(self, tmp_path: Path) -> None:
        """Environment accounts list default first, then sorted account ids."""
        # Arrange
        resolver = ProviderCredentialResolver(
            _registry(),
            process_env={
                "OPENAI_API_KEY__ZETA": "zeta-secret",
                "OPENAI_API_KEY": "default-secret",
                "OPENAI_API_KEY__ALPHA": "alpha-secret",
            },
            token_store=TokenStore(tmp_path),
        )

        # Act
        accounts = resolver.list_accounts("openai", "api-key")

        # Assert
        assert [account.id for account in accounts] == ["default", "alpha", "zeta"]
        assert all(account.usable for account in accounts)
        assert [account.credential_key for account in accounts] == [
            "OPENAI_API_KEY",
            "OPENAI_API_KEY__ALPHA",
            "OPENAI_API_KEY__ZETA",
        ]

    def test_reports_sources_with_process_env_winning(self, tmp_path: Path) -> None:
        """Process env entries shadow data-dir entries for the same account."""
        # Arrange
        resolver = ProviderCredentialResolver(
            _registry(),
            process_env={"OPENAI_API_KEY__WORK": "process-secret"},
            fallback_credentials={
                "OPENAI_API_KEY__WORK": "data-dir-secret",
                "OPENAI_API_KEY__TEAM": "team-secret",
            },
            token_store=TokenStore(tmp_path),
        )

        # Act
        accounts = {account.id: account for account in resolver.list_accounts("openai", "api-key")}

        # Assert
        assert accounts["work"].source == "process_env"
        assert accounts["team"].source == "data_dir"

    def test_empty_process_env_value_shadows_data_dir_and_is_unusable(self, tmp_path: Path) -> None:
        """An empty process-env value shadows the data-dir credential."""
        # Arrange
        resolver = ProviderCredentialResolver(
            _registry(),
            process_env={"OPENAI_API_KEY__WORK": ""},
            fallback_credentials={"OPENAI_API_KEY__WORK": "data-dir-secret"},
            token_store=TokenStore(tmp_path),
        )

        # Act
        accounts = resolver.list_accounts("openai", "api-key")

        # Assert
        assert [(account.id, account.usable, account.source) for account in accounts] == [
            ("work", False, "process_env")
        ]

    def test_oauth_accounts_come_from_token_store(self, tmp_path: Path) -> None:
        """Token-store connections list stored accounts with oauth source."""
        # Arrange
        token_store = TokenStore(tmp_path)
        token_store.save("openai", "subscription", OAuthToken(access_token="default-secret"))
        token_store.save(
            "openai",
            "subscription",
            OAuthToken(access_token="work-secret"),
            account_id="work",
        )
        resolver = ProviderCredentialResolver(_registry(), process_env={}, token_store=token_store)

        # Act
        accounts = resolver.list_accounts("openai", "subscription")

        # Assert
        assert [(account.id, account.usable, account.source) for account in accounts] == [
            ("default", True, "oauth"),
            ("work", True, "oauth"),
        ]
        assert all(account.credential_key == "" for account in accounts)

    def test_unknown_connection_raises_config_error(self, tmp_path: Path) -> None:
        """Listing accounts for an unknown connection raises ConfigError."""
        # Arrange
        resolver = ProviderCredentialResolver(
            _registry(), process_env={}, token_store=TokenStore(tmp_path)
        )

        # Act / Assert
        with pytest.raises(ConfigError, match="Unknown connection id"):
            resolver.list_accounts("openai", "missing")


class TestHasCredentialsWithAccounts:
    def test_account_pinned_connection_id_checks_exact_account(self, tmp_path: Path) -> None:
        """provider:connection:account checks exactly that account."""
        # Arrange
        resolver = ProviderCredentialResolver(
            _registry(),
            process_env={"OPENAI_API_KEY__WORK": "work-secret"},
            token_store=TokenStore(tmp_path),
        )

        # Act / Assert
        assert resolver.has_credentials("openai", "openai:api-key:work") is True
        assert resolver.has_credentials("openai", "openai:api-key:other") is False
        assert resolver.has_credentials("openai", "openai:api-key:default") is False

    def test_connection_id_without_account_accepts_any_usable_account(self, tmp_path: Path) -> None:
        """A bare connection id is usable when any account has a credential."""
        # Arrange
        resolver = ProviderCredentialResolver(
            _registry(),
            process_env={"OPENAI_API_KEY__WORK": "work-secret"},
            token_store=TokenStore(tmp_path),
        )

        # Act / Assert
        assert resolver.has_credentials("openai", "openai:api-key") is True
        assert resolver.has_credentials("openai") is True


class TestGetCredentialsWithAccounts:
    def test_account_pinned_lookup_returns_exact_credential(self, tmp_path: Path) -> None:
        """An explicit account returns exactly that account's credential."""
        # Arrange
        resolver = ProviderCredentialResolver(
            _registry(),
            process_env={
                "OPENAI_API_KEY": "default-secret",
                "OPENAI_API_KEY__WORK": "work-secret",
            },
            token_store=TokenStore(tmp_path),
        )

        # Act / Assert
        assert resolver.get_credentials("openai", "openai:api-key:work") == "work-secret"

    def test_missing_account_error_names_account_but_not_secrets(self, tmp_path: Path) -> None:
        """The missing-account error names the account without leaking values."""
        # Arrange
        resolver = ProviderCredentialResolver(
            _registry(),
            process_env={"OPENAI_API_KEY": "default-secret"},
            token_store=TokenStore(tmp_path),
        )

        # Act / Assert
        with pytest.raises(ConfigError, match="account 'work'") as error_info:
            resolver.get_credentials("openai", "openai:api-key:work")
        assert "default-secret" not in str(error_info.value)

    def test_connection_lookup_prefers_default_account(self, tmp_path: Path) -> None:
        """Without an account, the default account wins over named ones."""
        # Arrange
        resolver = ProviderCredentialResolver(
            _registry(),
            process_env={
                "OPENAI_API_KEY__ALPHA": "alpha-secret",
                "OPENAI_API_KEY": "default-secret",
            },
            token_store=TokenStore(tmp_path),
        )

        # Act / Assert
        assert resolver.get_credentials("openai", "openai:api-key") == "default-secret"

    def test_connection_lookup_uses_first_alphabetical_without_default(
        self, tmp_path: Path
    ) -> None:
        """Without a default account, the alphabetically first account wins."""
        # Arrange
        resolver = ProviderCredentialResolver(
            _registry(),
            process_env={
                "OPENAI_API_KEY__ZETA": "zeta-secret",
                "OPENAI_API_KEY__ALPHA": "alpha-secret",
            },
            token_store=TokenStore(tmp_path),
        )

        # Act / Assert
        assert resolver.get_credentials("openai", "openai:api-key") == "alpha-secret"

    def test_oauth_account_pinned_lookup_returns_stored_token(self, tmp_path: Path) -> None:
        """An explicit OAuth account returns that account's access token."""
        # Arrange
        token_store = TokenStore(tmp_path)
        token_store.save(
            "openai",
            "subscription",
            OAuthToken(access_token="work-token"),
            account_id="work",
        )
        resolver = ProviderCredentialResolver(_registry(), process_env={}, token_store=token_store)

        # Act / Assert
        assert resolver.get_credentials("openai", "openai:subscription:work") == "work-token"

    def test_oauth_missing_account_raises_config_error_naming_account(self, tmp_path: Path) -> None:
        """A missing OAuth account token raises ConfigError naming the account."""
        # Arrange
        resolver = ProviderCredentialResolver(
            _registry(), process_env={}, token_store=TokenStore(tmp_path)
        )

        # Act / Assert
        with pytest.raises(ConfigError, match="account 'work'"):
            resolver.get_credentials("openai", "openai:subscription:work")

    def test_oauth_stub_with_credential_key_resolves_derived_env_accounts(
        self, tmp_path: Path
    ) -> None:
        """OAuth stubs with a credential_key resolve accounts through env keys."""
        # Arrange
        resolver = ProviderCredentialResolver(
            _registry(),
            process_env={"OPENAI_STUB_KEY__WORK": "stub-work-secret"},
            token_store=TokenStore(tmp_path),
        )

        # Act / Assert
        assert resolver.get_credentials("openai", "openai:oauth-stub:work") == "stub-work-secret"
        assert resolver.get_credentials("openai", "openai:oauth-stub") == "stub-work-secret"


class TestResolveAccountId:
    def test_explicit_usable_account_is_returned(self, tmp_path: Path) -> None:
        """An explicit usable account id resolves to itself."""
        # Arrange
        resolver = ProviderCredentialResolver(
            _registry(),
            process_env={"OPENAI_API_KEY__WORK": "work-secret"},
            token_store=TokenStore(tmp_path),
        )

        # Act / Assert
        assert resolver.resolve_account_id("openai", "api-key", "work") == "work"

    def test_explicit_account_without_credential_raises_config_error(self, tmp_path: Path) -> None:
        """An explicit account with no credential raises ConfigError."""
        # Arrange
        resolver = ProviderCredentialResolver(
            _registry(),
            process_env={"OPENAI_API_KEY": "default-secret"},
            token_store=TokenStore(tmp_path),
        )

        # Act / Assert
        with pytest.raises(ConfigError, match="No credential for account 'work'"):
            resolver.resolve_account_id("openai", "api-key", "work")

    def test_invalid_explicit_account_id_raises_config_error(self, tmp_path: Path) -> None:
        """Malformed explicit account ids are rejected up front."""
        # Arrange
        resolver = ProviderCredentialResolver(
            _registry(), process_env={}, token_store=TokenStore(tmp_path)
        )

        # Act / Assert
        with pytest.raises(ConfigError, match="Invalid account id"):
            resolver.resolve_account_id("openai", "api-key", "WORK")

    def test_no_account_resolves_to_first_usable(self, tmp_path: Path) -> None:
        """Without an explicit account, the first usable account in order wins."""
        # Arrange
        token_store = TokenStore(tmp_path)
        token_store.save(
            "openai",
            "subscription",
            OAuthToken(access_token="work-token"),
            account_id="work",
        )
        resolver = ProviderCredentialResolver(_registry(), process_env={}, token_store=token_store)

        # Act / Assert
        assert resolver.resolve_account_id("openai", "subscription") == "work"

    def test_no_usable_account_raises_config_error(self, tmp_path: Path) -> None:
        """When no account is usable, resolution raises ConfigError."""
        # Arrange
        resolver = ProviderCredentialResolver(
            _registry(), process_env={}, token_store=TokenStore(tmp_path)
        )

        # Act / Assert
        with pytest.raises(ConfigError, match="No usable account"):
            resolver.resolve_account_id("openai", "subscription")
