"""Kimi Chat Completions adapter for Coding Plan and Platform Connections.

Per-Model reasoning, sampling, output-limit, replay, media and request-size rules
live in ``resources/wire/kimi.json``; this Adapter owns the catalog and video parts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, override

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.openai_compatible import OpenAICompatibleAdapter

KIMI_CODING_MODE = "coding_plan"
KIMI_K2_RECOMMENDED_MAX_OUTPUT_TOKENS = 32768
KIMI_COMMON_SUPPORTED_PARAMETERS = (
    "max_completion_tokens",
    "parallel_tool_calls",
    "prompt_cache_key",
    "response_format",
    "thinking",
    "tools",
)

KIMI_MODEL_FACTS: dict[str, dict[str, Any]] = {
    "kimi-k3": {
        "name": "Kimi K3",
        "context_window": 1048576,
        "max_output_tokens": 131072,
        "input_modalities": ("text", "image", "video"),
        "reasoning_control": "levels",
        "reasoning_levels": ("low", "high", "max"),
        "supported_parameters": (*KIMI_COMMON_SUPPORTED_PARAMETERS, "reasoning_effort"),
    },
    "k3": {
        "name": "Kimi K3",
        "context_window": 1048576,
        "max_output_tokens": 131072,
        "input_modalities": ("text", "image", "video"),
        "reasoning_control": "levels",
        "reasoning_levels": ("low", "high", "max"),
        "supported_parameters": (*KIMI_COMMON_SUPPORTED_PARAMETERS, "reasoning_effort"),
    },
    "k3-256k": {
        "name": "Kimi K3 256K",
        "context_window": 262144,
        "max_output_tokens": 131072,
        "input_modalities": ("text", "image"),
        "reasoning_control": "levels",
        "reasoning_levels": ("low", "high", "max"),
        "supported_parameters": (*KIMI_COMMON_SUPPORTED_PARAMETERS, "reasoning_effort"),
    },
    "kimi-k2.6": {
        "name": "Kimi K2.6",
        "context_window": 262144,
        "max_output_tokens": KIMI_K2_RECOMMENDED_MAX_OUTPUT_TOKENS,
        "input_modalities": ("text", "image", "video"),
        "reasoning_control": "on_off",
        "reasoning_levels": (),
        "supported_parameters": KIMI_COMMON_SUPPORTED_PARAMETERS,
    },
    "kimi-k2.7-code": {
        "name": "Kimi K2.7 Code",
        "context_window": 262144,
        "max_output_tokens": KIMI_K2_RECOMMENDED_MAX_OUTPUT_TOKENS,
        "input_modalities": ("text", "image", "video"),
        "reasoning_control": None,
        "reasoning_levels": (),
        "supported_parameters": KIMI_COMMON_SUPPORTED_PARAMETERS,
    },
    "kimi-k2.7-code-highspeed": {
        "name": "Kimi K2.7 Code HighSpeed",
        "context_window": 262144,
        "max_output_tokens": KIMI_K2_RECOMMENDED_MAX_OUTPUT_TOKENS,
        "input_modalities": ("text", "image", "video"),
        "reasoning_control": None,
        "reasoning_levels": (),
        "supported_parameters": KIMI_COMMON_SUPPORTED_PARAMETERS,
    },
    "kimi-for-coding": {
        "name": "Kimi K2.7 Code",
        "context_window": 262144,
        "max_output_tokens": KIMI_K2_RECOMMENDED_MAX_OUTPUT_TOKENS,
        "input_modalities": ("text", "image", "video"),
        "reasoning_control": None,
        "reasoning_levels": (),
        "supported_parameters": KIMI_COMMON_SUPPORTED_PARAMETERS,
    },
    "kimi-for-coding-highspeed": {
        "name": "Kimi K2.7 Code HighSpeed",
        "context_window": 262144,
        "max_output_tokens": KIMI_K2_RECOMMENDED_MAX_OUTPUT_TOKENS,
        "input_modalities": ("text", "image", "video"),
        "reasoning_control": None,
        "reasoning_levels": (),
        "supported_parameters": KIMI_COMMON_SUPPORTED_PARAMETERS,
    },
}


class KimiAdapter(OpenAICompatibleAdapter):
    """Kimi's Chat wire: stable prompt-cache keys, video parts and the Model catalog."""

    @override
    def request_context_kwargs(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
        prompt_cache_affinity_id: str | None = None,
    ) -> dict[str, Any]:
        """Give Coding Plan requests the stable cache key required for good quota use."""

        del project_id
        conversation_id = f"{agent_id}:{session_id}"
        return {"prompt_cache_key": prompt_cache_affinity_id or conversation_id}

    @override
    def _format_user_content_part(self, part: Any) -> dict[str, Any]:
        if isinstance(part, dict) and part.get("type") == "media":
            base64_data = part.get("base64")
            media_type = part.get("media_type")
            if (
                isinstance(base64_data, str)
                and isinstance(media_type, str)
                and media_type.startswith("video/")
            ):
                return {
                    "type": "video_url",
                    "video_url": {"url": f"data:{media_type};base64,{base64_data}"},
                }
        return super()._format_user_content_part(part)

    @classmethod
    @override
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        base_model = super().normalize_catalog_entry(raw, defaults)
        facts = KIMI_MODEL_FACTS.get(base_model.model_id)
        if facts is None:
            input_modalities = list(base_model.capabilities.input_modalities)
            if raw.get("supports_image_in") is True and "image" not in input_modalities:
                input_modalities.append("image")
            if raw.get("supports_video_in") is True and "video" not in input_modalities:
                input_modalities.append("video")
            reasoning_supported = (
                True
                if raw.get("supports_reasoning") is True
                else base_model.capabilities.reasoning.supported
            )
            return Model(
                model_id=base_model.model_id,
                name=base_model.name,
                capabilities=Capabilities(
                    vision="image" in input_modalities,
                    tools=base_model.capabilities.tools,
                    json_mode=base_model.capabilities.json_mode,
                    reasoning=ReasoningCapabilities(supported=reasoning_supported),
                    input_modalities=tuple(input_modalities),
                    output_modalities=base_model.capabilities.output_modalities,
                    supported_parameters=base_model.capabilities.supported_parameters,
                ),
                context_window=base_model.context_window,
                max_output_tokens=base_model.max_output_tokens,
            )

        known_input_modalities = tuple(facts["input_modalities"])
        return Model(
            model_id=base_model.model_id,
            name=str(facts["name"]),
            capabilities=Capabilities(
                vision="image" in known_input_modalities,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(
                    supported=True,
                    control=facts["reasoning_control"],
                    levels=tuple(facts["reasoning_levels"]),
                ),
                input_modalities=known_input_modalities,
                output_modalities=("text",),
                supported_parameters=tuple(facts["supported_parameters"]),
            ),
            context_window=int(facts["context_window"]),
            max_output_tokens=int(facts["max_output_tokens"]),
        )
