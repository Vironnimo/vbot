"""Tests for runtime providers."""

from pathlib import Path

import pytest

from core.models.models import ModelRegistry
from core.providers.accounts import ConnectionRef
from core.providers.credentials import ProviderCredentialResolver
from core.providers.kimi import KIMI_CODING_MODE
from core.providers.ollama import OllamaAdapter, OllamaCloudAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.providers import ProviderRegistry
from core.providers.stepfun import STEPFUN_DIRECT_MODE, STEPFUN_PLAN_MODE
from core.providers.token_getter import OAuthTokenGetter, StaticTokenGetter
from core.providers.token_store import OAuthToken
from core.runtime.runtime import Runtime
from core.utils.config import Config
from core.utils.errors import ConfigError
from tests.core.runtime.runtime_providers_test_support import (
    runtime as runtime,
)


# ------------------------------------------------------------------
# Provider registry loading
# ------------------------------------------------------------------
def test_runtime_loads_providers(runtime: Runtime) -> None:
    """Runtime.start() loads a ProviderRegistry from resources/."""
    # Assert
    assert runtime.providers is not None
    assert isinstance(runtime.providers, ProviderRegistry)


def test_runtime_providers_populated(runtime: Runtime) -> None:
    """The provider registry contains the expected provider IDs."""
    # Assert
    ids = runtime.providers.list_ids()
    assert "openai" in ids
    assert "anthropic" in ids
    assert "openrouter" in ids
    assert "minimax" in ids
    assert "kimi" in ids
    assert "xai" in ids
    assert "nous" in ids
    assert "stepfun" in ids
    assert "opencode-zen" in ids
    assert "ollama" in ids
    assert "ollama-cloud" in ids


