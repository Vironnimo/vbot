"""Portal RPCs: the live Portals of a Session for its cards, and closing one from a card."""

from __future__ import annotations

from typing import Any

from core.tools.portal_registry import Portal
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.validation import (
    _reject_unsupported,
    _required_session_address,
    _required_string,
)

JsonObject = dict[str, Any]


def _portal_list(state: Any, params: JsonObject) -> JsonObject:
    """List a Session's open Portals, each with the capability that opens it.

    The transport binds each capability to the requesting principal. Portals
    have ports of their own, which a reverse proxy in front of vBot does not
    forward: ``reverse_proxy`` tells the card that only this computer reaches them.
    """
    _reject_unsupported(params, {"agent_id", "session_id"}, "portal.list")
    address = _required_session_address(params)
    registry = state.runtime.portals
    access = state.access.config
    return {
        "portals": [_portal_payload(state, portal) for portal in registry.list(address)],
        "reverse_proxy": access.mode == "authenticated" and not access.direct_tls,
    }


def _portal_close(state: Any, params: JsonObject) -> JsonObject:
    """Close one Portal; closing a Portal that is no longer open changes nothing."""
    _reject_unsupported(params, {"id"}, "portal.close")
    portal_id = _required_string(params, "id")
    closed = state.runtime.portals.close(portal_id)
    return {"id": portal_id, "closed": closed is not None}


def _portal_payload(state: Any, portal: Portal) -> JsonObject:
    return {
        "id": portal.id,
        "title": portal.title,
        "path": portal.path,
        "target_port": portal.target_port,
        "listen_port": portal.listen_port,
        "listening": portal.listening,
        "token": state.file_delivery.portal_token(portal.id),
    }


def method_handlers() -> dict[str, RpcMethodHandler]:
    return {"portal.list": _portal_list, "portal.close": _portal_close}
