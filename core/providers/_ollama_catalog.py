"""Ollama catalog."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.models.models import (
    REASONING_CONTROL_LEVELS,
    REASONING_CONTROL_ON_OFF,
    Capabilities,
    Model,
    ReasoningCapabilities,
)
from core.providers._ollama_constants import (
    _CAPABILITY_THINKING,
    _CAPABILITY_TOOLS,
    _CAPABILITY_VISION,
    OLLAMA_GPT_OSS_EFFORTS,
    OLLAMA_METADATA_KEY,
)


def _enrich_from_show(model: Model, show_response: Mapping[str, Any]) -> Model:
    """Return *model* enriched with capabilities and window from ``/api/show``."""

    capabilities_list = show_response.get("capabilities")
    capability_names = (
        {name for name in capabilities_list if isinstance(name, str)}
        if isinstance(capabilities_list, list)
        else set()
    )

    tools = _CAPABILITY_TOOLS in capability_names
    vision = _CAPABILITY_VISION in capability_names
    thinking = _CAPABILITY_THINKING in capability_names

    reasoning = _ollama_reasoning_capabilities(model.model_id, thinking)
    input_modalities = ("text", "image") if vision else ("text",)

    return Model(
        model_id=model.model_id,
        name=model.name,
        capabilities=Capabilities(
            vision=vision,
            tools=tools,
            json_mode=False,
            reasoning=reasoning,
            input_modalities=input_modalities,
            output_modalities=("text",),
        ),
        context_window=_context_window_from_show(show_response),
        max_output_tokens=model.max_output_tokens,
        family=model.family,
        metadata=_ollama_enriched_metadata(model),
        connections=model.connections,
    )


def _ollama_reasoning_capabilities(
    model_id: str,
    thinking: bool,
) -> ReasoningCapabilities:
    if not thinking:
        return ReasoningCapabilities(supported=False)
    if _is_gpt_oss_model(model_id):
        return ReasoningCapabilities(
            supported=True,
            control=REASONING_CONTROL_LEVELS,
            levels=OLLAMA_GPT_OSS_EFFORTS,
        )
    return ReasoningCapabilities(supported=True, control=REASONING_CONTROL_ON_OFF)


def _is_gpt_oss_model(model_id: str) -> bool:
    return model_id.split("::", 1)[0].split(":", 1)[0].lower() == "gpt-oss"


def _ollama_enriched_metadata(model: Model) -> dict[str, Any]:
    metadata = {
        key: dict(value) if isinstance(value, Mapping) else value
        for key, value in model.metadata.items()
    }
    ollama_metadata = metadata.get(OLLAMA_METADATA_KEY)
    provider_metadata = dict(ollama_metadata) if isinstance(ollama_metadata, Mapping) else {}
    metadata[OLLAMA_METADATA_KEY] = provider_metadata
    return metadata


def _context_window_from_show(show_response: Mapping[str, Any]) -> int | None:
    """Read the model's theoretical max context from ``model_info``.

    The window lives under the key ``"<architecture>.context_length"`` where
    ``<architecture>`` is ``model_info["general.architecture"]`` (e.g.
    ``mistral3.context_length``). Only that exact key is read — a suffix scan
    would wrongly match ``*.rope.scaling.original_context_length``.
    """

    model_info = show_response.get("model_info")
    if not isinstance(model_info, Mapping):
        return None
    architecture = model_info.get("general.architecture")
    if not isinstance(architecture, str) or not architecture:
        return None
    value = model_info.get(f"{architecture}.context_length")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value
