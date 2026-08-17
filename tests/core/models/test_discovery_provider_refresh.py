"""Provider catalog refresh, persistence, retry, and failure tests."""

from __future__ import annotations

from dataclasses import replace

from .discovery_test_support import (
    _SIMPLE_MODELS_URL,
    API_KEY,
    FIXTURES_DIR,
    GITHUB_COPILOT_MODELS_URL,
    OPENAI_SUBSCRIPTION_MODELS_URL,
    OPENCODE_GO_MODELS_URL,
    OPENROUTER_MODELS_URL,
    STUB_DISCOVERY_MODELS_URL,
    Any,
    AuthConfig,
    Capabilities,
    CatalogEntrySkipped,
    ConnectionConfig,
    Model,
    ModelDiscoveryError,
    ModelRegistry,
    ModelsDevCatalog,
    Path,
    ProviderConfig,
    ReasoningCapabilities,
    _simple_compatible_config,
    discovery_module,
    httpx,
    json,
    jwt_with_openai_account,
    logging,
    mock_openai_codex_package,
    mock_openrouter_image_catalog,
    model_data,
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
    async def test_opencode_zen_discovery_enriches_exact_allowlist_and_merges_connections(
        self,
        tmp_path: Path,
    ) -> None:
        resources_dir = tmp_path / "resources"
        config = ProviderConfig(
            id="opencode-zen",
            name="OpenCode Zen",
            adapter="opencode_zen",
            base_url="https://opencode.ai/zen/v1",
            connections=[
                ConnectionConfig(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=AuthConfig(
                        header="Authorization",
                        prefix="Bearer ",
                        credential_key="OPENCODE_API_KEY",
                    ),
                    models_endpoint="/models",
                ),
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
        catalog = ModelsDevCatalog(
            {
                "models": {
                    "google/gemini-3.5-flash": {
                        "id": "google/gemini-3.5-flash",
                        "name": "Gemini 3.5 Flash",
                        "modalities": {
                            "input": ["text", "image", "video", "audio", "pdf"],
                            "output": ["text"],
                        },
                        "reasoning": True,
                    }
                },
                "providers": {
                    "opencode": {
                        "id": "opencode",
                        "name": "OpenCode",
                        "models": {
                            "gemini-3.5-flash": {
                                "id": "gemini-3.5-flash",
                                "name": "Gemini 3.5 Flash",
                                "family": "gemini",
                                "limit": {"context": 1_048_576, "output": 65_536},
                                "modalities": {
                                    "input": ["text", "image", "video", "audio", "pdf"],
                                    "output": ["text"],
                                },
                                "reasoning": True,
                                "tool_call": True,
                                "reasoning_options": [
                                    {
                                        "type": "effort",
                                        "values": ["minimal", "low", "medium", "high"],
                                    }
                                ],
                            }
                        },
                    }
                },
            }
        )
        route = respx.get("https://opencode.ai/zen/v1/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "gemini-3.5-flash"},
                        {"id": "claude-opus-4-1"},
                        {"id": "glm-5"},
                        {"id": "unreviewed-future-model"},
                    ]
                },
            )
        )

        first = await refresh_models(
            config,
            "api-key-secret",
            resources_dir,
            credential_connection=config.get_connection("api-key"),
            models_dev_catalog=catalog,
        )
        second = await refresh_models(
            config,
            "account-access-token",
            resources_dir,
            credential_connection=config.get_connection("account"),
            models_dev_catalog=catalog,
        )

        written = json.loads(
            (resources_dir / "models" / "opencode-zen.json").read_text(encoding="utf-8")
        )
        raw = json.loads(
            (resources_dir / "models" / "opencode-zen.raw.json").read_text(encoding="utf-8")
        )
        gemini = written["models"]["gemini-3.5-flash"]
        opus = written["models"]["claude-opus-4-1"]
        assert first["model_count"] == 2
        assert second["model_count"] == 2
        assert route.call_count == 2
        assert set(written["models"]) == {"gemini-3.5-flash", "claude-opus-4-1"}
        assert gemini["connections"] == ["api-key", "account"]
        assert gemini["context_window"] == 1_048_576
        assert gemini["max_output_tokens"] == 65_536
        assert gemini["capabilities"]["input_modalities"] == [
            "text",
            "image",
            "video",
            "audio",
            "pdf",
        ]
        assert gemini["metadata"]["opencode_zen"]["protocol"] == ("gemini_generate_content")
        assert opus["metadata"]["opencode_zen"]["deprecates_at"] == "2026-08-05"
        assert {entry["id"] for entry in raw["raw_response"]["data"]} == {
            "gemini-3.5-flash",
            "claude-opus-4-1",
            "glm-5",
            "unreviewed-future-model",
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_tags_models_with_selected_connection_id(
        self,
        tmp_path: Path,
        openai_subscription_connection_config: ProviderConfig,
    ):
        """Refresh of a connection stamps every catalog entry with its local id.

        The merged catalog is loaded through :class:`ModelRegistry` and the
        per-model ``connections`` tuple must contain the connection that
        produced the fetch. Other models on disk from a different
        connection (if any) would be preserved — this is the no-existing
        baseline.
        """

        resources_dir = tmp_path / "resources"
        access_token = jwt_with_openai_account("acct_openai")
        mock_openai_codex_package()
        respx.get(OPENAI_SUBSCRIPTION_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "slug": "gpt-5-codex",
                            "display_name": "GPT-5 Codex",
                            "input_modalities": ["text", "image"],
                            "context_window": 272000,
                            "supports_parallel_tool_calls": True,
                        }
                    ]
                },
            )
        )

        result = await refresh_models(
            openai_subscription_connection_config,
            access_token,
            resources_dir,
            credential_connection=openai_subscription_connection_config.connections[0],
        )

        registry = ModelRegistry.load(resources_dir)
        model = registry.get("openai", "gpt-5-codex")
        catalog_data = json.loads(
            (resources_dir / "models" / "openai.json").read_text(encoding="utf-8")
        )
        assert result["provider_id"] == "openai"
        assert result["model_count"] == 1
        assert model.connections == ("subscription",)
        assert model.name == "GPT-5 Codex"
        assert catalog_data["models"]["gpt-5-codex"]["connections"] == ["subscription"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_merges_models_from_other_connection(
        self,
        tmp_path: Path,
        openai_subscription_connection_config: ProviderConfig,
    ):
        """A second refresh of a different connection leaves earlier entries alone.

        Existing entries tagged with the *other* connection are preserved
        in the shared catalog; entries tagged with the refreshed
        connection are replaced. The catalog is loaded end-to-end through
        :class:`ModelRegistry` to confirm the per-model
        ``connections`` tuple round-trips.
        """

        resources_dir = tmp_path / "resources"
        catalog_path = resources_dir / "models" / "openai.json"
        existing_data = {
            "provider_id": "openai",
            "source": "discovery",
            "fetched_at": "2026-05-08T19:08:00+00:00",
            "models": {
                "gpt-5.2": {
                    "name": "GPT-5.2",
                    "capabilities": {
                        "vision": True,
                        "tools": True,
                        "json_mode": True,
                        "reasoning": {"supported": True},
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                        "supported_parameters": ["tools", "response_format", "reasoning"],
                        "task_types": ["chat", "text_output"],
                    },
                    "context_window": 256000,
                    "max_output_tokens": 32000,
                    "connections": ["api-key"],
                },
                "stale-subscription-model": {
                    "name": "Stale Subscription Model",
                    "capabilities": {
                        "vision": False,
                        "tools": True,
                        "json_mode": True,
                        "reasoning": {"supported": True},
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "supported_parameters": ["tools"],
                        "task_types": ["chat"],
                    },
                    "context_window": 128000,
                    "max_output_tokens": 16000,
                    "connections": ["subscription"],
                },
            },
        }
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_text(json.dumps(existing_data, indent=2), encoding="utf-8")

        access_token = jwt_with_openai_account("acct_openai")
        mock_openai_codex_package()
        respx.get(OPENAI_SUBSCRIPTION_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "slug": "gpt-5.4",
                            "display_name": "GPT-5.4",
                            "input_modalities": ["text", "image"],
                            "context_window": 256000,
                            "supports_parallel_tool_calls": True,
                        }
                    ]
                },
            )
        )

        result = await refresh_models(
            openai_subscription_connection_config,
            access_token,
            resources_dir,
            credential_connection=openai_subscription_connection_config.connections[0],
        )

        registry = ModelRegistry.load(resources_dir)
        merged_data = json.loads(catalog_path.read_text(encoding="utf-8"))
        assert result["provider_id"] == "openai"
        assert result["model_count"] == 2

        # The api-key entry is preserved untouched.
        assert "gpt-5.2" in merged_data["models"]
        assert merged_data["models"]["gpt-5.2"]["connections"] == ["api-key"]
        api_key_model = registry.get("openai", "gpt-5.2")
        assert api_key_model.connections == ("api-key",)

        # The stale subscription entry is replaced by the fresh fetch.
        assert "stale-subscription-model" not in merged_data["models"]
        assert "gpt-5.4" in merged_data["models"]
        assert merged_data["models"]["gpt-5.4"]["connections"] == ["subscription"]
        fresh_model = registry.get("openai", "gpt-5.4")
        assert fresh_model.connections == ("subscription",)

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_uses_connection_endpoint_and_base_url(
        self,
        tmp_path: Path,
        openai_subscription_connection_config: ProviderConfig,
    ):
        """The connection's ``base_url`` + ``models_endpoint`` drive the fetch URL.

        The provider-level defaults would point at the platform endpoint
        (a totally different host); refresh must combine the connection
        values into the request URL and target Codex's ``/codex/models``.
        """

        resources_dir = tmp_path / "resources"
        access_token = jwt_with_openai_account("acct_openai")
        mock_openai_codex_package()
        expected_url = f"{OPENAI_SUBSCRIPTION_MODELS_URL}?client_version=0.144.6"
        route = respx.get(expected_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "slug": "gpt-5-codex",
                            "display_name": "GPT-5 Codex",
                            "input_modalities": ["text", "image"],
                            "context_window": 272000,
                            "supports_parallel_tool_calls": True,
                        }
                    ]
                },
            )
        )

        await refresh_models(
            openai_subscription_connection_config,
            access_token,
            resources_dir,
            credential_connection=openai_subscription_connection_config.connections[0],
        )

        request = route.calls.last.request
        assert str(request.url).split("?")[0] == OPENAI_SUBSCRIPTION_MODELS_URL
        assert request.url.params["client_version"] == "0.144.6"
        assert request.headers["Authorization"] == f"Bearer {access_token}"
        assert request.headers["chatgpt-account-id"] == "acct_openai"
        assert request.headers["OpenAI-Beta"] == "responses=experimental"
        assert request.headers["originator"] == "vbot"

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_uses_codex_version_fallback_for_bad_package_metadata(
        self,
        tmp_path: Path,
        openai_subscription_connection_config: ProviderConfig,
    ):
        """Malformed package metadata cannot prevent Subscription discovery."""

        access_token = jwt_with_openai_account("acct_openai")
        mock_openai_codex_package("next")
        expected_url = f"{OPENAI_SUBSCRIPTION_MODELS_URL}?client_version=0.144.0"
        route = respx.get(expected_url).mock(
            return_value=httpx.Response(
                200,
                json={"models": [{"slug": "gpt-5.6-sol", "display_name": "GPT-5.6 Sol"}]},
            )
        )

        await refresh_models(
            openai_subscription_connection_config,
            access_token,
            tmp_path / "resources",
            credential_connection=openai_subscription_connection_config.connections[0],
        )

        assert route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_without_endpoint_raises_value_error(
        self,
        tmp_path: Path,
    ):
        """A connection with no effective ``models_endpoint`` is rejected loudly."""

        config = ProviderConfig(
            id="openai",
            name="OpenAI",
            adapter="openai",
            base_url="https://api.openai.com/v1",
            connections=[
                ConnectionConfig(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=AuthConfig(
                        header="Authorization",
                        prefix="Bearer ",
                        credential_key="OPENAI_API_KEY",
                    ),
                )
            ],
            defaults={"max_tokens": 8192},
        )

        with pytest.raises(ValueError):
            await refresh_models(
                config,
                "sk-test",
                tmp_path / "resources",
                credential_connection=config.connections[0],
            )

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
    async def test_refresh_models_writes_json_and_registry_reads_it(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        resources_dir = tmp_path / "resources"
        mock_openrouter_image_catalog()
        route = respx.get(OPENROUTER_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        raw_openrouter_model(model_id="model-a", name="Model A"),
                        raw_openrouter_model(
                            model_id="model-b",
                            name="Model B",
                            max_completion_tokens=None,
                        ),
                    ]
                },
            )
        )

        result = await refresh_models(openrouter_config, API_KEY, resources_dir)

        output_path = resources_dir / "models" / "openrouter.json"
        raw_output_path = resources_dir / "models" / "openrouter.raw.json"
        output_data = json.loads(output_path.read_text(encoding="utf-8"))
        raw_output_data = json.loads(raw_output_path.read_text(encoding="utf-8"))
        registry = ModelRegistry.load(resources_dir)
        model_b = registry.get("openrouter", "model-b")

        assert result["provider_id"] == "openrouter"
        assert result["model_count"] == 2
        assert result["fetched_at"] == output_data["fetched_at"]
        assert output_data["source"] == "discovery"
        assert raw_output_path.exists()
        assert raw_output_data["provider_id"] == "openrouter"
        assert raw_output_data["fetched_at"] == output_data["fetched_at"]
        assert model_b.name == "Model B"
        assert model_b.max_output_tokens is None
        assert route.calls.last.request.headers["Authorization"] == f"Bearer {API_KEY}"
        assert route.calls.last.request.headers["X-Title"] == "vBot"

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_writes_pure_projection_and_overrides_apply_at_load(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        """Refresh stays DUMB: it no longer bakes ``<provider>.overrides.json``
        into ``<provider>.json``. The pure provider projection is written (only
        the fetched model), and the override correction + the override-only model
        come in at LOAD (Phase 2 assembly) instead — proving the move."""

        resources_dir = tmp_path / "resources"
        mock_openrouter_image_catalog()
        models_dir = resources_dir / "models"
        models_dir.mkdir(parents=True)
        overrides_path = models_dir / "openrouter.overrides.json"
        overrides_path.write_text(
            json.dumps(
                {
                    "provider_id": "openrouter",
                    "models": {
                        "model-a": {"name": "Corrected Model A"},
                        "override-only": model_data("Override Only"),
                    },
                }
            ),
            encoding="utf-8",
        )
        respx.get(OPENROUTER_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": [raw_openrouter_model(model_id="model-a", name="Model A")]},
            )
        )

        result = await refresh_models(openrouter_config, API_KEY, resources_dir)

        output_path = models_dir / "openrouter.json"
        raw_output_path = models_dir / "openrouter.raw.json"
        # Refresh wrote the PURE provider projection: only the fetched model, no
        # override baking. The override-only model is absent from the file.
        assert result["model_count"] == 1
        written = json.loads(output_path.read_text(encoding="utf-8"))
        assert set(written["models"]) == {"model-a"}
        assert written["models"]["model-a"]["name"] == "Model A"
        assert "override-only" not in written["models"]
        assert raw_output_path.exists()
        assert overrides_path.exists()
        # The overrides apply at LOAD: the correction wins, the override-only
        # model appears in the assembled registry.
        registry = ModelRegistry.load(resources_dir)
        assert registry.get("openrouter", "model-a").name == "Corrected Model A"
        assert registry.get("openrouter", "override-only").name == "Override Only"

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_uses_selected_connection_auth_headers(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        selected_connection = ConnectionConfig(
            id="secondary",
            type="api_key",
            label="Secondary",
            auth=AuthConfig(
                header="x-api-key",
                prefix="Token ",
                credential_key="SECONDARY_KEY",
            ),
        )
        provider_config = ProviderConfig(
            id=openrouter_config.id,
            name=openrouter_config.name,
            adapter=openrouter_config.adapter,
            base_url=openrouter_config.base_url,
            connections=[openrouter_config.connections[0], selected_connection],
            defaults=openrouter_config.defaults,
            extra_headers=openrouter_config.extra_headers,
            models_endpoint=openrouter_config.models_endpoint,
        )
        route = respx.get(OPENROUTER_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": [raw_openrouter_model(model_id="model-a", name="Model A")]},
            )
        )
        resources_dir = tmp_path / "resources"
        mock_openrouter_image_catalog()

        await refresh_models(
            provider_config,
            API_KEY,
            resources_dir,
            credential_connection=selected_connection,
        )

        assert (resources_dir / "models" / "openrouter.json").exists()
        assert (resources_dir / "models" / "openrouter.raw.json").exists()
        assert route.calls.last.request.headers["x-api-key"] == f"Token {API_KEY}"
        assert "Authorization" not in route.calls.last.request.headers

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_keyless_connection_sends_no_auth_header(
        self,
        tmp_path: Path,
    ):
        """A ``none`` connection refreshes with an empty credential and no auth header."""
        keyless_connection = ConnectionConfig(
            id="local",
            type="none",
            label="Local",
            auth=AuthConfig(header="", prefix="", credential_key=""),
        )
        provider_config = ProviderConfig(
            id="localhost",
            name="Localhost",
            adapter="openai_compatible",
            base_url="http://localhost:9999/v1",
            connections=[keyless_connection],
            models_endpoint="/models",
        )
        route = respx.get("http://localhost:9999/v1/models").mock(
            return_value=httpx.Response(200, json={"models": [{"id": "test-model"}]})
        )

        result = await refresh_models(
            provider_config,
            "",
            tmp_path / "resources",
            credential_connection=keyless_connection,
        )

        registry = ModelRegistry.load(tmp_path / "resources")
        assert result["model_count"] == 1
        assert registry.get("localhost", "test-model").connections == ("local",)
        assert "Authorization" not in route.calls.last.request.headers

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_ollama_enriches_from_api_show(self, tmp_path: Path):
        """Ollama refresh normalizes /api/tags and enriches via POST /api/show."""
        keyless_connection = ConnectionConfig(
            id="local",
            type="none",
            label="Local",
            auth=AuthConfig(header="", prefix="", credential_key=""),
        )
        provider_config = ProviderConfig(
            id="ollama",
            name="Ollama",
            adapter="ollama",
            base_url="http://localhost:11434",
            connections=[keyless_connection],
            models_endpoint="/api/tags",
        )
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
            "kimi-k2.6:cloud": {
                "capabilities": ["completion", "tools", "thinking"],
                "model_info": {},
            },
        }

        def _show_side_effect(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json=show_responses[body["model"]])

        show_route = respx.post("http://localhost:11434/api/show").mock(
            side_effect=_show_side_effect
        )

        result = await refresh_models(
            provider_config,
            "",
            tmp_path / "resources",
            credential_connection=keyless_connection,
        )

        registry = ModelRegistry.load(tmp_path / "resources")
        local_model = registry.get("ollama", "ministral-3:8b")
        cloud_model = registry.get("ollama", "kimi-k2.6:cloud")
        raw_data = json.loads(
            (tmp_path / "resources" / "models" / "ollama.raw.json").read_text(encoding="utf-8")
        )
        assert result["model_count"] == 2
        assert show_route.call_count == 2
        assert local_model.capabilities.tools is True
        assert local_model.capabilities.vision is True
        assert local_model.context_window == 262144
        assert local_model.metadata["ollama"] == {"local": True}
        assert local_model.connections == ("local",)
        assert cloud_model.capabilities.reasoning.supported is True
        assert cloud_model.metadata["ollama"] == {"remote": True}
        assert len(raw_data["raw_enrichment_responses"]) == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_direct_ollama_cloud_is_remote_without_auth_header(
        self,
        tmp_path: Path,
    ) -> None:
        cloud_connection = ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API key",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
                credential_key="OLLAMA_API_KEY",
            ),
            mode="cloud",
            catalog_requires_credentials=False,
        )
        provider_config = ProviderConfig(
            id="ollama-cloud",
            name="Ollama Cloud",
            adapter="ollama",
            base_url="https://ollama.com",
            connections=[cloud_connection],
            models_endpoint="/api/tags",
        )
        tags_route = respx.get("https://ollama.com/api/tags").mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "glm-5.1",
                            "model": "glm-5.1",
                            "details": {"family": "glm5.1"},
                        }
                    ]
                },
            )
        )
        show_route = respx.post("https://ollama.com/api/show").mock(
            return_value=httpx.Response(
                200,
                json={
                    "capabilities": ["completion", "tools", "thinking"],
                    "model_info": {
                        "general.architecture": "glm5.1",
                        "glm5.1.context_length": 202752,
                    },
                },
            )
        )

        result = await refresh_models(
            provider_config,
            "",
            tmp_path / "resources",
            credential_connection=cloud_connection,
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
    async def test_refresh_models_ollama_show_failure_keeps_baseline(self, tmp_path: Path):
        """A failing /api/show degrades to the conservative catalog, not a failed refresh."""
        keyless_connection = ConnectionConfig(
            id="local",
            type="none",
            label="Local",
            auth=AuthConfig(header="", prefix="", credential_key=""),
        )
        provider_config = ProviderConfig(
            id="ollama",
            name="Ollama",
            adapter="ollama",
            base_url="http://localhost:11434",
            connections=[keyless_connection],
            models_endpoint="/api/tags",
        )
        respx.get("http://localhost:11434/api/tags").mock(
            return_value=httpx.Response(
                200,
                json={"models": [{"model": "ministral-3:8b", "details": {"family": "mistral3"}}]},
            )
        )
        respx.post("http://localhost:11434/api/show").mock(
            return_value=httpx.Response(404, json={"error": "model not found"})
        )

        result = await refresh_models(
            provider_config,
            "",
            tmp_path / "resources",
            credential_connection=keyless_connection,
        )

        registry = ModelRegistry.load(tmp_path / "resources")
        model = registry.get("ollama", "ministral-3:8b")
        assert result["model_count"] == 1
        assert model.capabilities.tools is False
        assert model.context_window is None

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

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_writes_raw_file_with_full_provider_response(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        resources_dir = tmp_path / "resources"
        mock_openrouter_image_catalog()
        raw_response = {
            "data": [
                {
                    **raw_openrouter_model(model_id="model-a", name="Model A"),
                    "future_field": "value",
                }
            ],
            "extra_key": "preserved",
        }
        respx.get(OPENROUTER_MODELS_URL).mock(return_value=httpx.Response(200, json=raw_response))

        await refresh_models(openrouter_config, API_KEY, resources_dir)

        raw_output_data = json.loads(
            (resources_dir / "models" / "openrouter.raw.json").read_text(encoding="utf-8")
        )
        sanitized_output_data = json.loads(
            (resources_dir / "models" / "openrouter.json").read_text(encoding="utf-8")
        )

        assert raw_output_data["raw_response"]["extra_key"] == "preserved"
        assert raw_output_data["raw_response"]["data"][0]["future_field"] == "value"
        assert "extra_key" not in sanitized_output_data
        assert "future_field" not in sanitized_output_data["models"]["model-a"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_raw_file_contains_unfiltered_data(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        class _DropModelBFilter:
            def accepts(self, raw_model):
                return raw_model.get("id") != "model-b"

        resources_dir = tmp_path / "resources"
        mock_openrouter_image_catalog()
        raw_response = {
            "data": [
                raw_openrouter_model(model_id="model-a", name="Model A"),
                raw_openrouter_model(model_id="model-b", name="Model B"),
            ]
        }
        respx.get(OPENROUTER_MODELS_URL).mock(return_value=httpx.Response(200, json=raw_response))

        await refresh_models(
            openrouter_config,
            API_KEY,
            resources_dir,
            raw_filter=_DropModelBFilter(),
        )

        raw_output_data = json.loads(
            (resources_dir / "models" / "openrouter.raw.json").read_text(encoding="utf-8")
        )
        sanitized_output_data = json.loads(
            (resources_dir / "models" / "openrouter.json").read_text(encoding="utf-8")
        )

        assert len(raw_output_data["raw_response"]["data"]) == 2
        assert {model["id"] for model in raw_output_data["raw_response"]["data"]} == {
            "model-a",
            "model-b",
        }
        assert set(sanitized_output_data["models"].keys()) == {"model-a"}

    @pytest.mark.asyncio
    async def test_refresh_models_rejects_unknown_discovery_adapter(self, tmp_path: Path):
        provider_config = ProviderConfig(
            id="unknown-provider",
            name="Unknown Provider",
            adapter="unknown_adapter",
            base_url="https://example.test",
            connections=[],
            defaults={},
            extra_headers={},
            models_endpoint="/models",
        )

        with pytest.raises(ModelDiscoveryError, match="unknown_adapter"):
            await refresh_models(provider_config, API_KEY, tmp_path / "resources")

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_maps_invalid_json_response_to_discovery_error(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        respx.get(OPENROUTER_MODELS_URL).mock(return_value=httpx.Response(200, text="not-json"))

        with pytest.raises(ModelDiscoveryError):
            await refresh_models(openrouter_config, API_KEY, tmp_path / "resources")

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_logs_warning_on_catalog_refresh_failure(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
        caplog: Any,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A primary catalog-refresh failure logs a warning (no traceback) before raising."""

        async def _no_sleep(_delay: float) -> None:
            return None

        monkeypatch.setattr("core.utils.retry.asyncio.sleep", _no_sleep)
        respx.get(OPENROUTER_MODELS_URL).mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )

        with (
            caplog.at_level(logging.WARNING, logger="vbot.models.discovery"),
            pytest.raises(ModelDiscoveryError),
        ):
            await refresh_models(openrouter_config, API_KEY, tmp_path / "resources")

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        failure_record = next(
            r for r in warning_records if "Model catalog refresh failed" in r.getMessage()
        )
        assert openrouter_config.id in failure_record.getMessage()
        assert failure_record.exc_info is None

    @respx.mock
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [500, 503])
    async def test_refresh_models_retries_transient_status_then_succeeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        status_code: int,
    ):
        """A retryable GET status is re-issued with backoff before succeeding."""

        # Skip the real backoff sleep so the retry path stays fast.
        async def _no_sleep(_delay: float) -> None:
            return None

        monkeypatch.setattr("core.utils.retry.asyncio.sleep", _no_sleep)

        responses = [
            httpx.Response(status_code, text="Transient provider failure"),
            httpx.Response(200, json={"data": [{"id": "model-a", "name": "Model A"}]}),
        ]
        route = respx.get(_SIMPLE_MODELS_URL).mock(side_effect=responses)

        result = await refresh_models(_simple_compatible_config(), API_KEY, tmp_path / "resources")

        assert route.call_count == 2
        assert result["model_count"] == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_does_not_retry_fatal_status(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A fatal status (404) aborts immediately without retrying."""

        async def _fail_if_called(_delay: float) -> None:
            raise AssertionError("fatal status must not trigger a retry sleep")

        monkeypatch.setattr("core.utils.retry.asyncio.sleep", _fail_if_called)
        route = respx.get(_SIMPLE_MODELS_URL).mock(
            return_value=httpx.Response(404, text="Not Found")
        )

        with pytest.raises(ModelDiscoveryError):
            await refresh_models(_simple_compatible_config(), API_KEY, tmp_path / "resources")

        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_retries_transport_error_then_succeeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A transient transport failure is re-issued before succeeding."""

        async def _no_sleep(_delay: float) -> None:
            return None

        monkeypatch.setattr("core.utils.retry.asyncio.sleep", _no_sleep)

        responses: list[httpx.Response | Exception] = [
            httpx.ConnectError("connection reset"),
            httpx.Response(200, json={"data": [{"id": "model-a", "name": "Model A"}]}),
        ]
        route = respx.get(_SIMPLE_MODELS_URL).mock(side_effect=responses)

        result = await refresh_models(_simple_compatible_config(), API_KEY, tmp_path / "resources")

        assert route.call_count == 2
        assert result["model_count"] == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_skips_catalog_entry_skipped_and_continues(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        class _SkipEntryAdapter:
            @staticmethod
            def normalize_catalog_entry(raw_model: dict, defaults: dict | None) -> Model:
                if raw_model.get("id") == "skip-me":
                    raise CatalogEntrySkipped("skip expected non-chat model")
                return Model(
                    model_id=str(raw_model["id"]),
                    name=str(raw_model.get("name", "Kept Model")),
                    capabilities=Capabilities(
                        vision=False,
                        tools=True,
                        json_mode=True,
                        reasoning=ReasoningCapabilities(supported=False),
                    ),
                    context_window=8192,
                    max_output_tokens=2048,
                )

        provider_config = ProviderConfig(
            id="stub-provider",
            name="Stub Provider",
            adapter="stub_skip_adapter",
            base_url="https://stub-provider.example/v1",
            connections=[
                ConnectionConfig(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=AuthConfig(
                        header="Authorization",
                        prefix="Bearer ",
                        credential_key="STUB_PROVIDER_KEY",
                    ),
                )
            ],
            defaults={"max_tokens": 2048},
            models_endpoint="/models",
        )

        monkeypatch.setitem(
            discovery_module._DISCOVERY_ADAPTER_MAP,
            "stub_skip_adapter",
            _SkipEntryAdapter,
        )
        respx.get(STUB_DISCOVERY_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "skip-me", "name": "Skipped Model"},
                        {"id": "keep-me", "name": "Kept Model"},
                    ]
                },
            )
        )
        resources_dir = tmp_path / "resources"

        result = await refresh_models(provider_config, API_KEY, resources_dir)

        registry = ModelRegistry.load(resources_dir)
        assert result["provider_id"] == "stub-provider"
        assert result["model_count"] == 1
        assert registry.get("stub-provider", "keep-me").name == "Kept Model"
        with pytest.raises(KeyError):
            registry.get("stub-provider", "skip-me")

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_wraps_normalizer_value_error_as_discovery_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        class _ErroringAdapter:
            @staticmethod
            def normalize_catalog_entry(raw_model: dict, defaults: dict | None) -> Model:
                raise ValueError("schema mismatch")

        provider_config = ProviderConfig(
            id="stub-provider",
            name="Stub Provider",
            adapter="stub_error_adapter",
            base_url="https://stub-provider.example/v1",
            connections=[
                ConnectionConfig(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=AuthConfig(
                        header="Authorization",
                        prefix="Bearer ",
                        credential_key="STUB_PROVIDER_KEY",
                    ),
                )
            ],
            defaults={"max_tokens": 2048},
            models_endpoint="/models",
        )

        monkeypatch.setitem(
            discovery_module._DISCOVERY_ADAPTER_MAP,
            "stub_error_adapter",
            _ErroringAdapter,
        )
        respx.get(STUB_DISCOVERY_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "broken-model", "name": "Broken Model"}]},
            )
        )
        resources_dir = tmp_path / "resources"

        with pytest.raises(ModelDiscoveryError, match="schema mismatch"):
            await refresh_models(provider_config, API_KEY, resources_dir)

        assert (resources_dir / "models" / "stub-provider.raw.json").exists()
        assert not (resources_dir / "models" / "stub-provider.json").exists()
