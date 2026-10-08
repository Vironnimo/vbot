"""Learn wire facts from a Provider's request rejections and streamed replies.

A Model that vBot has not verified can reject an optional request parameter
(``temperature`` on a reasoning Model), a combination of them (``temperature``
together with ``top_p``), a reasoning effort value (``minimal``
where only ``low`` exists) or the explicit off switch of its reasoning dialect
(``thinking: {type: disabled}`` on an always-thinking Model). Instead of failing
every later request the same way, the codec records the rejection as a learned
wire fact, rebuilds the request from the updated wire profile and retries.
Learned facts feed the profile below every explicit Model entry, so later
requests start from the corrected shape and a configured Model entry is never
silently overridden.

Every request path learns the same way. A request
(:func:`execute_learning_from_rejections`) is established when its reply
arrives; a stream (:func:`stream_learning_from_rejections`) when its first
delta arrives, so a rejection that a wire reports as an in-band stream event
(the Codex WebSocket) is learned like an HTTP rejection. A stream also records
that the Model returned reasoning, and that it ignores reasoning off when it
reasons although the request turned reasoning off.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Literal

from core.providers.errors import ProviderAuthError, ProviderError
from core.providers.reasoning import detail_names_rejected_effort, split_inline_reasoning
from core.providers.reasoning_dialects import ReasoningCarriers, dialect_carriers
from core.providers.wire_profile import WireProfile
from core.providers.wire_profiles import WireBinding
from core.utils.logging import get_logger

__all__ = ["execute_learning_from_rejections", "stream_learning_from_rejections"]

_LOGGER = get_logger("providers.wire_learning")

_MAX_LESSONS = 2
"""At most two lessons (rejected parameter, parameter combination, effort or off
switch) per request."""

_SAMPLING_PARAMETERS: tuple[str, ...] = ("temperature", "top_p", "top_k")
"""Sampling parameters a backend may reject for one Model and accept for another."""

_SAMPLING_CARRIERS: Mapping[str, tuple[tuple[str, ...], Mapping[str, str]]] = {
    "gemini": (
        ("generationConfig",),
        {"temperature": "temperature", "top_p": "topP", "top_k": "topK"},
    ),
}
"""Protocols that carry the sampling parameters below the top level or renamed:
``protocol -> (location path, parameter -> request field)``. Every other protocol
sends them as top-level fields under their own names."""

_PARAMETER_REJECTION_MARKERS: tuple[str, ...] = (
    "unsupported parameter",
    "unsupported_parameter",
    "not supported",
    "does not support",
    "unknown parameter",
    "unrecognized request argument",
    "unrecognized parameter",
    "invalid parameter",
    "is deprecated for this model",
)
"""A parameter rejection names one of these markers and the parameter."""

_EXCLUSIVE_REJECTION_MARKERS: tuple[str, ...] = (
    "cannot both be specified",
    "cannot both be set",
    "cannot be specified together",
    "cannot be used together",
    "mutually exclusive",
    "only one of",
)
"""A combination rejection names one of these markers and the parameters combined."""

_OFF_REJECTION_MARKERS: tuple[str, ...] = (
    *_PARAMETER_REJECTION_MARKERS,
    "unsupported value",
    "unsupported_value",
    "not allowed",
    "cannot be disabled",
)
"""An off-switch rejection names one of these markers, the switch and its off value."""

_REASONING_META_PROOF_KEYS: tuple[str, ...] = (
    "reasoning_details",
    "encrypted_content",
    "reasoning_items",
    "content_blocks",
)
"""Reasoning-meta entries only a Model that reasoned returns: Chat Completions
``reasoning_details``/``encrypted_content``, Responses reasoning items and
Messages thinking blocks. Response ids, the raw Responses output and Gemini
parts are returned with or without reasoning."""

_INLINE_REASONING_WATCH_CHARACTERS = 64 * 1024
"""How much answer content an off request is watched for inline reasoning;
Models put it at the start, so a longer answer is no longer checked."""


@dataclass(frozen=True)
class _Lesson:
    """One rejection attributed to what the request carried."""

    kind: Literal["parameter", "exclusive", "effort"]
    rejected: tuple[str, ...]
    """The sampling parameter, the combined parameters (the one to keep first),
    or the effort value (``none`` for the off switch)."""
    description: str


async def execute_learning_from_rejections[T](
    execute_attempt: Callable[[], Awaitable[T]],
    payload: dict[str, Any],
    *,
    rebuild: Callable[[], dict[str, Any]],
    wire: WireBinding,
    model_id: str,
    provider_label: str,
) -> T:
    """Run one request, learning from a rejected parameter, effort or off switch.

    ``execute_attempt`` performs one full (``retry_async``-wrapped) request over
    the shared ``payload`` dict. On a fatal, non-auth ``ProviderError`` whose
    detail attributes the rejection to what ``payload`` carries, the fact is
    recorded, ``payload`` is replaced by ``rebuild()`` and the request runs
    again:

    - a sampling parameter the payload carries, named next to a rejection
      marker, is dropped;
    - two or more sampling parameters the payload carries, named next to a
      marker that they cannot be combined, become a group of which only the
      first (``temperature``, ``top_p``, ``top_k`` order) is sent;
    - the effort value the payload carries, named next to its field, leaves the
      ladder (an explicit ``none`` outside the ladder becomes an omitted off);
    - the dialect's explicit off switch (``thinking: {type: disabled}``), named
      with its value next to a rejection marker, is recorded as a rejected
      ``none`` effort, which turns the off render into omission.

    Parameters rejected or combined during this request stay removed even when
    an explicit Model entry keeps sending them. Every other error (auth, retryable, a
    rejection of something the payload does not carry, a detail without a
    rejection marker), and a rebuild that changes nothing, propagates unchanged.
    """

    rejected_parameters: set[str] = set()
    exclusive_groups: list[tuple[str, ...]] = []
    for _ in range(_MAX_LESSONS):
        try:
            return await execute_attempt()
        except ProviderError as error:
            if error.retryable or isinstance(error, ProviderAuthError):
                raise
            profile = wire.profile(model_id)
            lesson = _learn(str(error), payload, profile)
            if lesson is None:
                raise
            if lesson.kind == "parameter":
                wire.observe_rejected_parameter(model_id, lesson.rejected[0])
                rejected_parameters.add(lesson.rejected[0])
            elif lesson.kind == "exclusive":
                wire.observe_exclusive_parameters(model_id, lesson.rejected)
                exclusive_groups.append(lesson.rejected)
            else:
                wire.observe_rejected_effort(model_id, lesson.rejected[0])
            rebuilt = rebuild()
            for parameter in rejected_parameters:
                _remove_sampling_parameter(rebuilt, profile.protocol, parameter)
            for group in exclusive_groups:
                carried = [
                    parameter
                    for parameter in group
                    if _carries_sampling_parameter(rebuilt, profile.protocol, parameter)
                ]
                for parameter in carried[1:]:
                    _remove_sampling_parameter(rebuilt, profile.protocol, parameter)
            if rebuilt == payload:
                raise
            _LOGGER.warning(
                "%s rejected %s for %s; retrying with the learned wire shape",
                provider_label,
                lesson.description,
                model_id,
            )
            payload.clear()
            payload.update(rebuilt)
    return await execute_attempt()


async def stream_learning_from_rejections(
    open_stream: Callable[[], AsyncGenerator[dict[str, Any]]],
    payload: dict[str, Any],
    *,
    rebuild: Callable[[], dict[str, Any]],
    wire: WireBinding,
    model_id: str,
    provider_label: str,
) -> AsyncGenerator[dict[str, Any]]:
    """Stream one request, learning from a rejection before its first delta.

    ``open_stream`` opens one full stream exchange over the shared ``payload``.
    An error raised before the stream yields its first delta (an HTTP rejection
    or an in-band error event) is learned and retried exactly like
    :func:`execute_learning_from_rejections`; once a delta was yielded, every
    error propagates unchanged. A delta carrying reasoning records that the
    Model returned reasoning; when the request turned reasoning off, reasoning
    in a delta or inline in the answer (``split_inline_reasoning``) records
    that the Model ignores reasoning off.
    """

    async def establish() -> tuple[AsyncGenerator[dict[str, Any]], dict[str, Any] | None]:
        stream = open_stream()
        try:
            return stream, await anext(stream)
        except StopAsyncIteration:
            return stream, None
        except BaseException:
            await stream.aclose()
            raise

    stream, first = await execute_learning_from_rejections(
        establish,
        payload,
        rebuild=rebuild,
        wire=wire,
        model_id=model_id,
        provider_label=provider_label,
    )
    returned_reasoning = False
    off_watch = _OffWatch() if _requests_off(payload, wire.profile(model_id)) else None
    async with aclosing(stream):
        if first is None:
            return
        delta = first
        while True:
            if not returned_reasoning and _delta_returns_reasoning(delta):
                returned_reasoning = True
                wire.observe_reasoning_returned(model_id)
            if off_watch is not None and off_watch.reasons(delta, returned_reasoning):
                off_watch = None
                wire.observe_off_ignored(model_id)
            yield delta
            try:
                delta = await anext(stream)
            except StopAsyncIteration:
                return


def _learn(detail: str, payload: Mapping[str, Any], profile: WireProfile) -> _Lesson | None:
    lowered = detail.lower()
    named = tuple(
        parameter
        for parameter in _SAMPLING_PARAMETERS
        if _names_field(lowered, parameter)
        and _carries_sampling_parameter(payload, profile.protocol, parameter)
    )
    if len(named) >= 2 and any(marker in lowered for marker in _EXCLUSIVE_REJECTION_MARKERS):
        listed = " with ".join(repr(parameter) for parameter in named)
        return _Lesson("exclusive", named, f"parameters {listed} combined")
    if named and any(marker in lowered for marker in _PARAMETER_REJECTION_MARKERS):
        return _Lesson("parameter", named[:1], f"parameter {named[0]!r}")
    carriers = dialect_carriers(profile.reasoning.dialect)
    # Only a rejection that names the value this request sent is attributable;
    # anything vaguer must not teach every later request a narrower wire.
    effort = _value_at(payload, carriers.effort)
    if (
        isinstance(effort, str)
        and _names_effort_field(lowered, carriers.effort)
        and _names_value(lowered, effort)
    ):
        return _Lesson("effort", (effort,), f"reasoning effort {effort!r}")
    if _rejects_off_switch(lowered, payload, carriers):
        switch = ".".join(carriers.off_switch)
        return _Lesson("effort", ("none",), f"reasoning off switch {switch}={carriers.off_value!r}")
    return None


def _requests_off(payload: Mapping[str, Any], profile: WireProfile) -> bool:
    """Whether ``payload`` turns reasoning off: a ``none`` effort or the off switch."""

    carriers = dialect_carriers(profile.reasoning.dialect)
    if carriers.effort and _value_at(payload, carriers.effort) == "none":
        return True
    return (
        bool(carriers.off_switch)
        and carriers.off_value is not None
        and _value_at(payload, carriers.off_switch) == carriers.off_value
    )


class _OffWatch:
    """Watch a stream whose request turned reasoning off for reasoning anyway."""

    def __init__(self) -> None:
        self._content = ""

    def reasons(self, delta: Mapping[str, Any], returned_reasoning: bool) -> bool:
        if returned_reasoning:
            return True
        text = delta.get("text") if delta.get("type") == "content_delta" else None
        if not isinstance(text, str) or len(self._content) >= _INLINE_REASONING_WATCH_CHARACTERS:
            return False
        self._content += text
        # Every reasoning tag ends with ``>``; only such a delta can complete one.
        return ">" in text and split_inline_reasoning(self._content, None)[1] is not None


def _rejects_off_switch(
    lowered: str, payload: Mapping[str, Any], carriers: ReasoningCarriers
) -> bool:
    switch = carriers.off_switch
    value = carriers.off_value
    return (
        bool(switch)
        and value is not None
        and _value_at(payload, switch) == value
        and any(marker in lowered for marker in _OFF_REJECTION_MARKERS)
        and _names_field(lowered, switch[0])
        and _names_value(lowered, value)
    )


def _names_effort_field(lowered: str, path: tuple[str, ...]) -> bool:
    if not path:
        return False
    if detail_names_rejected_effort(lowered):
        return True
    dotted = (".".join(_snake(segment) for segment in path), ".".join(path).lower())
    if any(spelling in lowered for spelling in dotted):
        return True
    # A compound leaf (``thinkingLevel``) is distinctive alone; ``effort`` is not.
    leaf = path[-1]
    return "_" in _snake(leaf) and _names_field(lowered, leaf)


def _names_field(lowered: str, name: str) -> bool:
    words = _snake(name).split("_")
    spellings = dict.fromkeys(("_".join(words), "".join(words), " ".join(words)))
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(spelling)}(?![a-z0-9])", lowered) is not None
        for spelling in spellings
    )


def _names_value(lowered: str, value: str) -> bool:
    return re.search(rf"(?<![a-z_]){re.escape(value.lower())}(?![a-z_])", lowered) is not None


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def _value_at(payload: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = payload if path else None
    for segment in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(segment)
    return value


def _sampling_carrier(protocol: str, parameter: str) -> tuple[tuple[str, ...], str]:
    location, fields = _SAMPLING_CARRIERS.get(protocol, ((), {}))
    return location, fields.get(parameter, parameter)


def _carries_sampling_parameter(payload: Mapping[str, Any], protocol: str, parameter: str) -> bool:
    location, field = _sampling_carrier(protocol, parameter)
    container = _value_at(payload, location) if location else payload
    return isinstance(container, Mapping) and field in container


def _remove_sampling_parameter(payload: dict[str, Any], protocol: str, parameter: str) -> None:
    location, field = _sampling_carrier(protocol, parameter)
    container = _value_at(payload, location) if location else payload
    if isinstance(container, dict):
        container.pop(field, None)


def _delta_returns_reasoning(delta: Mapping[str, Any]) -> bool:
    kind = delta.get("type")
    if kind == "reasoning_delta":
        text = delta.get("text")
        return isinstance(text, str) and bool(text)
    if kind == "reasoning_meta":
        meta = delta.get("reasoning_meta")
        return isinstance(meta, Mapping) and any(
            meta.get(key) for key in _REASONING_META_PROOF_KEYS
        )
    return False
