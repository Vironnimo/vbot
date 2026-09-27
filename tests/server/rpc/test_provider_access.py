"""Account resolution for the OAuth credential that RPCs open on a Connection.

The settings and ``connection.list`` projections are covered through their RPCs
in ``test_connection_methods.py``. This account-suffix path is reachable only via
``debug.model_probe``, so it is exercised on the helper directly.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import server.rpc.provider_access as provider_access
from server.rpc.provider_access import _runtime_provider_credential


class _RecordingTokenGetter:
    instances: list[_RecordingTokenGetter] = []

    def __init__(
        self, token_store: Any, provider_id: str, connection_id: str, oauth: Any, *, account_id: str
    ) -> None:
        self.account_id = account_id
        _RecordingTokenGetter.instances.append(self)

    async def __aenter__(self) -> _RecordingTokenGetter:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def __call__(self) -> str:
        return f"token-for-{self.account_id}"


class _AccountResolvingCredentials:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str | None]] = []

    def resolve_account_id(
        self, provider_id: str, connection_id: str, account_id: str | None = None
    ) -> str:
        self.requests.append((provider_id, connection_id, account_id))
        return account_id or "first-usable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connection_id", "expected_request", "expected_account"),
    [
        ("openai:subscription:work", "work", "work"),
        ("openai:subscription", None, "first-usable"),
    ],
)
async def test_oauth_credential_honors_explicit_account_suffix(
    monkeypatch: pytest.MonkeyPatch,
    connection_id: str,
    expected_request: str | None,
    expected_account: str,
) -> None:
    _RecordingTokenGetter.instances = []
    monkeypatch.setattr(provider_access, "OAuthTokenGetter", _RecordingTokenGetter)
    credentials = _AccountResolvingCredentials()
    runtime = SimpleNamespace(provider_credentials=credentials, token_store=object())
    connection = SimpleNamespace(id="subscription", type="oauth", oauth=object())

    async with _runtime_provider_credential(
        runtime, "openai", connection_id, connection
    ) as credential:
        assert not isinstance(credential, str)
        assert await credential() == f"token-for-{expected_account}"

    assert credentials.requests == [("openai", "subscription", expected_request)]
    assert [getter.account_id for getter in _RecordingTokenGetter.instances] == [expected_account]
