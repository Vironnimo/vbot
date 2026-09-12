"""Chat Completions streaming deltas and stable Tool Call slots."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.providers._chat_completions_constants import (
    _OPENAI_STREAM_REASONING_DETAILS_STATE_KEY,
    _OPENAI_TOOL_CALL_INDEX_IDS_STATE_KEY,
)
from core.providers._chat_completions_wire import (
    _extract_openai_reasoning,
    _extract_openai_reasoning_meta,
    _extract_stream_usage,
    _normalize_openai_finish_reason,
    _openai_concealed_transport_failure,
)
from core.providers.errors import (
    ProviderError,
    classify_in_band_provider_error,
)
from core.utils.tokens import (
    continues_reasoning_text_block,
)


def _normalize_openai_stream_chunk(
    chunk: dict[str, Any],
    tool_call_slots: set[int],
    *,
    normalization_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    error = chunk.get("error")
    if isinstance(error, dict):
        raise classify_in_band_provider_error(error)
    if error is not None:
        # A non-object error payload carries nothing classifiable and stays fatal.
        raise ProviderError(f"Provider stream error: {error}", retryable=False)

    normalized_deltas: list[dict[str, Any]] = []
    for choice in _stream_choices(chunk):
        delta = choice.get("delta", {})
        if isinstance(delta, dict):
            normalized_deltas.extend(
                _normalize_openai_message_delta(
                    delta,
                    tool_call_slots,
                    normalization_state=normalization_state,
                )
            )

        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            transport_failure = _openai_concealed_transport_failure(
                finish_reason,
                choice.get("native_finish_reason"),
            )
            if transport_failure is not None:
                raise transport_failure
            normalized_deltas.append(
                {
                    "type": "finish",
                    "reason": _normalize_openai_finish_reason(
                        finish_reason,
                        has_tool_calls=bool(tool_call_slots),
                    ),
                }
            )

    # OpenAI streaming includes usage only in the final chunk when
    # stream_options.include_usage is set. Extract token usage when present.
    usage_delta = _extract_stream_usage(chunk)
    if usage_delta is not None:
        normalized_deltas.append(usage_delta)

    return normalized_deltas


def _stream_choices(chunk: dict[str, Any]) -> list[dict[str, Any]]:
    choices = chunk.get("choices", [])
    if not isinstance(choices, list):
        return []
    return [choice for choice in choices if isinstance(choice, dict)]


def _normalize_openai_message_delta(
    delta: dict[str, Any],
    tool_call_slots: set[int],
    *,
    normalization_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    normalized_deltas: list[dict[str, Any]] = []
    content = delta.get("content")
    if isinstance(content, str) and content:
        normalized_deltas.append({"type": "content_delta", "text": content})

    reasoning = _extract_openai_reasoning(delta)
    if reasoning:
        normalized_deltas.append({"type": "reasoning_delta", "text": reasoning})

    reasoning_meta = _extract_openai_reasoning_meta(delta)
    if reasoning_meta:
        reasoning_meta = _accumulate_openai_stream_reasoning_details(
            reasoning_meta,
            normalization_state,
        )
        normalized_deltas.append({"type": "reasoning_meta", "reasoning_meta": reasoning_meta})

    normalized_deltas.extend(
        _normalize_openai_tool_call_deltas(
            delta,
            tool_call_slots,
            normalization_state=normalization_state,
        )
    )
    return normalized_deltas


def _normalize_openai_tool_call_deltas(
    delta: dict[str, Any],
    tool_call_slots: set[int],
    *,
    normalization_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    raw_tool_calls = delta.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return []

    normalized_deltas: list[dict[str, Any]] = []
    for position, raw_tool_call in enumerate(raw_tool_calls):
        if not isinstance(raw_tool_call, dict):
            continue
        tool_call_index = _openai_tool_call_index(raw_tool_call, position)
        provider_id = raw_tool_call.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            provider_id = None
        tool_call_index = _redirect_reused_tool_call_index(
            tool_call_index,
            provider_id,
            tool_call_slots,
            normalization_state,
        )
        tool_call_slots.add(tool_call_index)
        function = raw_tool_call.get("function", {})
        if not isinstance(function, dict):
            function = {}
        name_delta = function.get("name")
        arguments_delta = function.get("arguments")
        if not isinstance(name_delta, str):
            name_delta = ""
        if not isinstance(arguments_delta, str):
            arguments_delta = ""
        if provider_id is None and not name_delta and not arguments_delta:
            continue
        normalized_delta: dict[str, Any] = {
            "type": "tool_call_delta",
            "slot": tool_call_index,
            "name_delta": name_delta,
            "arguments_delta": arguments_delta,
        }
        if provider_id is not None:
            normalized_delta["id"] = provider_id
        normalized_deltas.append(normalized_delta)
    return normalized_deltas


def _openai_tool_call_index(raw_tool_call: dict[str, Any], position: int) -> int:
    index = raw_tool_call.get("index")
    return index if isinstance(index, int) else position


def _redirect_reused_tool_call_index(
    raw_index: int,
    provider_id: str | None,
    tool_call_slots: set[int],
    normalization_state: dict[str, Any] | None,
) -> int:
    """Return the accumulator slot for a raw wire Tool-call index.

    Ollama-compatible endpoints may reuse one index (typically ``0``) for every
    Tool Call in a parallel batch, distinguishing the calls only by ``id``. A
    same-index delta carrying a *different* id therefore starts a new call and
    is redirected to a fresh virtual slot instead of being merged into the
    previous call's fragments. An id-less fragment keeps the tracked virtual
    slot of its raw index. Requires the per-stream ``normalization_state``
    mapping; without it the raw index wins.
    """

    if normalization_state is None:
        return raw_index
    tracked = normalization_state.get(_OPENAI_TOOL_CALL_INDEX_IDS_STATE_KEY)
    if not isinstance(tracked, dict):
        tracked = {}
        normalization_state[_OPENAI_TOOL_CALL_INDEX_IDS_STATE_KEY] = tracked
    entry = tracked.get(raw_index)
    if isinstance(entry, list) and len(entry) == 2:
        last_id, virtual_index = entry
        if (
            isinstance(last_id, str)
            and isinstance(virtual_index, int)
            and not isinstance(virtual_index, bool)
        ):
            if provider_id is None or provider_id == last_id:
                return virtual_index
            fresh_index = max(tool_call_slots, default=raw_index) + 1
            tracked[raw_index] = [provider_id, fresh_index]
            return fresh_index
    if provider_id is None:
        return raw_index
    tracked[raw_index] = [provider_id, raw_index]
    return raw_index


def _accumulate_openai_stream_reasoning_details(
    reasoning_meta: dict[str, Any],
    normalization_state: dict[str, Any] | None,
) -> dict[str, Any]:
    """Preserve separately streamed reasoning details in Provider order.

    OpenAI-compatible gateways may emit one ``reasoning_details`` array per
    sibling Tool Call. Chat intentionally treats reasoning metadata as opaque
    and therefore shallow-merges normalized metadata deltas. Emit the complete
    accumulated array from this Adapter so a later sibling cannot overwrite an
    earlier detail. Details carrying a stable ``id`` update in place, retaining
    first-seen order while allowing a Provider's cumulative snapshot to refine
    an earlier fragment. Consecutive id-less ``text`` fragments of identical
    shape are per-delta continuations of one logical block and merge into it,
    so a streamed reasoning text persists as one item instead of one item per
    token (see :func:`continues_reasoning_text_block`).
    """

    details = reasoning_meta.get("reasoning_details")
    if normalization_state is None or not isinstance(details, list):
        return reasoning_meta

    accumulated = normalization_state.get(_OPENAI_STREAM_REASONING_DETAILS_STATE_KEY)
    if not isinstance(accumulated, list):
        accumulated = []
        normalization_state[_OPENAI_STREAM_REASONING_DETAILS_STATE_KEY] = accumulated

    for detail in details:
        normalized_detail = dict(detail) if isinstance(detail, Mapping) else detail
        detail_id = detail.get("id") if isinstance(detail, Mapping) else None
        if isinstance(detail_id, str) and detail_id:
            matching_index = next(
                (
                    index
                    for index, existing in enumerate(accumulated)
                    if isinstance(existing, Mapping) and existing.get("id") == detail_id
                ),
                None,
            )
            if matching_index is not None:
                accumulated[matching_index] = normalized_detail
                continue
        previous = accumulated[-1] if accumulated else None
        previous_text = previous.get("text") if isinstance(previous, Mapping) else None
        if (
            isinstance(previous, Mapping)
            and isinstance(previous_text, str)
            and continues_reasoning_text_block(previous, normalized_detail)
        ):
            accumulated[len(accumulated) - 1] = {
                **previous,
                "text": previous_text + normalized_detail["text"],
            }
            continue
        accumulated.append(normalized_detail)

    merged = dict(reasoning_meta)
    merged["reasoning_details"] = [
        dict(detail) if isinstance(detail, Mapping) else detail for detail in accumulated
    ]
    return merged
