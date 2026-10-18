"""OpenAI Platform catalog admission and Codex catalog normalization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.models.models import (
    Capabilities,
    Model,
    ReasoningCapabilities,
    text_embedding_capabilities,
)
from core.providers._openai_constants import (
    DISCOVERY_JSON_PARAMETER_NAMES,
    DISCOVERY_REASONING_PARAMETER_NAMES,
    DISCOVERY_TOOL_PARAMETER_NAMES,
)
from core.providers.openai_compatible import OpenAICompatibleAdapter

_PLATFORM_EMBEDDING_PREFIX = "text-embedding-"


def _is_platform_chat_or_embedding_model(model_id: str) -> bool:
    """Admit new ids except recognizable non-chat endpoint families.

    Platform ``/models`` reports ids, not endpoint compatibility. These task
    families need their own normalization and request builders; they must not
    acquire chat capabilities from the sparse-listing default. Unknown names
    remain eligible. Fine-tunes inherit the task of their base Model.
    """

    base_id = model_id.split(":", 2)[1] if model_id.startswith("ft:") else model_id
    if base_id.startswith(_PLATFORM_NON_CHAT_PREFIXES) or base_id in {
        "ada",
        "babbage",
        "curie",
        "davinci",
    }:
        return False
    # GPT chat audio is a Chat Completions Model; only these task-specific
    # components identify incompatible speech, realtime or legacy endpoints.
    return not (
        base_id.startswith("gpt-")
        and {"realtime", "transcribe", "tts", "instruct"}.intersection(base_id.split("-"))
    )


_PLATFORM_NON_CHAT_PREFIXES = (
    "dall-e-",
    "gpt-image-",
    "chatgpt-image-",
    "gpt-realtime",
    "gpt-live",
    "gpt-transcribe",
    "tts-",
    "whisper-",
    "sora-",
    "omni-moderation-",
    "text-moderation-",
    "text-davinci-",
    "text-curie-",
    "text-babbage-",
    "text-ada-",
    "code-davinci-",
    "code-cushman-",
    "davinci-",
    "babbage-",
    "curie-",
    "ada-",
    "text-search-",
    "text-similarity-",
    "code-search-",
)


def _normalize_catalog_raw(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    normalized = dict(raw)
    if not _optional_string(normalized.get("id")):
        slug = _optional_string(normalized.get("slug")) or _optional_string(normalized.get("model"))
        if slug:
            normalized["id"] = slug
    if not _optional_string(normalized.get("name")):
        display_name = _optional_string(normalized.get("display_name")) or _optional_string(
            normalized.get("title")
        )
        if display_name:
            normalized["name"] = display_name
    return normalized


def _optional_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _optional_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_set(value: Any) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str) and item)


def _subscription_capability_supported(
    raw_parameters: frozenset[str],
    default_value: bool,
    metadata_sources: tuple[Mapping[str, Any], ...],
    explicit_keys: tuple[str, ...],
    matching_parameters: frozenset[str],
) -> bool:
    explicit_value = _first_optional_bool(metadata_sources, explicit_keys)
    if explicit_value is not None:
        return explicit_value
    if raw_parameters:
        return bool(raw_parameters & matching_parameters)
    return default_value


def _subscription_supported_parameters(
    raw_parameters: frozenset[str],
    tools_supported: bool,
    json_supported: bool,
    reasoning_supported: bool,
) -> list[str]:
    supported_parameters: list[str] = []
    sparse_catalog = not raw_parameters
    if tools_supported:
        supported_parameters.append("tools")
    if json_supported:
        supported_parameters.append("response_format")
    if reasoning_supported:
        supported_parameters.append("reasoning")
    if tools_supported and (sparse_catalog or "parallel_tool_calls" in raw_parameters):
        supported_parameters.append("parallel_tool_calls")
    return supported_parameters


def _first_optional_bool(
    metadata_sources: tuple[Mapping[str, Any], ...],
    keys: tuple[str, ...],
) -> bool | None:
    for source in metadata_sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, bool):
                return value
            if key == "reasoning" and isinstance(value, Mapping):
                supported = value.get("supported")
                if isinstance(supported, bool):
                    return supported
    return None


def normalize_catalog_entry(
    raw: Mapping[str, Any], defaults: Mapping[str, Any] | None = None
) -> Model:
    """Normalize a Codex or Platform chat/embedding listing entry."""

    model_id = raw.get("id")
    if isinstance(model_id, str) and model_id.startswith(_PLATFORM_EMBEDDING_PREFIX):
        # Only the third generation supports shortened vectors (``dimensions``).
        return Model(
            model_id=model_id,
            name=model_id,
            capabilities=text_embedding_capabilities(
                ("dimensions",) if model_id.startswith("text-embedding-3-") else ()
            ),
            context_window=None,
            max_output_tokens=None,
        )
    return OpenAICompatibleAdapter.normalize_catalog_entry(_normalize_catalog_raw(raw), defaults)


def normalize_subscription_catalog_entry(raw: Mapping[str, Any], base_model: Model) -> Model:
    """Project Codex capability metadata over the normalized catalog identity."""
    if "text_embedding" in base_model.capabilities.task_types:
        return base_model
    normalized_raw = _normalize_catalog_raw(raw)
    capabilities = _optional_mapping(normalized_raw.get("capabilities"))
    supports = _optional_mapping(capabilities.get("supports"))
    raw_parameters = _string_set(normalized_raw.get("supported_parameters"))
    tools_supported = _subscription_capability_supported(
        raw_parameters,
        base_model.capabilities.tools,
        (normalized_raw, capabilities, supports),
        ("supports_tools", "tools", "tool_calls", "function_calling"),
        DISCOVERY_TOOL_PARAMETER_NAMES,
    )
    json_supported = _subscription_capability_supported(
        raw_parameters,
        base_model.capabilities.json_mode or not raw_parameters,
        (normalized_raw, capabilities, supports),
        (
            "supports_json_mode",
            "json_mode",
            "supports_structured_outputs",
            "structured_outputs",
        ),
        DISCOVERY_JSON_PARAMETER_NAMES,
    )
    reasoning_supported = _subscription_capability_supported(
        raw_parameters,
        base_model.capabilities.reasoning.supported or not raw_parameters,
        (normalized_raw, capabilities, supports),
        ("supports_reasoning", "reasoning_supported", "reasoning"),
        DISCOVERY_REASONING_PARAMETER_NAMES,
    )

    return Model(
        model_id=base_model.model_id,
        name=base_model.name,
        capabilities=Capabilities(
            vision=base_model.capabilities.vision,
            tools=tools_supported,
            json_mode=json_supported,
            reasoning=ReasoningCapabilities(supported=reasoning_supported),
            input_modalities=base_model.capabilities.input_modalities,
            output_modalities=base_model.capabilities.output_modalities,
            supported_parameters=tuple(
                _subscription_supported_parameters(
                    raw_parameters,
                    tools_supported,
                    json_supported,
                    reasoning_supported,
                )
            ),
        ),
        context_window=base_model.context_window,
        max_output_tokens=base_model.max_output_tokens,
        metadata=base_model.metadata,
    )
