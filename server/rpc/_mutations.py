"""Keep RPC persistence, live publication and dependent read projections coherent."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from core.utils.logging import get_logger
from core.utils.workers import settle_before_cancelling
from server.rpc.errors import RpcError

JsonObject = dict[str, Any]
MutationHandler = Callable[[Any, JsonObject], Awaitable[JsonObject]]

_LOGGER = get_logger("server.rpc.mutations")


@dataclass
class _ReadLease:
    lock: asyncio.Lock
    accepting: bool = True
    readers: int = 0
    idle: asyncio.Event = field(default_factory=asyncio.Event)

    async def drain(self) -> None:
        self.accepting = False
        if self.readers:
            await self.idle.wait()


_READ_LEASES: ContextVar[tuple[_ReadLease, ...]] = ContextVar("rpc_read_leases", default=())


def _current_lease(lock: asyncio.Lock) -> _ReadLease | None:
    return next(
        (lease for lease in reversed(_READ_LEASES.get()) if lease.lock is lock and lease.accepting),
        None,
    )


def state_lock(state: Any, lock_attribute: str) -> asyncio.Lock:
    """Return the Event Loop lock shared by one state's mutation and read surfaces."""
    lock: asyncio.Lock | None = getattr(state, lock_attribute, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(state, lock_attribute, lock)
    return lock


@asynccontextmanager
async def state_read_lock(state: Any, lock_attribute: str) -> AsyncIterator[None]:
    """Join an enclosing mutation's read lease, or acquire its publication lock.

    Lifecycle callbacks run in descendant tasks. Their reads observe the state
    currently being published without waiting for the mutation awaiting them.
    Unrelated calls still wait, and inherited contexts lose access on settlement.
    """
    lock = state_lock(state, lock_attribute)
    lease = _current_lease(lock)
    if lease is None:
        async with lock:
            yield
        return
    lease.readers += 1
    lease.idle.clear()
    try:
        yield
    finally:
        lease.readers -= 1
        if not lease.readers:
            lease.idle.set()


def serialized_read(handler: MutationHandler, *, lock_attribute: str) -> MutationHandler:
    """Keep one read projection on the same published configuration across awaits."""

    async def run(state: Any, params: JsonObject) -> JsonObject:
        async with state_read_lock(state, lock_attribute):
            return await handler(state, params)

    return run


def serialized_mutation(handler: MutationHandler, *, lock_attribute: str) -> MutationHandler:
    """Keep a state's mutation sequence intact even when its caller is cancelled."""

    async def run(state: Any, params: JsonObject) -> JsonObject:
        lock = state_lock(state, lock_attribute)
        if _current_lease(lock) is not None:
            raise RpcError(
                "mutation_in_progress",
                "A callback cannot start another mutation of the state being published",
            )
        async with lock:
            lease = _ReadLease(lock)

            async def apply() -> JsonObject:
                token = _READ_LEASES.set((*_READ_LEASES.get(), lease))
                try:
                    return await handler(state, params)
                finally:
                    _READ_LEASES.reset(token)
                    # Detached callback work may already have an admitted read.
                    # Keep the publication lock until its workers have settled.
                    await settle_before_cancelling(lease.drain())

            # Keep the lock until refresh/publication catch up with any write,
            # including when the caller requests cancellation more than once.
            return await settle_before_cancelling(
                apply(),
                on_late_failure=partial(_log_cancelled_failure, handler.__name__),
            )

    return run


def _log_cancelled_failure(handler: str, error: BaseException) -> None:
    if isinstance(error, RpcError):
        _LOGGER.warning("Cancelled RPC mutation rejected (handler=%s code=%s)", handler, error.code)
        return
    _LOGGER.error("Cancelled RPC mutation failed (handler=%s)", handler, exc_info=error)
