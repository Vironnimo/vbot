"""Serialize RPC mutations through persistence, live refresh and publication."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from core.utils.logging import get_logger
from server.rpc.errors import RpcError

JsonObject = dict[str, Any]
MutationHandler = Callable[[Any, JsonObject], Awaitable[JsonObject]]

_LOGGER = get_logger("server.rpc.mutations")


def serialized_mutation(handler: MutationHandler, *, lock_attribute: str) -> MutationHandler:
    """Keep a state's mutation sequence intact even when its caller is cancelled."""

    async def run(state: Any, params: JsonObject) -> JsonObject:
        lock = getattr(state, lock_attribute, None)
        if lock is None:
            lock = asyncio.Lock()
            setattr(state, lock_attribute, lock)
        async with lock:
            task = asyncio.ensure_future(handler(state, params))
            try:
                # Waiting never cancels the admitted task. Unlike a cancelled
                # shield, it also leaves failure reporting to this RPC owner.
                await asyncio.wait({task})
                return task.result()
            except asyncio.CancelledError:
                # Keep the lock until refresh/publication catch up with any write,
                # including when the caller requests cancellation more than once.
                while not task.done():
                    with suppress(asyncio.CancelledError):
                        await asyncio.wait({task})
                if not task.cancelled():
                    error = task.exception()
                    if isinstance(error, RpcError):
                        _LOGGER.warning(
                            "Cancelled RPC mutation rejected (handler=%s code=%s)",
                            handler.__name__,
                            error.code,
                        )
                    elif error is not None:
                        _LOGGER.error(
                            "Cancelled RPC mutation failed (handler=%s)",
                            handler.__name__,
                            exc_info=(type(error), error, error.__traceback__),
                        )
                raise

    return run
