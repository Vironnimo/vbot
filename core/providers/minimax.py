"""MiniMax provider adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.openai_compatible import (
    OpenAICompatibleAdapter,
    _read_optional_non_empty_string,
    _read_string,
)
from core.providers.reasoning import (
    REASONING_INTENT_DEFAULT,
    REASONING_INTENT_OFF,
    REASONING_REPLAY_FULL_HISTORY,
    ReasoningIntent,
    ReasoningReplayPolicy,
    model_reasoning_control,
    model_reasoning_levels,
    resolve_reasoning_intent,
)

MINIMAX_M3_MODEL_ID = "MiniMax-M3"

# MiniMax M3 engages reasoning as a binary thinking toggle (``adaptive`` on /
# ``disabled`` off), not a per-level effort, so the resolver only needs to tell
# active efforts from ``none``. This floor lets any active effort resolve to an
# ``effort`` intent (rendered as adaptive) while ``none`` resolves to ``off``.
MINIMAX_M3_EFFORT_FLOOR = ("minimal", "low", "medium", "high", "xhigh", "max")
MINIMAX_M2_SUPPORTED_PARAMETERS = (
    "max_tokens",
    "reasoning_split",
    "temperature",
    "tools",
    "top_p",
)
MINIMAX_M3_SUPPORTED_PARAMETERS = (
    "max_completion_tokens",
    "max_tokens",
    "reasoning_split",
    "stream_options",
    "temperature",
    "thinking",
    "tools",
    "top_p",
)
MINIMAX_REASONING_PAYLOAD_KEYS = ("reasoning", "reasoning_effort", "include_reasoning")

MINIMAX_MODEL_FACTS: dict[str, dict[str, Any]] = {
    "MiniMax-M2": {
        "name": "MiniMax M2",
        "context_window": 204800,
        "input_modalities": ("text",),
        "supported_parameters": MINIMAX_M2_SUPPORTED_PARAMETERS,
    },
    "MiniMax-M2.1": {
        "name": "MiniMax M2.1",
        "context_window": 204800,
        "input_modalities": ("text",),
        "supported_parameters": MINIMAX_M2_SUPPORTED_PARAMETERS,
    },
    "MiniMax-M2.1-highspeed": {
        "name": "MiniMax M2.1 Highspeed",
        "context_window": 204800,
        "input_modalities": ("text",),
        "supported_parameters": MINIMAX_M2_SUPPORTED_PARAMETERS,
    },
    "MiniMax-M2.5": {
        "name": "MiniMax M2.5",
        "context_window": 204800,
        "input_modalities": ("text",),
        "supported_parameters": MINIMAX_M2_SUPPORTED_PARAMETERS,
    },
    "MiniMax-M2.5-highspeed": {
        "name": "MiniMax M2.5 Highspeed",
        "context_window": 204800,
        "input_modalities": ("text",),
        "supported_parameters": MINIMAX_M2_SUPPORTED_PARAMETERS,
    },
    "MiniMax-M2.7": {
        "name": "MiniMax M2.7",
        "context_window": 204800,
        "input_modalities": ("text",),
        "supported_parameters": MINIMAX_M2_SUPPORTED_PARAMETERS,
    },
    "MiniMax-M2.7-highspeed": {
        "name": "MiniMax M2.7 Highspeed",
        "context_window": 204800,
        "input_modalities": ("text",),
        "supported_parameters": MINIMAX_M2_SUPPORTED_PARAMETERS,
    },
    MINIMAX_M3_MODEL_ID: {
        "name": "MiniMax M3",
        "context_window": 1000000,
        "input_modalities": ("text", "image", "video"),
        "supported_parameters": MINIMAX_M3_SUPPORTED_PARAMETERS,
    },
}


class MiniMaxAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter with MiniMax catalog and thinking behavior."""

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one MiniMax ``/models`` entry into a vBot ``Model``."""

        model_id = _read_string(raw, "id")
        facts = MINIMAX_MODEL_FACTS.get(model_id)
        if facts is None:
            return super().normalize_catalog_entry(raw, defaults)

        name = (
            _read_optional_non_empty_string(raw, "name")
            or _read_optional_non_empty_string(raw, "display_name")
            or str(facts["name"])
        )
        input_modalities = tuple(facts["input_modalities"])

        return Model(
            model_id=model_id,
            name=name,
            capabilities=Capabilities(
                vision="image" in input_modalities,
                tools=True,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=True),
                input_modalities=input_modalities,
                output_modalities=("text",),
                supported_parameters=tuple(facts["supported_parameters"]),
            ),
            context_window=int(facts["context_window"]),
            max_output_tokens=None,
        )

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build a MiniMax payload without unsupported OpenAI reasoning controls."""

        thinking_effort = kwargs.pop("thinking_effort", "")
        reasoning_effort = kwargs.pop("reasoning_effort", "")
        payload = super()._build_payload(messages, model_id, **kwargs)
        for key in MINIMAX_REASONING_PAYLOAD_KEYS:
            payload.pop(key, None)

        if self._model_reasoning_supported(model_id) is False:
            payload.pop("thinking", None)
            payload.pop("reasoning_split", None)
            return payload

        if model_id != MINIMAX_M3_MODEL_ID:
            # M2.x reasons by default; split the trace into reasoning_details so
            # it is captured separately (not inline <think>) and stays replayable
            # across runs under the full_history policy.
            payload.pop("thinking", None)
            payload.setdefault("reasoning_split", True)
            return payload

        intent = resolve_reasoning_intent(
            supported=self._model_reasoning_supported(model_id),
            control=model_reasoning_control(self._model_lookup, model_id),
            levels=model_reasoning_levels(self._model_lookup, model_id) or MINIMAX_M3_EFFORT_FLOOR,
            effort=thinking_effort or reasoning_effort,
        )
        _render_minimax_m3_thinking(payload, intent)
        return payload

    def reasoning_replay_policy(self, model_id: str) -> ReasoningReplayPolicy:
        """Replay reasoning across runs — MiniMax's own guidance requires it.

        MiniMax documents that preserving the reasoning trace across multi-turn
        interactions is essential and that discarding it measurably degrades
        quality. ``_build_payload`` defaults ``reasoning_split: true`` so the
        trace is captured as ``reasoning_details``; the generic request builder
        then replays ``reasoning_meta.reasoning_details`` on same-model history
        (the chat layer's gate strips cross-model entries).

        Not yet probed against the live MiniMax API (no credentials in this
        environment); the implemented behavior is pinned by unit tests and the
        deferred live verification is recorded in ``.vorch/FLAGGED.md``.
        """
        del model_id
        return REASONING_REPLAY_FULL_HISTORY

    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        normalized = super().normalize_response(response, model_id=model_id)
        if normalized.get("reasoning") is None:
            reasoning = _extract_reasoning_details_text(normalized.get("reasoning_meta"))
            if reasoning:
                normalized["reasoning"] = reasoning
        return normalized


