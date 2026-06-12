"""Account identifiers for provider connections.

An *account* is a named credential slot on a provider connection. The
compositional connection id grammar is ``provider:connection[:account]``;
an absent account resolves to the first usable account deterministically
(``default`` first, then the remaining accounts sorted alphabetically).

For API-key connections, each account maps to an environment credential
key derived from the connection's base ``credential_key``: the default
account uses the base key unchanged, named accounts use
``BASE__<ACCOUNT>`` (account id uppercased). The account id alphabet is
restricted to lowercase letters, digits, and underscores so the
derivation stays bijective and OAuth token filenames stay unambiguous.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.utils.errors import ConfigError

DEFAULT_ACCOUNT_ID = "default"
ACCOUNT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")
CREDENTIAL_KEY_ACCOUNT_SEPARATOR = "__"


@dataclass(frozen=True)
class ProviderAccount:
    """One named credential slot on a provider connection.

    Attributes:
        id: Account identifier (``"default"`` for the unnamed slot).
        usable: Whether the account currently has a usable credential.
        source: Where the credential lives — ``"process_env"``,
            ``"data_dir"``, or ``"oauth"``.
        credential_key: The derived environment key for env-backed
            accounts; empty for OAuth token-store accounts.
    """

    id: str
    usable: bool
    source: str
    credential_key: str = ""


def validate_account_id(value: str) -> str:
    """Return *value* if it is a well-formed account id, else raise ConfigError."""

    if not isinstance(value, str) or not ACCOUNT_ID_PATTERN.fullmatch(value):
        raise ConfigError(
            f"Invalid account id '{value}': account ids must be 1-32 characters of "
            "lowercase letters, digits, or underscores, starting with a letter or digit"
        )
    return value


def derive_credential_key(base_key: str, account_id: str) -> str:
    """Return the environment credential key for *account_id* on *base_key*."""

    if account_id == DEFAULT_ACCOUNT_ID:
        return base_key
    return f"{base_key}{CREDENTIAL_KEY_ACCOUNT_SEPARATOR}{account_id.upper()}"


def account_id_from_credential_key(base_key: str, env_key: str) -> str | None:
    """Return the account id encoded in *env_key*, or ``None`` when unrelated.

    The base key itself maps to the default account. ``BASE__<SUFFIX>``
    maps to the lowercased suffix when that suffix is a valid account id.
    A ``BASE__DEFAULT`` key maps to ``None`` — the default account's only
    canonical key is the base key, so the derived spelling is rejected to
    keep the mapping unambiguous.
    """

    if not base_key:
        return None
    if env_key == base_key:
        return DEFAULT_ACCOUNT_ID

    prefix = f"{base_key}{CREDENTIAL_KEY_ACCOUNT_SEPARATOR}"
    if not env_key.startswith(prefix):
        return None
    account_id = env_key.removeprefix(prefix).lower()
    if account_id == DEFAULT_ACCOUNT_ID or not ACCOUNT_ID_PATTERN.fullmatch(account_id):
        return None
    return account_id


def split_connection_id(provider_id: str, connection_id: str) -> tuple[str, str | None]:
    """Split a compositional ``provider:connection[:account]`` id.

    Validates that *connection_id* carries the ``provider_id`` prefix and,
    when present, that the account part is a well-formed account id.

    Returns:
        ``(local_connection_id, account_id)`` where ``account_id`` is
        ``None`` when no account part is present.

    Raises:
        ConfigError: If the id is malformed, carries the wrong provider
            prefix, or has an invalid account id.
    """

    expected_prefix = f"{provider_id}:"
    if not connection_id.startswith(expected_prefix):
        raise ConfigError(f"Unknown connection id '{connection_id}' for provider '{provider_id}'")

    remainder = connection_id.removeprefix(expected_prefix)
    local_connection_id, separator, account_id = remainder.partition(":")
    if not local_connection_id:
        raise ConfigError(f"Unknown connection id '{connection_id}' for provider '{provider_id}'")
    if not separator:
        return local_connection_id, None
    return local_connection_id, validate_account_id(account_id)


def compose_connection_id(
    provider_id: str,
    local_connection_id: str,
    account_id: str | None = None,
) -> str:
    """Return the compositional ``provider:connection[:account]`` id."""

    if account_id is None:
        return f"{provider_id}:{local_connection_id}"
    return f"{provider_id}:{local_connection_id}:{account_id}"


def sorted_account_ids(account_ids: list[str]) -> list[str]:
    """Return account ids in deterministic order: default first, then sorted."""

    return sorted(
        account_ids, key=lambda account_id: (account_id != DEFAULT_ACCOUNT_ID, account_id)
    )
