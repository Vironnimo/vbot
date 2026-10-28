"""Bounded worker admission for blocking Tool operations."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from functools import update_wrapper
from typing import Any

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
    return handler(context, arguments)


class _OffloadedToolHandler:
    """Keep the blocking entry available to the registry's worker admission gate."""

    __wrapped__: ToolHandler

    def __init__(self, handler: ToolHandler) -> None:
        update_wrapper(self, handler)

    async def __call__(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        result = await run_tool_worker(self.__wrapped__, context, arguments)
        if inspect.isawaitable(result):
            return await result
        return result


def offload_tool_handler(handler: ToolHandler) -> ToolHandler:
    """Run one blocking Tool implementation in a worker without unsafe cancellation.

    Worker threads cannot be stopped once the handler starts. If the Tool task is
    cancelled, wait for the handler to finish before propagating cancellation so
    callers never treat an in-flight filesystem mutation as completed or abandoned.
    """

    return _OffloadedToolHandler(handler)
