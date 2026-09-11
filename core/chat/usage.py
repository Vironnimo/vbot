"""Session-level token usage aggregation shared by chat.history and Run events."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from core.chat.messages import (
    ChatMessage,
    usage_token_is_estimated,
)
from core.chat.wire_shaping import _embed_notes_into_request
from core.providers.adapter import estimate_wire_request_input_tokens
from core.utils.tokens import estimate_request_input_tokens

JsonObject = dict[str, Any]


@dataclass
class RequestContextUsage:
    """One Run's measured request anchor, without retaining content or pixels.

    Local estimation error in the unchanged request cancels out. A changed
    route, prompt epoch, System Prompt or Tool catalog starts a new estimate.
    Signed deltas also account for image retirement and request-only hooks.
    """

    _key: str | None = None
    _request_hash: str | None = None
    _input_tokens: int | None = None
    _request_estimate: int = 0
    _output_tokens: int | None = None

    def reset(self) -> None:
        self._key = None
        self._input_tokens = None

    def observe(
        self,
        usage: Mapping[str, Any],
        messages: Sequence[Mapping[str, Any]],
        *,
        adapter: Any,
        model_id: str,
        tools: Sequence[Mapping[str, Any]],
        scope: str,
    ) -> None:
        tokens = _optional_non_negative_int(usage.get("input_tokens"))
        if tokens is None or usage_token_is_estimated(usage, "input_tokens"):
            return
        self._key = self._context_key(messages, adapter, model_id, tools, scope)
        self._request_hash = _context_digest(messages)
        self._input_tokens = tokens
        self._request_estimate = estimate_wire_request_input_tokens(
            adapter, messages, model_id=model_id, tools=tools
        )
        self._output_tokens = (
            _optional_non_negative_int(usage.get("output_tokens"))
            if not usage_token_is_estimated(usage, "output_tokens")
            else None
        )

    def project(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        adapter: Any,
        model_id: str,
        tools: Sequence[Mapping[str, Any]],
        scope: str,
    ) -> JsonObject:
        estimated = estimate_wire_request_input_tokens(
            adapter, messages, model_id=model_id, tools=tools
        )
        key = self._context_key(messages, adapter, model_id, tools, scope)
        if self._input_tokens is None or key != self._key:
            return {"tokens": estimated, "estimated": True}
        delta = estimated - self._request_estimate
        if self._input_tokens + delta <= 0 and estimated > 0:
            return {"tokens": estimated, "estimated": True}
        changed = _context_digest(messages) != self._request_hash
        result: JsonObject = {
            "tokens": max(0, self._input_tokens + delta),
            "estimated": changed,
            "provider_input_tokens": self._input_tokens,
        }
        if self._output_tokens is not None:
            result["provider_output_tokens"] = self._output_tokens
        if changed:
            result["estimated_delta_tokens"] = delta
        return result

    @staticmethod
    def _context_key(messages: Any, adapter: Any, model_id: str, tools: Any, scope: str) -> str:
        return _context_digest(
            [
                id(adapter),
                model_id,
                scope,
                tools,
                [message for message in messages if message.get("role") == "system"],
            ]
        )


def _context_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_model_step_context_usage(
    usage: Mapping[str, Any] | None,
    current_request_messages: Sequence[Mapping[str, Any]],
    *,
    estimated_delta_messages: Sequence[Mapping[str, Any]] = (),
    tools: Sequence[Mapping[str, Any]] = (),
) -> JsonObject:
    """Project the Context after one completed Model step.

    Provider input Usage anchors the request that just ran, while Provider
    output Usage accounts for the Assistant response appended after it. Either
    counter may be estimated independently when the Provider omits it. Only
    newer request messages, normally Tool Results, need an additional
    structured estimate.
    """

    input_tokens = _optional_non_negative_int(usage.get("input_tokens") if usage else None)
    output_tokens = _optional_non_negative_int(usage.get("output_tokens") if usage else None)
    if usage is not None and input_tokens is not None:
        output_tokens = output_tokens or 0
        input_estimated = usage_token_is_estimated(usage, "input_tokens")
        output_estimated = (
            usage_token_is_estimated(usage, "output_tokens") if "output_tokens" in usage else False
        )
        estimated_delta_tokens, _ = estimate_request_input_tokens(estimated_delta_messages)
        projected: JsonObject = {
            "tokens": input_tokens + output_tokens + estimated_delta_tokens,
            "estimated": input_estimated
            or output_estimated
            or bool(output_tokens)
            or bool(estimated_delta_messages),
        }
        if not input_estimated:
            projected["provider_input_tokens"] = input_tokens
        if not output_estimated:
            projected["provider_output_tokens"] = output_tokens
        if estimated_delta_messages:
            projected["estimated_delta_tokens"] = estimated_delta_tokens
        return projected

    estimated_tokens, _ = estimate_request_input_tokens(current_request_messages, tools)
    return {"tokens": estimated_tokens, "estimated": True}


def latest_session_context_usage(messages: list[ChatMessage]) -> JsonObject | None:
    """Return the newest durable server projection of a Session's Context.

    An Assistant turn with canonical Usage anchors the projection until a newer
    Compaction checkpoint replaces it. Each token field retains its own measured
    or estimated provenance, and only provider-visible messages appended after
    that anchor need a new estimate. This lets ``chat.history`` restore the same
    semantic value used by live Run events without summing the whole transcript.
    """

    assistant_index = _latest_usage_assistant_index(messages)
    checkpoint_index = _latest_context_checkpoint_index(messages)
    if assistant_index is None and checkpoint_index is None:
        return None

    if checkpoint_index is not None and (
        assistant_index is None or checkpoint_index > assistant_index
    ):
        checkpoint_usage = messages[checkpoint_index].usage or {}
        context_after = _optional_non_negative_int(checkpoint_usage.get("context_tokens_after"))
        if context_after is None:
            return None
        delta_messages = _provider_visible_delta(messages[checkpoint_index + 1 :])
        delta_tokens, _ = estimate_request_input_tokens(delta_messages)
        return {"tokens": context_after + delta_tokens, "estimated": True}

    assert assistant_index is not None
    assistant_usage = messages[assistant_index].usage or {}
    saved_projection = assistant_usage.get("context_usage")
    if (
        isinstance(saved_projection, dict)
        and _optional_non_negative_int(saved_projection.get("tokens")) is not None
    ):
        saved = dict(saved_projection)
        delta_messages = _provider_visible_delta(messages[assistant_index + 1 :])
        if delta_messages:
            delta_tokens, _ = estimate_request_input_tokens(delta_messages)
            saved["tokens"] += delta_tokens
            saved["estimated"] = True
            saved["estimated_delta_tokens"] = saved.get("estimated_delta_tokens", 0) + delta_tokens
        return saved
    input_tokens = _optional_non_negative_int(assistant_usage.get("input_tokens"))
    if input_tokens is None:
        return None
    output_tokens = _optional_non_negative_int(assistant_usage.get("output_tokens")) or 0
    delta_messages = _provider_visible_delta(messages[assistant_index + 1 :])
    delta_tokens, _ = estimate_request_input_tokens(delta_messages)
    input_estimated = usage_token_is_estimated(assistant_usage, "input_tokens")
    output_estimated = usage_token_is_estimated(assistant_usage, "output_tokens")
    estimated = input_estimated or output_estimated or bool(output_tokens) or bool(delta_messages)
    projected: JsonObject = {
        "tokens": input_tokens + output_tokens + delta_tokens,
        "estimated": estimated,
    }
    if not input_estimated:
        projected["provider_input_tokens"] = input_tokens
    if not output_estimated:
        projected["provider_output_tokens"] = output_tokens
    if delta_messages:
        projected["estimated_delta_tokens"] = delta_tokens
    return projected


def checkpoint_context_usage(checkpoint: ChatMessage) -> JsonObject | None:
    """Project the estimated post-Compaction Context from one checkpoint."""

    usage = checkpoint.usage or {}
    context_after = _optional_non_negative_int(usage.get("context_tokens_after"))
    if context_after is None:
        return None
    return {"tokens": context_after, "estimated": True}


def aggregate_session_usage(messages: list[ChatMessage]) -> JsonObject:
    """Sum provider-reported usage across a session's assistant turns.

    Returns the canonical ``session_usage`` payload carried by the
    ``chat.history`` response and the terminal Run events. Token totals cover
    only provider-reported fields; a partially reported turn can contribute
    measured output while its estimated input remains excluded. Turns with any
    estimated field are counted separately from fully measured turns.
    Canonical ``input_tokens`` already includes cached tokens, so
    ``cache_read_tokens``/``cache_write_tokens`` are informational subsets of
    the input total, never added on top. Canonical ``reasoning_tokens`` is an
    optional subset of ``output_tokens`` and likewise never changes totals.
    """
    totals = _empty_session_usage()
    for message in messages:
        if message.role != "assistant" or not isinstance(message.usage, dict):
            continue
        totals = add_session_turn_usage(totals, message.usage)
    return totals


def add_session_turn_usage(totals: JsonObject, usage: JsonObject) -> JsonObject:
    """Return canonical session totals with one persisted assistant turn added."""
    updated = dict(totals)
    input_estimated = usage_token_is_estimated(usage, "input_tokens")
    output_estimated = usage_token_is_estimated(usage, "output_tokens")
    if input_estimated or output_estimated:
        updated["estimated_turns"] = _non_negative_int(updated.get("estimated_turns")) + 1
    else:
        updated["measured_turns"] = _non_negative_int(updated.get("measured_turns")) + 1
    # Field *presence* distinguishes "provider reported zero cache" from
    # "provider does not report caching" — consumers need that to avoid
    # painting a non-caching provider as a 0% hit rate.
    if not input_estimated and ("cache_read_tokens" in usage or "cache_write_tokens" in usage):
        updated["cache_turns"] = _non_negative_int(updated.get("cache_turns")) + 1
    reasoning_tokens = _optional_non_negative_int(usage.get("reasoning_tokens"))
    if not output_estimated and reasoning_tokens is not None:
        updated["reasoning_turns"] = _non_negative_int(updated.get("reasoning_turns")) + 1
        updated["reasoning_tokens"] = (
            _non_negative_int(updated.get("reasoning_tokens")) + reasoning_tokens
        )
    measured_keys: list[str] = []
    if not input_estimated:
        measured_keys.extend(("input_tokens", "cache_read_tokens", "cache_write_tokens"))
    if not output_estimated:
        measured_keys.append("output_tokens")
    for key in measured_keys:
        updated[key] = _non_negative_int(updated.get(key)) + _non_negative_int(usage.get(key))
    return updated


def _empty_session_usage() -> JsonObject:
    return {
        "measured_turns": 0,
        "estimated_turns": 0,
        "cache_turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _optional_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _latest_usage_assistant_index(messages: list[ChatMessage]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role == "assistant" and isinstance(message.usage, dict):
            return index
    return None


def _latest_context_checkpoint_index(messages: list[ChatMessage]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role != "compaction_checkpoint" or not isinstance(message.usage, dict):
            continue
        if _optional_non_negative_int(message.usage.get("context_tokens_after")) is not None:
            return index
    return None


def _provider_visible_delta(messages: list[ChatMessage]) -> list[JsonObject]:
    if not messages:
        return []
    return _embed_notes_into_request(messages)
