"""Negotiated MCP operations, catalog publication and long-lived subscriptions."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import warnings
import weakref
from collections.abc import Callable, Coroutine, Mapping
from typing import TYPE_CHECKING, Any, cast

import mcp.types as types
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.client.extension import ClaimContext
from mcp.shared.dispatcher import CallOptions
from mcp.shared.exceptions import MCPDeprecationWarning
from mcp.shared.subscriptions import SUBSCRIPTION_ID_META_KEY

from core.tools.tools import ToolContext
from core.utils.workers import settle_before_cancelling

from ._calls import (
    _CURRENT_CALL,
    InvalidToolResultError,
    SubscriptionEndedError,
    UnsupportedOperationError,
    _Call,
)
from ._discovery import remote_tool_name, usable_tool
from ._events import dump
from ._tasks import (
    TASKS_EXTENSION,
    TERMINAL_STATUSES,
    ExtensionWire,
    LegacyWire,
    TaskHandle,
    TaskRun,
    TaskState,
    TaskUnknownError,
    TaskWire,
    cancel_quietly,
    drive,
    legacy_state,
)

if TYPE_CHECKING:
    from .client import ConnectionRunner

_LOGGER = logging.getLogger("vbot.extensions.mcp")
_sleep = asyncio.sleep

CATALOG_REFRESH_DELAY_SECONDS = 0.5

DISCOVERY_PROTOCOL_VERSION = "2026-07-28"

RESULT_PROBLEM_CHARACTERS = 300
_SUBSCRIPTION_ENDED = (
    "The subscription request ended because another request unsubscribed this resource."
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


@functools.cache
def _model_schema_json(operation: str) -> str:
    """The JSON Schema of a protocol operation's parameter model, built once.

    Building it from the pydantic model takes milliseconds, and every browse
    of a connection lists all operations twice (target and definition).
    """
    return json.dumps(OPERATION_MODELS[operation].model_json_schema(by_alias=True))


def operation_schema(operation: str) -> dict[str, Any]:
    """The arguments schema of *operation*; a fresh copy the caller may change."""
    if operation in OPERATION_MODELS:
        schema: dict[str, Any] = json.loads(_model_schema_json(operation))
        return schema
    properties = {"after": {"type": "integer", "minimum": 0}} if operation == "events" else {}
    return {"type": "object", "properties": properties, "additionalProperties": False}


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


class ConnectionProtocol:
    """Negotiated operations and subscriptions for one runner's current SDK connection."""

    def __init__(self, runner: ConnectionRunner) -> None:
        self._runner = runner
        self._refreshing = asyncio.Lock()
        self._published: str | None = None
        # Remote Tools left out of the current catalog (``_skip_unusable_tools``).
        self._skipped: set[str] = set()
        self._subscriptions: dict[str, asyncio.Task[None]] = {}
        self._resource_watches: dict[str, tuple[asyncio.Task[None], asyncio.Future[None]]] = {}
        self._resource_subscriptions: set[str] = set()
        self._resource_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._task_cancels: set[asyncio.Task[None]] = set()
        self._catalog_stale = False
        self._transport_warned = False
        self._log_level: str | None = None
        self._legacy_features: set[str] = set()

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
            self._skip_unusable_tools(catalog)
            fingerprint = hashlib.sha256(
                json.dumps(catalog, sort_keys=True, default=str).encode()
            ).hexdigest()
            if fingerprint != self._published:
                self._runner.publish(self._runner, catalog)
                self._published = fingerprint
            self._runner.catalog = catalog
            return catalog

    def _skip_unusable_tools(self, catalog: dict[str, Any]) -> None:
        """Leave out the Tools whose input schema vBot's Tool contract refuses.

        The catalog keeps the other Tools with their schemas in the contract's
        dialect (``usable_tool``). One refused schema must not cost the connection
        its other Tools, nor list a Tool that no call could reach. Each Tool
        records a ``tool_skipped`` event and logs one WARNING, without its schema,
        when it starts being skipped.
        """
        usable: list[dict[str, Any]] = []
        skipped: set[str] = set()
        for listed in catalog["tools"]:
            tool, problem = usable_tool(self._runner.id, listed)
            if problem is None:
                usable.append(tool)
                continue
            skipped.add(tool["name"])
            if tool["name"] in self._skipped:
                continue
            self._runner._events.record("tool_skipped", {"tool": tool["name"], "problem": problem})
            _LOGGER.warning(
                "MCP tool skipped: its input schema does not fit the Tool contract "
                "(connection=%s tool=%s)",
                self._runner.id,
                remote_tool_name(self._runner.id, tool["name"]),
            )
        catalog["tools"] = usable
        self._skipped = skipped

    def _client(self) -> Client:
        if self._runner.client is None:
            raise ValueError("MCP connection is not connected")
        return self._runner.client

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
                problem = self._runner.redact(str(error).split("\n", 1)[0])
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
        if operation in unsupported_operations(self._runner.catalog):
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
                    for item in self._runner.catalog.get("tools", [])
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
            # Do not hold the URI lock through acknowledgement: unsubscribe
            # must be able to end a subscription while its server still waits.
            async with self._resource_locks.setdefault(uri, asyncio.Lock()):
                ready = self._watch(uri)
            await asyncio.shield(ready)
            current = self._resource_watches.get(uri)
            if current is None or current[1] is not ready:
                raise SubscriptionEndedError(_SUBSCRIPTION_ENDED)
            self._resource_subscriptions.add(uri)
            return {"subscribed": uri}
        if operation == "resources/unsubscribe":
            uri = arguments["uri"]
            async with self._resource_locks.setdefault(uri, asyncio.Lock()):
                self._resource_subscriptions.discard(uri)
                watch = self._resource_watches.pop(uri, None)
                if watch is not None:
                    watch[0].cancel()
                    # Finish the admitted unsubscribe before a later subscribe
                    # starts, even when its own caller stops waiting meanwhile.
                    await settle_before_cancelling(self._unsubscribe_resource(client, uri, watch))
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
            return self._runner.events(arguments.get("after", 0))
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
        last: tuple[str, str | None] | None = None

        def observe(state: TaskState) -> None:
            nonlocal last
            if (state.status, state.message) != last:
                last = (state.status, state.message)
                self._runner._events.record(
                    "task_status",
                    {"task_id": state.task_id, "status": state.status, "message": state.message},
                )

        abandon = functools.partial(self._abandon_task, wire)
        driving = False
        try:
            if call is not None and call.runner is self._runner:
                call.server_task = run
                if self._runner._recovery is not None:
                    await self._runner._recovery.remember(
                        self._runner, call.context, call.arguments, run
                    )
                    run.checkpoint = lambda: self._runner._recovery.checkpoint(run)
            driving = True
            result = await drive(wire, run, observe=observe, abandon=abandon, resumed=resumed)
            if self._runner._recovery is not None:
                await self._runner._recovery.finish(run, dump(result))
            return result
        except BaseException as error:
            if not driving and run.state.status not in TERMINAL_STATUSES and not run.kept:
                abandon(run.state.task_id)
            if (
                self._runner._recovery is not None
                and run.receipt_id is not None
                and not run.kept
                and (not driving or isinstance(error, asyncio.CancelledError))
            ):
                await self._runner._recovery.forget(run.receipt_id)
            raise

    def _abandon_task(self, wire: TaskWire, task_id: str) -> None:
        """Cancel the server task of a call that stopped waiting, without blocking it."""

        async def cancel() -> None:
            # Part of no call: the call that started the task has ended.
            _CURRENT_CALL.set(None)
            await cancel_quietly(wire, task_id)

        task = asyncio.create_task(cancel(), name=f"mcp-task-cancel:{self._runner.id}")
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
        options: CallOptions = {
            "timeout": self._runner.config["timeout"],
            "on_progress": self._progress,
        }
        session._stamp({"method": method, "params": arguments}, options)
        return dict(await session._dispatcher.send_raw_request(method, arguments, options))

    def _offers_roots(self) -> bool:
        return self._runner.config.get("roots") == "workspace"

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
                self._runner.id,
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
                self._runner._events.record(
                    "resource_changed", {"uri": uri, "event": dump(message)}
                )
            elif isinstance(
                message,
                types.ToolListChangedNotification
                | types.ResourceListChangedNotification
                | types.PromptListChangedNotification,
            ):
                self._runner._events.record("catalog_changed", dump(message))
                self._catalog_changed()
            else:
                self._runner._events.record("notification", dump(message))
        elif isinstance(message, Exception):
            # A transport error that leaves the connection up, such as a line that
            # is not JSON on stdout; a transport that ends also closes its stream.
            error = self._runner.safe_error(message)
            self._runner._events.record("transport_error", {"error": error})
            if self._transport_warned:
                _LOGGER.debug("MCP transport error (connection=%s): %s", self._runner.id, error)
            else:
                self._transport_warned = True
                _LOGGER.warning("MCP transport error (connection=%s): %s", self._runner.id, error)

    def _catalog_changed(self) -> None:
        """Refresh the catalog soon after a legacy server reported a change.

        A current-protocol connection learns changes through its catalog watch.
        """
        client = self._runner.client
        if (
            client is None
            or self._runner.state != "connected"
            or client.protocol_version >= DISCOVERY_PROTOCOL_VERSION
        ):
            return
        self._catalog_stale = True
        refresh = self._subscriptions.get("catalog-refresh")
        if refresh is None or refresh.done():
            self._subscriptions["catalog-refresh"] = asyncio.create_task(
                self._refresh_catalog(), name=f"mcp-catalog-refresh:{self._runner.id}"
            )

    async def _refresh_catalog(self) -> None:
        # A notification inside a call may have started this task; it is no part of that call.
        _CURRENT_CALL.set(None)
        while self._catalog_stale:
            self._catalog_stale = False
            # Changes reported meanwhile join this refresh.
            await _sleep(CATALOG_REFRESH_DELAY_SECONDS)
            try:
                await self._runner.invoke("catalog", {})
            except ValueError as error:
                self._runner._events.record("subscription_failed", {"error": str(error)})
                return

    async def _progress(self, progress: float, total: float | None, message: str | None) -> None:
        self._runner._events.record(
            "progress", {"progress": progress, "total": total, "message": message}
        )

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
                    self._runner._events.record("catalog_changed", dump(event))
                    await self._runner.invoke("catalog", {})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._runner._events.record(
                "subscription_failed", {"error": self._runner.safe_error(error)}
            )

    def _watch(self, uri: str) -> asyncio.Future[None]:
        """The acknowledgement of this connection's subscription to *uri*, started if needed."""
        watch = self._resource_watches.get(uri)
        if watch is None or watch[0].done():
            ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            # A restored subscription has no waiter; its failure is an event.
            ready.add_done_callback(lambda done: done.cancelled() or done.exception())
            watch = (
                asyncio.create_task(
                    self._watch_resource(uri, ready), name=f"mcp-resource-watch:{self._runner.id}"
                ),
                ready,
            )

            def stopped(task: asyncio.Task[None]) -> None:
                # An unsubscribe can cancel the watch before its coroutine
                # starts, so its own cancellation handler is insufficient.
                if task.cancelled() and not ready.done():
                    ready.set_exception(SubscriptionEndedError(_SUBSCRIPTION_ENDED))

            watch[0].add_done_callback(stopped)
            self._resource_watches[uri] = watch
        return watch[1]

    async def _unsubscribe_resource(
        self, client: Client, uri: str, watch: tuple[asyncio.Task[None], asyncio.Future[None]]
    ) -> None:
        await asyncio.gather(watch[0], return_exceptions=True)
        if client.protocol_version < DISCOVERY_PROTOCOL_VERSION:
            # A pending subscribe can already have taken effect remotely before
            # its acknowledgement; cancelling its wait does not undo that effect.
            await self._legacy("resource_subscriptions", lambda: client.unsubscribe_resource(uri))

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
                        self._runner._events.record(
                            "resource_changed", {"uri": uri, "event": dump(event)}
                        )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._runner._events.record(
                "subscription_failed", {"uri": uri, "error": self._runner.safe_error(error)}
            )
            if not ready.done():
                ready.set_exception(ValueError(self._runner.safe_error(error)))
