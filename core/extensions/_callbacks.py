"""Bounded callback execution, lifecycle deadlines and slow-handler diagnostics."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from typing import Any

from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

_LOGGER = get_logger("extensions")
_EXTENSION_WORKER_LIMIT = 8
_SLOW_EXTENSION_HANDLER_SECONDS = 1.0
# Startup, shutdown and quiesce each get this long before their caller moves on.
_LIFECYCLE_HANDLER_TIMEOUT_SECONDS = 30.0
_EXTENSION_WORKERS = BoundedWorkerPool(
    name="extension",
    max_workers=_EXTENSION_WORKER_LIMIT,
)
_detached_tasks: set[asyncio.Task[Any]] = set()


class ExtensionHandlerTimeoutError(TimeoutError):
    """Raised when a caller stops waiting for an Extension callback at its deadline."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__(f"timed out after {timeout_seconds:g} seconds")


async def invoke_extension_handler(
    handler: Callable[..., Any],
    *arguments: Any,
    **keyword_arguments: Any,
) -> Any:
    """Invoke one sync or async Extension callback without loop-blocking sync work."""
    if inspect.iscoroutinefunction(handler):
        result = handler(*arguments, **keyword_arguments)
    else:
        result = await _EXTENSION_WORKERS.run(
            handler,
            *arguments,
            **keyword_arguments,
        )
    if inspect.isawaitable(result):
        return await result
    return result


async def invoke_lifecycle_handler(
    handler: Callable[[], Any],
    *,
    extension_name: str,
    description: str,
    cancel_on_timeout: bool,
) -> Any:
    """Invoke one lifecycle callback; stop waiting for it at the lifecycle deadline.

    At the deadline the callback is detached and ``ExtensionHandlerTimeoutError``
    raised, so Extension code - even code that ignores cancellation or blocks a
    worker thread - cannot hold its caller. With *cancel_on_timeout* the
    callback is also asked to cancel; without it, it keeps running unchanged.
    Cancelling the caller cancels the callback and propagates. A detached
    callback's later failure is still logged.
    """
    task = asyncio.ensure_future(invoke_extension_handler(handler))
    try:
        done, _ = await asyncio.wait({task}, timeout=_LIFECYCLE_HANDLER_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        task.cancel()
        detach_extension_task(task, extension_name=extension_name, description=description)
        raise
    if not done:
        if cancel_on_timeout:
            task.cancel()
        detach_extension_task(task, extension_name=extension_name, description=description)
        raise ExtensionHandlerTimeoutError(_LIFECYCLE_HANDLER_TIMEOUT_SECONDS)
    return task.result()


def detach_extension_task(
    task: asyncio.Task[Any], *, extension_name: str, description: str
) -> None:
    """Keep abandoned Extension work referenced and log its failure after detachment."""
    _detached_tasks.add(task)

    def finished(completed: asyncio.Task[Any]) -> None:
        _detached_tasks.discard(completed)
        if completed.cancelled():
            return
        try:
            completed.result()
        except Exception as exc:
            _LOGGER.error(
                "Extension %r detached %s raised: %s",
                extension_name,
                description,
                exc,
                exc_info=True,
            )

    task.add_done_callback(finished)


def _log_slow_extension_handler(
    *,
    extension_name: str,
    handler_kind: str,
    started_at: float,
) -> None:
    elapsed = time.perf_counter() - started_at
    if elapsed < _SLOW_EXTENSION_HANDLER_SECONDS:
        return
    _LOGGER.warning(
        "Extension %r %s handler completed slowly (duration=%.3fs)",
        extension_name,
        handler_kind,
        elapsed,
    )
