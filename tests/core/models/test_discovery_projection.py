"""Models: Provider-specific catalog projections through ``refresh_models``.

Each test drives one Provider's discovery hooks (supplementary and task
catalogs, per-model enrichment, Connection filters, models.dev enrichment)
end to end: HTTP catalog -> raw dump and projection -> ``ModelRegistry``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.models.discovery import refresh_models
from core.models.models import ModelRegistry
from core.models.models_dev import ModelsDevCatalog
from core.providers import OpenCodeZenAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

from .discovery_test_support import (
    API_KEY,
    FIXTURES_DIR,
    GITHUB_COPILOT_MODELS_URL,
    OPENCODE_GO_MODELS_URL,
    OPENROUTER_IMAGE_MODELS_URL,
    OPENROUTER_MODELS_URL,
    OPENROUTER_VIDEO_MODELS_URL,
    api_key_connection,
    github_copilot_config,
    keyless_connection,
    model_data,
    opencode_go_config,
    openrouter_config,
    raw_openrouter_model,
    read_models_file,
)


@respx.mock
@pytest.mark.asyncio
async def test_opencode_zen_writes_admitted_models_and_merges_connections(tmp_path: Path) -> None:
    """Zen admits a Model its protocol hint or a reviewed rule routes, unless free or retired."""
    resources_dir = tmp_path / "resources"
    config = ProviderConfig(
        id="opencode-zen",
        name="OpenCode Zen",
        adapter="opencode_zen",
        base_url="https://opencode.ai/zen/v1",
        connections=[
            api_key_connection("OPENCODE_API_KEY", models_endpoint="/models"),
            ConnectionConfig(
                id="account",
                type="oauth",
                label="OpenCode Account",
                auth=AuthConfig(header="Authorization", prefix="Bearer "),
                models_endpoint="/models",
            ),
        ],
        defaults={"max_tokens": 8192},
        models_endpoint="/models",
        models_dev_id="opencode",
        catalog_exclusions=frozenset({"glm-5"}),
    )
    modalities = {"input": ["text", "image", "video", "audio", "pdf"], "output": ["text"]}

    def section_model(model_id: str, npm: str | None = None) -> dict[str, Any]:
        return {"id": model_id, "name": model_id, **({"provider": {"npm": npm}} if npm else {})}

    catalog = ModelsDevCatalog(
        {
            "models": {
                "google/gemini-3.5-flash": {
                    "id": "google/gemini-3.5-flash",
                    "name": "Gemini 3.5 Flash",
                    "modalities": modalities,
                    "reasoning": True,
                }
            },
            "providers": {
                "opencode": {
                    "id": "opencode",
                    "name": "OpenCode",
                    # The section's default package is no Model's protocol hint.
                    "npm": "@ai-sdk/openai-compatible",
                    "models": {
                        "gemini-3.5-flash": {
                            **section_model("gemini-3.5-flash", "@ai-sdk/google"),
                            "name": "Gemini 3.5 Flash",
                            "family": "gemini",
                            "limit": {"context": 1_048_576, "output": 65_536},
                            "modalities": modalities,
                            "reasoning": True,
                            "tool_call": True,
                            "reasoning_options": [
                                {"type": "effort", "values": ["minimal", "low", "medium", "high"]}
                            ],
                        },
                        "claude-future-6": section_model("claude-future-6", "@ai-sdk/anthropic"),
                        "unreviewed-future-model": section_model("unreviewed-future-model"),
                        "muse-spark-1.3-contributor-free": section_model(
                            "muse-spark-1.3-contributor-free", "@ai-sdk/openai"
                        ),
                    },
                }
            },
        }
    )
    live_ids = [
        "gemini-3.5-flash",
        "claude-future-6",
        "glm-5.1",
        "glm-5",
        "unreviewed-future-model",
        "muse-spark-1.3-contributor-free",
        "mimo-v2.6-flash-free",
    ]
    route = respx.get("https://opencode.ai/zen/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": item} for item in live_ids]})
    )

    counts = [
        (
            await refresh_models(
                config,
                credential,
                resources_dir,
                credential_connection=config.get_connection(connection_id),
                models_dev_catalog=catalog,
            )
        )["model_count"]
        for connection_id, credential in (("api-key", "api-key-secret"), ("account", "token"))
    ]

    written = read_models_file(resources_dir, "opencode-zen.json")["models"]
    gemini = written["gemini-3.5-flash"]
    assert counts == [3, 3]
    assert route.call_count == 2
    assert set(written) == {"gemini-3.5-flash", "claude-future-6", "glm-5.1"}
    assert gemini["connections"] == ["api-key", "account"]
    assert (gemini["context_window"], gemini["max_output_tokens"]) == (1_048_576, 65_536)
    assert gemini["capabilities"]["input_modalities"] == modalities["input"]
    assert written["claude-future-6"]["metadata"] == {"opencode_zen": {"npm": "@ai-sdk/anthropic"}}
    assert "metadata" not in written["glm-5.1"]

    registry = ModelRegistry.load(resources_dir)
    adapter = OpenCodeZenAdapter(
        config, "key", model_lookup=lambda model_id: registry.get("opencode-zen", model_id)
    )
    profiles = {model_id: adapter.wire_profile(model_id) for model_id in written}
    assert {
        model_id: (profile.protocol, profile.admission.state)
        for model_id, profile in profiles.items()
    } == {
        "gemini-3.5-flash": ("gemini", "available"),
        "claude-future-6": ("messages", "available"),
        "glm-5.1": ("chat_completions", "available"),
    }
    assert profiles["claude-future-6"].provenance["protocol"] == "catalog_hint"
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_opencode_go_projects_its_catalog_without_excluded_models(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    config = opencode_go_config(catalog_exclusions=frozenset({"broken-preview"}))
    route = respx.get(OPENCODE_GO_MODELS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    raw_openrouter_model(model_id="deepseek/deepseek-r1", name="DeepSeek R1"),
                    raw_openrouter_model(model_id="broken-preview", name="Broken"),
                ]
            },
        )
    )

    result = await refresh_models(config, API_KEY, resources_dir)

    assert result["model_count"] == 1
    assert set(read_models_file(resources_dir, "opencode-go.json")["models"]) == {
        "deepseek/deepseek-r1"
    }
    model = ModelRegistry.load(resources_dir).get("opencode-go", "deepseek/deepseek-r1")
    assert model.name == "DeepSeek R1"
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {API_KEY}"


@respx.mock
@pytest.mark.asyncio
async def test_github_copilot_projects_selectable_models_and_their_metadata(
    tmp_path: Path,
) -> None:
    resources_dir = tmp_path / "resources"
    payload = json.loads(
        (FIXTURES_DIR / "github_copilot_models_raw.json").read_text(encoding="utf-8")
    )
    selectable = {entry["id"] for entry in payload["data"]}
    # Hidden, non-chat and websocket-only entries are not projected.
    payload["data"] += [
        {
            "id": "hidden-chat",
            "name": "Hidden Chat",
            "model_picker_enabled": False,
            "capabilities": {"type": "chat", "supports": {}},
        },
        {
            "id": "embedding-only",
            "name": "Embedding Only",
            "capabilities": {"type": "embeddings", "supports": {}},
        },
        {
            "id": "websocket-only",
            "name": "Websocket Only",
            "supported_endpoints": ["ws:/responses"],
            "capabilities": {"type": "chat", "supports": {}},
        },
    ]
    route = respx.get(GITHUB_COPILOT_MODELS_URL).mock(
        return_value=httpx.Response(200, json=payload)
    )

    result = await refresh_models(github_copilot_config(), API_KEY, resources_dir)

    written = read_models_file(resources_dir, "github-copilot.json")["models"]
    assert result["model_count"] == len(selectable)
    assert set(written) == selectable
    assert written["gpt-5-mini"]["metadata"]["github_copilot"] == {
        "family": "gpt-5-mini",
        "parallel_tool_calls": True,
        "reasoning_efforts": ["low", "medium", "high"],
        "streaming": True,
        "structured_outputs": True,
        "supported_endpoints": ["/chat/completions", "/responses", "ws:/responses"],
        "tool_calls": True,
        "vendor": "Azure OpenAI",
        "version": "gpt-5-mini",
    }
    registry = ModelRegistry.load(resources_dir)
    gpt_4o = registry.get("github-copilot", "gpt-4o")
    assert (gpt_4o.capabilities.vision, gpt_4o.context_window, gpt_4o.max_output_tokens) == (
        True,
        128000,
        4096,
    )
    assert registry.get("github-copilot", "gemini-2.5-pro").capabilities.reasoning.supported
    assert registry.get("github-copilot", "gpt-5-mini").metadata["github_copilot"][
        "supported_endpoints"
    ] == ("/chat/completions", "/responses", "ws:/responses")
    headers = route.calls.last.request.headers
    assert headers["Authorization"] == f"Bearer {API_KEY}"
    assert headers["Copilot-Integration-Id"] == "vbot"


@respx.mock
@pytest.mark.asyncio
async def test_nous_projects_agent_models_for_the_selected_connection(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    connection = ConnectionConfig(
        id="subscription",
        type="oauth",
        label="Portal Login",
        auth=AuthConfig(header="Authorization", prefix="Bearer "),
        models_endpoint="/models",
    )
    config = ProviderConfig(
        id="nous",
        name="Nous Portal",
        adapter="nous",
        base_url="https://inference-api.nousresearch.com/v1",
        connections=[connection],
        defaults={"max_tokens": 32000},
    )
    route = respx.get("https://inference-api.nousresearch.com/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "vendor/agent-model",
                        "name": "Agent Model",
                        "supported_parameters": ["tools", "reasoning"],
                        "context_length": 200000,
                        "top_provider": {"max_completion_tokens": 64000},
                    },
                    {"id": "Hermes-4-70B", "name": "Hermes 4 70B"},
                ]
            },
        )
    )

    result = await refresh_models(
        config, "nous-oauth-jwt", resources_dir, credential_connection=connection
    )

    written = read_models_file(resources_dir, "nous.json")["models"]
    assert result["model_count"] == 1
    assert set(written) == {"vendor/agent-model"}
    assert written["vendor/agent-model"]["connections"] == ["subscription"]
    assert written["vendor/agent-model"]["max_output_tokens"] == 32000
    assert route.calls.last.request.headers["authorization"] == "Bearer nous-oauth-jwt"


@respx.mock
@pytest.mark.asyncio
async def test_stepfun_direct_refresh_keeps_other_connection_memberships(tmp_path: Path) -> None:
    """A refresh replaces only the refreshed Connection's memberships."""

    resources_dir = tmp_path / "resources"
    models_dir = resources_dir / "models"
    models_dir.mkdir(parents=True)
    (models_dir / "stepfun.json").write_text(
        json.dumps(
            {
                "provider_id": "stepfun",
                "models": {
                    "step-3.7-flash": model_data() | {"connections": ["step-plan"]},
                    "step-3.5-flash": model_data() | {"connections": ["step-plan", "direct-api"]},
                    "step-router-v1": model_data() | {"connections": ["step-plan"]},
                    "retired": model_data() | {"connections": ["direct-api"]},
                },
            }
        ),
        encoding="utf-8",
    )
    direct = api_key_connection(
        "STEPFUN_DIRECT_API_KEY",
        id="direct-api",
        label="Direct API",
        mode="direct_api",
        models_endpoint="/models",
    )
    config = ProviderConfig(
        id="stepfun",
        name="StepFun",
        adapter="stepfun",
        base_url="https://api.stepfun.com/v1",
        connections=[direct],
        defaults={"temperature": 0.5},
        context_window=256000,
    )
    live_ids = ["step-3.7-flash", "step-router-v1", "stepaudio-2.5-chat"]
    route = respx.get("https://api.stepfun.com/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": item} for item in live_ids]})
    )

    result = await refresh_models(
        config, "direct-token", resources_dir, credential_connection=direct
    )

    written = read_models_file(resources_dir, "stepfun.json")["models"]
    assert result["model_count"] == 3
    assert {model_id: data["connections"] for model_id, data in written.items()} == {
        "step-3.7-flash": ["step-plan", "direct-api"],
        "step-3.5-flash": ["step-plan"],
        "step-router-v1": ["step-plan"],
    }
    assert route.calls.last.request.headers["authorization"] == "Bearer direct-token"


