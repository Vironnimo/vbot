"""``POST /api/hooks/<job id>``: where a Cron webhook job receives its requests.

The access guard applies the access mode's peer, transport and Host rules and
then lets the request through without a principal: the request proves the
job's own secret (``Authorization: Bearer <secret>`` or GitHub's
``X-Hub-Signature-256``), which the Cron service checks. Before the body is
read, the access service counts the request against the webhook rate limits.

Answers: 202 once the request is stored for the job's next Run; 404 for an
unknown, inactive or non-webhook job and for a wrong or missing secret alike;
413 for a body over ``WEBHOOK_BODY_MAX_BYTES``; 429 with ``Retry-After`` while
the client is over a limit; 503 when a request that proved the secret could
not be stored. Nothing of a request's body or headers is logged.
"""

from __future__ import annotations

import json
import socket
from typing import Any, Literal

from core.access.config import AccessConfig, normalize_host_name, parse_ip
from core.automation.cron import WEBHOOK_BODY_MAX_BYTES, WEBHOOK_PATH_PREFIX, WebhookReach
from server._bind import ServerBindState
from server._http_dependencies import FastAPIType, Request, Response
from server.access import RATE_LIMITED, AccessService
from server.access._service import WEBHOOK_REFUSED, WEBHOOK_TOO_LARGE

_NOT_FOUND = (404, "not_found", "Not found")
_UNAVAILABLE = (503, "unavailable", "The request could not be stored; try again later")


def register_webhook_routes(app: FastAPIType, access: AccessService) -> None:
    """Mount the Cron webhook route (exempt from authentication by the access guard)."""

    @app.post(f"{WEBHOOK_PATH_PREFIX}{{job_id}}", include_in_schema=False)
    async def cron_webhook(request: Request, job_id: str) -> Response:
        path = str(request.scope.get("path") or "")
        facts = access.inspect(request.scope)
        wait = access.admit_webhook(facts, path)
        if wait is not None:
            return _answer(429, RATE_LIMITED, "Too many webhook requests", retry_after=wait)
        body = await _read_body(request)
        if body is None:
            access.refused_webhook(WEBHOOK_TOO_LARGE, facts, path)
            return _answer(
                413, "too_large", f"The request body exceeds {WEBHOOK_BODY_MAX_BYTES} bytes"
            )
        headers = [
            (name.decode("latin-1").lower(), value.decode("latin-1"))
            for name, value in request.scope.get("headers") or ()
        ]
        receipt: Literal["accepted", "refused", "unavailable"] = "refused"
        try:
            cron = request.app.state.runtime.cron_service
        except AttributeError, RuntimeError:
            cron = None
        if cron is not None:
            receipt = await cron.receive_webhook(job_id, headers, body)
        if receipt == "accepted":
            return Response(
                json.dumps({"ok": True}),
                status_code=202,
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )
        if receipt == "unavailable":
            return _answer(*_UNAVAILABLE, retry_after=30)
        access.refused_webhook(WEBHOOK_REFUSED, facts, path)
        return _answer(*_NOT_FOUND)


def webhook_base_url(
    access: AccessConfig, server_bind: ServerBindState
) -> tuple[str, WebhookReach]:
    """The address webhook URLs start with, and who can reach it.

    The public URL when one is configured; otherwise the address the server
    listens on. For every interface that is this computer's name on the home
    network (``lan``), else loopback.
    """
    if access.public_url is not None:
        return access.public_url, "internet"
    reach: WebhookReach = "network" if access.mode == "lan" else "computer"
    host = server_bind["listen_host"].removeprefix("[").removesuffix("]")
    if host in {"", "*", "0.0.0.0", "::"}:
        name = _machine_name() if reach == "network" else None
        address = name or ("[::1]" if host == "::" else "127.0.0.1")
    elif ":" in host:
        address = f"[{host}]"
    else:
        address = host
    scheme = "https" if access.direct_tls else "http"
    return f"{scheme}://{address}:{server_bind['listen_port']}", reach


def _machine_name() -> str | None:
    """This computer's host name, which the access guard answers to; None without one."""
    try:
        name = normalize_host_name(socket.gethostname())
    except OSError:
        return None
    return name if name is not None and parse_ip(name) is None else None


async def _read_body(request: Request) -> bytes | None:
    """The body, or None once it exceeds ``WEBHOOK_BODY_MAX_BYTES``."""
    declared = (request.headers.get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > WEBHOOK_BODY_MAX_BYTES:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > WEBHOOK_BODY_MAX_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _answer(status: int, code: str, message: str, *, retry_after: float | None = None) -> Response:
    headers: dict[str, Any] = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = str(max(1, round(retry_after)))
    return Response(
        json.dumps({"ok": False, "error": {"code": code, "message": message}}),
        status_code=status,
        media_type="application/json",
        headers=headers,
    )
