"""Live checks of one Model's wire profile through the production Adapter path.

Every check sends real requests through ``send``/``stream`` exactly as Chat
does, so the request shape under test is the one the resolved wire profile
renders. The learning executor stays active: a rejected parameter or effort is
learned, retried and reported, which is precisely the evidence a verified
profile entry needs. Checks print measurements only, never prompts, keys or
full responses.
"""

from __future__ import annotations

import base64
import struct
import time
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.providers.adapter import ProviderAdapter
from core.providers.errors import ProviderError
from core.providers.reasoning import (
    REASONING_REPLAY_POLICIES,
    THINKING_EFFORT_RANKS,
    ReasoningIntent,
    ReasoningReplayPolicy,
    normalize_thinking_effort,
)

CHECKS: tuple[str, ...] = (
    "send",
    "stream",
    "efforts",
    "sampling",
    "tool_replay",
    "replay",
    "image",
)
"""Every check, in run order."""

_PROMPT = [{"role": "user", "content": "Reply with exactly one word: ready"}]
# Effort rungs need a question worth reasoning about: a Model with adaptive
# reasoning answers a trivial prompt without any, whatever the requested effort.
_REASONING_PROMPT = [
    {
        "role": "user",
        "content": "How many prime numbers lie between 100 and 150? Reply with only the number.",
    }
]
_OUTPUT_TOKENS = 2048
_WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Return the current weather for a city.",
    "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "City name"}},
        "required": ["city"],
    },
}


