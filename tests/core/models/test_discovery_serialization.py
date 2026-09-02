"""Model discovery serialization and typed-reasoning tests."""

from __future__ import annotations

from .discovery_test_support import (
    API_KEY,
    STUB_DISCOVERY_MODELS_URL,
    AuthConfig,
    Capabilities,
    ConnectionConfig,
    Model,
    ModelRegistry,
    Path,
    ProviderConfig,
    ReasoningCapabilities,
    discovery_module,
    httpx,
    pytest,
    refresh_models,
    respx,
)
from .discovery_test_support import _clear_registry_cache as _clear_registry_cache


class TestModelToData:
    def test_model_to_data_round_trips_supported_voices(self) -> None:
        """The ``_model_to_data`` write path keeps ``supported_voices`` stable so a
        normalized OpenRouter TTS entry survives serialize → ``ModelRegistry.load``."""

        from core.models.discovery import _model_to_data

        model = Model(
            model_id="hexgrad/kokoro-82m",
            name="hexgrad: Kokoro 82M",
            capabilities=Capabilities(
                vision=False,
                tools=False,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=False),
                input_modalities=("text",),
                output_modalities=("speech",),
                supported_parameters=("response_format", "seed"),
                supported_voices=("af_aoede", "af_sky", "am_adam"),
                task_types=("audio_generation", "text_to_speech"),
            ),
            context_window=4096,
            max_output_tokens=None,
        )

        data = _model_to_data(model)

        assert data["capabilities"]["supported_voices"] == [
            "af_aoede",
            "af_sky",
            "am_adam",
        ]

    def test_model_to_data_serializes_null_context_window(self) -> None:
        """A ``None`` context window serializes to JSON ``null`` (mirroring
        ``max_output_tokens``) so the honest gap round-trips through the catalog
        instead of being faked with a constant."""

        from core.models.discovery import _model_to_data

        model = Model(
            model_id="window-less",
            name="Window-less",
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=False),
            ),
            context_window=None,
            max_output_tokens=None,
        )

        data = _model_to_data(model)

        assert data["context_window"] is None

    def test_model_to_data_serializes_connection_context_windows(self) -> None:
        from core.models.discovery import _model_to_data

        model = Model(
            model_id="shared-wire-model",
            name="Shared Wire Model",
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=272_000,
            max_output_tokens=128_000,
            connection_context_windows={"api-key": 1_050_000, "subscription": 272_000},
        )

        data = _model_to_data(model)

        assert data["connection_context_windows"] == {
            "api-key": 1_050_000,
            "subscription": 272_000,
        }


class TestTypedReasoningSerialization:
    """The discovery serializer + validator round-trip the typed reasoning
    shape and ``family``, and reject malformed reasoning blocks."""

    def test_model_to_data_omits_unset_reasoning_control_fields(self) -> None:
        """A supported model with no projected ladder serializes back to the
        bare ``{"supported": true}`` form — control/levels/budget_max are
        omitted when unset, matching how connections/metadata are omitted."""

        from core.models.discovery import _model_to_data

        model = Model(
            model_id="minimal",
            name="Minimal",
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=32000,
            max_output_tokens=4096,
        )

        data = _model_to_data(model)

        assert data["capabilities"]["reasoning"] == {"supported": True}
        assert "family" not in data

    def test_model_to_data_emits_typed_reasoning_and_family(self) -> None:
        from core.models.discovery import _model_to_data

        model = Model(
            model_id="levels-model",
            name="Levels Model",
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(
                    supported=True,
                    control="levels",
                    levels=("low", "medium", "high"),
                ),
            ),
            context_window=128000,
            max_output_tokens=16000,
            family="gpt-5.2",
        )

        data = _model_to_data(model)

        assert data["capabilities"]["reasoning"] == {
            "supported": True,
            "control": "levels",
            "levels": ["low", "medium", "high"],
        }
        assert data["family"] == "gpt-5.2"

    def test_model_to_data_emits_budget_max(self) -> None:
        from core.models.discovery import _model_to_data

        model = Model(
            model_id="budget-model",
            name="Budget Model",
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(
                    supported=True,
                    control="budget",
                    budget_max=32000,
                ),
            ),
            context_window=200000,
            max_output_tokens=64000,
        )

        data = _model_to_data(model)

        assert data["capabilities"]["reasoning"] == {
            "supported": True,
            "control": "budget",
            "budget_max": 32000,
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_round_trips_typed_reasoning_shape(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A normalized model carrying the typed reasoning ladder survives
        serialize → write → ``ModelRegistry.load`` with the control fields
        intact. The adapter is stubbed so the test is pure (no network)."""

        class _TypedReasoningAdapter:
            @staticmethod
            def normalize_catalog_entry(raw_model: dict, defaults: dict | None) -> Model:
                return Model(
                    model_id=str(raw_model["id"]),
                    name=str(raw_model.get("name", "Typed")),
                    capabilities=Capabilities(
                        vision=False,
                        tools=True,
                        json_mode=True,
                        reasoning=ReasoningCapabilities(
                            supported=True,
                            control="levels",
                            levels=("low", "medium", "high"),
                        ),
                    ),
                    context_window=128000,
                    max_output_tokens=16000,
                    family="deepseek-v4",
                )

        provider_config = ProviderConfig(
            id="stub-provider",
            name="Stub Provider",
            adapter="stub_typed_adapter",
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
            defaults={},
            models_endpoint="/models",
        )
        monkeypatch.setitem(
            discovery_module._DISCOVERY_ADAPTER_MAP,
            "stub_typed_adapter",
            _TypedReasoningAdapter,
        )
        respx.get(STUB_DISCOVERY_MODELS_URL).mock(
            return_value=httpx.Response(
                200, json={"data": [{"id": "typed-model", "name": "Typed Model"}]}
            )
        )
        resources_dir = tmp_path / "resources"

        await refresh_models(provider_config, API_KEY, resources_dir)

        registry = ModelRegistry.load(resources_dir)
        model = registry.get("stub-provider", "typed-model")
        assert model.capabilities.reasoning.control == "levels"
        assert model.capabilities.reasoning.levels == ("low", "medium", "high")
        assert model.family == "deepseek-v4"
