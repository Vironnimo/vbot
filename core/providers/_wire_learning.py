"""Learn wire facts from a Provider's request rejections and streamed replies.

A Model that vBot has not verified can reject an optional request parameter
(``temperature`` on a reasoning Model), a combination of them (``temperature``
together with ``top_p``), a reasoning effort value (``minimal``
where only ``low`` exists) or the explicit off switch of its reasoning dialect
(``thinking: {type: disabled}`` on an always-thinking Model) or a mid-conversation
effort update (``core.providers.effort_updates``). A rejection can also state
the Model's output maximum or Context window. Instead of failing
every later request the same way, the codec records the rejection as a learned
wire fact, rebuilds the request from the updated wire profile and retries.
Learned facts feed the profile below every explicit Model entry, so later
requests start from the corrected shape and a configured Model entry is never
silently overridden; a stated output maximum bounds every layer, and a stated
Context window replaces the catalog window the Adapter budgets against.

An explicit image-count rejection instead records the limit and raises
``ProviderImageLimitError``: Chat alone knows which delivered images can retire.
It can arrive after heartbeats or Usage, but never teaches after Model output.
Native Ollama borrows this image learner without enabling parameter learning.

Every request path learns the same way. A request
(:func:`execute_learning_from_rejections`) is established when its reply
arrives; a stream (:func:`stream_learning_from_rejections`) when its first
delta arrives, so a rejection that a wire reports as an in-band stream event
(the Codex WebSocket) is learned like an HTTP rejection. A stream also records
that the Model returned reasoning, and that it ignores reasoning off when it
reasons although the request turned reasoning off.

Both take the request's :class:`RequestPlan` and render it off the Event Loop:
the first build and every rebuild after a lesson run in one request-body worker
hop each, together with encoding, the size check and the comparison that
decides whether a rebuild changed anything.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Literal

from core.providers.errors import (
    ProviderAuthError,
    ProviderContextOverflowError,
    ProviderError,
    ProviderImageLimitError,
)
from core.providers.http import (
    PreparedRequest,
    RequestPlan,
    prepare_request,
    revise_request,
)
from core.providers.reasoning import detail_names_rejected_effort, split_inline_reasoning
from core.providers.reasoning_dialects import ReasoningCarriers, dialect_carriers
from core.providers.wire_profile import WireProfile
from core.providers.wire_profiles import WireBinding
from core.utils.logging import get_logger

__all__ = [
    "execute_learning_from_rejections",
    "learn_image_limit",
    "stream_learning_from_rejections",
]

_LOGGER = get_logger("providers.wire_learning")


class FailureUsageDelta(dict[str, Any]):
    """Usage immediately followed by an in-band error, before learning decides to retry."""


_USAGE_TOTAL_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "reported_cost_usd",
)


def _failed_attempt_usage(deltas: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Sum reported counters for rejected attempts, leaving absent measurements unknown."""
    total: dict[str, Any] = {"type": "usage"}
    for delta in deltas:
        for key in _USAGE_TOTAL_FIELDS:
            value = delta.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                total[key] = total.get(key, 0) + value
    return total if len(total) > 1 else None


def _include_failed_attempt_usage(
    delta: dict[str, Any], failed: dict[str, Any] | None, *, repaired: bool
) -> dict[str, Any]:
    """Add earlier attempts to this attempt's cumulative snapshots, never to speed."""
    if not repaired or delta.get("type") != "usage":
        return delta
    result = dict(delta)
    result["last_request_usage"] = {key: delta[key] for key in _USAGE_TOTAL_FIELDS if key in delta}
    if failed is None:
        return result
    for key in _USAGE_TOTAL_FIELDS:
        value = delta.get(key)
        previous = failed.get(key)
        if isinstance(value, (int, float)) and isinstance(previous, (int, float)):
            result[key] = value + previous
    return result


_MAX_LESSONS = 2
"""At most two lessons (rejected parameter, parameter combination, effort, off
switch, effort update or stated limit) per request."""

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


_EFFORT_UPDATE_REJECTION = re.compile(
    r"messages\.\d+\.output_config|per-turn effort|configuration_update"
)
"""A rejection of an effort update names its carrier: a per-message Messages
``output_config`` or a Responses ``configuration_update`` item."""

