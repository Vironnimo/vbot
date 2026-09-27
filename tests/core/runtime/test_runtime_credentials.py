"""Runtime credential sources: data-directory ``.env`` fallback behind the process environment."""

from __future__ import annotations

import os

import pytest

from core.chat.errors import ChatError
from core.chat.model_resolution import _resolve_agent_connection
from core.providers.accounts import ConnectionRef
from core.runtime.runtime import Runtime
from core.utils.config import Config
from core.utils.errors import ConfigError

OPENROUTER = ConnectionRef("openrouter", "openrouter:api-key")


@pytest.mark.asyncio
async def test_data_dir_env_supplies_provider_credentials_behind_the_process_environment(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    config.data_dir.joinpath(".env").write_text(
        "OPENROUTER_API_KEY=sk-or-from-data-dir\n", encoding="utf-8"
    )
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
        # ``GitHub_Token`` resolves a `.env` ``GITHUB_TOKEN`` on Windows.
        for ignore_case in (True, False):
            monkeypatch.setattr("core.runtime.runtime._ENVIRONMENT_NAMES_IGNORE_CASE", ignore_case)
            if ignore_case:
                assert runtime.resolve_environment_credential("GitHub_Token") == "fallback-token"
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
