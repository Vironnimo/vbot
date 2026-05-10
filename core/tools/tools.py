"""Tool definitions, registry, result envelopes, and execution scheduling."""

from __future__ import annotations

import asyncio
import inspect
import weakref
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from core.utils.errors import VBotError

TOOL_ALLOWLIST_WILDCARD = "*"
DEFAULT_TOOL_CONCURRENCY_LIMIT = 50

JsonObject = dict[str, Any]
ToolEmitHook = Callable[[str, JsonObject], None | Awaitable[None]]
ToolCancellationHook = Callable[[], bool]
ToolNoteHook = Callable[[str], None]
ToolSkillActivationHook = Callable[[str, JsonObject], JsonObject]
ToolHandler = Callable[["ToolContext", JsonObject], JsonObject | Awaitable[JsonObject]]


class ToolError(VBotError):
    """Base class for expected tool registry errors."""


class ToolNotFoundError(ToolError):
    """Raised when a tool name is unknown to the registry."""


class ToolNotAllowedError(ToolError):
    """Raised when a tool exists but is not on the caller's allowlist."""


class DuplicateToolError(ToolError):
    """Raised when registering a tool name more than once."""


@dataclass(frozen=True)
class ToolContext:
    """Runtime-owned execution identity passed to a single tool call."""

    agent_id: str
    session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    tool_call_index: int
    workspace: Path
    app_root: Path
    data_root: Path
    emit_hook: ToolEmitHook | None = None
    cancellation_hook: ToolCancellationHook | None = None
    note_hook: ToolNoteHook | None = None
    skill_activation_hook: ToolSkillActivationHook | None = None
    allowed_skills: Sequence[str] | None = None

    async def emit(self, event_type: str, payload: JsonObject) -> None:
        """Emit a tool lifecycle event through the runtime hook, when present."""
        if self.emit_hook is None:
            return

        result = self.emit_hook(event_type, payload)
        if inspect.isawaitable(result):
            await result

    def is_cancelled(self) -> bool:
        """Return whether the owning run has requested cancellation."""
        if self.cancellation_hook is None:
            return False

        return self.cancellation_hook()

    def add_note(self, content: str) -> None:
        """Add a kernel-internal note through the runtime hook, when present."""
        if self.note_hook is None:
            return

        self.note_hook(content)

    def activate_skill(self, name: str, data: JsonObject) -> JsonObject | None:
        """Activate skill context through the runtime hook, when present."""
        if self.skill_activation_hook is None:
            return None

        return self.skill_activation_hook(name, data)


@dataclass(frozen=True)
class ToolCall:
    """A provider-requested tool invocation to schedule."""

    id: str
    name: str
    arguments: Any


@dataclass(frozen=True)
class ToolExecutionConfig:
    """Runtime fields shared by every tool call in one execution group."""

    agent_id: str
    session_id: str
    run_id: str
    workspace: Path
    app_root: Path
    data_root: Path
    allowed_tools: Sequence[str] | None = None
    emit_hook: ToolEmitHook | None = None
    cancellation_hook: ToolCancellationHook | None = None
    note_hook: ToolNoteHook | None = None
    skill_activation_hook: ToolSkillActivationHook | None = None
    allowed_skills: Sequence[str] | None = None


@dataclass(frozen=True)
class Tool:
    """A callable tool exposed to an agent."""

    name: str
    description: str
    parameters: JsonObject
    handler: ToolHandler
    internal: bool = False


def tool_success(data: JsonObject, artifacts: list[JsonObject] | None = None) -> JsonObject:
    """Return a stable success envelope for a tool result."""
    if not isinstance(data, dict):
        raise ValueError("Tool success data must be a JSON object")

    return {
        "ok": True,
        "error": None,
        "data": data,
        "artifacts": _copy_artifacts(artifacts),
    }


def tool_failure(
    code: str,
    message: str,
    artifacts: list[JsonObject] | None = None,
) -> JsonObject:
    """Return a stable failure envelope for a tool result."""
    if not code:
        raise ValueError("Tool failure code is required")
    if not message:
        raise ValueError("Tool failure message is required")

    return {
        "ok": False,
        "error": {"code": code, "message": message},
        "data": None,
        "artifacts": _copy_artifacts(artifacts),
    }


