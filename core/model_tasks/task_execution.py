"""Shared binding-resolution plumbing for task execution services.

Every task execution service starts a request the
same way: resolve the configured binding for its task type, merge stored
options over the backend schema defaults, and parse the target id — mapping
every ``TaskModelError`` to the task's own configuration-error class.
:class:`TaskBindingResolver` owns that sequence once.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

from core.model_tasks.embeddings_providers import EmbeddingUsage
from core.model_tasks.model_tasks import (
    TaskModelBinding,
    TaskModelError,
    TaskModelTargetRef,
    parse_task_model_target_id,
)
from core.usage import UsageRecorder
from core.utils.errors import TaskError

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class TaskUsageContext:
    """Optional caller identity; never enters a task's Model request."""

    agent_id: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    owner_name: str | None = None
    group_id: str | None = None


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
        await self._recorder.finish(call_id, normalized or None, status=status)

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
    async def attempt(self) -> AsyncIterator[str]:
        call_id = await self.start()
        status: Literal["completed", "failed", "cancelled"] = "completed"
        try:
            yield call_id
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except BaseException:
            status = "failed"
            raise
        finally:
            await self.finish(call_id, status=status)


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

    def resolve(self, task_type: str) -> tuple[TaskModelBinding, JsonObject, TaskModelTargetRef]:
        """Return the configured binding, merged options, and parsed target ref."""
        binding = self.binding_for(task_type)
        try:
            self._model_tasks.validate_execution_target(binding)
            options = self._model_tasks.options_with_defaults(binding)
            return binding, options, self.parse_target(binding.target)
        except TaskModelError as exc:
            raise self._configuration_error(str(exc)) from exc

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
