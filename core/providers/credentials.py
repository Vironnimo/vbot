"""Provider credential resolution helpers.

Credentials are resolved per connection *account* — a named credential
slot (see :mod:`core.providers.accounts`). API-key connections (and
OAuth stubs that carry a ``credential_key``) map accounts to environment
keys derived from the connection's base ``credential_key``; token-store
OAuth connections map accounts to per-account token files.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from core.providers.accounts import (
    DEFAULT_ACCOUNT_ID,
    ProviderAccount,
    account_id_from_credential_key,
    derive_credential_key,
    split_connection_id,
    validate_account_id,
)
from core.providers.providers import ConnectionConfig, ProviderRegistry
from core.providers.token_store import TokenStore
from core.utils.errors import ConfigError


class ProviderCredentialResolver:
    """Resolve provider credentials from process env with data-dir fallback."""

    def __init__(
        self,
        provider_registry: ProviderRegistry,
        *,
        fallback_credentials: Mapping[str, str] | None = None,
        process_env: Mapping[str, str] | None = None,
        token_store: TokenStore | None = None,
    ) -> None:
        self._provider_registry = provider_registry
        self._fallback_credentials = dict(fallback_credentials or {})
        self._process_env = os.environ if process_env is None else process_env
        self._token_store = token_store

    def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool:
        """Return whether a provider, connection, or account has credentials.

        *connection_id* uses the compositional ``provider:connection[:account]``
        grammar. Without an account part, any usable account on the
        connection counts; without a connection id, any usable account on
        any connection counts.
        """

        provider_config = self._provider_registry.get(provider_id)
        if connection_id is not None:
            connection, account_id = self._get_connection(provider_id, connection_id)
            return self._has_connection_credentials(provider_id, connection, account_id)

        for connection in provider_config.connections:
            if self._has_connection_credentials(provider_id, connection, None):
                return True
        return False

    def get_credentials(self, provider_id: str, connection_id: str | None = None) -> str:
        """Return the configured credential value for a provider, connection, or account.

        With an account part in *connection_id*, exactly that account's
        credential is returned. Without one, the first usable account in
        deterministic order (``default`` first, then sorted) is used.
        """

        provider_config = self._provider_registry.get(provider_id)
        if connection_id is not None:
            connection, account_id = self._get_connection(provider_id, connection_id)
            return self._get_connection_credentials(provider_id, connection, account_id)

        for connection in provider_config.connections:
            if self._has_connection_credentials(provider_id, connection, None):
                return self._get_connection_credentials(provider_id, connection, None)

        credential_names = ", ".join(
            connection.auth.credential_key for connection in provider_config.connections
        )
        raise ConfigError(
            f"Provider credentials not found for provider '{provider_id}': "
            f"credentials '{credential_names}' are not set"
        )

    def list_accounts(self, provider_id: str, local_connection_id: str) -> list[ProviderAccount]:
        """Return the connection's accounts, default first then sorted."""

        connection = self._get_local_connection(provider_id, local_connection_id)
        return self._connection_accounts(provider_id, connection)

    def resolve_account_id(
        self,
        provider_id: str,
        local_connection_id: str,
        account_id: str | None = None,
    ) -> str:
        """Resolve an explicit or implicit account id to a usable account.

        An explicit *account_id* must name an account with a credential;
        ``None`` resolves to the first usable account in deterministic
        order. Raises ``ConfigError`` when no usable account matches.
        """

        accounts = self.list_accounts(provider_id, local_connection_id)
        if account_id is not None:
            validate_account_id(account_id)
            if any(account.id == account_id and account.usable for account in accounts):
                return account_id
            raise ConfigError(
                f"No credential for account '{account_id}' on provider "
                f"'{provider_id}' connection '{local_connection_id}'"
            )

        for account in accounts:
            if account.usable:
                return account.id
        raise ConfigError(
            f"No usable account for provider '{provider_id}' connection '{local_connection_id}'"
        )

    def _get_connection_credentials(
        self,
        provider_id: str,
        connection: ConnectionConfig,
        account_id: str | None,
    ) -> str:
        if account_id is not None:
            return self._get_account_credentials(provider_id, connection, account_id)

        first_usable = self._first_usable_account_id(provider_id, connection)
        if first_usable is not None:
            return self._get_account_credentials(provider_id, connection, first_usable)

        if self._uses_token_store(connection):
            raise ConfigError(
                f"Provider credentials not found for provider '{provider_id}': "
                f"OAuth token for connection '{connection.id}' is not set"
            )
        raise ConfigError(
            f"Provider credentials not found for provider '{provider_id}': "
            f"credential '{connection.auth.credential_key}' is not set"
        )

    def _get_account_credentials(
        self,
        provider_id: str,
        connection: ConnectionConfig,
        account_id: str,
    ) -> str:
        if self._uses_token_store(connection):
            token = None
            if self._token_store is not None:
                token = self._token_store.load(provider_id, connection.id, account_id=account_id)
            if token is not None:
                return token.access_token
            raise ConfigError(
                f"Provider credentials not found for provider '{provider_id}': OAuth token "
                f"for connection '{connection.id}' account '{account_id}' is not set"
            )

        derived_key = derive_credential_key(connection.auth.credential_key, account_id)
        credential_value = self._resolve_credential_value(derived_key)
        if credential_value:
            return credential_value

        raise ConfigError(
            f"Provider credentials not found for provider '{provider_id}': credential "
            f"'{derived_key}' for account '{account_id}' is not set"
        )

    def _get_connection(
        self,
        provider_id: str,
        connection_id: str,
    ) -> tuple[ConnectionConfig, str | None]:
        local_id, account_id = split_connection_id(provider_id, connection_id)
        provider_config = self._provider_registry.get(provider_id)
        try:
            return provider_config.get_connection(local_id), account_id
        except KeyError as error:
            raise ConfigError(
                f"Unknown connection id '{connection_id}' for provider '{provider_id}'"
            ) from error

    def _get_local_connection(
        self,
        provider_id: str,
        local_connection_id: str,
    ) -> ConnectionConfig:
        provider_config = self._provider_registry.get(provider_id)
        try:
            return provider_config.get_connection(local_connection_id)
        except KeyError as error:
            raise ConfigError(
                f"Unknown connection id '{local_connection_id}' for provider '{provider_id}'"
            ) from error

    def _has_connection_credentials(
        self,
        provider_id: str,
        connection: ConnectionConfig,
        account_id: str | None,
    ) -> bool:
        accounts = self._connection_accounts(provider_id, connection)
        if account_id is None:
            return any(account.usable for account in accounts)
        return any(account.id == account_id and account.usable for account in accounts)

    def _first_usable_account_id(
        self,
        provider_id: str,
        connection: ConnectionConfig,
    ) -> str | None:
        for account in self._connection_accounts(provider_id, connection):
            if account.usable:
                return account.id
        return None

    def _connection_accounts(
        self,
        provider_id: str,
        connection: ConnectionConfig,
    ) -> list[ProviderAccount]:
        if self._uses_token_store(connection):
            return self._token_store_accounts(provider_id, connection)
        return self._environment_accounts(connection)

    def _token_store_accounts(
        self,
        provider_id: str,
        connection: ConnectionConfig,
    ) -> list[ProviderAccount]:
        if self._token_store is None:
            return []
        return [
            ProviderAccount(
                id=account_id,
                usable=self._token_store.has_valid_token(
                    provider_id, connection.id, account_id=account_id
                ),
                source="oauth",
            )
            for account_id in self._token_store.list_account_ids(provider_id, connection.id)
        ]

    def _environment_accounts(self, connection: ConnectionConfig) -> list[ProviderAccount]:
        base_key = connection.auth.credential_key
        accounts: dict[str, ProviderAccount] = {}
        sources: list[tuple[str, Mapping[str, str]]] = [
            ("process_env", self._process_env),
            ("data_dir", self._fallback_credentials),
        ]
        for source, mapping in sources:
            for env_key, value in mapping.items():
                account_id = account_id_from_credential_key(base_key, env_key)
                if account_id is None or account_id in accounts:
                    continue
                accounts[account_id] = ProviderAccount(
                    id=account_id,
                    usable=bool(value),
                    source=source,
                    credential_key=derive_credential_key(base_key, account_id),
                )
        return sorted(
            accounts.values(),
            key=lambda account: (account.id != DEFAULT_ACCOUNT_ID, account.id),
        )

    def _uses_token_store(self, connection: ConnectionConfig) -> bool:
        return connection.type == "oauth" and (
            connection.oauth is not None or not connection.auth.credential_key
        )

    def _resolve_credential_value(self, credential_key: str) -> str:
        if credential_key in self._process_env:
            return self._process_env[credential_key]
        return self._fallback_credentials.get(credential_key, "")
