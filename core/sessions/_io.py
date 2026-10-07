"""Reentrant Session write leases and chat-history cursors."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import json
from dataclasses import dataclass
from typing import Self

from core.sessions._types import _CHAT_HISTORY_CURSOR_PREFIX


class SessionWriteLeaseScope:
    """Lets work started by one task share the Session write leases that task holds.

    Session write locks are reentrant for the holding task and the tasks it
    starts while it holds them. Work started *before* the holder took a lock
    does not carry that lease: if the holder then waits for such work while the
    work waits for the same lock, both stop. Inside this scope, a lease the
    owning task takes is shared with every task whose context carries the scope,
    so that work joins the lease instead of waiting for it. Leases the other
    tasks take themselves stay their own.
    """

    def __init__(self) -> None:
        self._owner: asyncio.Task[object] | None = None
        self._token: contextvars.Token[SessionWriteLeaseScope | None] | None = None

    def __enter__(self) -> Self:
        if self._token is not None:
            raise RuntimeError("session write lease scope is already active")
        self._owner = asyncio.current_task()
        self._token = _LEASE_SCOPE.set(self)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._token is not None:
            _LEASE_SCOPE.reset(self._token)
            self._token = None
        self._owner = None

    def _shares(self) -> bool:
        return self._owner is not None and asyncio.current_task() is self._owner


_LEASE_SCOPE: contextvars.ContextVar[SessionWriteLeaseScope | None] = contextvars.ContextVar(
    "session_write_lease_scope", default=None
)


@dataclass
class _SessionWriteLease:
    holders: int = 1
    active: bool = True
    # The scope whose tasks may join this lease; ``None`` keeps it private.
    scope: SessionWriteLeaseScope | None = None


class _SessionWriteLock:
    def __init__(self) -> None:
        self._held: _SessionWriteLease | None = None
        self._waiters: list[asyncio.Future[None]] = []
        self._leases: contextvars.ContextVar[tuple[_SessionWriteLease, ...]] = (
            contextvars.ContextVar("session_write_lock_leases", default=())
        )

    def locked(self) -> bool:
        return self._held is not None

    async def __aenter__(self) -> Self:
        stack = tuple(lease for lease in self._leases.get() if lease.active)
        if stack:
            stack[-1].holders += 1
            self._leases.set((*stack, stack[-1]))
            return self
        scope = _LEASE_SCOPE.get()
        while True:
            held = self._held
            if held is None:
                lease = _SessionWriteLease(
                    scope=scope if scope is not None and scope._shares() else None
                )
                self._held = lease
                break
            if scope is not None and held.scope is scope:
                held.holders += 1
                lease = held
                break
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            try:
                await waiter
            finally:
                self._waiters.remove(waiter)
        self._leases.set((*stack, lease))
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        stack = self._leases.get()
        if not stack:
            raise RuntimeError("session write lock exited without an active lease")
        lease = stack[-1]
        self._leases.set(stack[:-1])
        lease.holders -= 1
        if lease.holders == 0:
            lease.active = False
            self._held = None
            # Every waiter checks again; one takes the lock, the rest wait on.
            for waiter in self._waiters:
                if not waiter.done():
                    waiter.set_result(None)


def _encode_chat_history_cursor(generation_id: str, sequence: int) -> str:
    body = json.dumps(
        {"generation_id": generation_id, "sequence": sequence},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(body).digest()
    token = base64.urlsafe_b64encode(body + digest).decode("ascii").rstrip("=")
    return f"{_CHAT_HISTORY_CURSOR_PREFIX}{token}"
