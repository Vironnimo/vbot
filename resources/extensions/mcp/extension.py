"""Bundled MCP integration: connection management and ordinary vBot Tools."""

from __future__ import annotations

import asyncio
import copy
import difflib
import hashlib
import json
import re
import uuid
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from core.extensions import ExtensionAPI
from core.extensions.operations import ExtensionHost
from core.projects.address import parse_agent_address
from core.tools._argument_repair import normalize_call_arguments
from core.tools.availability import resolve_tool_access
from core.tools.contracts import ToolContractError, compile_tool_contract
from core.tools.tools import (
    ToolContext,
    run_tool_worker,
    tool_failure,
    tool_success,
)
from core.utils.config import VBOT_ROOT
from core.utils.ids import new_id

from ._arguments import browse_normalizer
from ._definitions import (
    GUIDANCE_PREVIEW_CHARACTERS,
    MAX_FINISHED_JOBS,
    MCP_DESCRIPTION,
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
from ._views import argument_problem, compact, schema_summary
from .client import READ_OPERATIONS, ConnectionRunner, InvocationNotSentError, operation_schema
from .config import CONNECTION_SCHEMA, ConnectionStore, validate_connection
from .content import RESULT_VIEW_CHARACTERS, ContentStore, pointer_part
from .interactions import InputRequests

__all__ = [
    "GUIDANCE_PREVIEW_CHARACTERS",
    "MAX_FINISHED_JOBS",
    "MCPService",
    "MCP_DESCRIPTION",
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


_TARGET_KINDS = ("tool", "resource", "template", "prompt", "operation")

_DETAIL_CHARACTERS = 300


def remote_tool_name(connection: str, name: str) -> str:
    label = re.sub(r"[^a-zA-Z0-9_]", "_", name)[:TOOL_NAME_LABEL_LENGTH]
    digest = hashlib.sha256(name.encode()).hexdigest()[:TOOL_NAME_HASH_LENGTH]
    return f"mcp_{connection}_{label}_{digest}"


def _parse_target(target: str) -> tuple[str | None, str, str | None]:
    """Split ``kind:name:fingerprint``; kind and fingerprint may be absent."""
    parts = target.split(":")
    kind = parts[0].strip().casefold() if len(parts) > 1 else None
    rest = parts[1:] if kind in _TARGET_KINDS else parts
    if kind not in _TARGET_KINDS:
        kind = None
    fingerprint = None
    if len(rest) > 1 and re.fullmatch(rf"[0-9a-f]{{{TARGET_FINGERPRINT_LENGTH}}}", rest[-1]):
        fingerprint = rest[-1]
        rest = rest[:-1]
    return kind, ":".join(rest).strip(), fingerprint


def _folded(name: str) -> str:
    return re.sub(r"[\s_-]+", "_", name.strip().casefold())


def _one_line(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


class MCPService:
    def __init__(self, api: ExtensionAPI) -> None:
        self.api = api
        self.host: ExtensionHost | None = None
        self.store: ConnectionStore | None = None
        self.content: ContentStore | None = None
        self.connections: dict[str, dict[str, Any]] = {}
        self.runners: dict[str, ConnectionRunner] = {}
        self.inputs = InputRequests()
        self.jobs: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self._runner_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
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
        for config in self.connections.values():
            self._runner(config)
            if config["enabled"]:
                self._runner(config).start()

    async def close(self) -> None:
        self._closed = True
        for task in self.jobs.values():
            task.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)
        await asyncio.gather(*(runner.close() for runner in self.runners.values()))
        self.runners.clear()

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
            self._publish(self.runners[identifier], {"tools": []})
        return self.runners[identifier]

    def _connection(self, identifier: str) -> dict[str, Any]:
        config = self.connections.get(identifier)
        if config is None:
            raise ValueError(f"Unknown MCP connection: {identifier}")
        return config

    def _publish(self, runner: ConnectionRunner, catalog: dict[str, Any]) -> None:
        if self._closed or self.runners.get(runner.id) is not runner:
            return
        parent = f"mcp_{runner.id}"
        declarations = [
            {
                "name": parent,
                "description": MCP_DESCRIPTION,
                "parameters": MCP_PARAMETERS,
                "handler": self._handler(runner.id),
                "ready": lambda: bool(self.connections.get(runner.id, {}).get("enabled")),
                "parallel_safe": False,
                "open_input_schema": True,
                "requires_opt_in": True,
                "argument_normalizer": browse_normalizer(parent),
            }
        ]
        for tool in catalog["tools"]:
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
            async with self._runner_locks[connection]:
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

    @staticmethod
    def _target(kind: str, name: str, definition: Any) -> str:
        fingerprint = hashlib.sha256(
            json.dumps(definition, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:TARGET_FINGERPRINT_LENGTH]
        return f"{kind}:{name}:{fingerprint}"

    def _operation_target(self, operation: str) -> str:
        return self._target("operation", operation, operation_schema(operation))

    @staticmethod
    def _summarize(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "target": entry["target"],
            "kind": entry["kind"],
            "name": entry["name"],
            "description": entry["description"][:SEARCH_SUMMARY_CHARACTERS],
            "describe": {"action": "describe", "target": entry["target"]},
        }

    @staticmethod
    def _search_entries(
        entries: list[dict[str, Any]], query: str = "", kind: str | None = None
    ) -> list[dict[str, Any]]:
        words = set(query.casefold().split())
        order = {
            name: index
            for index, name in enumerate(
                ("tool", "resource", "template", "prompt", "connection", "operation")
            )
        }
        scored = []
        for entry in entries:
            if kind and entry["kind"] != kind:
                continue
            text = (entry["name"] + " " + entry["description"]).casefold()
            score = sum(word in text for word in words)
            if not words or score:
                scored.append((-score, order[entry["kind"]], entry["name"], entry))
        return [item[3] for item in sorted(scored, key=lambda item: item[:3])]

    def _entries(self, runner: ConnectionRunner, allowed: tuple[str, ...]) -> list[dict[str, Any]]:
        entries = []
        for kind, field in (
            ("tool", "tools"),
            ("resource", "resources"),
            ("template", "resource_templates"),
            ("prompt", "prompts"),
        ):
            for definition in runner.catalog.get(field, []):
                name = (
                    definition.get("name") or definition.get("uri") or definition.get("uriTemplate")
                )
                if kind == "tool" and remote_tool_name(runner.id, name) not in allowed:
                    continue
                entries.append(
                    {
                        "kind": kind,
                        "name": name,
                        "target": self._target(kind, name, definition),
                        "description": definition.get("description")
                        or definition.get("title")
                        or name,
                        "definition": definition,
                    }
                )
        entries.extend(
            {
                "kind": "operation",
                "name": name,
                "target": self._operation_target(name),
                "description": description,
                "definition": operation_schema(name),
            }
            for name, description in MCP_OPERATION_DESCRIPTIONS.items()
        )
        entries.append(
            {
                "kind": "connection",
                "name": runner.id,
                "target": "connection",
                "description": "Connection details: server, capabilities and guidance.",
                "definition": {
                    key: value
                    for key, value in runner.catalog.items()
                    if key not in {"tools", "resources", "resource_templates", "prompts", "pages"}
                },
            }
        )
        return sorted(entries, key=lambda entry: (entry["kind"], entry["name"]))

    @staticmethod
    def _arguments_schema(entry: dict[str, Any]) -> dict[str, Any]:
        if entry["kind"] == "tool":
            return dict(entry["definition"]["inputSchema"])
        if entry["kind"] == "operation":
            return dict(entry["definition"])
        if entry["kind"] == "template":
            return operation_schema("resources/read")
        if entry["kind"] == "prompt":
            arguments = entry["definition"].get("arguments", [])
            return {
                "type": "object",
                "properties": {
                    argument["name"]: {
                        "type": "string",
                        "description": argument.get("description", ""),
                    }
                    for argument in arguments
                },
                "required": [
                    argument["name"] for argument in arguments if argument.get("required")
                ],
            }
        return {"type": "object", "properties": {}, "required": []}

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
            allowed = self._allowed(context)
            if not self._connection(runner.id)["enabled"]:
                return tool_failure(
                    "mcp_access_denied",
                    MCP_MESSAGES["disabled"].format(connection=runner.id),
                    retryable=False,
                )
            if f"mcp_{runner.id}" not in allowed:
                return tool_failure("mcp_access_denied", MCP_MESSAGES["access_denied"])
        entries = self._entries(runner, allowed)
        if action == "search":
            return await self._search(runner, context, arguments, entries)
        entry, note, failure = self._resolve(entries, arguments)
        if failure is not None:
            return failure
        assert entry is not None
        if action == "describe":
            return await self._describe(runner, context, entry, note)
        return await self._call_target(runner, context, entry, arguments, allowed)

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
        detail = runner._redact(str(error))[:_DETAIL_CHARACTERS]
        return tool_failure(
            "mcp_request_failed",
            MCP_MESSAGES["unreachable"].format(connection=runner.id, detail=detail),
            retryable=True,
        )

    async def _search(
        self,
        runner: ConnectionRunner,
        context: ToolContext,
        arguments: dict[str, Any],
        entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        assert self.content is not None
        query = arguments.get("query", "")
        kind = arguments.get("kind")
        matches = self._search_entries(entries, query, kind)
        offset = arguments.get("offset", 0)
        limit = min(arguments.get("limit", SEARCH_PAGE_SIZE), SEARCH_PAGE_SIZE)
        instructions = runner.catalog.get("instructions") or ""
        prompts = [entry for entry in entries if entry["kind"] == "prompt"]
        available = {
            kind_name: sum(entry["kind"] == kind_name for entry in entries)
            for kind_name in ("tool", "resource", "template", "prompt")
        }
        payload = {
            "connection": runner.id,
            "matches": [self._summarize(entry) for entry in matches],
            "total": len(matches),
            "available": available,
            "server_guidance": {
                "instructions": instructions,
                "prompts": [self._summarize(entry) for entry in prompts],
            },
        }
        if not context.result_payloads_available:
            # Outside a Session nothing reads a saved result later: return everything.
            return tool_success({"complete": True, "value": payload})
        page = matches[offset : offset + limit]
        lines = [
            f"{entry['target']}: {_one_line(entry['description'], SEARCH_SUMMARY_CHARACTERS)}"
            for entry in page
        ]
        while len(lines) > 1 and len("\n".join(lines)) > RESULT_VIEW_CHARACTERS - 2000:
            lines.pop()
            page = page[: len(lines)]
        view: dict[str, Any] = {
            "connection": runner.id,
            "available": ", ".join(
                _plural(count, name) for name, count in available.items() if count
            )
            or "no tools, resources or prompts",
        }
        if page:
            view["matches"] = f"{offset + 1}-{offset + len(page)} of {len(matches)}"
        else:
            view["matches"] = f"none after {offset} of {len(matches)}" if matches else "none"
        if offset + len(page) < len(matches):
            view["next"] = {**arguments, "offset": offset + len(page)}
        elif offset and offset >= len(matches):
            view["next"] = {**arguments, "offset": 0}
        if not matches and query.strip():
            view["note"] = MCP_MESSAGES["no_matches"]
            view["next"] = {"action": "search", "kind": "tool"}
        sections = ["\n".join(lines)] if lines else []
        if not query.strip() and not offset and kind is None:
            if instructions:
                shown = instructions[:GUIDANCE_PREVIEW_CHARACTERS]
                sections.append(f"Server guidance (external, from the MCP server):\n{shown}")
                if len(instructions) > GUIDANCE_PREVIEW_CHARACTERS:
                    identifier = self.content.attach(payload, context, runner.id)
                    view["guidance"] = MCP_MESSAGES["guidance_incomplete"]
                    view["guidance_read"] = {
                        "action": "read",
                        "result_id": identifier,
                        "pointer": "/server_guidance/instructions",
                        "offset": GUIDANCE_PREVIEW_CHARACTERS,
                    }
            unlisted = [entry for entry in prompts if entry not in page]
            if unlisted:
                prompt_lines = [
                    f"{entry['target']}: "
                    f"{_one_line(entry['description'], SEARCH_SUMMARY_CHARACTERS)}"
                    for entry in unlisted[:3]
                ]
                if len(unlisted) > 3:
                    more = compact({"action": "search", "kind": "prompt"})
                    prompt_lines.append(f"{len(unlisted) - 3} more: {more}")
                sections.append("Prompts (server workflows):\n" + "\n".join(prompt_lines))
        elif instructions:
            view["server_guidance"] = f"shown by {compact({'action': 'search'})}"
        if sections:
            view["content"] = "\n\n".join(sections)
        return tool_success(view)

    def _resolve(
        self, entries: list[dict[str, Any]], arguments: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
        """Find the one target a describe or call names; never choose between several.

        A full target from search matches exactly. A name, with or without its
        kind, matches when it names one item (ignoring case and separators).
        A fingerprint of an earlier definition is not substituted for a call.
        """
        target = arguments["target"]
        exact = next((entry for entry in entries if entry["target"] == target), None)
        if exact is not None:
            return exact, None, None
        kind, name, fingerprint = _parse_target(target)
        pool = [
            entry
            for entry in entries
            if entry["kind"] != "connection" and (kind is None or entry["kind"] == kind)
        ]

        def names(entry: dict[str, Any]) -> set[str]:
            definition = entry["definition"]
            return {
                value
                for value in (entry["name"], definition.get("uri"), definition.get("uriTemplate"))
                if isinstance(value, str)
            }

        named = [entry for entry in pool if name in names(entry)]
        if not named:
            named = [
                entry for entry in pool if _folded(name) in {_folded(item) for item in names(entry)}
            ]
        if len(named) == 1:
            entry = named[0]
            if fingerprint is None:
                return entry, None, None
            if arguments["action"] == "describe":
                return entry, MCP_MESSAGES["target_updated"].format(previous=target), None
            describe = compact({"action": "describe", "target": entry["target"]})
            return (
                None,
                None,
                tool_failure(
                    "mcp_target_changed",
                    MCP_MESSAGES["target_changed"].format(
                        item=f"{entry['kind']} {entry['name']}",
                        target=entry["target"],
                        describe=describe,
                    ),
                ),
            )
        shown = name[:80] or target[:80]
        if len(named) > 1:
            return (
                None,
                None,
                tool_failure(
                    "mcp_unknown_target",
                    MCP_MESSAGES["target_ambiguous"].format(
                        name=shown, targets=", ".join(entry["target"] for entry in named)
                    ),
                ),
            )
        return None, None, tool_failure("mcp_unknown_target", self._unknown(pool, kind, shown))

    @staticmethod
    def _unknown(pool: list[dict[str, Any]], kind: str | None, name: str) -> str:
        subject = f"No {kind}" if kind else "No tool, resource, prompt or operation"
        message = f"{subject} named {name} is available on this connection, so nothing was run."
        by_name = {entry["name"].casefold(): entry for entry in pool}
        close = difflib.get_close_matches(name.casefold(), list(by_name), n=5, cutoff=0.6)
        words = [word for word in re.split(r"[\s_./:-]+", name.casefold()) if len(word) > 2]
        close += [
            key
            for key, entry in by_name.items()
            if key not in close and words and any(word in key for word in words)
        ][: max(0, 5 - len(close))]
        candidates = [by_name[key]["target"] for key in close]
        if len(candidates) == 1:
            return (
                f"{message} The closest is {candidates[0]}; if you mean it, repeat the call "
                f'with "target":"{candidates[0]}".'
            )
        if candidates:
            return (
                f"{message} Close names: {', '.join(candidates)}. Repeat the call with the "
                "target you mean."
            )
        query = " ".join(words) or name
        return f"{message} Find it with {compact({'action': 'search', 'query': query[:60]})}."

    async def _describe(
        self,
        runner: ConnectionRunner,
        context: ToolContext,
        entry: dict[str, Any],
        note: str | None,
    ) -> dict[str, Any]:
        kind = entry["kind"]
        payload: dict[str, Any] = {"target": entry["target"]}
        if note is not None:
            payload["note"] = note
        if kind != "connection" and entry["description"] != entry["name"]:
            payload["description"] = entry["description"]
        hidden = {"name", "description", "inputSchema" if kind == "tool" else ""}
        if kind == "prompt":
            hidden.add("arguments")
        details = (
            {key: value for key, value in entry["definition"].items() if key not in hidden}
            if kind != "operation"
            else {}
        )
        if details:
            payload["definition"] = details
        if kind != "connection":
            payload["arguments_schema"] = self._arguments_schema(entry)
            payload["call"] = (
                f'{{"action":"call","target":"{entry["target"]}","arguments":{{...}}}}'
                " with arguments matching arguments_schema"
            )
        source = entry["name"] if kind == "tool" else None
        return await self._present(runner, context, payload, source=source)

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
        source = entry["name"] if entry["kind"] == "tool" else None
        schema = self._arguments_schema(entry)
        inputs = arguments.get("arguments", {})
        try:
            contract = compile_tool_contract(
                name="mcp_target", input_schema=schema, require_closed_input=False
            )
            inputs = (
                normalize_call_arguments(contract, inputs, enum_fields=("level",))
                if entry["kind"] == "operation" and entry["name"] == "logging/setLevel"
                else contract.normalize_arguments(inputs)
            )
        except ToolContractError as repair_error:
            return self._invalid_target_arguments(runner, entry, schema, str(repair_error))
        error = best_match(Draft202012Validator(schema).iter_errors(inputs))
        if error is not None:
            pointer = "/arguments" + "".join(
                "/" + pointer_part(str(part)) for part in error.absolute_path
            )
            problem = argument_problem(
                str(error.validator), error.validator_value, error.schema, error.instance, pointer
            )
            return self._invalid_target_arguments(runner, entry, schema, problem)
        if source is not None:
            registry = self.api.operations.tool_registry
            if registry is None:
                raise RuntimeError("MCP Tools are not bound")
            return await registry.dispatch(
                replace(
                    context, tool_name=remote_tool_name(runner.id, source), input_contract=None
                ),
                inputs,
                allowed,
            )
        if entry["kind"] == "resource":
            operation, inputs = "resources/read", {"uri": entry["definition"]["uri"]}
        elif entry["kind"] == "template":
            operation = "resources/read"
        elif entry["kind"] == "prompt":
            operation, inputs = "prompts/get", {"name": entry["name"], "arguments": inputs}
        else:
            operation = entry["name"]
        return await self._call(runner, context, operation, inputs)

    @staticmethod
    def _invalid_target_arguments(
        runner: ConnectionRunner, entry: dict[str, Any], schema: dict[str, Any], problem: str
    ) -> dict[str, Any]:
        return tool_failure(
            "mcp_invalid_arguments",
            MCP_MESSAGES["target_invalid"].format(
                item=f"{entry['kind']} {entry['name']}",
                problem=runner._redact(problem)[:500],
                summary=schema_summary(schema),
                describe=compact({"action": "describe", "target": entry["target"]}),
            ),
        )

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
            detail = runner._redact(str(error))[:_DETAIL_CHARACTERS]
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
            detail = runner._safe_error(error)
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
            return self._job_status(arguments["job_id"])
        if operation == "cancel-job":
            identifier = arguments["job_id"]
            if identifier not in self.jobs:
                raise ValueError("Unknown MCP management job")
            self.jobs[identifier].cancel()
            await asyncio.gather(self.jobs[identifier], return_exceptions=True)
            return self._job_status(identifier)
        if operation == "save":
            return await self._save(arguments["connection"])
        identifier = arguments["id"]
        config = self._connection(identifier)
        if operation == "status":
            return self._status(identifier)
        if operation == "inspect":
            runner = self.runners.get(identifier)
            catalog = runner.catalog if runner else {}
            entries = [
                {
                    "kind": "tool",
                    "name": item["name"],
                    "description": item.get("description") or item.get("title") or item["name"],
                    "target": self._target("tool", item["name"], item),
                }
                for item in catalog.get("tools", [])
            ]
            matches = self._search_entries(entries, arguments.get("query", ""))
            offset = arguments.get("offset", 0)
            return {
                **self._status(identifier),
                "catalog_available": bool(catalog),
                "tools": [
                    {**self._summarize(item), "description": item["description"]}
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
        if operation in {"enable", "disable", "remove"}:
            return await self._mutate(operation, config)
        async with self._runner_locks[identifier]:
            config = self._connection(identifier)
            if operation == "credential":
                sources = set(config.get("credential_environment", {}).values()) | set(
                    config.get("credential_headers", {}).values()
                )
                if arguments["key"] not in sources:
                    raise ValueError("Credential must be referenced by this MCP connection")
                self._host().set_credential(arguments["key"], arguments["value"])
                await self._stop(identifier)
                self._runner(config)
                return {
                    "id": identifier,
                    "credential": arguments["key"],
                    "set": bool(arguments["value"]),
                }
            if operation == "disconnect":
                await self._stop(identifier)
                self._runner(config)
                return self._status(identifier)
            if not config["enabled"]:
                raise ValueError("MCP connection is disabled")
            runner = self._runner(config)
            if operation == "connect":
                runner.start()
                return self._status(identifier)
            if operation == "events":
                return runner.events(arguments.get("after", 0))
            if operation == "test":
                return self._start_job(self._test(runner))
            if operation in {"invoke", "explore"}:
                return self._start_job(self._invoke_for_agent(runner, arguments))
        raise ValueError(f"Unknown MCP management operation: {operation}")

    def _status(self, identifier: str) -> dict[str, Any]:
        config = self._connection(identifier)
        runner = self.runners.get(identifier)
        status = (
            runner.status()
            if runner is not None
            else {"id": identifier, "state": "disconnected", "error": None}
        )
        return {
            **status,
            "configuration": copy.deepcopy(config),
            "pending_requests": [
                item for item in self.inputs.list() if item["connection"] == identifier
            ],
        }

    async def _save(self, value: dict[str, Any]) -> dict[str, Any]:
        config = validate_connection(value)
        async with self._lock, self._runner_locks[config["id"]]:
            records = {**self.connections, config["id"]: config}
            if self.store is None:
                raise RuntimeError("MCP store was not initialized")
            await run_tool_worker(self.store.save, records)
            await self._stop(config["id"])
            self.connections = records
            if config["enabled"]:
                self._runner(config).start()
        self.api.logger.info("MCP connection configured (connection=%s)", config["id"])
        return self._status(config["id"])

    async def _mutate(self, operation: str, original: dict[str, Any]) -> dict[str, Any]:
        identifier = original["id"]
        async with self._lock, self._runner_locks[identifier]:
            config = copy.deepcopy(self._connection(identifier))
            records = dict(self.connections)
            if operation == "remove":
                records.pop(identifier)
            elif operation in {"enable", "disable"}:
                config["enabled"] = operation == "enable"
                records[identifier] = config
            if self.store is None:
                raise RuntimeError("MCP store was not initialized")
            await run_tool_worker(self.store.save, records)
            self.connections = records
            if operation in {"disable", "remove"}:
                await self._stop(identifier)
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

    async def _stop(self, identifier: str) -> None:
        runner = self.runners.pop(identifier, None)
        try:
            if runner is not None:
                await runner.close()
        finally:
            self.api.operations.replace_tools(identifier, [])

    async def _test(self, runner: ConnectionRunner) -> dict[str, Any]:
        catalog = await runner.invoke("catalog", {})
        health = await runner.invoke("ping", {})
        verified = list(dict.fromkeys(["catalog", health.get("verified", "ping")]))
        return {"status": runner.status(), "catalog": catalog, "verified": verified}

    async def _invoke_for_agent(
        self, runner: ConnectionRunner, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        agent_id, project_id = parse_agent_address(arguments["agent"])
        agent = self._host().resolve_agent(project_id, agent_id)
        registry = self.api.operations.tool_registry
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
        resolve_cwd = self._host().resolve_cwd
        context = ToolContext(
            agent_id=agent_id,
            project_id=project_id,
            session_id="mcp-management",
            run_id="mcp-management",
            tool_call_id=str(uuid.uuid4()),
            tool_name=name,
            tool_call_index=0,
            workspace=Path(agent.workspace or runner.config.get("cwd") or self._host().data_dir),
            cwd=resolve_cwd(project_id, agent_id) if resolve_cwd else None,
            vbot_root=VBOT_ROOT,
            data_root=self._host().data_dir,
        )
        handler_arguments = (
            inputs.get("arguments", {})
            if operation == "tools/call"
            else (
                {key: value for key, value in arguments.items() if key not in {"id", "agent"}}
                if operation is None
                else {
                    "action": "call",
                    "target": self._operation_target(operation),
                    "arguments": inputs,
                }
            )
        )
        return await registry.dispatch(context, handler_arguments, resolution.allowed_tools)

    def _start_job(self, coroutine: Any) -> dict[str, Any]:
        completed = [identifier for identifier, task in self.jobs.items() if task.done()]
        for identifier in completed[:-MAX_FINISHED_JOBS]:
            self.jobs.pop(identifier)
        identifier = new_id("job", claim=lambda candidate: candidate not in self.jobs)
        self.jobs[identifier] = asyncio.create_task(coroutine)
        self.jobs[identifier].add_done_callback(self._observe_job)
        return self._job_status(identifier)

    @staticmethod
    def _observe_job(task: asyncio.Task[dict[str, Any]]) -> None:
        if not task.cancelled():
            task.exception()

    def _job_status(self, identifier: str) -> dict[str, Any]:
        task = self.jobs.get(identifier)
        if task is None:
            raise ValueError("Unknown MCP management job")
        if not task.done():
            return {"job_id": identifier, "state": "running", "requests": self.inputs.list()}
        if task.cancelled():
            return {"job_id": identifier, "state": "cancelled"}
        error = task.exception()
        if error is not None:
            return {"job_id": identifier, "state": "failed", "error": str(error)}
        result = task.result()
        state = "failed" if result.get("ok") is False else "completed"
        return {"job_id": identifier, "state": state, "result": result}


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


def register(api: ExtensionAPI) -> None:
    service = MCPService(api)
    api.operations.startup.append(service.start)
    api.operations.pending_inputs = service.inputs.list
    api.operations.input_response_operation = "respond"
    api.on_shutdown(service.close)
    base = {"id": {"type": "string"}}
    descriptions = {
        "list": "List saved connections, live connection state, and effective Agent access.",
        "requests": (
            "List pending server inputs, including OAuth and elicitation; answer with respond."
        ),
        "status": "Read one connection's saved configuration, live state, and Agent access.",
        "remove": "Remove a saved connection and stop its client and published Tools.",
        "enable": "Enable a saved connection and start connecting; inspect status for readiness.",
        "disable": "Disable a saved connection and stop its client and published Tools.",
        "connect": "Start connecting an enabled connection; inspect status for readiness.",
        "disconnect": "Close the current client without disabling the saved connection.",
        "test": "Start a catalog/health check; use the returned job_id with job for its outcome.",
        "save": "Create or replace a complete connection; read status before replacing one.",
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
        "invoke": (
            "Invoke an exact MCP operation as an Agent; inspect the returned job_id with job."
        ),
    }
    schemas: dict[str, dict[str, Any]] = {
        **{name: {} for name in ("list", "requests")},
        **dict.fromkeys(
            ("status", "remove", "enable", "disable", "connect", "disconnect", "test"), base
        ),
        "save": {"connection": CONNECTION_SCHEMA},
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
            else [key for key in properties if key not in {"after", "arguments"}]
        )

        async def handler(arguments: dict[str, Any], operation: str = name) -> dict[str, Any]:
            return await service.manage(operation, arguments)

        api.operations.register(
            name,
            descriptions[name],
            {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            handler,
            secret=name in {"credential", "respond"},
        )
