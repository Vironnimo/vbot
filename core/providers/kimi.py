"""Kimi Chat Completions adapter for Coding Plan and Platform Connections."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.adapter import ModelLookup
from core.providers.errors import ProviderError
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.reasoning import (
    REASONING_REPLAY_CURRENT_RUN,
    REASONING_REPLAY_FULL_HISTORY,
    REASONING_REPLAY_NONE,
    ReasoningReplayPolicy,
    model_reasoning_supported,
    normalize_thinking_effort,
    remove_reasoning_kwargs,
)
from core.providers.token_getter import TokenGetter

KIMI_CODING_MODE = "coding_plan"
KIMI_K3_MODEL_IDS = frozenset({"kimi-k3", "k3", "k3-256k"})
KIMI_K26_MODEL_IDS = frozenset({"kimi-k2.6"})
KIMI_K27_MODEL_IDS = frozenset(
    {
        "kimi-k2.7-code",
        "kimi-k2.7-code-highspeed",
        "kimi-for-coding",
        "kimi-for-coding-highspeed",
    }
)
KIMI_IMAGE_VIDEO_MEDIA_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "video/mp4",
        "video/quicktime",
        "video/webm",
    }
)
KIMI_PLATFORM_MAX_REQUEST_BODY_BYTES = 100_000_000
KIMI_CODING_MAX_REQUEST_BODY_BYTES = 80 * 1024 * 1024
KIMI_K2_RECOMMENDED_MAX_OUTPUT_TOKENS = 32768
KIMI_REASONING_REQUEST_KEYS = (
    "reasoning",
    "include_reasoning",
    "thinking",
    "output_config",
)
KIMI_SAMPLING_REQUEST_KEYS = ("temperature", "top_p", "n")
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
    """Apply Kimi's strict per-Model reasoning and multimodal contracts."""

    def __init__(
        self,
        config: ProviderConfig,
        token_getter: TokenGetter | str,
        base_url: str | None = None,
        auth_config: AuthConfig | None = None,
        model_lookup: ModelLookup | None = None,
        debug_recorder: ProviderDebugRecorder | None = None,
        *,
        connection_mode: str | None = None,
    ) -> None:
        super().__init__(
            config,
            token_getter,
            base_url,
            auth_config,
            model_lookup,
            debug_recorder,
            connection_mode=connection_mode,
        )

    def wire_media_support(self, model_id: str) -> frozenset[str]:
        del model_id
        return KIMI_IMAGE_VIDEO_MEDIA_TYPES

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

    def reasoning_replay_policy(self, model_id: str) -> ReasoningReplayPolicy:
        reasoning_supported = model_reasoning_supported(self._model_lookup, model_id)
        if reasoning_supported is True:
            return REASONING_REPLAY_FULL_HISTORY
        if reasoning_supported is False:
            return REASONING_REPLAY_NONE
        return REASONING_REPLAY_CURRENT_RUN

    def _format_assistant_message(self, message: dict[str, Any]) -> dict[str, Any]:
        formatted = super()._format_assistant_message(message)
        reasoning = message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            formatted["reasoning_content"] = reasoning
        return formatted

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

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        request_kwargs = dict(kwargs)
        thinking_effort = request_kwargs.pop("thinking_effort", None)
        reasoning_effort = request_kwargs.pop("reasoning_effort", None)
        effort = thinking_effort or reasoning_effort
        remove_reasoning_kwargs(request_kwargs, *KIMI_REASONING_REQUEST_KEYS)
        for parameter_name in KIMI_SAMPLING_REQUEST_KEYS:
            request_kwargs.pop(parameter_name, None)

        payload = super()._build_payload(messages, model_id, **request_kwargs)
        for parameter_name in KIMI_SAMPLING_REQUEST_KEYS:
            payload.pop(parameter_name, None)
        self._normalize_output_limit(payload)

        bare_model_id = model_id.split("::", 1)[0]
        if bare_model_id in KIMI_K3_MODEL_IDS:
            self._apply_k3_reasoning(payload, effort)
        elif bare_model_id in KIMI_K26_MODEL_IDS:
            self._apply_k2_reasoning(payload, enabled=not _is_none_effort(effort))
        elif bare_model_id in KIMI_K27_MODEL_IDS:
            disable = self._connection_mode == KIMI_CODING_MODE and _is_none_effort(effort)
            self._apply_k2_reasoning(payload, enabled=not disable)
        elif model_reasoning_supported(self._model_lookup, bare_model_id) is False:
            self._strip_historical_reasoning(payload)

        _validate_kimi_request_size(
            payload,
            max_bytes=(
                KIMI_CODING_MAX_REQUEST_BODY_BYTES
                if self._connection_mode == KIMI_CODING_MODE
                else KIMI_PLATFORM_MAX_REQUEST_BODY_BYTES
            ),
        )
        return payload

    def _apply_k3_reasoning(self, payload: dict[str, Any], effort: Any) -> None:
        normalized = normalize_thinking_effort(effort)
        if normalized == "none":
            if self._connection_mode == KIMI_CODING_MODE:
                payload["thinking"] = {"type": "disabled"}
                self._strip_historical_reasoning(payload)
            else:
                payload["reasoning_effort"] = "low"
            return
        if normalized in {"minimal", "low"}:
            payload["reasoning_effort"] = "low"
        elif normalized in {"medium", "high"}:
            payload["reasoning_effort"] = "high"
        elif normalized in {"xhigh", "max"}:
            payload["reasoning_effort"] = "max"

    @staticmethod
    def _apply_k2_reasoning(payload: dict[str, Any], *, enabled: bool) -> None:
        if enabled:
            payload["thinking"] = {"type": "enabled", "keep": "all"}
            return
        payload["thinking"] = {"type": "disabled"}
        KimiAdapter._strip_historical_reasoning(payload)

    @staticmethod
    def _strip_historical_reasoning(payload: dict[str, Any]) -> None:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return
        for message in messages:
            if isinstance(message, dict):
                message.pop("reasoning_content", None)

    @staticmethod
    def _normalize_output_limit(payload: dict[str, Any]) -> None:
        limits = [
            value
            for key in ("max_tokens", "max_output_tokens", "max_completion_tokens")
            if isinstance((value := payload.pop(key, None)), int)
            and not isinstance(value, bool)
            and value > 0
        ]
        if limits:
            payload["max_completion_tokens"] = min(limits)

    @classmethod
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


def _is_none_effort(effort: Any) -> bool:
    return normalize_thinking_effort(effort) == "none"


def _validate_kimi_request_size(payload: Mapping[str, Any], *, max_bytes: int) -> None:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not any(
        isinstance(message, Mapping) and isinstance(message.get("content"), list)
        for message in messages
    ):
        return
    body_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if body_size > max_bytes:
        raise ProviderError(
            "Kimi request body exceeds the Connection's multimodal size limit "
            f"({body_size} > {max_bytes} bytes)",
            retryable=False,
        )
