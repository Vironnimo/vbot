"""Mistral provider adapter."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.errors import CatalogEntrySkipped
from core.providers.openai_compatible import (
    OpenAICompatibleAdapter,
    _extract_openai_reasoning_meta,
    _extract_openai_tool_calls,
    _extract_openai_usage,
    _extract_stream_usage,
    _first_choice_message,
    _normalize_openai_finish_reason,
    _parse_optional_int,
    _provider_default_max_tokens,
    _read_optional_non_empty_string,
    _read_string,
)
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.token_getter import TokenGetter

MISTRAL_HIGH_REASONING_EFFORTS = {"medium", "high", "xhigh", "max"}
MISTRAL_PROMPT_MODE_REASONING_MODEL_PREFIXES = ("magistral-medium",)


class MistralAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter with Mistral-specific catalog and reasoning behavior."""

    def __init__(
        self,
        config: ProviderConfig,
        token_getter: TokenGetter | str,
        base_url: str | None = None,
        auth_config: AuthConfig | None = None,
        model_reasoning_supported_lookup: Callable[[str], bool | None] | None = None,
    ) -> None:
        super().__init__(config, token_getter, base_url, auth_config)
        self._model_reasoning_supported_lookup = model_reasoning_supported_lookup

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one Mistral ``/models`` entry into a vBot ``Model``."""

        model_id = _read_string(raw, "id")
        name = _read_optional_non_empty_string(raw, "name") or model_id

        capabilities_raw = raw.get("capabilities", {})
        if not isinstance(capabilities_raw, dict):
            capabilities_raw = {}

        if raw.get("archived") is True or capabilities_raw.get("completion_chat") is not True:
            raise CatalogEntrySkipped(f"Skipped non-chat model: {raw.get('id')}")

        context_window = _parse_optional_int(raw.get("max_context_length")) or 0
        reasoning_supported = capabilities_raw.get("reasoning", False) is True

        return Model(
            model_id=model_id,
            name=name,
            capabilities=Capabilities(
                vision=capabilities_raw.get("vision", False) is True,
                tools=capabilities_raw.get("function_calling", False) is True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=reasoning_supported),
            ),
            context_window=context_window,
            max_output_tokens=_provider_default_max_tokens(defaults),
        )

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build a Mistral payload with model-specific reasoning protocol mapping."""

        thinking_effort = kwargs.pop("thinking_effort", "")
        payload = super()._build_payload(messages, model_id, **kwargs)

        if self._model_reasoning_supported(model_id) is False:
            payload.pop("reasoning_effort", None)
            payload.pop("prompt_mode", None)
            return payload

        use_prompt_mode_reasoning = any(
            model_id.startswith(prefix) for prefix in MISTRAL_PROMPT_MODE_REASONING_MODEL_PREFIXES
        )

        if thinking_effort in MISTRAL_HIGH_REASONING_EFFORTS:
            if use_prompt_mode_reasoning:
                payload["prompt_mode"] = "reasoning"
                payload.pop("reasoning_effort", None)
            else:
                payload["reasoning_effort"] = "high"
        elif thinking_effort == "none":
            if use_prompt_mode_reasoning:
                payload.pop("reasoning_effort", None)
                payload.pop("prompt_mode", None)
            else:
                payload["reasoning_effort"] = "none"

        return payload

    def _model_reasoning_supported(self, model_id: str) -> bool | None:
        if self._model_reasoning_supported_lookup is None:
            return None
        return self._model_reasoning_supported_lookup(model_id)

    def normalize_response(self, response: dict[str, Any]) -> dict[str, Any]:
        message = _first_choice_message(response)
        content = message.get("content")
        if not isinstance(content, list):
            return super().normalize_response(response)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                text = item.get("text")
                if isinstance(text, str):
                    content_parts.append(text)
            elif item_type == "thinking":
                thinking = item.get("thinking")
                if isinstance(thinking, str):
                    reasoning_parts.append(thinking)

        normalized: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
            "reasoning": "".join(reasoning_parts) or None,
            "reasoning_meta": _extract_openai_reasoning_meta(message),
            "tool_calls": _extract_openai_tool_calls(message),
        }
        usage = _extract_openai_usage(response)
        if usage is not None:
            normalized["usage"] = usage
        return normalized

    def _normalize_stream_chunk(
        self,
        raw_chunk: dict[str, Any],
        tool_call_ids_by_index: dict[int, str],
    ) -> list[dict[str, Any]]:
        choices_raw = raw_chunk.get("choices", [])
        if not isinstance(choices_raw, list):
            return super()._normalize_stream_chunk(raw_chunk, tool_call_ids_by_index)

        choices = [choice for choice in choices_raw if isinstance(choice, dict)]
        has_typed_content_delta = any(
            isinstance(choice.get("delta"), dict)
            and isinstance(choice["delta"].get("content"), list)
            for choice in choices
        )
        if not has_typed_content_delta:
            return super()._normalize_stream_chunk(raw_chunk, tool_call_ids_by_index)

        normalized_deltas: list[dict[str, Any]] = []
        for choice in choices:
            delta = choice.get("delta", {})
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        item_type = item.get("type")
                        if item_type == "thinking":
                            thinking = item.get("thinking")
                            if isinstance(thinking, str) and thinking:
                                normalized_deltas.append(
                                    {"type": "reasoning_delta", "text": thinking}
                                )
                        elif item_type == "text":
                            text = item.get("text")
                            if isinstance(text, str) and text:
                                normalized_deltas.append({"type": "content_delta", "text": text})

            finish_reason = choice.get("finish_reason")
            if finish_reason is not None:
                normalized_deltas.append(
                    {
                        "type": "finish",
                        "reason": _normalize_openai_finish_reason(
                            finish_reason,
                            has_tool_calls=bool(tool_call_ids_by_index),
                        ),
                    }
                )

        usage_delta = _extract_stream_usage(raw_chunk)
        if usage_delta is not None:
            normalized_deltas.append(usage_delta)

        return normalized_deltas
