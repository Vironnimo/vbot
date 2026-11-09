"""Shared binding-resolution plumbing for task execution services.

Every task execution service starts a request the
same way: resolve the configured binding for its task type, merge stored
options over the backend schema defaults, and parse the target id — mapping
every ``TaskModelError`` to the task's own configuration-error class.
:class:`TaskBindingResolver` owns that sequence once.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

from core.debug import DebugContext
from core.model_tasks.embeddings_providers import EmbeddingUsage
from core.model_tasks.model_tasks import (
    TaskModelBinding,
    TaskModelError,
    TaskModelTargetRef,
    parse_task_model_target_id,
)
from core.providers.errors import NetworkError, ProviderError, ProviderRateLimitError
from core.usage import UsageRecorder
from core.utils.errors import ConfigError, FailureClass, TaskError
from core.utils.logging import get_logger
from core.utils.retry import caller_owns_retries, compute_retry_delay
from core.utils.workers import BoundedWorkerPool

JsonObject = dict[str, Any]
_TASK_BINDING_WORKERS = BoundedWorkerPool(name="task-binding", max_workers=4)
_LOGGER = get_logger("model_tasks.recovery")
_sleep = asyncio.sleep
_clock = time.monotonic


@dataclass
class TaskAttempt:
    """One resolved execution. Seal it as soon as output or a job escapes."""

    binding: TaskModelBinding
    options: JsonObject
    target: TaskModelTargetRef
    sealed: bool = False

    def seal(self) -> None:
        self.sealed = True


class TaskCandidateMismatchError(Exception):
    """A candidate cannot honor this call, before any request was sent."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        super().__init__(str(error))


def _recoverable(error: BaseException) -> tuple[bool, BaseException]:
    """Inspect explicit causes; a wrapper must not erase an unsafe outcome."""
    from core.model_tasks._speech_context import LocalSpeechError

    causes: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in causes:
        causes.append(current)
        current = current.__cause__
    if any(
        getattr(cause, "failure_class", None) in {"refused", "outcome_unknown"}
        or getattr(cause, "code", None) in {"generation_refused", "provider_outcome_unknown"}
        for cause in causes
    ):
        return False, error
    for cause in reversed(causes):
        if isinstance(cause, ProviderError):
            return (
                bool(cause.retryable)
                or cause.failure_class in {"auth", "usage_limit"}
                or cause.status_code in {401, 402, 403, 404}
            ), cause
        if isinstance(cause, NetworkError | ConfigError | TaskModelError | LocalSpeechError):
            return True, cause
    return False, error


@dataclass(frozen=True)
class TaskUsageContext:
    """Optional caller identity; never enters a task's Model request."""

    agent_id: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    owner_name: str | None = None
    group_id: str | None = None


def task_debug_context(
    context: TaskUsageContext | None,
    target: TaskModelTargetRef,
    *,
    streaming: bool = False,
) -> DebugContext | None:
    """Return the debug-trace context for a task request made for a Run, else None."""

    if context is None or not context.run_id:
        return None
    return DebugContext(
        run_id=context.run_id,
        agent_id=context.agent_id or "",
        session_id=context.session_id or "",
        provider_id=target.provider_id,
        connection_id=target.connection_id,
        model_id=target.model_id,
        streaming=streaming,
        iteration_number=0,
    )