@respx.mock
@pytest.mark.asyncio
async def test_xai_subscription_uses_plain_oauth_discovery(tmp_path: Path) -> None:
    """xAI inherits the OpenAI catalog normalizer but not Codex account routing:
    its token carries no ChatGPT account claim and needs no ``client_version``."""

    resources_dir = tmp_path / "resources"
    subscription = ConnectionConfig(
        id="subscription",
        type="oauth",
        label="SuperGrok Login (Subscription)",
        auth=AuthConfig(header="Authorization", prefix="Bearer "),
        models_endpoint="/language-models",
    )
    config = ProviderConfig(
        id="xai",
        name="xAI",
        adapter="xai",
        base_url="https://api.x.ai/v1",
        connections=[subscription],
        defaults={"max_tokens": 8192},
    )
    route = respx.get("https://api.x.ai/v1/language-models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "grok-4.5",
                        "name": "Grok 4.5",
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                        "context_window": 500000,
                    }
                ]
            },
        )
    )

    result = await refresh_models(
        config, "xai-token-without-chatgpt-claim", resources_dir, credential_connection=subscription
    )

    written = read_models_file(resources_dir, "xai.json")["models"]
    request = route.calls.last.request
    assert result["model_count"] == 1
    assert (written["grok-4.5"]["name"], written["grok-4.5"]["connections"]) == (
        "Grok 4.5",
        ["subscription"],
    )
    assert request.headers["Authorization"] == "Bearer xai-token-without-chatgpt-claim"
    assert "chatgpt-account-id" not in request.headers
    assert "client_version" not in request.url.params


