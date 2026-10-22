"""Bounded worker admission for blocking Tool operations."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from core.debug import timing
from core.tools._tool_context import (
    ToolContext,
    ToolHandler,
)
from core.tools.contracts import JsonObject
from core.utils.workers import BoundedWorkerPool

DEFAULT_TOOL_WORKER_LIMIT = 8

_TOOL_WORKERS = BoundedWorkerPool(
    name="tool",
    max_workers=DEFAULT_TOOL_WORKER_LIMIT,
)


async def run_tool_worker[ToolWorkerResult](
    function: Callable[..., ToolWorkerResult],
    *arguments: Any,
    **keyword_arguments: Any,
) -> ToolWorkerResult:
    """Run blocking Tool work through the dedicated backpressured worker pool."""
    return await _TOOL_WORKERS.run(function, *arguments, **keyword_arguments)


def _invoke_sync_tool_handler(
    handler: ToolHandler,
    context: ToolContext,
    arguments: JsonObject,
) -> Any:
    timing.mark(
        "handler_started",
        run=context.run_id,
        iteration=context.iteration_number,
        call=context.tool_call_id,
        index=context.tool_call_index,
        tool=context.tool_name,
    )
    return handler(context, arguments)


def offload_tool_handler(handler: ToolHandler) -> ToolHandler:
    """Run one blocking Tool implementation in a worker without unsafe cancellation.

    Worker threads cannot be stopped once the handler starts. If the Tool task is
    cancelled, wait for the handler to finish before propagating cancellation so
    callers never treat an in-flight filesystem mutation as completed or abandoned.
    """

    @wraps(handler)
    async def offloaded(context: ToolContext, arguments: JsonObject) -> JsonObject:
        result = await run_tool_worker(_invoke_worker_handler, handler, context, arguments)
        if inspect.isawaitable(result):
            return await result
        return result

    return offloaded


def _invoke_worker_handler(
    handler: ToolHandler, context: ToolContext, arguments: JsonObject
) -> JsonObject | Awaitable[JsonObject]:
    timing.mark(
        "worker_handler_started",
        run=context.run_id,
        iteration=context.iteration_number,
        call=context.tool_call_id,
        index=context.tool_call_index,
        tool=context.tool_name,
    )
    return handler(context, arguments)
