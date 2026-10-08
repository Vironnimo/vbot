"""Session Context accounting and token Usage aggregation.

One rule decides a request's Context everywhere: the newest Provider-measured
input of a compatible request plus the corrected local estimate of what changed
since; without such a measurement, the corrected local estimate of the whole
request. "Corrected" always means the local count times the target Model's
learned input estimate factor (``core/usage`` calibration). Live Runs, Session
history, Compaction and the pre-send guard all read their numbers from here.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.chat.messages import (
    CONTEXT_ESTIMATION_FIELD,
    ChatMessage,
    usage_token_is_estimated,
)
from core.chat.wire_shaping import _embed_notes_into_request
from core.providers.adapter import estimate_wire_request_input_tokens
from core.utils.tokens import estimate_request_input_tokens

JsonObject = dict[str, Any]
# A Run estimates the same request before sending it, when observing its Usage,
# for Compaction and often again as the next request; a few entries cover that.
_ESTIMATE_MEMO_SIZE = 4


class InputEstimateCalibration(Protocol):
    """The learned per-Model correction of local input estimates (``UsageRecorder``)."""

    def input_estimate_factor(self, model: str) -> float: ...

    def record_input_estimate(self, model: str, *, measured: int, estimated: int) -> None: ...


class ContextTarget(Protocol):
    """The route a request goes to: its Adapter renders and counts the wire."""

    @property
    def adapter(self) -> Any: ...

    @property
    def model_id(self) -> str: ...

    @property
    def model_reference(self) -> str: ...


@dataclass(frozen=True)
class ContextRoute:
    """A ``ContextTarget`` for callers without a Run's Model target."""

    adapter: Any
    model_id: str
    model_reference: str


@dataclass(frozen=True)
class _Anchor:
    """The newest measured request: its Provider input and uncorrected local estimate."""

    key: str
    request: str
    input_tokens: int
    output_tokens: int | None
    estimate: int

    def to_dict(self) -> JsonObject:
        value: JsonObject = {
            "key": self.key,
            "request": self.request,
            "input_tokens": self.input_tokens,
            "estimate": self.estimate,
        }
        if self.output_tokens is not None:
            value["output_tokens"] = self.output_tokens
        return value

    @classmethod
    def from_dict(cls, value: Any) -> _Anchor | None:
        if not isinstance(value, Mapping):
            return None
        key, request = value.get("key"), value.get("request")
        input_tokens = _optional_non_negative_int(value.get("input_tokens"))
        estimate = _optional_non_negative_int(value.get("estimate"))
        if not isinstance(key, str) or not isinstance(request, str):
            return None
        if input_tokens is None or estimate is None:
            return None
        output_tokens = _optional_non_negative_int(value.get("output_tokens"))
        return cls(key, request, input_tokens, output_tokens, estimate)


