"""OpenRouter provider adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.openai_compatible import (
    OpenAICompatibleAdapter,
    _parse_optional_int,
    _read_int,
    _read_mapping,
    _read_string,
    _read_string_list,
)
from core.providers.reasoning import closest_supported_effort, model_reasoning_levels

OPENROUTER_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}

# OpenRouter uses the ``output_modalities`` query parameter to filter models
# by their output capability.  The default ``/models`` call returns only
# text-output models, so every non-text-output catalog family needs its own
# supplementary fetch: ``transcription`` (STT), ``speech`` (TTS), ``image``
# (image generation), ``audio`` (generic audio generation), ``video`` (video
# generation), and ``embeddings`` (text embedding).  Without these filters
# the corresponding task types (``video_generation``, ``text_embedding``,
# etc.) stay empty even though OpenRouter publishes those models.
SUPPLEMENTARY_OUTPUT_MODALITIES = (
    "transcription",
    "speech",
    "image",
    "audio",
    "video",
    "embeddings",
)


class OpenRouterAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter with OpenRouter-specific behavior."""

    @classmethod
    def supplementary_discovery_params(cls) -> list[dict[str, str]]:
        """Return query-parameter dicts for supplementary model fetches.

        The OpenRouter ``/models`` endpoint defaults to returning only
        text-output models.  Dedicated STT, TTS, image-, audio-, video-,
        and text-embedding-generation models are excluded unless the
        ``output_modalities`` query parameter is set to ``transcription``,
        ``speech``, ``image``, ``audio``, ``video``, or ``embeddings``
        respectively.

        Each dict returned here is appended as query parameters to the
        models endpoint URL during discovery, and the resulting models
        are merged into the main catalog (deduplicated by ``model_id``).
        """
        return [{"output_modalities": m} for m in SUPPLEMENTARY_OUTPUT_MODALITIES]

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one OpenRouter ``/models`` entry into a vBot ``Model``."""

        architecture = _read_mapping(raw, "architecture")
        top_provider = _read_mapping(raw, "top_provider")
        supported_parameters = _read_string_list(raw, "supported_parameters")
        input_modalities = _read_string_list(architecture, "input_modalities")
        output_modalities = (
            _read_string_list(architecture, "output_modalities")
            if "output_modalities" in architecture
            else ["text"]
        )
        # OpenRouter publishes ``supported_voices`` as a top-level array of
        # plain voice-id strings on TTS-capable models (and empty/present on
        # other models). Defensive default keeps the field safe to read for
        # every model entry — providers omit it, not raise, when irrelevant.
        supported_voices = _read_optional_string_list(raw, "supported_voices")

        return Model(
            model_id=_read_string(raw, "id"),
            name=_read_string(raw, "name"),
            capabilities=Capabilities(
                vision="image" in input_modalities,
                tools="tools" in supported_parameters,
                json_mode=(
                    "response_format" in supported_parameters
                    or "structured_outputs" in supported_parameters
                ),
                reasoning=ReasoningCapabilities(
                    supported=(
                        "reasoning" in supported_parameters
                        or "include_reasoning" in supported_parameters
                    ),
                ),
                input_modalities=tuple(input_modalities),
                output_modalities=tuple(output_modalities),
                supported_parameters=tuple(supported_parameters),
                supported_voices=tuple(supported_voices),
            ),
            context_window=_read_int(raw, "context_length"),
            max_output_tokens=_parse_optional_int(top_provider.get("max_completion_tokens")),
            metadata=_openrouter_runtime_metadata(architecture),
        )

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build an OpenRouter payload with OpenRouter reasoning parameters."""

        thinking_effort = kwargs.pop("thinking_effort", "")
        reasoning_effort = kwargs.pop("reasoning_effort", "")
        payload = super()._build_payload(messages, model_id, **kwargs)
        if self._model_reasoning_supported(model_id) is False:
            payload.pop("reasoning", None)
            payload.pop("include_reasoning", None)
            return payload

        # Snap against the effective per-model ladder when the DB carries one;
        # the provider-global constant is only the floor for a model without a
        # feed ladder.
        ladder = (
            model_reasoning_levels(self._model_lookup, model_id) or OPENROUTER_REASONING_EFFORTS
        )
        supported_effort = closest_supported_effort(
            thinking_effort or reasoning_effort,
            ladder,
        )
        if supported_effort is not None:
            payload["reasoning"] = {"effort": supported_effort}
            payload["include_reasoning"] = True
        return payload


def _openrouter_runtime_metadata(architecture: Mapping[str, Any]) -> Mapping[str, Any]:
    modality = architecture.get("modality")
    if isinstance(modality, str) and modality:
        return {"openrouter": {"modality": modality}}
    return {}


def _read_optional_string_list(data: Mapping[str, Any], key: str) -> list[str]:
    """Read an optional list-of-strings field, returning ``[]`` when absent or malformed.

    Used for OpenRouter fields that are present-but-empty on most models (such as
    ``supported_voices`` on non-TTS models) where a missing or wrong-shaped value
    is a normal "not applicable" signal rather than a hard schema error.
    """

    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return []
    return value
