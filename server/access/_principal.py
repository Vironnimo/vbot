"""Who a request acts for: the principal the access guard attached."""

from __future__ import annotations

from collections.abc import Mapping
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

_CURRENT: ContextVar[Principal | None] = ContextVar("vbot_access_principal", default=None)


def current_principal() -> Principal | None:
    """The principal of the request being handled, for RPC handlers.

    ``None`` outside a request, for example for in-process RPC dispatch.
    """
    return _CURRENT.get()


def scope_principal(scope: Mapping[str, Any]) -> Principal | None:
    principal = scope.get(PRINCIPAL_SCOPE_KEY)
    return principal if isinstance(principal, Principal) else None


def bind_principal(principal: Principal | None) -> Any:
    """Set the principal for the current context; returns the reset token."""
    return _CURRENT.set(principal)


def unbind_principal(token: Any) -> None:
    _CURRENT.reset(token)