@respx.mock
@pytest.mark.asyncio
async def test_openai_platform_discovers_only_its_embedding_models(tmp_path: Path) -> None:
    """The Platform listing carries bare ids; chat, speech and image Models stay
    curated, so only ``text-embedding-*`` ids become text embedding Models."""

    resources_dir = tmp_path / "resources"
    models_dir = resources_dir / "models"
    models_dir.mkdir(parents=True)
    (models_dir / "openai.json").write_text(
        json.dumps(
            {
                "provider_id": "openai",
                "models": {"gpt-5-codex": model_data() | {"connections": ["subscription"]}},
            }
        ),
        encoding="utf-8",
    )
    platform = api_key_connection("OPENAI_API_KEY", models_endpoint="/models")
    config = ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai",
        base_url="https://api.openai.com/v1",
        connections=[platform],
    )
    live_ids = ["gpt-5.2", "whisper-1", "text-embedding-3-small", "text-embedding-ada-002"]
    route = respx.get("https://api.openai.com/v1/models").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": item, "object": "model"} for item in live_ids]}
        )
    )

    await refresh_models(config, API_KEY, resources_dir, credential_connection=platform)

    written = read_models_file(resources_dir, "openai.json")["models"]
    assert {model_id: data["connections"] for model_id, data in written.items()} == {
        "gpt-5-codex": ["subscription"],
        "text-embedding-3-small": ["api-key"],
        "text-embedding-ada-002": ["api-key"],
    }
    registry = ModelRegistry.load(resources_dir, custom_providers={})
    small = registry.get("openai", "text-embedding-3-small")
    assert small.capabilities.task_types == ("text_embedding",)
    assert small.capabilities.supported_parameters == ("dimensions",)
    assert registry.get("openai", "text-embedding-ada-002").capabilities.supported_parameters == ()
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert "chatgpt-account-id" not in request.headers
    assert "client_version" not in request.url.params


