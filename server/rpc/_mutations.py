"""Serialize RPC mutations through persistence, live refresh and publication."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from core.utils.logging import get_logger
from core.utils.workers import settle_before_cancelling
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
            # Keep the lock until refresh/publication catch up with any write,
            # including when the caller requests cancellation more than once.
            return await settle_before_cancelling(
                handler(state, params),
                on_late_failure=partial(_log_cancelled_failure, handler.__name__),
            )

    return run


def _log_cancelled_failure(handler: str, error: BaseException) -> None:
    if isinstance(error, RpcError):
        _LOGGER.warning("Cancelled RPC mutation rejected (handler=%s code=%s)", handler, error.code)
        return
    _LOGGER.error("Cancelled RPC mutation failed (handler=%s)", handler, exc_info=error)
