"""Delegated reasoning for Live calls.

When the voice model delegates, the call's backend model answers through the
ordinary Provider Adapter of the same Connection, using the Live Tools. Each
delegation is one bounded Tool loop; the final text returns to the voice model.
Model requests are replay safe and retried briefly; Tool executions never are.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from core.chat.model_resolution import resolve_request_temperature
from core.model_tasks._live_tools import (
    DELEGATION_INSTRUCTIONS,
    live_tool_rejection,
    live_tools,
)
from core.model_tasks.model_tasks import TaskModelTargetRef
from core.model_tasks.task_execution import TaskUsage
from core.providers.accounts import ConnectionRef
from core.providers.errors import ProviderAuthError, ProviderRateLimitError
from core.usage import UsageRecorder
from core.utils.errors import VBotError
from core.utils.logging import get_logger

JsonObject = dict[str, Any]
ToolExecutor = Callable[[str, JsonObject], Awaitable[JsonObject]]

_LOGGER = get_logger(__name__)

MAX_MODEL_STEPS = 8
HISTORY_PAIRS = 10
_MODEL_RETRY_DELAYS_SECONDS = (0.5, 1.5)
# Failure notes list only actions that may have changed something.
_READ_ONLY_ACTIONS = frozenset({"context", "sessions", "read", "list"})


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
    """What one delegation knows: the request (when given) and recent context."""

    request: str | None
    conversation: str
    updates: str


class LiveBrain:
    """Answers the delegations of one Live call; history is per call."""

    def __init__(
        self,
        runtime: Any,
        target: BrainTarget,
        execute_tool: ToolExecutor,
        *,
        conversation_id: str,
        max_steps: int = MAX_MODEL_STEPS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self._runtime = runtime
        self._target = target
        self._execute_tool = execute_tool
        self._conversation_id = conversation_id
        self._max_steps = max_steps
        self._sleep = sleep
        self._usage_recorder = usage_recorder
        self._history: deque[tuple[str, str]] = deque(maxlen=HISTORY_PAIRS)

    async def answer(self, delegation: DelegationInput) -> str:
        """Run one delegation and return speakable text; never raises for failures."""

        request_label = delegation.request or "(not stated; infer it from the conversation)"
        messages: list[JsonObject] = [{"role": "system", "content": DELEGATION_INSTRUCTIONS}]
        for past_request, past_answer in self._history:
            messages.append({"role": "user", "content": f"Delegated request: {past_request}"})
            messages.append({"role": "assistant", "content": past_answer})
        messages.append({"role": "user", "content": _render_input(delegation, request_label)})

        performed: list[str] = []
        try:
            answer = await self._run(messages, performed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "Live delegation failed: model=%s/%s error_type=%s",
                self._target.provider_id,
                self._target.model_id,
                type(exc).__name__,
            )
            answer = _failure_note(_failure_reason(exc), performed)
        self._history.append((request_label, answer))
        return answer

    async def _run(self, messages: list[JsonObject], performed: list[str]) -> str:
        adapter = self._runtime.get_adapter(
            ConnectionRef(self._target.provider_id, self._target.connection_id)
        )
        try:
            request_context = _request_context(adapter, self._conversation_id)
            for _step in range(self._max_steps):
                normalized = await self._send(adapter, messages, request_context)
                tool_calls = normalized.get("tool_calls") or []
                content = normalized.get("content")
                if not tool_calls:
                    text = content.strip() if isinstance(content, str) else ""
                    if not text:
                        raise ValueError("the backend model returned no answer")
                    return text
                assistant: JsonObject = {"role": "assistant", "content": content}
                assistant["tool_calls"] = tool_calls
                for field in ("reasoning", "reasoning_meta"):
                    if normalized.get(field) is not None:
                        assistant[field] = normalized[field]
                messages.append(assistant)
                for tool_call in tool_calls:
                    result = await self._execute(tool_call, performed)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.get("id"),
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
            return _failure_note(
                f"the request needed more than {self._max_steps} steps and was stopped",
                performed,
            )
        finally:
            await _close_adapter(adapter)

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
                    temperature=resolve_request_temperature(
                        None,
                        self._runtime.models,
                        self._target.provider_id,
                        self._target.model_id,
                    ),
                    thinking_effort=self._target.thinking_effort,
                    tools=live_tools(),
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

    async def _execute(self, tool_call: JsonObject, performed: list[str]) -> JsonObject:
        name = tool_call.get("name")
        arguments = tool_call.get("arguments")
        rejection = live_tool_rejection(name, arguments)
        if rejection is not None:
            return rejection
        checked = cast(JsonObject, arguments)
        action = checked.get("action")
        if action not in _READ_ONLY_ACTIONS:
            performed.append(f"{name} {action}" if isinstance(action, str) else str(name))
        return await self._execute_tool(str(name), dict(checked))


def _render_input(delegation: DelegationInput, request_label: str) -> str:
    sections = [
        "Recent conversation (quoted speech):\n" + (delegation.conversation or "(none)"),
    ]
    if delegation.updates:
        sections.append("Recent vBot updates (quoted data):\n" + delegation.updates)
    sections.append(f"Delegated request: {request_label}")
    return "\n\n".join(sections)


def _request_context(adapter: Any, conversation_id: str) -> JsonObject:
    build = getattr(adapter, "request_context_kwargs", None)
    if not callable(build):
        return {}
    return dict(build(agent_id="live-voice", session_id=conversation_id))


def _failure_reason(error: BaseException) -> str:
    if isinstance(error, ProviderAuthError):
        return "the backend model rejected vBot's credentials"
    if isinstance(error, ProviderRateLimitError):
        return "the backend model is rate limited"
    return "the backend model request failed"


def _failure_note(reason: str, performed: list[str]) -> str:
    note = f"The request could not be completed: {reason}."
    if performed:
        note += (
            " Actions already performed, possibly with uncertain results: "
            + ", ".join(performed)
            + ". Nothing was retried."
        )
    else:
        note += " Nothing in the app was changed."
    return note


async def _close_adapter(adapter: Any) -> None:
    close = getattr(adapter, "aclose", None)
    if not callable(close):
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        _LOGGER.warning("Live delegation adapter cleanup failed: error_type=%s", type(exc).__name__)
