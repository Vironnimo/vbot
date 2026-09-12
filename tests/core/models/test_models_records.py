"""Tests for models records."""



from dataclasses import FrozenInstanceError

import pytest

from core.models.models import (
    Capabilities,
    Model,
    ReasoningCapabilities,
    derive_model_task_types,
)
from tests.core.models.models_test_support import (
    _clear_registry_cache as _clear_registry_cache,
)


# ---------------------------------------------------------------------------
# ReasoningCapabilities
# ---------------------------------------------------------------------------
class TestReasoningCapabilities:
    def test_fields(self):
        caps = ReasoningCapabilities(supported=True)
        assert caps.supported is True

    def test_typed_control_fields_default_to_absent(self):
        """The minimal ``supported``-only form leaves the control fields unset,
        so a ``{"supported": true}`` model with no projected ladder is valid."""

        caps = ReasoningCapabilities(supported=True)

        assert caps.control is None
        assert caps.levels == ()
        assert caps.budget_max is None

    def test_levels_control_carries_ladder(self):
        caps = ReasoningCapabilities(
            supported=True,
            control="levels",
            levels=("low", "medium", "high"),
        )

        assert caps.control == "levels"
        assert caps.levels == ("low", "medium", "high")
        assert caps.budget_max is None

    def test_budget_control_carries_max(self):
        caps = ReasoningCapabilities(supported=True, control="budget", budget_max=32000)

        assert caps.control == "budget"
        assert caps.budget_max == 32000
        assert caps.levels == ()

    def test_on_off_control(self):
        caps = ReasoningCapabilities(supported=True, control="on_off")

        assert caps.control == "on_off"
        assert caps.levels == ()
        assert caps.budget_max is None

    def test_frozen(self):
        caps = ReasoningCapabilities(supported=True)
        with pytest.raises(FrozenInstanceError):
            caps.supported = False  # type: ignore[misc]

    def test_typed_control_fields_frozen(self):
        caps = ReasoningCapabilities(supported=True, control="levels", levels=("high",))
        with pytest.raises(FrozenInstanceError):
            caps.control = "on_off"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------
class TestCapabilities:
    def test_fields(self):
        reasoning = ReasoningCapabilities(supported=True)
        caps = Capabilities(
            vision=True,
            tools=False,
            json_mode=True,
            reasoning=reasoning,
            input_modalities=("Text", "Image", "image"),
            output_modalities=("Text", "Audio"),
            supported_parameters=("response_format", "tools", "tools"),
        )
        assert caps.vision is True
        assert caps.tools is False
        assert caps.json_mode is True
        assert caps.reasoning is reasoning
        assert caps.input_modalities == ("text", "image")
        assert caps.output_modalities == ("text", "audio")
        assert caps.supported_parameters == ("response_format", "tools")
        assert caps.supported_voices == ()
        assert caps.task_types == (
            "chat",
            "text_output",
            "image_input",
            "image_understanding",
            "audio_generation",
        )

    def test_supported_voices_default_to_empty_tuple(self):
        caps = Capabilities(
            vision=False,
            tools=False,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        )

        assert caps.supported_voices == ()

    def test_supported_voices_normalizes_dedupes_and_sorts(self):
        caps = Capabilities(
            vision=False,
            tools=False,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
            supported_voices=(" af_sky ", "af_aoede", "af_sky", ""),
        )

        assert caps.supported_voices == ("af_aoede", "af_sky")

    def test_legacy_vision_derives_text_image_input(self):
        caps = Capabilities(
            vision=True,
            tools=False,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        )

        assert caps.input_modalities == ("text", "image")
        assert caps.output_modalities == ("text",)
        assert "image_understanding" in caps.task_types

    def test_derives_generation_task_types(self):
        assert derive_model_task_types(("text", "image"), ("text", "image")) == (
            "chat",
            "text_output",
            "image_input",
            "image_understanding",
            "image_generation",
        )
        # Generic "audio" output does NOT imply text_to_speech —
        # only "speech" output does.
        assert derive_model_task_types(("text",), ("audio",)) == ("audio_generation",)
        # Dedicated TTS models have "speech" in output_modalities.
        assert derive_model_task_types(("text",), ("speech",)) == (
            "audio_generation",
            "text_to_speech",
        )
        # Dedicated STT models have "transcription" in output_modalities.
        # They also get audio_input since they accept audio.
        assert derive_model_task_types(("audio",), ("transcription",)) == (
            "text_output",
            "audio_input",
            "speech_to_text",
        )
        # Multimodal models with audio input and text output get speech_to_text.
        assert derive_model_task_types(("text", "audio"), ("text",)) == (
            "chat",
            "text_output",
            "audio_input",
            "speech_to_text",
        )
        # Dedicated embedding models have "embeddings" in output_modalities.
        # They are NOT tagged chat/text_output — their output is a vector,
        # not text. Mirror of the "speech" → text_to_speech alias.
        assert derive_model_task_types(("text",), ("embeddings",)) == ("text_embedding",)

    def test_frozen(self):
        reasoning = ReasoningCapabilities(supported=False)
        caps = Capabilities(
            vision=False,
            tools=False,
            json_mode=False,
            reasoning=reasoning,
        )
        with pytest.raises(FrozenInstanceError):
            caps.vision = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
