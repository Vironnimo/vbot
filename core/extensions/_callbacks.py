"""Bounded callback execution, lifecycle deadlines and slow-handler diagnostics."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from typing import Any

from core.extensions._declarations import HookContext, HookHandler, LifecycleHandler
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
    return await invoke_isolated_handler(
        handler,
        extension_name=extension_name,
        description=description,
        timeout_seconds=_LIFECYCLE_HANDLER_TIMEOUT_SECONDS,
        cancel_on_timeout=cancel_on_timeout,
    )


async def invoke_isolated_handler(
    handler: Callable[..., Any],
    *arguments: Any,
    extension_name: str,
    description: str,
    timeout_seconds: float | None = None,
    cancel_on_timeout: bool = True,
    **keyword_arguments: Any,
) -> Any:
    """Keep callback self-cancellation separate from cancellation of its caller."""
    task = asyncio.create_task(invoke_extension_handler(handler, *arguments, **keyword_arguments))
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
    except asyncio.CancelledError:
        task.cancel()
        detach_extension_task(task, extension_name=extension_name, description=description)
        raise
    if not done:
        if cancel_on_timeout:
            task.cancel()
        detach_extension_task(task, extension_name=extension_name, description=description)
        assert timeout_seconds is not None
        raise ExtensionHandlerTimeoutError(timeout_seconds)
    if task.cancelled():
        # The wait above, not the child result, carries caller cancellation.
        raise RuntimeError("Extension callback cancelled itself")
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


HANDLER_FAILED = object()


async def invoke_lifecycle_fail_open(
    phase: str, extension_name: str, handler: LifecycleHandler
) -> None:
    """Call one lifecycle handler with fail-open isolation (logs at ``error``).

    A handler that exceeds the lifecycle deadline is asked to cancel and
    detached, so it cannot hold startup, shutdown, reload or disable.
    """
    try:
        await invoke_lifecycle_handler(
            handler,
            extension_name=extension_name,
            description=f"{phase} handler",
            cancel_on_timeout=True,
        )
    except ExtensionHandlerTimeoutError as exc:
        _LOGGER.error(
            "Extension %r %s handler %s; continuing without it",
            extension_name,
            phase,
            exc,
        )
    except Exception as exc:
        _LOGGER.error(
            "Extension %r %s handler raised: %s",
            extension_name,
            phase,
            exc,
            exc_info=True,
        )


async def invoke_hook_fail_open(
    event: str,
    extension_name: str,
    handler: HookHandler,
    ctx: HookContext,
    payload: dict[str, Any],
) -> Any:
    """Call one handler with per-handler exception isolation.

    Awaits async handlers. On failure logs at ``warning`` and returns the
    ``HANDLER_FAILED`` sentinel so callers can skip the handler without
    confusing a raised handler with one that returned ``None``.
    """
    started_at = time.perf_counter()
    try:
        return await invoke_isolated_handler(
            handler, ctx, extension_name=extension_name, description=f"{event} handler", **payload
        )
    except Exception as exc:
        _LOGGER.warning(
            "Extension %r %s handler raised: %s",
            extension_name,
            event,
            exc,
            exc_info=True,
        )
        return HANDLER_FAILED
    finally:
        _log_slow_extension_handler(
            extension_name=extension_name,
            handler_kind=event,
            started_at=started_at,
        )
