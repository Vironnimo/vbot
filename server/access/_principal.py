"""Who a request acts for: the principal the access guard attached."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

from core.access.devices import HOST_PRINCIPAL, Device

#: The request scope key that carries the principal.
PRINCIPAL_SCOPE_KEY = "vbot.principal"

PrincipalKind = Literal["host", "device", "open"]


@dataclass(frozen=True)
class Principal:
    """``host``: the local access token. ``device``: a paired device.
    ``open``: a request ``local`` or ``lan`` admitted without a credential."""

    kind: PrincipalKind
    device: Device | None = None

    @property
    def actor(self) -> str:
        """How logs and the device store name this principal."""
        if self.device is not None:
            return self.device.id
        return HOST_PRINCIPAL if self.kind == "host" else "open"


HOST = Principal("host")
OPEN = Principal("open")


@dataclass(frozen=True)
class _Binding:
    principal: Principal | None
    task: asyncio.Task[Any] | None


_CURRENT: ContextVar[_Binding | None] = ContextVar("vbot_access_principal", default=None)


def current_principal() -> Principal | None:
    """The principal of the HTTP RPC request being dispatched, for RPC handlers.

    ``None`` everywhere else: in-process dispatch (Live calls) and every task a
    request starts (Runs, background work). Such tasks inherit the request's
    context, so the binding also names its task and counts only there.
    """
    binding = _CURRENT.get()
    if binding is None or binding.task is not _current_task():
        return None
    return binding.principal


def scope_principal(scope: Mapping[str, Any]) -> Principal | None:
    principal = scope.get(PRINCIPAL_SCOPE_KEY)
    return principal if isinstance(principal, Principal) else None


@contextmanager
def bound_principal(principal: Principal | None) -> Iterator[None]:
    """Let RPC handlers in this task act for ``principal``; ``None`` for none."""
    token = _CURRENT.set(_Binding(principal, _current_task()))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def _current_task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None
