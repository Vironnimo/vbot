"""Mistral provider adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.adapter import MISTRAL_TOOL_CALL_ID_PROFILE, normalize_tool_call_ids
from core.providers.errors import CatalogEntrySkipped
from core.providers.openai_compatible import (
    OpenAICompatibleAdapter,
    _extract_openai_reasoning_meta,
    _extract_openai_terminal_outcome,
    _extract_openai_tool_calls,
    _extract_openai_usage,
    _extract_stream_usage,
    _first_choice_message,
    _normalize_openai_finish_reason,
    _normalize_openai_tool_call_deltas,
    _parse_optional_int,
    _read_optional_non_empty_string,
    _read_string,
)
from core.providers.reasoning import (
    closest_supported_effort,
    model_reasoning_levels,
)

MISTRAL_REASONING_EFFORTS = {"none", "high"}

# Provider-scoped metadata blob + field carrying the magistral reasoning-mode
# wire fact (Phase 5). The decision "this model engages reasoning via
# ``prompt_mode: reasoning`` instead of ``reasoning_effort``" is a published
# per-model FACT, so it lives in model data as
# ``metadata.mistral.prompt_mode == "reasoning"``, not in a name-prefix guess.
# The adapter still owns the MECHANICS — building the wire request from that fact.
MISTRAL_METADATA_KEY = "mistral"
PROMPT_MODE_METADATA_KEY = "prompt_mode"
PROMPT_MODE_REASONING = "reasoning"
MISTRAL_CONTENT_CHUNKS_META_KEY = "content_chunks"


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

        # Absent → ``None`` (honest "unknown"), filled by the read-side default
        # chain at use time — no fake ``0`` written into the catalog.
        context_window = _parse_optional_int(raw.get("max_context_length"))
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
        wire_messages = normalize_tool_call_ids(messages, MISTRAL_TOOL_CALL_ID_PROFILE)
        payload = super()._build_payload(wire_messages, model_id, **kwargs)

        if self._model_reasoning_supported(model_id) is False:
            payload.pop("reasoning_effort", None)
            payload.pop("prompt_mode", None)
            return payload

        use_prompt_mode_reasoning = self._uses_prompt_mode_reasoning(model_id)

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

    def _uses_prompt_mode_reasoning(self, model_id: str) -> bool:
        """Return whether this model engages reasoning via ``prompt_mode``.

        Reads the per-model wire fact ``metadata.mistral.prompt_mode ==
        "reasoning"`` from the injected catalog (Phase 5) instead of guessing
        from a ``magistral-medium`` name prefix. A model with no such metadata
        (every non-magistral model) uses the default ``reasoning_effort`` wire.
        The connection-pin suffix is stripped before lookup, mirroring the
        shared reasoning helpers.
        """

        if self._model_lookup is None:
            return False
        model = self._model_lookup(model_id.split("::", 1)[0])
        if model is None:
            return False
        mistral_metadata = model.metadata.get(MISTRAL_METADATA_KEY)
        if not isinstance(mistral_metadata, Mapping):
            return False
        return mistral_metadata.get(PROMPT_MODE_METADATA_KEY) == PROMPT_MODE_REASONING

    def _format_assistant_message(
        self,
        message: dict[str, Any],
        *,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Replay the original Mistral content chunks whenever they are available."""
        wire = super()._format_assistant_message(message, model_id=model_id)
        wire.pop("reasoning_content", None)
        reasoning_meta = message.get("reasoning_meta")
        if isinstance(reasoning_meta, Mapping):
            stored_chunks = reasoning_meta.get(MISTRAL_CONTENT_CHUNKS_META_KEY)
            if isinstance(stored_chunks, list) and all(
                isinstance(item, Mapping) for item in stored_chunks
            ):
                wire["content"] = [dict(item) for item in stored_chunks]
                return wire

        # Backward compatibility for Sessions written before exact chunks were
        # persisted. New responses never take this lossy reconstruction path.
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

    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        message = _first_choice_message(response)
        content = message.get("content")
        if not isinstance(content, list):
            return super().normalize_response(response, model_id=model_id)

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
            "reasoning_meta": _mistral_reasoning_meta(message, content),
            "tool_calls": _extract_openai_tool_calls(message),
        }
        normalized["terminal_outcome"] = _extract_openai_terminal_outcome(
            response,
            has_tool_calls=bool(normalized["tool_calls"]),
        )
        usage = _extract_openai_usage(response)
        if usage is not None:
            normalized["usage"] = usage
        return normalized

    def _normalize_stream_chunk(
        self,
        raw_chunk: dict[str, Any],
        tool_call_slots: set[int],
        normalization_state: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        choices_raw = raw_chunk.get("choices", [])
        if not isinstance(choices_raw, list):
            return super()._normalize_stream_chunk(raw_chunk, tool_call_slots)

        choices = [choice for choice in choices_raw if isinstance(choice, dict)]
        state = normalization_state if normalization_state is not None else {}
        recorded_chunks = state.get(MISTRAL_CONTENT_CHUNKS_META_KEY)
        has_typed_content_delta = any(
            isinstance(choice.get("delta"), dict)
            and isinstance(choice["delta"].get("content"), list)
            for choice in choices
        )
        if not has_typed_content_delta and not isinstance(recorded_chunks, list):
            return super()._normalize_stream_chunk(
                raw_chunk,
                tool_call_slots,
                normalization_state,
            )

        normalized_deltas: list[dict[str, Any]] = []
        content_chunks = state.setdefault(MISTRAL_CONTENT_CHUNKS_META_KEY, [])
        if not isinstance(content_chunks, list):
            content_chunks = []
            state[MISTRAL_CONTENT_CHUNKS_META_KEY] = content_chunks
        for choice in choices:
            delta = choice.get("delta", {})
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        content_chunks.append(dict(item))
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
                normalized_deltas.extend(
                    _normalize_openai_tool_call_deltas(
                        delta,
                        tool_call_slots,
                        normalization_state=normalization_state,
                    )
                )

            finish_reason = choice.get("finish_reason")
            if finish_reason is not None:
                if content_chunks:
                    normalized_deltas.append(
                        {
                            "type": "reasoning_meta",
                            "reasoning_meta": {
                                MISTRAL_CONTENT_CHUNKS_META_KEY: [
                                    dict(item)
                                    for item in content_chunks
                                    if isinstance(item, Mapping)
                                ]
                            },
                        }
                    )
                normalized_deltas.append(
                    {
                        "type": "finish",
                        "reason": _normalize_openai_finish_reason(
                            finish_reason,
                            has_tool_calls=bool(tool_call_slots),
                        ),
                    }
                )

        usage_delta = _extract_stream_usage(raw_chunk)
        if usage_delta is not None:
            normalized_deltas.append(usage_delta)

        return normalized_deltas


def _mistral_reasoning_meta(
    message: Mapping[str, Any],
    content_chunks: list[Any],
) -> dict[str, Any]:
    meta = _extract_openai_reasoning_meta(dict(message)) or {}
    meta[MISTRAL_CONTENT_CHUNKS_META_KEY] = [
        dict(item) for item in content_chunks if isinstance(item, Mapping)
    ]
    return meta