_STATED_OUTPUT_MAXIMUM: tuple[re.Pattern[str], ...] = (
    # OpenAI and backends that copy it: "max_tokens is too large: 384000. This
    # model supports at most 131072 completion tokens."
    re.compile(r"supports at most (\d+) completion tokens"),
    # Anthropic Messages: "max_tokens: 128001 > 64000, which is the maximum
    # allowed number of output tokens for claude-opus-4-5-20251101".
    re.compile(r"max_tokens: \d+ > (\d+), which is the maximum allowed number of output tokens"),
    # DeepSeek: "Invalid max_tokens value, the valid range of max_tokens is [1, 393216]".
    re.compile(r"the valid range of max_tokens is \[1, (\d+)\]"),
)
"""Rejection wordings that state the Model's output maximum (lowercased)."""

_STATED_CONTEXT_WINDOW: tuple[re.Pattern[str], ...] = (
    # OpenAI and vLLM ("This model's ..."), OpenRouter ("This endpoint's ...").
    re.compile(r"this (?:model|endpoint)'s maximum context length is (\d+) tokens"),
    # SGLang: "Requested token count exceeds the model's maximum context length of 32768 tokens."
    re.compile(r"exceeds the model's maximum context length of (\d+) tokens"),
    # Anthropic Messages: "prompt is too long: 208310 tokens > 200000 maximum" and
    # "input length and `max_tokens` exceed context limit: 197879 + 21333 > 200000".
    re.compile(r"prompt is too long: \d+ tokens > (\d+) maximum"),
    re.compile(r"exceed context limit: \d+ \+ \d+ > (\d+)"),
)
"""Rejection wordings that state the Model's Context window (lowercased)."""

_LLAMA_CPP_CONTEXT_ERROR = "exceed_context_size_error"
_LLAMA_CPP_CONTEXT_WINDOW = re.compile(r'"n_ctx"\s*:\s*(\d+)')
"""llama.cpp's server rejects an oversized request with this error type and
states the window of the serving slot as ``n_ctx`` in the error body."""

_OUTPUT_LIMIT_FIELDS: tuple[tuple[str, ...], ...] = (
    ("max_tokens",),
    ("max_completion_tokens",),
    ("max_output_tokens",),
    ("generationConfig", "maxOutputTokens"),
)
"""Where a request carries its output allowance, on every protocol."""


@dataclass(frozen=True)
class _Lesson:
    """One rejection attributed to what the request carried."""

    kind: Literal[
        "parameter", "exclusive", "effort", "effort_updates", "max_output", "context_window"
    ]
    rejected: tuple[str, ...]
    """The sampling parameter, the combined parameters (the one to keep first),
    the effort value (``none`` for the off switch), or nothing (effort updates
    and stated limits)."""
    description: str
    limit: int = 0
    """The stated output maximum or Context window."""


