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
    text_embedding_capabilities,
)
from core.providers._ollama_constants import (
    _CAPABILITY_COMPLETION,
    _CAPABILITY_EMBEDDING,
    _CAPABILITY_THINKING,
    _CAPABILITY_TOOLS,
    _CAPABILITY_VISION,
    OLLAMA_GPT_OSS_EFFORTS,
    OLLAMA_METADATA_KEY,
)


def _enrich_from_show(model: Model, show_response: Mapping[str, Any]) -> Model:
    """Return *model* enriched with capabilities and window from ``/api/show``."""

    return Model(
        model_id=model.model_id,
        name=model.name,
        capabilities=_ollama_capabilities(
            model.model_id, _capability_names(show_response.get("capabilities"))
        ),
        context_window=_context_window_from_show(show_response),
        max_output_tokens=model.max_output_tokens,
        family=model.family,
        metadata=_ollama_enriched_metadata(model),
        connections=model.connections,
    )


def _capability_names(raw_capabilities: Any) -> set[str]:
    """Return the capability names of an ``/api/tags`` or ``/api/show`` entry."""

    if not isinstance(raw_capabilities, list):
        return set()
    return {name for name in raw_capabilities if isinstance(name, str)}


def _ollama_capabilities(model_id: str, capability_names: set[str]) -> Capabilities:
    """Project Ollama capability names into vBot capabilities.

    ``embedding`` marks a Model that returns vectors through the embeddings
    API. Without ``completion`` it is an embedding Model only, never offered as
    a chat Model; an entry without capability names stays a conservative
    text Model until ``/api/show`` reports them.
    """

    embedding = _CAPABILITY_EMBEDDING in capability_names
    if embedding and _CAPABILITY_COMPLETION not in capability_names:
        return text_embedding_capabilities()
    vision = _CAPABILITY_VISION in capability_names
    return Capabilities(
        vision=vision,
        tools=_CAPABILITY_TOOLS in capability_names,
        json_mode=False,
        reasoning=_ollama_reasoning_capabilities(
            model_id, _CAPABILITY_THINKING in capability_names
        ),
        input_modalities=("text", "image") if vision else ("text",),
        output_modalities=("text", "embeddings") if embedding else ("text",),
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
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return value
