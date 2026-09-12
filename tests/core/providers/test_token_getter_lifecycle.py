"""Token getter: lifecycle behavior."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.providers.errors import ProviderAuthError
from core.providers.providers import OAuthConfig
from core.providers.token_getter import (
    OAuthTokenGetter,
)
from core.providers.token_store import OAuthToken, TokenStore
from tests.core.providers.token_getter_helpers import (
    CONNECTION_ID,
    PROVIDER_ID,
    TOKEN_EXCHANGE_URL,
)
from tests.core.providers.token_getter_helpers import (
    oauth_config as oauth_config,
)


class StubAsyncClient:
    def __init__(self, response: httpx.Response | None = None, **_kwargs: object) -> None:
        self.closed = False
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._response = response or httpx.Response(
            200,
            json={
                "token": "fresh-copilot-token",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            },
        )

    async def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
        self.requests.append((url, headers))
        return self._response

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_oauth_token_getter_preserves_injected_client_lifecycle(
    tmp_path: Path,
    oauth_config: OAuthConfig,
) -> None:
    """OAuthTokenGetter does not close caller-injected clients."""

    token_store = TokenStore(tmp_path)
    token_store.save(
        PROVIDER_ID,
        CONNECTION_ID,
        OAuthToken(
            access_token="expired-copilot-token",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
            extra={"github_oauth_token": "github-oauth-secret"},
        ),
    )
    client = StubAsyncClient()

    async with OAuthTokenGetter(
        token_store,
        PROVIDER_ID,
        CONNECTION_ID,
        oauth_config,
        client=client,  # type: ignore[arg-type]
    ) as getter:
        token = await getter()

    assert token == "fresh-copilot-token"
    assert client.closed is False
    assert client.requests[0][0] == TOKEN_EXCHANGE_URL
    assert client.requests[0][1] == {
        "Accept": "application/json",
        "Authorization": "Bearer github-oauth-secret",
        "Copilot-Integration-Id": "vscode-chat",
        "Editor-Version": "vscode/1.128.0",
    }


@pytest.mark.asyncio
async def test_oauth_token_getter_aclose_closes_owned_created_client(
    tmp_path: Path,
    oauth_config: OAuthConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Internally-created clients are closed by the async context manager."""

    token_store = TokenStore(tmp_path)
    token_store.save(
        PROVIDER_ID,
        CONNECTION_ID,
        OAuthToken(
            access_token="expired-copilot-token",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
            extra={"github_oauth_token": "github-oauth-secret"},
        ),
    )
    clients: list[StubAsyncClient] = []

    def make_client(**_kwargs: object) -> StubAsyncClient:
        client = StubAsyncClient()
        clients.append(client)
        return client

    monkeypatch.setattr("core.providers.token_getter.httpx.AsyncClient", make_client)
    getter = OAuthTokenGetter(token_store, PROVIDER_ID, CONNECTION_ID, oauth_config)

    async with getter:
        token = await getter()

    assert token == "fresh-copilot-token"
    assert len(clients) == 1
    assert clients[0].closed is True


@respx.mock
@pytest.mark.asyncio
async def test_oauth_token_getter_logs_info_on_successful_refresh(
    tmp_path: Path,
    oauth_config: OAuthConfig,
    caplog: Any,
) -> None:
    """A successful token refresh logs at info with non-secret identifiers only."""

    token_store = TokenStore(tmp_path)
    token_store.save(
        PROVIDER_ID,
        CONNECTION_ID,
        OAuthToken(
            access_token="expired-copilot-token",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
            extra={"github_oauth_token": "github-oauth-secret"},
        ),
    )
    respx.get(TOKEN_EXCHANGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "fresh-copilot-token",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            },
        )
    )
    getter = OAuthTokenGetter(token_store, PROVIDER_ID, CONNECTION_ID, oauth_config)

    with caplog.at_level(logging.INFO, logger="vbot.providers.token_getter"):
        token = await getter()

    assert token == "fresh-copilot-token"
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert info_records
    log_text = caplog.text
    assert PROVIDER_ID in log_text
    assert CONNECTION_ID in log_text
    # No secrets leak into the logs.
    assert "fresh-copilot-token" not in log_text
    assert "github-oauth-secret" not in log_text


@respx.mock
@pytest.mark.asyncio
async def test_oauth_token_getter_logs_warning_on_refresh_request_failure(
    tmp_path: Path,
    oauth_config: OAuthConfig,
    caplog: Any,
) -> None:
    """A failed refresh request logs at warning (no traceback) before raising."""

    token_store = TokenStore(tmp_path)
    token_store.save(
        PROVIDER_ID,
        CONNECTION_ID,
        OAuthToken(
            access_token="expired-copilot-token",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
            extra={"github_oauth_token": "github-oauth-secret"},
        ),
    )
    respx.get(TOKEN_EXCHANGE_URL).mock(return_value=httpx.Response(401, text="unauthorized"))
    getter = OAuthTokenGetter(token_store, PROVIDER_ID, CONNECTION_ID, oauth_config)

    with (
        caplog.at_level(logging.WARNING, logger="vbot.providers.token_getter"),
        pytest.raises(ProviderAuthError),
    ):
        await getter()

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "expected a warning log for the failed refresh"
    failure_record = warning_records[0]
    assert failure_record.exc_info is None
    assert PROVIDER_ID in caplog.text


@pytest.mark.asyncio
async def test_oauth_token_getter_logs_warning_when_no_token(
    tmp_path: Path,
    oauth_config: OAuthConfig,
    caplog: Any,
) -> None:
    """A missing stored token logs at warning before requiring a reconnect."""

    getter = OAuthTokenGetter(TokenStore(tmp_path), PROVIDER_ID, CONNECTION_ID, oauth_config)

    with (
        caplog.at_level(logging.WARNING, logger="vbot.providers.token_getter"),
        pytest.raises(ProviderAuthError),
    ):
        await getter()

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("No usable OAuth token" in r.getMessage() for r in warning_records)
    assert all(r.exc_info is None for r in warning_records)