async def execute_learning_from_rejections[T](
    execute_attempt: Callable[[PreparedRequest], Awaitable[T]],
    plan: RequestPlan,
    *,
    wire: WireBinding,
    model_id: str,
    provider_label: str,
) -> T:
    """Run one request, learning from a rejected parameter, effort or off switch.

    The request is rendered from ``plan`` off the Event Loop, and
    ``execute_attempt`` performs one full (``retry_async``-wrapped) request with
    it. On a fatal, non-auth ``ProviderError`` whose detail attributes the
    rejection to what the sent payload carries, the fact is recorded, the plan
    is rebuilt and the request runs again:

    - a sampling parameter the payload carries, named next to a rejection
      marker, is dropped;
    - two or more sampling parameters the payload carries, named next to a
      marker that they cannot be combined, become a group of which only the
      first (``temperature``, ``top_p``, ``top_k`` order) is sent;
    - the effort value the payload carries, named next to its field, leaves the
      ladder (an explicit ``none`` outside the ladder becomes an omitted off);
    - the dialect's explicit off switch (``thinking: {type: disabled}``), named
      with its value next to a rejection marker, is recorded as a rejected
      ``none`` effort, which turns the off render into omission;
    - an effort update the payload carries, named by its carrier, turns effort
      updates off for the Model, so the request sends the effort now in effect;
    - an output maximum the detail states in a known wording, below the output
      allowance the payload carries, becomes the Model's output cap;
    - a Context window the detail states in a known wording becomes the window
      the Adapter clamps the output allowance against.

    Parameters rejected or combined during this request stay removed even when
    an explicit Model entry keeps sending them. A stated limit is learned from a
    retryable error too (a router such as OpenRouter reports it as one), because
    its number holds whatever the classification. Every other error (auth,
    retryable, a rejection of something the payload does not carry, a detail
    without a rejection marker), and a rebuild that changes nothing, propagates
    unchanged.
    """

    request = await prepare_request(plan)
    rejected_parameters: set[str] = set()
    exclusive_groups: list[tuple[str, ...]] = []
    for _ in range(_MAX_LESSONS):
        try:
            return await execute_attempt(request)
        except ProviderError as error:
            if isinstance(error, ProviderAuthError):
                raise
            profile = wire.profile(model_id)
            image_error = learn_image_limit(
                error,
                _request_image_count(request.payload, profile.protocol),
                wire=wire,
                model_id=model_id,
            )
            if image_error is not None:
                raise image_error from error
            lesson = _learn(str(error), request.payload, profile, limits_only=error.retryable)
            if lesson is None:
                raise
            if lesson.kind == "max_output":
                wire.observe_max_output_tokens(model_id, lesson.limit)
            elif lesson.kind == "context_window":
                wire.observe_context_window(model_id, lesson.limit)
            elif lesson.kind == "parameter":
                wire.observe_rejected_parameter(model_id, lesson.rejected[0])
                rejected_parameters.add(lesson.rejected[0])
            elif lesson.kind == "exclusive":
                wire.observe_exclusive_parameters(model_id, lesson.rejected)
                exclusive_groups.append(lesson.rejected)
            elif lesson.kind == "effort_updates":
                wire.observe_effort_updates_rejected(model_id)
            else:
                wire.observe_rejected_effort(model_id, lesson.rejected[0])
            try:
                revised = await revise_request(
                    plan,
                    _LessonRevision(
                        request.payload,
                        profile.protocol,
                        tuple(rejected_parameters),
                        tuple(exclusive_groups),
                    ),
                )
            except ProviderError as rebuild_error:
                # A learned smaller window may refuse this unchanged input
                # locally. Preserve the remote rejection that proved it too
                # large, so Compaction can reduce the input itself.
                if lesson.kind == "context_window" and isinstance(
                    error, ProviderContextOverflowError
                ):
                    raise error from rebuild_error
                raise
            if revised is None:
                raise
            _LOGGER.warning(
                "%s rejected %s for %s; retrying with the learned wire shape",
                provider_label,
                lesson.description,
                model_id,
            )
            request = revised
    return await execute_attempt(request)


async def stream_learning_from_rejections(
    open_stream: Callable[[PreparedRequest], AsyncGenerator[dict[str, Any]]],
    plan: RequestPlan,
    *,
    wire: WireBinding,
    model_id: str,
    provider_label: str,
) -> AsyncGenerator[dict[str, Any]]:
    """Stream one request, learning from a rejection before its first delta.

    ``open_stream`` opens one full stream exchange for the request rendered
    from ``plan``. An error raised before the stream yields its first delta (an
    HTTP rejection or an in-band error event) is learned and retried exactly
    like :func:`execute_learning_from_rejections`; once a delta was yielded,
    parameter errors propagate unchanged and the request is never sent again, so
    its payload and body are released. A delta carrying reasoning records that
    the Model returned reasoning; when the request turned reasoning off,
    reasoning in a delta or inline in the answer (``split_inline_reasoning``)
    records that the Model ignores reasoning off.

    An image-count error after only heartbeats or Usage still reaches Chat as
    ``ProviderImageLimitError``; after Model output it propagates unchanged.
    """

    established: list[PreparedRequest] = []
    failure_usage: list[dict[str, Any]] = []
    attempts = 0
    last_attempt_usage: dict[str, Any] = {}

    async def establish(
        request: PreparedRequest,
    ) -> tuple[AsyncGenerator[dict[str, Any]], dict[str, Any] | None]:
        nonlocal attempts, last_attempt_usage
        attempts += 1
        last_attempt_usage = {}
        stream = open_stream(request)
        try:
            first = await anext(stream)
            while isinstance(first, FailureUsageDelta):
                failure_usage.append(dict(first))
                last_attempt_usage.update(
                    {key: first[key] for key in _USAGE_TOTAL_FIELDS if key in first}
                )
                first = await anext(stream)
        except StopAsyncIteration:
            first = None
        except BaseException:
            await stream.aclose()
            raise
        established.append(request)
        return stream, first

    try:
        stream, first = await execute_learning_from_rejections(
            establish,
            plan,
            wire=wire,
            model_id=model_id,
            provider_label=provider_label,
        )
    except ProviderError:
        combined = _failed_attempt_usage(failure_usage)
        if combined is not None:
            if attempts > 1:
                combined["last_request_usage"] = last_attempt_usage
            yield combined
        raise
    combined = _failed_attempt_usage(failure_usage)
    request = established.pop()
    profile = wire.profile(model_id)
    image_count = _request_image_count(request.payload, profile.protocol)
    off_watch = _OffWatch() if _requests_off(request.payload, profile) else None
    request.release()
    del request
    returned_reasoning = False
    emitted_output = False
    async with aclosing(stream):
        if combined is not None:
            if attempts > 1:
                combined["last_request_usage"] = {}
            yield combined
        if first is None:
            return
        delta = first
        while True:
            if delta.get("type") not in {"usage", "heartbeat"}:
                emitted_output = True
            if not returned_reasoning and _delta_returns_reasoning(delta):
                returned_reasoning = True
                wire.observe_reasoning_returned(model_id)
            if off_watch is not None and off_watch.reasons(delta, returned_reasoning):
                off_watch = None
                wire.observe_off_ignored(model_id)
            yield _include_failed_attempt_usage(delta, combined, repaired=attempts > 1)
            try:
                delta = await anext(stream)
            except StopAsyncIteration:
                return
            except ProviderError as error:
                if not emitted_output:
                    learned = learn_image_limit(error, image_count, wire=wire, model_id=model_id)
                    if learned is not None:
                        raise learned from error
                raise