def _ollama_config(connection: ConnectionConfig, **changes: Any) -> ProviderConfig:
    return ProviderConfig(
        **{
            "id": "ollama",
            "name": "Ollama",
            "adapter": "ollama",
            "base_url": "http://localhost:11434",
            "connections": [connection],
            "models_endpoint": "/api/tags",
        }
        | changes
    )


@respx.mock
@pytest.mark.asyncio
async def test_ollama_enriches_tags_through_api_show(tmp_path: Path) -> None:
    connection = keyless_connection()
    respx.get("http://localhost:11434/api/tags").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "ministral-3:8b",
                        "model": "ministral-3:8b",
                        "details": {"family": "mistral3"},
                    },
                    {
                        "name": "kimi-k2.6:cloud",
                        "model": "kimi-k2.6:cloud",
                        "remote_model": "kimi-k2.6",
                        "remote_host": "https://ollama.com:443",
                        "details": {"family": "kimi"},
                    },
                ]
            },
        )
    )
    show_responses = {
        "ministral-3:8b": {
            "capabilities": ["completion", "vision", "tools"],
            "model_info": {
                "general.architecture": "mistral3",
                "mistral3.context_length": 262144,
                "mistral3.rope.scaling.original_context_length": 16384,
            },
        },
        "kimi-k2.6:cloud": {"capabilities": ["completion", "tools", "thinking"], "model_info": {}},
    }
    show_route = respx.post("http://localhost:11434/api/show").mock(
        side_effect=lambda request: httpx.Response(
            200, json=show_responses[json.loads(request.content)["model"]]
        )
    )

    result = await refresh_models(
        _ollama_config(connection), "", tmp_path / "resources", credential_connection=connection
    )

    registry = ModelRegistry.load(tmp_path / "resources")
    local = registry.get("ollama", "ministral-3:8b")
    cloud = registry.get("ollama", "kimi-k2.6:cloud")
    assert result["model_count"] == 2
    assert show_route.call_count == 2
    assert (local.capabilities.tools, local.capabilities.vision) == (True, True)
    assert local.context_window == 262144
    assert local.metadata["ollama"] == {"local": True}
    assert local.connections == ("local",)
    assert cloud.capabilities.reasoning.supported is True
    assert cloud.metadata["ollama"] == {"remote": True}


