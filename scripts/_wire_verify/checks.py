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
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.providers.adapter import ProviderAdapter
from core.providers.errors import ProviderError
from core.providers.reasoning import (
    THINKING_EFFORT_RANKS,
    ReasoningIntent,
    normalize_thinking_effort,
)

CHECKS: tuple[str, ...] = ("send", "stream", "efforts", "sampling", "tool_replay", "image")
"""Every check, in run order."""

_PROMPT = [{"role": "user", "content": "Reply with exactly one word: ready"}]
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
# A 2x2 red PNG; small enough for every image wire.
_RED_PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000020000000208020000"
        "00fdd49a730000001649444154789c63f8cfc0c0f09f81818181010014"
        "06017f8e3b6c2c0000000049454e44ae426082"
    )
).decode("ascii")


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
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    normalized: dict[str, Any] = field(default_factory=dict)
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
    reply = _Reply(
        content=str(normalized.get("content") or ""),
        reasoning=str(normalized.get("reasoning") or ""),
        reasoning_meta=bool(normalized.get("reasoning_meta")),
        reasoning_tokens=usage.get("reasoning_tokens") if isinstance(usage, Mapping) else None,
        tool_calls=list(normalized.get("tool_calls") or []),
        normalized=normalized,
    )
    reply.seconds = time.monotonic() - started
    return reply


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
    return {
        "content_chars": len(reply.content),
        "reasoning_chars": len(reply.reasoning),
        "reasoning_meta": reply.reasoning_meta,
        "reasoning_tokens": reply.reasoning_tokens,
        "seconds": round(reply.seconds, 1),
    }


def _error_summary(error: ProviderError) -> str:
    text = " ".join(str(error).split())
    return text[:240]


# -- checks ------------------------------------------------------------------------


async def _check_send(adapter: ProviderAdapter, model_id: str, vision: bool) -> CheckResult:
    del vision
    reply = await _send(adapter, model_id, _PROMPT)
    status = "ok" if reply.content.strip() else "warn"
    detail = "" if status == "ok" else "no visible content returned"
    return CheckResult("send", status, detail, _reply_facts(reply))


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
            reply = await _stream(adapter, model_id, _PROMPT, thinking_effort=effort)
        except ProviderError as error:
            results.append(CheckResult(name, "fail", _error_summary(error)))
            continue
        facts = {"sent": _decision_label(decision), **_reply_facts(reply)}
        results.append(CheckResult(name, *_judge_effort(effort, decision, reply), facts))
    return results


def _decision_label(decision: ReasoningIntent) -> str:
    if decision.effort_level is not None:
        return f"{decision.kind}:{decision.effort_level}"
    if decision.budget_tokens is not None:
        return f"{decision.kind}:{decision.budget_tokens}"
    return decision.kind


def _judge_effort(effort: str, decision: ReasoningIntent, reply: _Reply) -> tuple[str, str]:
    returned = bool(reply.reasoning) or reply.reasoning_meta
    tokens = reply.reasoning_tokens
    if normalize_thinking_effort(effort) == "none":
        if decision.kind == "off" and (returned or (tokens or 0) > 0):
            return "warn", "reasoning was disabled but the Model still reasoned"
        return "ok", ""
    if decision.requests_reasoning and not returned and tokens == 0:
        return "warn", "requested reasoning came back with zero reasoning tokens"
    return "ok", ""


async def _check_sampling(adapter: ProviderAdapter, model_id: str, vision: bool) -> CheckResult:
    del vision
    reply = await _send(adapter, model_id, _PROMPT, temperature=0.3, top_p=0.9)
    rejected = adapter.wire_profile(model_id).request.parameters
    dropped = sorted(name for name, rule in rejected.items() if rule.mode == "drop")
    facts = {"dropped": dropped, **_reply_facts(reply)}
    return CheckResult("sampling", "ok", "", facts)


async def _check_tool_replay(adapter: ProviderAdapter, model_id: str, vision: bool) -> CheckResult:
    """A Tool Call turn with reasoning, replayed into its continuation."""

    del vision
    wire = adapter.wire_profile(model_id).reasoning
    effort = _strongest_effort(wire.ladder) if wire.supported is not False else None
    question = {"role": "user", "content": "What is the weather in Paris? Use the tool."}
    first = await _send(
        adapter,
        model_id,
        [question],
        tools=[_WEATHER_TOOL],
        **({"thinking_effort": effort} if effort else {}),
    )
    if not first.tool_calls:
        return CheckResult("tool_replay", "warn", "the Model answered without a Tool Call")
    call = first.tool_calls[0]
    history: list[dict[str, Any]] = [
        question,
        first.normalized,
        {"role": "tool", "tool_call_id": call["id"], "content": '{"temp_c": 21, "sky": "clear"}'},
    ]
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
    status = "ok" if second.content.strip() or second.tool_calls else "warn"
    detail = "" if status == "ok" else "the continuation returned nothing"
    return CheckResult("tool_replay", status, detail, facts)


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
    detail = "" if status == "ok" else "the answer did not name the image color"
    return CheckResult("image", status, detail, _reply_facts(reply))


_RUNNERS = {
    "send": _check_send,
    "stream": _check_stream,
    "sampling": _check_sampling,
    "tool_replay": _check_tool_replay,
    "image": _check_image,
}