def test_runtime_provider_config_fields(runtime: Runtime) -> None:
    """Provider configs have the expected field values."""
    # Act
    openai_config = runtime.providers.get("openai")
    openrouter_config = runtime.providers.get("openrouter")
    github_copilot_config = runtime.providers.get("github-copilot")
    minimax_config = runtime.providers.get("minimax")
    kimi_config = runtime.providers.get("kimi")
    xai_config = runtime.providers.get("xai")
    nous_config = runtime.providers.get("nous")
    stepfun_config = runtime.providers.get("stepfun")
    opencode_zen_config = runtime.providers.get("opencode-zen")

    # Assert
    assert openai_config.id == "openai"
    assert openai_config.name == "OpenAI"
    assert openai_config.adapter == "openai"
    assert openai_config.base_url == "https://api.openai.com/v1"
    assert [connection.id for connection in openai_config.connections] == [
        "api-key",
        "subscription",
    ]
    assert openai_config.get_connection("api-key").auth.credential_key == "OPENAI_API_KEY"
    codex_connection = openai_config.get_connection("subscription")
    assert codex_connection.mode == "codex_responses"
    codex_oauth = codex_connection.oauth
    assert codex_oauth is not None
    assert codex_oauth.device_flow == "openai_codex"
    assert openrouter_config.adapter == "openrouter"
    assert github_copilot_config.adapter == "github_copilot"
    assert minimax_config.adapter == "minimax"
    assert minimax_config.base_url == "https://api.minimax.io/v1"
    assert minimax_config.models_endpoint is None
    assert [connection.id for connection in minimax_config.connections] == [
        "api-key",
        "api-key-cn",
        "subscription",
    ]
    assert minimax_config.get_connection("api-key").auth.credential_key == "MINIMAX_API_KEY"
    assert minimax_config.get_connection("api-key").models_endpoint == "/models"
    minimax_cn = minimax_config.get_connection("api-key-cn")
    assert minimax_cn.base_url == "https://api.minimaxi.com/v1"
    assert minimax_cn.auth.credential_key == "MINIMAX_CN_API_KEY"
    minimax_subscription = minimax_config.get_connection("subscription")
    assert minimax_subscription.base_url == "https://api.minimax.io/anthropic/v1"
    assert minimax_subscription.mode == "anthropic_messages"
    assert minimax_subscription.models_endpoint == "/models"
    assert minimax_subscription.oauth is not None
    assert minimax_subscription.oauth.device_flow == "minimax_oauth"
    assert kimi_config.adapter == "kimi"
    assert kimi_config.base_url == "https://api.moonshot.ai/v1"
    assert [connection.id for connection in kimi_config.connections] == [
        "coding-plan",
        "api-key",
        "api-key-cn",
    ]
    kimi_coding = kimi_config.get_connection("coding-plan")
    assert kimi_coding.base_url == "https://api.kimi.com/coding/v1"
    assert kimi_coding.mode == KIMI_CODING_MODE
    assert kimi_coding.auth.credential_key == "KIMI_CODING_API_KEY"
    assert kimi_coding.models_endpoint == "/models"
    assert kimi_config.get_connection("api-key").auth.credential_key == "KIMI_API_KEY"
    kimi_cn = kimi_config.get_connection("api-key-cn")
    assert kimi_cn.base_url == "https://api.moonshot.cn/v1"
    assert kimi_cn.auth.credential_key == "KIMI_CN_API_KEY"
    assert xai_config.adapter == "xai"
    assert xai_config.base_url == "https://api.x.ai/v1"
    assert [connection.id for connection in xai_config.connections] == [
        "api-key",
        "subscription",
    ]
    assert xai_config.get_connection("api-key").models_endpoint == "/language-models"
    xai_oauth = xai_config.get_connection("subscription").oauth
    assert xai_oauth is not None
    assert xai_oauth.device_flow == "xai_oauth"
    assert xai_oauth.device_auth_url == "https://auth.x.ai/oauth2/device/code"
    assert xai_oauth.token_url == "https://auth.x.ai/oauth2/token"
    assert nous_config.adapter == "nous"
    assert nous_config.base_url == "https://inference-api.nousresearch.com/v1"
    assert [connection.id for connection in nous_config.connections] == [
        "api-key",
        "subscription",
    ]
    assert nous_config.get_connection("api-key").auth.credential_key == "NOUS_API_KEY"
    nous_oauth = nous_config.get_connection("subscription").oauth
    assert nous_oauth is not None
    assert nous_oauth.device_flow == "nous_oauth"
    assert nous_oauth.client_id == "hermes-cli"
    assert nous_oauth.scopes == ["inference:invoke"]
    assert stepfun_config.adapter == "stepfun"
    assert stepfun_config.base_url == "https://api.stepfun.com/v1"
    assert [connection.id for connection in stepfun_config.connections] == [
        "direct-api",
        "step-plan",
    ]
    stepfun_direct = stepfun_config.get_connection("direct-api")
    assert stepfun_direct.mode == STEPFUN_DIRECT_MODE
    assert stepfun_direct.auth.credential_key == "STEPFUN_DIRECT_API_KEY"
    assert stepfun_direct.models_endpoint == "/models"
    stepfun_plan = stepfun_config.get_connection("step-plan")
    assert stepfun_plan.mode == STEPFUN_PLAN_MODE
    assert stepfun_plan.base_url == "https://api.stepfun.com/step_plan/v1"
    assert stepfun_plan.auth.credential_key == "STEPFUN_API_KEY"
    assert stepfun_plan.models_endpoint == "/models"
    assert opencode_zen_config.adapter == "opencode_zen"
    assert opencode_zen_config.base_url == "https://opencode.ai/zen/v1"
    assert [connection.id for connection in opencode_zen_config.connections] == [
        "api-key",
        "account",
    ]
    zen_api_key = opencode_zen_config.get_connection("api-key")
    assert zen_api_key.auth.credential_key == "OPENCODE_API_KEY"
    assert zen_api_key.models_endpoint == "/models"
    zen_oauth = opencode_zen_config.get_connection("account").oauth
    assert zen_oauth is not None
    assert zen_oauth.device_flow == "opencode_oauth"
    assert zen_oauth.client_id == "opencode-cli"
    assert zen_oauth.device_auth_url == "https://console.opencode.ai/auth/device/code"
    assert zen_oauth.token_url == "https://console.opencode.ai/auth/device/token"