@respx.mock
@pytest.mark.asyncio
async def test_direct_ollama_cloud_catalog_is_remote_and_public(tmp_path: Path) -> None:
    connection = api_key_connection(
        "OLLAMA_API_KEY", mode="cloud", catalog_requires_credentials=False
    )
    config = _ollama_config(connection, id="ollama-cloud", base_url="https://ollama.com")
    tags_route = respx.get("https://ollama.com/api/tags").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [{"name": "glm-5.1", "model": "glm-5.1", "details": {"family": "glm5.1"}}]
            },
        )
    )
    show_route = respx.post("https://ollama.com/api/show").mock(
        return_value=httpx.Response(
            200,
            json={
                "capabilities": ["completion", "tools", "thinking"],
                "model_info": {"general.architecture": "glm5.1", "glm5.1.context_length": 202752},
            },
        )
    )

    result = await refresh_models(
        config, "", tmp_path / "resources", credential_connection=connection
    )

    model = ModelRegistry.load(tmp_path / "resources").get("ollama-cloud", "glm-5.1")
    assert result["model_count"] == 1
    assert model.metadata["ollama"] == {"remote": True}
    assert model.connections == ("api-key",)
    assert model.context_window == 202752
    assert "Authorization" not in tags_route.calls.last.request.headers
    assert "Authorization" not in show_route.calls.last.request.headers


@respx.mock
@pytest.mark.asyncio
async def test_failed_ollama_enrichment_keeps_the_conservative_catalog(tmp_path: Path) -> None:
    connection = keyless_connection()
    respx.get("http://localhost:11434/api/tags").mock(
        return_value=httpx.Response(
            200, json={"models": [{"model": "ministral-3:8b", "details": {"family": "mistral3"}}]}
        )
    )
    respx.post("http://localhost:11434/api/show").mock(
        return_value=httpx.Response(404, json={"error": "model not found"})
    )

    result = await refresh_models(
        _ollama_config(connection), "", tmp_path / "resources", credential_connection=connection
    )

    model = ModelRegistry.load(tmp_path / "resources").get("ollama", "ministral-3:8b")
    assert result["model_count"] == 1
    assert (model.capabilities.tools, model.context_window) == (False, None)


def _mock_openrouter_catalogs(
    by_output_modality: dict[str | None, httpx.Response],
) -> None:
    """Answer the main catalog (``None``) and each supplementary ``output_modalities``
    fetch; a supplementary modality without an entry returns an empty list."""

    def handler(request: httpx.Request) -> httpx.Response:
        modality = request.url.params.get("output_modalities")
        default = httpx.Response(200, json={"data": []})
        return by_output_modality.get(modality, default)

    respx.get(OPENROUTER_MODELS_URL).mock(side_effect=handler)


def _catalog(*entries: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"data": list(entries)})