@dataclass(frozen=True, eq=False)
class _LessonRevision:
    """Apply a request's lessons to its rebuilt payload; decline an unchanged rebuild.

    Runs on the request-body worker with the rebuild, so comparing the rebuild
    with the payload that was sent takes no Event-Loop time.
    """

    sent: Mapping[str, Any]
    protocol: str
    rejected_parameters: tuple[str, ...]
    exclusive_groups: tuple[tuple[str, ...], ...]

    def __call__(self, rebuilt: dict[str, Any]) -> bool:
        for parameter in self.rejected_parameters:
            _remove_sampling_parameter(rebuilt, self.protocol, parameter)
        for group in self.exclusive_groups:
            carried = [
                parameter
                for parameter in group
                if _carries_sampling_parameter(rebuilt, self.protocol, parameter)
            ]
            for parameter in carried[1:]:
                _remove_sampling_parameter(rebuilt, self.protocol, parameter)
        return rebuilt != self.sent


def _learn(
    detail: str, payload: Mapping[str, Any], profile: WireProfile, *, limits_only: bool
) -> _Lesson | None:
    lowered = detail.lower()
    limit = _learn_limit(lowered, payload)
    if limit is not None or limits_only:
        return limit
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
    if _EFFORT_UPDATE_REJECTION.search(lowered) and _carries_effort_update(payload):
        return _Lesson("effort_updates", (), "effort updates")
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


# A complete positive integer: do not learn 2 from a malformed 2.5 or 2,000.
_IMAGE_COUNT_LIMIT = r"([1-9]\d{0,5})(?!\d|[.,]\d)"
_STATED_IMAGE_MAXIMUM = tuple(
    re.compile(wording)
    for wording in (
        rf"a request may include at most {_IMAGE_COUNT_LIMIT} images\b",
        rf"too many images in request:\s*\d+\s*>\s*{_IMAGE_COUNT_LIMIT}\b",
        r"request contains \d+ images, exceeding the maximum of "
        rf"{_IMAGE_COUNT_LIMIT} allowed per request\b",
        rf"too many images:\s*max {_IMAGE_COUNT_LIMIT} images per request\b",
        # Azure and compatible gateways (including forwarded OpenRouter errors).
        rf"exceeded maximum number of images\s*\({_IMAGE_COUNT_LIMIT}\)",
        rf"too many images in request:\s*\d+,\s*maximum allowed:\s*{_IMAGE_COUNT_LIMIT}\b",
        rf"too many images in request\.\s*max is {_IMAGE_COUNT_LIMIT}\b",
        # vLLM's older request wording and its current prompt wording.
        rf"at most {_IMAGE_COUNT_LIMIT} image\(s\) may be provided in one (?:request|prompt)\b",
        # Groq: require the end of the statement, not a per-minute/message quota.
        rf"too many images provided\.\s*this model supports up to {_IMAGE_COUNT_LIMIT} images"
        r"(?=$|[.!\"'\\}\]])",
        # Bedrock's Llama path; this also works through a compatible gateway.
        rf"too many input images:\s*\d+,\s*max allowed:\s*{_IMAGE_COUNT_LIMIT}\b",
        # Reported Gemini routes; the Model name is not evidence of a fixed cap.
        r"too many images:\s*maximum allowed for model [\w./:-]+ is "
        rf"{_IMAGE_COUNT_LIMIT},\s*got \d+\b",
        # Anthropic/Bedrock combine images with documents or rendered PDF pages.
        # Only a zero document count establishes a reusable image-only ceiling.
        rf"too many images and documents:\s*\d+\s*\+\s*0\s*>\s*{_IMAGE_COUNT_LIMIT}\b",
        rf"too much media:\s*0 document pages\s*\+\s*\d+ images\s*>\s*{_IMAGE_COUNT_LIMIT}\b",
    )
)
"""Reported rejection forms; sources and scope are in providers/request-policy.md."""