def _solid_png(size: int, rgb: tuple[int, int, int]) -> bytes:
    """Return a valid ``size`` x ``size`` single-color RGB PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rows = (b"\x00" + bytes(rgb) * size) * size
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


# A small red PNG; small enough for every image wire.
_RED_PNG = base64.b64encode(_solid_png(64, (255, 0, 0))).decode("ascii")


@dataclass
class CheckResult:
    """The outcome of one check."""

    name: str
    status: str  # "ok", "warn", "fail" or "skipped"
    detail: str = ""
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Reply:
    content: str = ""
    reasoning: str = ""
    reasoning_meta: bool = False
    reasoning_tokens: int | None = None
    input_tokens: int | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    normalized: dict[str, Any] = field(default_factory=dict)
    unread_fields: list[str] = field(default_factory=list)
    seconds: float = 0.0


async def run_checks(
    adapter: ProviderAdapter,
    model_id: str,
    *,
    checks: Sequence[str] = CHECKS,
    vision: bool = False,
) -> list[CheckResult]:
    """Run ``checks`` against ``model_id`` and return one result per check."""

    results: list[CheckResult] = []
    for name in checks:
        if name == "efforts":
            results.extend(await _check_efforts(adapter, model_id))
            continue
        runner = _RUNNERS[name]
        try:
            results.append(await runner(adapter, model_id, vision))
        except ProviderError as error:
            results.append(CheckResult(name, "fail", _error_summary(error)))
    return results


# -- requests --------------------------------------------------------------------

# Chat merges the Adapter's per-conversation request context into every request
# (OpenCode Go refuses a request without its session header); the checks send
# one fixed synthetic conversation identity.
_AGENT_ID = "wire-verify"
_SESSION_ID = "wire-verify"


def _request_kwargs(adapter: ProviderAdapter, kwargs: Mapping[str, Any]) -> dict[str, Any]:
    context = adapter.request_context_kwargs(agent_id=_AGENT_ID, session_id=_SESSION_ID)
    return {**context, **kwargs}


async def _send(
    adapter: ProviderAdapter, model_id: str, messages: list[dict[str, Any]], **kwargs: Any
) -> _Reply:
    started = time.monotonic()
    raw = await adapter.send(
        messages,
        model_id=model_id,
        max_tokens=_OUTPUT_TOKENS,
        **_request_kwargs(adapter, kwargs),
    )
    normalized = adapter.normalize_response(raw, model_id=model_id)
    usage = normalized.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    reply = _Reply(
        content=str(normalized.get("content") or ""),
        reasoning=str(normalized.get("reasoning") or ""),
        reasoning_meta=bool(normalized.get("reasoning_meta")),
        reasoning_tokens=_count(usage.get("reasoning_tokens")),
        input_tokens=_count(usage.get("input_tokens")),
        tool_calls=list(normalized.get("tool_calls") or []),
        normalized=normalized,
    )
    if not reply.reasoning:
        read = adapter.wire_profile(model_id).response.reasoning_fields
        reply.unread_fields = _unread_message_fields(raw, read)
    reply.seconds = time.monotonic() - started
    return reply


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# Chat Completions message fields that never carry reasoning.
_ANSWER_FIELDS = frozenset(
    {"role", "content", "tool_calls", "function_call", "refusal", "annotations", "audio", "name"}
)


def _unread_message_fields(raw: Any, read: Sequence[str]) -> list[str]:
    """Return non-empty text fields of a Chat Completions message vBot did not read.

    Reasoning in such a field is invisible to vBot: neither shown nor replayed.
    Other wires answer in typed items or blocks and report nothing here.
    """

    choices = raw.get("choices") if isinstance(raw, Mapping) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return []
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        return []
    return sorted(
        str(name)
        for name, value in message.items()
        if name not in _ANSWER_FIELDS
        and name not in read
        and isinstance(value, str)
        and value.strip()
    )


async def _stream(
    adapter: ProviderAdapter, model_id: str, messages: list[dict[str, Any]], **kwargs: Any
) -> _Reply:
    started = time.monotonic()
    reply = _Reply()
    async for delta in adapter.stream(
        messages,
        model_id=model_id,
        max_tokens=_OUTPUT_TOKENS,
        **_request_kwargs(adapter, kwargs),
    ):
        kind = delta.get("type")
        if kind == "content_delta":
            reply.content += str(delta.get("text") or "")
        elif kind == "reasoning_delta":
            reply.reasoning += str(delta.get("text") or "")
        elif kind == "reasoning_meta":
            reply.reasoning_meta = True
        elif kind == "usage" and isinstance(delta.get("reasoning_tokens"), int):
            reply.reasoning_tokens = delta["reasoning_tokens"]
    reply.seconds = time.monotonic() - started
    return reply


def _reply_facts(reply: _Reply) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "content_chars": len(reply.content),
        "reasoning_chars": len(reply.reasoning),
        "reasoning_meta": reply.reasoning_meta,
        "reasoning_tokens": reply.reasoning_tokens,
        "seconds": round(reply.seconds, 1),
    }
    if reply.unread_fields:
        facts["unread_fields"] = reply.unread_fields
    return facts


def _carrier_warning(reply: _Reply) -> str:
    """Name text fields that may carry reasoning the profile does not read."""

    if not reply.unread_fields:
        return ""
    return (
        "the reply carried unread text in "
        + ", ".join(reply.unread_fields)
        + "; if that is reasoning, add it to response.reasoning_fields"
    )


def _error_summary(error: ProviderError) -> str:
    text = " ".join(str(error).split())
    return text[:240]


# -- checks ------------------------------------------------------------------------


async def _check_send(adapter: ProviderAdapter, model_id: str, vision: bool) -> CheckResult:
    del vision
    reply = await _send(adapter, model_id, _PROMPT)
    detail = "" if reply.content.strip() else "no visible content returned"
    detail = detail or _carrier_warning(reply)
    return CheckResult("send", "warn" if detail else "ok", detail, _reply_facts(reply))


async def _check_stream(adapter: ProviderAdapter, model_id: str, vision: bool) -> CheckResult:
    del vision
    reply = await _stream(adapter, model_id, _PROMPT)
    status = "ok" if reply.content.strip() else "warn"
    detail = "" if status == "ok" else "no visible content streamed"
    return CheckResult("stream", status, detail, _reply_facts(reply))


async def _check_efforts(adapter: ProviderAdapter, model_id: str) -> list[CheckResult]:
    """Stream once per wire rung plus ``none`` and judge each rendered decision."""

    wire = adapter.wire_profile(model_id).reasoning
    if wire.supported is False or wire.dialect == "none":
        return [CheckResult("efforts", "skipped", "the profile sends no reasoning control")]
    efforts = list(dict.fromkeys(level for level in (*wire.ladder, "none") if level))
    results: list[CheckResult] = []
    for effort in efforts:
        name = f"effort:{effort}"
        # Described per rung: a learned rejection changes the decision for later rungs.
        decision = adapter.describe_reasoning_render(model_id, effort)
        try:
            reply = await _stream(adapter, model_id, _REASONING_PROMPT, thinking_effort=effort)
        except ProviderError as error:
            results.append(CheckResult(name, "fail", _error_summary(error)))
            continue
        facts = {"sent": _decision_label(decision), **_reply_facts(reply)}
        results.append(CheckResult(name, *_judge_effort(effort, decision, reply), facts))
    return _accept_adaptive_skips(results)


def _accept_adaptive_skips(results: list[CheckResult]) -> list[CheckResult]:
    """Accept low rungs without reasoning when a higher rung of the run reasoned.

    A Model that reasons adaptively may answer at a low effort without reasoning;
    a higher rung that reasoned shows the control reaches the Model.
    """

    accepted: list[CheckResult] = []
    for index, result in enumerate(results):
        reasoned_above = any(
            later.status == "ok" and (later.facts.get("reasoning_tokens") or 0) > 0
            for later in results[index + 1 :]
            if later.name != "effort:none"
        )
        if result.detail == _ZERO_REASONING and reasoned_above:
            result = CheckResult(
                result.name,
                "ok",
                "no reasoning at this effort; higher rungs reasoned",
                result.facts,
            )
        accepted.append(result)
    return accepted


_ZERO_REASONING = "requested reasoning came back with zero reasoning tokens"


def _decision_label(decision: ReasoningIntent) -> str:
    if decision.effort_level is not None:
        return f"{decision.kind}:{decision.effort_level}"
    if decision.budget_tokens is not None:
        return f"{decision.kind}:{decision.budget_tokens}"
    return decision.kind


_EMPTY_REASONING_TOKENS = 2
"""Reasoning tokens an empty reasoning block may count (Kimi reports 1 with
reasoning off); more, or any returned Reasoning, means the Model reasoned."""


def _judge_effort(effort: str, decision: ReasoningIntent, reply: _Reply) -> tuple[str, str]:
    tokens = reply.reasoning_tokens
    # Opaque reasoning state alone proves nothing when the Provider counts zero
    # reasoning tokens (a Responses reasoning item survives with reasoning off).
    returned = bool(reply.reasoning) or (reply.reasoning_meta and tokens != 0)
    if normalize_thinking_effort(effort) == "none":
        if decision.kind == "off" and (returned or (tokens or 0) > _EMPTY_REASONING_TOKENS):
            return "warn", "reasoning was disabled but the Model still reasoned"
        return "ok", ""
    if decision.requests_reasoning and not returned and tokens == 0:
        return "warn", _ZERO_REASONING
    return "ok", ""


async def _check_sampling(adapter: ProviderAdapter, model_id: str, vision: bool) -> CheckResult:
    del vision
    sampling = {"temperature": 0.3, "top_p": 0.9}
    reply = await _send(adapter, model_id, _PROMPT, **sampling)
    # What the profile left out: parameters outside an allowlist, dropped
    # parameters and exclusive group members.
    rules = adapter.wire_profile(model_id).request
    allowed = rules.allowed_parameters
    shaped = {name: value for name, value in sampling.items() if allowed is None or name in allowed}
    rules.shape_parameters(shaped, reasoning_active=False)
    facts = {"dropped": sorted(set(sampling) - set(shaped)), **_reply_facts(reply)}
    return CheckResult("sampling", "ok", "", facts)


async def _check_tool_replay(adapter: ProviderAdapter, model_id: str, vision: bool) -> CheckResult:
    """A Tool Call turn with reasoning, replayed into its continuation."""

    del vision
    wire = adapter.wire_profile(model_id).reasoning
    effort = _strongest_effort(wire.ladder) if wire.supported is not False else None
    first = await _send(
        adapter,
        model_id,
        [_QUESTION],
        tools=[_WEATHER_TOOL],
        **({"thinking_effort": effort} if effort else {}),
    )
    if not first.tool_calls:
        return CheckResult("tool_replay", "warn", "the Model answered without a Tool Call")
    history = [_QUESTION, first.normalized, _tool_result(first)]
    second = await _send(
        adapter,
        model_id,
        history,
        tools=[_WEATHER_TOOL],
        **({"thinking_effort": effort} if effort else {}),
    )
    facts = {
        "effort": effort,
        "replayed_reasoning_chars": len(first.reasoning),
        "replayed_reasoning_meta": first.reasoning_meta,
        **_reply_facts(second),
    }
    answered = second.content.strip() or second.tool_calls
    detail = "" if answered else "the continuation returned nothing"
    detail = detail or _carrier_warning(first)
    return CheckResult("tool_replay", "warn" if detail else "ok", detail, facts)


# The city takes a reasoning step, so even a Model that reasons on demand fills
# its reasoning state before the Tool Call.
_QUESTION = {
    "role": "user",
    "content": (
        "I am in the capital of the country that won the 2018 FIFA World Cup. "
        "What is the weather here? Use the tool."
    ),
}
_FOLLOW_UP = {"role": "user", "content": "Should I take an umbrella? Answer in one sentence."}


def _tool_result(reply: _Reply) -> dict[str, Any]:
    call = reply.tool_calls[0]
    return {"role": "tool", "tool_call_id": call["id"], "content": '{"temp_c": 21, "sky": "clear"}'}


async def _check_replay(adapter: ProviderAdapter, model_id: str, vision: bool) -> CheckResult:
    """Measure whether replayed reasoning reaches the Model, inside a Run and across Runs.

    Each scope sends three otherwise identical requests and compares the
    Provider-reported input tokens: A without the reasoning, B with the
    reasoning on its carrier, and C with the same text as visible content (the
    accounting control; readable reasoning only). B above A means the Provider
    consumes the carrier; B not above A while C is above A means the carrier is
    stripped or ignored. Anything else stays unresolved.

    The in-Run and cross-Run scopes vary the Tool Call turn's reasoning; the
    answer-turn scope varies only the reasoning of the earlier Run's final
    answer, which tells ``tool_turns`` from ``full_history``.
    """

    del vision
    profile = adapter.wire_profile(model_id)
    effort = (
        _strongest_effort(profile.reasoning.ladder)
        if profile.reasoning.supported is not False
        else None
    )
    if effort is None:
        return CheckResult("replay", "skipped", "the profile requests no reasoning")
    options: dict[str, Any] = {"tools": [_WEATHER_TOOL], "thinking_effort": effort}
    first = await _send(adapter, model_id, [_QUESTION], **options)
    if not first.tool_calls:
        return CheckResult("replay", "warn", "the Model answered without a Tool Call")
    if not first.reasoning and not first.reasoning_meta:
        warning = _carrier_warning(first)
        if warning:
            return CheckResult("replay", "warn", warning, _reply_facts(first))
        return CheckResult("replay", "skipped", "the Tool Call turn returned no reasoning")
    if not first.reasoning and first.reasoning_tokens == 0:
        return CheckResult(
            "replay",
            "skipped",
            "the Tool Call turn spent no reasoning tokens, so its reasoning state is empty",
            _reply_facts(first),
        )

    in_run = [_QUESTION, first.normalized, _tool_result(first)]
    in_run_tokens, answer = await _measure(adapter, model_id, in_run, options)
    final = {"role": "assistant", "content": answer.content or "It is 21 C and clear in Paris."}
    cross_run = [*in_run, final, _FOLLOW_UP]
    cross_run_tokens, _ = await _measure(adapter, model_id, cross_run, options)
    answer_tokens: _Tokens = (None, None, None)
    if answer.content and (answer.reasoning or answer.reasoning_meta):
        answered = [*in_run, answer.normalized, _FOLLOW_UP]
        answer_tokens, _ = await _measure(adapter, model_id, answered, options, varied=3)

    in_run_result = _classify(*in_run_tokens)
    cross_run_result = _classify(*cross_run_tokens)
    answer_result = _classify(*answer_tokens)
    measured = _measured_scope(in_run_result, cross_run_result, answer_result)
    scope = profile.replay.scope
    facts = {
        "effort": effort,
        "first_reasoning_tokens": first.reasoning_tokens,
        "profile_scope": scope,
        "in_run": _scope_facts(in_run_tokens, in_run_result),
        "cross_run": _scope_facts(cross_run_tokens, cross_run_result),
        "answer_turn": _scope_facts(answer_tokens, answer_result),
        "measured_scope": measured,
    }
    # A tool_turns profile drops answer-turn reasoning the Provider would bill
    # on purpose, so only reasoning on Tool Call turns counts against it.
    needed = "tool_turns" if measured == "full_history" and scope == "tool_turns" else measured
    detail = ""
    if needed is not None and _scope_rank(needed) > _scope_rank(scope):
        detail = f"the Model consumes replayed reasoning ({measured}); the profile replays {scope}"
    elif measured == "none" and scope != "none":
        detail = f"the Provider ignores replayed reasoning; the profile replays {scope}"
    return CheckResult("replay", "warn" if detail else "ok", detail, facts)


_Tokens = tuple[int | None, int | None, int | None]


async def _measure(
    adapter: ProviderAdapter,
    model_id: str,
    history: list[dict[str, Any]],
    options: Mapping[str, Any],
    *,
    varied: int | None = None,
) -> tuple[_Tokens, _Reply]:
    """Return input tokens for A (stripped), B (carried), C (control) and B's reply.

    ``varied`` limits A and C to the reasoning of that one history message;
    every Assistant message by default.
    """

    carried = await _send(adapter, model_id, history, **options)
    stripped = await _send(adapter, model_id, _without_reasoning(history, varied), **options)
    control_history = _reasoning_as_content(history, varied)
    control = (
        await _send(adapter, model_id, control_history, **options)
        if control_history is not None
        else None
    )
    tokens = (
        stripped.input_tokens,
        carried.input_tokens,
        control.input_tokens if control is not None else None,
    )
    return tokens, carried


_REASONING_KEYS = ("reasoning", "reasoning_meta", "reasoning_scope")


def _varies(index: int, message: Mapping[str, Any], varied: int | None) -> bool:
    return message.get("role") == "assistant" and varied in (None, index)


def _without_reasoning(
    history: Sequence[Mapping[str, Any]], varied: int | None = None
) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in message.items() if key not in _REASONING_KEYS}
        if _varies(index, message, varied)
        else dict(message)
        for index, message in enumerate(history)
    ]


def _reasoning_as_content(
    history: Sequence[Mapping[str, Any]], varied: int | None = None
) -> list[dict[str, Any]] | None:
    """Move readable reasoning into visible Assistant content, or ``None`` if there is none."""

    stripped = _without_reasoning(history, varied)
    moved = False
    for index, (original, message) in enumerate(zip(history, stripped, strict=True)):
        reasoning = original.get("reasoning") if _varies(index, original, varied) else None
        if isinstance(reasoning, str) and reasoning:
            message["content"] = f"{reasoning}\n\n{message.get('content') or ''}".strip()
            moved = True
    return stripped if moved else None


def _classify(stripped: int | None, carried: int | None, control: int | None) -> str:
    if stripped is None or carried is None:
        return "unresolved"
    if carried > stripped:
        return "consumed"
    if control is not None and control > stripped:
        return "ignored"
    return "unresolved"


def _scope_facts(tokens: _Tokens, result: str) -> dict[str, Any]:
    stripped, carried, control = tokens
    return {"a": stripped, "b": carried, "c": control, "result": result}


def _measured_scope(in_run: str, cross_run: str, answer_turn: str) -> ReasoningReplayPolicy | None:
    if cross_run == "consumed":
        # Only a demonstrably ignored answer turn narrows the scope.
        return "tool_turns" if answer_turn == "ignored" else "full_history"
    if in_run == "consumed" and cross_run == "ignored":
        return "current_run"
    if in_run == "ignored" and cross_run == "ignored":
        return "none"
    return None


def _scope_rank(scope: ReasoningReplayPolicy) -> int:
    return REASONING_REPLAY_POLICIES.index(scope)


def _strongest_effort(ladder: Sequence[str]) -> str | None:
    active = [level for level in ladder if level in THINKING_EFFORT_RANKS and level != "none"]
    return max(active, key=THINKING_EFFORT_RANKS.__getitem__) if active else None


async def _check_image(adapter: ProviderAdapter, model_id: str, vision: bool) -> CheckResult:
    if not vision or "image/png" not in adapter.wire_media_support(model_id):
        return CheckResult("image", "skipped", "no image input on this Model or wire")
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "Which color is this image? Answer with one word."},
            {"type": "media", "media_type": "image/png", "base64": _RED_PNG},
        ],
    }
    reply = await _send(adapter, model_id, [message])
    status = "ok" if "red" in reply.content.lower() else "warn"
    detail = "" if status == "ok" else f"the answer did not name red: {reply.content[:60]!r}"
    return CheckResult("image", status, detail, _reply_facts(reply))


_RUNNERS = {
    "send": _check_send,
    "stream": _check_stream,
    "sampling": _check_sampling,
    "tool_replay": _check_tool_replay,
    "replay": _check_replay,
    "image": _check_image,
}