def _audio_model(model_id: str, output: str) -> dict[str, Any]:
    input_modalities = ["audio"] if output == "transcription" else ["text"]
    return raw_openrouter_model(
        model_id=model_id,
        name=model_id,
        input_modalities=input_modalities,
        output_modalities=[output],
    )


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_merges_supplementary_and_task_catalogs(tmp_path: Path) -> None:
    """Supplementary fetches add dedicated audio Models once; the image task
    catalog enriches a chat-catalog Model and adds an image-API-only Model."""

    resources_dir = tmp_path / "resources"
    gpt_audio = raw_openrouter_model(
        model_id="openai/gpt-audio", name="GPT Audio", output_modalities=["text", "audio"]
    )
    _mock_openrouter_catalogs(
        {
            None: _catalog(
                raw_openrouter_model(model_id="openai/gpt-4o", name="GPT-4o"),
                gpt_audio,
                raw_openrouter_model(
                    model_id="recraft/recraft-v3", name="Recraft V3", output_modalities=["image"]
                ),
            ),
            "transcription": _catalog(gpt_audio, _audio_model("openai/whisper-1", "transcription")),
            "speech": _catalog(_audio_model("openai/gpt-4o-mini-tts", "speech")),
        }
    )
    respx.get(OPENROUTER_IMAGE_MODELS_URL).mock(
        return_value=_catalog(
            {
                "id": "recraft/recraft-v3",
                "name": "Recraft: Recraft V3",
                "supported_parameters": {"n": {"type": "range", "min": 1, "max": 6}},
            },
            {
                "id": "future-lab/pixel-marvel",
                "name": "Pixel Marvel",
                "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]},
                "supported_parameters": {
                    "aspect_ratio": {"type": "enum", "values": ["1:1", "16:9"]}
                },
            },
        )
    )
    respx.get(f"{OPENROUTER_IMAGE_MODELS_URL}/recraft/recraft-v3/endpoints").mock(
        return_value=httpx.Response(
            200,
            json={
                "endpoints": [
                    {
                        "provider_slug": "recraft",
                        "allowed_passthrough_parameters": ["style", "controls"],
                    }
                ]
            },
        )
    )
    respx.get(f"{OPENROUTER_IMAGE_MODELS_URL}/future-lab/pixel-marvel/endpoints").mock(
        return_value=httpx.Response(200, json={"endpoints": []})
    )
    respx.get(OPENROUTER_VIDEO_MODELS_URL).mock(return_value=_catalog())

    result = await refresh_models(openrouter_config(), API_KEY, resources_dir)

    written = read_models_file(resources_dir, "openrouter.json")["models"]
    assert result["model_count"] == 6
    assert set(written) == {
        "openai/gpt-4o",
        "openai/gpt-audio",
        "openai/whisper-1",
        "openai/gpt-4o-mini-tts",
        "recraft/recraft-v3",
        "future-lab/pixel-marvel",
    }
    recraft = written["recraft/recraft-v3"]
    assert recraft["name"] == "Recraft V3"
    assert recraft["capabilities"]["task_options"]["image_generation"] == {
        "parameters": {"n": {"type": "range", "min": 1, "max": 6}},
        "passthrough": {"recraft": ["controls", "style"]},
    }
    pixel = ModelRegistry.load(resources_dir).get("openrouter", "future-lab/pixel-marvel")
    assert pixel.name == "Pixel Marvel"
    assert pixel.capabilities.task_options["image_generation"]["parameters"]["aspect_ratio"][
        "values"
    ] == ("1:1", "16:9")


@respx.mock
@pytest.mark.asyncio
async def test_failed_optional_openrouter_catalogs_do_not_block_the_refresh(
    tmp_path: Path,
) -> None:
    resources_dir = tmp_path / "resources"
    rejected = httpx.Response(400, text="Invalid request")
    main = _catalog(raw_openrouter_model(model_id="model-a", name="Model A"))
    by_modality: dict[str | None, httpx.Response] = {"transcription": rejected, None: main}
    _mock_openrouter_catalogs(by_modality)
    respx.get(OPENROUTER_IMAGE_MODELS_URL).mock(return_value=rejected)
    respx.get(OPENROUTER_VIDEO_MODELS_URL).mock(return_value=_catalog())

    result = await refresh_models(openrouter_config(), API_KEY, resources_dir)

    written = read_models_file(resources_dir, "openrouter.json")["models"]
    assert result["model_count"] == 1
    assert "task_options" not in written["model-a"]["capabilities"]
    assert ModelRegistry.load(resources_dir).get("openrouter", "model-a").name == "Model A"