def test_runtime_loads_xai_model_overrides(runtime: Runtime) -> None:
    grok_45 = runtime.models.get("xai", "grok-4.5")
    grok_fixed = runtime.models.get("xai", "grok-4.20-0309-reasoning")
    grok_multi = runtime.models.get("xai", "grok-4.20-multi-agent-0309")

    assert grok_45.connections == ("api-key", "subscription")
    assert grok_45.capabilities.reasoning.levels == ("low", "medium", "high")
    assert grok_fixed.capabilities.input_modalities == ("text", "image")
    assert grok_fixed.capabilities.reasoning.levels == ()
    assert grok_multi.capabilities.reasoning.levels == (
        "low",
        "medium",
        "high",
        "xhigh",
    )
    assert grok_multi.context_window == 1000000


def test_runtime_loads_opencode_zen_current_catalog_and_connection_allowlist(
    runtime: Runtime,
) -> None:
    models = runtime.models.list_for_provider("opencode-zen")
    gemini = runtime.models.get("opencode-zen", "gemini-3.5-flash")
    expiring = runtime.models.get("opencode-zen", "claude-opus-4-1")
    free = runtime.models.get("opencode-zen", "big-pickle")

    assert len(models) == 53
    assert gemini.connections == ("api-key", "account")
    assert gemini.context_window == 1_048_576
    assert gemini.max_output_tokens == 65_536
    assert gemini.capabilities.input_modalities == (
        "text",
        "image",
        "video",
        "audio",
        "pdf",
    )
    assert gemini.metadata["opencode_zen"]["protocol"] == "gemini_generate_content"
    assert expiring.metadata["opencode_zen"]["deprecates_at"] == "2026-08-05"
    assert free.metadata["opencode_zen"]["privacy"] == "free_model_data_collection"
    assert {model.model_id for model in models}.isdisjoint(
        {
            "gpt-5.2-codex",
            "gpt-5.1-codex",
            "gpt-5.1-codex-max",
            "gpt-5.1-codex-mini",
            "gpt-5-codex",
            "claude-sonnet-4",
            "glm-5",
        }
    )


def test_runtime_loads_kimi_models_with_connection_limits(runtime: Runtime) -> None:
    coding_k3 = runtime.models.get("kimi", "k3")
    coding_k3_256k = runtime.models.get("kimi", "k3-256k")
    coding_k27 = runtime.models.get("kimi", "kimi-for-coding")
    direct_k3 = runtime.models.get("kimi", "kimi-k3")
    direct_k26 = runtime.models.get("kimi", "kimi-k2.6")

    assert coding_k3.connections == ("coding-plan",)
    assert coding_k3.context_window == 1048576
    assert coding_k3.capabilities.reasoning.levels == ("low", "high", "max")
    assert coding_k3_256k.connections == ("coding-plan",)
    assert coding_k3_256k.context_window == 262144
    assert coding_k3_256k.capabilities.input_modalities == ("text", "image")
    assert coding_k27.connections == ("coding-plan",)
    assert coding_k27.max_output_tokens == 32768
    assert direct_k3.connections == ("api-key", "api-key-cn")
    assert direct_k26.connections == ("api-key", "api-key-cn")
    assert direct_k26.capabilities.reasoning.control == "on_off"
    assert direct_k26.max_output_tokens == 32768
    assert all(
        model.model_id != "kimi-k2-thinking" for model in runtime.models.list_for_provider("kimi")
    )


def test_runtime_loads_minimax_override_only_models_with_connection_limits(
    runtime: Runtime,
) -> None:
    m25 = runtime.models.get("minimax", "MiniMax-M2.5")
    m27 = runtime.models.get("minimax", "MiniMax-M2.7")
    m3 = runtime.models.get("minimax", "MiniMax-M3")

    assert m25.connections == ("api-key", "api-key-cn")
    assert m27.connections == ("api-key", "api-key-cn", "subscription")
    assert m27.context_window == 204800
    assert m27.max_output_tokens == 65536
    assert m3.connections == ("api-key", "api-key-cn")
    assert m3.context_window == 1000000
    assert m3.max_output_tokens == 131072