def learn_image_limit(
    error: ProviderError,
    image_count: int,
    *,
    wire: WireBinding,
    model_id: str,
) -> ProviderImageLimitError | None:
    """Learn an explicit image-count rejection before output; leave retirement to Chat."""

    if isinstance(error, ProviderAuthError) or error.status_code not in {None, 400, 422}:
        return None
    if error.retryable and error.status_code is None:
        return None
    limit = _stated(str(error).lower(), _STATED_IMAGE_MAXIMUM)
    if limit is None or image_count <= limit:
        return None
    wire.observe_request_max_images(model_id, limit)
    normalized = ProviderImageLimitError(limit, str(error))
    normalized.status_code = error.status_code
    return normalized


def _request_image_count(payload: Mapping[str, Any], protocol: str) -> int:
    """Count only wire image parts, never text, Tool arguments or image output settings."""

    field = (
        "contents" if protocol == "gemini" else "input" if protocol == "responses" else "messages"
    )
    messages = payload.get(field)
    if not isinstance(messages, list):
        return 0
    count = 0
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        blocks = message.get("parts" if protocol == "gemini" else "content")
        pending = list(blocks) if isinstance(blocks, list) else []
        while pending:
            block = pending.pop()
            if not isinstance(block, Mapping):
                continue
            if block.get("type") in {"image", "image_url", "input_image"}:
                count += 1
            elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                pending.extend(block["content"])
            elif protocol == "gemini":
                media = block.get("inlineData", block.get("fileData"))
                if isinstance(media, Mapping) and str(media.get("mimeType", "")).startswith(
                    "image/"
                ):
                    count += 1
    return count


def _learn_limit(lowered: str, payload: Mapping[str, Any]) -> _Lesson | None:
    """A limit the detail states in a known wording; never a number guessed from prose.

    A stated output maximum teaches only when the payload asked for more, so a
    rejection of something else that happens to quote a maximum is not learned.
    """

    maximum = _stated(lowered, _STATED_OUTPUT_MAXIMUM)
    if maximum is not None:
        sent = [
            value
            for path in _OUTPUT_LIMIT_FIELDS
            if isinstance(value := _value_at(payload, path), int) and not isinstance(value, bool)
        ]
        if sent and max(sent) > maximum:
            return _Lesson(
                "max_output",
                (),
                f"an output allowance above its stated maximum of {maximum} tokens",
                maximum,
            )
        return None
    window = _stated(lowered, _STATED_CONTEXT_WINDOW)
    if window is None and _LLAMA_CPP_CONTEXT_ERROR in lowered:
        window = _stated(lowered, (_LLAMA_CPP_CONTEXT_WINDOW,))
    if window is None:
        return None
    return _Lesson(
        "context_window",
        (),
        f"a request beyond its stated Context window of {window} tokens",
        window,
    )


def _stated(lowered: str, wordings: tuple[re.Pattern[str], ...]) -> int | None:
    for wording in wordings:
        match = wording.search(lowered)
        if match is not None and (value := int(match.group(1))) > 0:
            return value
    return None


def _carries_effort_update(payload: Mapping[str, Any]) -> bool:
    """Whether ``payload`` switches the effort mid-conversation."""

    messages = payload.get("messages")
    if isinstance(messages, list) and any(
        isinstance(message, Mapping) and "output_config" in message for message in messages
    ):
        return True
    items = payload.get("input")
    return isinstance(items, list) and any(
        isinstance(item, Mapping) and item.get("type") == "configuration_update" for item in items
    )


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
