"""Tests for discovery persistence."""

from __future__ import annotations

from .discovery_test_support import (
    _SIMPLE_MODELS_URL,
    API_KEY,
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
    Path,
    ProviderConfig,
    ReasoningCapabilities,
    _simple_compatible_config,
    discovery_module,
    httpx,
    json,
    logging,
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
