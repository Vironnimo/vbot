"""Tool batch scheduling, concurrency barriers, and per-call cancellation hooks."""

from __future__ import annotations

import asyncio
import contextvars
import weakref
from collections.abc import Sequence
from contextlib import nullcontext
from functools import partial
from typing import TYPE_CHECKING, ClassVar

from core.tools._tool_context import (
    ToolCall,
    ToolCancelCallback,
    ToolCancelCheckHook,
    ToolCancelRegistrationHook,
    ToolContext,
    ToolExecutionConfig,
    ToolResultPersistedCallback,
    ToolResultPersistedHook,
)
from core.tools._tool_results import (
    tool_failure_for_exception,
)
from core.tools.contracts import JsonObject

if TYPE_CHECKING:
    from core.tools.tools import ToolRegistry

DEFAULT_TOOL_CONCURRENCY_LIMIT = 500


class ToolExecutor:
    """Schedule concurrent Tool groups around explicit serial barriers."""

    _global_semaphores: ClassVar[
        weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Semaphore]]
    ] = weakref.WeakKeyDictionary()

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        per_run_limit: int = DEFAULT_TOOL_CONCURRENCY_LIMIT,
        global_limit: int = DEFAULT_TOOL_CONCURRENCY_LIMIT,
    ) -> None:
        if per_run_limit < 1:
            raise ValueError("Per-run tool concurrency limit must be at least 1")
        if global_limit < 1:
            raise ValueError("Global tool concurrency limit must be at least 1")

        self._registry = registry
        self._per_run_limit = per_run_limit
        self._global_limit = global_limit

    async def execute_many(
        self,
        tool_calls: Sequence[ToolCall],
        config: ToolExecutionConfig,
    ) -> list[JsonObject]:
        """Execute parallel-by-default calls and return results in request order."""
        batch = self.start_batch(config)
        for tool_call in tool_calls:
            batch.add(tool_call)
        return await batch.results()

    def start_batch(
        self,
        config: ToolExecutionConfig,
        *,
        context: contextvars.Context | None = None,
    ) -> ToolBatch:
        """Open a batch that starts each call as soon as it is added.

        ``context`` is the ``contextvars`` state the calls run in (a copy per
        call); by default each call copies the context of the caller of ``add``.
        """
        return ToolBatch(self, config, context=context)

    async def _execute_one(
        self,
        tool_call: ToolCall,
        index: int,
        config: ToolExecutionConfig,
        per_run_semaphore: asyncio.Semaphore,
    ) -> JsonObject:
        needs_slot = self._registry.requires_execution_slot(tool_call.name)
        global_slot = self._get_global_semaphore() if needs_slot else nullcontext()
        async with per_run_semaphore, global_slot:
            # Per-call cancel hooks close over tool_call.id so concurrent sibling
            # tool calls in one execution group each register/inspect their own id.
            cancel_registration_hook, cancel_check_hook = _build_per_call_cancel_hooks(
                config, tool_call.id
            )
            result_persisted_hook: ToolResultPersistedHook | None = None
            result_persisted_registrar = config.tool_call_result_persisted_registrar
            if result_persisted_registrar is not None:

                def register_result_persisted(callback: ToolResultPersistedCallback) -> None:
                    result_persisted_registrar(tool_call.id, callback)

                result_persisted_hook = register_result_persisted
            context = ToolContext(
                agent_id=config.agent_id,
                session_id=config.session_id,
                run_id=config.run_id,
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                tool_call_index=index,
                workspace=config.workspace,
                vbot_root=config.vbot_root,
                data_root=config.data_root,
                iteration_number=config.iteration_number,
                agent_model=config.agent_model,
                agent_thinking_effort=config.agent_thinking_effort,
                execution_owner=config.execution_owner,
                cwd=config.cwd,
                project_id=config.project_id,
                skill_project_id=config.skill_project_id,
                working_project_id=config.working_project_id,
                skill_agent_id=config.skill_agent_id,
                run_kind=config.run_kind,
                subagent_session=config.subagent_session,
                permissions=config.permissions,
                reply_surface=config.reply_surface,
                emit_hook=config.emit_hook,
                cancellation_hook=config.cancellation_hook,
                cancel_registration_hook=cancel_registration_hook,
                cancel_check_hook=cancel_check_hook,
                note_hook=config.note_hook,
                skill_activation_hook=(
                    partial(config.skill_activation_hook, tool_call.id)
                    if config.skill_activation_hook is not None
                    else None
                ),
                result_persisted_hook=result_persisted_hook,
                delivery_receipt_hook=config.tool_delivery_receipt_registrar,
                request_turn_end_hook=config.tool_turn_end_registrar,
                result_payload_hook=config.tool_result_payload_registrar,
                allowed_skills=config.allowed_skills,
                skill_env_keys=config.skill_env_keys,
                tool_settings=config.tool_settings,
                delegation_bindings=config.delegation_bindings,
                session_tool_grants=config.session_tool_grants,
                input_contract=config.input_contracts.get(tool_call.name),
                # On-demand Tools have contracts before the Model was shown them.
                offered_tools=(
                    frozenset(config.input_contracts).difference(config.loadable_tools)
                    if config.input_contracts
                    else None
                ),
                loadable_tools=config.loadable_tools,
                unloadable_tools=frozenset(config.unloadable_tools),
                tool_load_hook=config.tool_load_registrar,
                worked_paths_hook=config.worked_paths_registrar,
                change_tracker=config.change_tracker,
            )
            return await self._dispatch_with_envelope(context, tool_call, config.allowed_tools)

    def _get_global_semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        loop_semaphores = self._global_semaphores.setdefault(loop, {})
        semaphore = loop_semaphores.get(self._global_limit)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._global_limit)
            loop_semaphores[self._global_limit] = semaphore
        return semaphore

    async def _dispatch_with_envelope(
        self,
        context: ToolContext,
        tool_call: ToolCall,
        allowed_tools: Sequence[str] | None,
    ) -> JsonObject:
        try:
            return await self._registry.dispatch(context, tool_call.arguments, allowed_tools)
        except Exception as error:
            return tool_failure_for_exception(context.tool_name, error)


