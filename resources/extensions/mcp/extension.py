"""Bundled MCP integration: connection management and ordinary vBot Tools.

``MCPService`` owns the saved connections, one ``ConnectionRunner`` per
connection, and the Tools each connection publishes: the connection Tool
``mcp_<id>`` and one deferred follower per remote Tool. Finding and describing
catalog items lives in ``_discovery``, the management surface in
``_management``, and the protocol client in ``client``.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any

from core.extensions import ExtensionAPI
from core.extensions.operations import PENDING_INPUTS_RESOURCE, ExtensionHost
from core.tools.availability import resolve_tool_access
from core.tools.tools import (
    ToolContext,
    run_tool_worker,
    tool_failure,
    tool_success,
)

from ._arguments import browse_normalizer
from ._catalog import CatalogSummaries, catalog_summary
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
)
from ._discovery import (
    catalog_entries,
    describe_payload,
    entry_operation,
    invalid_target_arguments,
    names_denied_tool,
    noted,
    remote_tool_name,
    resolve,
    search_entries,
    search_page,
    summarize,
    target_arguments,
)
from ._management import ManagementJobs, check_connection, invoke_for_agent, register_management
from ._oauth import forget_sign_in, sign_in_status
from ._views import compact
from .client import READ_OPERATIONS, ConnectionRunner, InvocationNotSentError
from .config import ConnectionStore, validate_connection
from .content import ContentStore
from .interactions import InputRequests

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


@dataclass
class _ConnectionLock:
    lock: asyncio.Lock
    users: int = 0


class MCPService:
    def __init__(self, api: ExtensionAPI) -> None:
        self.api = api
        self.host: ExtensionHost | None = None
        self.store: ConnectionStore | None = None
        self.content: ContentStore | None = None
        self.connections: dict[str, dict[str, Any]] = {}
        self.runners: dict[str, ConnectionRunner] = {}
        self.inputs = InputRequests(on_change=self._inputs_changed)
        self._inputs_revision = 0
        self.jobs = ManagementJobs(self.inputs)
        # Serializes changes to the saved connections; held only while they change.
        self._lock = asyncio.Lock()
        # Per connection while in use: runner changes and the calls that select a runner.
        self._runner_locks: dict[str, _ConnectionLock] = {}
        # Per connection, the server title and Tool names its description shows.
        self.catalogs = CatalogSummaries(api.logger)
        self._closed = False
        self._startup_error: str | None = None

    async def start(self, host: ExtensionHost) -> None:
        if host.state_dir is None:
            raise RuntimeError("MCP requires an owner-bound Extension host")
        self.host = host
        self.store = ConnectionStore(host.state_dir)
        self.content = ContentStore(host)
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
        for config in self.connections.values():
            self._runner(config)
            if config["enabled"]:
                self._runner(config).start()

    async def close(self) -> None:
        self._closed = True
        await self.jobs.close()
        await asyncio.gather(*(runner.close() for runner in self.runners.values()))
        self.runners.clear()
        await self.catalogs.close()

    def _inputs_changed(self, request_id: str) -> None:
        """Tell accessors that the pending inputs gained or lost *request_id*."""
        host = self.host
        if self._closed or host is None or host.publish_change is None:
            return
        self._inputs_revision += 1
        try:
            host.publish_change(PENDING_INPUTS_RESOURCE, [request_id], self._inputs_revision)
        except ValueError:
            # The registration retired for a reload or disable, which
            # invalidates every Extension surface itself.
            self.api.logger.debug("Pending input change not published: registration retired")

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
        identifier = config["id"]
        if identifier not in self.runners:
            self.runners[identifier] = ConnectionRunner(
                config, self._host(), self.inputs, self._publish, authorize=self._authorize
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

        ``None`` means no catalog has arrived yet on this runner: the description
        keeps the last known Tool names and no remote Tool is registered.
        """
        if self._closed or self.runners.get(runner.id) is not runner:
            return
        if catalog is not None:
            self.catalogs.record(runner.id, catalog_summary(catalog))
        parent = f"mcp_{runner.id}"
        about = self.connections.get(runner.id, runner.config).get("description")
        declarations = [
            {
                "name": parent,
                "description": self.catalogs.description(runner.id, about),
                "parameters": MCP_PARAMETERS,
                "handler": self._handler(runner.id),
                "ready": lambda: bool(self.connections.get(runner.id, {}).get("enabled")),
                "parallel_safe": False,
                "open_input_schema": True,
                "requires_opt_in": True,
                "argument_normalizer": browse_normalizer(parent),
                "definition_change_note": self.catalogs.change_note(runner.id),
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
                    "ready": lambda: runner.state == "connected",
                    "parallel_safe": False,
                    "open_input_schema": True,
                    "deferred": True,
                    "catalog_visible": False,
                    "activation": "follows",
                    "activation_source": parent,
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

    def _authorize(self, context: ToolContext) -> None:
        """Recheck the ordinary Tool policy when a queued invocation begins."""
        if context.tool_name not in self._allowed(context):
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
                    self._authorize(context)
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
                        describe=compact({"action": "describe", "target": f"tool:{remote}"}),
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

    def _allowed(self, context: ToolContext) -> tuple[str, ...]:
        agent = self._host().resolve_tool_agent(context)
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
        allowed = self._allowed(context)
        if f"mcp_{runner.id}" not in allowed:
            return tool_failure("mcp_access_denied", MCP_MESSAGES["access_denied"])
        action = arguments["action"]
        if action == "read":
            return await self._read(runner, context, arguments, allowed)
        if not runner.catalog or runner.state != "connected":
            try:
                await runner.invoke("catalog", {})
            except ValueError as error:
                return self._unreachable(runner, error)
            # Reconnecting can publish new Tools; resolve followers against that catalog.
            gone = self._gone(runner.id)
            if gone is not None:
                return gone
            allowed = self._allowed(context)
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
        entry, note, failure = resolve(entries, arguments)
        if failure is not None:
            if failure["error"]["code"] == "mcp_unknown_target" and names_denied_tool(
                runner.id, runner.catalog, arguments["target"], allowed
            ):
                return tool_failure("mcp_access_denied", MCP_MESSAGES["access_denied"])
            return failure
        assert entry is not None
        if action == "describe":
            source = entry["name"] if entry["kind"] == "tool" else None
            return await self._present(
                runner, context, describe_payload(entry, note), source=source
            )
        result = await self._call_target(runner, context, entry, arguments, allowed)
        return result if note is None else noted(result, note)

    async def _read(
        self,
        runner: ConnectionRunner,
        context: ToolContext,
        arguments: dict[str, Any],
        allowed: tuple[str, ...],
    ) -> dict[str, Any]:
        assert self.content is not None
        try:
            document = await self.content.load_result(arguments["result_id"], context, runner.id)
        except ValueError as error:
            return tool_failure("invalid_arguments", str(error))
        if document["source"] and remote_tool_name(runner.id, document["source"]) not in allowed:
            return tool_failure("mcp_access_denied", MCP_MESSAGES["access_denied"])
        try:
            page = await run_tool_worker(self.content.read_result, document, arguments)
        except ValueError as error:
            return tool_failure("invalid_arguments", str(error))
        return tool_success(page)

    def _unreachable(self, runner: ConnectionRunner, error: Exception) -> dict[str, Any]:
        detail = runner.redact(str(error))[:_DETAIL_CHARACTERS]
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
        try:
            payload = await runner.invoke(operation, arguments, context)
        except InvocationNotSentError as error:
            if error.denied:
                return tool_failure("mcp_access_denied", MCP_MESSAGES["access_denied"])
            return self._unreachable(runner, error)
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

    async def manage(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._host()
        if self._startup_error is not None:
            raise ValueError(self._startup_error)
        if operation == "list":
            return {
                "connections": [self._status(identifier) for identifier in self.connections],
                "configuration_issues": copy.deepcopy(self.store.issues) if self.store else [],
            }
        if operation == "requests":
            return {"requests": self.inputs.list()}
        if operation == "respond":
            return self.inputs.respond(arguments["request_id"], arguments["response"])
        if operation == "job":
            return self.jobs.status(arguments["job_id"])
        if operation == "cancel-job":
            return await self.jobs.cancel(arguments["job_id"])
        if operation == "save":
            return await self._save(arguments["connection"])
        identifier = arguments["id"]
        config = self._connection(identifier)
        if operation == "status":
            return self._status(identifier)
        if operation == "inspect":
            return self._inspect(identifier, arguments)
        if operation in {"enable", "disable", "remove"}:
            return await self._mutate(operation, config)
        async with self._connection_lock(identifier):
            config = self._connection(identifier)
            if operation == "credential":
                sources = set(config.get("credential_environment", {}).values()) | set(
                    config.get("credential_headers", {}).values()
                )
                if config.get("oauth_client_secret"):
                    sources.add(config["oauth_client_secret"])
                if arguments["key"] not in sources:
                    raise ValueError("Credential must be referenced by this MCP connection")
                self._host().set_credential(arguments["key"], arguments["value"])
                # The variable name only: never its value.
                self.api.logger.info(
                    "MCP connection credential %s (connection=%s variable=%s)",
                    "set" if arguments["value"] else "cleared",
                    identifier,
                    arguments["key"],
                )
                await self._stop(identifier)
                self._runner(config)
                return {
                    "id": identifier,
                    "credential": arguments["key"],
                    "set": bool(arguments["value"]),
                }
            if operation == "reauthorize":
                if not config.get("oauth"):
                    raise ValueError("MCP connection does not sign in with OAuth")
                await self._stop(identifier)
                forget_sign_in(self._host(), identifier)
                self.api.logger.info("MCP connection signed out (connection=%s)", identifier)
                runner = self._runner(config)
                if config["enabled"]:
                    runner.start()
                return self._status(identifier)
            if operation == "disconnect":
                previous = self.runners.get(identifier)
                await self._stop(identifier)
                self._runner(config)
                if previous is not None and previous.state != "disconnected":
                    self.api.logger.info("MCP connection disconnected (connection=%s)", identifier)
                return self._status(identifier)
            if not config["enabled"]:
                raise ValueError("MCP connection is disabled")
            runner = self._runner(config)
            if operation == "connect":
                if runner.state not in {"connecting", "connected"}:
                    # A connection that then fails logs its own WARNING.
                    self.api.logger.info("MCP connection started (connection=%s)", identifier)
                runner.start()
                return self._status(identifier)
            if operation == "events":
                return runner.events(arguments.get("after", 0))
            if operation == "test":
                return self.jobs.start(check_connection(runner))
            if operation in {"invoke", "explore"}:
                return self.jobs.start(
                    invoke_for_agent(self._host(), self.api.operations, runner, arguments)
                )
        raise ValueError(f"Unknown MCP management operation: {operation}")

    def _status(self, identifier: str) -> dict[str, Any]:
        config = self._connection(identifier)
        runner = self.runners.get(identifier)
        status = (
            runner.status()
            if runner is not None
            else {"id": identifier, "state": "disconnected", "error": None}
        )
        result = {
            **status,
            "configuration": copy.deepcopy(config),
            "pending_requests": [
                item for item in self.inputs.list() if item["connection"] == identifier
            ],
        }
        if config.get("oauth"):
            result["oauth"] = sign_in_status(self._host(), config)
        return result

    def _inspect(self, identifier: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """The cached Tool catalog and guidance of a connection, one page of Tools at a time."""
        runner = self.runners.get(identifier)
        catalog = runner.catalog if runner else {}
        entries = catalog_entries(identifier, catalog, None)
        matches = search_entries(entries, arguments.get("query", ""), "tool")
        offset = arguments.get("offset", 0)
        return {
            **self._status(identifier),
            "catalog_available": bool(catalog),
            "tools": [
                {**summarize(item), "description": item["description"]}
                for item in matches[offset : offset + SEARCH_PAGE_SIZE]
            ],
            "total": len(matches),
            "offset": offset,
            "previous_offset": max(0, offset - SEARCH_PAGE_SIZE) if offset else None,
            "next_offset": offset + SEARCH_PAGE_SIZE
            if offset + SEARCH_PAGE_SIZE < len(matches)
            else None,
            "instructions": catalog.get("instructions") or "",
            "prompts": [
                {"name": item["name"], "description": item.get("description", "")}
                for item in catalog.get("prompts", [])
            ],
        }

    async def _save(self, value: dict[str, Any]) -> dict[str, Any]:
        config = validate_connection(value)
        identifier = config["id"]
        # The connection's lock spans the replacement of its runner; the saved
        # connections are locked only while they change, so closing this runner
        # never holds up changes to other connections.
        async with self._connection_lock(identifier):
            async with self._lock:
                previous = self.connections.get(identifier)
                await self._store_connections({**self.connections, identifier: config})
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
                    forget_sign_in(self._host(), identifier)
                if config["enabled"]:
                    self._runner(config).start()
        self.api.logger.info("MCP connection configured (connection=%s)", identifier)
        return self._status(identifier)

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
                self.catalogs.forget(identifier)
            if operation in {"disable", "remove"}:
                await self._stop(identifier)
            if operation == "remove":
                forget_sign_in(self._host(), identifier)
            elif config["enabled"]:
                self._runner(config).start()
        self.api.logger.info(
            "MCP connection updated (connection=%s operation=%s)", identifier, operation
        )
        return (
            {"id": identifier, "removed": True}
            if operation == "remove"
            else self._status(identifier)
        )

    async def _store_connections(self, records: dict[str, dict[str, Any]]) -> None:
        """Save *records* and make them the current connections; the caller holds ``_lock``."""
        if self.store is None:
            raise RuntimeError("MCP store was not initialized")
        await run_tool_worker(self.store.save, records)
        self.connections = records

    async def _stop(self, identifier: str) -> None:
        runner = self.runners.pop(identifier, None)
        try:
            if runner is not None:
                await runner.close()
        finally:
            self.api.operations.replace_tools(identifier, [])


def _without_description(config: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in config.items() if key != "description"}


def _signs_in_elsewhere(previous: dict[str, Any], config: dict[str, Any]) -> bool:
    """Whether a sign-in stored for *previous* no longer belongs to *config*."""
    return not config.get("oauth") or any(
        previous.get(key) != config.get(key) for key in ("url", "oauth_client_id")
    )


def register(api: ExtensionAPI) -> None:
    service = MCPService(api)
    api.operations.startup.append(service.start)
    api.operations.pending_inputs = service.inputs.list
    api.operations.input_response_operation = "respond"
    api.on_shutdown(service.close)
    api.register_prompt_block("mcp_guidance", default_text=MCP_GUIDANCE, requires_tool="mcp_*")
    register_management(api, service.manage)