def test_runtime_loads_nous_curated_fallback_catalog(runtime: Runtime) -> None:
    models = {model.model_id: model for model in runtime.models.list_for_provider("nous")}

    assert set(models) == {
        "anthropic/claude-sonnet-4.6",
        "deepseek/deepseek-v4-pro",
        "google/gemini-3-pro-preview",
        "openai/gpt-5.5-pro",
    }
    assert all(model.connections == ("api-key", "subscription") for model in models.values())
    assert all(model.max_output_tokens == 32000 for model in models.values())
    assert models["anthropic/claude-sonnet-4.6"].capabilities.tools is True
    assert models["google/gemini-3-pro-preview"].context_window == 1048576


def test_runtime_loads_stepfun_models_with_connection_limits(runtime: Runtime) -> None:
    models = {model.model_id: model for model in runtime.models.list_for_provider("stepfun")}

    assert set(models) == {
        "step-3.5-flash",
        "step-3.5-flash-2603",
        "step-3.7-flash",
        "step-router-v1",
    }
    assert models["step-3.5-flash"].connections == ("direct-api", "step-plan")
    assert models["step-3.5-flash"].capabilities.reasoning.levels == ()
    assert models["step-3.5-flash-2603"].capabilities.reasoning.levels == ("low", "high")
    assert models["step-3.7-flash"].capabilities.reasoning.levels == (
        "low",
        "medium",
        "high",
    )
    assert models["step-3.7-flash"].capabilities.input_modalities == (
        "text",
        "image",
        "video",
    )
    assert models["step-router-v1"].connections == ("step-plan",)
    assert models["step-router-v1"].max_output_tokens == 250000
    assert models["step-router-v1"].metadata["stepfun"]["routes_between"] == (
        "deepseek-v4-pro",
        "step-3.7-flash",
    )