class TestModel:
    def test_fields(self):
        reasoning = ReasoningCapabilities(supported=True)
        capabilities = Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=reasoning,
        )
        model = Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=capabilities,
            context_window=128000,
            max_output_tokens=16384,
        )
        assert model.model_id == "gpt-5.2"
        assert model.name == "GPT-5.2"
        assert model.capabilities is capabilities
        assert model.context_window == 128000
        assert model.max_output_tokens == 16384
        assert model.metadata == {}

    def test_context_window_is_optional(self):
        # A missing context window stays missing in the data rather than being
        # faked with a constant; the read-side default chain fills it at use time.
        model = Model(
            model_id="custom-model",
            name="Custom Model",
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=False),
            ),
            context_window=None,
            max_output_tokens=None,
        )

        assert model.context_window is None

    def test_family_defaults_to_empty_string(self):
        model = Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=Capabilities(
                vision=True,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=128000,
            max_output_tokens=16384,
        )

        assert model.family == ""

    def test_family_is_first_class_field(self):
        model = Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=Capabilities(
                vision=True,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=128000,
            max_output_tokens=16384,
            family="gpt-5.2",
        )

        assert model.family == "gpt-5.2"

    def test_metadata_field_is_optional_and_immutable(self):
        capabilities = Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=True),
        )
        model = Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=capabilities,
            context_window=128000,
            max_output_tokens=16384,
            metadata={"github_copilot": {"supported_endpoints": ["/responses"]}},
        )

        assert model.metadata["github_copilot"]["supported_endpoints"] == ("/responses",)
        with pytest.raises(TypeError):
            model.metadata["github_copilot"] = {}  # type: ignore[index]

    def test_frozen(self):
        capabilities = Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=True),
        )
        model = Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=capabilities,
            context_window=128000,
            max_output_tokens=16384,
        )
        with pytest.raises(FrozenInstanceError):
            model.model_id = "changed"  # type: ignore[misc]

    def test_nested_capabilities_frozen(self):
        capabilities = Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=True),
        )
        model = Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=capabilities,
            context_window=128000,
            max_output_tokens=16384,
        )
        with pytest.raises(FrozenInstanceError):
            model.capabilities.vision = False  # type: ignore[misc]

    def test_nested_reasoning_frozen(self):
        reasoning = ReasoningCapabilities(supported=True)
        capabilities = Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=reasoning,
        )
        model = Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=capabilities,
            context_window=128000,
            max_output_tokens=16384,
        )
        with pytest.raises(FrozenInstanceError):
            model.capabilities.reasoning.supported = False  # type: ignore[misc]

    def test_embedding_model_derives_text_embedding_task_type(self):
        """A Model with output_modalities=("embeddings",) and no explicit
        task_types derives task_types=("text_embedding",). Mirrors the
        speech → text_to_speech alias.
        """

        capabilities = Capabilities(
            vision=False,
            tools=False,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
            output_modalities=("embeddings",),
        )
        model = Model(
            model_id="text-embedding-3-small",
            name="Text Embedding 3 Small",
            capabilities=capabilities,
            context_window=8192,
            max_output_tokens=None,
        )

        assert model.capabilities.task_types == ("text_embedding",)