def _render_minimax_m3_thinking(payload: dict[str, Any], intent: ReasoningIntent) -> None:
    """Render a reasoning intent onto a MiniMax M3 payload.

    M3 has a binary thinking toggle and a ``reasoning_split`` capture flag, no
    native token budget — so ``budget``/``on`` intents render the same as an
    ``effort`` (adaptive thinking). ``off`` disables thinking and drops the
    split; ``default`` (no effort selected) keeps M3's reason-by-default with the
    split on. A caller-set value is left untouched (``setdefault``/guarded pop).
    """

    if intent.kind == REASONING_INTENT_OFF:
        payload.setdefault("thinking", {"type": "disabled"})
        payload.pop("reasoning_split", None)
    elif intent.kind == REASONING_INTENT_DEFAULT:
        payload.setdefault("reasoning_split", True)
    else:
        payload.setdefault("thinking", {"type": "adaptive"})
        payload.setdefault("reasoning_split", True)


def _extract_reasoning_details_text(reasoning_meta: Any) -> str | None:
    if not isinstance(reasoning_meta, dict):
        return None

    reasoning_details = reasoning_meta.get("reasoning_details")
    if not isinstance(reasoning_details, list):
        return None

    parts: list[str] = []
    for detail in reasoning_details:
        if not isinstance(detail, dict):
            continue
        text = detail.get("text")
        if isinstance(text, str):
            parts.append(text)

    return "".join(parts) or None
