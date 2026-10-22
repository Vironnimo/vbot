"""The MCP Extension's management surface for the CLI, WebUI and RPC.

Management operations run outside any Session. Long ones (test, explore,
invoke) run as background jobs that callers poll by id; explore and invoke act
as a named Agent under that Agent's Tool policy and return complete payloads
inline, because no Session could read a saved result later.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.extensions import ExtensionAPI
from core.extensions.operations import ExtensionHost, ExtensionOperations
from core.projects.address import parse_agent_address
from core.tools.availability import resolve_tool_access
from core.tools.tools import ToolContext
from core.utils.config import VBOT_ROOT
from core.utils.ids import new_id

from ._definitions import MAX_FINISHED_JOBS, MCP_OPERATIONS, MCP_PARAMETERS, SEARCH_PAGE_SIZE
from ._discovery import (
    catalog_entries,
    operation_target,
    remote_tool_name,
    search_entries,
    summarize,
)
from ._importer import MAX_SETUP_CHARACTERS, ImportDraft, parse_setup
from .config import CONNECTION_SCHEMA
from .interactions import InputRequests

if TYPE_CHECKING:
    from .client import ConnectionRunner
    from .extension import MCPService

# The resources of the changes the service publishes to accessors
# (``ExtensionHost.publish_change``): what ``list`` and ``status`` report for a
# connection, and a management job that finished.
CONNECTIONS_RESOURCE = "connections"
JOBS_RESOURCE = "jobs"

_DESCRIPTIONS = {
    "list": "List saved connections, live connection state, and effective Agent access.",
    "requests": (
        "List pending server inputs: OAuth sign-ins, sampling approvals and elicitation; "
        "answer with respond."
    ),
    "status": "Read one connection's saved configuration, live state, and Agent access.",
    "remove": "Remove a saved connection and stop its client and published Tools.",
    "enable": "Enable a saved connection and start connecting; inspect status for readiness.",
    "disable": "Disable a saved connection and stop its client and published Tools.",
    "connect": "Start connecting an enabled connection; inspect status for readiness.",
    "disconnect": "Close the current client without disabling the saved connection.",
    "reconnect": (
        "Close and restart an enabled connection, including a local server's process; "
        "inspect status for readiness."
    ),
    "reauthorize": (
        "Sign an OAuth connection out: delete its stored tokens and registered client, "
        "then reconnect an enabled connection, which starts a new sign-in."
    ),
    "test": "Start a catalog/health check; use the returned job_id with job for its outcome.",
    "save": "Create or replace a complete connection; read status before replacing one.",
    "import": (
        "Preview connections from another client's MCP setup, a command line or a URL; "
        "apply saves the selected ones."
    ),
    "catalog": (
        "List hosted MCP services that add_from_catalog adds in one step, with the ids of "
        "saved connections that already reach each one."
    ),
    "add_from_catalog": (
        "Add a catalog service as a new enabled connection under a free id, never replacing "
        "one. An oauth service waits for the user to sign in in a browser (see requests). "
        "No Agent can use the connection until its mcp_<id> Tool is enabled for that Agent."
    ),
    "events": "Read sequenced connection events after a cursor; inspect reported gaps.",
    "inspect": "Read the cached Tool catalog and guidance without connecting or calling Tools.",
    "credential": "Set or clear a referenced credential and reset the client; use JSON stdin.",
    "respond": "Answer one pending input from requests using JSON stdin.",
    "job": "Read a management job's running, completed, failed, or cancelled state and result.",
    "cancel-job": "Cancel a management job; remote effects already performed are not undone.",
    "explore": (
        "Search, describe, or call as an Agent; the job result holds the complete "
        "payload. Inspect the returned job_id with job."
    ),
    "invoke": "Invoke an exact MCP operation as an Agent; inspect the returned job_id with job.",
}

# Management calls run outside a Session and return complete payloads inline,
# so they have no saved result to read.
_EXPLORE_PROPERTIES: dict[str, Any] = {
    **{
        key: value
        for key, value in MCP_PARAMETERS["properties"].items()
        if key not in {"result_id", "pointer", "fields"}
    },
    "action": {
        **MCP_PARAMETERS["properties"]["action"],
        "enum": ["search", "describe", "call"],
        "description": "Search available items, describe targets, or call one.",
    },
}


# ``contentMediaType`` marks ``source`` as a document: the CLI reads it from a
# file or standard input instead of a shell argument.
_IMPORT_PROPERTIES: dict[str, Any] = {
    "source": {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_SETUP_CHARACTERS,
        "contentMediaType": "text/plain",
        "description": (
            "Setup text: an mcpServers, servers or mcp_servers configuration, a single "
            "server object, claude mcp add or another command line, or a server URL."
        ),
    },
    "apply": {
        "type": "boolean",
        "description": "Save the chosen servers; omit to preview without saving.",
    },
    "servers": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Names from the preview to save; omit to save the selected ones.",
    },
    "ids": {
        "type": "object",
        "additionalProperties": {"type": "string"},
        "description": "Connection id per server name, replacing the proposed one.",
    },
}

_ADD_FROM_CATALOG_PROPERTIES: dict[str, Any] = {
    "entry": {"type": "string", "description": "The id of an entry that catalog lists."},
    "read_only": {
        "type": "boolean",
        "description": "Use the service's read-only address; only for entries with read_only_url.",
    },
}

# Required arguments where not every listed property is required.
_REQUIRED: dict[str, list[str]] = {
    "explore": ["id", "agent", "action"],
    "inspect": ["id"],
    "import": ["source"],
    "add_from_catalog": ["entry"],
    "events": ["id"],
    "invoke": ["id", "agent", "operation"],
}


def register_management(
    api: ExtensionAPI, manage: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
) -> None:
    """Register every management operation; each one calls *manage* with its name."""
    base = {"id": {"type": "string"}}
    schemas: dict[str, dict[str, Any]] = {
        **{name: {} for name in ("list", "requests", "catalog")},
        **dict.fromkeys(
            (
                "status",
                "remove",
                "enable",
                "disable",
                "connect",
                "disconnect",
                "reconnect",
                "reauthorize",
                "test",
            ),
            base,
        ),
        "save": {"connection": CONNECTION_SCHEMA},
        "import": _IMPORT_PROPERTIES,
        "add_from_catalog": _ADD_FROM_CATALOG_PROPERTIES,
        "events": {**base, "after": {"type": "integer", "minimum": 0}},
        "inspect": {
            **base,
            "query": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
        },
        "credential": {**base, "key": {"type": "string"}, "value": {"type": "string"}},
        "respond": {"request_id": {"type": "string"}, "response": {"type": "object"}},
        **{name: {"job_id": {"type": "string"}} for name in ("job", "cancel-job")},
        "explore": {**base, "agent": {"type": "string"}, **_EXPLORE_PROPERTIES},
        "invoke": {
            **base,
            "agent": {"type": "string"},
            "operation": {"enum": [*MCP_OPERATIONS, "tools/call"]},
            "arguments": {"type": "object"},
        },
    }
    for name, properties in schemas.items():
        required = _REQUIRED.get(name, list(properties))

        async def handler(arguments: dict[str, Any], operation: str = name) -> dict[str, Any]:
            return await manage(operation, arguments)

        api.operations.register(
            name,
            _DESCRIPTIONS[name],
            {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            handler,
            secret=name in {"credential", "respond"},
        )


class ManagementJobs:
    """Background management jobs; only the newest finished ones are kept.

    *on_finish* receives the id of each job that finished, however it ended.
    """

    def __init__(
        self, inputs: InputRequests, *, on_finish: Callable[[str], None] | None = None
    ) -> None:
        self._inputs = inputs
        self._on_finish = on_finish
        self._tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}

    def start(self, coroutine: Coroutine[Any, Any, dict[str, Any]]) -> dict[str, Any]:
        completed = [identifier for identifier, task in self._tasks.items() if task.done()]
        for identifier in completed[:-MAX_FINISHED_JOBS]:
            self._tasks.pop(identifier)
        identifier = new_id("job", claim=lambda candidate: candidate not in self._tasks)
        self._tasks[identifier] = asyncio.create_task(coroutine, name=f"mcp-job:{identifier}")
        self._tasks[identifier].add_done_callback(_observe)
        if self._on_finish is not None:
            on_finish = self._on_finish
            self._tasks[identifier].add_done_callback(lambda _task: on_finish(identifier))
        return self.status(identifier)

    def status(self, identifier: str) -> dict[str, Any]:
        task = self._tasks.get(identifier)
        if task is None:
            raise ValueError("Unknown MCP management job")
        if not task.done():
            return {"job_id": identifier, "state": "running", "requests": self._inputs.list()}
        if task.cancelled():
            return {"job_id": identifier, "state": "cancelled"}
        error = task.exception()
        if error is not None:
            return {"job_id": identifier, "state": "failed", "error": str(error)}
        result = task.result()
        state = "failed" if result.get("ok") is False else "completed"
        return {"job_id": identifier, "state": state, "result": result}

    async def wait(self, identifier: str) -> dict[str, Any]:
        """The status of the job once it has finished, however it ended."""
        task = self._tasks.get(identifier)
        if task is None:
            raise ValueError("Unknown MCP management job")
        await asyncio.gather(task, return_exceptions=True)
        return self.status(identifier)

    async def cancel(self, identifier: str) -> dict[str, Any]:
        task = self._tasks.get(identifier)
        if task is None:
            raise ValueError("Unknown MCP management job")
        task.cancel()
        return await self.wait(identifier)

    async def close(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)


def _observe(task: asyncio.Task[dict[str, Any]]) -> None:
    if not task.cancelled():
        task.exception()


async def check_connection(runner: ConnectionRunner) -> dict[str, Any]:
    """The test job: load the catalog, then verify the connection is alive."""
    catalog = await runner.invoke("catalog", {})
    health = await runner.invoke("ping", {})
    verified = list(dict.fromkeys(["catalog", health.get("verified", "ping")]))
    return {"status": runner.status(), "catalog": catalog, "verified": verified}


async def invoke_for_agent(
    host: ExtensionHost,
    operations: ExtensionOperations,
    runner: ConnectionRunner,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Run explore or invoke as the named Agent, through its Tool policy and Permission Rules."""
    agent_id, project_id = parse_agent_address(arguments["agent"])
    agent = host.resolve_agent(project_id, agent_id)
    registry = operations.tool_registry
    if registry is None:
        raise RuntimeError("MCP Tools are not bound")
    operation = arguments.get("operation")
    if operation is not None:
        await runner.invoke("catalog", {})
    resolution = resolve_tool_access(
        agent.tool_access,
        registry.list_tools(),
        agent.memory_prompt_mode,
        workspace=agent.workspace,
    )
    inputs = arguments.get("arguments", {})
    name = (
        remote_tool_name(runner.id, inputs["name"])
        if operation == "tools/call"
        else f"mcp_{runner.id}"
    )
    if name not in resolution.allowed_tools:
        raise ValueError("Agent Tool policy does not permit this MCP operation")
    if host.resolve_permissions is None:
        raise RuntimeError("Agent Permission Rules are unavailable")
    # The Agent's Permission Rules confine the call as in a new Session of it,
    # so the roots a server asks for stay within what the Agent may read.
    permissions = await host.resolve_permissions(project_id, agent_id)
    resolve_cwd = host.resolve_cwd
    context = ToolContext(
        agent_id=agent_id,
        project_id=project_id,
        session_id="mcp-management",
        run_id="mcp-management",
        tool_call_id=str(uuid.uuid4()),
        tool_name=name,
        tool_call_index=0,
        workspace=Path(agent.workspace or runner.config.get("cwd") or host.data_dir),
        cwd=resolve_cwd(project_id, agent_id) if resolve_cwd else None,
        vbot_root=VBOT_ROOT,
        data_root=host.data_dir,
        permissions=permissions,
    )
    handler_arguments = (
        inputs.get("arguments", {})
        if operation == "tools/call"
        else (
            {key: value for key, value in arguments.items() if key not in {"id", "agent"}}
            if operation is None
            else {"action": "call", "target": operation_target(operation), "arguments": inputs}
        )
    )
    return await registry.dispatch(context, handler_arguments, resolution.allowed_tools)


