"""models.dev enrichment and canonical projection tests."""

from __future__ import annotations

from .discovery_test_support import (
    API_KEY,
    OPENROUTER_MODELS_URL,
    AuthConfig,
    ConnectionConfig,
    ModelRegistry,
    ModelsDevCatalog,
    Path,
    ProviderConfig,
    discovery_module,
    httpx,
    is_provider_file,
    json,
    mock_openrouter_image_catalog,
    pytest,
    raw_openrouter_model,
    refresh_canonical_layer,
    refresh_models,
    respx,
)
from .discovery_test_support import _clear_registry_cache as _clear_registry_cache
from .discovery_test_support import openrouter_config as openrouter_config

CATALOG_FIXTURE = Path(__file__).parent / "fixtures" / "models_dev_catalog.json"


def _fixture_catalog() -> ModelsDevCatalog:
    raw = json.loads(CATALOG_FIXTURE.read_text(encoding="utf-8"))
    return ModelsDevCatalog(raw)


class TestRefreshModelsDevEnrichment:
    """Refresh stamps the models.dev canonical pointer + deviating ladder, and
    the deepseek-v4-pro worked example round-trips refresh → load."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_stamps_auto_canonical_pointer(
        self,
        tmp_path: Path,
    ):
        # Arrange — a lab-section provider (deepseek) whose wire-id matches a
        # canonical provider section gets an auto canonical pointer.
        provider_config = ProviderConfig(
            id="deepseek",
            name="DeepSeek",
            adapter="openai_compatible",
            base_url="https://api.deepseek.com/v1",
            connections=[
                ConnectionConfig(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=AuthConfig(
                        header="Authorization",
                        prefix="Bearer ",
                        credential_key="DEEPSEEK_KEY",
                    ),
                )
            ],
            defaults={},
            models_endpoint="/models",
        )
        respx.get("https://api.deepseek.com/v1/models").mock(
            return_value=httpx.Response(
                200, json={"data": [{"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro"}]}
            )
        )
        resources_dir = tmp_path / "resources"

        # Act
        await refresh_models(
            provider_config,
            API_KEY,
            resources_dir,
            models_dev_catalog=_fixture_catalog(),
        )

        # Assert — the provider file carries the auto canonical pointer.
        written = json.loads(
            (resources_dir / "models" / "deepseek.json").read_text(encoding="utf-8")
        )
        assert written["models"]["deepseek-v4-pro"]["canonical"] == "deepseek/deepseek-v4-pro"

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_fills_limits_modalities_family_from_models_dev(self, tmp_path: Path):
        """A bare endpoint (id only) gets context_window / max_output_tokens /
        family and widened modalities from the provider's models.dev section —
        the facts a gateway endpoint omits. Modalities widen as a strict superset
        of what the endpoint reported (add, never drop)."""
        provider_config = ProviderConfig(
            id="alibaba",
            name="Alibaba",
            adapter="openai_compatible",
            base_url="https://example.test/v1",
            connections=[
                ConnectionConfig(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=AuthConfig(
                        header="Authorization",
                        prefix="Bearer ",
                        credential_key="ALIBABA_KEY",
                    ),
                )
            ],
            defaults={},
            models_endpoint="/models",
        )
        respx.get("https://example.test/v1/models").mock(
            return_value=httpx.Response(
                200, json={"data": [{"id": "qwen3.5-plus", "name": "Qwen3.5 Plus"}]}
            )
        )
        resources_dir = tmp_path / "resources"

        # Act
        await refresh_models(
            provider_config,
            API_KEY,
            resources_dir,
            models_dev_catalog=_fixture_catalog(),
        )

        # Assert — limits, family, and the widened modalities all come from
        # the alibaba models.dev section (the endpoint reported only id + name).
        model = json.loads((resources_dir / "models" / "alibaba.json").read_text(encoding="utf-8"))[
            "models"
        ]["qwen3.5-plus"]
        assert model["context_window"] == 1000000
        assert model["max_output_tokens"] == 65536
        assert model["family"] == "qwen"
        assert model["capabilities"]["input_modalities"] == ["text", "image", "video"]
        assert model["capabilities"]["vision"] is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_deepseek_v4_pro_canonical_and_openrouter_deviation_round_trip(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        """The worked example end-to-end: canonical [high, max] (lab spec),
        OpenRouter [high, xhigh] (provider deviation), via refresh → load."""

        resources_dir = tmp_path / "resources"
        mock_openrouter_image_catalog()
        catalog = _fixture_catalog()

        # Canonical layer (models.json) with the lifted lab ladder.
        await refresh_canonical_layer(resources_dir, catalog=catalog)

        # OpenRouter provider refresh: wire-id is the canonical id deepseek/deepseek-v4-pro.
        respx.get(OPENROUTER_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        raw_openrouter_model(
                            model_id="deepseek/deepseek-v4-pro",
                            name="DeepSeek V4 Pro",
                            input_modalities=["text"],
                            supported_parameters=["tools", "reasoning"],
                        )
                    ]
                },
            )
        )
        await refresh_models(
            openrouter_config,
            API_KEY,
            resources_dir,
            models_dev_catalog=catalog,
        )

        registry = ModelRegistry.load(resources_dir)
        model = registry.get("openrouter", "deepseek/deepseek-v4-pro")
        # The effective ladder is OpenRouter's deviation (provider layer wins).
        assert model.capabilities.reasoning.control == "levels"
        assert model.capabilities.reasoning.levels == ("high", "xhigh")

        # The canonical base itself holds the lab spec [high, max].
        canonical = json.loads(
            (resources_dir / "models" / "models.json").read_text(encoding="utf-8")
        )
        canonical_reasoning = canonical["models"]["deepseek/deepseek-v4-pro"]["capabilities"][
            "reasoning"
        ]
        assert canonical_reasoning == {
            "supported": True,
            "control": "levels",
            "levels": ["high", "max"],
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_non_deviating_provider_inherits_canonical_ladder_at_load(
        self,
        tmp_path: Path,
    ):
        """A provider whose ladder equals the lab spec gets NO provider-layer
        ladder stamped; it inherits the canonical ladder at load."""

        resources_dir = tmp_path / "resources"
        catalog = _fixture_catalog()
        await refresh_canonical_layer(resources_dir, catalog=catalog)

        provider_config = ProviderConfig(
            id="deepseek",
            name="DeepSeek",
            adapter="openai_compatible",
            base_url="https://api.deepseek.com/v1",
            connections=[
                ConnectionConfig(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=AuthConfig(
                        header="Authorization",
                        prefix="Bearer ",
                        credential_key="DEEPSEEK_KEY",
                    ),
                )
            ],
            defaults={},
            models_endpoint="/models",
        )
        respx.get("https://api.deepseek.com/v1/models").mock(
            return_value=httpx.Response(
                200, json={"data": [{"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro"}]}
            )
        )
        await refresh_models(
            provider_config,
            API_KEY,
            resources_dir,
            models_dev_catalog=catalog,
        )

        # The provider file must carry NO reasoning block at all (dropped so the
        # canonical ladder flows through at load — handoff: non-deviating provider
        # layer is empty).
        written = json.loads(
            (resources_dir / "models" / "deepseek.json").read_text(encoding="utf-8")
        )
        assert "reasoning" not in written["models"]["deepseek-v4-pro"]["capabilities"]

        # At load, it inherits the canonical lab ladder [high, max].
        registry = ModelRegistry.load(resources_dir)
        model = registry.get("deepseek", "deepseek-v4-pro")
        assert model.capabilities.reasoning.levels == ("high", "max")

    @pytest.mark.asyncio
    async def test_raw_catalog_dump_is_written_and_not_read_at_runtime(
        self,
        tmp_path: Path,
    ):
        """The raw catalog.json dump is written by the canonical refresh and is
        NOT read by the registry read path."""

        resources_dir = tmp_path / "resources"
        await refresh_canonical_layer(resources_dir, catalog=_fixture_catalog())

        raw_path = resources_dir / "models" / "models.dev.catalog.raw.json"
        assert raw_path.exists()
        # The registry classifier rejects the raw dump as a provider file.
        assert is_provider_file(raw_path.name) is False
        # Loading the registry must not surface any raw-dump model.
        registry = ModelRegistry.load(resources_dir)
        assert registry.list_for_provider("models.dev.catalog.raw") == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_projects_interleaved_to_reasoning_response_field(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        """models.dev ``interleaved`` projects to metadata.<provider>.reasoning_response_field."""

        resources_dir = tmp_path / "resources"
        mock_openrouter_image_catalog()
        respx.get(OPENROUTER_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        raw_openrouter_model(
                            model_id="deepseek/deepseek-v4-pro",
                            name="DeepSeek V4 Pro",
                            input_modalities=["text"],
                            supported_parameters=["tools", "reasoning"],
                        )
                    ]
                },
            )
        )

        await refresh_models(
            openrouter_config,
            API_KEY,
            resources_dir,
            models_dev_catalog=_fixture_catalog(),
        )

        written = json.loads(
            (resources_dir / "models" / "openrouter.json").read_text(encoding="utf-8")
        )
        metadata = written["models"]["deepseek/deepseek-v4-pro"]["metadata"]
        assert metadata["openrouter"]["reasoning_response_field"] == "reasoning_content"

        # And it loads onto the effective Model's provider-scoped metadata.
        registry = ModelRegistry.load(resources_dir)
        model = registry.get("openrouter", "deepseek/deepseek-v4-pro")
        assert model.metadata["openrouter"]["reasoning_response_field"] == "reasoning_content"

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_omits_reasoning_response_field_without_interleaved(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        """A model with no models.dev ``interleaved`` gets no reasoning_response_field."""

        resources_dir = tmp_path / "resources"
        mock_openrouter_image_catalog()
        respx.get(OPENROUTER_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        raw_openrouter_model(
                            model_id="google/gemini-2.5-flash",
                            name="Gemini 2.5 Flash",
                        )
                    ]
                },
            )
        )

        await refresh_models(
            openrouter_config,
            API_KEY,
            resources_dir,
            models_dev_catalog=_fixture_catalog(),
        )

        written = json.loads(
            (resources_dir / "models" / "openrouter.json").read_text(encoding="utf-8")
        )
        model_data = written["models"]["google/gemini-2.5-flash"]
        metadata = model_data.get("metadata", {})
        assert "openrouter" not in metadata or "reasoning_response_field" not in metadata.get(
            "openrouter", {}
        )


def test_anthropic_adapter_registered_for_discovery() -> None:
    """The Anthropic adapter is wired into the refresh pipeline like every other."""
    from core.providers.anthropic import AnthropicAdapter

    assert discovery_module._DISCOVERY_ADAPTER_MAP["anthropic"] is AnthropicAdapter
    assert discovery_module._adapter_class_for_discovery("anthropic") is AnthropicAdapter


def test_xai_adapter_registered_for_discovery() -> None:
    from core.providers.xai import XAIAdapter

    assert discovery_module._DISCOVERY_ADAPTER_MAP["xai"] is XAIAdapter
    assert discovery_module._adapter_class_for_discovery("xai") is XAIAdapter


def test_kimi_adapter_registered_for_discovery() -> None:
    from core.providers.kimi import KimiAdapter

    assert discovery_module._DISCOVERY_ADAPTER_MAP["kimi"] is KimiAdapter
    assert discovery_module._adapter_class_for_discovery("kimi") is KimiAdapter
