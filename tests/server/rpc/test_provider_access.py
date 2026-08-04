"""Tests for shared Provider/Connection RPC projections."""

from __future__ import annotations

from types import SimpleNamespace

from core.providers.accounts import ProviderAccount
from server.rpc.provider_access import _provider_settings_connection


class StubCredentials:
    def has_credentials(self, provider_id: str, connection_id: str) -> bool:
        return provider_id == "ollama" and connection_id == "ollama:local"

    def is_connection_enabled(self, provider_id: str, connection_id: str) -> bool:
        return False

    def is_connection_added(self, provider_id: str, connection_id: str) -> bool:
        return False

    def is_usable(self, provider_id: str, connection_id: str) -> bool:
        return False

    def list_accounts(self, provider_id: str, connection_id: str) -> list[ProviderAccount]:
        return [ProviderAccount(id="default", usable=True, source="none")]


def test_settings_connection_keeps_configured_enabled_and_usable_distinct() -> None:
    runtime = SimpleNamespace(provider_credentials=StubCredentials())
    connection = SimpleNamespace(
        id="local",
        type="none",
        label="Local",
        auto_refresh=False,
    )

    response = _provider_settings_connection(runtime, "ollama", connection)

    assert response == {
        "id": "ollama:local",
        "type": "none",
        "label": "Local",
        "added": False,
        "configured": True,
        "enabled": False,
        "usable": False,
        "accounts": [
            {
                "id": "default",
                "usable": True,
                "source": "none",
                "credential_key": "",
            }
        ],
    }
