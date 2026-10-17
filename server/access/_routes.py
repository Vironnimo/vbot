"""The unauthenticated access endpoints: status, pairing and login ticket redemption."""

from __future__ import annotations

import html
import json
from typing import Any

from server._http_dependencies import (
    FastAPIType,
    HTTPException,
    RedirectResponse,
    Request,
    Response,
)
from server._request_bodies import _read_json_payload_with_limit, _require_json_media_type
from server.access._principal import scope_principal
from server.access._rules import (
    AUTH_STATUS_PATH,
    PAIR_POLL_PATH,
    PAIR_START_PATH,
    TICKET_PATH_PREFIX,
    RequestFacts,
)
from server.access._service import (
    DEVICE_COOKIE,
    DEVICE_COOKIE_MAX_AGE,
    DEVICES_UNAVAILABLE,
    INVALID_REQUEST,
    AccessError,
    AccessService,
)

_NO_STORE = {"Cache-Control": "no-store"}
_TICKET_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
}


def register_access_routes(app: FastAPIType, service: AccessService) -> None:
    """Mount ``/api/auth/*`` and ``/auth/ticket/<ticket>`` (exempt from the guard)."""

    @app.get(AUTH_STATUS_PATH, include_in_schema=False)
    async def auth_status(request: Request) -> Response:
        principal = scope_principal(request.scope)
        return _json(
            {
                "mode": service.config.mode,
                "mode_label": service.config.label,
                "authenticated": principal is not None,
                "principal": None if principal is None else principal.kind,
                "device": (
                    None
                    if principal is None or principal.device is None
                    else {
                        "id": principal.device.id,
                        "name": principal.device.name,
                        "kind": principal.device.kind,
                    }
                ),
            }
        )

    @app.post(PAIR_START_PATH, include_in_schema=False)
    async def pair_start(request: Request) -> Response:
        try:
            body = await _json_body(request)
            return _json(service.pair_start(body, _facts(service, request)))
        except AccessError as exc:
            return _error(exc)

    @app.post(PAIR_POLL_PATH, include_in_schema=False)
    async def pair_poll(request: Request) -> Response:
        try:
            body = await _json_body(request)
            payload, cookie_token = await service.pair_poll(body, _facts(service, request))
        except AccessError as exc:
            return _error(exc)
        response = _json(payload)
        if cookie_token is not None:
            _set_device_cookie(response, cookie_token)
        return response

    @app.get(TICKET_PATH_PREFIX + "{ticket}", include_in_schema=False)
    async def redeem_ticket(request: Request, ticket: str) -> Response:
        facts = _facts(service, request)
        try:
            redemption = await service.redeem_ticket(ticket, facts, scope_principal(request.scope))
        except AccessError as exc:
            if exc.code == DEVICES_UNAVAILABLE:
                return _ticket_page(
                    "Sign-in not available",
                    "The server cannot check its devices right now. "
                    "Try again later; the host finds the cause in the server log.",
                    status_code=503,
                )
            redemption = None
        if redemption is None:
            service.refused("invalid_ticket", facts, request.url.path)
            service.failed_authentication(facts)
            return _ticket_page(
                "Sign-in link not valid",
                "This sign-in link is invalid, expired or already used. "
                "Open vBot again from the device that created it.",
                status_code=400,
            )
        response = RedirectResponse(
            redemption.next,
            status_code=303,
            headers={**_NO_STORE, "Referrer-Policy": "no-referrer"},
        )
        if redemption.token is not None:
            _set_device_cookie(response, redemption.token)
        return response


def _facts(service: AccessService, request: Request) -> RequestFacts:
    return service.inspect(request.scope)


async def _json_body(request: Request) -> Any:
    try:
        _require_json_media_type(request)
        return await _read_json_payload_with_limit(request)
    except ValueError:
        raise AccessError(INVALID_REQUEST, "The body must be valid JSON") from None
    except HTTPException as exc:
        raise AccessError(INVALID_REQUEST, str(exc.detail), status=exc.status_code) from None


def _json(payload: dict[str, Any], *, status_code: int = 200) -> Response:
    return Response(
        json.dumps(payload),
        status_code=status_code,
        media_type="application/json",
        headers=_NO_STORE,
    )


def _error(exc: AccessError) -> Response:
    response = _json(
        {"ok": False, "error": {"code": exc.code, "message": exc.message}},
        status_code=exc.status,
    )
    if exc.retry_after is not None:
        response.headers["Retry-After"] = str(exc.retry_after)
    return response


def _set_device_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        DEVICE_COOKIE,
        token,
        max_age=DEVICE_COOKIE_MAX_AGE,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _ticket_page(title: str, message: str, *, status_code: int) -> Response:
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;"
        "padding:0 1rem;line-height:1.5}</style></head>"
        f"<body><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></body></html>"
    )
    return Response(
        body,
        status_code=status_code,
        headers=_TICKET_PAGE_HEADERS,
        media_type="text/html; charset=utf-8",
    )
