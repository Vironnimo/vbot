"""Minimal clients for vBot's public RPC envelope and per-Run SSE stream."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

RPC_PATH = "/api/rpc"


class RpcCallError(RuntimeError):
    """An RPC returned ``{ok: false}`` or an unusable response."""

    def __init__(self, method: str, code: str, message: str) -> None:
        super().__init__(f"{method} failed ({code}): {message}")
        self.method = method
        self.code = code
        self.message = message


class RpcCaller(Protocol):
    """What the harness needs from an RPC client (stubbed in tests)."""

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any: ...


def unwrap_envelope(method: str, status_code: int, text: str) -> Any:
    """Return ``result`` from an RPC response body or raise :class:`RpcCallError`."""
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RpcCallError(method, f"http_{status_code}", text[:200]) from exc
    if not isinstance(envelope, dict):
        raise RpcCallError(method, f"http_{status_code}", "response is not an object")
    if envelope.get("ok") is True:
        return envelope.get("result")
    error = envelope.get("error")
    if isinstance(error, dict):
        raise RpcCallError(
            method,
            str(error.get("code") or f"http_{status_code}"),
            str(error.get("message") or ""),
        )
    raise RpcCallError(method, f"http_{status_code}", text[:200])


class RpcClient:
    """Synchronous RPC client for setup, seeding and recording control."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 60.0) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout_seconds, trust_env=False)

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        response = self._client.post(RPC_PATH, json={"method": method, "params": params or {}})
        return unwrap_envelope(method, response.status_code, response.text)

    def get(self, path: str) -> httpx.Response:
        return self._client.get(path)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RpcClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass(frozen=True)
class SseEvent:
    """One dispatched Server-Sent Event."""

    event: str
    data: str
    event_id: str | None


class SseParser:
    """Incremental line parser for ``text/event-stream`` bodies."""

    def __init__(self) -> None:
        self._event = ""
        self._data: list[str] = []
        self._event_id: str | None = None

    def feed(self, line: str) -> SseEvent | None:
        """Consume one line (without its terminator); return an event on dispatch."""
        if line == "":
            if not self._data and not self._event:
                return None
            event = SseEvent(self._event or "message", "\n".join(self._data), self._event_id)
            self._event, self._data, self._event_id = "", [], None
            return event
        if line.startswith(":"):
            return None
        field_name, _separator, value = line.partition(":")
        value = value.removeprefix(" ")
        if field_name == "event":
            self._event = value
        elif field_name == "data":
            self._data.append(value)
        elif field_name == "id":
            self._event_id = value
        return None


class AsyncRpcClient:
    """Asynchronous client used by the concurrent load driver."""

    def __init__(self, base_url: str, *, max_connections: int) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            trust_env=False,
            timeout=httpx.Timeout(60.0, read=None),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_connections,
            ),
        )

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._client.post(
            RPC_PATH, json={"method": method, "params": params or {}}
        )
        return unwrap_envelope(method, response.status_code, response.text)

    async def events(self, path: str) -> AsyncIterator[SseEvent]:
        """Yield SSE events from ``path`` until the server closes the stream."""
        parser = SseParser()
        async with self._client.stream("GET", path) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode("utf-8", "replace")
                raise RpcCallError(path, f"http_{response.status_code}", body[:200])
            async for line in response.aiter_lines():
                event = parser.feed(line)
                if event is not None:
                    yield event

    async def aclose(self) -> None:
        await self._client.aclose()