@dataclass
class RequestContextUsage:
    """A Session's Context accounting for one Run, without content or pixels.

    The anchor is the newest measured request with its uncorrected local
    estimate; local estimation error in the unchanged part cancels out. It
    continues across Runs through the ``context_estimation`` record each
    Assistant step persists (``resume``) and holds while the route, prompt
    scope, System Prompt and Tool catalog stay the same; Compaction ends it.
    Measured input is never scaled; only estimated parts are corrected. Every
    measured request also teaches the calibration. Wire estimates are memoized
    by the request's digests, so an identical request is estimated once.
    """

    calibration: InputEstimateCalibration | None = None
    _anchor: _Anchor | None = None
    _estimates: OrderedDict[tuple[str, str], int] = field(default_factory=OrderedDict, repr=False)

    @classmethod
    def resume(
        cls, messages: Sequence[ChatMessage], calibration: InputEstimateCalibration | None
    ) -> RequestContextUsage:
        """Continue the Session's newest measured anchor unless Compaction followed it."""
        accounting = cls(calibration)
        history = list(messages)
        index = _latest_usage_assistant_index(history)
        checkpoint_index = _latest_context_checkpoint_index(history)
        if index is None or (checkpoint_index is not None and checkpoint_index > index):
            return accounting
        record = (history[index].usage or {}).get(CONTEXT_ESTIMATION_FIELD)
        if isinstance(record, Mapping):
            accounting._anchor = _Anchor.from_dict(record.get("anchor"))
        return accounting

    def reset(self) -> None:
        self._anchor = None

    def factor(self, target: ContextTarget) -> float:
        """The correction for local estimates of requests to ``target``."""
        if self.calibration is None:
            return 1.0
        return self.calibration.input_estimate_factor(target.model_reference)

    def estimation_record(self, target: ContextTarget) -> JsonObject:
        """The ``context_estimation`` value persisted with a step's Usage."""
        record: JsonObject = {"factor": self.factor(target)}
        if self._anchor is not None:
            record["anchor"] = self._anchor.to_dict()
        return record

    def observe(
        self,
        usage: Mapping[str, Any],
        messages: Sequence[Mapping[str, Any]],
        *,
        target: ContextTarget,
        tools: Sequence[Mapping[str, Any]],
        scope: str,
    ) -> None:
        """Anchor on a measured request and teach the calibration; blocking.

        Estimated input never anchors and never replaces a measurement.
        """
        tokens = _optional_non_negative_int(usage.get("input_tokens"))
        if tokens is None or usage_token_is_estimated(usage, "input_tokens"):
            return
        key = _context_key(messages, target, tools, scope)
        request = _context_digest(messages)
        estimate = self._estimate(key, request, target, messages, tools)
        output_tokens = (
            None
            if usage_token_is_estimated(usage, "output_tokens")
            else _optional_non_negative_int(usage.get("output_tokens"))
        )
        self._anchor = _Anchor(key, request, tokens, output_tokens, estimate)
        if self.calibration is not None:
            self.calibration.record_input_estimate(
                target.model_reference, measured=tokens, estimated=estimate
            )

    def project(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        target: ContextTarget,
        tools: Sequence[Mapping[str, Any]],
        scope: str,
        context_window: int | None = None,
    ) -> JsonObject:
        """Project the request's Context in tokens.

        ``context_window`` is the effective window of the Model the request goes
        to; the projection carries it, so the Session's Context usage names the
        window it fills.
        """
        key = _context_key(messages, target, tools, scope)
        request = _context_digest(messages)
        estimate = self._estimate(key, request, target, messages, tools)
        factor = self.factor(target)
        result: JsonObject = {"tokens": round(estimate * factor), "estimated": True}
        anchor = self._anchor
        if anchor is not None and anchor.key == key:
            delta = round((estimate - anchor.estimate) * factor)
            # A subtraction that would erase a nonempty request is no evidence.
            if anchor.input_tokens + delta > 0 or estimate == 0:
                changed = request != anchor.request
                result = {
                    "tokens": max(0, anchor.input_tokens + delta),
                    "estimated": changed,
                    "provider_input_tokens": anchor.input_tokens,
                }
                if anchor.output_tokens is not None:
                    result["provider_output_tokens"] = anchor.output_tokens
                if changed:
                    result["estimated_delta_tokens"] = delta
        return with_context_window(result, context_window)

    def estimate(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        target: ContextTarget,
        tools: Sequence[Mapping[str, Any]],
    ) -> int:
        """The corrected local estimate of a whole request, ignoring the anchor."""
        key = _context_key(messages, target, tools, "")
        estimate = self._estimate(key, _context_digest(messages), target, messages, tools)
        return round(estimate * self.factor(target))

    def _estimate(
        self,
        key: str,
        request: str,
        target: ContextTarget,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> int:
        """Uncorrected wire estimate, reused for identical requests.

        The context key covers the route, prompt scope, Tool catalog and System
        Prompt, the request digest every message; only digests and counts are
        retained.
        """
        memo_key = (key, request)
        cached = self._estimates.get(memo_key)
        if cached is not None:
            self._estimates.move_to_end(memo_key)
            return cached
        estimated = estimate_wire_request_input_tokens(
            target.adapter, messages, model_id=target.model_id, tools=tools
        )
        self._estimates[memo_key] = estimated
        while len(self._estimates) > _ESTIMATE_MEMO_SIZE:
            self._estimates.popitem(last=False)
        return estimated


def _context_key(
    messages: Sequence[Mapping[str, Any]], target: ContextTarget, tools: Any, scope: str
) -> str:
    # Stable across Runs and processes: an anchor outlives the Run's Adapter.
    return _context_digest(
        [
            target.model_reference,
            scope,
            tools,
            [message for message in messages if message.get("role") == "system"],
        ]
    )


def _context_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def latest_session_context_usage(messages: list[ChatMessage]) -> JsonObject | None:
    """Return the newest durable server projection of a Session's Context.

    The newest Assistant turn with Usage anchors the projection through the
    ``context_usage`` snapshot the Agentic Loop stores on every Assistant step,
    until a newer Compaction checkpoint replaces it. Only provider-visible
    messages appended after that anchor need a new estimate. This lets
    ``chat.history`` restore the same semantic value used by live Run events
    without summing the whole transcript. An anchor without a snapshot has no
    Context projection. The newer messages are corrected by the factor the
    anchor recorded (``context_estimation``), like every other estimate.
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
        delta_tokens = _corrected_estimate(delta_messages, messages[checkpoint_index])
        return with_context_window(
            {"tokens": context_after + delta_tokens, "estimated": True},
            latest_context_window(messages),
        )

    assert assistant_index is not None
    saved_projection = (messages[assistant_index].usage or {}).get("context_usage")
    if (
        not isinstance(saved_projection, dict)
        or _optional_non_negative_int(saved_projection.get("tokens")) is None
    ):
        return None
    saved = dict(saved_projection)
    delta_messages = _provider_visible_delta(messages[assistant_index + 1 :])
    if delta_messages:
        delta_tokens = _corrected_estimate(delta_messages, messages[assistant_index])
        saved["tokens"] += delta_tokens
        saved["estimated"] = True
        saved["estimated_delta_tokens"] = saved.get("estimated_delta_tokens", 0) + delta_tokens
    return saved


def _corrected_estimate(request_messages: list[JsonObject], anchor: ChatMessage) -> int:
    """Estimate messages appended after ``anchor`` with the factor it recorded."""
    record = (anchor.usage or {}).get(CONTEXT_ESTIMATION_FIELD)
    factor = record.get("factor") if isinstance(record, Mapping) else None
    if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not factor > 0:
        factor = 1.0
    return round(estimate_request_input_tokens(request_messages)[0] * factor)


def checkpoint_context_usage(
    checkpoint: ChatMessage, context_window: int | None = None
) -> JsonObject | None:
    """Project the estimated post-Compaction Context from one checkpoint."""

    usage = checkpoint.usage or {}
    context_after = _optional_non_negative_int(usage.get("context_tokens_after"))
    if context_after is None:
        return None
    return with_context_window({"tokens": context_after, "estimated": True}, context_window)


def with_context_window(usage: JsonObject, context_window: int | None) -> JsonObject:
    """Return ``usage`` naming the Model window it fills, when that is known."""
    if context_window is None or context_window <= 0:
        return usage
    return {**usage, "context_window": context_window}


def latest_context_window(messages: Sequence[ChatMessage]) -> int | None:
    """Return the window of the Model that last answered in the Session, if recorded."""
    for message in reversed(messages):
        if message.role != "assistant" or not isinstance(message.usage, dict):
            continue
        projection = message.usage.get("context_usage")
        if isinstance(projection, dict):
            window = _optional_non_negative_int(projection.get("context_window"))
            if window:
                return window
    return None


def aggregate_session_usage(messages: list[ChatMessage]) -> JsonObject:
    """Sum token usage across a session's assistant turns.

    Returns the canonical ``session_usage`` payload carried by the
    ``chat.history`` response and the terminal Run events. Every Assistant
    turn counts, estimated counters included; a counter a turn does not
    report counts as zero. Canonical ``input_tokens`` already includes cached
    tokens, so ``cache_read_tokens``/``cache_write_tokens`` are subsets of the
    input total, never added on top, and the Session's cache hit rate is
    ``cache_read_tokens / input_tokens``. Canonical ``reasoning_tokens`` is a
    subset of ``output_tokens`` and likewise never changes totals.
    """
    totals = _empty_session_usage()
    for message in messages:
        if message.role != "assistant" or not isinstance(message.usage, dict):
            continue
        totals = add_session_turn_usage(totals, message.usage)
    return totals


def add_session_turn_usage(totals: JsonObject, usage: JsonObject) -> JsonObject:
    """Return canonical session totals with one persisted assistant turn added."""
    return {
        key: _non_negative_int(totals.get(key)) + _non_negative_int(usage.get(key))
        for key in _SESSION_USAGE_KEYS
    }


_SESSION_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def _empty_session_usage() -> JsonObject:
    return dict.fromkeys(_SESSION_USAGE_KEYS, 0)


def _non_negative_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return value


def _optional_non_negative_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
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
