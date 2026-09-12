"""Bounded callback execution and slow-handler diagnostics."""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from typing import Any

from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

_LOGGER = get_logger("extensions")
_EXTENSION_WORKER_LIMIT = 8
_SLOW_EXTENSION_HANDLER_SECONDS = 1.0
_EXTENSION_WORKERS = BoundedWorkerPool(
    name="extension",
    max_workers=_EXTENSION_WORKER_LIMIT,
)


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
