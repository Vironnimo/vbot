"""Delegated reasoning for Live calls.

When the voice model delegates, the call's backend model answers through the
ordinary Provider Adapter of the same Connection, using the host's Tools. Each
delegation is one bounded Tool loop; the final text returns to the voice model.
The call keeps one Adapter, so its connections stay warm across delegations.
Model requests are replay safe and retried briefly; Tool calls never are. Every
delegation is handed to the call's recorder with its step timings.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.model_tasks.model_tasks import TaskModelTargetRef
from core.model_tasks.task_execution import TaskUsage
from core.providers.accounts import ConnectionRef
from core.providers.adapter import TOOL_CALL_REJECTION_FIELD
from core.providers.errors import ProviderAuthError, ProviderRateLimitError
from core.usage import UsageRecorder
from core.utils.errors import VBotError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.model_tasks.live import LiveToolRun

JsonObject = dict[str, Any]
ToolRunner = Callable[..., Awaitable["LiveToolRun"]]
Recorder = Callable[[JsonObject], None]

_LOGGER = get_logger(__name__)

MAX_MODEL_STEPS = 8
HISTORY_PAIRS = 10
# Every answer ends with this label and what the Tools reported changing.
EFFECTS_LABEL = "vBot changes for this request (from the Tool results):"
_MAX_EFFECTS = 8
_EFFECT_CHARS = 240
_MODEL_RETRY_DELAYS_SECONDS = (0.5, 1.5)


@dataclass(frozen=True)
class BrainTarget:
    """The backend model: exact Provider Connection, model id, and reasoning effort.

    ``thinking_effort`` is the requested reasoning effort; ``None`` leaves it to
    the Model's Provider default. The Adapter fits it to the Model's ladder.
    """

    provider_id: str
    connection_id: str
    model_id: str
    thinking_effort: str | None


@dataclass(frozen=True)
class DelegationInput:
    """What one delegation knows: the request (when given) and recent context.

    ``refs`` lists the refs earlier Tool results of the call named, one labeled
    line each; the answers in the history do not carry them. ``state`` is what
    vBot shows right now (see ``LiveCallHost.current_state``).
    """

    request: str | None
    conversation: str
    updates: str
    refs: str = ""
    state: str = ""


@dataclass
class _Progress:
    """What one delegation did so far.

    ``effects`` holds what each Tool call that may have changed something
    reported, in order; every answer ends with them.
    """

    effects: list[str] = field(default_factory=list)
    model_ms: list[int] = field(default_factory=list)
    tool_ms: list[int] = field(default_factory=list)


class LiveBrain:
    """Answers the delegations of one Live call; history is per call.

    *run_tool* runs one Tool call as the Model made it (see
    ``LiveCallHost.run_tool``). Call :meth:`aclose` once the call ended.
    """

    def __init__(
        self,
        runtime: Any,
        target: BrainTarget,
        *,
        instructions: str,
        tools: Sequence[JsonObject],
        run_tool: ToolRunner,
        conversation_id: str,
        record: Recorder | None = None,
        max_steps: int = MAX_MODEL_STEPS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self._runtime = runtime
        self._target = target
        self._instructions = instructions
        self._tools = [dict(tool) for tool in tools]
        self._run_tool = run_tool
        self._record = record
        self._clock = clock
        self._conversation_id = conversation_id
        self._max_steps = max_steps
        self._sleep = sleep
        self._usage_recorder = usage_recorder
        self._history: deque[tuple[str, str]] = deque(maxlen=HISTORY_PAIRS)
        self._adapter: Any = None
        self._closed = False

    async def answer(self, delegation: DelegationInput) -> str:
        """Run one delegation and return speakable text; never raises for failures."""

        request_label = delegation.request or "(not stated; infer it from the conversation)"
        messages: list[JsonObject] = [{"role": "system", "content": self._instructions}]
        for past_request, past_answer in self._history:
            messages.append({"role": "user", "content": f"Delegated request: {past_request}"})
            messages.append({"role": "assistant", "content": past_answer})
        messages.append({"role": "user", "content": _render_input(delegation, request_label)})

        progress = _Progress()
        started = self._clock()
        failure: str | None = None
        try:
            answer, failure = await self._run(messages, progress)
        except asyncio.CancelledError:
            self._record_delegation(delegation, None, progress, "cancelled", started)
            raise
        except Exception as exc:
            _LOGGER.warning(
                "Live delegation failed (model=%s/%s error_type=%s)",
                self._target.provider_id,
                self._target.model_id,
                type(exc).__name__,
            )
            failure = _failure_reason(exc)
            answer = _failure_note(failure)
        effects = _effects_line(progress.effects)
        answer = f"{answer}\n\n{effects}" if answer else effects
        self._record_delegation(delegation, answer, progress, failure, started)
        self._history.append((request_label, answer))
        return answer

    async def aclose(self) -> None:
        """Release the Adapter; later delegations fail instead of opening another."""

        self._closed = True
        adapter, self._adapter = self._adapter, None
        if adapter is not None:
            await _close_adapter(adapter)

    def _get_adapter(self) -> Any:
        if self._closed:
            raise RuntimeError("the Live call ended")
        if self._adapter is None:
            self._adapter = self._runtime.get_adapter(
                ConnectionRef(self._target.provider_id, self._target.connection_id)
            )
        return self._adapter

    async def _run(self, messages: list[JsonObject], progress: _Progress) -> tuple[str, str | None]:
        """Return the final answer and, when the loop gave up, the reason."""
        adapter = self._get_adapter()
        request_context = _request_context(adapter, self._conversation_id)
        for _step in range(self._max_steps):
            started = self._clock()
            normalized = await self._send(adapter, messages, request_context)
            progress.model_ms.append(_milliseconds(self._clock() - started))
            tool_calls = normalized.get("tool_calls") or []
            content = normalized.get("content")
            if not tool_calls:
                text = content.strip() if isinstance(content, str) else ""
                # After a change (such as ending the call) the effects line says
                # everything; without one, silence is a failure.
                if not text and not progress.effects:
                    raise ValueError("the backend model returned no answer")
                return text, None
            assistant: JsonObject = {"role": "assistant", "content": content}
            assistant["tool_calls"] = tool_calls
            for key in ("reasoning", "reasoning_meta"):
                if normalized.get(key) is not None:
                    assistant[key] = normalized[key]
            messages.append(assistant)
            for tool_call in tool_calls:
                result = await self._execute(tool_call, progress)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        reason = f"the request needed more than {self._max_steps} steps and was stopped"
        return _failure_note(reason), reason

    async def _send(
        self, adapter: Any, messages: list[JsonObject], request_context: JsonObject
    ) -> JsonObject:
        delays = iter(_MODEL_RETRY_DELAYS_SECONDS)
        target = self._target
        usage = TaskUsage(
            self._usage_recorder,
            "live_voice_backend",
            TaskModelTargetRef(
                kind="provider",
                target=f"{target.provider_id}/{target.model_id}",
                provider_id=target.provider_id,
                model_id=target.model_id,
                connection_id=target.connection_id,
            ),
        )
        while True:
            call_id = await usage.start()
            try:
                response: JsonObject = await adapter.send(
                    messages,
                    model_id=self._target.model_id,
                    thinking_effort=self._target.thinking_effort,
                    tools=[dict(tool) for tool in self._tools],
                    **request_context,
                )
            except BaseException as exc:
                await usage.finish(
                    call_id,
                    status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                )
                delay = next(delays, None) if isinstance(exc, VBotError) else None
                if delay is not None and getattr(exc, "retryable", False):
                    await self._sleep(delay)
                    continue
                raise
            try:
                normalized: JsonObject = adapter.normalize_response(
                    response, model_id=target.model_id
                )
            except BaseException:
                await usage.finish(call_id, status="failed")
                raise
            await usage.finish(call_id, result=normalized)
            return normalized

    async def _execute(self, tool_call: JsonObject, progress: _Progress) -> JsonObject:
        """Run one Tool call once through the host and note what it may have changed."""

        started = self._clock()
        # The Adapter may replace unusable arguments with an empty placeholder;
        # its rejection is the result, whatever the host would make of them.
        run = await self._run_tool(
            tool_call.get("name"),
            tool_call.get("arguments"),
            rejection=tool_call.get(TOOL_CALL_REJECTION_FIELD),
        )
        progress.tool_ms.append(_milliseconds(self._clock() - started))
        if run.changed:
            progress.effects.append(_effect(run.result))
        return run.result

    def _record_delegation(
        self,
        delegation: DelegationInput,
        answer: str | None,
        progress: _Progress,
        failure: str | None,
        started: float,
    ) -> None:
        if self._record is None:
            return
        try:
            self._record(
                {
                    "type": "delegation",
                    "request": delegation.request,
                    "answer": answer,
                    "steps": len(progress.model_ms),
                    "tool_calls": len(progress.tool_ms),
                    "model_ms": progress.model_ms,
                    "tool_ms": progress.tool_ms,
                    "failure": failure,
                    "duration_ms": _milliseconds(self._clock() - started),
                }
            )
        except Exception as exc:
            _LOGGER.warning("Live call record failed (error_type=%s)", type(exc).__name__)


def _milliseconds(seconds: float) -> int:
    return max(0, round(seconds * 1000))


def _render_input(delegation: DelegationInput, request_label: str) -> str:
    sections = [
        "Recent conversation (quoted speech):\n" + (delegation.conversation or "(none)"),
    ]
    if delegation.state:
        sections.append(
            "vBot right now (quoted data, the overview taken just before this request):\n"
            + delegation.state
        )
    if delegation.updates:
        sections.append("Recent vBot updates (quoted data):\n" + delegation.updates)
    if delegation.refs:
        sections.append(
            "Refs earlier results named (quoted data; still valid as targets):\n" + delegation.refs
        )
    sections.append(f"Delegated request: {request_label}")
    return "\n\n".join(sections)


def _effect(result: JsonObject) -> str:
    """One Tool result as a short fact: its text, or its failure marked as such."""
    if result.get("ok") is True:
        data = result.get("data")
        text = str(data.get("content") or "") if isinstance(data, dict) else ""
    else:
        error = result.get("error")
        message = str(error.get("message") or "") if isinstance(error, dict) else ""
        text = f"Failed: {message}" if message else "Failed."
    text = " ".join(text.split())
    if len(text) > _EFFECT_CHARS:
        text = text[: _EFFECT_CHARS - 3].rstrip() + "..."
    return text or "Done."


def _effects_line(effects: list[str]) -> str:
    """The closing line of every answer: what vBot changed, taken from the Tool results."""
    if not effects:
        return f"{EFFECTS_LABEL} nothing."
    shown = effects[:_MAX_EFFECTS]
    if len(effects) > len(shown):
        shown.append(f"and {len(effects) - len(shown)} more")
    return f"{EFFECTS_LABEL} " + " | ".join(shown)


def _request_context(adapter: Any, conversation_id: str) -> JsonObject:
    return dict(adapter.request_context_kwargs(agent_id="live-voice", session_id=conversation_id))


def _failure_reason(error: BaseException) -> str:
    if isinstance(error, ProviderAuthError):
        return "the backend model rejected vBot's credentials"
    if isinstance(error, ProviderRateLimitError):
        return "the backend model is rate limited"
    return "the backend model request failed"


def _failure_note(reason: str) -> str:
    """The answer when the loop gave up; the effects line says what already happened."""
    return f"The request could not be completed: {reason}. Nothing was retried."


async def _close_adapter(adapter: Any) -> None:
    close = getattr(adapter, "aclose", None)
    if not callable(close):
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        _LOGGER.warning(
            "Live delegation adapter cleanup failed (error_type=%s)", type(exc).__name__
        )
