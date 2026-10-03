"""MCP transport, negotiated capabilities, request isolation, and connection lifecycle."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import json
import logging
import os
import random
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Callable, Coroutine, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, suppress
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast, override

import anyio
import httpx2
import mcp.types as types
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.client.auth import OAuthFlowError
from mcp.client.extension import ClaimContext
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import MCP_SESSION_ID, streamable_http_client
from mcp.os.win32.utilities import get_windows_executable_command
from mcp.shared.dispatcher import CallOptions
from mcp.shared.exceptions import MCPDeprecationWarning, MCPError
from mcp.shared.message import SessionMessage
from mcp.shared.subscriptions import SUBSCRIPTION_ID_META_KEY

from core.extensions.operations import ExtensionHost
from core.tools.tools import ToolContext
from core.utils.errors import VBotError

from ._callbacks import ServerRequests
from ._events import ConnectionEvents, MissingCredentialError, dump
from ._network import http_client
from ._oauth import ConnectionOAuth
from ._tasks import (
    TASKS_EXTENSION,
    ExtensionWire,
    LegacyWire,
    TaskEndedError,
    TaskHandle,
    TaskRun,
    TasksExtension,
    TaskState,
    TaskUnknownError,
    TaskWire,
    cancel_quietly,
    drive,
    legacy_state,
)
from .interactions import InputRequests

CONNECTION_QUEUE_LIMIT = 64
# Calls one connection runs at the same time; further calls wait in its queue.
CONNECTION_CONCURRENCY = 8
MAX_READ_RETRIES = 3
READ_RETRY_BASE_SECONDS = 0.5
RETRYABLE_READ_STATUSES = frozenset({429, 500, 502, 503, 504})
READ_OPERATIONS = frozenset(
    {
        "catalog",
        "resources/read",
        "prompts/get",
        "completion/complete",
        "ping",
        "tasks/get",
        "tasks/result",
        "tasks/list",
    }
)
CONNECTION_CLOSE_TIMEOUT_SECONDS = 15
# How long a connection that ends waits for its interrupted calls to stop, so their
# cancellation still reaches the server.
CALL_STOP_SECONDS = 5
# Automatic reconnects after an established connection was lost; later calls reconnect.
MAX_RECONNECTS = 3
# How often a task-backed Tool call resumes its server task after losing its connection.
MAX_TASK_RESUMES = 3
# Catalog change notifications within this window cause one catalog refresh.
CATALOG_REFRESH_DELAY_SECONDS = 0.5
DISCOVERY_PROTOCOL_VERSION = "2026-07-28"
# Characters of an invalid Tool result's validation problem that a caller sees.
RESULT_PROBLEM_CHARACTERS = 300
_LOGGER = logging.getLogger("vbot.extensions.mcp")
# Tests patch this seam instead of the process-wide ``asyncio.sleep``.
_sleep = asyncio.sleep
# A failed sign-in (``OAuthFlowError``) is expected too: SDK 2.2 also raises it when the
# server's OAuth metadata answers with 5xx/429 or names another issuer.
EXPECTED_FAILURES = (
    ValueError,
    OSError,
    TimeoutError,
    MCPError,
    OAuthFlowError,
    httpx2.HTTPError,
    anyio.EndOfStream,
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
)
# HTTP failures that prove the server did not process a request: no connection, or a
# sign-in that failed after the server refused the request.
_UNSENT_HTTP_ERRORS = (
    httpx2.ConnectError,
    httpx2.ConnectTimeout,
    httpx2.PoolTimeout,
    OAuthFlowError,
)
# Characters cmd.exe interprets outside quotes in the command line of a batch file.
_BATCH_METACHARACTERS = frozenset("&|<>^")


OPERATION_MODELS: dict[str, Any] = {
    "tools/call": types.CallToolRequestParams,
    "resources/read": types.ReadResourceRequestParams,
    "prompts/get": types.GetPromptRequestParams,
    "completion/complete": types.CompleteRequestParams,
    "resources/subscribe": types.SubscribeRequestParams,
    "resources/unsubscribe": types.UnsubscribeRequestParams,
    "logging/setLevel": types.SetLevelRequestParams,
    "tasks/get": types.GetTaskRequestParams,
    "tasks/result": types.GetTaskPayloadRequestParams,
    "tasks/cancel": types.CancelTaskRequestParams,
    "tasks/list": types.PaginatedRequestParams,
}


# The explicit task operations. A 2026-07-28 server's Tasks extension defines only
# tasks/get and tasks/cancel; a 2025-11-25 server that declares tasks answers
# tasks/get and tasks/result, and tasks/list and tasks/cancel when it declares them.
TASK_OPERATIONS = frozenset({"tasks/get", "tasks/result", "tasks/list", "tasks/cancel"})


def unsupported_operations(catalog: Mapping[str, Any]) -> frozenset[str]:
    """The protocol operations the server whose *catalog* this is does not offer.

    The negotiated protocol and the server's capabilities in the catalog decide;
    a catalog without them (no connection yet) offers no task operation.
    """
    capabilities = catalog.get("capabilities")
    if not isinstance(capabilities, Mapping):
        return TASK_OPERATIONS
    offered: set[str] = set()
    if str(catalog.get("protocol_version") or "") >= DISCOVERY_PROTOCOL_VERSION:
        extensions = capabilities.get("extensions")
        if isinstance(extensions, Mapping) and TASKS_EXTENSION in extensions:
            offered = {"tasks/get", "tasks/cancel"}
    else:
        tasks = capabilities.get("tasks")
        if isinstance(tasks, Mapping):
            offered = {"tasks/get", "tasks/result"} | {
                f"tasks/{name}" for name in ("list", "cancel") if tasks.get(name) is not None
            }
    return TASK_OPERATIONS - offered


def operation_schema(operation: str) -> dict[str, Any]:
    model = OPERATION_MODELS.get(operation)
    if model is not None:
        return dict(model.model_json_schema(by_alias=True))
    properties = {"after": {"type": "integer", "minimum": 0}} if operation == "events" else {}
    return {"type": "object", "properties": properties, "additionalProperties": False}


class _ObservedHTTPClient(httpx2.AsyncClient):
    """One connection's HTTP client; tells its runner what each exchange proves.

    The runner learns which requests reached the server and when the server
    ended the connection's session, which the SDK reports only as error text.
    """

    def __init__(self, observer: ConnectionRunner, **kwargs: Any) -> None:
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


class InvocationNotSentError(ValueError):
    """An invocation that never reached the server, so it changed nothing there.

    *denied*: vBot refused it (Tool access). *refused*: the HTTP status with which
    the server refused it unprocessed (3xx or 4xx); *message* then describes that
    answer.
    """

    def __init__(self, message: str, *, denied: bool = False, refused: int | None = None) -> None:
        super().__init__(message)
        self.denied = denied
        self.refused = refused


class UnsupportedOperationError(InvocationNotSentError):
    """An operation the server does not offer under the negotiated protocol; never sent."""


class InvalidToolResultError(ValueError):
    """A Tool ran, but its result does not satisfy the output schema the Tool declares.

    *problem* is redacted and bounded; *payload* is the result as received.
    """

    def __init__(self, problem: str, payload: dict[str, Any]) -> None:
        super().__init__(problem)
        self.problem = problem
        self.payload = payload


@dataclass(eq=False)
class _Call:
    """One admitted invocation, from its queue entry until its result is settled."""

    runner: ConnectionRunner
    operation: str
    arguments: dict[str, Any]
    context: ToolContext | None
    result: asyncio.Future[dict[str, Any]]
    # Whether a request of this call may have reached the server.
    delivered: bool
    # The connection task that admitted the call.
    owner: asyncio.Task[None] | None
    task: asyncio.Task[None] | None = None
    # A read that may run again: it failed transiently or lost its connection.
    retry: bool = False
    # The HTTP status of the last answer of 300 or more to a request of this call.
    http_status: int | None = None
    # The server task a task-backed Tool call waits for, kept across a lost connection.
    server_task: TaskRun | None = None
    # Whether this call resumes *server_task* of an earlier call whose connection was lost.
    resuming: bool = False


# The call a task serves; transports carry it into the server requests of that call.
_CURRENT_CALL: contextvars.ContextVar[_Call | None] = contextvars.ContextVar(
    "mcp_current_call", default=None
)


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


class ConnectionRunner:
    """One connection's lifecycle: one task owns each SDK connection from entry to exit.

    Calls run concurrently, up to ``CONNECTION_CONCURRENCY`` per connection, behind
    a bounded queue. A server request inside a call belongs to that call's Agent
    (``_attributed_context``). A connection that ends while calls run settles each
    of them as never sent or as unconfirmed; it never cancels its callers, and a
    mutation is never automatically replayed. A lost connection reconnects, and a
    task-backed Tool call it interrupted resumes its server task there.
    """

    def __init__(
        self,
        config: dict[str, Any],
        host: ExtensionHost,
        inputs: InputRequests,
        publish: Any,
        *,
        authorize: Callable[[ToolContext], None] | None = None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.host = host
        self.inputs = inputs
        self.publish = publish
        self._authorize = authorize
        self.id = config["id"]
        self._on_change = on_change
        self._state = "disconnected"
        self.error: str | None = None
        self.client: Client | None = None
        self.catalog: dict[str, Any] = {}
        # Each connection task gets its own queue, shut down when that connection ends.
        self._queue: asyncio.Queue[_Call] = asyncio.Queue(CONNECTION_QUEUE_LIMIT)
        self._task: asyncio.Task[None] | None = None
        self._calls: set[_Call] = set()
        self._ready = asyncio.Event()
        self._closing = False
        # Why the current connection was lost, once it was; and the task it ended.
        self._loss: str | None = None
        self._lost_owner: asyncio.Task[None] | None = None
        # Whether this connection's transport reports which requests it delivered,
        # and whether its HTTP client does so instead of its write stream.
        self._tracks_delivery = False
        self._http_marks_delivery = False
        self._events = ConnectionEvents(host, lambda: self.config, on_stderr=self._stderr_changed)
        self._requests = ServerRequests(
            self.id,
            host,
            inputs,
            self._events,
            self._attributed_context,
            config=lambda: self.config,
        )
        self._refreshing = asyncio.Lock()
        self._published: str | None = None
        # Background work of the current connection: catalog watch and refresh.
        self._subscriptions: dict[str, asyncio.Task[None]] = {}
        self._resource_watches: dict[str, tuple[asyncio.Task[None], asyncio.Future[None]]] = {}
        # Resources subscribed through this runner; each connection subscribes them again.
        self._resource_subscriptions: set[str] = set()
        # Cancellations of server tasks whose calls stopped waiting.
        self._task_cancels: set[asyncio.Task[None]] = set()
        # Cancellations of kept server tasks, each waiting for a connection to send it.
        self._kept_cancels: set[asyncio.Task[None]] = set()
        self._catalog_stale = False
        self._transport_warned = False
        # The log level an Agent chose with logging/setLevel; until then requests
        # carry none and the server applies its own default.
        self._log_level: str | None = None
        # Protocol features removed in 2026-07-28 that this legacy connection used.
        self._legacy_features: set[str] = set()
        # A failing stretch spans lazy reconnects until one connection comes up.
        self._failures = 0
        self._failing_since: float | None = None
        self._automatic = False
        self._reconnects = 0
        self._reconnect: asyncio.Task[None] | None = None

    @property
    def state(self) -> str:
        """``disconnected``, ``connecting``, ``connected`` or ``failed``."""
        return self._state

    @state.setter
    def state(self, value: str) -> None:
        changed = value != self._state
        self._state = value
        if changed and self._on_change is not None:
            self._on_change()

    def _stderr_changed(self) -> None:
        # A failed connection reports its stderr tail, which can still grow.
        if self._state == "failed" and self._on_change is not None:
            self._on_change()

    def start(self, *, automatic: bool = False) -> None:
        if self._task is not None and not self._task.done():
            return
        self._cancel_reconnect()
        self._closing = False
        self._automatic = automatic
        self._loss = None
        self._ready.clear()
        self.state = "connecting"
        self._queue = asyncio.Queue(CONNECTION_QUEUE_LIMIT)
        self._task = asyncio.create_task(self._run(), name=f"mcp:{self.id}")
        self._task.add_done_callback(self._finished)

    @staticmethod
    def _finished(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def close(self) -> None:
        self._closing = True
        self._cancel_reconnect()
        self._ready.set()
        self._reject_queued()
        # Callers learn the outcome now: SDK teardown below may take long or hang.
        self._interrupt_calls()
        self.inputs.cancel_connection(self.id)
        if self._task is not None and not self._task.done():
            # Cancellation also interrupts handshakes and pending OAuth.
            self._task.cancel()
            # ``asyncio.wait`` bounds the wait even when the task ignores cancellation;
            # ``wait_for`` would keep waiting for the cancelled task to finish.
            done, _ = await asyncio.wait({self._task}, timeout=CONNECTION_CLOSE_TIMEOUT_SECONDS)
            if not done:
                # The closing flag already stops reconnects; the stuck task is left behind
                # so callers can still retire this connection's Tools.
                _LOGGER.warning(
                    "MCP connection did not stop within %ss; abandoning it (connection=%s)",
                    CONNECTION_CLOSE_TIMEOUT_SECONDS,
                    self.id,
                )
        self.state = "disconnected"

    def _reject_queued(self) -> None:
        """Release unsent calls even when SDK teardown cannot finish.

        Shutting the queue down first refuses callers still waiting for room, so
        nothing can join it after this drain. A read refused because its
        connection ended may run again on the next connection.
        """
        self._queue.shutdown()
        while True:
            try:
                call = self._queue.get_nowait()
            except asyncio.QueueShutDown, asyncio.QueueEmpty:
                return
            if not call.result.done():
                call.retry = not self._closing
                call.result.set_exception(
                    InvocationNotSentError(self.error or "MCP connection closed")
                )

    def _interrupt_calls(self) -> None:
        """Settle every running call of this connection and stop its task."""
        for call in tuple(self._calls):
            self._settle_interrupted(call)
            if call.task is not None:
                call.task.cancel()

    def _settle_interrupted(self, call: _Call, *, final: bool = False) -> None:
        """Settle *call*, which its connection's end interrupted, by what reached the server.

        Until the cause of a failed connection is known, the call stays pending
        unless *final*: the end of its connection task settles it with the cause.
        """
        if call.result.done():
            return
        if self._closing:
            call.result.set_exception(
                ValueError("The MCP connection was closed or reconfigured while the call ran")
                if call.delivered
                else InvocationNotSentError(
                    "The MCP connection was closed or reconfigured before the call was sent"
                )
            )
            return
        if call.server_task is not None:
            # The server keeps its task without this connection: the call resumes it.
            call.server_task.kept = True
        reason = self._loss or (self.error if self.state == "failed" else None)
        if reason is None:
            if not final:
                return
            reason = self.error or "the MCP connection ended"
        call.retry = True
        call.result.set_exception(
            ValueError(f"The connection was lost while the call ran: {reason}")
            if call.delivered
            else InvocationNotSentError(f"Connection lost: {reason}")
        )

    def status(self) -> dict[str, Any]:
        failed = self.state == "failed"
        return {
            "id": self.id,
            "state": self.state,
            "error": self.error,
            "problem": self._events.problem if failed else None,
            "stderr_tail": self._events.stderr_tail() if failed else [],
            "protocol_version": self.catalog.get("protocol_version"),
            "capabilities": self.catalog.get("capabilities", {}),
            "counts": {
                name: len(self.catalog.get(name, []))
                for name in ("tools", "resources", "resource_templates", "prompts")
            },
        }

    def events(self, after: int = 0) -> dict[str, Any]:
        """The connection's events after cursor *after*, with the count of dropped ones."""
        return self._events.read(after)

    def redact(self, message: str) -> str:
        """*message* without the credentials of this connection."""
        return self._events.redact(message)

    def safe_error(self, error: BaseException) -> str:
        """*error* as one line naming its type, without the credentials of this connection."""
        return self._events.safe_error(error)

    async def invoke(
        self, operation: str, arguments: dict[str, Any], context: ToolContext | None = None
    ) -> dict[str, Any]:
        """Run *operation* on this connection, reconnecting it when it is not up.

        Raises ``InvocationNotSentError`` when nothing reached the server and
        ``ValueError`` when no result came back. A read that failed transiently or
        lost its connection runs again, at most ``MAX_READ_RETRIES`` times; a Tool
        call whose server task outlived its connection resumes that task.
        """
        for attempt in range(MAX_READ_RETRIES + 1):
            call = await self._submit(operation, arguments, context)
            try:
                return await call.result
            except ValueError as error:
                if call.server_task is not None and call.server_task.kept:
                    return await self._resume(call, error)
                if (
                    not call.retry
                    or operation not in READ_OPERATIONS
                    or attempt == MAX_READ_RETRIES
                    or self._closing
                ):
                    raise
            delay = READ_RETRY_BASE_SECONDS * (2**attempt) * random.uniform(0.5, 1.5)
            self._events.record("read_retry", {"operation": operation, "attempt": attempt + 1})
            await _sleep(delay)
        raise AssertionError("Read retry loop must return or raise")

    async def _resume(self, lost: _Call, error: ValueError) -> dict[str, Any]:
        """Wait on the next connection for the server task of *lost*, whose connection ended.

        A server keeps its task when a connection ends, so the call reads the same
        task again once the connection is back, at most ``MAX_TASK_RESUMES`` times.
        A server that no longer knows the task leaves the outcome unconfirmed. A
        call that stops waiting before it resumed the task cancels it.
        """
        run = lost.server_task
        assert run is not None
        for attempt in range(1, MAX_TASK_RESUMES + 1):
            self._events.record("task_resumed", {"task_id": run.state.task_id, "attempt": attempt})
            try:
                call = await self._submit(lost.operation, lost.arguments, lost.context, run)
            except asyncio.CancelledError:
                self._forget_kept(run)
                raise
            except InvocationNotSentError:
                # No connection came back.
                break
            try:
                return await call.result
            except asyncio.CancelledError:
                if run.kept:
                    # Cancelled before the call resumed the task.
                    self._forget_kept(run)
                raise
            except TaskUnknownError as unknown:
                raise ValueError(
                    f"{error}. After vBot reconnected, the MCP server no longer knew this "
                    f"call's background work: {self.redact(str(unknown))}"
                ) from None
            except InvocationNotSentError:
                # The resume never ran: refused, or its connection ended first.
                if not call.retry:
                    break
            except ValueError as failed:
                if not run.kept:
                    # The server's account of the task, or a failure on the new connection.
                    raise
                error = failed
        self._forget_kept(run)
        raise error

    def _forget_kept(self, run: TaskRun) -> None:
        """Cancel the kept server task of a call that stopped waiting, once a connection is up."""

        async def cancel() -> None:
            # Best effort, like ``cancel_quietly``: the call's outcome is already settled.
            with suppress(ValueError):
                await self.invoke("tasks/cancel", {"taskId": run.state.task_id})

        task = asyncio.create_task(cancel(), name=f"mcp-task-cancel:{self.id}")
        self._kept_cancels.add(task)
        task.add_done_callback(self._kept_cancels.discard)

    async def _submit(
        self,
        operation: str,
        arguments: dict[str, Any],
        context: ToolContext | None,
        resumed: TaskRun | None = None,
    ) -> _Call:
        owner = await self._connected()
        call = _Call(
            self,
            operation,
            arguments,
            context,
            asyncio.get_running_loop().create_future(),
            delivered=not self._tracks_delivery,
            owner=owner,
            server_task=resumed,
            resuming=resumed is not None,
        )
        try:
            # The queue belongs to the admitting connection: once that connection
            # ends, a call still waiting for room is refused, never sent later.
            await self._queue.put(call)
        except asyncio.QueueShutDown:
            call.retry = not self._closing
            call.result.set_exception(InvocationNotSentError(self.error or "MCP connection closed"))
        return call

    async def _connected(self) -> asyncio.Task[None]:
        """Wait until this connection is up, starting it when it is not running.

        The wait is bounded by the configured timeout. A connection lost while a
        call waits is replaced once: the call waits for its end and reconnects.
        """
        deadline = asyncio.get_running_loop().time() + self.config["timeout"]
        for _ in range(2):
            if self._closing:
                raise InvocationNotSentError("MCP connection is closing")
            if self._task is None or self._task.done():
                self.start()
            owner = self._task
            assert owner is not None
            try:
                async with asyncio.timeout_at(deadline):
                    await self._ready.wait()
            except TimeoutError:
                raise InvocationNotSentError(self._not_connected()) from None
            if self._closing:
                raise InvocationNotSentError("MCP connection is closing")
            if owner is self._task and not owner.done() and self.state == "connected":
                return owner
            if not (owner.done() and self._lost_owner is owner):
                break
        raise InvocationNotSentError(self.error or "MCP connection did not become ready")

    def _not_connected(self) -> str:
        """Why a call stopped waiting for the connection."""
        waited = f"MCP connection did not connect within {self.config['timeout']:g} seconds"
        if any(
            item["connection"] == self.id and item["kind"] == "oauth" for item in self.inputs.list()
        ):
            return f"{waited}: its sign-in is waiting for the user"
        return f"{waited}; last error: {self.error}" if self.error else waited

    async def _run(self) -> None:
        established = False
        try:
            async with AsyncExitStack() as stack:
                self._http_marks_delivery = False
                transport = await self._transport(stack)
                # A transport the SDK enters itself; an in-process server has no streams.
                self._tracks_delivery = isinstance(transport, AbstractAsyncContextManager)
                if self._tracks_delivery:
                    transport = _ObservedTransport(
                        transport, self, marks_delivery=not self._http_marks_delivery
                    )
                # A capability the connection's policy turns off is not offered at all.
                sampling = self.config.get("sampling", "off") != "off"
                client = Client(
                    transport,
                    read_timeout_seconds=self.config["timeout"],
                    mode="legacy" if self.config["transport"] == "sse" else "auto",
                    sampling_callback=self._requests.sample if sampling else None,
                    sampling_capabilities=(
                        types.SamplingCapability(tools=types.SamplingToolsCapability())
                        if sampling
                        else None
                    ),
                    elicitation_callback=self._requests.elicit,
                    list_roots_callback=self._requests.roots if self._offers_roots() else None,
                    logging_callback=self._requests.log,
                    message_handler=self._message,
                    client_info=types.Implementation(name="vbot", version="1"),
                    # No response cache: every listing and read reaches the server (see the
                    # MCP domain map, Compatibility boundary).
                    cache=None,
                    # Offered on 2026-07-28 connections only: legacy tasks need no declaration.
                    extensions=[TasksExtension(self._resolve_task)],
                )
                self.client = await stack.enter_async_context(client)
                self._check_results(self.client)
                self._transport_warned = False
                await self._refresh()
                self.state = "connected"
                self.error = None
                self._reconnects = 0
                self._log_connected()
                established = True
                self._ready.set()
                self._subscriptions["catalog"] = asyncio.create_task(
                    self._watch_catalog(), name=f"mcp-catalog-watch:{self.id}"
                )
                # A server forgets subscriptions with its session; a failure is a
                # ``subscription_failed`` event and the next connection tries again.
                for uri in self._resource_subscriptions:
                    self._watch(uri)
                try:
                    await self._serve()
                finally:
                    await self._stop_work()
        except asyncio.CancelledError:
            raise
        except (Exception, BaseExceptionGroup) as error:
            self._events.diagnose(error)
            expected = self._expected(error)
            if self._loss is None:
                self.state = "failed"
                self.error = self.safe_error(error)
                self._events.record("connection_failed", {"error": self.error})
                self._log_failed(expected=expected)
            elif not expected:
                _LOGGER.error(
                    "MCP connection crashed (connection=%s): %s", self.id, self.safe_error(error)
                )
            if not expected:
                raise
        finally:
            self.client = None
            self._ready.set()
            self._reject_queued()
            for call in tuple(self._calls):
                self._settle_interrupted(call, final=True)
                if call.task is None or call.task.done():
                    self._calls.discard(call)
            current = asyncio.current_task()
            if (
                not self._closing
                and (established or self._automatic)
                and (current is None or not current.cancelling())
            ):
                self._schedule_reconnect()

    async def _stop_work(self) -> None:
        """Stop the calls and background work of a connection that is ending.

        Its calls are settled first; then their tasks get a short time to stop
        while the transport still carries their cancellation to the server.
        """
        self._reject_queued()
        self._interrupt_calls()
        tasks: set[asyncio.Task[Any]] = {call.task for call in self._calls if call.task is not None}
        background = [
            *self._subscriptions.values(),
            *(task for task, _ in self._resource_watches.values()),
        ]
        self._subscriptions.clear()
        self._resource_watches.clear()
        for task in background:
            task.cancel()
        tasks.update(background)
        deadline = asyncio.get_running_loop().time() + CALL_STOP_SECONDS
        if tasks:
            await asyncio.wait(tasks, timeout=CALL_STOP_SECONDS)
        # Stopped calls ask the server to cancel their tasks meanwhile; the
        # transport ends after this, so later cancellations could not be sent.
        if self._task_cancels:
            remaining = deadline - asyncio.get_running_loop().time()
            await asyncio.wait(set(self._task_cancels), timeout=max(0.0, remaining))
            for task in self._task_cancels:
                task.cancel()

    def _lose(self, reason: str) -> None:
        """End the current connection, which can no longer serve calls, because of *reason*."""
        owner = self._task
        if (
            self._closing
            or self._loss is not None
            or self.state != "connected"
            or owner is None
            or owner.done()
        ):
            return
        self._loss = reason
        self._lost_owner = owner
        self.state = "failed"
        self.error = f"Connection lost: {reason}"
        # An earlier failure's cause no longer applies; the error that ends
        # the connection's task, if any, is diagnosed next.
        self._events.diagnose(None)
        self._ready.clear()
        self._events.record("connection_failed", {"error": self.error})
        self._log_failed(expected=True)
        self._reject_queued()
        self._interrupt_calls()

    def _schedule_reconnect(self) -> None:
        if self._reconnects >= MAX_RECONNECTS:
            return
        delay = READ_RETRY_BASE_SECONDS * (2**self._reconnects) * random.uniform(0.5, 1.5)
        self._reconnects += 1
        self._reconnect = asyncio.create_task(
            self._reconnect_after(delay), name=f"mcp-reconnect:{self.id}"
        )

    async def _reconnect_after(self, delay: float) -> None:
        await _sleep(delay)
        self._reconnect = None
        if not self._closing:
            self.start(automatic=True)

    def _cancel_reconnect(self) -> None:
        if self._reconnect is not None and self._reconnect is not asyncio.current_task():
            self._reconnect.cancel()
        self._reconnect = None

    def _log_connected(self) -> None:
        """Log a connection that came up: a start, or the end of a failing stretch."""
        tools = len(self.catalog.get("tools", []))
        if self._failing_since is None:
            _LOGGER.info("MCP connection established (connection=%s tools=%d)", self.id, tools)
            return
        _LOGGER.info(
            "MCP connection recovered (connection=%s tools=%d failures=%d down_for=%.1fs)",
            self.id,
            tools,
            self._failures,
            time.monotonic() - self._failing_since,
        )
        self._failures = 0
        self._failing_since = None

    def _log_failed(self, *, expected: bool) -> None:
        """Log an expected failure once per failing stretch; a crash always logs."""
        self._failures += 1
        repeated = self._failing_since is not None
        if not repeated:
            self._failing_since = time.monotonic()
        if not expected:
            _LOGGER.error("MCP connection crashed (connection=%s): %s", self.id, self.error)
        elif repeated:
            _LOGGER.debug(
                "MCP connection failed again (connection=%s failures=%d): %s",
                self.id,
                self._failures,
                self.error,
            )
        else:
            _LOGGER.warning("MCP connection failed (connection=%s): %s", self.id, self.error)

    async def _transport(self, stack: AsyncExitStack) -> Any:
        if self.config["transport"] == "stdio":
            environment = dict(self.config.get("environment", {}))
            for key, source in self.config.get("credential_environment", {}).items():
                environment[key] = self._credential(source)
            args = self.config.get("args", [])
            if sys.platform == "win32" and (
                problem := _batch_argument_problem(self.config["command"], args)
            ):
                raise ValueError(f"MCP server not started: {problem}")
            parameters = StdioServerParameters(
                command=self.config["command"],
                args=args,
                cwd=self.config.get("cwd"),
                env=environment,
                # A byte sequence that is not UTF-8 must not end the connection.
                encoding_error_handler="replace",
            )
            reader, writer = os.pipe()
            errors = stack.enter_context(os.fdopen(writer, "w", encoding="utf-8"))
            loop = asyncio.get_running_loop()
            thread = threading.Thread(
                target=self._events.drain_stderr, args=(reader, loop), daemon=True
            )
            thread.start()
            return stdio_client(parameters, errlog=errors)
        headers = {
            key: self._credential(source)
            for key, source in self.config.get("credential_headers", {}).items()
        }
        auth = (
            ConnectionOAuth(self.config, self.host, self.inputs).provider()
            if self.config.get("oauth")
            else None
        )
        url = self.config["url"]
        self._http_marks_delivery = True
        observed = functools.partial(_ObservedHTTPClient, self)
        if self.config["transport"] == "sse":

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
                timeout=self.config["timeout"],
                auth=auth,
                httpx_client_factory=sse_http_client,
            )
        http = await stack.enter_async_context(
            http_client(
                url,
                headers=headers,
                auth=auth,
                timeout=httpx2.Timeout(self.config["timeout"], connect=15.0),
                factory=observed,
            )
        )
        return streamable_http_client(url, http_client=http)

    def _credential(self, key: str) -> str:
        value = self.host.resolve_credential(key)
        if not value:
            raise MissingCredentialError(key)
        return value

    def _http_failed(self, request: httpx2.Request, error: Exception) -> None:
        """An HTTP request of this connection failed without a response."""
        if request.method != "POST":
            return
        if not isinstance(error, _UNSENT_HTTP_ERRORS) and _carries_request(request):
            self._mark_delivered()
        # Both SDK transports stop sending after a failed message POST.
        self._lose(self.safe_error(error))

    async def _http_answered(self, request: httpx2.Request, response: httpx2.Response) -> None:
        """An HTTP request of this connection got *response*."""
        status = response.status_code
        if request.method == "DELETE":
            return
        if request.method == "POST" or self.config["transport"] == "sse":
            # A failure diagnosis names this status; a server may refuse the
            # optional GET stream of Streamable HTTP without harm.
            self._events.observe_status(status)
        if (
            status == 404
            and request.headers.get(MCP_SESSION_ID)
            and not await _reports_unknown_method(response)
        ):
            # The server ended the session without processing the request; the
            # client must start a new session (Streamable HTTP, 2025 protocols).
            self._lose("the MCP server ended the session of this connection")
            return
        if request.method != "POST":
            return
        # A redirect or a 4xx refusal leaves the request unprocessed; a 5xx may
        # come after the server began to process it.
        if _carries_request(request):
            if status < 300 or status >= 500:
                self._mark_delivered()
            if status >= 300:
                call = _CURRENT_CALL.get()
                if call is not None and call.runner is self:
                    call.http_status = status
        if self.config["transport"] == "sse" and not 200 <= status < 300:
            # The legacy SSE transport stops sending after a refused message.
            self._lose(f"the MCP server refused a message with HTTP {status}")

    def _mark_delivered(self) -> None:
        call = _CURRENT_CALL.get()
        if call is not None and call.runner is self:
            call.delivered = True

    async def _serve(self) -> None:
        slots = asyncio.Semaphore(CONNECTION_CONCURRENCY)
        while not self._closing:
            await slots.acquire()
            try:
                call = await self._queue.get()
            except asyncio.QueueShutDown:
                slots.release()
                return
            if call.result.done():
                # Its caller stopped waiting while it was queued.
                slots.release()
                continue
            if call.context is not None and self._authorize is not None:
                try:
                    self._authorize(call.context)
                except (ValueError, VBotError) as error:
                    call.result.set_exception(InvocationNotSentError(str(error), denied=True))
                    slots.release()
                    continue
            self._start(call, slots)

    def _start(self, call: _Call, slots: asyncio.Semaphore) -> None:
        task = asyncio.create_task(
            self._run_call(call), name=f"mcp-call:{self.id}:{call.operation}"
        )
        call.task = task
        self._calls.add(call)

        def cancel(result: asyncio.Future[dict[str, Any]]) -> None:
            # Its caller was cancelled: cancel the request so the server stops too.
            if result.cancelled():
                task.cancel()

        def finished(_: asyncio.Task[None]) -> None:
            slots.release()
            owner = call.owner
            if not call.result.done() and owner is not None and not owner.done():
                # Its connection failed without a known cause yet; the end of the
                # connection task settles it.
                return
            self._settle_interrupted(call, final=True)
            self._calls.discard(call)
            if not self._calls and owner is self._task:
                # Inputs a finished call's server still waits for have no one to answer.
                self.inputs.cancel_connection(self.id)

        call.result.add_done_callback(cancel)
        task.add_done_callback(finished)

    async def _run_call(self, call: _Call) -> None:
        # Each task runs in its own context copy: this marks only this call.
        _CURRENT_CALL.set(call)
        try:
            if call.resuming:
                value = await self._resume_task(call)
            else:
                value = await self._perform(call.operation, call.arguments, call.context)
        except asyncio.CancelledError:
            # By its caller (its result is cancelled), or by its connection's end.
            self._settle_interrupted(call)
            raise
        except Exception as error:
            self._fail(call, error)
        else:
            if not call.result.done():
                call.result.set_result(value)
        if call.context is not None and self._serving(call):
            try:
                # The server reads the roots again: none belong to it after the call.
                await self._notify_roots_changed()
            except EXPECTED_FAILURES as error:
                self._events.record("request_failed", {"operation": "roots", "error": str(error)})

    def _serving(self, call: _Call) -> bool:
        return (
            not self._closing
            and self._loss is None
            and self.state == "connected"
            and call.owner is self._task
        )

    def _fail(self, call: _Call, error: Exception) -> None:
        safe = self.safe_error(error)
        self._events.record("request_failed", {"operation": call.operation, "error": safe})
        if call.result.done():
            return
        if isinstance(
            error,
            InvalidToolResultError | TaskEndedError | TaskUnknownError | UnsupportedOperationError,
        ):
            # The server's own account of the call, or vBot's refusal to send it,
            # whatever happens to the connection.
            call.result.set_exception(error)
            return
        owner = call.owner
        if (
            self._closing
            or self._loss is not None
            or owner is None
            or owner.done()
            or owner.cancelling()
        ):
            # Its connection is ending: the outcome depends on what reached the server.
            self._settle_interrupted(call)
            return
        if not self._expected(error):
            _LOGGER.error(
                "MCP call failed unexpectedly (connection=%s operation=%s): %s",
                self.id,
                call.operation,
                safe,
                exc_info=error,
            )
        status = call.http_status
        call.retry = (
            isinstance(error, OSError | TimeoutError)
            or status in RETRYABLE_READ_STATUSES
            or (
                isinstance(error, httpx2.HTTPError)
                and (
                    not isinstance(error, httpx2.HTTPStatusError)
                    or error.response.status_code in RETRYABLE_READ_STATUSES
                )
            )
        )
        if status is not None and status < 500 and not call.delivered:
            # Every request of the call was redirected or refused, none processed.
            call.result.set_exception(
                InvocationNotSentError(self.redact(_refusal(status, error)), refused=status)
            )
            return
        call.result.set_exception(ValueError(safe))

    def _attributed_context(self) -> ToolContext | None:
        """The Agent call a server request belongs to, or ``None`` for no Agent.

        A request the transport carries inside a call (current-protocol input
        requests, Streamable HTTP responses of that call) belongs to that call;
        otherwise to the only call in flight; otherwise to none.
        """
        call = _CURRENT_CALL.get()
        if call is not None and call.runner is self:
            return call.context if call in self._calls else None
        if len(self._calls) == 1:
            return next(iter(self._calls)).context
        return None

    async def _all(
        self, method: Any, field: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Every item of one catalog list, and each page's own metadata."""
        cursor = None
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        while True:
            page = await method(cursor=cursor)
            # The page's own metadata; its items are kept once, in the catalog's list.
            pages.append(
                page.model_dump(mode="json", by_alias=True, exclude_none=True, exclude={field})
            )
            items.extend(dump(item) for item in getattr(page, field))
            cursor = page.next_cursor
            if cursor is None:
                return items, pages
            if cursor in seen:
                raise ValueError("MCP server repeated a pagination cursor")
            seen.add(cursor)

    async def _refresh(self) -> dict[str, Any]:
        """List the catalog again; publish it when it changed."""
        async with self._refreshing:
            client = self._client()
            capabilities = client.server_capabilities
            catalog: dict[str, Any] = {
                "protocol_version": client.protocol_version,
                "capabilities": dump(capabilities),
                "server_info": dump(client.server_info),
                "instructions": client.instructions,
                "tools": [],
                "resources": [],
                "resource_templates": [],
                "prompts": [],
            }
            pages: dict[str, list[dict[str, Any]]] = {}
            for capability, method, field in (
                (capabilities.tools, client.list_tools, "tools"),
                (capabilities.resources, client.list_resources, "resources"),
                (capabilities.resources, client.list_resource_templates, "resource_templates"),
                (capabilities.prompts, client.list_prompts, "prompts"),
            ):
                if capability is not None:
                    catalog[field], pages[field] = await self._all(method, field)
            catalog["pages"] = pages
            fingerprint = hashlib.sha256(
                json.dumps(catalog, sort_keys=True, default=str).encode()
            ).hexdigest()
            if fingerprint != self._published:
                self.publish(self, catalog)
                self._published = fingerprint
            self.catalog = catalog
            return catalog

    def _client(self) -> Client:
        if self.client is None:
            raise ValueError("MCP connection is not connected")
        return self.client

    def _check_results(self, client: Client) -> None:
        """Make a Tool result that fails its output schema a failure of that call alone.

        SDK 2.2.0 validates each Tool result against the Tool's output schema
        inside ``call_tool`` and raises a plain ``RuntimeError`` when the result
        or the schema is invalid (an unresolvable ``$ref`` too). That would read
        as a crash; this session-level seam turns it into ``InvalidToolResultError``
        carrying the redacted problem and the result as received.
        """
        session = client.session
        validate = session.validate_tool_result

        async def checked(name: str, result: types.CallToolResult) -> None:
            try:
                await validate(name, result)
            except RuntimeError as error:
                if type(error) is not RuntimeError:
                    raise
                problem = self.redact(str(error).split("\n", 1)[0])
                raise InvalidToolResultError(
                    problem[:RESULT_PROBLEM_CHARACTERS], dump(result)
                ) from error

        session.validate_tool_result = checked  # type: ignore[method-assign]

    async def _perform(
        self, operation: str, arguments: dict[str, Any], context: ToolContext | None
    ) -> dict[str, Any]:
        errors = list(Draft202012Validator(operation_schema(operation)).iter_errors(arguments))
        if errors:
            paths = ["/".join(map(str, error.absolute_path)) or "arguments" for error in errors]
            raise ValueError(f"Invalid MCP operation arguments at: {', '.join(paths)}")
        if operation in unsupported_operations(self.catalog):
            raise UnsupportedOperationError(
                f"{operation} is not supported by this connection's protocol"
            )
        client = self._client()
        if context is not None:
            await self._notify_roots_changed()
        if operation == "catalog":
            return await self._refresh()
        if operation == "tools/call":
            tool: dict[str, Any] = next(
                (
                    item
                    for item in self.catalog.get("tools", [])
                    if item["name"] == arguments["name"]
                ),
                {},
            )
            # From 2026-07-28 the server decides; the Tasks extension's claim drives its task.
            if client.protocol_version < DISCOVERY_PROTOCOL_VERSION and (
                tool.get("execution", {}).get("taskSupport") == "required" or "task" in arguments
            ):
                return await self._legacy_task_call(arguments)
            return dump(
                await client.call_tool(
                    arguments["name"],
                    arguments.get("arguments", {}),
                    progress_callback=self._progress,
                    meta=self._request_meta(),
                )
            )
        if operation == "resources/read":
            return dump(await client.read_resource(arguments["uri"], meta=self._request_meta()))
        if operation == "prompts/get":
            return dump(
                await client.get_prompt(
                    arguments["name"], arguments.get("arguments"), meta=self._request_meta()
                )
            )
        if operation == "completion/complete":
            reference = arguments["ref"]
            model = (
                types.PromptReference
                if reference["type"] == "ref/prompt"
                else types.ResourceTemplateReference
            )
            return dump(
                await client.complete(
                    model.model_validate(reference),
                    arguments["argument"],
                    arguments.get("context", {}).get("arguments"),
                )
            )
        if operation == "resources/subscribe":
            uri = arguments["uri"]
            # Concurrent subscribers share one acknowledgement; one that is
            # cancelled leaves it to the others.
            await asyncio.shield(self._watch(uri))
            self._resource_subscriptions.add(uri)
            return {"subscribed": uri}
        if operation == "resources/unsubscribe":
            uri = arguments["uri"]
            self._resource_subscriptions.discard(uri)
            watch = self._resource_watches.pop(uri, None)
            if watch is not None:
                watch[0].cancel()
                await asyncio.gather(watch[0], return_exceptions=True)
                subscribed = (
                    watch[1].done() and not watch[1].cancelled() and not watch[1].exception()
                )
                if subscribed and client.protocol_version < DISCOVERY_PROTOCOL_VERSION:
                    await self._legacy(
                        "resource_subscriptions", lambda: client.unsubscribe_resource(uri)
                    )
            return {"unsubscribed": uri}
        if operation == "logging/setLevel":
            level = arguments["level"]
            types.LoggingMessageNotificationParams.model_validate({"level": level, "data": None})
            self._log_level = level
            if client.protocol_version >= DISCOVERY_PROTOCOL_VERSION:
                return {"level": level, "scope": "subsequent_requests"}
            return dump(await self._legacy("logging", lambda: client.set_logging_level(level)))
        if operation == "ping":
            if client.protocol_version >= DISCOVERY_PROTOCOL_VERSION:
                await self._refresh()
                return {
                    "verified": "catalog",
                    "reason": "ping is not defined in the negotiated protocol",
                }
            return dump(await self._legacy("ping", client.send_ping))
        if operation == "events":
            return self.events(arguments.get("after", 0))
        if operation.startswith("tasks/"):
            return await self._task_request(operation, arguments)
        raise ValueError(f"Unknown MCP operation: {operation}")

    def _request_meta(self) -> types.RequestParamsMeta | None:
        if self._log_level is None or self._client().protocol_version < DISCOVERY_PROTOCOL_VERSION:
            return None
        return cast(types.RequestParamsMeta, {types.LOG_LEVEL_META_KEY: self._log_level})

    async def _task_request(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        request_types: dict[str, Any] = {
            "tasks/get": types.GetTaskRequest,
            "tasks/result": types.GetTaskPayloadRequest,
            "tasks/list": types.ListTasksRequest,
            "tasks/cancel": types.CancelTaskRequest,
        }
        request_type = request_types.get(operation)
        if request_type is None:
            raise ValueError("Unknown MCP task operation")
        request_type.model_validate({"method": operation, "params": arguments})
        client = self._client()
        if client.protocol_version >= DISCOVERY_PROTOCOL_VERSION and operation in {
            "tasks/get",
            "tasks/cancel",
        }:
            return await ExtensionWire(client.session).request(operation, arguments["taskId"])
        return await self._task_send(operation, arguments)

    async def _legacy_task_call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run a 2025-11-25 Tool call as a task and return the task's Tool result."""
        started = await self._task_send(
            "tools/call", {**arguments, "task": arguments.get("task", {})}
        )
        task = types.CreateTaskResult.model_validate(started).task
        session = self._client().session
        run = TaskRun(legacy_state(task), legacy=True)
        result = await self._drive_task(LegacyWire(session), run)
        if not result.is_error:
            # As ``call_tool`` does for a direct result.
            await session.validate_tool_result(arguments["name"], result)
        return dump(result)

    async def _resolve_task(
        self, handle: TaskHandle, context: ClaimContext
    ) -> types.CallToolResult:
        """Finish a task a 2026-07-28 server returned for a Tool call (Tasks extension)."""
        run = TaskRun(handle.state(), legacy=False)
        return await self._drive_task(ExtensionWire(context.session), run)

    async def _resume_task(self, call: _Call) -> dict[str, Any]:
        """Continue on this connection the server task of a call whose connection was lost."""
        run = call.server_task
        assert run is not None
        client = self._client()
        if (client.protocol_version < DISCOVERY_PROTOCOL_VERSION) != run.legacy:
            raise TaskUnknownError(f"it now speaks MCP protocol {client.protocol_version}")
        if call.context is not None:
            await self._notify_roots_changed()
        session = client.session
        wire: TaskWire = LegacyWire(session) if run.legacy else ExtensionWire(session)
        result = await self._drive_task(wire, run, resumed=True)
        if not result.is_error:
            # As ``call_tool`` does for a direct result.
            await session.validate_tool_result(call.arguments["name"], result)
        return dump(result)

    async def _drive_task(
        self, wire: TaskWire, run: TaskRun, *, resumed: bool = False
    ) -> types.CallToolResult:
        """Run a server task to its end; each status change becomes a ``task_status`` event.

        The task is kept with its call, so a lost connection does not end the wait;
        *resumed* continues the task of a call whose connection was lost.
        """
        call = _CURRENT_CALL.get()
        if call is not None and call.runner is self:
            call.server_task = run
        last: tuple[str, str | None] | None = None

        def observe(state: TaskState) -> None:
            nonlocal last
            if (state.status, state.message) != last:
                last = (state.status, state.message)
                self._events.record(
                    "task_status",
                    {"task_id": state.task_id, "status": state.status, "message": state.message},
                )

        abandon = functools.partial(self._abandon_task, wire)
        return await drive(wire, run, observe=observe, abandon=abandon, resumed=resumed)

    def _abandon_task(self, wire: TaskWire, task_id: str) -> None:
        """Cancel the server task of a call that stopped waiting, without blocking it."""

        async def cancel() -> None:
            # Part of no call: the call that started the task has ended.
            _CURRENT_CALL.set(None)
            await cancel_quietly(wire, task_id)

        task = asyncio.create_task(cancel(), name=f"mcp-task-cancel:{self.id}")
        self._task_cancels.add(task)
        task.add_done_callback(self._task_cancels.discard)

    async def _task_send(self, method: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # The pinned SDK's (2.2.0) typed send_request validates historical task handles
        # as CallToolResult and rejects them. Its pinned dispatcher retains the same
        # transport, cancellation and progress semantics without that wrong schema.
        # Only the 2025-11-25 call that starts a task and the explicit task operations
        # use it; ``_tasks`` drives tasks through public SDK requests. Keep this one
        # compatibility seam covered by the real stdio task test.
        session = self._client().session
        options: CallOptions = {"timeout": self.config["timeout"], "on_progress": self._progress}
        session._stamp({"method": method, "params": arguments}, options)
        return dict(await session._dispatcher.send_raw_request(method, arguments, options))

    def _offers_roots(self) -> bool:
        return self.config.get("roots") == "workspace"

    async def _notify_roots_changed(self) -> None:
        client = self._client()
        if self._offers_roots() and client.protocol_version < DISCOVERY_PROTOCOL_VERSION:
            await self._legacy("roots", client.send_roots_list_changed)

    async def _legacy[Result](
        self, feature: str, call: Callable[[], Coroutine[Any, Any, Result]]
    ) -> Result:
        """Run an SDK call that a server older than protocol 2026-07-28 still needs.

        The SDK flags such calls with ``MCPDeprecationWarning`` (SEP-2577). A
        legacy connection depends on them, so the connection logs each feature
        once instead. The warnings filter is process-wide and the SDK warns only
        while the call starts, so just that synchronous start runs inside it.
        """
        if feature not in self._legacy_features:
            self._legacy_features.add(feature)
            _LOGGER.info(
                "MCP connection uses a feature deprecated since protocol %s "
                "(connection=%s feature=%s protocol=%s)",
                DISCOVERY_PROTOCOL_VERSION,
                self.id,
                feature,
                self._client().protocol_version,
            )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", MCPDeprecationWarning)
            started = asyncio.create_task(call(), eager_start=True)
        return await started

    async def _message(self, message: Any) -> None:
        """Record each server notification once, as its own kind of event."""
        if isinstance(message, types.ServerNotification):
            if _recorded_elsewhere(message):
                return
            if isinstance(message, types.ResourceUpdatedNotification):
                # A legacy subscription's change.
                uri = str(message.params.uri)
                self._events.record("resource_changed", {"uri": uri, "event": dump(message)})
            elif isinstance(
                message,
                types.ToolListChangedNotification
                | types.ResourceListChangedNotification
                | types.PromptListChangedNotification,
            ):
                self._events.record("catalog_changed", dump(message))
                self._catalog_changed()
            else:
                self._events.record("notification", dump(message))
        elif isinstance(message, Exception):
            # A transport error that leaves the connection up, such as a line that
            # is not JSON on stdout; a transport that ends also closes its stream.
            error = self.safe_error(message)
            self._events.record("transport_error", {"error": error})
            if self._transport_warned:
                _LOGGER.debug("MCP transport error (connection=%s): %s", self.id, error)
            else:
                self._transport_warned = True
                _LOGGER.warning("MCP transport error (connection=%s): %s", self.id, error)

    def _catalog_changed(self) -> None:
        """Refresh the catalog soon after a legacy server reported a change.

        A current-protocol connection learns changes through its catalog watch.
        """
        client = self.client
        if (
            client is None
            or self.state != "connected"
            or client.protocol_version >= DISCOVERY_PROTOCOL_VERSION
        ):
            return
        self._catalog_stale = True
        refresh = self._subscriptions.get("catalog-refresh")
        if refresh is None or refresh.done():
            self._subscriptions["catalog-refresh"] = asyncio.create_task(
                self._refresh_catalog(), name=f"mcp-catalog-refresh:{self.id}"
            )

    async def _refresh_catalog(self) -> None:
        # A notification inside a call may have started this task; it is no part of that call.
        _CURRENT_CALL.set(None)
        while self._catalog_stale:
            self._catalog_stale = False
            # Changes reported meanwhile join this refresh.
            await _sleep(CATALOG_REFRESH_DELAY_SECONDS)
            try:
                await self.invoke("catalog", {})
            except ValueError as error:
                self._events.record("subscription_failed", {"error": str(error)})
                return

    async def _progress(self, progress: float, total: float | None, message: str | None) -> None:
        self._events.record("progress", {"progress": progress, "total": total, "message": message})

    async def _watch_catalog(self) -> None:
        capabilities = self._client().server_capabilities
        flags = {
            f"{name}_list_changed": bool(value is not None and value.list_changed)
            for name, value in (
                ("tools", capabilities.tools),
                ("resources", capabilities.resources),
                ("prompts", capabilities.prompts),
            )
        }
        if not any(flags.values()) or self._client().protocol_version < DISCOVERY_PROTOCOL_VERSION:
            return
        try:
            async with self._client().listen(
                tools_list_changed=flags["tools_list_changed"],
                resources_list_changed=flags["resources_list_changed"],
                prompts_list_changed=flags["prompts_list_changed"],
            ) as events:
                async for event in events:
                    self._events.record("catalog_changed", dump(event))
                    await self.invoke("catalog", {})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._events.record("subscription_failed", {"error": self.safe_error(error)})

    def _watch(self, uri: str) -> asyncio.Future[None]:
        """The acknowledgement of this connection's subscription to *uri*, started if needed."""
        watch = self._resource_watches.get(uri)
        if watch is None or watch[0].done():
            ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            # A restored subscription has no waiter; its failure is an event.
            ready.add_done_callback(lambda done: done.cancelled() or done.exception())
            watch = (
                asyncio.create_task(
                    self._watch_resource(uri, ready), name=f"mcp-resource-watch:{self.id}"
                ),
                ready,
            )
            self._resource_watches[uri] = watch
        return watch[1]

    async def _watch_resource(self, uri: str, ready: asyncio.Future[None]) -> None:
        """Subscribe to *uri* for this connection; changes become ``resource_changed`` events."""
        # The subscribing call created this task, but the watch outlives that call.
        _CURRENT_CALL.set(None)
        client = self._client()
        try:
            if client.protocol_version < DISCOVERY_PROTOCOL_VERSION:
                await self._legacy("resource_subscriptions", lambda: client.subscribe_resource(uri))
                ready.set_result(None)
                # Changes arrive as notifications (``_message``); like a listen
                # stream, the subscription lasts while this task runs.
                await asyncio.Event().wait()
            else:
                async with client.listen(resource_subscriptions=[uri]) as events:
                    ready.set_result(None)
                    async for event in events:
                        self._events.record("resource_changed", {"uri": uri, "event": dump(event)})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._events.record(
                "subscription_failed", {"uri": uri, "error": self.safe_error(error)}
            )
            if not ready.done():
                ready.set_exception(ValueError(self.safe_error(error)))

    @staticmethod
    def _expected(error: BaseException) -> bool:
        if isinstance(error, BaseExceptionGroup):
            return all(ConnectionRunner._expected(item) for item in error.exceptions)
        return isinstance(error, EXPECTED_FAILURES)


def _recorded_elsewhere(message: types.ServerNotification) -> bool:
    """Whether another handler records *message*: the SDK also hands it to ``_message``.

    Log messages and the progress of vBot's requests have their callbacks; a
    ``subscriptions/listen`` stream's events, stamped with its subscription id,
    their watch (``_watch_catalog``, ``_watch_resource``).
    """
    if isinstance(message, types.LoggingMessageNotification | types.ProgressNotification):
        return True
    meta = getattr(getattr(message, "params", None), "meta", None)
    return isinstance(meta, dict) and SUBSCRIPTION_ID_META_KEY in meta


def _refusal(status: int, error: Exception) -> str:
    """The server's HTTP *status* answer to a call, with its JSON-RPC error message if any."""
    try:
        text = f"HTTP {status} {HTTPStatus(status).phrase}"
    except ValueError:
        text = f"HTTP {status}"
    # The SDK turns an answer without a JSON-RPC error into this placeholder.
    if isinstance(error, MCPError) and error.message != "Server returned an error response":
        text += f": {error.message}"
    return text


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
