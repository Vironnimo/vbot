"""Bounded Session I/O and reentrant write leases."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from core.sessions._types import _CHAT_HISTORY_CURSOR_PREFIX
from core.utils.workers import BoundedWorkerPool

_SessionIoResult = TypeVar("_SessionIoResult")
_SESSION_IO_WORKERS = BoundedWorkerPool(name="session-io", max_workers=8)


@dataclass
class _SessionWriteLease:
    holders: int = 1
    active: bool = True


class _SessionWriteLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._leases: contextvars.ContextVar[tuple[_SessionWriteLease, ...]] = (
            contextvars.ContextVar("session_write_lock_leases", default=())
        )

    async def __aenter__(self) -> _SessionWriteLock:
        stack = tuple(lease for lease in self._leases.get() if lease.active)
        if stack:
            stack[-1].holders += 1
            self._leases.set((*stack, stack[-1]))
            return self
        await self._lock.acquire()
        self._leases.set((*stack, _SessionWriteLease()))
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
            self._lock.release()


async def _run_session_io(
    function: Callable[..., _SessionIoResult],
    *arguments: Any,
) -> _SessionIoResult:
    return await _SESSION_IO_WORKERS.run(function, *arguments)


def _encode_chat_history_cursor(generation_id: str, sequence: int) -> str:
    body = json.dumps(
        {"generation_id": generation_id, "sequence": sequence},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(body).digest()
    token = base64.urlsafe_b64encode(body + digest).decode("ascii").rstrip("=")
    return f"{_CHAT_HISTORY_CURSOR_PREFIX}{token}"
