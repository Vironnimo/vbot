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
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.chat.messages import (
    CONTEXT_ESTIMATION_FIELD,
    ChatMessage,
    usage_token_is_estimated,
)
from core.chat.wire_shaping import _embed_notes_into_request
from core.providers.adapter import estimate_wire_request_input_tokens
from core.providers.providers import RequestInput
from core.utils.tokens import ESTIMATOR_VERSION, estimate_request_input_tokens

JsonObject = dict[str, Any]
# Whole-request counts by digest, for a request that is neither the newest
# counted one nor a continuation of it (an earlier request counted again).
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
class RequestContextEstimate:
    """Digests and an uncorrected count of one prepared request, without its content.

    Reuse this value only for that exact request. A new hook result, continuation,
    image projection, route or prompt scope needs a fresh ``prepare`` call. The
    measurement anchor and calibration remain live when this value is projected.
    """

    model_reference: str
    key: str
    request: str
    tokens: int


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


@dataclass(frozen=True)
class _CountedRequest:
    """The newest counted request, so that the next request counts only what changed.

    It references the request's messages, which the Run's live request shares,
    and never copies them. ``fields`` holds each message's top-level keys and
    values to notice a change in place; ``digests`` its content digest.
    ``window`` is where the newest window starts (``_opens_window``) and
    ``window_tokens`` the wire count of the request from there, with its System
    Prompt and without Tools; ``calls`` maps each Tool-call id to the first
    message calling it.
    """

    key: str
    adapter: Any
    model_id: str
    messages: tuple[Mapping[str, Any], ...]
    fields: tuple[tuple[Any, ...], ...]
    digests: tuple[bytes, ...]
    system: tuple[int, ...]
    calls: Mapping[str, int]
    request: str
    tokens: int
    window: int | None
    window_tokens: int


