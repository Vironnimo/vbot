"""Account selection for the Provider-owned catalog discovery credential."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import respx

import core.providers._catalog_refresh as catalog_refresh
from core.models.models import ModelRegistry
from core.providers.account_catalogs import AccountCatalogs, account_model_routes
from core.providers.accounts import Account
from core.providers.credentials import ProviderCredentialResolver
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


@pytest.mark.asyncio
@respx.mock
async def test_private_catalog_selects_per_account_and_survives_an_offline_restart(
    tmp_path: Path,
) -> None:
    resources = Path(__file__).resolve().parents[3] / "resources"
    registry = ProviderRegistry.load(resources)
    keys = {"OPENCODE_API_KEY": "personal", "OPENCODE_API_KEY__WORK": "business"}
    store = tmp_path / "private-catalogs"

    def resolver() -> ProviderCredentialResolver:
        return ProviderCredentialResolver(
            registry, process_env=keys, account_catalogs=AccountCatalogs(store)
        )

    credentials = resolver()
    storage = StorageManager(tmp_path)
    runtime = ProviderRuntime(
        providers=registry,
        models=ModelRegistry({}),
        credentials=credentials,
        token_store=TokenStore(storage.secrets),
        storage=storage,
        resources_path=resources,
        logger=logging.getLogger("vbot.providers.runtime"),
    )
    provider = "opencode-zen"
    route = "opencode-zen:api-key"
    memberships = {"Bearer personal": ["one"], "Bearer business": ["two"]}

    def models(request: httpx.Request) -> httpx.Response:
        ids = memberships[request.headers["Authorization"]]
        return httpx.Response(200, json={"data": [{"id": model} for model in ids]})

    endpoint = respx.get("https://opencode.ai/inference/v1/models").mock(side_effect=models)
    assert await catalog_refresh.refresh_account_catalogs(runtime, provider_id=provider) == []
    assert endpoint.call_count == 2
    choices = account_model_routes(credentials, provider, route)
    assert choices("one") == route + ":default"
    assert choices("two") == route + ":work"
    assert choices("missing") is None
    assert account_model_routes(credentials, provider, route + ":default")("two") is None

    memberships["Bearer personal"] = ["three"]
    assert (
        await catalog_refresh.refresh_account_catalogs(runtime, provider_id=provider, force=True)
        == []
    )
    restarted = resolver()
    assert account_model_routes(restarted, provider, route)("one") is None
    assert account_model_routes(restarted, provider, route)("three") == route + ":default"
    memberships["Bearer personal"] = [" "]
    errors = await catalog_refresh.refresh_account_catalogs(
        runtime, provider_id=provider, force=True
    )
    assert [error["connection_id"] for error in errors] == [route + ":default"]
    assert account_model_routes(credentials, provider, route)("three") == route + ":default"
    memberships["Bearer personal"] = []
    assert (
        await catalog_refresh.refresh_account_catalogs(runtime, provider_id=provider, force=True)
        == []
    )
    assert account_model_routes(credentials, provider, route)("three") is None
    # A replaced slot does not inherit the previous workspace. A confirmed sibling still wins.
    keys["OPENCODE_API_KEY"] = "new"
    assert account_model_routes(restarted, provider, route)("two") == route + ":work"
    assert account_model_routes(restarted, provider, route)("unknown") == route + ":default"
    del keys["OPENCODE_API_KEY"]
    previously_chosen = account_model_routes(restarted, provider, route)("two")
    keys["OPENCODE_API_KEY"] = "personal"
    assert previously_chosen == route + ":work"
    assert restarted.get_credentials(provider, previously_chosen) == "business"
    assert all("business" not in path.read_text() for path in store.rglob("*.json"))
    await runtime.aclose_transports()
    runtime.close_wire_observations()
