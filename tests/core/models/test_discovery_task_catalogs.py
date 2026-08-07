"""Supplementary and task-capability catalog refresh tests."""

from __future__ import annotations

from .discovery_test_support import (
    API_KEY,
    OPENROUTER_IMAGE_MODELS_URL,
    OPENROUTER_MODELS_URL,
    ModelRegistry,
    Path,
    ProviderConfig,
    httpx,
    json,
    mock_openrouter_image_catalog,
    pytest,
    raw_openrouter_model,
    refresh_models,
    respx,
)
from .discovery_test_support import _clear_registry_cache as _clear_registry_cache
from .discovery_test_support import openrouter_config as openrouter_config


class TestRefreshModels:
    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_fetches_supplementary_openrouter_models(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        """OpenRouter discovery fetches STT/TTS models via supplementary API calls."""
        resources_dir = tmp_path / "resources"
        mock_openrouter_image_catalog()

        # Main catalog returns a chat model and a multimodal audio model.
        main_models = {
            "data": [
                raw_openrouter_model(model_id="openai/gpt-4o", name="GPT-4o"),
                raw_openrouter_model(
                    model_id="openai/gpt-audio",
                    name="GPT Audio",
                    output_modalities=["text", "audio"],
                ),
            ]
        }
        # Supplementary STT fetch returns a whisper model.
        stt_models = {
            "data": [
                raw_openrouter_model(
                    model_id="openai/whisper-1",
                    name="Whisper 1",
                    input_modalities=["audio"],
                    output_modalities=["transcription"],
                ),
            ]
        }
        # Supplementary TTS fetch returns a TTS model.
        tts_models = {
            "data": [
                raw_openrouter_model(
                    model_id="openai/gpt-4o-mini-tts",
                    name="GPT-4o Mini TTS",
                    input_modalities=["text"],
                    output_modalities=["speech"],
                ),
            ]
        }

        # respx does not distinguish URLs by query params, so use side_effect.
        def openrouter_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "output_modalities=transcription" in url:
                return httpx.Response(200, json=stt_models)
            if "output_modalities=speech" in url:
                return httpx.Response(200, json=tts_models)
            return httpx.Response(200, json=main_models)

        respx.get(OPENROUTER_MODELS_URL).mock(side_effect=openrouter_handler)

        result = await refresh_models(openrouter_config, API_KEY, resources_dir)
        registry = ModelRegistry.load(resources_dir)

        assert result["model_count"] == 4
        assert registry.get("openrouter", "openai/gpt-4o") is not None
        assert registry.get("openrouter", "openai/gpt-audio") is not None
        assert registry.get("openrouter", "openai/whisper-1") is not None
        assert registry.get("openrouter", "openai/gpt-4o-mini-tts") is not None

        # Verify task types are derived correctly
        whisper = registry.get("openrouter", "openai/whisper-1")
        assert "speech_to_text" in whisper.capabilities.task_types

        tts = registry.get("openrouter", "openai/gpt-4o-mini-tts")
        assert "text_to_speech" in tts.capabilities.task_types

        # GPT Audio should have audio_generation but NOT text_to_speech
        gpt_audio = registry.get("openrouter", "openai/gpt-audio")
        assert "audio_generation" in gpt_audio.capabilities.task_types
        assert "text_to_speech" not in gpt_audio.capabilities.task_types

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_deduplicates_supplementary_models(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        """Supplementary fetches that return already-known models are deduplicated."""
        resources_dir = tmp_path / "resources"
        mock_openrouter_image_catalog()

        # Main catalog includes gpt-audio; supplementary also returns it.
        main_models = {
            "data": [
                raw_openrouter_model(
                    model_id="openai/gpt-audio",
                    name="GPT Audio",
                    output_modalities=["text", "audio"],
                ),
            ]
        }
        duplicate_stt = {
            "data": [
                # Duplicate of gpt-audio from main catalog.
                raw_openrouter_model(
                    model_id="openai/gpt-audio",
                    name="GPT Audio",
                    output_modalities=["text", "audio"],
                ),
                raw_openrouter_model(
                    model_id="openai/whisper-1",
                    name="Whisper 1",
                    input_modalities=["audio"],
                    output_modalities=["transcription"],
                ),
            ]
        }
        empty_speech: dict[str, object] = {"data": []}

        # respx does not distinguish URLs by query params, so use side_effect.
        def openrouter_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "output_modalities=transcription" in url:
                return httpx.Response(200, json=duplicate_stt)
            if "output_modalities=speech" in url:
                return httpx.Response(200, json=empty_speech)
            return httpx.Response(200, json=main_models)

        respx.get(OPENROUTER_MODELS_URL).mock(side_effect=openrouter_handler)

        result = await refresh_models(openrouter_config, API_KEY, resources_dir)

        # gpt-audio should appear only once.
        assert result["model_count"] == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_supplementary_fetch_failure_does_not_block(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        """If a supplementary fetch fails, discovery still completes with main models."""
        resources_dir = tmp_path / "resources"
        mock_openrouter_image_catalog()

        main_models = {
            "data": [
                raw_openrouter_model(model_id="openai/gpt-4o", name="GPT-4o"),
            ]
        }

        # respx does not distinguish URLs by query params, so use side_effect.
        def openrouter_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "output_modalities=transcription" in url:
                return httpx.Response(400, text="Invalid supplementary request")
            if "output_modalities=speech" in url:
                return httpx.Response(400, text="Invalid supplementary request")
            return httpx.Response(200, json=main_models)

        respx.get(OPENROUTER_MODELS_URL).mock(side_effect=openrouter_handler)

        result = await refresh_models(openrouter_config, API_KEY, resources_dir)

        # Main models are still available.
        assert result["model_count"] == 1
        registry = ModelRegistry.load(resources_dir)
        assert registry.get("openrouter", "openai/gpt-4o") is not None

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_projects_image_task_options(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        """The image task catalog projects typed option schemas into the
        provider file: an existing chat-catalog model is enriched, an
        image-API-only model is added, and the raw dump records the task
        responses."""

        resources_dir = tmp_path / "resources"
        respx.get(OPENROUTER_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        raw_openrouter_model(
                            model_id="recraft/recraft-v3",
                            name="Recraft V3",
                            output_modalities=["image"],
                        )
                    ]
                },
            )
        )
        mock_openrouter_image_catalog(
            [
                {
                    "id": "recraft/recraft-v3",
                    "name": "Recraft: Recraft V3",
                    "supported_parameters": {"n": {"type": "range", "min": 1, "max": 6}},
                },
                {
                    "id": "future-lab/pixel-marvel",
                    "name": "Pixel Marvel",
                    "architecture": {
                        "input_modalities": ["text"],
                        "output_modalities": ["image"],
                    },
                    "supported_parameters": {
                        "aspect_ratio": {"type": "enum", "values": ["1:1", "16:9"]}
                    },
                },
            ]
        )
        respx.get("https://openrouter.ai/api/v1/images/models/recraft/recraft-v3/endpoints").mock(
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
        respx.get(
            "https://openrouter.ai/api/v1/images/models/future-lab/pixel-marvel/endpoints"
        ).mock(return_value=httpx.Response(200, json={"endpoints": []}))

        result = await refresh_models(openrouter_config, API_KEY, resources_dir)

        assert result["model_count"] == 2
        written = json.loads(
            (resources_dir / "models" / "openrouter.json").read_text(encoding="utf-8")
        )
        recraft = written["models"]["recraft/recraft-v3"]
        image_options = recraft["capabilities"]["task_options"]["image_generation"]
        assert image_options["parameters"]["n"] == {"type": "range", "min": 1, "max": 6}
        assert image_options["passthrough"] == {"recraft": ["controls", "style"]}
        # The chat-catalog identity is preserved on the enriched model.
        assert recraft["name"] == "Recraft V3"

        pixel = written["models"]["future-lab/pixel-marvel"]
        assert pixel["name"] == "Pixel Marvel"
        pixel_parameters = pixel["capabilities"]["task_options"]["image_generation"]["parameters"]
        assert pixel_parameters["aspect_ratio"] == {"type": "enum", "values": ["1:1", "16:9"]}

        raw_output_data = json.loads(
            (resources_dir / "models" / "openrouter.raw.json").read_text(encoding="utf-8")
        )
        assert "/images/models" in raw_output_data["raw_task_responses"]

        # The assembled registry surfaces both models with their task options.
        registry = ModelRegistry.load(resources_dir)
        loaded = registry.get("openrouter", "future-lab/pixel-marvel")
        loaded_parameters = loaded.capabilities.task_options["image_generation"]["parameters"]
        assert loaded_parameters["aspect_ratio"]["values"] == ("1:1", "16:9")

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_image_catalog_failure_does_not_block(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        """A failing image task catalog degrades to a refresh without task
        options — the chat catalog still lands."""

        resources_dir = tmp_path / "resources"
        respx.get(OPENROUTER_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": [raw_openrouter_model(model_id="model-a", name="Model A")]},
            )
        )
        respx.get(OPENROUTER_IMAGE_MODELS_URL).mock(
            return_value=httpx.Response(400, text="Invalid task-catalog request")
        )

        result = await refresh_models(openrouter_config, API_KEY, resources_dir)

        assert result["model_count"] == 1
        written = json.loads(
            (resources_dir / "models" / "openrouter.json").read_text(encoding="utf-8")
        )
        assert "task_options" not in written["models"]["model-a"]["capabilities"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_raw_file_records_supplementary_models_once(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        """Supplementary models appear exactly once in the persisted raw payload."""
        resources_dir = tmp_path / "resources"
        mock_openrouter_image_catalog()

        main_models = {
            "data": [raw_openrouter_model(model_id="openai/gpt-4o", name="GPT-4o")],
        }
        stt_models = {
            "data": [
                raw_openrouter_model(
                    model_id="openai/whisper-1",
                    name="Whisper 1",
                    input_modalities=["audio"],
                    output_modalities=["transcription"],
                ),
            ]
        }

        def openrouter_handler(request: httpx.Request) -> httpx.Response:
            if "output_modalities=transcription" in str(request.url):
                return httpx.Response(200, json=stt_models)
            return httpx.Response(200, json=main_models)

        respx.get(OPENROUTER_MODELS_URL).mock(side_effect=openrouter_handler)

        await refresh_models(openrouter_config, API_KEY, resources_dir)

        raw_output_data = json.loads(
            (resources_dir / "models" / "openrouter.raw.json").read_text(encoding="utf-8")
        )
        raw_ids = [model["id"] for model in raw_output_data["raw_response"]["data"]]

        # The supplementary STT model must not be duplicated in the raw payload.
        assert raw_ids.count("openai/whisper-1") == 1
        assert sorted(raw_ids) == ["openai/gpt-4o", "openai/whisper-1"]


CATALOG_FIXTURE = Path(__file__).parent / "fixtures" / "models_dev_catalog.json"