@dataclass
class RequestContextUsage:
    """A Session's Context accounting for one Run.

    The anchor is the newest measured request with its uncorrected local
    estimate; local estimation error in the unchanged part cancels out. It
    continues across Runs through the ``context_estimation`` record each
    Assistant step persists (``resume``) and holds while the route, prompt
    scope, System Prompt and Tool catalog stay the same; Compaction ends it.
    Measured input is never scaled; only estimated parts are corrected. Every
    measured request also teaches the calibration.

    Counting keeps digests and counts plus references to the newest counted
    request's messages, never copies or pixels of its own: an identical request
    is counted once, and a continuation counts only its newest window
    (``_count``).
    """

    calibration: InputEstimateCalibration | None = None
    _anchor: _Anchor | None = None
    _estimates: OrderedDict[tuple[str, str], int] = field(default_factory=OrderedDict, repr=False)
    _counted: _CountedRequest | None = field(default=None, repr=False)

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
        self.observe_prepared(
            usage, self.prepare(messages, target=target, tools=tools, scope=scope)
        )

    def prepare(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        target: ContextTarget,
        tools: Sequence[Mapping[str, Any]],
        scope: str,
    ) -> RequestContextEstimate:
        """Count one request once for its send, measured response and Context consumers."""
        key = _context_key(messages, target, tools, scope)
        request, tokens = self._count(key, target, messages, tools)
        return RequestContextEstimate(target.model_reference, key, request, tokens)

    def observe_prepared(self, usage: Mapping[str, Any], prepared: RequestContextEstimate) -> None:
        """Anchor on the response to an exactly prepared request; blocking calibration write."""
        tokens = _optional_non_negative_int(usage.get("input_tokens"))
        if tokens is None or usage_token_is_estimated(usage, "input_tokens"):
            return
        output_tokens = (
            None
            if usage_token_is_estimated(usage, "output_tokens")
            else _optional_non_negative_int(usage.get("output_tokens"))
        )
        self._anchor = _Anchor(
            prepared.key, prepared.request, tokens, output_tokens, prepared.tokens
        )
        if self.calibration is not None:
            self.calibration.record_input_estimate(
                prepared.model_reference, measured=tokens, estimated=prepared.tokens
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

        ``context_window`` is the Context capacity of the Model the request goes
        to: its effective window, or the Provider's smaller input limit. The
        projection carries it, so the Session's Context usage names the window
        it fills.
        """
        return self.project_prepared(
            self.prepare(messages, target=target, tools=tools, scope=scope),
            context_window=context_window,
        )

    def project_prepared(
        self, prepared: RequestContextEstimate, *, context_window: int | None = None
    ) -> JsonObject:
        """Project an unchanged request with the current anchor and calibration; no scanning."""
        factor = (
            self.calibration.input_estimate_factor(prepared.model_reference)
            if self.calibration is not None
            else 1.0
        )
        result: JsonObject = {"tokens": round(prepared.tokens * factor), "estimated": True}
        anchor = self._anchor
        if anchor is not None and anchor.key == prepared.key:
            delta = round((prepared.tokens - anchor.estimate) * factor)
            # A subtraction that would erase a nonempty request is no evidence.
            if anchor.input_tokens + delta > 0 or prepared.tokens == 0:
                changed = prepared.request != anchor.request
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
        _request, estimate = self._count(key, target, messages, tools)
        return round(estimate * self.factor(target))

    def _count(
        self,
        key: str,
        target: ContextTarget,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> tuple[str, int]:
        """The request digest and uncorrected wire count, from the newest counted request.

        The context key covers the route, prompt scope, Tool catalog and System
        Prompt. Messages the newest counted request holds unchanged keep their
        digests; only the others are digested.

        A request that keeps the newest counted request up to that request's
        newest window start (``_opens_window``) counts as the counted request
        plus the change of the window count; windows count with the System
        Prompt and without Tools (``_window``). This equals the whole-request
        wire estimate: each wire estimator sums independent per-item counts,
        list framing that grows by one per item, and a part fixed by the System
        Prompt and Tools; from a window start on, each wire renders the request
        apart from what precedes it while no Tool-call id repeats
        (``_window_calls``). Any other request is counted whole. Two rare cases
        can differ from a whole count: a wire id derived from a digest that
        collides with another id, and a request over the Gemini wire's image
        cap, which a whole count refuses; Chat limits a request's images to its
        route's cap before counting.
        """
        request = list(messages)
        counted = self._counted
        shared = 0 if counted is None else _shared_prefix(counted, request)
        same_route = (
            counted is not None
            and counted.key == key
            and counted.adapter is target.adapter
            and counted.model_id == target.model_id
        )
        if counted is not None and same_route and shared == len(request) == len(counted.messages):
            return counted.request, counted.tokens

        new = request[shared:]
        fields = (() if counted is None else counted.fields[:shared]) + tuple(map(_fields, new))
        digests = (() if counted is None else counted.digests[:shared]) + tuple(
            map(_message_digest, new)
        )
        system = (
            () if counted is None else tuple(i for i in counted.system if i < shared)
        ) + tuple(
            shared + index for index, message in enumerate(new) if message.get("role") == "system"
        )
        digest = hashlib.sha256(b"".join(digests)).hexdigest()
        counts = None
        if (
            counted is not None
            and same_route
            and counted.window is not None
            and counted.window < shared
        ):
            counts = _count_from_window(counted, request, system, target)
        if counts is None:
            counts = self._count_whole(key, digest, request, system, target, tools)
        tokens, calls, window, window_tokens = counts
        self._remember(key, digest, tokens)
        self._counted = _CountedRequest(
            key,
            target.adapter,
            target.model_id,
            tuple(request),
            fields,
            digests,
            system,
            calls,
            digest,
            tokens,
            window,
            window_tokens,
        )
        return digest, tokens

    def _count_whole(
        self,
        key: str,
        digest: str,
        request: list[Mapping[str, Any]],
        system: tuple[int, ...],
        target: ContextTarget,
        tools: Sequence[Mapping[str, Any]],
    ) -> tuple[int, dict[str, int], int | None, int]:
        """Count a whole request and its newest window."""
        tokens = self._estimates.get((key, digest))
        if tokens is None:
            tokens = _wire_count(target, request, tools)
        window = _newest_window(request, 0)
        window_tokens = 0
        if window is not None:
            window_tokens = _wire_count(target, _window(request, window, system))
        return tokens, _call_indices(request), window, window_tokens

    def _remember(self, key: str, digest: str, tokens: int) -> None:
        memo_key = (key, digest)
        self._estimates[memo_key] = tokens
        self._estimates.move_to_end(memo_key)
        while len(self._estimates) > _ESTIMATE_MEMO_SIZE:
            self._estimates.popitem(last=False)


def _count_from_window(
    counted: _CountedRequest,
    request: list[Mapping[str, Any]],
    system: tuple[int, ...],
    target: ContextTarget,
) -> tuple[int, dict[str, int], int | None, int] | None:
    """Count ``request`` by the change of the counted request's newest window, if exact."""
    assert counted.window is not None
    window = counted.window
    calls = _window_calls(request, window, counted.calls)
    if calls is None:
        return None
    window_tokens = _wire_count(target, _window(request, window, system))
    tokens = counted.tokens + window_tokens - counted.window_tokens
    newest = _newest_window(request, window + 1)
    if newest is not None:
        window = newest
        window_tokens = _wire_count(target, _window(request, window, system))
    return tokens, calls, window, window_tokens


def _wire_count(
    target: ContextTarget,
    messages: list[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] = (),
) -> int:
    return estimate_wire_request_input_tokens(
        target.adapter, messages, model_id=target.model_id, tools=tools
    )


def _opens_window(message: Mapping[str, Any]) -> bool:
    """Whether every wire renders the request from ``message`` on apart from what precedes it.

    A user message and an Assistant message with Tool Calls each render at
    least one wire item, end the Tool Result batch before them (Messages
    grouping, Tool media moved behind the batch) and start a new Tool-call
    correlation.
    """
    role = message.get("role")
    if role == "user":
        return True
    calls = message.get("tool_calls")
    return (
        role == "assistant"
        and isinstance(calls, (list, tuple))
        and any(isinstance(call, Mapping) for call in calls)
    )


def _newest_window(request: list[Mapping[str, Any]], start: int) -> int | None:
    """The index of the last window-opening message at or after ``start``."""
    for index in range(len(request) - 1, start - 1, -1):
        if _opens_window(request[index]):
            return index
    return None


def _window(
    request: list[Mapping[str, Any]], start: int, system: tuple[int, ...]
) -> list[Mapping[str, Any]]:
    """The request from ``start`` on, after the System Prompt messages before it."""
    return [*(request[index] for index in system if index < start), *request[start:]]


def _tool_call_ids(message: Mapping[str, Any]) -> Iterator[str]:
    calls = message.get("tool_calls")
    if message.get("role") != "assistant" or not isinstance(calls, (list, tuple)):
        return
    for call in calls:
        if isinstance(call, Mapping) and isinstance(call.get("id"), str):
            yield call["id"]


def _call_indices(request: list[Mapping[str, Any]]) -> dict[str, int]:
    """Each Tool-call id with the index of the first message calling it."""
    calls: dict[str, int] = {}
    for index, message in enumerate(request):
        for call_id in _tool_call_ids(message):
            calls.setdefault(call_id, index)
    return calls


def _window_calls(
    request: list[Mapping[str, Any]], start: int, calls: Mapping[str, int]
) -> dict[str, int] | None:
    """The Tool-call ids of ``request`` if it counts apart from ``start``, else ``None``.

    Wires normalize Tool-call ids across the whole request, and Gemini names a
    Tool Result after its call wherever that is. So the window counts apart only
    when it repeats no Tool-call id and answers no call made before it.
    """
    merged = {call_id: index for call_id, index in calls.items() if index < start}
    for index in range(start, len(request)):
        message = request[index]
        if message.get("role") == "tool":
            answered = message.get("tool_call_id")
            if isinstance(answered, str) and merged.get(answered, start) < start:
                return None
            continue
        for call_id in _tool_call_ids(message):
            if call_id in merged:
                return None
            merged[call_id] = index
    return merged


def _shared_prefix(counted: _CountedRequest, request: list[Mapping[str, Any]]) -> int:
    """How many leading messages of ``request`` the counted request holds, unchanged."""
    limit = min(len(counted.messages), len(request))
    for index in range(limit):
        message = request[index]
        if message is not counted.messages[index] or _fields(message) != counted.fields[index]:
            return index
    return limit


def _fields(message: Mapping[str, Any]) -> tuple[Any, ...]:
    return (*message, *message.values())


def _message_digest(message: Mapping[str, Any]) -> bytes:
    return hashlib.sha256(
        json.dumps(message, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).digest()


def _context_key(
    messages: Sequence[Mapping[str, Any]], target: ContextTarget, tools: Any, scope: str
) -> str:
    # Stable across Runs and processes: an anchor outlives the Run's Adapter.
    # Only the System Prompt text reaches the wire; every request build makes a
    # system message with a new id and timestamp, which must not end the anchor.
    # An anchor's estimate from another estimator version is no base for a delta.
    return _context_digest(
        [
            ESTIMATOR_VERSION,
            target.model_reference,
            scope,
            tools,
            [message.get("content") for message in messages if message.get("role") == "system"],
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


def projected_request_input(context_usage: Mapping[str, Any]) -> RequestInput:
    """The request input a Context projection describes, measured and estimated parts apart.

    Against a Provider measurement only the estimated change since it is an
    estimate; without one the whole projection is. Only estimated parts carry
    the estimation margin when the request's output is sized.
    """
    tokens = _non_negative_int(context_usage.get("tokens"))
    if _optional_non_negative_int(context_usage.get("provider_input_tokens")) is None:
        return RequestInput.estimate(tokens)
    if not context_usage.get("estimated"):
        return RequestInput.measured(tokens)
    change = context_usage.get("estimated_delta_tokens")
    if not isinstance(change, int) or isinstance(change, bool):
        return RequestInput.estimate(tokens)
    return RequestInput.measured(tokens, estimated_change=change)


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
