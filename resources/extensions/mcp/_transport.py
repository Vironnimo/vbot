"""Observed MCP transports and the evidence each exchange gives about delivery."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, cast, override

import anyio
import httpx2
import mcp.types as types
from mcp.client.auth import OAuthFlowError
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import MCP_SESSION_ID, streamable_http_client
from mcp.os.win32.utilities import get_windows_executable_command
from mcp.shared.message import SessionMessage

from ._calls import _CURRENT_CALL
from ._events import MissingCredentialError
from ._network import http_client
from ._oauth import ConnectionOAuth

if TYPE_CHECKING:
    from .client import ConnectionRunner


class _ObservedHTTPClient(httpx2.AsyncClient):
    """One connection's HTTP client; tells its runner what each exchange proves.

    The runner learns which requests reached the server and when the server
    ended the connection's session, which the SDK reports only as error text.
    """

    def __init__(self, observer: ConnectionTransport, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._observer = observer

    @override
    async def send(self, request: httpx2.Request, **kwargs: Any) -> httpx2.Response:
        try:
            response = await super().send(request, **kwargs)
        except Exception as error:
            self._observer._http_failed(request, error)
            raise
        await self._observer._http_answered(request, response)
        return response


def _batch_argument_problem(command: str, args: list[str]) -> str | None:
    """Why Windows would not pass *args* unchanged to *command*, or ``None``.

    Windows runs a ``.cmd`` or ``.bat`` file, such as ``npx.cmd``, through
    cmd.exe, which interprets ``& | < > ^`` outside quotes, ends the command at
    a line break, and even inside quotes expands ``%NAME%`` (any two ``%``
    characters may enclose a variable name) and, where delayed expansion is on,
    ``!NAME!`` (a lone ``!`` disappears). Python quotes arguments only for the
    program's own parser (BatBadBut, CVE-2024-24576).
    """
    executable = get_windows_executable_command(command)
    if not executable.lower().endswith((".cmd", ".bat")):
        return None
    quoted = False
    # The argument with a '%' that a later '%' would close into a variable name.
    opened: int | None = None
    for index, argument in enumerate(args, start=1):
        # The command line is each argument's quoted form joined by spaces.
        for character in subprocess.list2cmdline([argument]):
            culprit = index
            if character == '"':
                quoted = not quoted
                continue
            if character == "%":
                if opened is None:
                    opened = index
                    continue
                culprit = opened
            elif character not in "\r\n!" and (quoted or character not in _BATCH_METACHARACTERS):
                continue
            return (
                f"argument {culprit} contains {character!r}, which cmd.exe interprets "
                f"when Windows runs {Path(executable).name}; start the server's program "
                "directly or pass the value through an environment variable"
            )
    return None


class _ObservedReadStream:
    """A connection's read stream that reports its end."""

    def __init__(self, inner: Any, ended: Callable[[], None]) -> None:
        self._inner = inner
        self._ended = ended

    @property
    def last_context(self) -> contextvars.Context | None:
        return cast(contextvars.Context | None, getattr(self._inner, "last_context", None))

    async def receive(self) -> Any:
        try:
            return await self._inner.receive()
        except anyio.EndOfStream, anyio.ClosedResourceError:
            self._ended()
            raise

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Any:
        try:
            return await self._inner.__anext__()
        except StopAsyncIteration, anyio.ClosedResourceError:
            self._ended()
            raise

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def __aenter__(self) -> Self:
        await self._inner.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        return cast(bool | None, await self._inner.__aexit__(exc_type, exc_val, exc_tb))


class _ObservedWriteStream:
    """A connection's write stream that marks delivered requests and reports a closed one."""

    def __init__(self, inner: Any, runner: ConnectionRunner, *, marks_delivery: bool) -> None:
        self._inner = inner
        self._runner = runner
        self._marks_delivery = marks_delivery

    async def send(self, item: SessionMessage) -> None:
        call = _CURRENT_CALL.get()
        marked = (
            self._marks_delivery
            and call is not None
            and call.runner is self._runner
            and isinstance(item.message, types.JSONRPCRequest)
        )
        previous = call.delivered if call is not None else False
        if marked and call is not None:
            # A started write counts: it can hand the request over and still raise.
            call.delivered = True
        try:
            await self._inner.send(item)
        except anyio.BrokenResourceError, anyio.ClosedResourceError:
            if marked and call is not None:
                call.delivered = previous
            self._runner._lose("the connection to the MCP server closed")
            raise

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def __aenter__(self) -> Self:
        await self._inner.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        return cast(bool | None, await self._inner.__aexit__(exc_type, exc_val, exc_tb))


class _ObservedTransport:
    """A transport whose streams report a connection's end and its delivered requests."""

    def __init__(
        self,
        inner: AbstractAsyncContextManager[Any],
        runner: ConnectionRunner,
        *,
        marks_delivery: bool,
    ) -> None:
        self._inner = inner
        self._runner = runner
        self._marks_delivery = marks_delivery

    async def __aenter__(self) -> tuple[_ObservedReadStream, _ObservedWriteStream]:
        read_stream, write_stream = await self._inner.__aenter__()
        return (
            _ObservedReadStream(
                read_stream, lambda: self._runner._lose("the MCP server closed the connection")
            ),
            _ObservedWriteStream(write_stream, self._runner, marks_delivery=self._marks_delivery),
        )

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        return await self._inner.__aexit__(exc_type, exc_val, exc_tb)


