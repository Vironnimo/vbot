"""Serialize RPC mutations through persistence, live refresh and publication."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

JsonObject = dict[str, Any]
MutationHandler = Callable[[Any, JsonObject], Awaitable[JsonObject]]


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
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Keep the lock until refresh/publication catch up with any write,
                # including when the caller requests cancellation more than once.
                while not task.done():
                    with suppress(asyncio.CancelledError, Exception):
                        await asyncio.shield(task)
                if not task.cancelled():
                    task.exception()
                raise

    return run
