"""MCP transport, negotiated capabilities, request isolation, and connection lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import threading
from collections import deque
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx2
import mcp_types as types
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.client.auth import OAuthClientProvider
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from mcp.shared.dispatcher import CallOptions
from mcp.shared.exceptions import MCPError

from core.extensions.operations import ExtensionHost
from core.projects.address import format_agent_address
from core.tools.tools import ToolContext

from .interactions import InputRequests

EVENT_HISTORY_LIMIT = 256
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
STDERR_CHUNK_SIZE = 4096
_LOGGER = logging.getLogger("vbot.extensions.mcp")
EXPECTED_FAILURES = (
    ValueError,
    OSError,
    TimeoutError,
    MCPError,
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


def dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="json", by_alias=True, exclude_none=True))
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return value
    raise TypeError("MCP payload must be an object")


class OAuthStorage:
    def __init__(self, host: ExtensionHost, identifier: str) -> None:
        self.host = host
        self.prefix = f"VBOT_MCP_{identifier.upper()}_OAUTH"

    async def get_tokens(self) -> OAuthToken | None:
        raw = self.host.resolve_credential(f"{self.prefix}_TOKENS")
        return OAuthToken.model_validate_json(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.host.set_credential(f"{self.prefix}_TOKENS", tokens.model_dump_json(by_alias=True))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self.host.resolve_credential(f"{self.prefix}_CLIENT")
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.host.set_credential(
            f"{self.prefix}_CLIENT", client_info.model_dump_json(by_alias=True)
        )


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
        self, config: dict[str, Any], host: ExtensionHost, inputs: InputRequests, publish: Any
    ) -> None:
        self.config = config
        self.host = host
        self.inputs = inputs
        self.publish = publish
        self.id = config["id"]
        self.state = "disconnected"
        self.error: str | None = None
        self.client: Client | None = None
        self.catalog: dict[str, Any] = {}
        self.context: ToolContext | None = None
        self._queue: asyncio.Queue[Invocation | None] = asyncio.Queue(CONNECTION_QUEUE_LIMIT)
        self._task: asyncio.Task[None] | None = None
        self._active: asyncio.Task[dict[str, Any]] | None = None
        self._ready = asyncio.Event()
        self._closing = False
        self._events: deque[dict[str, Any]] = deque(maxlen=EVENT_HISTORY_LIMIT)
        self._sequence = 0
        self._catalog_pages: dict[str, list[dict[str, Any]]] = {}
        self._subscriptions: dict[str, asyncio.Task[None]] = {}
        self._oauth_url: str | None = None
        self._log_level = "info"

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._closing = False
        self._ready.clear()
        self.state = "connecting"
        self._task = asyncio.create_task(self._run(), name=f"mcp:{self.id}")
        self._task.add_done_callback(self._finished)

    @staticmethod
    def _finished(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def close(self) -> None:
        self._closing = True
        self.inputs.cancel_connection(self.id)
        if self._active is not None:
            self._active.cancel()
        if self._task is not None and not self._task.done():
            # Cancellation also interrupts handshakes and pending OAuth.
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(self._task, CONNECTION_CLOSE_TIMEOUT_SECONDS)
        self.state = "disconnected"

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
        first = self._events[0]["sequence"] if self._events else self._sequence + 1
        return {
            "events": [event for event in self._events if event["sequence"] > after],
            "cursor": self._sequence,
            "missed_events": max(0, first - after - 1),
        }

    async def invoke(
        self, operation: str, arguments: dict[str, Any], context: ToolContext | None = None
    ) -> dict[str, Any]:
        if self._closing:
            raise ValueError("MCP connection is closing")
        if self._task is None or self._task.done():
            self.start()
        await self._ready.wait()
        if self.state != "connected":
            raise ValueError(self.error or "MCP connection did not become ready")
        result: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        await self._queue.put(Invocation(operation, arguments, context, result))
        return await result

    async def _run(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                transport = await self._transport(stack)
                client = Client(
                    transport,
                    read_timeout_seconds=self.config["timeout"],
                    mode="legacy" if self.config["transport"] == "sse" else "auto",
                    sampling_callback=self._sample,
                    sampling_capabilities=types.SamplingCapability(
                        tools=types.SamplingToolsCapability()
                    ),
                    elicitation_callback=self._elicit,
                    list_roots_callback=self._roots,
                    logging_callback=self._log,
                    message_handler=self._message,
                    client_info=types.Implementation(name="vbot", version="1"),
                )
                self.client = await stack.enter_async_context(client)
                await self._refresh()
                self.state = "connected"
                self.error = None
                self._ready.set()
                self._subscriptions["catalog"] = asyncio.create_task(self._watch_catalog())
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
            self.error = self._safe_error(error)
            self._record("connection_failed", {"error": self.error})
            if self._expected(error):
                _LOGGER.warning("MCP connection failed (connection=%s): %s", self.id, self.error)
            else:
                _LOGGER.error("MCP connection crashed (connection=%s): %s", self.id, self.error)
                raise
        finally:
            self.client = None
            self._ready.set()
            while not self._queue.empty():
                invocation = self._queue.get_nowait()
                if invocation is not None and not invocation.result.done():
                    invocation.result.set_exception(
                        ValueError(self.error or "MCP connection closed")
                    )

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
            thread = threading.Thread(target=self._read_stderr, args=(reader, loop), daemon=True)
            thread.start()
            return stdio_client(parameters, errlog=errors)
        headers = {
            key: self._credential(source)
            for key, source in self.config.get("credential_headers", {}).items()
        }
        auth = self._oauth() if self.config.get("oauth") else None
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

    def _oauth(self) -> OAuthClientProvider:
        return OAuthClientProvider(
            self.config["url"],
            OAuthClientMetadata(
                client_name="vBot",
                redirect_uris=[
                    self.config.get("oauth_redirect_uri", "http://localhost:8765/callback")
                ],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
            ),
            OAuthStorage(self.host, self.id),
            redirect_handler=self._oauth_redirect,
            callback_handler=self._oauth_callback,
        )

    async def _oauth_redirect(self, url: str) -> None:
        self._oauth_url = url

    async def _oauth_callback(self) -> AuthorizationCodeResult:
        response = await self.inputs.request(self.id, "oauth", {"url": self._oauth_url})
        query = parse_qs(urlsplit(response.get("redirect_url", "")).query)
        if "code" not in query:
            raise ValueError(
                "OAuth response requires the complete redirected URL containing the code"
            )
        return AuthorizationCodeResult(
            code=query["code"][0],
            state=query.get("state", [None])[0],
            iss=query.get("iss", [None])[0],
        )

    async def _serve(self) -> None:
        while not self._closing:
            invocation = await self._queue.get()
            if invocation is None:
                return
            if invocation.result.cancelled():
                continue
            if invocation.context is not None:
                address = format_agent_address(
                    invocation.context.agent_id, invocation.context.project_id
                )
                if address not in self.config["agents"]:
                    invocation.result.set_exception(ValueError("MCP connection access was revoked"))
                    continue
            self.context = invocation.context
            self._active = asyncio.create_task(
                self._perform_with_retries(invocation.operation, invocation.arguments)
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
                safe = self._safe_error(error)
                self._record("request_failed", {"operation": invocation.operation, "error": safe})
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
                self._record("read_retry", {"operation": operation, "attempt": attempt + 1})
                await asyncio.sleep(delay)
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
                subscription_task = asyncio.create_task(self._watch_resource(uri, ready))
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
        # SDK 2.1.1's typed send_request validates historical task handles as
        # CallToolResult and rejects them. Its pinned dispatcher retains the same
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

    async def _roots(self, context: Any) -> Any:
        roots = []
        if self.context is not None:
            roots.append(
                types.Root.model_validate({"uri": Path(self.context.effective_cwd).as_uri()})
            )
        return types.ListRootsResult(roots=roots)

    async def _sample(self, context: Any, params: Any) -> Any:
        if self.context is None:
            return types.ErrorData(
                code=types.INVALID_REQUEST, message="Sampling requires an active Agent invocation"
            )
        request = dump(params)
        self._record(
            "sampling_request",
            {"request": request, "model_policy": "configured_agent", "additional_context": "none"},
        )
        messages = sampling_messages(request)
        sample = await self.host.sample(
            self.context,
            {
                "messages": messages,
                "max_tokens": request["maxTokens"],
                "temperature": request.get("temperature"),
                "stop_sequences": request.get("stopSequences"),
                "tool_choice": request.get("toolChoice", {}).get("mode"),
                "tools": [
                    {
                        "name": tool["name"],
                        "description": tool.get("description", tool["name"]),
                        "parameters": tool["inputSchema"],
                    }
                    for tool in request.get("tools", [])
                ],
            },
        )
        self._record("sampling_usage", sample.get("usage", {}))
        content = []
        if sample.get("content"):
            content.append({"type": "text", "text": sample["content"]})
        for call in sample.get("tool_calls", []):
            content.append(
                {
                    "type": "tool_use",
                    "id": call["id"],
                    "name": call["name"],
                    "input": call["arguments"],
                }
            )
        stop_reason = "toolUse" if sample.get("tool_calls") else "endTurn"
        if sample.get("terminal_outcome") == "output_truncated":
            stop_reason = "maxTokens"
        result = {
            "role": "assistant",
            "model": sample["model"],
            "content": content,
            "stopReason": stop_reason,
        }
        if request.get("tools"):
            return types.CreateMessageResultWithTools.model_validate(result)
        result["content"] = content[0] if content else {"type": "text", "text": ""}
        return types.CreateMessageResult.model_validate(result)

    async def _elicit(self, context: Any, params: Any) -> Any:
        session_id = self.context.session_id if self.context is not None else None
        response = await self.inputs.request(self.id, "elicitation", dump(params), session_id)
        return types.ElicitResult.model_validate(response)

    async def _log(self, params: Any) -> None:
        self._record("log", dump(params))

    async def _message(self, message: Any) -> None:
        if isinstance(message, types.ServerNotification):
            self._record("notification", dump(message))
        elif isinstance(message, Exception):
            self.error = self._safe_error(message)
            self.state = "failed"
            self._record("connection_failed", {"error": self.error})
            if self._task is not None:
                self._task.cancel()

    async def _progress(self, progress: float, total: float | None, message: str | None) -> None:
        self._record("progress", {"progress": progress, "total": total, "message": message})

    def _record(self, kind: str, payload: Any) -> None:
        self._sequence += 1
        safe_payload = json.loads(
            self._redact(json.dumps(payload, ensure_ascii=False, default=dump))
        )
        self._events.append({"sequence": self._sequence, "kind": kind, "payload": safe_payload})

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
                    self._record("catalog_changed", dump(event))
                    await self.invoke("catalog", {})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._record("subscription_failed", {"error": self._safe_error(error)})

    async def _watch_resource(self, uri: str, ready: asyncio.Future[None]) -> None:
        try:
            async with self._client().listen(resource_subscriptions=[uri]) as events:
                if not ready.done():
                    ready.set_result(None)
                async for event in events:
                    self._record("resource_changed", {"uri": uri, "event": dump(event)})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._record("subscription_failed", {"uri": uri, "error": self._safe_error(error)})
            if not ready.done():
                ready.set_exception(ValueError(self._safe_error(error)))

    def _safe_error(self, error: BaseException) -> str:
        if isinstance(error, BaseExceptionGroup):
            message = "; ".join(self._safe_error(item) for item in error.exceptions)
        else:
            message = f"{type(error).__name__}: {error}"
        return self._redact(message)

    def _secrets(self) -> list[str]:
        keys = set(self.config.get("credential_environment", {}).values())
        keys.update(self.config.get("credential_headers", {}).values())
        secrets = [self.host.resolve_credential(key) for key in keys]
        prefix = f"VBOT_MCP_{self.id.upper()}_OAUTH"
        for suffix in ("TOKENS", "CLIENT"):
            raw = self.host.resolve_credential(f"{prefix}_{suffix}")
            if raw:
                secrets.append(raw)
                values = json.loads(raw)
                secrets.extend(
                    str(value)
                    for key, value in values.items()
                    if key in {"access_token", "refresh_token", "id_token", "client_secret"}
                    and value
                )
        return [value for value in secrets if value]

    def _redact(self, message: str) -> str:
        for secret in sorted(self._secrets(), key=len, reverse=True):
            message = message.replace(secret, "[redacted]")
        return message

    def _read_stderr(self, descriptor: int, loop: asyncio.AbstractEventLoop) -> None:
        # Drain continuously so a verbose server cannot block its protocol pipe.
        with os.fdopen(descriptor, "r", encoding="utf-8", errors="replace") as stream:
            pending = ""
            while chunk := stream.read(STDERR_CHUNK_SIZE):
                pending += chunk
                tail = max((len(secret) for secret in self._secrets()), default=0)
                if len(pending) <= tail:
                    continue
                boundary = len(pending) - tail
                for secret in self._secrets():
                    start = pending.rfind(secret, 0, boundary + len(secret))
                    if start >= 0 and start < boundary < start + len(secret):
                        boundary = start
                output, pending = pending[:boundary], pending[boundary:]
                if output and not loop.is_closed():
                    loop.call_soon_threadsafe(
                        self._record, "stderr", {"text": self._redact(output)}
                    )
            if pending and not loop.is_closed():
                loop.call_soon_threadsafe(self._record, "stderr", {"text": self._redact(pending)})

    @staticmethod
    def _expected(error: BaseException) -> bool:
        if isinstance(error, BaseExceptionGroup):
            return all(ConnectionRunner._expected(item) for item in error.exceptions)
        return isinstance(error, EXPECTED_FAILURES)


def sampling_messages(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Map only the server's explicit sampling context, never Session history."""
    messages: list[dict[str, Any]] = []
    if request.get("systemPrompt"):
        messages.append({"role": "system", "content": request["systemPrompt"]})
    for message in request["messages"]:
        blocks = (
            message["content"] if isinstance(message["content"], list) else [message["content"]]
        )
        content = []
        calls = []
        results = []
        for block in blocks:
            kind = block["type"]
            if kind == "text":
                content.append({"type": "text", "text": block["text"]})
            elif kind in {"image", "audio"}:
                content.append(
                    {"type": kind, "base64": block["data"], "media_type": block["mimeType"]}
                )
            elif kind == "tool_use":
                calls.append(
                    {"id": block["id"], "name": block["name"], "arguments": block["input"]}
                )
            elif kind == "tool_result":
                results.append(
                    {
                        "role": "tool",
                        "tool_call_id": block["toolUseId"],
                        "content": json.dumps(block, ensure_ascii=False),
                    }
                )
            else:
                raise ValueError(f"Unsupported sampling content type: {kind}")
        if content or calls:
            entry = {"role": message["role"], "content": content}
            if calls:
                entry["tool_calls"] = calls
            messages.append(entry)
        messages.extend(results)
    return messages