def test_runtime_injects_openrouter_routing_snapshot(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-token")
    runtime.storage.update_settings_sections(
        {
            "providers": {
                "openrouter": {
                    "routing": {
                        "default": {
                            "mode": "allowed",
                            "providers": ["anthropic"],
                            "blocked": ["deepinfra"],
                            "allow_fallbacks": False,
                        },
                        "models": {},
                    }
                }
            }
        }
    )

    adapter = runtime.get_adapter(ConnectionRef("openrouter", "openrouter:api-key"))

    assert isinstance(adapter, OpenRouterAdapter)
    assert adapter._routing["default"] == {  # type: ignore[attr-defined]
        "mode": "allowed",
        "providers": ["anthropic"],
        "blocked": ["deepinfra"],
        "allow_fallbacks": False,
    }


def test_provider_credential_resolver_has_credentials_for_connection(
    tmp_path: Path,
) -> None:
    """Per-connection credential checks use the connection auth config."""
    # Arrange
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()

    # Act / Assert
    assert runtime.provider_credentials.has_credentials("openai", "openai:api-key") is False
    resolver = ProviderCredentialResolver(
        runtime.providers,
        process_env={"OPENAI_API_KEY": "sk-test"},
    )
    assert resolver.has_credentials("openai", "openai:api-key") is True


def test_provider_credential_resolver_get_credentials_for_connection(
    tmp_path: Path,
) -> None:
    """Per-connection credential lookup returns the matching credential value."""
    # Arrange
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()
    resolver = ProviderCredentialResolver(
        runtime.providers,
        process_env={"OPENAI_API_KEY": "sk-test"},
    )

    # Act
    credential = resolver.get_credentials("openai", "openai:api-key")

    # Assert
    assert credential == "sk-test"


def test_provider_credential_resolver_get_connection_missing_credentials(
    tmp_path: Path,
) -> None:
    """Per-connection credential lookup raises ConfigError when missing."""
    # Arrange
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()
    resolver = ProviderCredentialResolver(runtime.providers, process_env={})

    # Act / Assert
    with pytest.raises(ConfigError):
        resolver.get_credentials("openai", "openai:api-key")


def test_provider_credential_resolver_connection_missing_from_env_and_fallback(
    tmp_path: Path,
) -> None:
    """A credential absent from process env and fallback is not usable."""
    # Arrange
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()
    resolver = ProviderCredentialResolver(
        runtime.providers,
        process_env={},
        fallback_credentials={"OTHER_KEY": "other-value"},
    )

    # Act / Assert
    assert resolver.has_credentials("openai", "openai:api-key") is False
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        resolver.get_credentials("openai", "openai:api-key")


def test_provider_credential_resolver_provider_level_delegates_to_first_usable(
    tmp_path: Path,
) -> None:
    """Provider-level lookups return the first usable connection in config order."""
    # Arrange
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()
    resolver = ProviderCredentialResolver(
        runtime.providers,
        process_env={
            "OPENAI_API_KEY": "api-key",
            "GITHUB_COPILOT_TOKEN": "copilot-token",
        },
    )

    # Act / Assert
    assert resolver.has_credentials("openai") is True
    assert resolver.get_credentials("openai") == "api-key"


def test_provider_credential_resolver_provider_level_skips_unusable_connection(
    tmp_path: Path,
) -> None:
    """Provider-level lookup skips missing credentials and uses the next usable connection."""
    # Arrange
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()
    resolver = ProviderCredentialResolver(
        runtime.providers,
        process_env={"OPENAI_API_KEY": "api-key"},
    )

    # Act / Assert
    assert resolver.has_credentials("openai") is True
    assert resolver.get_credentials("openai") == "api-key"


def test_provider_credential_resolver_unknown_connection_id_raises_config_error(
    tmp_path: Path,
) -> None:
    """Unknown connection IDs raise ConfigError."""
    # Arrange
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()

    # Act / Assert
    with pytest.raises(ConfigError):
        runtime.provider_credentials.has_credentials("openai", "openai:missing")


# ------------------------------------------------------------------
# Model registry loading
# ------------------------------------------------------------------
def test_runtime_loads_models(runtime: Runtime) -> None:
    """Runtime.start() loads a ModelRegistry from resources/."""
    # Assert
    assert runtime.models is not None
    assert isinstance(runtime.models, ModelRegistry)


def test_runtime_models_populated(runtime: Runtime) -> None:
    """The model registry contains models from all providers."""
    # Act
    openai_models = runtime.models.list_for_provider("openai")
    anthropic_models = runtime.models.list_for_provider("anthropic")
    openrouter_models = runtime.models.list_for_provider("openrouter")

    # Assert
    assert len(openai_models) > 0
    assert len(anthropic_models) > 0
    assert len(openrouter_models) > 0


def test_runtime_model_fields(runtime: Runtime) -> None:
    """Model entries have the expected field values."""
    # Act
    model = runtime.models.get("anthropic", "claude-sonnet-4-6")

    # Assert
    assert model.model_id == "claude-sonnet-4-6"
    assert model.name == "Claude Sonnet 4.6"
    assert model.context_window == 1000000
    assert model.capabilities.vision is True
    assert model.capabilities.reasoning.supported is True


# ------------------------------------------------------------------
# Public per-connection token accessors
# ------------------------------------------------------------------
def test_get_connection_token_getter_returns_static_for_api_key(runtime: Runtime) -> None:
    """An api-key connection yields a StaticTokenGetter."""
    # Arrange
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        runtime.providers,
        process_env={"MINIMAX_API_KEY": "minimax-token"},
    )

    # Act
    getter = runtime.get_connection_token_getter(ConnectionRef("minimax", "minimax:api-key"))

    # Assert
    assert isinstance(getter, StaticTokenGetter)


@pytest.mark.asyncio
async def test_token_getter_for_none_connection_is_static_and_empty(runtime: Runtime) -> None:
    """A keyless ``none`` connection yields a StaticTokenGetter with an empty token."""
    # Act — the shipped Ollama config carries a keyless local connection.
    getter = runtime.get_connection_token_getter(ConnectionRef("ollama", "ollama:local"))

    # Assert
    assert isinstance(getter, StaticTokenGetter)
    assert await getter() == ""


def test_runtime_wires_ollama_adapter_for_keyless_local_connection(runtime: Runtime) -> None:
    """get_adapter wires the Ollama adapter without any configured credential."""
    # Arrange — keyless local connections are disabled until the user opts in.
    runtime.storage.set_provider_connection_enabled("ollama:local", True)

    # Act
    adapter = runtime.get_adapter(ConnectionRef("ollama", "ollama:local"))

    # Assert
    assert isinstance(adapter, OllamaAdapter)