class ToolBatch:
    """One turn's Tool Calls, each started as soon as it is added.

    Calls run concurrently by default. A call whose Tool is not parallel-safe is
    an ordering barrier: it starts after every earlier call finished, and later
    calls start after it finished. Results keep the order the calls were added,
    so a caller can add calls while the Model still writes the rest of its turn.
    """

    def __init__(
        self,
        executor: ToolExecutor,
        config: ToolExecutionConfig,
        *,
        context: contextvars.Context | None,
    ) -> None:
        self._executor = executor
        self._config = config
        self._context = context
        self._per_run_semaphore = asyncio.Semaphore(executor._per_run_limit)
        self._tasks: list[asyncio.Task[JsonObject]] = []
        # The latest barrier call and the parallel calls added after it.
        self._barrier: asyncio.Task[JsonObject] | None = None
        self._group: list[asyncio.Task[JsonObject]] = []

    def __len__(self) -> int:
        return len(self._tasks)

    def add(self, tool_call: ToolCall) -> None:
        """Start one call at the next position, after the calls it must follow."""
        index = len(self._tasks)
        parallel = self._executor._registry.is_parallel_safe(tool_call.name)
        waits = [self._barrier] if self._barrier is not None else []
        if not parallel:
            waits.extend(self._group)
        task = asyncio.create_task(
            self._run(tool_call, index, waits),
            name=f"tool:{tool_call.name}:{tool_call.id}",
            context=self._context.copy() if self._context is not None else None,
        )
        self._tasks.append(task)
        if parallel:
            self._group.append(task)
        else:
            self._barrier = task
            self._group = []

    async def results(self) -> list[JsonObject]:
        """Wait for every added call and return the results in call order."""
        try:
            return list(await asyncio.gather(*self._tasks))
        except BaseException:
            # A child failure does not make gather cancel its siblings. Keep
            # ownership until their cancellation cleanup has settled, then
            # preserve the original abort for the Run boundary.
            await self.aclose()
            raise

    async def aclose(self) -> None:
        """Cancel every call that has not finished and wait until all settled."""
        for task in self._tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def settle(self) -> list[JsonObject | None]:
        """Cancel the unfinished calls; return each finished call's result in call order.

        A call that was cancelled or raised has no result and yields ``None``.
        """
        await self.aclose()
        return [
            task.result() if not task.cancelled() and task.exception() is None else None
            for task in self._tasks
        ]

    async def _run(
        self,
        tool_call: ToolCall,
        index: int,
        waits: list[asyncio.Task[JsonObject]],
    ) -> JsonObject:
        if waits:
            await asyncio.wait(waits)
            if any(task.cancelled() or task.exception() is not None for task in waits):
                # An earlier call was aborted; the batch is being torn down.
                raise asyncio.CancelledError
        return await self._executor._execute_one(
            tool_call, index, self._config, self._per_run_semaphore
        )


def _build_per_call_cancel_hooks(
    config: ToolExecutionConfig, tool_call_id: str
) -> tuple[ToolCancelRegistrationHook | None, ToolCancelCheckHook | None]:
    """Return per-call cancel hooks that close over *tool_call_id*.

    When the config carries a registrar/check that takes a tool call id, this
    binds the per-call id so concurrent sibling tool calls each see their own
    registry entry. Falls back to the group-wide hooks when the per-call fields
    are absent (e.g., executor tests that wire hooks directly).
    """
    registration_hook: ToolCancelRegistrationHook | None
    if config.tool_call_cancel_registrar is not None:
        registrar = config.tool_call_cancel_registrar

        def registration_hook(callback: ToolCancelCallback) -> None:
            registrar(tool_call_id, callback)

    else:
        registration_hook = config.cancel_registration_hook

    check_hook: ToolCancelCheckHook | None
    if config.tool_call_cancel_check is not None:
        check = config.tool_call_cancel_check

        def check_hook() -> bool:
            return check(tool_call_id)

    else:
        check_hook = config.cancel_check_hook

    return registration_hook, check_hook
