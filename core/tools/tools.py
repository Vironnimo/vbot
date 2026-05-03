"""Tool definitions and registry for agent tool calls.

The registry starts empty and is populated by callers through ``register()``.
It provides one allowlist implementation for both prompt tool listings and
provider API tool definitions, and dispatches registered callables through a
single async interface.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from core.utils.errors import VBotError

TOOL_ALLOWLIST_WILDCARD = "*"

JsonObject = dict[str, Any]
ToolHandler = Callable[[JsonObject], JsonObject | Awaitable[JsonObject]]


class ToolError(VBotError):
    """Base class for expected tool registry errors."""


class ToolNotFoundError(ToolError):
    """Raised when a tool name is unknown to the registry."""


class ToolNotAllowedError(ToolError):
    """Raised when a tool exists but is not on the caller's allowlist."""


class DuplicateToolError(ToolError):
    """Raised when registering a tool name more than once."""


@dataclass(frozen=True)
class Tool:
    """A callable tool exposed to an agent.

    Attributes:
        name: Stable tool identifier used in model tool calls.
        description: Human-readable description used in prompts and provider
            tool definitions.
        parameters: JSON Schema object for the tool arguments.
        handler: Sync or async callable that receives the call arguments and
            returns a JSON-compatible result object.
    """

    name: str
    description: str
    parameters: JsonObject
    handler: ToolHandler


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
    ) -> Tool:
        """Register a tool and return its immutable definition.

        Raises:
            ValueError: If the tool metadata is empty or the handler is not
                callable.
            DuplicateToolError: If a tool with the same name is already
                registered.
        """
        self._validate_tool(name, description, parameters, handler)
        if name in self._tools:
            raise DuplicateToolError(f"Tool already registered: {name}")

        tool = Tool(
            name=name,
            description=description,
            parameters=dict(parameters),
            handler=handler,
        )
        self._tools[name] = tool
        return tool

    def get(self, name: str) -> Tool:
        """Return a registered tool by name."""
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(f"Tool not found: {name}") from None

    def list_tools(self, allowed_tools: Sequence[str] | None = None) -> list[Tool]:
        """Return registered tools filtered by an allowlist.

        ``["*"]`` or ``None`` means all tools, ``[]`` means no tools, and any
        other list means exact tool-name matches only.
        """
        if allowed_tools is not None and TOOL_ALLOWLIST_WILDCARD not in allowed_tools:
            allowed_names = set(allowed_tools)
            tools = [tool for name, tool in self._tools.items() if name in allowed_names]
        else:
            tools = list(self._tools.values())

        return sorted(tools, key=lambda tool: tool.name)

    def provider_definitions(self, allowed_tools: Sequence[str] | None = None) -> list[JsonObject]:
        """Return provider-ready tool definitions for allowed tools."""
        return [self._to_provider_definition(tool) for tool in self.list_tools(allowed_tools)]

    def prompt_definitions(self, allowed_tools: Sequence[str] | None = None) -> list[JsonObject]:
        """Return prompt-ready name and description pairs for allowed tools."""
        return [
            {"name": tool.name, "description": tool.description}
            for tool in self.list_tools(allowed_tools)
        ]

    async def dispatch(
        self,
        name: str,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None = None,
    ) -> JsonObject:
        """Execute a registered allowed tool through an async interface."""
        tool = self.get(name)
        if not self._is_allowed(name, allowed_tools):
            raise ToolNotAllowedError(f"Tool not allowed: {name}")
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")

        result = tool.handler(arguments)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, dict):
            raise ValueError(f"Tool handler must return a JSON object: {name}")
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
    def _is_allowed(name: str, allowed_tools: Sequence[str] | None) -> bool:
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


__all__ = [
    "DuplicateToolError",
    "JsonObject",
    "TOOL_ALLOWLIST_WILDCARD",
    "Tool",
    "ToolError",
    "ToolHandler",
    "ToolNotAllowedError",
    "ToolNotFoundError",
    "ToolRegistry",
]