def test_runtime_wires_openai_compatible_adapter_for_ollama_cloud(
    runtime: Runtime,
) -> None:
    """Direct Cloud chat uses Ollama's OpenAI-compatible reasoning wire."""
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        runtime.providers,
        process_env={"OLLAMA_API_KEY": "ollama-secret"},
    )

    adapter = runtime.get_adapter(ConnectionRef("ollama-cloud", "ollama-cloud:api-key"))

    assert isinstance(adapter, OllamaCloudAdapter)


def test_runtime_wires_provider_and_model_reasoning_replay_precedence(runtime: Runtime) -> None:
    runtime._provider_credentials = ProviderCredentialResolver(  # type: ignore[attr-defined]
        runtime.providers,
        process_env={"OLLAMA_API_KEY": "ollama-secret"},
    )
    # Provider override applies to every model without a Model-level override.
    runtime.models._provider_reasoning_replay["ollama-cloud"] = "current_run"  # type: ignore[attr-defined]

    adapter = runtime.get_adapter(ConnectionRef("ollama-cloud", "ollama-cloud:api-key"))

    assert adapter.reasoning_replay_policy("unprofiled-model") == "current_run"
    # GLM-5.2 has no Model-level override anymore — it inherits the Provider policy.
    assert adapter.reasoning_replay_policy("glm-5.2") == "current_run"
    # The MiniMax M3 Model override (none) still wins over the Provider policy.
    assert adapter.reasoning_replay_policy("minimax-m3") == "none"


def test_get_adapter_rejects_disabled_connection(runtime: Runtime) -> None:
    """A disabled connection never reaches adapter construction."""
    # Act / Assert — ollama:local is keyless and therefore disabled by default.
    with pytest.raises(ConfigError):
        runtime.get_adapter(ConnectionRef("ollama", "ollama:local"))


def test_ollama_provider_config_fields(runtime: Runtime) -> None:
    """Local Ollama and direct Ollama Cloud are distinct Provider identities."""
    # Act
    local_config = runtime.providers.get("ollama")
    cloud_config = runtime.providers.get("ollama-cloud")

    # Assert
    assert local_config.adapter == "ollama"
    assert local_config.base_url == "http://localhost:11434"
    assert local_config.models_endpoint == "/api/tags"
    local = local_config.get_connection("local")
    assert local.type == "none"
    assert local.mode == "local"

    assert cloud_config.adapter == "ollama_cloud"
    assert cloud_config.base_url == "https://ollama.com"
    assert cloud_config.models_endpoint == "/api/tags"
    cloud = cloud_config.get_connection("api-key")
    assert cloud.type == "api_key"
    assert cloud.mode == "cloud"
    assert cloud.catalog_requires_credentials is False
    assert cloud.auth.credential_key == "OLLAMA_API_KEY"


@pytest.mark.asyncio
async def test_get_connection_token_getter_returns_oauth_for_subscription(
    runtime: Runtime,
) -> None:
    """An OAuth connection yields a refresh-capable OAuthTokenGetter."""
    # Arrange
    runtime.token_store.save(
        "openai",
        "subscription",
        OAuthToken(access_token="oauth-access-token"),
    )

    # Act
    getter = runtime.get_connection_token_getter(ConnectionRef("openai", "openai:subscription"))

    # Assert
    assert isinstance(getter, OAuthTokenGetter)
    assert await getter() == "oauth-access-token"


def test_get_connection_token_extra_returns_stored_extra(runtime: Runtime) -> None:
    """Stored OAuth token extra metadata is returned for the connection."""
    # Arrange
    runtime.token_store.save(
        "github-copilot",
        "oauth",
        OAuthToken(
            access_token="copilot-token",
            extra={"github_oauth_token": "gho_example"},
        ),
    )

    # Act
    extra = runtime.get_connection_token_extra(
        ConnectionRef("github-copilot", "github-copilot:oauth")
    )

    # Assert
    assert extra == {"github_oauth_token": "gho_example"}


