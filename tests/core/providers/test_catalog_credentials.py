"""Account selection for the Provider-owned catalog discovery credential."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import pytest

import core.providers._catalog_refresh as catalog_refresh
from core.models.models import ModelRegistry
from core.providers.accounts import Account
from core.providers.providers import AuthConfig, ConnectionConfig, OAuthConfig, ProviderRegistry
from core.providers.runtime import ProviderRuntime
from core.providers.token_store import TokenStore
from core.storage import StorageManager


class _RecordingTokenGetter:
    instances: list[_RecordingTokenGetter] = []

    def __init__(self, token_store: Any, account: Account, oauth: Any) -> None:
        self.account = account
        _RecordingTokenGetter.instances.append(self)

    async def __call__(self) -> str:
        return f"token-for-{self.account.account_id}"


class _AccountResolvingCredentials:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str | None]] = []

    def resolve_account_id(
        self, provider_id: str, connection_id: str, account_id: str | None = None
    ) -> str:
        self.requests.append((provider_id, connection_id, account_id))
        return account_id or "first_usable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connection_id", "expected_request", "expected_account"),
    [
        ("openai:subscription:work", "work", "work"),
        ("openai:subscription", None, "first_usable"),
    ],
)
async def test_oauth_credential_honors_explicit_account_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    connection_id: str,
    expected_request: str | None,
    expected_account: str,
) -> None:
    _RecordingTokenGetter.instances = []
    monkeypatch.setattr(catalog_refresh, "OAuthTokenGetter", _RecordingTokenGetter)
    credentials = _AccountResolvingCredentials()
    storage = StorageManager(tmp_path)
    runtime = ProviderRuntime(
        providers=ProviderRegistry({}),
        models=ModelRegistry({}),
        credentials=cast(Any, credentials),
        token_store=TokenStore(storage.secrets),
        storage=storage,
        resources_path=storage.resources_dir,
        logger=logging.getLogger("vbot.providers.runtime"),
    )
    connection = ConnectionConfig(
        id="subscription",
        type="oauth",
        label="Subscription",
        auth=AuthConfig(header="Authorization", prefix="Bearer "),
        oauth=OAuthConfig(
            flow="device", client_id="client", device_auth_url="", token_url="", scopes=[]
        ),
    )

    async with runtime.discovery_credential("openai", connection_id, connection) as (
        _connection,
        credential,
    ):
        assert not isinstance(credential, str)
        assert await credential() == f"token-for-{expected_account}"

    assert credentials.requests == [("openai", "subscription", expected_request)]
    assert [getter.account for getter in _RecordingTokenGetter.instances] == [
        Account("openai", "subscription", expected_account)
    ]
