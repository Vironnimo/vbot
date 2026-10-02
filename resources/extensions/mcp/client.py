"""MCP transport, negotiated capabilities, request isolation, and connection lifecycle."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, cast

import anyio
import httpx2
import mcp.types as types
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.client.auth import OAuthFlowError
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.dispatcher import CallOptions
from mcp.shared.exceptions import MCPError

from core.extensions.operations import ExtensionHost
from core.tools.tools import ToolContext
from core.utils.errors import VBotError

from ._callbacks import ServerRequests
from ._events import ConnectionEvents, dump
from ._oauth import ConnectionOAuth
from .interactions import InputRequests

CONNECTION_QUEUE_LIMIT = 64
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
DISCOVERY_PROTOCOL_VERSION = "2026-07-28"
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


def operation_schema(operation: str) -> dict[str, Any]:
    model = OPERATION_MODELS.get(operation)
    if model is not None:
        return dict(model.model_json_schema(by_alias=True))
    properties = {"after": {"type": "integer", "minimum": 0}} if operation == "events" else {}
    return {"type": "object", "properties": properties, "additionalProperties": False}


class InvocationNotSentError(ValueError):
    """An invocation that never reached the server, so it changed nothing there."""

    def __init__(self, message: str, *, denied: bool = False) -> None:
        super().__init__(message)
        self.denied = denied


@dataclass
class Invocation:
    operation: str
    arguments: dict[str, Any]
    context: ToolContext | None
    result: asyncio.Future[dict[str, Any]]


class ConnectionRunner:
    """One task owns each SDK context from entry through exit.

    Calls on one connection serialize so server-initiated requests cannot inherit
    another Agent's directories, Model, or Session. Different connections remain
    independent. A cancelled mutation is never automatically replayed.
    """

    def __init__(
        self,
        config: dict[str, Any],
        host: ExtensionHost,
        inputs: InputRequests,
        publish: Any,
        *,
        authorize: Callable[[ToolContext], None] | None = None,
    ) -> None:
        self.config = config
        self.host = host
        self.inputs = inputs
        self.publish = publish
        self._authorize = authorize
        self.id = config["id"]
        self.state = "disconnected"
        self.error: str | None = None
        self.client: Client | None = None
        self.catalog: dict[str, Any] = {}
        self.context: ToolContext | None = None
        # Each connection task gets its own queue, shut down when that connection ends.
        self._queue: asyncio.Queue[Invocation] = asyncio.Queue(CONNECTION_QUEUE_LIMIT)
        self._task: asyncio.Task[None] | None = None
        self._active: asyncio.Task[dict[str, Any]] | None = None
        self._ready = asyncio.Event()
        self._closing = False
        self._events = ConnectionEvents(host, lambda: self.config)
        self._requests = ServerRequests(self.id, host, inputs, self._events, lambda: self.context)
        self._catalog_pages: dict[str, list[dict[str, Any]]] = {}
        self._subscriptions: dict[str, asyncio.Task[None]] = {}
        self._log_level = "info"
        # A failing stretch spans lazy reconnects until one connection comes up.
        self._failures = 0
        self._failing_since: float | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._closing = False
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
        self._ready.set()
        self._reject_queued()
        self.inputs.cancel_connection(self.id)
        if self._active is not None:
            self._active.cancel()
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
        nothing can join it after this drain.
        """
        self._queue.shutdown()
        while True:
            try:
                invocation = self._queue.get_nowait()
            except asyncio.QueueShutDown:
                return
            if not invocation.result.done():
                invocation.result.set_exception(
                    InvocationNotSentError(self.error or "MCP connection closed")
                )

    def status(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state,
            "error": self.error,
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
        if self._closing:
            raise InvocationNotSentError("MCP connection is closing")
        if self._task is None or self._task.done():
            self.start()
        owner = self._task
        await self._ready.wait()
        if self._closing or self._task is not owner or owner is None or owner.done():
            raise InvocationNotSentError(self.error or "MCP connection closed")
        if self.state != "connected":
            raise InvocationNotSentError(self.error or "MCP connection did not become ready")
        result: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        try:
            # The queue belongs to the admitting connection: once that connection
            # ends, a call still waiting for room is refused, never sent later.
            await self._queue.put(Invocation(operation, arguments, context, result))
        except asyncio.QueueShutDown:
            raise InvocationNotSentError(self.error or "MCP connection closed") from None
        return await result

    async def _run(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                transport = await self._transport(stack)
                client = Client(
                    transport,
                    read_timeout_seconds=self.config["timeout"],
                    mode="legacy" if self.config["transport"] == "sse" else "auto",
                    sampling_callback=self._requests.sample,
                    sampling_capabilities=types.SamplingCapability(
                        tools=types.SamplingToolsCapability()
                    ),
                    elicitation_callback=self._requests.elicit,
                    list_roots_callback=self._requests.roots,
                    logging_callback=self._requests.log,
                    message_handler=self._message,
                    client_info=types.Implementation(name="vbot", version="1"),
                )
                self.client = await stack.enter_async_context(client)
                await self._refresh()
                self.state = "connected"
                self.error = None
                self._log_connected()
                self._ready.set()
                self._subscriptions["catalog"] = asyncio.create_task(
                    self._watch_catalog(), name=f"mcp-catalog-watch:{self.id}"
                )
                try:
                    await self._serve()
                finally:
                    for task in self._subscriptions.values():
                        task.cancel()
                    await asyncio.gather(*self._subscriptions.values(), return_exceptions=True)
                    self._subscriptions.clear()
        except asyncio.CancelledError:
            raise
        except (Exception, BaseExceptionGroup) as error:
            self.state = "failed"
            self.error = self.safe_error(error)
            self._events.record("connection_failed", {"error": self.error})
            expected = self._expected(error)
            self._log_failed(expected=expected)
            if not expected:
                raise
        finally:
            self.client = None
            self._ready.set()
            self._reject_queued()

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
            parameters = StdioServerParameters(
                command=self.config["command"],
                args=self.config.get("args", []),
                cwd=self.config.get("cwd"),
                env=environment,
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
        if self.config["transport"] == "sse":
            return sse_client(self.config["url"], headers=headers, auth=auth)
        http = await stack.enter_async_context(
            httpx2.AsyncClient(
                headers=headers,
                auth=auth,
                trust_env=False,
                timeout=httpx2.Timeout(self.config["timeout"], connect=15.0),
            )
        )
        return streamable_http_client(self.config["url"], http_client=http)

    def _credential(self, key: str) -> str:
        value = self.host.resolve_credential(key)
        if not value:
            raise ValueError(f"Missing MCP credential: {key}")
        return value

    async def _serve(self) -> None:
        while not self._closing:
            try:
                invocation = await self._queue.get()
            except asyncio.QueueShutDown:
                return
            if invocation.result.cancelled():
                continue
            if invocation.context is not None and self._authorize is not None:
                try:
                    self._authorize(invocation.context)
                except (ValueError, VBotError) as error:
                    invocation.result.set_exception(InvocationNotSentError(str(error), denied=True))
                    continue
            self.context = invocation.context
            self._active = asyncio.create_task(
                self._perform_with_retries(invocation.operation, invocation.arguments),
                name=f"mcp-call:{self.id}:{invocation.operation}",
            )
            active = self._active

            def cancel_active(
                future: asyncio.Future[dict[str, Any]], task: asyncio.Task[dict[str, Any]] = active
            ) -> None:
                if future.cancelled():
                    task.cancel()

            invocation.result.add_done_callback(cancel_active)
            try:
                value = await active
            except asyncio.CancelledError:
                invocation.result.cancel()
                if (
                    self._closing
                    or (owner := asyncio.current_task()) is not None
                    and owner.cancelling()
                ):
                    raise
            except Exception as error:
                safe = self.safe_error(error)
                self._events.record(
                    "request_failed", {"operation": invocation.operation, "error": safe}
                )
                if not invocation.result.done():
                    invocation.result.set_exception(
                        ValueError(safe) if self._expected(error) else error
                    )
                if not self._expected(error):
                    raise
            else:
                if not invocation.result.done():
                    invocation.result.set_result(value)
            finally:
                self.inputs.cancel_connection(self.id)
                self.context = None
                self._active = None
                if (
                    invocation.context is not None
                    and not self._closing
                    and self.state == "connected"
                ):
                    await self._notify_roots_changed()

    async def _all(self, method: Any, field: str) -> list[dict[str, Any]]:
        cursor = None
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        self._catalog_pages[field] = []
        while True:
            page = await method(cursor=cursor, cache_mode="refresh")
            self._catalog_pages[field].append(dump(page))
            items.extend(dump(item) for item in getattr(page, field))
            cursor = page.next_cursor
            if cursor is None:
                return items
            if cursor in seen:
                raise ValueError("MCP server repeated a pagination cursor")
            seen.add(cursor)

    async def _refresh(self) -> dict[str, Any]:
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
        for capability, method, field in (
            (capabilities.tools, client.list_tools, "tools"),
            (capabilities.resources, client.list_resources, "resources"),
            (capabilities.resources, client.list_resource_templates, "resource_templates"),
            (capabilities.prompts, client.list_prompts, "prompts"),
        ):
            if capability is not None:
                catalog[field] = await self._all(method, field)
        catalog["pages"] = dict(self._catalog_pages)
        self.publish(self, catalog)
        self.catalog = catalog
        return catalog

    def _client(self) -> Client:
        if self.client is None:
            raise ValueError("MCP connection is not connected")
        return self.client

    async def _perform_with_retries(
        self, operation: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        for attempt in range(MAX_READ_RETRIES + 1):
            try:
                return await self._perform(operation, arguments)
            except (httpx2.HTTPError, OSError, TimeoutError) as error:
                retryable = (
                    error.response.status_code in RETRYABLE_READ_STATUSES
                    if isinstance(error, httpx2.HTTPStatusError)
                    else True
                )
                if operation not in READ_OPERATIONS or not retryable or attempt == MAX_READ_RETRIES:
                    raise
                delay = READ_RETRY_BASE_SECONDS * (2**attempt) * random.uniform(0.5, 1.5)
                self._events.record("read_retry", {"operation": operation, "attempt": attempt + 1})
                await _sleep(delay)
        raise AssertionError("Read retry loop must return or raise")

    async def _perform(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        errors = list(Draft202012Validator(operation_schema(operation)).iter_errors(arguments))
        if errors:
            paths = ["/".join(map(str, error.absolute_path)) or "arguments" for error in errors]
            raise ValueError(f"Invalid MCP operation arguments at: {', '.join(paths)}")
        client = self._client()
        if self.context is not None:
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
            if tool.get("execution", {}).get("taskSupport") == "required" or "task" in arguments:
                result = await self._task_send(
                    "tools/call", {**arguments, "task": arguments.get("task", {})}
                )
                types.CreateTaskResult.model_validate(result)
                return result
            return dump(
                await client.call_tool(
                    arguments["name"],
                    arguments.get("arguments", {}),
                    progress_callback=self._progress,
                    meta=self._request_meta(),
                )
            )
        if operation == "resources/read":
            return dump(
                await client.read_resource(
                    arguments["uri"], cache_mode="refresh", meta=self._request_meta()
                )
            )
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
            existing = self._subscriptions.get(uri)
            if existing is None or existing.done():
                ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                subscription_task = asyncio.create_task(
                    self._watch_resource(uri, ready), name=f"mcp-resource-watch:{self.id}"
                )
                self._subscriptions[uri] = subscription_task
                await ready
            return {"subscribed": uri}
        if operation == "resources/unsubscribe":
            task = self._subscriptions.pop(arguments["uri"], None)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return {"unsubscribed": arguments["uri"]}
        if operation == "logging/setLevel":
            level = arguments["level"]
            types.LoggingMessageNotificationParams.model_validate({"level": level, "data": None})
            self._log_level = level
            if client.protocol_version >= DISCOVERY_PROTOCOL_VERSION:
                return {"level": level, "scope": "subsequent_requests"}
            return dump(await client.set_logging_level(level))
        if operation == "ping":
            if client.protocol_version >= DISCOVERY_PROTOCOL_VERSION:
                await self._refresh()
                return {
                    "verified": "catalog",
                    "reason": "ping is not defined in the negotiated protocol",
                }
            return dump(await client.send_ping())
        if operation == "events":
            return self.events(arguments.get("after", 0))
        if operation.startswith("tasks/"):
            return await self._task_request(operation, arguments)
        raise ValueError(f"Unknown MCP operation: {operation}")

    def _request_meta(self) -> types.RequestParamsMeta | None:
        if self._client().protocol_version < DISCOVERY_PROTOCOL_VERSION:
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
        return await self._task_send(operation, arguments)

    async def _task_send(self, method: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # The pinned SDK's (2.2.0) typed send_request validates historical task handles
        # as CallToolResult and rejects them. Its pinned dispatcher retains the same
        # transport, cancellation and progress semantics without that wrong schema.
        # Keep this one compatibility seam covered by the real stdio task test.
        session = self._client().session
        options: CallOptions = {"timeout": self.config["timeout"], "on_progress": self._progress}
        session._stamp({"method": method, "params": arguments}, options)
        return dict(await session._dispatcher.send_raw_request(method, arguments, options))

    async def _notify_roots_changed(self) -> None:
        client = self._client()
        if client.protocol_version < DISCOVERY_PROTOCOL_VERSION:
            await client.send_roots_list_changed()

    async def _message(self, message: Any) -> None:
        if isinstance(message, types.ServerNotification):
            self._events.record("notification", dump(message))
        elif isinstance(message, Exception):
            self.error = self.safe_error(message)
            self.state = "failed"
            self._events.record("connection_failed", {"error": self.error})
            if not self._closing:
                self._log_failed(expected=self._expected(message))
            if self._task is not None:
                self._task.cancel()

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
        if not any(flags.values()):
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

    async def _watch_resource(self, uri: str, ready: asyncio.Future[None]) -> None:
        try:
            async with self._client().listen(resource_subscriptions=[uri]) as events:
                if not ready.done():
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