def is_tool_result_envelope(result: JsonObject) -> bool:
    """Return whether a JSON object matches the stable tool result envelope."""
    if set(result) != {"ok", "error", "data", "artifacts"}:
        return False
    if not isinstance(result["ok"], bool):
        return False
    if not isinstance(result["artifacts"], list):
        return False

    if result["ok"]:
        return result["error"] is None and isinstance(result["data"], dict)

    return result["data"] is None and _is_error_object(result["error"])


class ToolRegistry:
    """Register, filter, describe, and dispatch agent tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: JsonObject,
        handler: ToolHandler,
        *,
        internal: bool = False,
    ) -> Tool:
        """Register a tool and return its immutable definition."""
        self._validate_tool(name, description, parameters, handler)
        if name in self._tools:
            raise DuplicateToolError(f"Tool already registered: {name}")

        tool = Tool(
            name=name,
            description=description,
            parameters=dict(parameters),
            handler=handler,
            internal=internal,
        )
        self._tools[name] = tool
        return tool

    def get(self, name: str) -> Tool:
        """Return a registered tool by name."""
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(f"Tool not found: {name}") from None

    def list_tools(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
    ) -> list[Tool]:
        """Return registered tools filtered by an allowlist."""
        if allowed_tools is not None and TOOL_ALLOWLIST_WILDCARD not in allowed_tools:
            allowed_names = set(allowed_tools)
            tools = [tool for name, tool in self._tools.items() if name in allowed_names]
        else:
            tools = list(self._tools.values())

        if not include_internal:
            tools = [tool for tool in tools if not tool.internal]

        return sorted(tools, key=lambda tool: tool.name)

    def provider_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
    ) -> list[JsonObject]:
        """Return provider-ready tool definitions for allowed tools."""
        return [
            self._to_provider_definition(tool)
            for tool in self.list_tools(allowed_tools, include_internal=include_internal)
        ]

    def prompt_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
    ) -> list[JsonObject]:
        """Return prompt-ready name and description pairs for allowed tools."""
        return [
            {"name": tool.name, "description": tool.description}
            for tool in self.list_tools(allowed_tools, include_internal=include_internal)
        ]

    async def dispatch(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None = None,
    ) -> JsonObject:
        """Execute a registered allowed tool through an async interface."""
        tool = self.get(context.tool_name)
        if not self._is_allowed(context.tool_name, allowed_tools, internal=tool.internal):
            raise ToolNotAllowedError(f"Tool not allowed: {context.tool_name}")
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")

        result = tool.handler(context, arguments)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, dict):
            raise ValueError(f"Tool handler must return a JSON object: {context.tool_name}")
        if not is_tool_result_envelope(result):
            raise ValueError(
                f"Tool handler must return a valid result envelope: {context.tool_name}"
            )
        return result

    @staticmethod
    def _validate_tool(
        name: str,
        description: str,
        parameters: JsonObject,
        handler: ToolHandler,
    ) -> None:
        if not name:
            raise ValueError("Tool name is required")
        if not description:
            raise ValueError("Tool description is required")
        if not isinstance(parameters, dict):
            raise ValueError("Tool parameters must be a JSON Schema object")
        if not callable(handler):
            raise ValueError("Tool handler must be callable")

    @staticmethod
    def _is_allowed(
        name: str,
        allowed_tools: Sequence[str] | None,
        *,
        internal: bool = False,
    ) -> bool:
        if internal:
            return True
        return (
            allowed_tools is None
            or TOOL_ALLOWLIST_WILDCARD in allowed_tools
            or name in allowed_tools
        )

    @staticmethod
    def _to_provider_definition(tool: Tool) -> JsonObject:
        return {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.parameters),
        }


class ToolExecutor:
    """Schedule tool calls concurrently while preserving returned call order."""

    _global_semaphores: ClassVar[
        weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Semaphore]]
    ] = weakref.WeakKeyDictionary()

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        per_run_limit: int = DEFAULT_TOOL_CONCURRENCY_LIMIT,
        global_limit: int = DEFAULT_TOOL_CONCURRENCY_LIMIT,
    ) -> None:
        if per_run_limit < 1:
            raise ValueError("Per-run tool concurrency limit must be at least 1")
        if global_limit < 1:
            raise ValueError("Global tool concurrency limit must be at least 1")

        self._registry = registry
        self._per_run_limit = per_run_limit
        self._global_limit = global_limit

    async def execute_many(
        self,
        tool_calls: Sequence[ToolCall],
        config: ToolExecutionConfig,
    ) -> list[JsonObject]:
        """Execute tool calls concurrently and return results in request order."""
        per_run_semaphore = asyncio.Semaphore(self._per_run_limit)
        tasks = [
            asyncio.create_task(
                self._execute_one(tool_call, index, config, per_run_semaphore),
                name=f"tool:{tool_call.name}:{tool_call.id}",
            )
            for index, tool_call in enumerate(tool_calls)
        ]

        if not tasks:
            return []

        return await asyncio.gather(*tasks)

    async def _execute_one(
        self,
        tool_call: ToolCall,
        index: int,
        config: ToolExecutionConfig,
        per_run_semaphore: asyncio.Semaphore,
    ) -> JsonObject:
        async with per_run_semaphore, self._get_global_semaphore():
            context = ToolContext(
                agent_id=config.agent_id,
                session_id=config.session_id,
                run_id=config.run_id,
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                tool_call_index=index,
                workspace=config.workspace,
                app_root=config.app_root,
                data_root=config.data_root,
                emit_hook=config.emit_hook,
                cancellation_hook=config.cancellation_hook,
                note_hook=config.note_hook,
                skill_activation_hook=config.skill_activation_hook,
                allowed_skills=config.allowed_skills,
            )
            return await self._dispatch_with_envelope(context, tool_call, config.allowed_tools)

    def _get_global_semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        loop_semaphores = self._global_semaphores.setdefault(loop, {})
        semaphore = loop_semaphores.get(self._global_limit)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._global_limit)
            loop_semaphores[self._global_limit] = semaphore
        return semaphore

    async def _dispatch_with_envelope(
        self,
        context: ToolContext,
        tool_call: ToolCall,
        allowed_tools: Sequence[str] | None,
    ) -> JsonObject:
        try:
            return await self._registry.dispatch(context, tool_call.arguments, allowed_tools)
        except ToolNotFoundError as error:
            return tool_failure("tool_not_found", str(error))
        except ToolNotAllowedError as error:
            return tool_failure("tool_not_allowed", str(error))
        except ValueError as error:
            return tool_failure(
                "invalid_tool_result" if "return" in str(error) else "invalid_arguments", str(error)
            )
        except Exception as error:
            return tool_failure("tool_execution_error", str(error))


def _copy_artifacts(artifacts: list[JsonObject] | None) -> list[JsonObject]:
    if artifacts is None:
        return []
    if not isinstance(artifacts, list):
        raise ValueError("Tool result artifacts must be a list")
    if not all(isinstance(artifact, dict) for artifact in artifacts):
        raise ValueError("Tool result artifacts must contain JSON objects")

    return [dict(artifact) for artifact in artifacts]


def _is_error_object(error: Any) -> bool:
    return (
        isinstance(error, dict)
        and set(error) == {"code", "message"}
        and isinstance(error["code"], str)
        and isinstance(error["message"], str)
        and bool(error["code"])
        and bool(error["message"])
    )


__all__ = [
    "DEFAULT_TOOL_CONCURRENCY_LIMIT",
    "DuplicateToolError",
    "JsonObject",
    "TOOL_ALLOWLIST_WILDCARD",
    "Tool",
    "ToolCall",
    "ToolCancellationHook",
    "ToolContext",
    "ToolEmitHook",
    "ToolError",
    "ToolExecutionConfig",
    "ToolExecutor",
    "ToolHandler",
    "ToolNoteHook",
    "ToolNotAllowedError",
    "ToolNotFoundError",
    "ToolRegistry",
    "is_tool_result_envelope",
    "tool_failure",
    "tool_success",
]