def test_copilot_adapter_uses_account_specific_exchange_endpoint(runtime: Runtime) -> None:
    """Copilot OAuth Accounts route through the endpoint returned by exchange."""

    runtime.token_store.save(
        "github-copilot",
        "oauth",
        OAuthToken(
            access_token="copilot-token",
            extra={
                "github_oauth_token": "gho_example",
                "copilot_api_endpoint": "https://api.enterprise.githubcopilot.com",
            },
        ),
    )

    adapter = runtime.get_adapter(ConnectionRef("github-copilot", "github-copilot:oauth"))

    assert str(adapter._client.base_url) == "https://api.enterprise.githubcopilot.com"  # type: ignore[attr-defined]


def test_get_connection_token_extra_returns_empty_when_absent(runtime: Runtime) -> None:
    """A connection with no stored token yields an empty extra mapping."""
    # Act
    extra = runtime.get_connection_token_extra(
        ConnectionRef("github-copilot", "github-copilot:oauth")
    )

    # Assert
    assert extra == {}


# ------------------------------------------------------------------
# Error cases: registries not accessible before start
# ------------------------------------------------------------------
def test_providers_not_accessible_before_start(tmp_path: Path) -> None:
    """Accessing providers before start() raises RuntimeError."""
    # Arrange
    config = Config(data_dir=tmp_path / "data")
    runtime = Runtime(config)

    # Act & Assert
    with pytest.raises(RuntimeError):
        _ = runtime.providers


def test_models_not_accessible_before_start(tmp_path: Path) -> None:
    """Accessing models before start() raises RuntimeError."""
    # Arrange
    config = Config(data_dir=tmp_path / "data")
    runtime = Runtime(config)

    # Act & Assert
    with pytest.raises(RuntimeError):
        _ = runtime.models


def test_phase_two_services_not_accessible_before_start(tmp_path: Path) -> None:
    """Accessing Phase 2 services before start() raises RuntimeError."""
    runtime = Runtime(Config(data_dir=tmp_path / "data"))

    for attribute_name in (
        "storage",
        "agents",
        "tools",
        "video",
        "music",
        "skills",
        "chat_sessions",
        "system_prompts",
    ):
        with pytest.raises(RuntimeError):
            getattr(runtime, attribute_name)


def test_runtime_loads_phase_two_services(runtime: Runtime) -> None:
    """Runtime.start() loads Phase 2 services alongside registries."""
    assert runtime.storage.data_dir.exists()
    assert runtime.agents.data_dir == runtime.storage.data_dir
    # The Home Assistant tools ship as a bundled extension; they are always
    # registered (readiness only hides them from model-facing surfaces until a
    # token is set), so they appear in the registered inventory here.
    registry = runtime.extensions
    assert registry is not None
    hidden_session_tools = {
        declaration.name
        for record in registry.records()
        if record.status == "loaded"
        for declaration in record.declarations.tools
        if declaration.session_scoped
    }
    assert [
        tool.name for tool in runtime.tools.list_tools() if tool.name not in hidden_session_tools
    ] == [
        "analyze_image",
        "apply_patch",
        "bash",
        "calendar",
        "computer",
        "cron",
        "edit",
        "generate_music",
        "generate_video",
        "glob",
        "grep",
        "ha_call_service",
        "ha_get_state",
        "ha_list_entities",
        "ha_list_services",
        "history",
        "image_generation",
        "memory",
        "process",
        "project",
        "read",
        "session_read",
        "session_search",
        "skill",
        "skill_manage",
        "status",
        "subagent",
        "terminal",
        "text_to_speech",
        "web_fetch",
        "web_search",
        "write",
    ]
    assert hidden_session_tools <= {tool.name for tool in runtime.tools.list_tools()}
    assert not hidden_session_tools & {
        tool.name for tool in runtime.tools.list_tools(include_catalog_hidden=False)
    }
    assert [skill.name for skill in runtime.skills.list_all()] == [
        "coding-agents",
        "computer-use",
        "free-models",
        "home-assistant",
        "pdf",
        "playwright-cli",
        "vbot-cli",
        "weather",
    ]
    assert runtime.skills.invalid_diagnostics() == []
    assert runtime.storage.layout.sessions_db_path.is_file()
