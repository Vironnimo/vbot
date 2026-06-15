"""Mistral provider adapter."""

from __future__ import annotations

from collections.abc import Mapping
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
    _read_optional_non_empty_string,
    _read_string,
)
from core.providers.reasoning import (
    REASONING_REPLAY_FULL_HISTORY,
    ReasoningReplayPolicy,
    closest_supported_effort,
    model_reasoning_levels,
)

MISTRAL_REASONING_EFFORTS = {"none", "high"}
MISTRAL_PROMPT_MODE_REASONING_MODEL_PREFIXES = ("magistral-medium",)


def _flatten_thinking(value: Any) -> str:
    """Flatten a Mistral ThinkChunk ``thinking`` payload to plain text.

    Current reasoning models (``mistral-medium-3-5``, ``mistral-small-latest``,
    …) return ``thinking`` as a list of ``{"type": "text", "text": ...}`` chunks;
    older magistral models returned a plain string. Both shapes flatten here.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            chunk["text"]
            for chunk in value
            if isinstance(chunk, dict)
            and chunk.get("type") == "text"
            and isinstance(chunk.get("text"), str)
        )
    return ""


class MistralAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter with Mistral-specific catalog and reasoning behavior."""

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
        vision_supported = capabilities_raw.get("vision", False) is True
        tools_supported = capabilities_raw.get("function_calling", False) is True
        audio_transcription_supported = capabilities_raw.get("audio_transcription", False) is True
        input_modalities = ["text"]
        if vision_supported:
            input_modalities.append("image")
        if audio_transcription_supported:
            input_modalities.append("audio")
        supported_parameters = ["response_format"]
        if tools_supported:
            supported_parameters.append("tools")
        if reasoning_supported:
            supported_parameters.append("reasoning")
        if audio_transcription_supported:
            supported_parameters.append("audio_transcription")

        return Model(
            model_id=model_id,
            name=name,
            capabilities=Capabilities(
                vision=vision_supported,
                tools=tools_supported,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=reasoning_supported),
                input_modalities=tuple(input_modalities),
                output_modalities=("text",),
                supported_parameters=tuple(supported_parameters),
            ),
            context_window=context_window,
            max_output_tokens=None,
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

        # Snap against the effective per-model ladder when present; the binary
        # ``{none, high}`` constant is the floor for a model without a feed
        # ladder (all Mistral models today — their ladder is not yet projected).
        ladder = model_reasoning_levels(self._model_lookup, model_id) or MISTRAL_REASONING_EFFORTS
        supported_effort = closest_supported_effort(thinking_effort, ladder)
        # Mistral's wire reasoning is a binary thinking toggle: any active
        # (non-``none``) snapped effort engages thinking, ``none`` disables it.
        # Mapping every active effort to Mistral's single thinking mode keeps a
        # multi-level feed ladder from silently dropping a mid-level selection.
        if supported_effort is not None and supported_effort != "none":
            if use_prompt_mode_reasoning:
                payload["prompt_mode"] = "reasoning"
                payload.pop("reasoning_effort", None)
            else:
                payload["reasoning_effort"] = "high"
        elif supported_effort == "none":
            if use_prompt_mode_reasoning:
                payload.pop("reasoning_effort", None)
                payload.pop("prompt_mode", None)
            else:
                payload["reasoning_effort"] = "none"

        return payload

    def reasoning_replay_policy(self, model_id: str) -> ReasoningReplayPolicy:
        """Replay reasoning across runs — Mistral requires it and the API accepts it.

        Mistral's guidance is explicit and cross-turn: always replay the full
        assistant message including the thinking trace; dropping it degrades
        output quality. Verified against the live API (2026-06-13): replaying a
        reconstructed ThinkChunk on a same-model follow-up returns 200, including
        when the new request sets ``reasoning_effort: "none"`` — so no
        thinking-disabled guard is needed. The chat layer's same-model gate
        strips cross-model entries.
        """
        del model_id
        return REASONING_REPLAY_FULL_HISTORY

    def _format_assistant_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Render replayed reasoning as a Mistral ThinkChunk ahead of the answer.

        Mistral emits reasoning as a ``content`` chunk list
        (``[{"type": "thinking", "thinking": [TextChunk]}, {"type": "text", …}]``)
        and expects the same shape replayed back. The chat layer keeps the
        visible ``reasoning`` text on replayed (and in-run) assistant turns, so
        the trace is reconstructed here from that text. When no reasoning is
        present the generic plain-string content is kept unchanged.
        """
        wire = super()._format_assistant_message(message)
        reasoning = message.get("reasoning")
        if not isinstance(reasoning, str) or not reasoning:
            return wire
        content_chunks: list[dict[str, Any]] = [
            {"type": "thinking", "thinking": [{"type": "text", "text": reasoning}]}
        ]
        visible = wire.get("content")
        if isinstance(visible, str) and visible:
            content_chunks.append({"type": "text", "text": visible})
        wire["content"] = content_chunks
        return wire

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
                thinking = _flatten_thinking(item.get("thinking"))
                if thinking:
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
                            thinking = _flatten_thinking(item.get("thinking"))
                            if thinking:
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
