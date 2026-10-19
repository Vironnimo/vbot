"""Long-lived asynchronous transport for the terminal accessor.

No mutation is retried here. A missing response preserves delivery uncertainty;
reconnecting streams only reads state, never repeats the operator's submission.
"""

from __future__ import annotations

import asyncio
import json
import math
import mimetypes
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal, Self, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from cli._server_target import ServerInstance
from cli.server_client import (
    DEVICES_UNAVAILABLE,
    WEBSOCKET_REFUSALS,
    access_refusal,
    http_options,
    websocket_target,
)
from core.utils.http_status import parse_retry_after

type JsonObject = dict[str, Any]

#: HTTP statuses of the server's access refusals.
_REFUSAL_STATUSES = {401: "auth_required", 403: "access_denied"}


class ClientError(Exception):
    """An expected server or transport refusal, with delivery evidence."""

    def __init__(
        self,
        message: str,
        *,
        state: Literal["not_sent", "unknown", "responded"] = "responded",
        code: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.state = state
        self.code = code
        self.retry_after = retry_after


class TerminalClient:
    """The TUI's connection to one server; requests carry the target's credential.

    ``transport`` replaces the network, for tests.
    """

    def __init__(
        self, target: ServerInstance, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.target = target
        self.url = target.url.rstrip("/")
        self.connection_id = str(uuid.uuid4())
        platform = "Windows" if sys.platform == "win32" else "Linux"
        self.user_agent = f"vBot-TUI/1 ({platform})"
        self.http = httpx.AsyncClient(
            base_url=self.url,
            timeout=httpx.Timeout(10, read=30),
            headers={"User-Agent": self.user_agent},
            transport=transport,
            **http_options(target),
        )

    def refusal(self, code: str, *, retry_after: float | None = None) -> ClientError:
        """The error for an access refusal, saying what this target needs."""

        if code == DEVICES_UNAVAILABLE:
            retry_after = max(30.0, retry_after or 0.0)
        return ClientError(
            access_refusal(self.target, code) or code, code=code, retry_after=retry_after
        )

    async def _check_access(self, response: httpx.Response) -> None:
        """Preserve access refusals, including the Device store's retry interval."""
        code = _REFUSAL_STATUSES.get(response.status_code)
        if response.status_code == 503:
            await response.aread()
            try:
                body = response.json()
            except ValueError:
                body = None
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict) and error.get("code") == DEVICES_UNAVAILABLE:
                code = DEVICES_UNAVAILABLE
        if code is not None:
            raise self.refusal(code, retry_after=_retry_after(response))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.http.aclose()

    async def rpc(self, method: str, params: JsonObject | None = None) -> JsonObject:
        try:
            response = await self.http.post(
                "/api/rpc", json={"method": method, "params": params or {}}
            )
        except httpx.RequestError as exc:
            state: Literal["not_sent", "unknown"] = (
                "not_sent"
                if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                else "unknown"
            )
            raise ClientError(
                "Server connection failed. "
                + (
                    "The request was not sent."
                    if state == "not_sent"
                    else "Its outcome is unknown."
                ),
                state=state,
            ) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise ClientError(
                "The server returned no valid result; outcome unknown.", state="unknown"
            ) from exc
        if isinstance(body, dict):
            error = body.get("error")
            if body.get("ok") is False and isinstance(error, dict):
                message, code = error.get("message"), error.get("code")
                if isinstance(message, str) and isinstance(code, str):
                    if response.status_code in _REFUSAL_STATUSES or code == DEVICES_UNAVAILABLE:
                        raise self.refusal(code, retry_after=_retry_after(response))
                    raise ClientError(message, code=code, retry_after=_retry_after(response))
            if (
                response.status_code == 200
                and body.get("ok") is True
                and isinstance(body.get("result"), dict)
            ):
                return cast(JsonObject, body["result"])
        raise ClientError("The server returned no valid result; outcome unknown.", state="unknown")

    def server_path(self, url: str) -> str:
        """Accept a relative delivery URL or an absolute URL of this server only."""
        parsed = urlsplit(url)
        if parsed.scheme or parsed.netloc:
            own = urlsplit(self.url)
            if (parsed.scheme, parsed.netloc) != (own.scheme, own.netloc):
                raise ClientError("This link belongs to another server; open it separately.")
            return parsed.path + (f"?{parsed.query}" if parsed.query else "")
        if not url.startswith("/") or url.startswith("//"):
            raise ClientError("A server-relative delivery URL is required.")
        return url

    async def timeline(self, path: str) -> AsyncIterator[JsonObject]:
        """Each connection begins with an authoritative server snapshot."""
        try:
            async with self.http.stream(
                "GET", self.server_path(path), timeout=httpx.Timeout(10, read=60)
            ) as response:
                await self._check_access(response)
                if response.status_code != 200:
                    raise ClientError(
                        f"Timeline unavailable (HTTP {response.status_code}).",
                        retry_after=_retry_after(response),
                    )
                data: list[str] = []
                name = ""
                async for line in response.aiter_lines():
                    if not line:
                        if data and name != "heartbeat":
                            try:
                                item = json.loads("\n".join(data))
                            except ValueError as exc:
                                raise ClientError(
                                    "The timeline stream returned invalid data."
                                ) from exc
                            if isinstance(item, dict):
                                item.setdefault("type", name)
                                yield item
                        data, name = [], ""
                    elif not line.startswith(":"):
                        field, _, value = line.partition(":")
                        if field == "event":
                            name = value.removeprefix(" ")
                        elif field == "data":
                            data.append(value.removeprefix(" "))
        except httpx.RequestError as exc:
            raise ClientError("Timeline connection lost; the Run can still be working.") from exc

    async def events(self) -> AsyncIterator[JsonObject]:
        """Server events; an access refusal (close 4401/4403/4503) raises its ``ClientError``."""

        query = urlencode({"accessor": "tui", "connection_id": self.connection_id})
        url, options = websocket_target(self.target, f"/ws?{query}")
        async with connect(url, user_agent_header=self.user_agent, **options) as socket:
            try:
                async for raw in socket:
                    if isinstance(raw, str):
                        try:
                            event = json.loads(raw)
                        except ValueError as exc:
                            raise ClientError("The event stream returned invalid data.") from exc
                        if isinstance(event, dict):
                            yield event
            except ConnectionClosed as closed:
                code = closed.rcvd.code if closed.rcvd is not None else None
                if code in WEBSOCKET_REFUSALS:
                    raise self.refusal(WEBSOCKET_REFUSALS[code]) from closed
                raise

    async def upload(self, path: Path) -> JsonObject:
        """Upload a local file. The server owns type and size validation."""
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            with path.open("rb") as source:
                response = await self.http.post(
                    "/api/upload", files={"file": (path.name, source, media_type)}
                )
            await self._check_access(response)
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ClientError("The attachment upload did not return a usable result.") from exc
        if not isinstance(result, dict) or not isinstance(result.get("attachment_id"), str):
            raise ClientError("The attachment upload did not return a usable result.")
        return result

    async def download(self, url: str, destination: Path) -> None:
        """Stream to a newly created local file; never replace an existing file."""
        path = self.server_path(url)
        parsed = urlsplit(path)
        if parsed.path.startswith("/api/files/"):
            query = [
                (key, value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                if key != "download"
            ]
            query.append(("download", "true"))
            path = urlunsplit(("", "", parsed.path, urlencode(query), ""))
        opened = False
        try:
            async with self.http.stream("GET", path) as response:
                await self._check_access(response)
                response.raise_for_status()
                with destination.open("xb") as output:
                    opened = True
                    async for chunk in response.aiter_bytes():
                        await asyncio.to_thread(output.write, chunk)
        except BaseException:
            if opened:
                destination.unlink(missing_ok=True)
            raise


def _retry_after(response: httpx.Response) -> float | None:
    seconds = parse_retry_after(response.headers)
    return seconds if seconds is not None and math.isfinite(seconds) else None
