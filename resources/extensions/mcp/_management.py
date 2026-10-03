"""The MCP Extension's management surface for the CLI, WebUI and RPC.

Management operations run outside any Session. Long ones (test, explore,
invoke) run as background jobs that callers poll by id; explore and invoke act
as a named Agent under that Agent's Tool policy and return complete payloads
inline, because no Session could read a saved result later.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

from core.extensions import ExtensionAPI
from core.extensions.operations import ExtensionHost, ExtensionOperations
from core.projects.address import parse_agent_address
from core.tools.availability import resolve_tool_access
from core.tools.tools import ToolContext
from core.utils.config import VBOT_ROOT
from core.utils.ids import new_id

from ._definitions import MAX_FINISHED_JOBS, MCP_OPERATIONS, MCP_PARAMETERS
from ._discovery import operation_target, remote_tool_name
from ._importer import MAX_SETUP_CHARACTERS
from .client import ConnectionRunner
from .config import CONNECTION_SCHEMA
from .interactions import InputRequests

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
        "description": "Search available items, describe one target, or call it.",
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


def register_management(
    api: ExtensionAPI, manage: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
) -> None:
    """Register every management operation; each one calls *manage* with its name."""
    base = {"id": {"type": "string"}}
    schemas: dict[str, dict[str, Any]] = {
        **{name: {} for name in ("list", "requests")},
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
        required = (
            ["id", "agent", "action"]
            if name == "explore"
            else ["id"]
            if name == "inspect"
            else ["source"]
            if name == "import"
            else [key for key in properties if key not in {"after", "arguments"}]
        )

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
    """Run explore or invoke as the named Agent, through that Agent's Tool policy."""
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