def _credential_names(config: dict[str, Any]) -> set[str]:
    """The credentials *config* references: variables, headers and an OAuth client secret."""
    names = {
        *config.get("credential_environment", {}).values(),
        *config.get("credential_headers", {}).values(),
    }
    if config.get("oauth_client_secret"):
        names.add(config["oauth_client_secret"])
    return names


class ConnectionManagement:
    """Management dispatch, status projection and connection onboarding."""

    def __init__(self, service: MCPService) -> None:
        self._service = service

    async def manage(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._service._host()
        if self._service._startup_error is not None:
            raise ValueError(self._service._startup_error)
        if operation == "list":
            return {
                "connections": [
                    self._status(identifier) for identifier in self._service.connections
                ],
                "configuration_issues": copy.deepcopy(self._service.store.issues)
                if self._service.store
                else [],
            }
        if operation == "requests":
            return {"requests": self._service.inputs.list()}
        if operation == "respond":
            return self._service.inputs.respond(arguments["request_id"], arguments["response"])
        if operation == "job":
            return self._service.jobs.status(arguments["job_id"])
        if operation == "cancel-job":
            return await self._service.jobs.cancel(arguments["job_id"])
        if operation == "save":
            return await self._service._save(arguments["connection"])
        if operation == "import":
            return await self._import(arguments)
        if operation == "catalog":
            return {"entries": self._service.connectors.listing(self._service.connections)}
        if operation == "add_from_catalog":
            return await self._add_from_catalog(arguments)
        identifier = arguments["id"]
        config = self._service._connection(identifier)
        if operation == "status":
            return self._status(identifier)
        if operation == "inspect":
            return self._inspect(identifier, arguments)
        if operation in {"enable", "disable", "remove"}:
            return await self._service._mutate(operation, config)
        async with self._service._connection_lock(identifier):
            config = self._service._connection(identifier)
            if operation == "credential":
                if arguments["key"] not in _credential_names(config):
                    raise ValueError("Credential must be referenced by this MCP connection")
                self._service._host().set_credential(arguments["key"], arguments["value"])
                # The variable name only: never its value.
                self._service.api.logger.info(
                    "MCP connection credential %s (connection=%s variable=%s)",
                    "set" if arguments["value"] else "cleared",
                    identifier,
                    arguments["key"],
                )
                await self._service._stop(identifier)
                self._service._runner(config)
                self._service._connection_changed(identifier)
                return {
                    "id": identifier,
                    "credential": arguments["key"],
                    "set": bool(arguments["value"]),
                }
            if operation == "reauthorize":
                if not config.get("oauth"):
                    raise ValueError("MCP connection does not sign in with OAuth")
                await self._service._stop(identifier)
                self._forget_sign_in(identifier)
                self._service._connection_changed(identifier)
                self._service.api.logger.info(
                    "MCP connection signed out (connection=%s)", identifier
                )
                runner = self._service._runner(config)
                if config["enabled"]:
                    runner.start()
                return self._status(identifier)
            if operation == "disconnect":
                previous = self._service.runners.get(identifier)
                await self._service._stop(identifier)
                self._service._runner(config)
                if previous is not None and previous.state != "disconnected":
                    self._service.api.logger.info(
                        "MCP connection disconnected (connection=%s)", identifier
                    )
                return self._status(identifier)
            if not config["enabled"]:
                raise ValueError("MCP connection is disabled")
            runner = self._service._runner(config)
            if operation == "connect":
                if runner.state not in {"connecting", "connected"}:
                    # A connection that then fails logs its own WARNING.
                    self._service.api.logger.info(
                        "MCP connection started (connection=%s)", identifier
                    )
                runner.start()
                return self._status(identifier)
            if operation == "reconnect":
                # A new client and, for a local server, a new process.
                (await self._service._replace_runner(identifier, config)).start()
                self._service.api.logger.info(
                    "MCP connection restarted (connection=%s)", identifier
                )
                return self._status(identifier)
            if operation == "events":
                return runner.events(arguments.get("after", 0))
            if operation == "test":
                return self._service.jobs.start(check_connection(runner))
            if operation in {"invoke", "explore"}:
                return self._service.jobs.start(
                    invoke_for_agent(
                        self._service._host(), self._service.api.operations, runner, arguments
                    )
                )
        raise ValueError(f"Unknown MCP management operation: {operation}")

    def _status(self, identifier: str) -> dict[str, Any]:
        config = self._service._connection(identifier)
        runner = self._service.runners.get(identifier)
        status = (
            runner.status()
            if runner is not None
            else {"id": identifier, "state": "disconnected", "error": None}
        )
        result = {
            **status,
            "configuration": copy.deepcopy(config),
            "missing_credentials": self._missing_credentials(config),
            "pending_requests": [
                item for item in self._service.inputs.list() if item["connection"] == identifier
            ],
        }
        if config.get("oauth"):
            from ._oauth import sign_in_status

            result["oauth"] = sign_in_status(self._service._host(), config)
        return result

    def _forget_sign_in(self, identifier: str) -> None:
        """Delete the stored OAuth tokens and registered client of *identifier*."""
        from ._oauth import forget_sign_in

        forget_sign_in(self._service._host(), identifier)

    def _missing_credentials(self, config: dict[str, Any]) -> list[str]:
        """The credentials *config* references that have no value yet."""
        host = self._service._host()
        return sorted(
            name for name in _credential_names(config) if not host.resolve_credential(name)
        )

    def _inspect(self, identifier: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """The cached Tool catalog and guidance of a connection, one page of Tools at a time."""
        runner = self._service.runners.get(identifier)
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

    async def _import(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Preview the servers of setup text, or with ``apply`` save the chosen ones.

        A preview stores nothing. Saving re-reads the text and never replaces a
        saved connection. A connection that cannot work yet, with a credential
        or a placeholder still to fill in, is saved disabled.
        """
        drafts = parse_setup(arguments["source"], arguments.get("ids"))
        preview = [self._import_preview(draft) for draft in drafts]
        if not arguments.get("apply"):
            return {"servers": preview}
        wanted = arguments.get("servers")
        names = {draft.name for draft in drafts}
        if wanted is not None and (unknown := sorted(set(wanted) - names)):
            raise ValueError(f"No imported server is named {', '.join(unknown)}")
        chosen = [
            (draft, item)
            for draft, item in zip(drafts, preview, strict=True)
            if (draft.name in wanted if wanted is not None else item["selected"])
        ]
        if not chosen:
            raise ValueError("Choose at least one server to import")
        problems = [
            f"{draft.name}: {draft.error or f'connection {draft.id} already exists'}"
            for draft, item in chosen
            if draft.error is not None or item["conflict"]
        ]
        if problems:
            raise ValueError("Cannot import " + "; ".join(problems))
        host = self._service._host()
        imported = []
        for draft, item in chosen:
            for credential in draft.credentials:
                if credential.state == "provided" and credential.value:
                    host.set_credential(credential.name, credential.value)
            connection = {**draft.connection, "enabled": item["enabled"]}
            imported.append(await self._service._save(connection, replace=False))
        stored = [
            credential
            for draft, _ in chosen
            for credential in draft.credentials
            if credential.state == "provided"
        ]
        self._service.api.logger.info(
            "MCP connections imported (connections=%s credentials=%d)",
            ",".join(draft.id for draft, _ in chosen),
            len(stored),
        )
        return {"imported": imported}

    def _import_preview(self, draft: ImportDraft) -> dict[str, Any]:
        """What importing *draft* would save; credential values never leave the draft.

        A credential is ``provided`` by the setup text, already ``set`` on the
        vBot host, or ``missing``: it still needs a value.
        """
        host = self._service._host()
        credentials = [
            {
                "name": credential.name,
                "kind": credential.kind,
                "target": credential.target,
                "state": (
                    credential.state
                    if credential.state != "reference"
                    else "set"
                    if host.resolve_credential(credential.name)
                    else "missing"
                ),
                "description": credential.description,
            }
            for credential in draft.credentials
        ]
        conflict = draft.id in self._service.connections
        complete = all(item["state"] != "missing" for item in credentials)
        return {
            "name": draft.name,
            "id": draft.id,
            "connection": copy.deepcopy(draft.connection) or None,
            "error": draft.error,
            "conflict": conflict,
            "selected": draft.error is None and not conflict and not draft.exposes_secret,
            # Saved enabled only when it can work: no placeholder left in it
            # (``unresolved``), not turned off by the setup text (``disabled``)
            # and no credential missing.
            "enabled": complete and not draft.unresolved and not draft.disabled,
            "unresolved": draft.unresolved,
            "disabled": draft.disabled,
            "credentials": credentials,
            "warnings": copy.deepcopy(draft.warnings),
        }

    async def _add_from_catalog(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Save a catalog entry as a new enabled connection; never replaces a saved one."""
        # A rejected record keeps its id too: saving under it would overwrite the record.
        rejected = {
            issue["connection_id"]
            for issue in (self._service.store.issues if self._service.store else [])
            if "connection_id" in issue
        }
        connection = self._service.connectors.connection(
            arguments["entry"],
            read_only=arguments.get("read_only", False),
            taken=set(self._service.connections) | rejected,
        )
        return await self._service._save(connection, replace=False)
