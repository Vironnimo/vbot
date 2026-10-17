"""Device management, pairing approval and login ticket RPC handlers.

Every handler acts for the principal the access guard attached to the request.
In-process dispatch has none and is refused: no Agent or Live call may manage
devices on its own.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from server.access import (
    ACCESS_DENIED,
    DEVICE_NOT_FOUND,
    PAIRING_NOT_FOUND,
    AccessError,
    AccessService,
    Principal,
    current_principal,
)
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError

JsonObject = dict[str, Any]

# Answers that are part of normal use: a mistyped code, a device removed meanwhile.
_ROUTINE_CODES = frozenset({PAIRING_NOT_FOUND, DEVICE_NOT_FOUND})


def _login_ticket(state: Any, params: JsonObject) -> JsonObject:
    _accept(params, "auth.login_ticket", {"name"})
    service, principal = _context(state)
    return _answer(lambda: service.issue_ticket(principal, params.get("name")))


def _list_devices(state: Any, params: JsonObject) -> JsonObject:
    _accept(params, "devices.list", set())
    service, principal = _context(state)
    return _answer(lambda: service.list_devices(principal))


async def _rename_device(state: Any, params: JsonObject) -> JsonObject:
    _accept(params, "devices.rename", {"id", "name"})
    service, principal = _context(state)
    return await _answer_async(
        lambda: service.rename_device(params.get("id"), params.get("name"), principal)
    )


async def _revoke_device(state: Any, params: JsonObject) -> JsonObject:
    _accept(params, "devices.revoke", {"id"})
    service, principal = _context(state)
    device_id = params.get("id")
    if not isinstance(device_id, str) or not device_id:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "devices.revoke needs the device id")
    return await _revoked(service.revoke_devices([device_id], principal))


async def _revoke_other_devices(state: Any, params: JsonObject) -> JsonObject:
    _accept(params, "devices.revoke_others", set())
    service, principal = _context(state)
    others = _answer(lambda: service.other_device_ids(principal))
    return await _revoked(service.revoke_devices(others, principal, missing_ok=True))


async def _log_out(state: Any, params: JsonObject) -> JsonObject:
    _accept(params, "devices.logout", set())
    service, principal = _context(state)
    if principal.device is None:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST, "Only a paired device can log out; this request has none"
        )
    return await _revoked(service.revoke_devices([principal.device.id], principal, missing_ok=True))


def _pending_pairing(state: Any, params: JsonObject) -> JsonObject:
    _accept(params, "devices.pending", {"code"})
    service, principal = _context(state)
    return _answer(lambda: service.pending_pairing(params.get("code"), principal))


def _approve_pairing(state: Any, params: JsonObject) -> JsonObject:
    _accept(params, "devices.approve", {"code"})
    service, principal = _context(state)
    return _answer(lambda: service.decide_pairing(params.get("code"), principal, approved=True))


def _deny_pairing(state: Any, params: JsonObject) -> JsonObject:
    _accept(params, "devices.deny", {"code"})
    service, principal = _context(state)
    return _answer(lambda: service.decide_pairing(params.get("code"), principal, approved=False))


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the access RPC handlers."""

    return {
        "auth.login_ticket": _login_ticket,
        "devices.list": _list_devices,
        "devices.rename": _rename_device,
        "devices.revoke": _revoke_device,
        "devices.revoke_others": _revoke_other_devices,
        "devices.logout": _log_out,
        "devices.pending": _pending_pairing,
        "devices.approve": _approve_pairing,
        "devices.deny": _deny_pairing,
    }


def _context(state: Any) -> tuple[AccessService, Principal]:
    principal = current_principal()
    service = getattr(state, "access", None)
    if principal is None or not isinstance(service, AccessService):
        raise RpcError(ACCESS_DENIED, "Device management needs a request from a signed-in client")
    return service, principal


def _accept(params: JsonObject, method: str, allowed: set[str]) -> None:
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"{method} does not accept: {', '.join(unknown)}")


def _answer[T](call: Callable[[], T]) -> T:
    try:
        return call()
    except AccessError as exc:
        raise _rpc_error(exc) from None


async def _answer_async(call: Callable[[], Awaitable[JsonObject]]) -> JsonObject:
    try:
        return await call()
    except AccessError as exc:
        raise _rpc_error(exc) from None


async def _revoked(revocation: Awaitable[list[Any]]) -> JsonObject:
    try:
        removed = await revocation
    except AccessError as exc:
        raise _rpc_error(exc) from None
    return {"revoked": [device.id for device in removed]}


def _rpc_error(exc: AccessError) -> RpcError:
    return RpcError(
        exc.code,
        exc.message,
        data=None if exc.retry_after is None else {"retry_after": exc.retry_after},
        routine=exc.code in _ROUTINE_CODES,
    )
