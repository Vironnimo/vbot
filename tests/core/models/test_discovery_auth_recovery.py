"""Models: rejected-token recovery for catalog refreshes.

Recovery is chosen by the Connection type: an OAuth Connection may refresh a
rejected token once per catalog request; other Connections and static
credentials never refresh.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from core.models.discovery import ModelDiscoveryError, refresh_models
from core.models.models import ModelRegistry
from core.providers.errors import ProviderAuthError, ProviderError
from core.providers.providers import OAuthConfig, ProviderConfig
from core.providers.token_getter import OAuthTokenGetter
from core.providers.token_store import OAuthToken, TokenStore

from .discovery_test_support import (
    OPENAI_SUBSCRIPTION_MODELS_URL,
    SIMPLE_MODELS_URL,
    jwt_with_openai_account,
    mock_openai_codex_package,
    openai_subscription_config,
    simple_compatible_config,
)


class _RecordingGetter:
    """A token getter that renews once after a 401 and records every refresh."""

    def __init__(self, token: str = "old-test-token") -> None:
        self.token = token
        self.refreshed: list[str] = []

    async def __call__(self) -> str:
        return self.token

    async def refresh_after_rejection(
        self, rejected: str, *, status_code: int, response_body: str
    ) -> str | None:
        if status_code != 401:
            return None
        self.refreshed.append(rejected)
        self.token = "new-test-token"
        return self.token


def _oauth_simple_config() -> ProviderConfig:
    config = simple_compatible_config()
    return replace(config, connections=[replace(config.connections[0], type="oauth")])


@respx.mock
@pytest.mark.asyncio
async def test_oauth_connection_retries_a_rejected_catalog_token_once(tmp_path: Path) -> None:
    config = _oauth_simple_config()
    getter = _RecordingGetter()
    route = respx.get(SIMPLE_MODELS_URL).mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"data": [{"id": "m"}]})]
    )

    result = await refresh_models(
        config, getter, tmp_path / "resources", credential_connection=config.connections[0]
    )

    assert result["model_count"] == 1
    assert getter.refreshed == ["old-test-token"]
    assert [call.request.headers["Authorization"] for call in route.calls] == [
        "Bearer old-test-token",
        "Bearer new-test-token",
    ]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_status", "retry_status", "expected_catalog_calls", "expected_refresh_calls"),
    [
        pytest.param(401, 200, 2, 1, id="renewed-token-succeeds"),
        pytest.param(401, 401, 2, 1, id="renewed-token-rejected-again"),
        pytest.param(403, 200, 1, 0, id="unrecognized-403-is-not-renewed"),
    ],
)
async def test_openai_subscription_catalog_renews_a_rejected_token_once(
    tmp_path: Path,
    initial_status: int,
    retry_status: int,
    expected_catalog_calls: int,
    expected_refresh_calls: int,
) -> None:
    config = openai_subscription_config()
    connection = config.get_connection("subscription")
    old_token = jwt_with_openai_account("old-test-account")
    new_token = jwt_with_openai_account("new-test-account")
    store = TokenStore(tmp_path / "data")
    store.save(
        "openai",
        "subscription",
        OAuthToken(
            access_token=old_token,
            refresh_token="test-refresh-secret",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    oauth = OAuthConfig(
        flow="device",
        device_flow="openai_codex",
        client_id="test-client",
        device_auth_url="https://auth.openai.com/device",
        token_url="https://auth.openai.com/oauth/token",
        scopes=[],
    )
    refresh_route = respx.post(oauth.token_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": new_token,
                "refresh_token": "rotated-test-refresh-secret",
                "expires_in": 3600,
            },
        )
    )
    mock_openai_codex_package()
    catalog_route = respx.get(OPENAI_SUBSCRIPTION_MODELS_URL).mock(
        side_effect=[
            httpx.Response(initial_status, json={"error": {"code": "token_expired"}}),
            httpx.Response(
                retry_status,
                json={"models": [{"slug": "test-model", "display_name": "Test Model"}]},
            ),
        ]
    )
    resources_dir = tmp_path / "resources"

    getter = OAuthTokenGetter(store, "openai", "subscription", oauth)
    if (initial_status, retry_status) == (401, 200):
        result = await refresh_models(
            config, getter, resources_dir, credential_connection=connection
        )
        assert result["model_count"] == 1
        assert ModelRegistry.load(resources_dir).get("openai", "test-model")
    else:
        with pytest.raises(ModelDiscoveryError) as caught:
            await refresh_models(config, getter, resources_dir, credential_connection=connection)
        assert isinstance(caught.value.__cause__, ProviderAuthError)
        assert not (resources_dir / "models" / "openai.json").exists()

    assert catalog_route.call_count == expected_catalog_calls
    assert refresh_route.call_count == expected_refresh_calls
    sent = [
        (call.request.headers["Authorization"], call.request.headers["chatgpt-account-id"])
        for call in catalog_route.calls
    ]
    assert (
        sent
        == [
            (f"Bearer {old_token}", "old-test-account"),
            (f"Bearer {new_token}", "new-test-account"),
        ][:expected_catalog_calls]
    )
    if expected_refresh_calls:
        saved = store.load("openai", "subscription")
        assert saved is not None
        assert (saved.access_token, saved.refresh_token) == (
            new_token,
            "rotated-test-refresh-secret",
        )


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        ProviderError("test-owned-refresh-outage", retryable=True),
        ProviderAuthError("test-owned-refresh-rejection"),
    ],
    ids=["retryable-outage", "rejection"],
)
async def test_failed_token_renewal_stops_the_refresh(
    tmp_path: Path, failure: ProviderError
) -> None:
    config = _oauth_simple_config()
    route = respx.get(SIMPLE_MODELS_URL).mock(return_value=httpx.Response(401))

    class FailingGetter(_RecordingGetter):
        async def refresh_after_rejection(
            self, rejected: str, *, status_code: int, response_body: str
        ) -> str | None:
            self.refreshed.append(rejected)
            raise failure

    getter = FailingGetter()
    with pytest.raises(ModelDiscoveryError) as caught:
        await refresh_models(
            config, getter, tmp_path / "resources", credential_connection=config.connections[0]
        )

    assert caught.value.__cause__ is failure
    assert route.call_count == 1
    assert getter.refreshed == ["old-test-token"]


@respx.mock
@pytest.mark.asyncio
async def test_token_getter_failure_is_not_a_rejected_request(tmp_path: Path) -> None:
    config = _oauth_simple_config()
    route = respx.get(SIMPLE_MODELS_URL).mock(return_value=httpx.Response(200))
    failure = ProviderAuthError("test-owned-getter-rejection")
    failure.status_code = 401

    class Getter(_RecordingGetter):
        async def __call__(self) -> str:
            raise failure

    getter = Getter()
    with pytest.raises(ModelDiscoveryError) as caught:
        await refresh_models(
            config, getter, tmp_path / "resources", credential_connection=config.connections[0]
        )

    assert caught.value.__cause__ is failure
    assert (route.call_count, getter.refreshed) == (0, [])


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connection_type", "static_credential"),
    [("api_key", False), ("oauth", True)],
    ids=["api-key-connection-with-a-refreshing-getter", "oauth-connection-with-a-static-token"],
)
async def test_other_credentials_never_renew_a_rejected_token(
    tmp_path: Path, connection_type: str, static_credential: bool
) -> None:
    config = simple_compatible_config()
    connection = replace(config.connections[0], type=connection_type)
    config = replace(config, connections=[connection])
    getter = _RecordingGetter()
    route = respx.get(SIMPLE_MODELS_URL).mock(return_value=httpx.Response(401))

    with pytest.raises(ModelDiscoveryError):
        await refresh_models(
            config,
            "static-test-token" if static_credential else getter,
            tmp_path / "resources",
            credential_connection=connection,
        )

    assert (route.call_count, getter.refreshed) == (1, [])
