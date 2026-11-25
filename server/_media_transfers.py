"""Short-lived reservations for resuming an interrupted browser media request."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Any

from core.utils.workers import finish_despite_cancel
from server._http_dependencies import FastAPIType, HTTPException, Request, Response
from server.access import scope_principal

_HEADER = "x-vbot-media-transfer"
_LIFETIME = 15 * 60
_LIMIT = 512
_LOGGER = logging.getLogger("vbot.server.media")
type Payload = dict[str, Any]


@dataclass
class Transfer:
    actor: str
    kind: str
    expire: Callable[[], None]
    timer: asyncio.TimerHandle | None = None
    observers: int = 0
    task: asyncio.Task[tuple[int, Payload]] | None = None
    released: bool = False

    def renew(self) -> None:
        if self.timer:
            self.timer.cancel()
            self.timer = None
        if not self.observers and not self.released:
            self.timer = asyncio.get_running_loop().call_later(_LIFETIME, self.expire)

    @contextmanager
    def observed(self) -> Iterator[None]:
        # Only unattended work expires. A slow upload or a connected user
        # listening to local inference keeps its original request lifetime.
        self.observers += 1
        self.renew()
        try:
            yield
        finally:
            self.observers -= 1
            self.renew()

    def start(self, operation: Callable[[], Awaitable[Payload]]) -> None:
        if self.released:
            raise HTTPException(410, "Media transfer expired")
        if self.task is not None:
            raise HTTPException(409, {"code": "media_already_submitted"})
        self.task = asyncio.create_task(self._execute(operation), name="browser-media-transfer")
        self.task.add_done_callback(lambda _: self.renew())

    async def _execute(self, operation: Callable[[], Awaitable[Payload]]) -> tuple[int, Payload]:
        # Keep only the public result. An exception retains the upload through its traceback.
        try:
            return 200, await operation()
        except HTTPException as exc:
            return exc.status_code, {"detail": exc.detail}
        except asyncio.CancelledError:
            return 410, {"detail": "Media transfer cancelled"}
        except Exception:
            _LOGGER.exception("Browser media transfer failed")
            return 500, {"detail": "Media transfer failed"}

    async def result(self) -> Payload:
        assert self.task is not None
        try:
            with self.observed():
                status, payload = await asyncio.shield(self.task)
        except asyncio.CancelledError:
            if self.released:
                raise HTTPException(410, "Media transfer cancelled") from None
            raise
        if status != 200:
            raise HTTPException(status, payload["detail"], headers={"X-Vbot-Media-Settled": "1"})
        return payload


class MediaTransfers:
    """One application owns its admitted requests across transient disconnects."""

    def __init__(self) -> None:
        self._entries: dict[str, Transfer] = {}
        self._settling: set[asyncio.Task[Any]] = set()

    def reserve(self, request: Request, kind: str) -> str:
        if kind not in {"attachment", "transcription"}:
            raise HTTPException(404, "Unknown media transfer")
        if len(self._entries) + len(self._settling) >= _LIMIT:
            raise HTTPException(429, "Too many pending media transfers")
        actor = self._actor(request)
        key = token_urlsafe(24)
        transfer = Transfer(actor, kind, lambda: self.release(key))
        self._entries[key] = transfer
        transfer.renew()
        return key

    @staticmethod
    def _actor(request: Request) -> str:
        principal = scope_principal(request.scope)
        if principal is None:
            raise HTTPException(403, "Media transfer access denied")
        return principal.actor

    def lookup(self, request: Request, kind: str) -> Transfer:
        transfer = self._entries.get(request.headers.get(_HEADER, ""))
        if transfer is None or transfer.kind != kind or transfer.actor != self._actor(request):
            raise HTTPException(410, "Media transfer expired")
        return transfer

    @contextmanager
    def receiving(self, request: Request, kind: str) -> Iterator[Transfer | None]:
        if _HEADER not in request.headers:
            yield None
            return
        transfer = self.lookup(request, kind)
        with transfer.observed():
            yield transfer

    def release(self, key: str) -> None:
        transfer = self._entries.pop(key, None)
        if transfer is not None:
            transfer.released = True
            transfer.renew()
            task = transfer.task
            if task is not None and not task.done():
                self._settling.add(task)
                task.add_done_callback(self._settling.discard)
                task.cancel()

    async def aclose(self) -> None:
        for key in list(self._entries):
            self.release(key)
        await asyncio.gather(*self._settling, return_exceptions=True)


async def until_disconnect[Result](request: Request, operation: Awaitable[Result]) -> Result:
    """Observe an operation after the body, cancelling its waiter when the client leaves."""

    async def disconnected() -> None:
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    task = asyncio.ensure_future(operation)
    disconnect = asyncio.create_task(disconnected(), name="media-disconnect")
    try:
        done, _pending = await asyncio.wait({task, disconnect}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        disconnect.result()
        # Nothing reaches the departed client; finish the request without an
        # unhandled cancellation or an internal-error log.
        raise HTTPException(status_code=499, detail="Client disconnected")
    finally:
        if not task.done():
            task.cancel()
        disconnect.cancel()
        await finish_despite_cancel(asyncio.gather(task, disconnect, return_exceptions=True))


def register_transfer_routes(app: FastAPIType) -> None:
    transfers = MediaTransfers()
    app.state.media_transfers = transfers

    @app.post("/api/media/transfers/{kind}")
    async def reserve(request: Request, kind: str) -> Payload:
        return {"ticket": transfers.reserve(request, kind)}

    @app.get("/api/media/transfers/{kind}")
    async def resume(request: Request, kind: str, response: Response) -> Any:
        headers = {"Cache-Control": "no-store"}
        response.headers.update(headers)
        try:
            transfer = transfers.lookup(request, kind)
            if transfer.task is None:
                return Response(status_code=204, headers=headers)
            return await until_disconnect(request, transfer.result())
        except HTTPException as exc:
            exc.headers = {**(exc.headers or {}), **headers}
            raise

    @app.delete("/api/media/transfers/{kind}")
    async def release(request: Request, kind: str) -> Response:
        transfers.lookup(request, kind)
        transfers.release(request.headers[_HEADER])
        return Response(status_code=204)
