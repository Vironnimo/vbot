"""Tests for discovery projection."""

from __future__ import annotations

from dataclasses import replace

from .discovery_test_support import (
    API_KEY,
    FIXTURES_DIR,
    GITHUB_COPILOT_MODELS_URL,
    OPENCODE_GO_MODELS_URL,
    AuthConfig,
    ConnectionConfig,
    ModelRegistry,
    Path,
    ProviderConfig,
    httpx,
    json,
    pytest,
    raw_openrouter_model,
    refresh_models,
    respx,
)
from .discovery_test_support import _clear_registry_cache as _clear_registry_cache
from .discovery_test_support import github_copilot_config as github_copilot_config
from .discovery_test_support import (
    openai_subscription_connection_config as openai_subscription_connection_config,
)
from .discovery_test_support import opencode_go_config as opencode_go_config
from .discovery_test_support import openrouter_config as openrouter_config


class TestRefreshModels:
    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_supports_opencode_go_discovery_adapter(
        self,
        tmp_path: Path,
        opencode_go_config: ProviderConfig,
    ):
        resources_dir = tmp_path / "resources"
        route = respx.get(OPENCODE_GO_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        raw_openrouter_model(
                            model_id="deepseek/deepseek-r1",
                            name="DeepSeek R1",
                        )
                    ]
                },
            )
        )

        result = await refresh_models(opencode_go_config, API_KEY, resources_dir)

        registry = ModelRegistry.load(resources_dir)
        model = registry.get("opencode-go", "deepseek/deepseek-r1")
        assert result["provider_id"] == "opencode-go"
        assert result["model_count"] == 1
        assert model.name == "DeepSeek R1"
        assert route.calls.last.request.headers["Authorization"] == f"Bearer {API_KEY}"

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_keeps_catalog_exclusions_only_in_raw_inspection_dump(
        self,
        tmp_path: Path,
        opencode_go_config: ProviderConfig,
    ) -> None:
        resources_dir = tmp_path / "resources"
        config = replace(
            opencode_go_config,
            catalog_exclusions=frozenset({"broken-preview"}),
        )
        respx.get(OPENCODE_GO_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        raw_openrouter_model(model_id="working", name="Working"),
                        raw_openrouter_model(model_id="broken-preview", name="Broken"),
                    ]
                },
            )
        )

        result = await refresh_models(config, API_KEY, resources_dir)

        raw = json.loads(
            (resources_dir / "models" / "opencode-go.raw.json").read_text(encoding="utf-8")
        )
        projected = json.loads(
            (resources_dir / "models" / "opencode-go.json").read_text(encoding="utf-8")
        )
        assert result["model_count"] == 1
        assert {entry["id"] for entry in raw["raw_response"]["data"]} == {
            "working",
            "broken-preview",
        }
        assert set(projected["models"]) == {"working"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_uses_tolerant_normalizer_for_github_copilot(
        self,
        tmp_path: Path,
        github_copilot_config: ProviderConfig,
    ):
        raw_fixture = json.loads(
            (FIXTURES_DIR / "github_copilot_models_raw.json").read_text(encoding="utf-8")
        )
        route = respx.get(GITHUB_COPILOT_MODELS_URL).mock(
            return_value=httpx.Response(200, json=raw_fixture)
        )

        result = await refresh_models(github_copilot_config, API_KEY, tmp_path / "resources")

        registry = ModelRegistry.load(tmp_path / "resources")
        gpt_4o = registry.get("github-copilot", "gpt-4o")
        gemini_2_5_pro = registry.get("github-copilot", "gemini-2.5-pro")
        output_data = json.loads(
            (tmp_path / "resources" / "models" / "github-copilot.json").read_text(encoding="utf-8")
        )
        raw_output_path = tmp_path / "resources" / "models" / "github-copilot.raw.json"
        raw_output_data = json.loads(raw_output_path.read_text(encoding="utf-8"))
        gpt_5_mini_data = output_data["models"]["gpt-5-mini"]
        assert result["model_count"] == 5
        assert raw_output_path.exists()
        assert raw_output_data["raw_response"] == raw_fixture
        assert gpt_4o.capabilities.vision is True
        assert gpt_4o.context_window == 128000
        assert gpt_4o.max_output_tokens == 4096
        assert gemini_2_5_pro.capabilities.reasoning.supported is True
        assert gpt_5_mini_data["metadata"]["github_copilot"] == {
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
        assert "policy" not in gpt_5_mini_data["metadata"]["github_copilot"]
        assert registry.get("github-copilot", "gpt-5-mini").metadata["github_copilot"][
            "supported_endpoints"
        ] == ("/chat/completions", "/responses", "ws:/responses")
        assert route.calls.last.request.headers["Authorization"] == f"Bearer {API_KEY}"
        assert route.calls.last.request.headers["Copilot-Integration-Id"] == "vbot"

    @respx.mock
    @pytest.mark.asyncio
    async def test_copilot_discovery_keeps_hidden_entries_only_in_raw_audit(
        self,
        tmp_path: Path,
        github_copilot_config: ProviderConfig,
    ) -> None:
        raw_fixture = {
            "data": [
                {
                    "id": "visible-chat",
                    "name": "Visible Chat",
                    "model_picker_enabled": True,
                    "supported_endpoints": ["/chat/completions"],
                    "capabilities": {"type": "chat", "supports": {}},
                },
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
        }
        respx.get(GITHUB_COPILOT_MODELS_URL).mock(
            return_value=httpx.Response(200, json=raw_fixture)
        )

        result = await refresh_models(
            github_copilot_config,
            API_KEY,
            tmp_path / "resources",
        )

        generated = json.loads(
            (tmp_path / "resources" / "models" / "github-copilot.json").read_text(encoding="utf-8")
        )
        raw = json.loads(
            (tmp_path / "resources" / "models" / "github-copilot.raw.json").read_text(
                encoding="utf-8"
            )
        )
        assert result["model_count"] == 1
        assert set(generated["models"]) == {"visible-chat"}
        assert {entry["id"] for entry in raw["raw_response"]["data"]} == {
            "visible-chat",
            "hidden-chat",
            "embedding-only",
            "websocket-only",
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_nous_discovery_uses_selected_connection_and_keeps_skips_in_raw(
        self,
        tmp_path: Path,
    ) -> None:
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
            config,
            "nous-oauth-jwt",
            tmp_path / "resources",
            credential_connection=connection,
        )

        generated = json.loads(
            (tmp_path / "resources" / "models" / "nous.json").read_text(encoding="utf-8")
        )
        raw = json.loads(
            (tmp_path / "resources" / "models" / "nous.raw.json").read_text(encoding="utf-8")
        )
        assert result["model_count"] == 1
        assert set(generated["models"]) == {"vendor/agent-model"}
        assert generated["models"]["vendor/agent-model"]["connections"] == ["subscription"]
        assert generated["models"]["vendor/agent-model"]["max_output_tokens"] == 32000
        assert {entry["id"] for entry in raw["raw_response"]["data"]} == {
            "vendor/agent-model",
            "Hermes-4-70B",
        }
        assert route.calls.last.request.headers["authorization"] == "Bearer nous-oauth-jwt"

    @respx.mock
    @pytest.mark.asyncio
    async def test_stepfun_discovery_preserves_other_connection_memberships_and_raw(
        self,
        tmp_path: Path,
    ) -> None:
        direct = ConnectionConfig(
            id="direct-api",
            type="api_key",
            label="Direct API",
            mode="direct_api",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
                credential_key="STEPFUN_DIRECT_API_KEY",
            ),
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
        resources_dir = tmp_path / "resources"
        models_dir = resources_dir / "models"
        models_dir.mkdir(parents=True)
        existing = json.loads(
            (Path("resources") / "models" / "stepfun.json").read_text(encoding="utf-8")
        )
        (models_dir / "stepfun.json").write_text(
            json.dumps(existing),
            encoding="utf-8",
        )
        route = respx.get("https://api.stepfun.com/v1/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "step-3.7-flash", "name": "Step 3.7 Flash"},
                        {"id": "step-router-v1", "name": "Step Router V1"},
                        {"id": "stepaudio-2.5-chat", "name": "StepAudio 2.5 Chat"},
                    ]
                },
            )
        )

        result = await refresh_models(
            config,
            "direct-token",
            resources_dir,
            credential_connection=direct,
        )

        generated = json.loads((models_dir / "stepfun.json").read_text(encoding="utf-8"))
        raw = json.loads((models_dir / "stepfun.raw.json").read_text(encoding="utf-8"))
        assert result["model_count"] == 4
        assert generated["models"]["step-3.7-flash"]["connections"] == [
            "step-plan",
            "direct-api",
        ]
        assert generated["models"]["step-3.5-flash"]["connections"] == ["step-plan"]
        assert generated["models"]["step-router-v1"]["connections"] == ["step-plan"]
        assert {entry["id"] for entry in raw["raw_response"]["data"]} == {
            "step-3.7-flash",
            "step-router-v1",
            "stepaudio-2.5-chat",
        }
        assert route.calls.last.request.headers["authorization"] == "Bearer direct-token"
