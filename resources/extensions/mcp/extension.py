"""Bundled MCP integration: connection management and ordinary vBot Tools.

``MCPService`` owns the saved connections, one ``ConnectionRunner`` per
connection, and the Tools each connection publishes: the connection Tool
``mcp_<id>`` and one deferred follower per remote Tool. Finding and describing
catalog items lives in ``_discovery``, the management surface in
``_management``, and the protocol client in ``client``.

The protocol client imports the MCP SDK and its HTTP stack, about half a second
of imports, so it loads in a worker once a connection exists: at start when
connections are saved, else when the first one is saved. Loading the Extension
and running it without connections never imports the SDK.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from core.extensions import ExtensionAPI
from core.extensions.operations import PENDING_INPUTS_RESOURCE, ExtensionHost
from core.tools.availability import resolve_tool_access
from core.tools.contracts import declared_parameters
from core.tools.tools import (
    ToolContext,
    run_tool_worker,
    tool_failure,
    tool_success,
)

from ._arguments import browse_normalizer, connection_display
from ._catalog import CatalogSummaries, catalog_summary
from ._connectors import ConnectorCatalog, load_connectors
from ._definitions import (
    GUIDANCE_PREVIEW_CHARACTERS,
    MAX_FINISHED_JOBS,
    MCP_GUIDANCE,
    MCP_MESSAGES,
    MCP_OPERATION_DESCRIPTIONS,
    MCP_OPERATIONS,
    MCP_PARAMETERS,
    SEARCH_PAGE_SIZE,
    SEARCH_SUMMARY_CHARACTERS,
    TARGET_FINGERPRINT_LENGTH,
    TOOL_NAME_HASH_LENGTH,
    TOOL_NAME_LABEL_LENGTH,
    describe_call,
)
from ._discovery import (
    DescribeOutcome,
    catalog_entries,
    describe_page,
    describe_payload,
    entry_operation,
    invalid_target_arguments,
    names_denied_tool,
    noted,
    remote_tool_name,
    resolve,
    search_page,
    target_arguments,
    unsupported_operation,
)
from ._management import (
    CONNECTIONS_RESOURCE,
    JOBS_RESOURCE,
    ConnectionManagement,
    ManagementJobs,
    _credential_names,
    register_management,
)
from ._recovery import TaskRecovery
from ._views import compact
from .config import ConnectionStore, validate_connection
from .content import ContentStore
from .interactions import InputRequests

if TYPE_CHECKING:
    from .client import ConnectionRunner, InvalidToolResultError

__all__ = [
    "GUIDANCE_PREVIEW_CHARACTERS",
    "MAX_FINISHED_JOBS",
    "MCPService",
    "MCP_GUIDANCE",
    "MCP_MESSAGES",
    "MCP_OPERATIONS",
    "MCP_OPERATION_DESCRIPTIONS",
    "MCP_PARAMETERS",
    "SEARCH_PAGE_SIZE",
    "SEARCH_SUMMARY_CHARACTERS",
    "TARGET_FINGERPRINT_LENGTH",
    "TOOL_NAME_HASH_LENGTH",
    "TOOL_NAME_LABEL_LENGTH",
    "register",
    "remote_tool_name",
]

_DETAIL_CHARACTERS = 300


async def _load_protocol_client() -> None:
    """Import ``client`` and with it the MCP SDK in a worker, off the Event Loop.

    The service's own imports of ``client`` and its protocol modules then find
    them loaded.
    """
    await run_tool_worker(importlib.import_module, ".client", __package__)


@dataclass
class _ConnectionLock:
    lock: asyncio.Lock
    users: int = 0


class MCPService:
    def __init__(self, api: ExtensionAPI) -> None:
        self.api = api
        self.management = ConnectionManagement(self)
        self.host: ExtensionHost | None = None
        self.store: ConnectionStore | None = None
        self.content: ContentStore | None = None
        self.connections: dict[str, dict[str, Any]] = {}
        self.runners: dict[str, ConnectionRunner] = {}
        self.inputs = InputRequests(on_change=self._inputs_changed)
        # One revision for every change this service publishes to accessors.
        self._revision = 0
        self.jobs = ManagementJobs(self.inputs, on_finish=self._job_finished)
        # Serializes changes to the saved connections; held only while they change.
        self._lock = asyncio.Lock()
        # Per connection while in use: runner changes and the calls that select a runner.
        self._runner_locks: dict[str, _ConnectionLock] = {}
        # Per connection, the server title and Tool names its description shows.
        self.catalogs = CatalogSummaries(api.logger)
        # The hosted services the user can add in one step (``catalog.json``).
        self.connectors = ConnectorCatalog(())
        self._closed = False
        self._startup_error: str | None = None
        self.recovery: TaskRecovery | None = None

    async def start(self, host: ExtensionHost) -> None:
        if host.state_dir is None:
            raise RuntimeError("MCP requires an owner-bound Extension host")
        self.host = host
        self.store = ConnectionStore(host.state_dir)
        self.content = ContentStore(host)
        self.connectors = await run_tool_worker(load_connectors)
        for issue in self.connectors.issues:
            self.api.logger.warning(
                "MCP catalog entry left out: %s", json.dumps(issue, ensure_ascii=True)
            )
        try:
            self.connections = await run_tool_worker(self.store.load)
        except (ValueError, OSError) as error:
            self._startup_error = str(error)
            self.api.logger.warning("MCP configuration could not be loaded: %s", error)
            return
        for issue in self.store.issues:
            self.api.logger.warning(
                "MCP configuration issue: %s", json.dumps(issue, ensure_ascii=True)
            )
        # Saved summaries first, so each connection Tool starts with its known names.
        await self.catalogs.open(host)
        self.recovery = TaskRecovery(host, self.api.logger)
        await self.recovery.open()
        if self.connections:
            await _load_protocol_client()
        for config in self.connections.values():
            self._runner(config)
            if config["enabled"]:
                self._runner(config).start()
        await self.recovery.resume(self.runners)

    async def close(self) -> None:
        self._closed = True
        if self.recovery is not None:
            await self.recovery.close()
        await self.jobs.close()
        await asyncio.gather(*(runner.close() for runner in self.runners.values()))
        self.runners.clear()
        await self.catalogs.close()

    def _inputs_changed(self, request_id: str) -> None:
        """Tell accessors that the pending inputs gained or lost *request_id*."""
        self._changed(PENDING_INPUTS_RESOURCE, [request_id])

    def _connection_changed(self, *identifiers: str) -> None:
        """Tell accessors that what ``status`` reports for *identifiers* changed."""
        self._changed(CONNECTIONS_RESOURCE, list(identifiers))

    def _job_finished(self, job_id: str) -> None:
        self._changed(JOBS_RESOURCE, [job_id])

    def _changed(self, resource: str, ids: list[str]) -> None:
        host = self.host
        if self._closed or host is None or host.publish_change is None:
            return
        self._revision += 1
        try:
            host.publish_change(resource, ids, self._revision)
        except ValueError:
            # The registration retired for a reload or disable, which
            # invalidates every Extension surface itself.
            self.api.logger.debug("MCP %s change not published: registration retired", resource)

    @asynccontextmanager
    async def _connection_lock(self, identifier: str) -> AsyncIterator[None]:
        """Hold *identifier*'s runner lock; it is dropped once nobody holds or awaits it."""
        entry = self._runner_locks.setdefault(identifier, _ConnectionLock(asyncio.Lock()))
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if not entry.users:
                del self._runner_locks[identifier]

    def _host(self) -> ExtensionHost:
        if self.host is None or self._closed:
            raise ValueError("MCP Extension is not running")
        return self.host

    def _runner(self, config: dict[str, Any]) -> ConnectionRunner:
        from .client import ConnectionRunner

        identifier = config["id"]
        if identifier not in self.runners:
            self.runners[identifier] = ConnectionRunner(
                config,
                self._host(),
                self.inputs,
                self._publish,
                authorize=self._authorize,
                recovery=self.recovery,
                on_change=lambda: self._connection_changed(identifier),
            )
            self._publish(self.runners[identifier], None)
        return self.runners[identifier]

    def _connection(self, identifier: str) -> dict[str, Any]:
        config = self.connections.get(identifier)
        if config is None:
            raise ValueError(f"Unknown MCP connection: {identifier}")
        return config

    def _publish(self, runner: ConnectionRunner, catalog: dict[str, Any] | None) -> None:
        """Publish the connection Tool and the remote Tools of *catalog*.

        ``None`` means no catalog has arrived yet on this runner: ``target`` keeps
        listing the last known Tool names and no remote Tool is registered.
        """
        if self._closed or self.runners.get(runner.id) is not runner:
            return
        removed = runner.id not in self.connections
        if catalog is not None and not removed:
            self.catalogs.record(runner.id, catalog_summary(catalog))
            self._connection_changed(runner.id)
        parent = f"mcp_{runner.id}"
        about = self.connections.get(runner.id, runner.config).get("description")
        unavailable = MCP_MESSAGES["removed" if removed else "disabled"].format(
            connection=runner.id
        )
        # A remote Tool is ready while its connection is up; naming one before
        # tells the Agent why it cannot run and what to do. A disconnected one
        # can run again through the connection Tool, which reconnects.
        enabled = bool(self.connections.get(runner.id, {}).get("enabled"))
        follower_hint = (
            MCP_MESSAGES["disconnected"].format(connection=runner.id) if enabled else unavailable
        )
        normalizer = browse_normalizer(parent)
        declarations = [
            {
                "name": parent,
                "description": self.catalogs.description(runner.id, about),
                "summary": self.catalogs.summary(runner.id, about),
                "parameters": self.catalogs.parameters(runner.id),
                "handler": self._handler(runner.id),
                "ready": lambda: bool(self.connections.get(runner.id, {}).get("enabled")),
                "readiness_hint": unavailable,
                "parallel_safe": False,
                "open_input_schema": True,
                "requires_opt_in": True,
                "argument_normalizer": normalizer,
                "display": connection_display(normalizer),
                "announce_changes": ["parameters.target"],
                # Secret values leave the machine to this server only where allowed.
                "secret_sinks": {"arguments": "external_tools"},
            }
        ]
        for tool in (catalog or {}).get("tools", []):
            description = tool.get("description") or tool.get("title") or tool["name"]
            parameters = tool["inputSchema"]
            declarations.append(
                {
                    "name": remote_tool_name(runner.id, tool["name"]),
                    "description": description,
                    "parameters": parameters,
                    "handler": self._handler(runner.id, tool["name"], copy.deepcopy(parameters)),
                    "ready": lambda: enabled and runner.state == "connected",
                    "readiness_hint": follower_hint,
                    "readiness_retryable": enabled,
                    "parallel_safe": False,
                    "open_input_schema": True,
                    "deferred": True,
                    "catalog_visible": False,
                    "activation": "follows",
                    "activation_source": parent,
                    "secret_sinks": dict.fromkeys(
                        declared_parameters(parameters)[0], "external_tools"
                    ),
                }
            )
        self.api.operations.replace_tools(runner.id, declarations)

    def _gone(self, connection: str) -> dict[str, Any] | None:
        """The failure for a call that finds the Extension stopped or *connection* removed.

        Both leave nothing to run; checked before any effect so neither reads as
        an access denial or an unknown outcome.
        """
        if self.host is None or self._closed:
            return tool_failure("mcp_request_failed", MCP_MESSAGES["not_running"], retryable=True)
        if connection not in self.connections:
            return tool_failure(
                "mcp_request_failed",
                MCP_MESSAGES["removed"].format(connection=connection),
                retryable=False,
            )
        return None

    async def _authorize(self, context: ToolContext) -> None:
        """Recheck the ordinary Tool policy when a queued invocation begins."""
        if context.tool_name not in await self._allowed(context):
            raise ValueError(MCP_MESSAGES["access_denied"])

    def _handler(
        self, connection: str, remote: str | None = None, schema: dict[str, Any] | None = None
    ) -> Any:
        async def invoke(context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
            # Select a runner only after an admitted configuration change has
            # finished closing the previous one and publishing its replacement.
            async with self._connection_lock(connection):
                gone = self._gone(connection)
                if gone is not None:
                    return gone
                config = self._connection(connection)
                if not config["enabled"]:
                    return tool_failure(
                        "mcp_access_denied",
                        MCP_MESSAGES["disabled"].format(connection=connection),
                        retryable=False,
                    )
                try:
                    await self._authorize(context)
                except ValueError:
                    return tool_failure("mcp_access_denied", MCP_MESSAGES["access_denied"])
                runner = self._runner(config)
            if remote is None:
                try:
                    return await self._browse(runner, context, arguments)
                except ValueError as error:
                    return tool_failure("mcp_request_failed", str(error))
            current = next(
                (tool for tool in runner.catalog.get("tools", []) if tool["name"] == remote),
                None,
            )
            if (
                current is None
                or current["inputSchema"] != schema
                or (
                    context.input_contract is not None
                    and context.input_contract.input_schema != schema
                )
            ):
                return tool_failure(
                    "mcp_tool_changed",
                    MCP_MESSAGES["tool_changed"].format(
                        tool=remote,
                        describe=compact(describe_call(f"tool:{remote}")),
                        connection=connection,
                    ),
                )
            return await self._call(
                runner,
                context,
                "tools/call",
                {"name": remote, "arguments": arguments},
                source=remote,
            )

        return invoke

    async def _allowed(self, context: ToolContext) -> tuple[str, ...]:
        host = self._host()
        agent = (
            await host.resolve_tool_agent_async(context)
            if host.resolve_tool_agent_async is not None
            else await run_tool_worker(host.resolve_tool_agent, context)
        )
        registry = self.api.operations.tool_registry
        if registry is None:
            raise RuntimeError("MCP Tools are not bound")
        allowed = resolve_tool_access(
            agent.tool_access,
            registry.list_tools(),
            agent.memory_prompt_mode,
            workspace=agent.workspace,
        ).allowed_tools
        return tuple(
            name
            for name in allowed
            if (context.tool_restriction is None or name in context.tool_restriction)
            and (context.tool_denial_resolver is None or context.tool_denial_resolver(name) is None)
        )

    async def _browse(
        self, runner: ConnectionRunner, context: ToolContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Run one normalized search, describe, call, or read of the connection Tool."""
        if self.content is None:
            raise RuntimeError("MCP content store was not initialized")
        allowed = await self._allowed(context)
        if f"mcp_{runner.id}" not in allowed:
            return tool_failure("mcp_access_denied", MCP_MESSAGES["access_denied"])
        action = arguments["action"]
        if action == "read":
            return await self._read(runner, context, arguments, allowed)
        if not runner.catalog or runner.state != "connected":
            try:
                await runner.invoke("catalog", {})
            except ValueError as error:
                return self._not_sent(runner, error)
            # Reconnecting can publish new Tools; resolve followers against that catalog.
            gone = self._gone(runner.id)
            if gone is not None:
                return gone
            allowed = await self._allowed(context)
            if not self._connection(runner.id)["enabled"]:
                return tool_failure(
                    "mcp_access_denied",
                    MCP_MESSAGES["disabled"].format(connection=runner.id),
                    retryable=False,
                )
            if f"mcp_{runner.id}" not in allowed:
                return tool_failure("mcp_access_denied", MCP_MESSAGES["access_denied"])
        entries = catalog_entries(runner.id, runner.catalog, allowed)
        if action == "search":
            return self._search(runner, context, arguments, entries)
        if action == "describe":
            return await self._describe(runner, context, arguments["targets"], entries, allowed)
        entry, note, failure = self._resolve(runner, entries, arguments, allowed)
        if failure is not None:
            return failure
        assert entry is not None
        result = await self._call_target(runner, context, entry, arguments, allowed)
        return result if note is None else noted(result, note)

    def _resolve(
        self,
        runner: ConnectionRunner,
        entries: list[dict[str, Any]],
        arguments: dict[str, Any],
        allowed: tuple[str, ...],
    ) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
        entry, note, failure = resolve(entries, arguments)
        if (
            failure is not None
            and failure["error"]["code"] == "mcp_unknown_target"
            and names_denied_tool(runner.id, runner.catalog, arguments["target"], allowed)
        ):
            failure = tool_failure("mcp_access_denied", MCP_MESSAGES["access_denied"])
        return entry, note, failure

    async def _describe(
        self,
        runner: ConnectionRunner,
        context: ToolContext,
        targets: list[str],
        entries: list[dict[str, Any]],
        allowed: tuple[str, ...],
    ) -> dict[str, Any]:
        outcomes: list[DescribeOutcome] = []
        shown: set[str] = set()
        for target in targets:
            arguments = {"action": "describe", "target": target}
            entry, note, failure = self._resolve(runner, entries, arguments, allowed)
            if entry is not None:
                # Two spellings of one item show it once.
                if entry["target"] in shown:
                    continue
                shown.add(entry["target"])
            outcomes.append((target, entry, note, failure))
        if len(outcomes) == 1:
            _, entry, note, failure = outcomes[0]
            if failure is not None:
                return failure
            assert entry is not None
            source = entry["name"] if entry["kind"] == "tool" else None
            return await self._present(
                runner, context, describe_payload(entry, note), source=source
            )
        failures = [outcome[3] for outcome in outcomes if outcome[3] is not None]
        if len(failures) == len(outcomes):
            codes = {failure["error"]["code"] for failure in failures}
            problems = "\n".join(
                f"- {target}: {failure['error']['message']}"
                for target, _, _, failure in outcomes
                if failure is not None
            )
            return tool_failure(
                codes.pop() if len(codes) == 1 else "mcp_unknown_target",
                MCP_MESSAGES["describe_failed"].format(problems=problems),
            )
        return tool_success(describe_page(outcomes, complete=not context.result_payloads_available))

    async def _read(
        self,
        runner: ConnectionRunner,
        context: ToolContext,
        arguments: dict[str, Any],
        allowed: tuple[str, ...],
    ) -> dict[str, Any]:
        assert self.content is not None
        try:
            document = (
                await self.recovery.load_result(arguments["result_id"], context, runner.id)
                if self.recovery is not None
                else None
            )
            if document is None:
                document = await self.content.load_result(
                    arguments["result_id"], context, runner.id
                )
        except ValueError as error:
            return tool_failure("invalid_arguments", str(error))
        if document["source"] and remote_tool_name(runner.id, document["source"]) not in allowed:
            return tool_failure("mcp_access_denied", MCP_MESSAGES["access_denied"])
        try:
            page = await run_tool_worker(self.content.read_result, document, arguments)
        except ValueError as error:
            return tool_failure("invalid_arguments", str(error))
        return tool_success(page)

    def _not_sent(self, runner: ConnectionRunner, error: Exception) -> dict[str, Any]:
        """The failure for a request that never ran: refused by the server, or not sent."""
        from .client import InvocationNotSentError

        detail = runner.redact(str(error))[:_DETAIL_CHARACTERS]
        refused = error.refused if isinstance(error, InvocationNotSentError) else None
        if refused is not None:
            message = "rate_limited" if refused == HTTPStatus.TOO_MANY_REQUESTS else "refused"
            return tool_failure(
                "mcp_request_refused",
                MCP_MESSAGES[message].format(connection=runner.id, detail=detail),
                retryable=message == "rate_limited",
            )
        return tool_failure(
            "mcp_request_failed",
            MCP_MESSAGES["unreachable"].format(connection=runner.id, detail=detail),
            retryable=True,
        )

    def _search(
        self,
        runner: ConnectionRunner,
        context: ToolContext,
        arguments: dict[str, Any],
        entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        content = self.content
        assert content is not None

        def attach(payload: dict[str, Any]) -> str:
            return content.attach(payload, context, runner.id)

        instructions = runner.catalog.get("instructions") or ""
        return tool_success(
            search_page(
                runner.id,
                instructions,
                entries,
                arguments,
                # Outside a Session nothing reads a saved result later: return everything.
                attach if context.result_payloads_available else None,
            )
        )

    async def _call_target(
        self,
        runner: ConnectionRunner,
        context: ToolContext,
        entry: dict[str, Any],
        arguments: dict[str, Any],
        allowed: tuple[str, ...],
    ) -> dict[str, Any]:
        if entry["kind"] == "connection":
            return tool_failure("invalid_arguments", MCP_MESSAGES["call_invalid"])
        inputs, problem = target_arguments(entry, arguments.get("arguments", {}))
        if problem is not None:
            return invalid_target_arguments(entry, runner.redact(problem))
        if entry["kind"] == "tool":
            registry = self.api.operations.tool_registry
            if registry is None:
                raise RuntimeError("MCP Tools are not bound")
            return await registry.dispatch(
                replace(
                    context,
                    tool_name=remote_tool_name(runner.id, entry["name"]),
                    input_contract=None,
                ),
                inputs,
                allowed,
            )
        if entry["kind"] == "resource":
            inputs = {"uri": entry["definition"]["uri"]}
        elif entry["kind"] == "prompt":
            inputs = {"name": entry["name"], "arguments": inputs}
        return await self._call(runner, context, entry_operation(entry), inputs)

    async def _call(
        self,
        runner: ConnectionRunner,
        context: ToolContext,
        operation: str,
        arguments: dict[str, Any],
        *,
        source: str | None = None,
    ) -> dict[str, Any]:
        from ._tasks import TaskEndedError
        from .client import (
            READ_OPERATIONS,
            InvalidToolResultError,
            InvocationNotSentError,
            SubscriptionEndedError,
            UnsupportedOperationError,
        )

        try:
            payload = await runner.invoke(operation, arguments, context)
        except UnsupportedOperationError:
            return unsupported_operation(operation)
        except InvocationNotSentError as error:
            if error.denied:
                return tool_failure("mcp_access_denied", MCP_MESSAGES["access_denied"])
            return self._not_sent(runner, error)
        except InvalidToolResultError as error:
            return await self._invalid_result(runner, context, error, source=source)
        except SubscriptionEndedError as error:
            return tool_failure("mcp_subscription_ended", str(error), retryable=False)
        except TaskEndedError as error:
            detail = runner.redact(str(error))[:_DETAIL_CHARACTERS]
            return tool_failure("mcp_task_ended", MCP_MESSAGES["task_ended"].format(detail=detail))
        except ValueError as error:
            detail = runner.redact(str(error))[:_DETAIL_CHARACTERS]
            if operation in READ_OPERATIONS:
                return tool_failure(
                    "mcp_call_unconfirmed",
                    MCP_MESSAGES["read_unconfirmed"].format(detail=detail),
                    retryable=True,
                )
            return tool_failure(
                "mcp_call_unconfirmed", MCP_MESSAGES["unconfirmed"].format(detail=detail)
            )
        try:
            return await self._present(runner, context, payload, source=source)
        except (ValueError, OSError) as error:
            detail = runner.safe_error(error)
            self.api.logger.warning(
                "MCP result preparation failed (connection=%s): %s", runner.id, detail
            )
            return tool_failure(
                "mcp_result_unavailable", MCP_MESSAGES["result_unavailable"].format(detail=detail)
            )

    async def _invalid_result(
        self,
        runner: ConnectionRunner,
        context: ToolContext,
        error: InvalidToolResultError,
        *,
        source: str | None,
    ) -> dict[str, Any]:
        """The failure for a Tool whose result fails the output schema it declares."""
        assert self.content is not None
        try:
            text, artifacts = await self.content.error_report(
                error.payload, context, runner.id, source=source
            )
        except (ValueError, OSError) as failure:
            detail = runner.safe_error(failure)
            self.api.logger.warning(
                "MCP result preparation failed (connection=%s): %s", runner.id, detail
            )
            return tool_failure(
                "mcp_result_unavailable", MCP_MESSAGES["result_unavailable"].format(detail=detail)
            )
        return tool_failure(
            "mcp_invalid_result",
            MCP_MESSAGES["invalid_result"].format(
                tool=source or "operation",
                problem=error.problem,
                text=text,
                connection=runner.id,
            ),
            artifacts=artifacts,
        )

    async def _present(
        self,
        runner: ConnectionRunner,
        context: ToolContext,
        payload: dict[str, Any],
        *,
        source: str | None = None,
    ) -> dict[str, Any]:
        if self.content is None:
            raise RuntimeError("MCP content store was not initialized")
        if payload.get("isError"):
            text, artifacts = await self.content.error_report(
                payload, context, runner.id, source=source
            )
            return tool_failure(
                "mcp_tool_error",
                MCP_MESSAGES["tool_error"].format(
                    item=f"tool {source}" if source else "operation", text=text
                ),
                artifacts=artifacts,
            )
        result, artifacts = await self.content.present(payload, context, runner.id, source=source)
        return tool_success(result, artifacts=artifacts)

    async def _save(
        self,
        value: dict[str, Any],
        *,
        replace: bool = True,
        credentials: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        config = validate_connection(value)
        identifier = config["id"]
        # The connection's lock spans the replacement of its runner; the saved
        # connections are locked only while they change, so closing this runner
        # never holds up changes to other connections.
        async with self._connection_lock(identifier):
            async with self._lock:
                previous = self.connections.get(identifier)
                if previous is not None and not replace:
                    raise ValueError(f"MCP connection {identifier} already exists")
                # Import credentials belong to the admitted connection save.
                # A competing import that loses this id must leave them alone.
                if credentials:
                    self._check_import_credentials(credentials)
                written: dict[str, str] = {}
                saving = False
                try:
                    for name, secret in (credentials or {}).items():
                        if secret:
                            written[name] = secret
                            self._host().set_credential(name, secret)
                    saving = True
                    await self._store_connections({**self.connections, identifier: config})
                except BaseException:
                    if written:
                        await self._rollback_import_credentials(written, check_saved=saving)
                    raise
            runner = self.runners.get(identifier)
            if (
                runner is not None
                and previous is not None
                and _without_description(previous) == _without_description(config)
            ):
                # Only the description changed: republish it and keep the connection.
                runner.config = config
                self._publish(runner, runner.catalog or None)
            else:
                await self._stop(identifier)
                if previous is not None and _signs_in_elsewhere(previous, config):
                    self.management._forget_sign_in(identifier)
                # A disabled connection keeps its connection Tool, not ready.
                runner = self._runner(config)
                if config["enabled"]:
                    runner.start()
        if self.recovery is not None:
            await self.recovery.resume(self.runners)
        self.api.logger.info("MCP connection configured (connection=%s)", identifier)
        return self.management._status(identifier)

    def _check_import_credentials(self, names: Iterable[str]) -> None:
        """Refuse generated names that would share another connection's credential."""
        used = {name for config in self.connections.values() for name in _credential_names(config)}
        host = self._host()
        for name in names:
            if name in used or host.resolve_credential(name):
                raise ValueError(
                    f"MCP import credential {name} is already in use; "
                    "choose a different connection id"
                )
            used.add(name)

    async def _rollback_import_credentials(
        self, written: dict[str, str], *, check_saved: bool
    ) -> None:
        """Clear only newly written, unreferenced values after a failed import save."""
        records = self.connections
        if check_saved:
            # A worker may have committed before raising or propagating cancellation.
            # The durable references decide which credentials must be retained.
            assert self.store is not None
            try:
                records = await run_tool_worker(self.store.load)
            except Exception as error:
                self.api.logger.warning(
                    "MCP import credentials kept: saved connections could not be checked "
                    "(error_type=%s)",
                    type(error).__name__,
                )
                return
        used = {name for config in records.values() for name in _credential_names(config)}
        for name, secret in written.items():
            try:
                host = self._host()
                if name not in used and host.resolve_credential(name) == secret:
                    host.set_credential(name, "")
            except Exception as error:
                self.api.logger.warning(
                    "MCP unused import credential could not be cleared (error_type=%s)",
                    type(error).__name__,
                )

    async def _mutate(self, operation: str, original: dict[str, Any]) -> dict[str, Any]:
        identifier = original["id"]
        async with self._connection_lock(identifier):
            async with self._lock:
                config = copy.deepcopy(self._connection(identifier))
                records = dict(self.connections)
                if operation == "remove":
                    records.pop(identifier)
                elif operation in {"enable", "disable"}:
                    config["enabled"] = operation == "enable"
                    records[identifier] = config
                await self._store_connections(records)
            if operation == "remove":
                # Closing can yield while the old Tools are still registered.
                # Publish the actual refusal before any concurrent call sees them.
                runner = self.runners.get(identifier)
                if runner is not None:
                    self._publish(runner, runner.catalog or None)
                self.catalogs.forget(identifier)
                await self._stop(identifier)
                self.management._forget_sign_in(identifier)
            elif operation == "disable":
                # Its Tools stay registered but not ready, so the Agent no longer
                # sees them and naming one says the connection is disabled.
                await self._replace_runner(identifier, config)
            else:
                runner = self._runner(config)
                # Republished, its Tools no longer say the connection is disabled.
                self._publish(runner, runner.catalog or None)
                runner.start()
        if self.recovery is not None:
            await self.recovery.resume(self.runners)
        self.api.logger.info(
            "MCP connection updated (connection=%s operation=%s)", identifier, operation
        )
        return (
            {"id": identifier, "removed": True}
            if operation == "remove"
            else self.management._status(identifier)
        )

    async def _store_connections(self, records: dict[str, dict[str, Any]]) -> None:
        """Save *records* and make them the current connections; the caller holds ``_lock``."""
        if self.store is None:
            raise RuntimeError("MCP store was not initialized")
        if records:
            await _load_protocol_client()
        await run_tool_worker(self.store.save, records)
        changed = [
            identifier
            for identifier in {**self.connections, **records}
            if self.connections.get(identifier) != records.get(identifier)
        ]
        self.connections = records
        if changed:
            self._connection_changed(*changed)

    async def _replace_runner(self, identifier: str, config: dict[str, Any]) -> ConnectionRunner:
        """Close *identifier*'s client and give the connection a new, unstarted runner.

        The connection's Tools stay registered with the last catalog but are not
        ready until the new runner connects; naming one tells the Agent why.
        """
        previous = self.runners.get(identifier)
        await self._stop(identifier)
        runner = self._runner(config)
        if previous is not None and previous.catalog:
            runner.catalog = previous.catalog
            self._publish(runner, runner.catalog)
        return runner

    async def _stop(self, identifier: str) -> None:
        runner = self.runners.pop(identifier, None)
        if self.recovery is not None:
            await self.recovery.stop_connection(identifier, runner=runner)
        try:
            if runner is not None:
                await runner.close()
        finally:
            if self.recovery is not None and identifier not in self.connections:
                await self.recovery.stop_connection(identifier, remove=True)
            self.api.operations.replace_tools(identifier, [])


def _without_description(config: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in config.items() if key != "description"}


def _signs_in_elsewhere(previous: dict[str, Any], config: dict[str, Any]) -> bool:
    """Whether a sign-in stored for *previous* no longer belongs to *config*."""
    return (
        not config.get("oauth")
        or any(previous.get(key) != config.get(key) for key in ("url", "oauth_client_id"))
        or (
            not config.get("oauth_client_id")
            and previous.get("oauth_client_metadata_url") != config.get("oauth_client_metadata_url")
        )
    )


def register(api: ExtensionAPI) -> None:
    service = MCPService(api)
    api.operations.startup.append(service.start)
    api.operations.pending_inputs = service.inputs.list
    api.operations.input_response_operation = "respond"
    api.on_shutdown(service.close)
    api.register_prompt_block("mcp_guidance", default_text=MCP_GUIDANCE, requires_tool="mcp_*")
    register_management(api, service.management.manage)
