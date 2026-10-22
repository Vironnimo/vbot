"""The environment names vBot knows as credentials (``Runtime.environment_credential_names``).

Each owner keeps its own names: the Provider registry its Connections' keys,
Channels their token variables, Extensions their secret settings fields, the
web Tools their service keys, the Secret Store the data-directory credentials.
This module only joins them, with the names vBot's own consumers resolved
through ``Runtime.resolve_environment_credential`` (an MCP connection's
credential references, an Agent's granted credentials), so a confined command
environment can withhold every credential vBot uses (``tools/shell.md`` ->
Environment layers).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.fetch_config import WEB_FETCH_CREDENTIALS
from core.providers.accounts import (
    CREDENTIAL_KEY_ACCOUNT_SEPARATOR,
    account_id_from_credential_key,
)
from core.search_config import WEB_SEARCH_CREDENTIALS
from core.secrets import credential_name_key

if TYPE_CHECKING:
    from core.channels.config import ChannelConfig
    from core.extensions import ExtensionRecord
    from core.providers.providers import ProviderRegistry


@dataclass(frozen=True)
class CredentialNames:
    """Environment names vBot knows as credentials, compared as the host compares them.

    ``names`` are exact names; ``account_bases`` are Provider keys whose Account
    variants (``BASE__<ACCOUNT>``, ``core.providers.accounts``) are credentials too.
    Both hold ``credential_name_key`` spellings: upper case on Windows.
    """

    names: frozenset[str] = frozenset()
    account_bases: frozenset[str] = frozenset()

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        key = credential_name_key(name)
        if key in self.names:
            return True
        separator = key.find(CREDENTIAL_KEY_ACCOUNT_SEPARATOR)
        while separator > 0:
            base = key[:separator]
            if base in self.account_bases and account_id_from_credential_key(base, key):
                return True
            separator = key.find(CREDENTIAL_KEY_ACCOUNT_SEPARATOR, separator + 1)
        return False


def known_credential_names(
    *,
    stored: Iterable[str],
    resolved: Iterable[str],
    providers: ProviderRegistry | None,
    channels: Iterable[ChannelConfig],
    extensions: Iterable[ExtensionRecord],
) -> CredentialNames:
    """Join every owner's credential names.

    *stored* are the data-directory credentials, *resolved* the names vBot's
    consumers resolved as environment credentials in this process.
    """
    provider_keys = providers.credential_keys() if providers is not None else frozenset()
    names: set[str] = {*stored, *resolved, *provider_keys}
    names.update(WEB_SEARCH_CREDENTIALS.values())
    names.update(WEB_FETCH_CREDENTIALS.values())
    for channel in channels:
        names.update((channel.token_env_var, channel.app_token_env_var))
    for record in extensions:
        if record.status != "loaded":
            continue
        for field in record.declarations.settings_schema or ():
            if field.type == "secret" and field.env_key:
                names.add(field.env_key)
    return CredentialNames(
        frozenset(credential_name_key(name) for name in names if name),
        frozenset(credential_name_key(key) for key in provider_keys),
    )


__all__ = ["CredentialNames", "known_credential_names"]
