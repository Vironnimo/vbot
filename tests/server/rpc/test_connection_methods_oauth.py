"""OAuth Device Flow RPCs: ``provider.connect``, ``disconnect`` and ``connection_status``.

The ``model.refresh_db`` OAuth credential tests live in ``test_model_methods_refresh.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.providers.auth_flow import DeviceFlowSession
from core.providers.providers import OAuthConfig
from core.providers.token_store import OAuthToken
from server.events import PROVIDER_AUTH_COMPLETED_EVENT
from tests.server.rpc.oauth_provider_test_support import (
    api_key_connection,
    copilot_provider,
    oauth_config,
    oauth_connection,
    oauth_provider_state,
)
from tests.server.rpc_test_support import JsonObject, resource_changes, rpc_error, rpc_result

_CONNECTION = {"provider_id": "github-copilot", "connection_id": "github-copilot:oauth"}


class StubDeviceFlowEngine:
    def __init__(self) -> None:
        self.started: list[tuple[str, str, OAuthConfig, str]] = []
        self.completions: list[Any] = []
        self.cancelled: list[tuple[str, str, str]] = []
        self.active: set[tuple[str, str, str]] = set()

    async def connect(
        self,
        provider_id: str,
        local_connection_id: str,
        oauth_config: OAuthConfig,
        on_complete: Any,
        *,
        account_id: str = "default",
    ) -> DeviceFlowSession:
        self.started.append((provider_id, local_connection_id, oauth_config, account_id))
        self.completions.append(on_complete)
        self.active.add((provider_id, local_connection_id, account_id))
        return DeviceFlowSession(
            device_code="device-code",
            user_code="ABCD-1234",
            verification_uri="https://github.com/login/device",
            expires_in=900,
            interval=5,
        )

    def is_flow_active(self, provider_id: str, connection_id: str, account_id: str) -> bool:
        return (provider_id, connection_id, account_id) in self.active

    def cancel_flow(
        self,
        provider_id: str,
        local_connection_id: str,
        account_id: str = "default",
    ) -> None:
        self.cancelled.append((provider_id, local_connection_id, account_id))
        self.active.discard((provider_id, local_connection_id, account_id))


def _oauth_state(tmp_path: Path) -> Any:
    state = oauth_provider_state(tmp_path, copilot_provider(oauth_connection()))
    state.device_flow_engine = StubDeviceFlowEngine()
    return state


def _account_params(account: str | None) -> JsonObject:
    return {**_CONNECTION, **({"account": account} if account else {})}


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [None, "work"])
async def test_provider_connect_starts_the_account_device_flow(
    tmp_path: Path, account: str | None
) -> None:
    state = _oauth_state(tmp_path)
    engine = state.device_flow_engine
    account_id = account or "default"

    result = await rpc_result(state, "provider.connect", **_account_params(account))

    assert result == {
        "user_code": "ABCD-1234",
        "verification_uri": "https://github.com/login/device",
        "expires_in": 900,
        "account": account_id,
    }
    assert engine.started == [("github-copilot", "oauth", oauth_config(), account_id)]
    assert engine.is_flow_active("github-copilot", "oauth", account_id)
    # Starting the flow does not establish the Connection yet: no reload signal.
    assert resource_changes(state) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("account", "success"), [(None, True), ("work", False)])
async def test_provider_connect_completion_publishes_the_auth_outcome(
    tmp_path: Path, account: str | None, success: bool
) -> None:
    state = _oauth_state(tmp_path)
    await rpc_result(state, "provider.connect", **_account_params(account))
    [on_complete] = state.device_flow_engine.completions

    await on_complete(success=success)

    auth_events = [
        event["payload"]
        for event in state.event_bus.events
        if event["type"] == PROVIDER_AUTH_COMPLETED_EVENT
    ]
    assert auth_events == [
        {
            "provider_id": "github-copilot",
            "connection_id": "github-copilot:oauth",
            "account": account or "default",
            "success": success,
        }
    ]
    # Only a successful login changes which Models are selectable.
    assert resource_changes(state) == ([{"kind": "providers"}] if success else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(("account", "kept"), [(None, "work"), ("work", "default")])
async def test_provider_disconnect_deletes_only_the_account_token_and_cancels_its_flow(
    tmp_path: Path, account: str | None, kept: str
) -> None:
    state = _oauth_state(tmp_path)
    token_store = state.runtime.token_store
    for account_id in ("default", "work"):
        token_store.save(
            "github-copilot",
            "oauth",
            OAuthToken(access_token=f"{account_id}-token"),
            account_id=account_id,
        )
    account_id = account or "default"

    result = await rpc_result(state, "provider.disconnect", **_account_params(account))

    assert result == {
        "provider_id": "github-copilot",
        "connection_id": "github-copilot:oauth",
        "account": account_id,
        "status": "disconnected",
    }
    assert token_store.load("github-copilot", "oauth", account_id=account_id) is None
    assert token_store.load("github-copilot", "oauth", account_id=kept) is not None
    assert state.device_flow_engine.cancelled == [("github-copilot", "oauth", account_id)]
    assert resource_changes(state) == [{"kind": "providers"}]


@pytest.mark.asyncio
async def test_provider_disconnect_removes_a_token_file_that_fails_to_load(
    tmp_path: Path,
) -> None:
    state = oauth_provider_state(tmp_path, copilot_provider(oauth_connection()))
    token_path = tmp_path / "oauth" / "github-copilot-oauth.json"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("not json", encoding="utf-8")

    result = await rpc_result(state, "provider.disconnect", **_CONNECTION)

    assert result["status"] == "disconnected"
    assert not token_path.exists()


@pytest.mark.asyncio
async def test_provider_connection_status_reports_per_account_state(tmp_path: Path) -> None:
    state = _oauth_state(tmp_path)
    state.runtime.token_store.save(
        "github-copilot",
        "oauth",
        OAuthToken(access_token="work-token"),
        account_id="work",
    )
    state.device_flow_engine.active.add(("github-copilot", "oauth", "work"))

    work = await rpc_result(state, "provider.connection_status", **_CONNECTION, account="work")
    default = await rpc_result(state, "provider.connection_status", **_CONNECTION)

    assert work == {
        "provider_id": "github-copilot",
        "connection_id": "github-copilot:oauth",
        "account": "work",
        "connected": True,
        "flow_active": True,
    }
    assert default == {
        "provider_id": "github-copilot",
        "connection_id": "github-copilot:oauth",
        "account": "default",
        "connected": False,
        "flow_active": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "code", "named"),
    [
        ({**_CONNECTION, "account": "Not-Valid"}, "invalid_request", "Not-Valid"),
        (
            {"provider_id": "github-copilot", "connection_id": "github-copilot:api-key"},
            "oauth_not_supported",
            "is not an OAuth connection",
        ),
    ],
)
async def test_refused_provider_connect_starts_no_flow(
    tmp_path: Path, params: JsonObject, code: str, named: str
) -> None:
    state = oauth_provider_state(
        tmp_path, copilot_provider(oauth_connection(), api_key_connection())
    )
    state.device_flow_engine = StubDeviceFlowEngine()

    error = await rpc_error(state, "provider.connect", **params)

    assert error["code"] == code
    assert named in error["message"]
    assert state.device_flow_engine.started == []