class TaskUsage:
    """Account for actual task requests without retaining their inputs or results.

    A provider client starts an entry inside each POST attempt. Subsequent GETs
    may enrich that entry (for example a completed Video job), but never count
    as another Model request. Local inference and Adapter-based tasks use the
    same lifecycle explicitly.
    """

    def __init__(
        self,
        recorder: UsageRecorder | None,
        kind: str,
        target: TaskModelTargetRef,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        project_id: str | None = None,
        context: TaskUsageContext | None = None,
    ) -> None:
        self._recorder = recorder
        self._kind = kind
        self._target = target
        self._context = {
            "agent_id": context.agent_id if context is not None else agent_id,
            "session_id": context.session_id if context is not None else session_id,
            "run_id": context.run_id if context is not None else run_id,
            "project_id": context.project_id if context is not None else project_id,
            "owner_name": context.owner_name if context is not None else None,
            "group_id": context.group_id if context is not None else None,
        }
        self._latest_call_id = ""

    async def start(self) -> str:
        if self._recorder is None:
            return ""
        target = self._target
        self._latest_call_id = await self._recorder.start(
            model=(
                target.target
                if target.kind == "local"
                else f"{target.provider_id}/{target.model_id}"
            ),
            kind=self._kind,
            connection_id=target.connection_id or None,
            **self._context,
        )
        return self._latest_call_id

    async def finish(
        self,
        call_id: str,
        *,
        usage: Mapping[str, Any] | None = None,
        result: Any = None,
        status: Literal["completed", "failed", "cancelled"] = "completed",
        failure: BaseException | FailureClass | None = None,
    ) -> None:
        if self._recorder is None or not call_id:
            return
        normalized = task_usage(usage)
        result_usage = (
            result.get("usage") if isinstance(result, Mapping) else getattr(result, "usage", None)
        )
        # Subscription image results distinguish the image Model from the
        # Responses carrier. These are separate Models, never additive fields.
        if isinstance(result_usage, Mapping) and isinstance(result_usage.get("image_gen"), Mapping):
            result_usage = result_usage["image_gen"]
        normalized.update(task_usage(result_usage))
        if self._kind == "text_embedding" and "input_tokens" in normalized:
            normalized["output_tokens"] = 0
        await self._recorder.finish(call_id, normalized or None, status=status, failure=failure)

    async def update(self, call_id: str, usage: Mapping[str, Any] | None) -> None:
        if self._recorder is not None and call_id:
            normalized = task_usage(usage)
            if normalized:
                await self._recorder.update(call_id, normalized)

    async def update_latest(self, usage: Mapping[str, Any] | None) -> None:
        await self.update(self._latest_call_id, usage)

    async def related(self, model_id: str, usage: Mapping[str, Any] | None) -> None:
        """Retain independently reported carrier work under its actual Model."""
        if self._recorder is None or not usage:
            return
        call_id = await self._recorder.start(
            model=f"{self._target.provider_id}/{model_id}",
            kind=self._kind,
            connection_id=self._target.connection_id or None,
            **self._context,
        )
        await self._recorder.finish(call_id, task_usage(usage) or None)

    @asynccontextmanager
    async def attempt(self, reported: Mapping[str, Any] | None = None) -> AsyncIterator[str]:
        """Record one attempt; *reported* holds Usage the caller saw so far.

        The attempt finishes with whatever *reported* holds when it ends, so a
        failed or cancelled request keeps the consumption reported before it.
        """
        call_id = await self.start()
        status: Literal["completed", "failed", "cancelled"] = "completed"
        failure: BaseException | None = None
        try:
            yield call_id
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except BaseException as exc:
            status = "failed"
            failure = exc
            raise
        finally:
            await self.finish(call_id, usage=reported, status=status, failure=failure)


def task_usage(usage: Any) -> JsonObject:
    """Project already reported task counters onto canonical Usage fields.

    The task wires use either canonical input/output names or the established
    OpenAI-compatible prompt/completion names. Unknown units and missing
    counters stay absent; no text-token estimate is applied to media tasks.
    """
    if isinstance(usage, EmbeddingUsage):
        normalized: JsonObject = {}
        if usage.input_token_reports:
            normalized["input_tokens"] = usage.input_tokens
        if usage.token_reports:
            normalized["output_tokens"] = 0
        if usage.cost_reports:
            normalized["reported_cost_usd"] = usage.cost
        return normalized
    if not isinstance(usage, Mapping):
        return {}
    result = {
        name: usage[name]
        for name in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "estimated",
            "input_tokens_estimated",
            "output_tokens_estimated",
            "reported_cost_usd",
        )
        if name in usage
    }
    for canonical, wire in (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
    ):
        if canonical not in result and wire in usage:
            result[canonical] = usage[wire]
    if "reported_cost_usd" not in result and isinstance(usage.get("cost"), (int, float, str)):
        result["reported_cost_usd"] = usage["cost"]
    if isinstance(usage.get("cost"), Mapping):
        result["cost"] = dict(usage["cost"])
    return result


