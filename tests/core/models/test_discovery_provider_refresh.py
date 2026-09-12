"""Tests for discovery provider refresh."""

from __future__ import annotations

from .discovery_test_support import (
    API_KEY,
    OPENAI_SUBSCRIPTION_MODELS_URL,
    OPENROUTER_MODELS_URL,
    AuthConfig,
    ConnectionConfig,
    ModelRegistry,
    ModelsDevCatalog,
    Path,
    ProviderConfig,
    httpx,
    json,
    jwt_with_openai_account,
    mock_openai_codex_package,
    mock_openrouter_image_catalog,
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
                            "visibility": "list",
                        },
                        {
                            "slug": "codex-auto-review",
                            "display_name": "Codex Auto Review",
                            "input_modalities": ["text", "image"],
                            "context_window": 272000,
                            "supports_parallel_tool_calls": True,
                            "visibility": "hide",
                        },
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
        assert "codex-auto-review" not in catalog_data["models"]
        raw_data = json.loads(
            (resources_dir / "models" / "openai.raw.json").read_text(encoding="utf-8")
        )
        assert {entry["slug"] for entry in raw_data["raw_response"]["models"]} == {
            "codex-auto-review",
            "gpt-5-codex",
        }

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