def _carries_request(request: httpx2.Request) -> bool:
    """Whether *request* may carry a JSON-RPC request, which the server would process."""
    try:
        message = json.loads(request.content)
    except httpx2.RequestNotRead, ValueError:
        return True
    return not isinstance(message, dict) or ("method" in message and "id" in message)


async def _reports_unknown_method(response: httpx2.Response) -> bool:
    """Whether a 404 answer is a JSON-RPC "method not found" rather than a lost session."""
    if not response.headers.get("content-type", "").lower().startswith("application/json"):
        return False
    try:
        body = json.loads(await response.aread())
    except httpx2.HTTPError, httpx2.StreamError, ValueError:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    return isinstance(error, dict) and error.get("code") == types.METHOD_NOT_FOUND


_UNSENT_HTTP_ERRORS = (
    httpx2.ConnectError,
    httpx2.ConnectTimeout,
    httpx2.PoolTimeout,
    OAuthFlowError,
)


_BATCH_METACHARACTERS = frozenset("&|<>^")


class ConnectionTransport:
    """Open and observe one runner's transport without owning call admission."""

    def __init__(self, runner: ConnectionRunner) -> None:
        self._runner = runner
        self._http_marks_delivery = False

    async def open(self, stack: AsyncExitStack) -> Any:
        if self._runner.config["transport"] == "stdio":
            environment = dict(self._runner.config.get("environment", {}))
            for key, source in self._runner.config.get("credential_environment", {}).items():
                environment[key] = self._credential(source)
            args = self._runner.config.get("args", [])
            if sys.platform == "win32" and (
                problem := _batch_argument_problem(self._runner.config["command"], args)
            ):
                raise ValueError(f"MCP server not started: {problem}")
            parameters = StdioServerParameters(
                command=self._runner.config["command"],
                args=args,
                cwd=self._runner.config.get("cwd"),
                env=environment,
                # A byte sequence that is not UTF-8 must not end the connection.
                encoding_error_handler="replace",
            )
            reader, writer = os.pipe()
            errors = stack.enter_context(os.fdopen(writer, "w", encoding="utf-8"))
            loop = asyncio.get_running_loop()
            thread = threading.Thread(
                target=self._runner._events.drain_stderr, args=(reader, loop), daemon=True
            )
            thread.start()
            return stdio_client(parameters, errlog=errors)
        headers = {
            key: self._credential(source)
            for key, source in self._runner.config.get("credential_headers", {}).items()
        }
        auth = (
            ConnectionOAuth(self._runner.config, self._runner.host, self._runner.inputs).provider()
            if self._runner.config.get("oauth")
            else None
        )
        url = self._runner.config["url"]
        self._http_marks_delivery = True
        observed = functools.partial(_ObservedHTTPClient, self)
        if self._runner.config["transport"] == "sse":

            def sse_http_client(
                headers: dict[str, str] | None = None,
                timeout: httpx2.Timeout | None = None,
                auth: httpx2.Auth | None = None,
            ) -> httpx2.AsyncClient:
                # ``sse_client`` always passes its own timeouts.
                return http_client(
                    url, headers=headers, timeout=timeout, auth=auth, factory=observed
                )

            return sse_client(
                url,
                headers=headers,
                timeout=self._runner.config["timeout"],
                auth=auth,
                httpx_client_factory=sse_http_client,
            )
        http = await stack.enter_async_context(
            http_client(
                url,
                headers=headers,
                auth=auth,
                timeout=httpx2.Timeout(self._runner.config["timeout"], connect=15.0),
                factory=observed,
            )
        )
        return streamable_http_client(url, http_client=http)

    def _credential(self, key: str) -> str:
        value = self._runner.host.resolve_credential(key)
        if not value:
            raise MissingCredentialError(key)
        return value

    def _http_failed(self, request: httpx2.Request, error: Exception) -> None:
        """An HTTP request of this connection failed without a response."""
        if request.method != "POST":
            return
        if not isinstance(error, _UNSENT_HTTP_ERRORS) and _carries_request(request):
            self._runner._mark_delivered()
        # Both SDK transports stop sending after a failed message POST.
        self._runner._lose(self._runner.safe_error(error))

    async def _http_answered(self, request: httpx2.Request, response: httpx2.Response) -> None:
        """An HTTP request of this connection got *response*."""
        status = response.status_code
        if request.method == "DELETE":
            return
        if request.method == "POST" or self._runner.config["transport"] == "sse":
            # A failure diagnosis names this status; a server may refuse the
            # optional GET stream of Streamable HTTP without harm.
            self._runner._events.observe_status(status)
        if (
            status == 404
            and request.headers.get(MCP_SESSION_ID)
            and not await _reports_unknown_method(response)
        ):
            # The server ended the session without processing the request; the
            # client must start a new session (Streamable HTTP, 2025 protocols).
            self._runner._lose("the MCP server ended the session of this connection")
            return
        if request.method != "POST":
            return
        # A redirect or a 4xx refusal leaves the request unprocessed; a 5xx may
        # come after the server began to process it.
        if _carries_request(request):
            if status < 300 or status >= 500:
                self._runner._mark_delivered()
            if status >= 300:
                call = _CURRENT_CALL.get()
                if call is not None and call.runner is self._runner:
                    call.http_status = status
        if self._runner.config["transport"] == "sse" and not 200 <= status < 300:
            # The legacy SSE transport stops sending after a refused message.
            self._runner._lose(f"the MCP server refused a message with HTTP {status}")