class TaskBindingResolver:
    """Resolve one task type to its (binding, options, target) triple."""

    def __init__(self, model_tasks: Any, *, configuration_error: type[TaskError]) -> None:
        self._model_tasks = model_tasks
        self._configuration_error = configuration_error
        self._failures: dict[str, tuple[float, float]] = {}

    async def execute[T](
        self,
        task_type: str,
        operation: Callable[[TaskAttempt], Awaitable[T]],
        *,
        bindings: tuple[TaskModelBinding, ...] | None = None,
    ) -> T:
        """Run one task against its configured chain, without nested HTTP retries.

        Every candidate keeps its own options and Usage. Cancellation, invalid
        input, refusals, ambiguous outcomes and sealed attempts never replay.
        A settings snapshot applies for the whole operation.
        """
        if bindings is None:
            primary = await _TASK_BINDING_WORKERS.run(self.binding_for, task_type)
            bindings = (primary, *getattr(primary, "fallbacks", ()))
        if len(bindings) == 1:
            resolved = await _TASK_BINDING_WORKERS.run(self.resolve_binding, bindings[0])
            try:
                return await operation(TaskAttempt(*resolved))
            except TaskCandidateMismatchError as exc:
                raise exc.error from exc
        candidates = self._candidates(bindings)
        last: Exception | None = None
        for index, (binding, key) in enumerate(candidates):
            incompatible = False
            follows = index + 1 < len(candidates)
            speech = task_type in {"speech_to_text", "text_to_speech", "live_voice"}
            attempts = (2 if follows else 3) if speech else (3 if follows else 4)
            if key in self._failures:
                attempts = min(attempts, 2)
            deadline = _clock() + (15 if speech else 60)
            for number in range(attempts):
                attempt = None
                try:
                    resolved = await _TASK_BINDING_WORKERS.run(self.resolve_binding, binding)
                    attempt = TaskAttempt(*resolved)
                    with caller_owns_retries():
                        result = await operation(attempt)
                    if self._failures.pop(key, None) is not None:
                        _LOGGER.info(
                            "Task Model route recovered (task=%s candidate=%s)", task_type, index
                        )
                    return result
                except TaskCandidateMismatchError as exc:
                    last = exc.error
                    incompatible = True
                    break
                except Exception as exc:
                    allowed, cause = _recoverable(exc)
                    if (attempt is not None and attempt.sealed) or not allowed:
                        raise
                    last = exc
                    retryable = bool(getattr(cause, "retryable", False))
                    if (
                        not retryable
                        or (follows and isinstance(cause, ProviderRateLimitError))
                        or number + 1 == attempts
                    ):
                        break
                    delay, _ = compute_retry_delay(
                        number, retry_after=getattr(cause, "retry_after", None)
                    )
                    if _clock() + delay >= deadline:
                        break
                    _LOGGER.debug(
                        "Task Model retry scheduled (task=%s candidate=%s attempt=%s)",
                        task_type,
                        index,
                        number + 2,
                    )
                    await _sleep(delay)
            if incompatible:
                continue
            pause = min(self._failures.get(key, (0, 450))[1] * 2, 3600)
            if key not in self._failures:
                _LOGGER.warning(
                    "Task Model route unavailable (task=%s candidate=%s)", task_type, index
                )
            self._failures[key] = (_clock() + pause, pause)
            # Bound process-local history across configuration edits.
            while len(self._failures) > 128:
                self._failures.pop(next(iter(self._failures)))
        assert last is not None
        raise last

    def resolve_binding(
        self, binding: TaskModelBinding
    ) -> tuple[TaskModelBinding, JsonObject, TaskModelTargetRef]:
        try:
            self._model_tasks.validate_execution_target(binding)
            options = self._model_tasks.options_with_defaults(binding)
            return binding, options, self.parse_target(binding.target)
        except TaskModelError as exc:
            raise self._configuration_error(str(exc)) from exc

    def _candidates(
        self, bindings: tuple[TaskModelBinding, ...]
    ) -> list[tuple[TaskModelBinding, str]]:
        candidates = []
        for binding in bindings:
            try:
                ref = self.parse_target(binding.target)
                identity: tuple[str, ...] = (
                    ref.kind,
                    ref.provider_id,
                    ref.model_id,
                    ref.connection_id,
                    ref.local_id,
                )
            except TaskError:
                identity = (binding.target,)
            key = json.dumps(
                [getattr(binding, "task_type", ""), identity, dict(binding.options)], sort_keys=True
            )
            candidates.append((binding, key))
        ready = [item for item in candidates if self._failures.get(item[1], (0, 0))[0] <= _clock()]
        return ready or [min(candidates, key=lambda item: self._failures[item[1]][0])]

    def resolve(
        self, task_type: str, *, validate: Callable[[TaskAttempt], object] | None = None
    ) -> tuple[TaskModelBinding, JsonObject, TaskModelTargetRef]:
        """Select a ready candidate, including the owning service's preflight."""
        primary = self.binding_for(task_type)
        last: TaskError | None = None
        for binding, _key in self._candidates((primary, *getattr(primary, "fallbacks", ()))):
            try:
                resolved = self.resolve_binding(binding)
                if validate is not None:
                    validate(TaskAttempt(*resolved))
                return resolved
            except TaskError as exc:
                last = exc
        assert last is not None
        raise last

    async def resolve_async(
        self, task_type: str
    ) -> tuple[TaskModelBinding, JsonObject, TaskModelTargetRef]:
        """Resolve current Settings and credential gates off the Event Loop."""
        return await _TASK_BINDING_WORKERS.run(self.resolve, task_type)

    def binding_for(self, task_type: str) -> TaskModelBinding:
        try:
            return self._model_tasks.binding_for(task_type)  # type: ignore[no-any-return]
        except TaskModelError as exc:
            raise self._configuration_error(str(exc)) from exc

    def parse_target(self, target: str) -> TaskModelTargetRef:
        try:
            return parse_task_model_target_id(target)
        except TaskModelError as exc:
            raise self._configuration_error(str(exc)) from exc
