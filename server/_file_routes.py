"""Capability HTTP routes that retain the verified file throughout the response."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from pathlib import Path
from secrets import token_hex
from typing import Any, override

from core.utils.workers import settle_before_cancelling
from server._capability_file import VerifiedFile
from server._http_dependencies import (
    FastAPIType,
    FileResponse,
    HTTPException,
    RedirectResponse,
    Request,
    Response,
)
from server.access import scope_principal
from server.file_delivery import FILE_PREVIEW_WORKERS, PREVIEW_URL_PREFIX, FileDelivery, OpenedFile

type Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class _CapabilityResponse(Response):  # type: ignore[misc]
    """Keep a file owned from worker admission until the ASGI response settles."""

    def __init__(self, prepare: Callable[[_CapabilityResponse], Awaitable[Response]]) -> None:
        super().__init__()
        self._prepare = prepare
        self._opened: OpenedFile | None = None

    async def open_file[Opened: OpenedFile | None](
        self, opener: Callable[..., Opened], *args: object
    ) -> Opened:
        def acquire() -> Opened:
            # Run records the result here even if cancellation prevents the
            # awaiting task from receiving it after the worker settles.
            opened = opener(*args)
            self._opened = opened
            return opened

        return await FILE_PREVIEW_WORKERS.run(acquire)

    @override
    async def __call__(self, scope: Any, receive: Any, send: Send) -> None:
        try:
            response = await self._prepare(self)
            await response(scope, receive, send)
        finally:
            if self._opened is not None:
                await settle_before_cancelling(FILE_PREVIEW_WORKERS.run(self._opened.close))


class _UnavailablePath:
    """Keep inherited response code from opening or exposing a pathname."""

    def __fspath__(self) -> str:
        raise RuntimeError("Capability responses require their verified file handle")

    @override
    def __str__(self) -> str:
        return "<verified file handle>"


class _HandleFileResponse(FileResponse):  # type: ignore[misc]
    """Reuse FileResponse's HTTP semantics while replacing its pathname reads."""

    def __init__(
        self,
        opened: OpenedFile,
        *,
        headers: Mapping[str, str],
        filename: str | None = None,
        disposition: str = "attachment",
    ) -> None:
        self._handle: VerifiedFile = opened.handle
        super().__init__(
            _UnavailablePath(),
            headers=headers,
            media_type=opened.presentation.media_type,
            filename=filename,
            stat_result=opened.handle.status,
            content_disposition_type=disposition,
        )

    @override
    async def _handle_simple(self, send: Send, send_header_only: bool, send_pathsend: bool) -> None:
        await send(
            {"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers}
        )
        await self._send_file(send, 0, self._handle.status.st_size if not send_header_only else 0)

    @override
    async def _handle_single_range(
        self, send: Send, start: int, end: int, file_size: int, send_header_only: bool
    ) -> None:
        await self._start_range(
            send,
            {
                b"content-range": f"bytes {start}-{end - 1}/{file_size}".encode("ascii"),
                b"content-length": str(end - start).encode("ascii"),
            },
        )
        await self._send_file(send, start, start if send_header_only else end)

    @override
    async def _handle_multiple_ranges(
        self, send: Send, ranges: list[tuple[int, int]], file_size: int, send_header_only: bool
    ) -> None:
        boundary = token_hex(13)
        size, part_header = self.generate_multipart(
            ranges, boundary, file_size, self.headers["content-type"]
        )
        await self._start_range(
            send,
            {
                b"content-type": f"multipart/byteranges; boundary={boundary}".encode("ascii"),
                b"content-length": str(size).encode("ascii"),
            },
        )
        if send_header_only:
            await self._send_body(send, b"")
            return
        for start, end in ranges:
            await self._send_body(send, part_header(start, end), more=True)
            await self._send_file(send, start, end, more=True)
            await self._send_body(send, b"\r\n", more=True)
        await self._send_body(send, f"--{boundary}--".encode("ascii"))

    async def _start_range(self, send: Send, changes: dict[bytes, bytes]) -> None:
        headers = [(key, value) for key, value in self.raw_headers if key not in changes]
        headers.extend(changes.items())
        await send({"type": "http.response.start", "status": 206, "headers": headers})

    async def _send_file(self, send: Send, start: int, end: int, *, more: bool = False) -> None:
        if start == end and not more:
            await self._send_body(send, b"")
        while start < end:
            chunk = await FILE_PREVIEW_WORKERS.run(
                self._handle.read, start, min(self.chunk_size, end - start)
            )
            if not chunk:
                raise OSError("The capability file was truncated while it was being served")
            start += len(chunk)
            await self._send_body(send, chunk, more=more or start < end)

    @staticmethod
    async def _send_body(send: Send, content: bytes, *, more: bool = False) -> None:
        await send({"type": "http.response.body", "body": content, "more_body": more})


def register_file_routes(app: FastAPIType) -> None:
    @app.get("/api/files/{token}")
    async def get_assistant_file(request: Request, token: str, download: bool = False) -> Response:
        delivery: FileDelivery = request.app.state.file_delivery

        async def prepare(resource: _CapabilityResponse) -> Response:
            opened = await resource.open_file(delivery.open_original, token)
            if opened is None:
                raise HTTPException(status_code=404)
            delivered = opened.presentation
            if delivered.media_type == "text/html" and not download:
                try:
                    preview = await FILE_PREVIEW_WORKERS.run(
                        delivery.preview_for_html, delivered.path
                    )
                    principal = scope_principal(request.scope)
                    preview = delivery.bind_payload(
                        preview,
                        principal.actor if principal else None,
                        expires_at=request.scope.get("vbot.capability_expires_at"),
                    )
                except (ValueError, OSError) as exc:
                    raise HTTPException(status_code=404) from exc
                return RedirectResponse(
                    preview["url"],
                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
                )
            return _HandleFileResponse(
                opened,
                filename=delivered.path.name,
                disposition="inline" if delivered.inline and not download else "attachment",
                headers=delivered.response_headers,
            )

        return _CapabilityResponse(prepare)

    @app.get("/api/preview-assets/{token}/.revision")
    async def get_preview_revision(request: Request, token: str) -> Response:
        delivery: FileDelivery = request.app.state.file_delivery
        try:
            revision = await FILE_PREVIEW_WORKERS.run(delivery.preview_revision, token)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=404) from exc
        return Response(
            json.dumps({"revision": revision}),
            media_type="application/json",
            headers=delivery.preview_headers(str(request.base_url), token),
        )

    @app.api_route("/api/preview-assets/{token}/{asset_path:path}", methods=["GET", "HEAD"])
    async def get_preview_asset(request: Request, token: str, asset_path: str) -> Response:
        delivery: FileDelivery = request.app.state.file_delivery
        base_url = str(request.base_url)

        async def prepare(resource: _CapabilityResponse) -> Response:
            try:
                opened = await resource.open_file(delivery.open_preview_file, token, asset_path)
                delivered = opened.presentation
                headers = delivery.preview_headers(base_url, token)
                if (
                    delivered.path.name == "index.html"
                    and asset_path
                    and not asset_path.endswith("/")
                    and asset_path.rsplit("/", 1)[-1] != "index.html"
                ):
                    return RedirectResponse(
                        str(request.url.replace(path=request.url.path + "/")), headers=headers
                    )
                if delivered.media_type == "text/html":
                    headers["Content-Disposition"] = "inline"
                    content = await FILE_PREVIEW_WORKERS.run(
                        delivery.preview_html,
                        opened.handle,
                        f"{PREVIEW_URL_PREFIX}{token}/.revision",
                    )
                    return Response(content, media_type="text/html", headers=headers)
                return _HandleFileResponse(opened, headers=headers)
            except ValueError, OSError:
                content, headers = await FILE_PREVIEW_WORKERS.run(
                    delivery.preview_unavailable, base_url, token
                )
                return Response(
                    content,
                    status_code=404,
                    media_type="text/html",
                    headers=headers,
                )

        return _CapabilityResponse(prepare)

    @app.api_route("/api/extension-assets/{token}/{asset_path:path}", methods=["GET", "HEAD"])
    async def get_extension_asset(request: Request, token: str, asset_path: str) -> Response:
        delivery: FileDelivery = request.app.state.file_delivery

        async def prepare(resource: _CapabilityResponse) -> Response:
            from core.extensions import ExtensionRegistrationIdentity

            try:
                claims = await FILE_PREVIEW_WORKERS.run(delivery.extension_page_claims, token)
                identity = ExtensionRegistrationIdentity(claims["extension"], claims["epoch"])
                registry = request.app.state.runtime.extensions
                if registry is None or not registry.is_registration_current(identity):
                    raise ValueError("Extension page is unavailable")
                declarations = await FILE_PREVIEW_WORKERS.run(registry.page_declarations)
                current = next(
                    (
                        entry
                        for candidate, declaration, entry in declarations
                        if candidate == identity and declaration.page_id == claims["page"]
                    ),
                    None,
                )
                if current is None or current.parent != Path(claims["root"]):
                    raise ValueError("Extension page is unavailable")
                opened = await resource.open_file(delivery.open_extension_asset, token, asset_path)
                if (
                    request.app.state.runtime.extensions is not registry
                    or not registry.is_registration_current(identity)
                ):
                    raise ValueError("Extension page is unavailable")
                return _HandleFileResponse(
                    opened,
                    headers=delivery.extension_page_headers(str(request.base_url), token),
                )
            except OSError, ValueError, KeyError:
                return Response(status_code=404)

        return _CapabilityResponse(prepare)
