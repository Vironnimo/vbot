"""Server RPC provider handlers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from server.rpc.methods import dispatch_rpc
from tests.server.rpc_test_support import (
    StubAdapter,
    _no_models_dev_fetch,
    make_state,
    openrouter_provider,
    openrouter_provider_with_secondary_connection,
)

__all__ = ["_no_models_dev_fetch"]


@pytest.mark.asyncio
async def test_connection_list_returns_connections_with_usability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "connection.list", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "connections": [
                {
                    "id": "anthropic:api-key",
                    "provider_id": "anthropic",
                    "type": "api_key",
                    "label": "API Key",
                    "enabled": True,
                    "usable": False,
                    "accounts": [
                        {
                            "id": "default",
                            "usable": False,
                            "source": "process_env",
                            "credential_key": "ANTHROPIC_API_KEY",
                        }
                    ],
                },
                {
                    "id": "ollama:api-key",
                    "provider_id": "ollama",
                    "type": "api_key",
                    "label": "API Key",
                    "enabled": True,
                    "usable": False,
                    "accounts": [],
                },
                {
                    "id": "openai:oauth",
                    "provider_id": "openai",
                    "type": "oauth",
                    "label": "OAuth",
                    "enabled": True,
                    "usable": False,
                    "accounts": [],
                },
                {
                    "id": "openai:api-key",
                    "provider_id": "openai",
                    "type": "api_key",
                    "label": "API Key",
                    "enabled": True,
                    "usable": True,
                    "accounts": [
                        {
                            "id": "default",
                            "usable": True,
                            "source": "process_env",
                            "credential_key": "OPENAI_API_KEY",
                        }
                    ],
                },
            ]
        },
    }


@pytest.mark.asyncio
async def test_connection_list_includes_named_accounts_from_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY__WORK", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())
    state.runtime.storage.set_data_dir_credential("OPENROUTER_API_KEY__WORK", "sk-or-work")

    response = await dispatch_rpc(state, {"method": "connection.list", "params": {}})

    assert response["ok"] is True
    openrouter = next(
        connection
        for connection in response["result"]["connections"]
        if connection["id"] == "openrouter:api-key"
    )
    assert openrouter["usable"] is True
    assert openrouter["accounts"] == [
        {
            "id": "work",
            "usable": True,
            "source": "data_dir",
            "credential_key": "OPENROUTER_API_KEY__WORK",
        }
    ]


@pytest.mark.asyncio
async def test_connection_list_rejects_params(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "connection.list", "params": {"x": 1}})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_provider_routing_options_delegates_to_openrouter_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RoutingAdapter(StubAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.model_ids: list[str | None] = []
            self.closed = False

        async def routing_provider_options(
            self, model_id: str | None = None
        ) -> list[dict[str, str]]:
            self.model_ids.append(model_id)
            return [{"slug": "anthropic", "name": "Anthropic"}]

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    adapter = RoutingAdapter()
    state = make_state(tmp_path, adapter)
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.routing_options",
            "params": {
                "provider_id": "openrouter",
                "model_id": "anthropic/claude-sonnet-4",
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {"providers": [{"slug": "anthropic", "name": "Anthropic"}]},
    }
    assert adapter.model_ids == ["anthropic/claude-sonnet-4"]
    assert adapter.closed is True


@pytest.mark.asyncio
async def test_provider_routing_options_rejects_non_openrouter_provider(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.routing_options",
            "params": {"provider_id": "openai"},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_provider_set_key_writes_api_key_credential_and_reloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.set_key",
            "params": {"provider_id": "openrouter", "value": "sk-or-test"},
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "provider_id": "openrouter",
            "connection_id": "openrouter:api-key",
            "account": "default",
            "credential_key": "OPENROUTER_API_KEY",
            "configured": True,
        },
    }
    assert state.runtime.storage.load_environment() == {"OPENROUTER_API_KEY": "sk-or-test"}
    assert state.runtime.provider_credentials.has_credentials("openrouter", "openrouter:api-key")


@pytest.mark.asyncio
async def test_provider_set_key_publishes_providers_resource_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.set_key",
            "params": {"provider_id": "openrouter", "value": "sk-or-test"},
        },
    )

    # Setting a key changes which models are selectable → signal a reload.
    assert response["ok"] is True
    assert [
        event["payload"] for event in state.event_bus.events if event["type"] == "resource_changed"
    ] == [{"kind": "providers"}]


@pytest.mark.asyncio
async def test_provider_set_key_with_account_writes_derived_credential_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY__WORK", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.set_key",
            "params": {"provider_id": "openrouter", "value": "sk-or-work", "account": "work"},
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "provider_id": "openrouter",
            "connection_id": "openrouter:api-key",
            "account": "work",
            "credential_key": "OPENROUTER_API_KEY__WORK",
            "configured": True,
        },
    }
    assert state.runtime.storage.load_environment() == {"OPENROUTER_API_KEY__WORK": "sk-or-work"}
    assert state.runtime.provider_credentials.has_credentials(
        "openrouter", "openrouter:api-key:work"
    )


@pytest.mark.asyncio
async def test_provider_set_key_rejects_invalid_account_id(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.set_key",
            "params": {"provider_id": "openrouter", "value": "secret", "account": "Not-Valid"},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "Invalid account id 'Not-Valid'" in response["error"]["message"]


@pytest.mark.asyncio
async def test_provider_set_key_rejects_conflicting_account_and_connection_id(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.set_key",
            "params": {
                "provider_id": "openrouter",
                "connection_id": "openrouter:api-key:work",
                "value": "secret",
                "account": "personal",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "conflicts with account 'work'" in response["error"]["message"]


@pytest.mark.asyncio
async def test_provider_set_key_rejects_oauth_connection(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider_with_secondary_connection())

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.set_key",
            "params": {
                "provider_id": "openrouter",
                "connection_id": "openrouter:oauth",
                "value": "secret",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "not an API key connection" in response["error"]["message"]


@pytest.mark.asyncio
async def test_provider_set_key_rejects_ambiguous_api_key_connection(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    provider = openrouter_provider()
    provider.connections.append(
        SimpleNamespace(
            id="secondary",
            type="api_key",
            label="Secondary API Key",
            auth=SimpleNamespace(credential_key="OPENROUTER_SECONDARY_API_KEY"),
        )
    )
    state.runtime.providers.add(provider)

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.set_key",
            "params": {"provider_id": "openrouter", "value": "secret"},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "multiple API key connections" in response["error"]["message"]


@pytest.mark.asyncio
async def test_provider_unset_key_removes_data_dir_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())
    state.runtime.storage.set_data_dir_credential("OPENROUTER_API_KEY", "sk-or-test")

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.unset_key",
            "params": {"provider_id": "openrouter"},
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "provider_id": "openrouter",
            "connection_id": "openrouter:api-key",
            "account": "default",
            "credential_key": "OPENROUTER_API_KEY",
            "removed": True,
            "configured": False,
        },
    }
    assert state.runtime.storage.load_environment() == {}


@pytest.mark.asyncio
async def test_provider_unset_key_publishes_providers_resource_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())
    state.runtime.storage.set_data_dir_credential("OPENROUTER_API_KEY", "sk-or-test")

    response = await dispatch_rpc(
        state,
        {"method": "provider.unset_key", "params": {"provider_id": "openrouter"}},
    )

    # Removing a key changes which models are selectable → signal a reload.
    assert response["ok"] is True
    assert [
        event["payload"] for event in state.event_bus.events if event["type"] == "resource_changed"
    ] == [{"kind": "providers"}]


@pytest.mark.asyncio
async def test_provider_unset_key_with_account_removes_derived_credential_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY__WORK", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())
    state.runtime.storage.set_data_dir_credential("OPENROUTER_API_KEY", "sk-or-default")
    state.runtime.storage.set_data_dir_credential("OPENROUTER_API_KEY__WORK", "sk-or-work")

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.unset_key",
            "params": {"provider_id": "openrouter", "account": "work"},
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "provider_id": "openrouter",
            "connection_id": "openrouter:api-key",
            "account": "work",
            "credential_key": "OPENROUTER_API_KEY__WORK",
            "removed": True,
            "configured": False,
        },
    }
    assert state.runtime.storage.load_environment() == {"OPENROUTER_API_KEY": "sk-or-default"}


@pytest.mark.asyncio
async def test_provider_unset_key_rejects_invalid_account_id(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.unset_key",
            "params": {"provider_id": "openrouter", "account": "UPPER"},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "Invalid account id 'UPPER'" in response["error"]["message"]


@pytest.mark.asyncio
async def test_provider_unset_key_reports_still_configured_from_process_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.unset_key",
            "params": {"provider_id": "openrouter"},
        },
    )

    assert response["ok"] is True
    assert response["result"]["removed"] is False
    assert response["result"]["configured"] is True


@pytest.mark.asyncio
async def test_provider_unset_key_rejects_oauth_connection(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider_with_secondary_connection())

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.unset_key",
            "params": {"provider_id": "openrouter", "connection_id": "openrouter:oauth"},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "not an API key connection" in response["error"]["message"]
