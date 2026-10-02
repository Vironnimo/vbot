"""Connection RPCs: listing, enablement, API keys and Custom Providers.

OAuth Device Flow RPCs live in ``test_connection_methods_oauth.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.providers.accounts import ConnectionRef
from core.providers.credentials import ProviderCredentialResolver
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import (
    AuthConfig,
    ConnectionConfig,
    ProviderConfig,
    ProviderRegistry,
)
from core.providers.reasoning import REASONING_INTENT_ON, ReasoningIntent
from core.runtime.runtime import Runtime
from core.storage.storage import StorageManager
from core.utils.config import Config
from server.events import ServerEventBus
from tests.core.providers.openai_compatible_test_support import sent_payload
from tests.server.rpc_test_support import (
    JsonObject,
    StubAdapter,
    _no_models_dev_fetch,
    make_state,
    openrouter_provider,
    openrouter_provider_with_secondary_connection,
    resource_changes,
    rpc_error,
    rpc_result,
)

__all__ = ["_no_models_dev_fetch"]

_REPO_RESOURCES = Path(__file__).resolve().parents[3] / "resources"
_CONNECTION_LOGGER = "vbot.server.rpc.connection_methods"


def _stored_credentials(state: Any) -> dict[str, str]:
    """Return the configured data-directory credentials, without empty placeholders.

    The first credential write seeds ``<data_dir>/.env`` from the bundled template,
    whose keys start empty.
    """

    return {key: value for key, value in state.runtime.storage.load_environment().items() if value}


def _openrouter_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY__WORK", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())
    return state


# ---------------------------------------------------------------------------
# connection.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_list_returns_connections_with_usability_and_accounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    state = _openrouter_state(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY__WORK=sk-or-work\n", encoding="utf-8")

    result = await rpc_result(state, "connection.list")

    assert result == {
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
            {
                "id": "openrouter:api-key",
                "provider_id": "openrouter",
                "type": "api_key",
                "label": "API Key",
                "enabled": True,
                "usable": True,
                "accounts": [
                    {
                        "id": "work",
                        "usable": True,
                        "source": "data_dir",
                        "credential_key": "OPENROUTER_API_KEY__WORK",
                    }
                ],
            },
        ]
    }


# ---------------------------------------------------------------------------
# connection.set_enabled
# ---------------------------------------------------------------------------


def _ollama_state(*, reachable: bool | None) -> SimpleNamespace:
    """Ollama with a keyless local auto-refresh Connection and a keyed cloud one."""

    provider = ProviderConfig(
        id="ollama",
        name="Ollama",
        adapter="ollama",
        base_url="http://localhost:11434",
        models_endpoint="/api/tags",
        connections=[
            ConnectionConfig(
                id="local",
                type="none",
                label="Local",
                auth=AuthConfig(header="", prefix="", credential_key=""),
                auto_refresh=True,
            ),
            ConnectionConfig(
                id="cloud",
                type="api_key",
                label="Ollama Cloud",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OLLAMA_API_KEY",
                ),
            ),
        ],
    )
    enabled_writes: list[tuple[str, bool]] = []
    refresh_calls: list[dict[str, Any]] = []

    async def maybe_refresh_local_catalogs(*, force: bool = False) -> None:
        refresh_calls.append({"force": force})

    runtime = SimpleNamespace(
        providers=SimpleNamespace(get=lambda _provider_id: provider),
        provider_credentials=SimpleNamespace(
            has_credentials=lambda _provider_id, connection_id: connection_id == "ollama:local",
            is_connection_enabled=lambda _provider_id, _connection_id: False,
            is_usable=lambda _provider_id, _connection_id: False,
        ),
        storage=SimpleNamespace(
            set_provider_connection_enabled=lambda key, enabled: enabled_writes.append(
                (key, enabled)
            )
        ),
        maybe_refresh_local_catalogs=maybe_refresh_local_catalogs,
        connection_reachability=lambda _connection_id: reachable,
    )
    return SimpleNamespace(
        runtime=runtime,
        event_bus=ServerEventBus(),
        enabled_writes=enabled_writes,
        refresh_calls=refresh_calls,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connection_id", "enabled", "reachable", "expected", "probed"),
    [
        # Enabling a local auto-refresh Connection forces a probe and reports it;
        # "enabled, but not running" is a valid outcome and the enable sticks.
        pytest.param(
            "ollama:local",
            True,
            True,
            {"configured": True, "reachable": True},
            True,
            id="enable-local-running",
        ),
        pytest.param(
            "ollama:local",
            True,
            False,
            {"configured": True, "reachable": False},
            True,
            id="enable-local-not-running",
        ),
        pytest.param(
            "ollama:local",
            False,
            None,
            {"configured": True, "reachable": None},
            False,
            id="disable-local",
        ),
        pytest.param("ollama:cloud", False, None, {"configured": False}, False, id="disable-cloud"),
    ],
)
async def test_connection_set_enabled_persists_and_reports_local_reachability(
    connection_id: str,
    enabled: bool,
    reachable: bool | None,
    expected: JsonObject,
    probed: bool,
) -> None:
    state = _ollama_state(reachable=reachable)

    result = await rpc_result(
        state,
        "connection.set_enabled",
        provider_id="ollama",
        connection_id=connection_id,
        enabled=enabled,
    )

    assert result == {
        "provider_id": "ollama",
        "connection_id": connection_id,
        "enabled": enabled,
        **expected,
    }
    assert state.enabled_writes == [(connection_id, enabled)]
    assert state.refresh_calls == ([{"force": True}] if probed else [])
    assert resource_changes(state) == [{"kind": "providers"}]


# ---------------------------------------------------------------------------
# provider.set_key / provider.unset_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account", "credential_key"),
    [(None, "OPENROUTER_API_KEY"), ("work", "OPENROUTER_API_KEY__WORK")],
)
async def test_provider_set_key_writes_the_account_credential_and_signals_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    account: str | None,
    credential_key: str,
) -> None:
    state = _openrouter_state(tmp_path, monkeypatch)
    account_params = {"account": account} if account else {}
    if account:
        state.runtime.storage.set_data_dir_credential("OPENROUTER_API_KEY__wOrK", "old-alias-key")

    result = await rpc_result(
        state, "provider.set_key", provider_id="openrouter", value="sk-or-test", **account_params
    )

    assert result == {
        "provider_id": "openrouter",
        "connection_id": "openrouter:api-key",
        "account": account or "default",
        "credential_key": credential_key,
        "configured": True,
    }
    assert _stored_credentials(state) == {credential_key: "sk-or-test"}
    account_suffix = f":{account}" if account else ""
    assert state.runtime.provider_credentials.has_credentials(
        "openrouter", f"openrouter:api-key{account_suffix}"
    )
    assert (
        state.runtime.provider_credentials.get_credentials(
            "openrouter", f"openrouter:api-key{account_suffix}"
        )
        == "sk-or-test"
    )
    # A credential change alters which Models are selectable.
    assert resource_changes(state) == [{"kind": "providers"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account", "credential_key", "remaining"),
    [
        (
            None,
            "OPENROUTER_API_KEY",
            {"OPENROUTER_API_KEY__WORK": "sk-or-work", "OPENROUTER_API_KEY__wOrK": "alias-work"},
        ),
        ("work", "OPENROUTER_API_KEY__WORK", {"OPENROUTER_API_KEY": "sk-or-default"}),
    ],
)
async def test_provider_unset_key_removes_only_the_account_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    account: str | None,
    credential_key: str,
    remaining: dict[str, str],
) -> None:
    state = _openrouter_state(tmp_path, monkeypatch)
    state.runtime.storage.set_data_dir_credential("OPENROUTER_API_KEY", "sk-or-default")
    state.runtime.storage.set_data_dir_credential("OPENROUTER_API_KEY__WORK", "sk-or-work")
    state.runtime.storage.set_data_dir_credential("OPENROUTER_API_KEY__wOrK", "alias-work")
    account_params = {"account": account} if account else {}

    result = await rpc_result(
        state, "provider.unset_key", provider_id="openrouter", **account_params
    )

    assert result == {
        "provider_id": "openrouter",
        "connection_id": "openrouter:api-key",
        "account": account or "default",
        "credential_key": credential_key,
        "removed": True,
        "configured": False,
    }
    assert _stored_credentials(state) == remaining
    assert resource_changes(state) == [{"kind": "providers"}]


@pytest.mark.asyncio
async def test_provider_unset_key_reports_still_configured_from_process_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _openrouter_state(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")

    result = await rpc_result(state, "provider.unset_key", provider_id="openrouter")

    assert result["removed"] is False
    assert result["configured"] is True


@pytest.mark.asyncio
async def test_opencode_key_mutations_update_both_provider_accounts(tmp_path: Path) -> None:
    """OpenCode Go and Zen share one API key, so either Provider's key RPC updates both."""

    providers = ProviderRegistry.load(_REPO_RESOURCES)
    storage = StorageManager(tmp_path)
    resolver = ProviderCredentialResolver(providers, process_env={})
    state = SimpleNamespace(
        runtime=SimpleNamespace(
            providers=providers,
            storage=storage,
            provider_credentials=resolver,
            reload_environment_credentials=lambda: resolver.reload_fallback_credentials(
                storage.load_environment()
            ),
        ),
        event_bus=ServerEventBus(),
    )
    targets = ("opencode-go", "opencode-zen")
    for provider, value in zip(targets, ("first-test", "second-test"), strict=True):
        result = await rpc_result(
            state,
            "provider.set_key",
            provider_id=provider,
            connection_id=f"{provider}:api-key",
            account="work",
            value=value,
        )
        assert value not in str(result)
        for target in targets:
            assert resolver.get_credentials(target, f"{target}:api-key:work") == value

    await rpc_result(
        state,
        "provider.unset_key",
        provider_id="opencode-go",
        connection_id="opencode-go:api-key",
        account="work",
    )

    for target in targets:
        assert not resolver.has_credentials(target, f"{target}:api-key:work")


