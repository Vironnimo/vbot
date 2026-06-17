"""OpenRouter provider adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.openai_compatible import (
    OpenAICompatibleAdapter,
    _parse_optional_int,
    _read_mapping,
    _read_string,
    _read_string_list,
)
from core.providers.reasoning import (
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_OFF,
    REASONING_INTENT_ON,
    ReasoningIntent,
    model_reasoning_budget_max,
    model_reasoning_control,
    model_reasoning_levels,
    resolve_reasoning_intent,
)

OPENROUTER_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
# OpenRouter's documented off-shape for a native thinking toggle (``on_off``
# models). An effort-spelled-off wire (``levels``/unknown control with a ``none``
# rung) keeps the byte-identical ``{"effort": "none"}`` instead — see the render.
OPENROUTER_REASONING_OFF = {"enabled": False}
OPENROUTER_NONE_EFFORT = "none"

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
            # OpenRouter reports ``context_length: 0`` for non-chat models
            # (transcription, image/video generation). A ``0`` is no usable
            # window, so it normalizes to ``None`` (honest "unknown") rather than
            # a fake fact; the read-side default chain fills it at use time.
            context_window=_parse_optional_int(raw.get("context_length")) or None,
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
        reasoning_supported = self._model_reasoning_supported(model_id)
        if reasoning_supported is False:
            payload.pop("reasoning", None)
            payload.pop("include_reasoning", None)
            return payload

        # Snap against the effective per-model ladder when the DB carries one;
        # the provider-global constant is only the floor for a model without a
        # feed ladder.
        intent = resolve_reasoning_intent(
            supported=reasoning_supported,
            control=model_reasoning_control(self._model_lookup, model_id),
            levels=(
                model_reasoning_levels(self._model_lookup, model_id)
                or tuple(OPENROUTER_REASONING_EFFORTS)
            ),
            effort=thinking_effort or reasoning_effort,
            budget_max=model_reasoning_budget_max(self._model_lookup, model_id),
            # OpenRouter resolves a budget from the effort internally, so vBot
            # deliberately never sends a token budget here (no ``max_tokens``).
            max_tokens=None,
        )
        _render_openrouter_reasoning(payload, intent)
        return payload


def _render_openrouter_reasoning(payload: dict[str, Any], intent: ReasoningIntent) -> None:
    """Render a reasoning intent onto an OpenRouter payload.

    OpenRouter speaks ``reasoning: {effort}`` / ``{enabled}``. An ``effort``
    intent maps straight through; ``budget`` also renders as an effort (OpenRouter
    maps effort→budget internally, so no token budget is sent); ``on`` toggles
    ``enabled: true``. ``off`` keeps the byte-identical ``{"effort": "none"}`` for
    an effort-spelled-off wire (``effort_level == "none"``) and falls back to the
    documented ``{"enabled": false}`` toggle otherwise; ``default`` omits the
    field entirely.
    """

    if intent.kind == REASONING_INTENT_ON:
        payload["reasoning"] = {"enabled": True}
        payload["include_reasoning"] = True
    elif intent.kind in (REASONING_INTENT_EFFORT, REASONING_INTENT_BUDGET):
        if intent.effort_level is not None:
            payload["reasoning"] = {"effort": intent.effort_level}
            payload["include_reasoning"] = True
    elif intent.kind == REASONING_INTENT_OFF:
        if intent.effort_level == OPENROUTER_NONE_EFFORT:
            payload["reasoning"] = {"effort": OPENROUTER_NONE_EFFORT}
            payload["include_reasoning"] = True
        else:
            payload["reasoning"] = dict(OPENROUTER_REASONING_OFF)


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
