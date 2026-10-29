"""MCP connection lifecycle, bounded admission, recovery and call settlement."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, suppress
from copy import deepcopy
from http import HTTPStatus
from typing import Any

import anyio
import httpx2
import mcp.types as types
from mcp import Client
from mcp.client.auth import OAuthFlowError
from mcp.shared.exceptions import MCPError

from core.extensions.operations import ExtensionHost
from core.tools.tools import ToolContext
from core.utils.errors import VBotError

from ._callbacks import ServerRequests
from ._calls import _CURRENT_CALL, _Call
from ._calls import InvalidToolResultError as InvalidToolResultError
from ._calls import InvocationNotSentError as InvocationNotSentError
from ._calls import SubscriptionEndedError as SubscriptionEndedError
from ._calls import ToolChangedError as ToolChangedError
from ._calls import UnsupportedOperationError as UnsupportedOperationError
from ._events import ConnectionEvents
from ._protocol import TASK_OPERATIONS as TASK_OPERATIONS
from ._protocol import ConnectionProtocol
from ._protocol import operation_schema as operation_schema
from ._protocol import unsupported_operations as unsupported_operations
from ._tasks import (
    TaskEndedError,
    TaskRun,
    TasksExtension,
    TaskUnknownError,
)
from ._transport import ConnectionTransport, _ObservedTransport
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
        authorize: Callable[[ToolContext], Awaitable[None] | None] | None = None,
        on_change: Callable[[], None] | None = None,
        recovery: Any = None,
    ) -> None:
        self.config = config
        self.host = host
        self.inputs = inputs
        self.publish = publish
        self._authorize = authorize
        self._recovery = recovery
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
        self._events = ConnectionEvents(host, lambda: self.config, on_stderr=self._stderr_changed)
        self._protocol = ConnectionProtocol(self)
        self._transport = ConnectionTransport(self)
        self._requests = ServerRequests(
            self.id,
            host,
            inputs,
            self._events,
            self._attributed_context,
            config=lambda: self.config,
        )
        # Cancellations of kept server tasks, each waiting for a connection to send it.
        self._kept_cancels: set[asyncio.Task[None]] = set()
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

    async def recover_task(
        self, run: TaskRun, context: ToolContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Read a persisted server task. This path never sends the original Tool call."""
        call = await self._submit("tools/call", arguments, context, run)
        try:
            return await call.result
        except (TaskEndedError, TaskUnknownError) as error:
            return {"isError": True, "content": [{"type": "text", "text": str(error)}]}

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
        schema = None
        if operation == "tools/call" and resumed is None:
            schema = next(
                (
                    deepcopy(tool["inputSchema"])
                    for tool in self.catalog.get("tools", [])
                    if tool["name"] == arguments.get("name")
                ),
                None,
            )
        owner = await self._connected()
        call = _Call(
            self,
            operation,
            arguments,
            context,
            asyncio.get_running_loop().create_future(),
            delivered=not self._tracks_delivery,
            owner=owner,
            input_schema=schema,
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
                self._transport._http_marks_delivery = False
                transport = await self._transport.open(stack)
                # A transport the SDK enters itself; an in-process server has no streams.
                self._tracks_delivery = isinstance(transport, AbstractAsyncContextManager)
                if self._tracks_delivery:
                    transport = _ObservedTransport(
                        transport, self, marks_delivery=not self._transport._http_marks_delivery
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
                    list_roots_callback=self._requests.roots
                    if self._protocol._offers_roots()
                    else None,
                    logging_callback=self._requests.log,
                    message_handler=self._protocol._message,
                    client_info=types.Implementation(name="vbot", version="1"),
                    # No response cache: every listing and read reaches the server (see the
                    # MCP domain map, Compatibility boundary).
                    cache=None,
                    # Offered on 2026-07-28 connections only: legacy tasks need no declaration.
                    extensions=[TasksExtension(self._protocol._resolve_task)],
                )
                self.client = await stack.enter_async_context(client)
                self._protocol._check_results(self.client)
                self._protocol._transport_warned = False
                await self._protocol._refresh()
                self.state = "connected"
                self.error = None
                self._reconnects = 0
                self._log_connected()
                established = True
                self._ready.set()
                self._protocol._subscriptions["catalog"] = asyncio.create_task(
                    self._protocol._watch_catalog(), name=f"mcp-catalog-watch:{self.id}"
                )
                # A server forgets subscriptions with its session; a failure is a
                # ``subscription_failed`` event and the next connection tries again.
                for uri in self._protocol._resource_subscriptions:
                    self._protocol._watch(uri)
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
            *self._protocol._subscriptions.values(),
            *(task for task, _ in self._protocol._resource_watches.values()),
        ]
        self._protocol._subscriptions.clear()
        self._protocol._resource_watches.clear()
        for task in background:
            task.cancel()
        tasks.update(background)
        deadline = asyncio.get_running_loop().time() + CALL_STOP_SECONDS
        if tasks:
            await asyncio.wait(tasks, timeout=CALL_STOP_SECONDS)
        # Stopped calls ask the server to cancel their tasks meanwhile; the
        # transport ends after this, so later cancellations could not be sent.
        if self._protocol._task_cancels:
            remaining = deadline - asyncio.get_running_loop().time()
            await asyncio.wait(set(self._protocol._task_cancels), timeout=max(0.0, remaining))
            for task in self._protocol._task_cancels:
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
                    authorized = self._authorize(call.context)
                    if authorized is not None:
                        await authorized
                except (ValueError, VBotError) as error:
                    if not call.result.done():
                        call.result.set_exception(InvocationNotSentError(str(error), denied=True))
                    slots.release()
                    continue
                except BaseException:
                    # Dequeue removed this call from the queue, but it has not
                    # joined _calls yet. Settle it even when authorization fails
                    # unexpectedly and ends the connection.
                    if not call.result.done():
                        call.result.set_exception(
                            InvocationNotSentError(
                                "MCP connection closed before authorization finished"
                            )
                        )
                    slots.release()
                    raise
            if call.result.done() or self._closing or call.owner is not self._task:
                if not call.result.done():
                    call.result.set_exception(
                        InvocationNotSentError("MCP connection closed before the call was sent")
                    )
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
            if not self._closing and not self._calls and owner is self._task:
                # Inputs a finished call's server still waits for have no one to answer.
                # Close already cancelled this runner's inputs; a late callback
                # must not cancel those of a replacement with the same id.
                self.inputs.cancel_connection(self.id)

        call.result.add_done_callback(cancel)
        task.add_done_callback(finished)

    async def _run_call(self, call: _Call) -> None:
        # Each task runs in its own context copy: this marks only this call.
        _CURRENT_CALL.set(call)
        try:
            if call.resuming:
                value = await self._protocol._resume_task(call)
            else:
                value = await self._protocol._perform(call.operation, call.arguments, call.context)
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
                await self._protocol._notify_roots_changed()
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
            InvalidToolResultError
            | TaskEndedError
            | TaskUnknownError
            | UnsupportedOperationError
            | ToolChangedError
            | SubscriptionEndedError,
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

    @staticmethod
    def _expected(error: BaseException) -> bool:
        if isinstance(error, BaseExceptionGroup):
            return all(ConnectionRunner._expected(item) for item in error.exceptions)
        return isinstance(error, EXPECTED_FAILURES)


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
