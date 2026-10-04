"""Runtime Provider wiring: bundled catalogs, adapter construction, and Connection access."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from core.providers.accounts import ConnectionRef
from core.providers.anthropic import AnthropicAdapter
from core.providers.errors import ProviderAuthError
from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.kimi import KIMI_CODING_MODE, KimiAdapter
from core.providers.minimax import MiniMaxAdapter
from core.providers.mistral import MistralAdapter
from core.providers.nous import NousAdapter
from core.providers.ollama import OllamaCloudAdapter
from core.providers.openai import CODEX_RESPONSES_MODE, OpenAIAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.opencode_zen import OpenCodeZenAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.reasoning_dialects import describe_profile_reasoning
from core.providers.runtime import ADAPTER_TYPES
from core.providers.stepfun import StepFunAdapter
from core.providers.token_getter import OAuthTokenGetter, StaticTokenGetter
from core.providers.token_store import OAuthToken
from core.providers.xai import XAIAdapter
from core.runtime.runtime import Runtime
from core.utils.errors import ConfigError


def test_bundled_provider_configs_expose_their_connections(shared_runtime: Runtime) -> None:
    providers = shared_runtime.providers
    assert set(providers.list_ids()) >= {
        "openai",
        "anthropic",
        "openrouter",
        "minimax",
        "kimi",
        "xai",
        "nous",
        "stepfun",
        "opencode-zen",
        "ollama",
        "ollama-cloud",
    }

    openai_config = providers.get("openai")
    assert (openai_config.name, openai_config.adapter, openai_config.base_url) == (
        "OpenAI",
        "openai",
        "https://api.openai.com/v1",
    )
    assert [connection.id for connection in openai_config.connections] == [
        "api-key",
        "subscription",
    ]
    assert openai_config.get_connection("api-key").auth.credential_key == "OPENAI_API_KEY"
    codex_connection = openai_config.get_connection("subscription")
    assert codex_connection.mode == "codex_responses"
    assert codex_connection.oauth is not None
    assert codex_connection.oauth.device_flow == "openai_codex"
    assert providers.get("openrouter").adapter == "openrouter"
    assert providers.get("github-copilot").adapter == "github_copilot"

    minimax_config = providers.get("minimax")
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

    kimi_config = providers.get("kimi")
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

    xai_config = providers.get("xai")
    assert xai_config.adapter == "xai"
    assert xai_config.base_url == "https://api.x.ai/v1"
    assert [connection.id for connection in xai_config.connections] == ["api-key", "subscription"]
    assert xai_config.get_connection("api-key").models_endpoint == "/language-models"
    xai_oauth = xai_config.get_connection("subscription").oauth
    assert xai_oauth is not None
    assert xai_oauth.device_flow == "xai_oauth"
    assert xai_oauth.device_auth_url == "https://auth.x.ai/oauth2/device/code"
    assert xai_oauth.token_url == "https://auth.x.ai/oauth2/token"

    nous_config = providers.get("nous")
    assert nous_config.adapter == "nous"
    assert nous_config.base_url == "https://inference-api.nousresearch.com/v1"
    assert [connection.id for connection in nous_config.connections] == ["api-key", "subscription"]
    assert nous_config.get_connection("api-key").auth.credential_key == "NOUS_API_KEY"
    nous_oauth = nous_config.get_connection("subscription").oauth
    assert nous_oauth is not None
    assert nous_oauth.device_flow == "nous_oauth"
    assert nous_oauth.client_id == "hermes-cli"
    assert nous_oauth.scopes == ["inference:invoke"]

    stepfun_config = providers.get("stepfun")
    assert stepfun_config.adapter == "stepfun"
    assert stepfun_config.base_url == "https://api.stepfun.com/v1"
    assert [connection.id for connection in stepfun_config.connections] == [
        "direct-api",
        "step-plan",
    ]
    stepfun_direct = stepfun_config.get_connection("direct-api")
    assert stepfun_direct.mode == "direct_api"
    assert stepfun_direct.auth.credential_key == "STEPFUN_DIRECT_API_KEY"
    assert stepfun_direct.models_endpoint == "/models"
    stepfun_plan = stepfun_config.get_connection("step-plan")
    assert stepfun_plan.mode == "step_plan"
    assert stepfun_plan.base_url == "https://api.stepfun.com/step_plan/v1"
    assert stepfun_plan.auth.credential_key == "STEPFUN_API_KEY"
    assert stepfun_plan.models_endpoint == "/models"

    zen_config = providers.get("opencode-zen")
    assert zen_config.adapter == "opencode_zen"
    assert zen_config.base_url == "https://opencode.ai/zen/v1"
    assert [connection.id for connection in zen_config.connections] == ["api-key", "account"]
    zen_api_key = zen_config.get_connection("api-key")
    assert zen_api_key.auth.credential_key == "OPENCODE_API_KEY"
    assert zen_api_key.models_endpoint == "/models"
    zen_oauth = zen_config.get_connection("account").oauth
    assert zen_oauth is not None
    assert zen_oauth.device_flow == "opencode_oauth"
    assert zen_oauth.client_id == "opencode-cli"
    assert zen_oauth.device_auth_url == "https://console.opencode.ai/auth/device/code"
    assert zen_oauth.token_url == "https://console.opencode.ai/auth/device/token"

    # Local Ollama and direct Ollama Cloud are distinct Provider identities.
    local_config = providers.get("ollama")
    assert (local_config.adapter, local_config.base_url, local_config.models_endpoint) == (
        "ollama",
        "http://localhost:11434",
        "/api/tags",
    )
    local = local_config.get_connection("local")
    assert (local.type, local.mode) == ("none", "local")
    cloud_config = providers.get("ollama-cloud")
    assert (cloud_config.adapter, cloud_config.base_url, cloud_config.models_endpoint) == (
        "ollama_cloud",
        "https://ollama.com",
        "/api/tags",
    )
    cloud = cloud_config.get_connection("api-key")
    assert (cloud.type, cloud.mode) == ("api_key", "cloud")
    assert cloud.catalog_requires_credentials is False
    assert cloud.auth.credential_key == "OLLAMA_API_KEY"


def test_get_model_reads_the_bundled_catalogs(shared_runtime: Runtime) -> None:
    expected = {
        ("openrouter", "anthropic/claude-sonnet-4"): (
            "Anthropic: Claude Sonnet 4",
            200000,
            64000,
            {"vision": True, "tools": True},
        ),
        ("openrouter", "anthropic/claude-haiku-4.5"): (
            "Anthropic: Claude Haiku 4.5",
            200000,
            64000,
            {"vision": True, "tools": True, "json_mode": True},
        ),
        ("openrouter", "openai/gpt-5.5"): (
            "OpenAI: GPT-5.5",
            1050000,
            128000,
            {"vision": True, "tools": True, "json_mode": True},
        ),
        ("openrouter", "anthropic/claude-opus-4.7"): (
            "Anthropic: Claude Opus 4.7",
            1000000,
            128000,
            {"vision": True, "tools": True, "json_mode": True},
        ),
        ("anthropic", "claude-sonnet-4-6"): ("Claude Sonnet 4.6", 1000000, None, {"vision": True}),
        ("openai", "gpt-5.2"): ("GPT-5.2", None, None, {}),
    }
    for (provider_id, model_id), (name, context, max_output, flags) in expected.items():
        model = shared_runtime.get_model(provider_id, model_id)
        assert (model.model_id, model.name) == (model_id, name)
        if context is not None:
            assert model.context_window == context
        if max_output is not None:
            assert model.max_output_tokens == max_output
        for flag in flags:
            assert getattr(model.capabilities, flag) is True
        if provider_id != "openai":
            assert model.capabilities.reasoning.supported is True
    with pytest.raises(KeyError):
        shared_runtime.get_model("nonexistent", "model")


def test_runtime_loads_xai_model_overrides(shared_runtime: Runtime) -> None:
    grok_45 = shared_runtime.models.get("xai", "grok-4.5")
    grok_fixed = shared_runtime.models.get("xai", "grok-4.20-0309-reasoning")
    grok_multi = shared_runtime.models.get("xai", "grok-4.20-multi-agent-0309")

    assert grok_45.connections == ("api-key", "subscription")
    assert grok_45.capabilities.reasoning.levels == ("low", "medium", "high")
    assert grok_fixed.capabilities.input_modalities == ("text", "image")
    assert grok_fixed.capabilities.reasoning.levels == ()
    assert grok_multi.capabilities.reasoning.levels == ("low", "medium", "high", "xhigh")
    assert grok_multi.context_window == 1000000


def test_runtime_loads_opencode_zen_current_catalog_and_connection_allowlist(
    shared_runtime: Runtime,
) -> None:
    models = shared_runtime.models.list_for_provider("opencode-zen")
    gemini = shared_runtime.models.get("opencode-zen", "gemini-3.5-flash")

    assert {model.model_id for model in models} >= {"gpt-6-sol", "gpt-6-luna", "claude-opus-5-5"}
    assert all(
        shared_runtime.models.get("opencode-zen", model_id).connections == ("api-key", "account")
        for model_id in ("gpt-6-sol", "gpt-6-luna", "claude-opus-5-5")
    )
    assert gemini.connections == ("api-key", "account")
    assert gemini.context_window == 1_048_576
    assert gemini.max_output_tokens == 65_536
    assert gemini.capabilities.input_modalities == ("text", "image", "video", "audio", "pdf")
    assert {model.model_id for model in models}.isdisjoint(
        {
            "big-pickle",
            "mimo-v2.6-flash-free",
            "claude-opus-4-1",
            "minimax-m2.5",
            "kimi-k2.5",
            "gpt-5.2-codex",
            "gpt-5.1-codex",
            "gpt-5.1-codex-max",
            "gpt-5.1-codex-mini",
            "gpt-5-codex",
            "claude-sonnet-4",
            "glm-5",
        }
    )


def test_runtime_loads_kimi_models_with_connection_limits(shared_runtime: Runtime) -> None:
    models = shared_runtime.models
    coding_k3 = models.get("kimi", "k3")
    coding_k3_256k = models.get("kimi", "k3-256k")
    coding_k27 = models.get("kimi", "kimi-for-coding")
    direct_k3 = models.get("kimi", "kimi-k3")
    direct_k26 = models.get("kimi", "kimi-k2.6")

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
    assert all(model.model_id != "kimi-k2-thinking" for model in models.list_for_provider("kimi"))


def test_runtime_loads_minimax_override_only_models_with_connection_limits(
    shared_runtime: Runtime,
) -> None:
    m25 = shared_runtime.models.get("minimax", "MiniMax-M2.5")
    m27 = shared_runtime.models.get("minimax", "MiniMax-M2.7")
    m3 = shared_runtime.models.get("minimax", "MiniMax-M3")

    assert m25.connections == ("api-key", "api-key-cn")
    assert m27.connections == ("api-key", "api-key-cn", "subscription")
    assert (m27.context_window, m27.max_output_tokens) == (204800, 65536)
    assert m3.connections == ("api-key", "api-key-cn")
    assert (m3.context_window, m3.max_output_tokens) == (1000000, 131072)


def test_runtime_loads_nous_curated_fallback_catalog(shared_runtime: Runtime) -> None:
    models = {model.model_id: model for model in shared_runtime.models.list_for_provider("nous")}

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


def test_runtime_loads_stepfun_models_with_connection_limits(shared_runtime: Runtime) -> None:
    models = {model.model_id: model for model in shared_runtime.models.list_for_provider("stepfun")}

    assert set(models) == {
        "step-3.5-flash",
        "step-3.5-flash-2603",
        "step-3.7-flash",
        "step-router-v1",
    }
    assert models["step-3.5-flash"].connections == ("direct-api", "step-plan")
    assert models["step-3.5-flash"].capabilities.reasoning.levels == ()
    assert models["step-3.5-flash-2603"].capabilities.reasoning.levels == ("low", "high")
    assert models["step-3.7-flash"].capabilities.reasoning.levels == ("low", "medium", "high")
    assert models["step-3.7-flash"].capabilities.input_modalities == ("text", "image", "video")
    assert models["step-router-v1"].connections == ("step-plan",)
    assert models["step-router-v1"].max_output_tokens == 250000


# Adapter internals are read directly: the Runtime's contract is what it hands
# each adapter (Connection mode, base URL, auth metadata, headers, model lookup).
_ADAPTER_WIRING: list[tuple[str, type, Callable[[Any], bool]]] = [
    (
        "openai:api-key",
        OpenAIAdapter,
        lambda adapter: (
            adapter._connection_mode is None
            and adapter._config.base_url == "https://api.openai.com/v1"
        ),
    ),
    (
        "anthropic:api-key",
        AnthropicAdapter,
        lambda adapter: (
            adapter._config.base_url == "https://api.anthropic.com/v1"
            and (adapter._auth_config.header, adapter._auth_config.prefix) == ("x-api-key", "")
        ),
    ),
    (
        "openrouter:api-key",
        OpenRouterAdapter,
        lambda adapter: "HTTP-Referer" in adapter._config.extra_headers,
    ),
    (
        "minimax:api-key-cn",
        MiniMaxAdapter,
        lambda adapter: str(adapter._client.base_url) == "https://api.minimaxi.com/v1/",
    ),
    ("mistral:api-key", MistralAdapter, lambda adapter: True),
    ("nous:api-key", NousAdapter, lambda adapter: True),
    (
        "stepfun:step-plan",
        StepFunAdapter,
        lambda adapter: (
            adapter._connection_mode == "step_plan"
            and str(adapter._client.base_url) == "https://api.stepfun.com/step_plan/v1/"
        ),
    ),
    (
        "kimi:coding-plan",
        KimiAdapter,
        lambda adapter: adapter._connection_mode == KIMI_CODING_MODE,
    ),
    ("opencode-go:api-key", OpenCodeGoAdapter, lambda adapter: True),
    ("opencode-zen:api-key", OpenCodeZenAdapter, lambda adapter: True),
    ("ollama-cloud:api-key", OllamaCloudAdapter, lambda adapter: True),
    ("xai:api-key", XAIAdapter, lambda adapter: True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connection_id", "adapter_type", "wired"),
    _ADAPTER_WIRING,
    ids=[connection_id for connection_id, _, _ in _ADAPTER_WIRING],
)
async def test_get_adapter_builds_the_bundled_adapter_for_each_api_key_connection(
    shared_runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    connection_id: str,
    adapter_type: type,
    wired: Callable[[Any], bool],
) -> None:
    provider_id, local_id = connection_id.split(":", 1)
    credential_key = (
        shared_runtime.providers.get(provider_id).get_connection(local_id).auth.credential_key
    )
    assert credential_key
    monkeypatch.setenv(credential_key, f"{provider_id}-token")

    adapter = shared_runtime.get_adapter(ConnectionRef(provider_id, connection_id))

    assert type(adapter) is adapter_type
    assert await adapter._token_getter() == f"{provider_id}-token"  # type: ignore[attr-defined]
    assert adapter._model_lookup is not None  # type: ignore[attr-defined]
    assert wired(adapter)


def test_get_adapter_scopes_model_lookup_wire_profiles_and_replay_to_its_connection(
    shared_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-token")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-token")

    anthropic = shared_runtime.get_adapter(ConnectionRef("anthropic", "anthropic:api-key"))
    lookup = anthropic._model_lookup
    assert lookup is not None
    assert lookup("claude-sonnet-4-6") == shared_runtime.models.get(
        "anthropic", "claude-sonnet-4-6"
    )
    # An OpenRouter-only model id is invisible to the Anthropic adapter.
    assert lookup("anthropic/claude-sonnet-4") is None
    profile = anthropic.wire_profile("claude-sonnet-4-6::api-key")
    assert (profile.provider_id, profile.connection_id, profile.model_id) == (
        "anthropic",
        "api-key",
        "claude-sonnet-4-6",
    )
    assert profile.protocol == "messages"
    assert profile.known_model
    assert anthropic.wire_profile("unknown-model").known_model is False
    # The Runtime reports the profile requests use, learned facts included.
    assert shared_runtime.wire_profile("anthropic", "api-key", "claude-sonnet-4-6") is profile
    assert shared_runtime.wire_status("anthropic", "api-key", "claude-sonnet-4-6") == (
        profile.status,
        profile.verification,
    )
    assert shared_runtime.learned_wire_facts("anthropic", "api-key", "claude-sonnet-4-6").is_empty()

    cloud = shared_runtime.get_adapter(ConnectionRef("ollama-cloud", "ollama-cloud:api-key"))
    # The wire file's default scope applies to every Model no rule narrows.
    assert cloud.reasoning_replay_policy("unprofiled-model") == "full_history"
    assert cloud.reasoning_replay_policy("glm-5.2") == "full_history"
    # Rules narrow exact Models.
    assert cloud.reasoning_replay_policy("minimax-m3") == "none"
    assert cloud.reasoning_replay_policy("kimi-k2.6") == "current_run"


@pytest.mark.parametrize(
    ("model", "connection_id"),
    [
        pytest.param("openai/gpt-5.2", "openai:api-key", id="first-usable"),
        pytest.param("openai/gpt-5.2::api-key:work", "openai:api-key:work", id="pinned"),
        pytest.param("anthropic/claude-sonnet-4-6", None, id="no-usable-connection"),
    ],
)
def test_status_describes_the_wire_profile_of_the_connection_chat_resolves(
    shared_runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    connection_id: str | None,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-default")
    monkeypatch.setenv("OPENAI_API_KEY__WORK", "sk-work")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    agent = SimpleNamespace(model=model, thinking_effort="high")
    described = shared_runtime.describe_agent_wire_profile(agent)
    reasoning = shared_runtime.describe_agent_reasoning_render(agent)

    if connection_id is None:
        assert (described, reasoning) == (None, None)
        return
    assert described is not None
    profile = shared_runtime.wire_profile("openai", "api-key", "gpt-5.2")
    assert (described.connection_id, described.status) == (connection_id, profile.status)
    assert described.learned.is_empty()
    # The thinking-effort report describes the same Connection's profile.
    assert reasoning == describe_profile_reasoning(profile, "high")


@pytest.mark.asyncio
async def test_api_key_accounts_resolve_their_suffixed_credentials(
    shared_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-default")
    monkeypatch.setenv("OPENAI_API_KEY__WORK", "sk-work")

    adapter = shared_runtime.get_adapter(ConnectionRef("openai", "openai:api-key:work"))
    getter = shared_runtime.get_connection_token_getter(ConnectionRef("openai", "openai:api-key"))

    assert await adapter._token_getter() == "sk-work"  # type: ignore[attr-defined]
    assert isinstance(getter, StaticTokenGetter)
    assert await getter() == "sk-default"
    with pytest.raises(ConfigError, match="missing"):
        shared_runtime.get_adapter(ConnectionRef("openai", "openai:api-key:missing"))


def test_get_adapter_rejects_unusable_connections(
    shared_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        shared_runtime.get_adapter(ConnectionRef("openai", "openai:api-key"))

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(ConfigError):
        shared_runtime.get_adapter(ConnectionRef("openai", "openai:missing"))
    with pytest.raises(ConfigError):
        shared_runtime.provider_credentials.has_credentials("openai", "openai:missing")
    with pytest.raises(KeyError, match="nonexistent"):
        shared_runtime.get_adapter(ConnectionRef("nonexistent", "nonexistent:api-key"))
    # Keyless local Connections stay disabled until the user opts in.
    with pytest.raises(ConfigError, match="disabled"):
        shared_runtime.get_adapter(ConnectionRef("ollama", "ollama:local"))
    monkeypatch.delitem(ADAPTER_TYPES, "openai")
    with pytest.raises(ConfigError, match="Unknown adapter type"):
        shared_runtime.get_adapter(ConnectionRef("openai", "openai:api-key"))


@pytest.mark.asyncio
async def test_get_adapter_reads_the_live_token_store_and_routing_settings(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    copilot = ConnectionRef("github-copilot", "github-copilot:oauth")
    # Without any stored token there is no usable OAuth Account.
    with pytest.raises(ConfigError):
        runtime.get_adapter(copilot)
    assert runtime.get_connection_token_extra(copilot) == {}

    extra = {
        "github_oauth_token": "gho_example",
        "copilot_api_endpoint": "https://api.enterprise.githubcopilot.com",
    }
    runtime.token_store.save(
        "github-copilot",
        "oauth",
        OAuthToken(access_token="work-token", extra=extra),
        account_id="work",
    )

    # Without an Account part, the first usable Account is bound, and its token
    # extra routes the adapter to the Account-specific exchange endpoint.
    adapter = runtime.get_adapter(copilot)
    assert isinstance(adapter, GitHubCopilotAdapter)
    assert await adapter._token_getter() == "work-token"  # type: ignore[attr-defined]
    assert str(adapter._client.base_url) == extra["copilot_api_endpoint"]  # type: ignore[attr-defined]
    assert runtime.get_connection_token_extra(copilot) == extra
    pinned = runtime.get_adapter(ConnectionRef("github-copilot", "github-copilot:oauth:work"))
    assert await pinned._token_getter() == "work-token"  # type: ignore[attr-defined]
    # An explicitly pinned Account is used as-is so mid-flight logins work; the
    # missing token surfaces at call time.
    pending = runtime.get_adapter(ConnectionRef("github-copilot", "github-copilot:oauth:pending"))
    with pytest.raises(ProviderAuthError):
        await pending._token_getter()  # type: ignore[attr-defined]

    runtime.token_store.save("openai", "subscription", OAuthToken(access_token="oauth-access"))
    subscription = ConnectionRef("openai", "openai:subscription")
    getter = runtime.get_connection_token_getter(subscription)
    assert isinstance(getter, OAuthTokenGetter)
    assert await getter() == "oauth-access"
    codex = runtime.get_adapter(subscription)
    assert isinstance(codex, OpenAIAdapter)
    assert codex._connection_mode == CODEX_RESPONSES_MODE  # type: ignore[attr-defined]

    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-token")
    routing = {
        "mode": "allowed",
        "providers": ["anthropic"],
        "blocked": ["deepinfra"],
        "allow_fallbacks": False,
    }
    runtime.storage.update_settings_sections(
        {"providers": {"openrouter": {"routing": {"default": routing, "models": {}}}}}
    )
    openrouter = runtime.get_adapter(ConnectionRef("openrouter", "openrouter:api-key"))
    assert openrouter._routing["default"] == routing  # type: ignore[attr-defined]
