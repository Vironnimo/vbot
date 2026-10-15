"""Long-lived asynchronous transport for the terminal accessor.

No mutation is retried here. A missing response preserves delivery uncertainty;
reconnecting streams only reads state, never repeats the operator's submission.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal, Self, cast
from urllib.parse import urlencode, urlsplit

import httpx
from websockets.asyncio.client import connect

from core.utils.tls import ssl_context_for

type JsonObject = dict[str, Any]


class ClientError(Exception):
    """An expected server or transport refusal, with delivery evidence."""

    def __init__(
        self,
        message: str,
        *,
        state: Literal["not_sent", "unknown", "responded"] = "responded",
        code: str = "",
    ) -> None:
        super().__init__(message)
        self.state = state
        self.code = code


class TerminalClient:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self.connection_id = str(uuid.uuid4())
        platform = "Windows" if sys.platform == "win32" else "Linux"
        self.user_agent = f"vBot-TUI/1 ({platform})"
        self.http = httpx.AsyncClient(
            base_url=self.url,
            trust_env=False,
            verify=ssl_context_for(self.url),
            timeout=httpx.Timeout(10, read=30),
            headers={"User-Agent": self.user_agent},
        )

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
                    raise ClientError(message, code=code)
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
                if response.status_code != 200:
                    raise ClientError(f"Timeline unavailable (HTTP {response.status_code}).")
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
        query = urlencode({"accessor": "tui", "connection_id": self.connection_id})
        url = self.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        kwargs: dict[str, Any] = {"proxy": None, "user_agent_header": self.user_agent}
        if url.startswith("wss://"):
            kwargs["ssl"] = ssl_context_for(self.url)
        async with connect(f"{url}/ws?{query}", **kwargs) as socket:
            async for raw in socket:
                if isinstance(raw, str):
                    try:
                        event = json.loads(raw)
                    except ValueError as exc:
                        raise ClientError("The event stream returned invalid data.") from exc
                    if isinstance(event, dict):
                        yield event

    async def upload(self, path: Path) -> JsonObject:
        """Upload a local file. The server owns type and size validation."""
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            with path.open("rb") as source:
                response = await self.http.post(
                    "/api/upload", files={"file": (path.name, source, media_type)}
                )
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
        opened = False
        try:
            async with self.http.stream("GET", path) as response:
                response.raise_for_status()
                with destination.open("xb") as output:
                    opened = True
                    async for chunk in response.aiter_bytes():
                        await asyncio.to_thread(output.write, chunk)
        except BaseException:
            if opened:
                destination.unlink(missing_ok=True)
            raise