@pytest.mark.asyncio
async def test_provider_key_changes_log_once_without_secret_or_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only an actual change is logged: a repeated save or removal stays silent."""

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    target = {"provider_id": "openai", "connection_id": "openai:api-key", "account": "private_slot"}

    def messages() -> list[str]:
        return [
            record.getMessage() for record in caplog.records if record.name == _CONNECTION_LOGGER
        ]

    with caplog.at_level(logging.INFO, logger=_CONNECTION_LOGGER):
        for _ in range(2):
            await rpc_result(state, "provider.set_key", **target, value="provider-secret-value")
        saved = messages()
        caplog.clear()
        removals = [await rpc_result(state, "provider.unset_key", **target) for _ in range(2)]
        removed = messages()

    assert [removal["removed"] for removal in removals] == [True, False]
    assert len(saved) == 1
    assert "provider=openai connection=api-key configured=true" in saved[0]
    assert len(removed) == 1
    assert "provider=openai connection=api-key configured=False" in removed[0]
    logged = " ".join(saved + removed)
    assert "provider-secret-value" not in logged
    assert "private_slot" not in logged


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _ambiguous_provider() -> SimpleNamespace:
    provider = openrouter_provider()
    provider.id = "ambiguous"
    provider.connections.append(
        SimpleNamespace(
            id="secondary",
            type="api_key",
            label="Secondary API Key",
            auth=SimpleNamespace(credential_key="AMBIGUOUS_SECONDARY_API_KEY"),
        )
    )
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "named"),
    [
        (
            "provider.set_key",
            {"provider_id": "openrouter", "value": "secret", "account": "Not-Valid"},
            "Not-Valid",
        ),
        ("provider.unset_key", {"provider_id": "openrouter", "account": "UPPER"}, "UPPER"),
        (
            "provider.set_key",
            {
                "provider_id": "openrouter",
                "connection_id": "openrouter:api-key:work",
                "value": "secret",
                "account": "personal",
            },
            "params.account 'personal' conflicts with account 'work'",
        ),
        (
            "provider.set_key",
            {"provider_id": "openrouter", "connection_id": "openrouter:oauth", "value": "secret"},
            "'openrouter:oauth' is not an API key connection",
        ),
        (
            "provider.unset_key",
            {"provider_id": "openrouter", "connection_id": "openrouter:oauth"},
            "'openrouter:oauth' is not an API key connection",
        ),
        (
            "provider.set_key",
            {"provider_id": "ambiguous", "value": "secret"},
            "has multiple API key connections; pass connection_id",
        ),
        (
            "connection.set_enabled",
            {"provider_id": "openrouter", "connection_id": "openrouter:api-key", "enabled": "yes"},
            "params.enabled must be a boolean",
        ),
        # Enablement is Connection-level; an Account suffix is an invalid target.
        (
            "connection.set_enabled",
            {
                "provider_id": "openrouter",
                "connection_id": "openrouter:api-key:work",
                "enabled": True,
            },
            "targets a connection, not an account",
        ),
        ("connection.list", {"x": 1}, "connection.list does not accept params"),
    ],
)
async def test_malformed_connection_requests_change_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    params: JsonObject,
    named: str,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider_with_secondary_connection())
    state.runtime.providers.add(_ambiguous_provider())

    error = await rpc_error(state, method, **params)

    assert error["code"] == "invalid_request"
    assert named in error["message"]
    assert state.runtime.storage.load_environment() == {}
    assert resource_changes(state) == []


# ---------------------------------------------------------------------------
# Custom Providers
# ---------------------------------------------------------------------------


def _custom_provider_payload(name: str = "Local AI", provider_id: str = "local-ai") -> JsonObject:
    return {
        "id": provider_id,
        "name": name,
        "adapter": "openai_compatible",
        "base_url": "http://127.0.0.1:8080/v1",
        "auth": "api_key",
        "models_endpoint": "/models",
        "models": {
            "chat-model": {
                "name": "Chat Model",
                "context_window": 65_536,
                "max_output_tokens": 2_048,
                "capabilities": {
                    "tools": True,
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                },
            }
        },
    }


@pytest.mark.asyncio
async def test_custom_provider_crud_is_live_and_keeps_key_out_of_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VBOT_CUSTOM_LOCAL_AI_API_KEY", raising=False)
    runtime = Runtime(Config(data_dir=tmp_path / "data"), safe_startup_mode="test")
    runtime.start()
    try:
        state = SimpleNamespace(
            runtime=runtime,
            event_bus=ServerEventBus(),
            server_bind={"listen_host": "127.0.0.1", "listen_port": 8420, "port_source": "default"},
        )
        providers = runtime.providers
        models = runtime.models

        bundled = await rpc_error(
            state, "provider.custom_save", provider=_custom_provider_payload(provider_id="openai")
        )
        # Model discovery names files after the id; Windows reserves device names.
        reserved = await rpc_error(
            state, "provider.custom_save", provider=_custom_provider_payload(provider_id="con")
        )
        saved = await rpc_result(
            state, "provider.custom_save", provider=_custom_provider_payload(), api_key="secret"
        )

        assert bundled == {
            "code": "invalid_request",
            "message": "Custom Provider id 'openai' conflicts with a bundled Provider",
        }
        assert reserved["code"] == "invalid_request"
        assert reserved["message"].startswith("The Custom Provider id 'con' is reserved on Windows")
        assert saved["provider"]["usable"] is True
        assert "api_key" not in saved["provider"]
        assert runtime.providers is providers
        assert runtime.models is models
        assert providers.get("local-ai").custom is True
        assert models.get("local-ai", "chat-model").context_window == 65_536
        assert runtime.storage.load_environment()["VBOT_CUSTOM_LOCAL_AI_API_KEY"] == "secret"
        assert "secret" not in runtime.storage.settings_path.read_text(encoding="utf-8")
        assert resource_changes(state) == [{"kind": "providers"}, {"kind": "models"}]

        updated = _custom_provider_payload("Renamed")
        updated["models"]["chat-model"]["name"] = "Renamed Model"
        await rpc_result(state, "provider.custom_save", provider=updated)

        assert providers.get("local-ai").name == "Renamed"
        assert models.get("local-ai", "chat-model").name == "Renamed Model"
        listed = await rpc_result(state, "provider.custom_list")
        assert [item["id"] for item in listed["providers"]] == ["local-ai"]

        # The settings projection keeps configured, enabled and usable distinct: the
        # bundled keyless local Ollama Connection needs no key but is not yet added,
        # and its reachability is unknown until a probe runs.
        settings = await rpc_result(state, "settings.get")
        ollama = next(item for item in settings["providers"]["items"] if item["id"] == "ollama")
        local = next(item for item in ollama["connections"] if item["id"] == "ollama:local")
        assert local == {
            "id": "ollama:local",
            "type": "none",
            "label": "Local",
            "added": False,
            "configured": True,
            "enabled": False,
            "usable": False,
            "reachable": None,
            "accounts": [{"id": "default", "usable": True, "source": "none", "credential_key": ""}],
        }
        custom_items = settings["providers"]["custom_endpoints"]["items"]
        assert [item["id"] for item in custom_items] == ["local-ai"]

        deleted = await rpc_result(state, "provider.custom_delete", provider_id="local-ai")

        assert deleted == {"provider_id": "local-ai", "deleted": True, "credentials_removed": 1}
        assert runtime.storage.load_custom_providers_settings() == {}
        assert "VBOT_CUSTOM_LOCAL_AI_API_KEY" not in runtime.storage.load_environment()
        with pytest.raises(KeyError):
            providers.get("local-ai")
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_custom_provider_wire_block_shapes_requests_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``wire`` block is the Provider's wire profile data: requests, ``model.get``
    and the reasoning report use it, a save applies it to existing Adapters, and a
    block with entries vBot would ignore is refused without saving anything."""

    monkeypatch.delenv("VBOT_CUSTOM_LOCAL_AI_API_KEY", raising=False)
    runtime = Runtime(Config(data_dir=tmp_path / "data"), safe_startup_mode="test")
    runtime.start()
    try:
        state = SimpleNamespace(
            runtime=runtime,
            event_bus=ServerEventBus(),
            server_bind={"listen_host": "127.0.0.1", "listen_port": 8420, "port_source": "default"},
        )
        provider = _custom_provider_payload()
        provider["models"]["chat-model"]["capabilities"]["reasoning"] = True
        provider["wire"] = {"defaults": {"reasoning": {"dialect": "thinking_toggle"}}}
        await rpc_result(state, "provider.custom_save", provider=provider, api_key="secret")
        adapter = runtime.get_adapter(ConnectionRef("local-ai", "local-ai:default"))
        assert isinstance(adapter, OpenAICompatibleAdapter)
        url = "http://127.0.0.1:8080/v1/chat/completions"

        toggled = await sent_payload(
            adapter, url=url, model_id="chat-model", thinking_effort="high"
        )
        model = (await rpc_result(state, "model.get", model="local-ai/chat-model"))["model"]

        assert toggled["thinking"] == {"type": "enabled"}
        assert "reasoning_effort" not in toggled
        assert model["wire_profiles"]["default"]["reasoning_dialect"] == "thinking_toggle"
        assert adapter.describe_reasoning_render("chat-model", "high") == (
            ReasoningIntent(REASONING_INTENT_ON)
        )

        refused = await rpc_error(
            state,
            "provider.custom_save",
            provider={**provider, "wire": {"connections": {"api-key": {}}}},
        )

        assert refused["code"] == "invalid_request"
        assert refused["data"] == {
            "wire_issues": [
                "wire.connections.api-key: unknown Connection (this Provider has default), "
                "ignoring it"
            ]
        }
        assert "was not saved" in refused["message"]
        assert (
            runtime.storage.load_custom_providers_settings()["local-ai"]["wire"]
            == (provider["wire"])
        )

        # Without a block, the same Adapter falls back to the protocol defaults.
        await rpc_result(state, "provider.custom_save", provider={**provider, "wire": None})
        effort = await sent_payload(adapter, url=url, model_id="chat-model", thinking_effort="high")

        assert effort["reasoning_effort"] == "high"
        assert "thinking" not in effort
        assert "wire" not in runtime.storage.load_custom_providers_settings()["local-ai"]
        await adapter.aclose()
    finally:
        await runtime.aclose()
