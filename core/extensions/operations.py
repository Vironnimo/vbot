"""Managed Extension operations and ownership-safe live Tool catalogs."""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from core.tools.tools import Tool, ToolContext, ToolRegistry

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class ExtensionHost:
    """Narrow, injected host capabilities; never exposes the Runtime itself."""

    data_dir: Path
    sample: Callable[[ToolContext, JsonObject], Awaitable[JsonObject]]
    resolve_agent: Callable[[str | None, str], Any]
    store_attachment: Callable[[str, bytes], Any]
    resolve_credential: Callable[[str], str]
    set_credential: Callable[[str, str], None]
    resolve_cwd: Callable[[str | None, str], Path] | None = None


@dataclass(frozen=True)
class ManagementOperation:
    name: str
    description: str
    parameters: JsonObject
    handler: Callable[[JsonObject], Awaitable[JsonObject]]
    secret: bool = False


@dataclass
class ExtensionOperations:
    """Extension-owned management and dynamic catalogs with bounded authority.

    A catalog replacement validates the complete candidate before publishing any
    change. No await occurs during publication, so callers see one generation.
    Retired owners cannot republish after disable or reload.
    """

    name: str
    operations: dict[str, ManagementOperation] = field(default_factory=dict)
    startup: list[Callable[[ExtensionHost], Awaitable[None]]] = field(default_factory=list)
    _registry: ToolRegistry | None = field(default=None, repr=False)
    _catalogs: dict[str, tuple[Tool, ...]] = field(default_factory=dict, repr=False)
    pending_inputs: Callable[[], list[JsonObject]] | None = None
    input_response_operation: str | None = None
    _closed: bool = False

    def register(
        self,
        name: str,
        description: str,
        parameters: JsonObject,
        handler: Callable[[JsonObject], Awaitable[JsonObject]],
        *,
        secret: bool = False,
    ) -> None:
        if name in self.operations or not name or not callable(handler):
            raise ValueError("Management operation must have a unique name and callable handler")
        Draft202012Validator.check_schema(parameters)
        self.operations[name] = ManagementOperation(
            name, description, copy.deepcopy(parameters), handler, secret
        )

    def describe(self) -> list[JsonObject]:
        return [
            {
                "name": operation.name,
                "description": operation.description,
                "parameters": copy.deepcopy(operation.parameters),
                "secret": operation.secret,
            }
            for operation in self.operations.values()
        ]

    async def invoke(self, name: str, arguments: JsonObject) -> JsonObject:
        if self._closed:
            raise ValueError("Extension has been deactivated; refresh its operation catalog")
        operation = self.operations.get(name)
        if operation is None:
            raise ValueError(f"Unknown operation {name!r}; available: {', '.join(self.operations)}")
        errors = list(Draft202012Validator(operation.parameters).iter_errors(arguments))
        if errors:
            # Validation errors can contain credential values. Expose paths only.
            paths = ["/".join(map(str, error.absolute_path)) or "arguments" for error in errors]
            raise ValueError(f"Invalid operation arguments at: {', '.join(paths)}")
        result = await operation.handler(copy.deepcopy(arguments))
        if not isinstance(result, dict):
            raise TypeError("Extension management result must be a JSON object")
        return result

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(tool.name for catalog in self._catalogs.values() for tool in catalog)

    @property
    def tool_registry(self) -> ToolRegistry | None:
        return self._registry

    def bind(self, registry: ToolRegistry) -> None:
        if self._registry is registry and not self._closed:
            return
        if self._closed or self._registry is not None:
            raise RuntimeError("Extension operation owner is already bound or retired")
        self._registry = registry

    def replace_tools(self, catalog: str, tools: Sequence[Mapping[str, Any]]) -> None:
        registry = self._registry
        if self._closed or registry is None:
            raise RuntimeError("Cannot publish Tools from an inactive Extension")
        previous = self._catalogs.get(catalog, ())
        previous_by_name = {tool.name: tool for tool in previous}
        existing = {tool.name: tool for tool in registry.list_tools(include_internal=True)}
        staged = ToolRegistry()
        declarations = [dict(tool) for tool in tools]
        for declaration in declarations:
            name = declaration.get("name")
            if name in existing and existing[name] is not previous_by_name.get(name):
                raise ValueError(f"Tool name is already owned: {name}")
            staged.register(**declaration, extension=self.name)
        candidates = staged.list_tools(include_internal=True)
        registry.replace_owned_tools(previous, candidates)
        self._catalogs[catalog] = tuple(candidates)

    def retire(self) -> None:
        self._closed = True
        if self._registry is not None:
            for tools in self._catalogs.values():
                self._registry.replace_owned_tools(tools, ())
        self._catalogs.clear()
