"""Runtime credential sources: data-directory credentials behind the process environment."""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest

from core.chat.errors import ChatError
from core.chat.model_resolution import _resolve_agent_connection
from core.providers.accounts import ConnectionRef
from core.runtime.runtime import Runtime
from core.secrets import CREDENTIALS, SecretStore
from core.utils.config import Config
from core.utils.errors import ConfigError
from tests.core.channels.channels_test_support import make_config
from tests.core.extensions.extension_test_support import write_extension

OPENROUTER = ConnectionRef("openrouter", "openrouter:api-key")


@pytest.mark.asyncio
async def test_data_dir_credentials_supply_providers_behind_the_process_environment(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    SecretStore(config.data_dir).set_entry(CREDENTIALS, "OPENROUTER_API_KEY", "sk-or-from-data-dir")
    # A plaintext .env of an earlier vBot is never read.
    config.data_dir.joinpath(".env").write_text("OPENAI_API_KEY=sk-plaintext\n", encoding="utf-8")
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        adapter = runtime.get_adapter(OPENROUTER)
        assert runtime.has_provider_credentials("openrouter") is True
        assert runtime.get_provider_credentials("openrouter") == "sk-or-from-data-dir"
        assert await adapter._token_getter() == "sk-or-from-data-dir"  # type: ignore[attr-defined]
        # Data-directory credentials never leak into the live process environment.
        assert "OPENROUTER_API_KEY" not in os.environ

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-process")
        assert runtime.get_provider_credentials("openrouter") == "sk-or-from-process"
        adapter = runtime.get_adapter(OPENROUTER)
        assert await adapter._token_getter() == "sk-or-from-process"  # type: ignore[attr-defined]

        # Even an empty process value wins over the data-directory fallback.
        monkeypatch.setenv("OPENROUTER_API_KEY", "")
        assert runtime.has_provider_credentials("openrouter") is False
        with pytest.raises(ConfigError):
            runtime.get_adapter(OPENROUTER)

        # Provider-level status: usable as soon as any Connection is usable.
        assert runtime.has_provider_credentials("openai") is False
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key")
        assert runtime.has_provider_credentials("openai") is True
    finally:
        runtime.stop()


def test_environment_credentials_prefer_the_process_and_reload_into_live_resolvers(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("CHANNEL_TOKEN_TEST", "GITHUB_TOKEN", "GitHub_Token", "OPENCODE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        assert runtime.environment_credential_source("CHANNEL_TOKEN_TEST") is None
        assert runtime.resolve_environment_credential("CHANNEL_TOKEN_TEST") == ""

        runtime.storage.set_data_dir_credential("CHANNEL_TOKEN_TEST", "data-token")
        runtime.storage.set_data_dir_credential("GITHUB_TOKEN", "fallback-token")
        runtime.reload_environment_credentials()
        assert runtime.environment_credential_source("CHANNEL_TOKEN_TEST") == "data_dir"
        assert runtime.resolve_environment_credential("CHANNEL_TOKEN_TEST") == "data-token"

        monkeypatch.setenv("CHANNEL_TOKEN_TEST", "process-token")
        assert runtime.environment_credential_source("CHANNEL_TOKEN_TEST") == (
            "process_environment"
        )
        assert runtime.resolve_environment_credential("CHANNEL_TOKEN_TEST") == "process-token"

        # The fallback follows the host's name case rules: a Skill granting
        # ``GitHub_Token`` resolves a data-directory ``GITHUB_TOKEN`` on Windows,
        # and every spelling resolves the uppercase entry, never an older
        # lowercase one an earlier build stored beside it.
        runtime.storage.secrets.set_entry(CREDENTIALS, "github_token", "older-spelling")
        runtime.reload_environment_credentials()
        for ignore_case in (True, False):
            monkeypatch.setattr("core.runtime.runtime._ENVIRONMENT_NAMES_IGNORE_CASE", ignore_case)
            if ignore_case:
                for spelling in ("GitHub_Token", "github_token"):
                    assert runtime.resolve_environment_credential(spelling) == "fallback-token"
                assert runtime.environment_credential_source("GitHub_Token") == "data_dir"
            else:
                assert runtime.resolve_environment_credential("GitHub_Token") == ""
                assert runtime.environment_credential_source("GitHub_Token") is None
            assert runtime.resolve_environment_credential("GITHUB_TOKEN") == "fallback-token"

        # A post-start API key enables bare-model resolution in the already
        # injected chat and Agent resolvers, without a restart or a pin.
        dependencies = runtime.chat_loop._dependencies  # noqa: SLF001 - injected resolver seam.
        injected_resolver = dependencies.provider_credentials
        model = "opencode-go/deepseek-v4-pro"
        agent = runtime.agents.update("main", model=model)
        with pytest.raises(ChatError):
            _resolve_agent_connection(dependencies, agent)

        runtime.storage.set_data_dir_credential("OPENCODE_API_KEY", "test-secret")
        runtime.reload_environment_credentials()

        assert runtime.provider_credentials is injected_resolver
        assert _resolve_agent_connection(dependencies, agent) == (
            "opencode-go",
            "opencode-go:api-key",
        )
        runtime.agent_resolver.require_model_configured(model)
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_environment_credential_names_join_every_owner_and_each_resolved_name(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_extension(
        config.data_dir / "extensions",
        "hub",
        "def register(api):\n"
        "    api.register_settings([\n"
        "        {'key': 'token', 'type': 'secret', 'label': 'Token', 'env_key': 'HUB_TOKEN'},\n"
        "    ])\n",
    )
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        await runtime.channel_service.create_channel(
            replace(make_config(enabled=False), agent_id="main")
        )
        await runtime.reload_extensions()
        runtime.storage.set_data_dir_credential("STORED_TOKEN", "stored")
        runtime.reload_environment_credentials()

        names = runtime.environment_credential_names()
        for name in (
            "OPENAI_API_KEY",
            "OPENAI_API_KEY__WORK",  # an Account of the Connection
            "BRAVE_API_KEY",
            "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
            "HUB_TOKEN",
            "STORED_TOKEN",
        ):
            assert name in names
        assert not any(name in names for name in ("PATH", "OPENAI_API_KEY__DEFAULT", "MCP_TOKEN"))
        # A name vBot's consumers resolve, such as an MCP credential reference, counts from then on.
        runtime.resolve_environment_credential("MCP_TOKEN")
        assert "MCP_TOKEN" in runtime.environment_credential_names()
        # Names compare as the host compares them.
        monkeypatch.setattr("core.secrets.store.CREDENTIAL_NAMES_IGNORE_CASE", True)
        assert "openai_api_key__work" in runtime.environment_credential_names()
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["reload", "restart"])
async def test_async_credential_reload_cannot_restore_a_superseded_snapshot(
    config: Config, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    monkeypatch.delenv("CHANNEL_TOKEN_TEST", raising=False)
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    loop = asyncio.get_running_loop()
    retire = Mock(wraps=runtime._provider_operations().retire_http_transports)
    monkeypatch.setattr(runtime._provider_operations(), "retire_http_transports", retire)
    entered = asyncio.Event()
    release = threading.Event()
    load_environment = runtime.storage.load_environment

    def old_snapshot() -> dict[str, str]:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return {"CHANNEL_TOKEN_TEST": "old"}

    monkeypatch.setattr(runtime.storage, "load_environment", old_snapshot)
    pending = asyncio.create_task(runtime.reload_environment_credentials_async())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        monkeypatch.setattr(runtime.storage, "load_environment", load_environment)
        runtime.storage.set_data_dir_credential("CHANNEL_TOKEN_TEST", "new")
        if replacement == "restart":
            await runtime.aclose()
            runtime.start()
        else:
            runtime.reload_environment_credentials()
        release.set()
        await pending
        assert runtime.resolve_environment_credential("CHANNEL_TOKEN_TEST") == "new"
        # Only a published snapshot retires pools; stale reads cannot disturb them.
        assert retire.call_count == (1 if replacement == "reload" else 0)
    finally:
        release.set()
        await pending
        await runtime.aclose()
