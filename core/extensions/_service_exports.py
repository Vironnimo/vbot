"""Declaration validation and registration lifetime for exported Services.

This is an internal part of the existing Service catalog, not another registry.
"""

from __future__ import annotations

import copy
import inspect
import json
import re
from collections.abc import Callable
from contextvars import copy_context
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from jsonschema import Draft202012Validator

if TYPE_CHECKING:
    from core.extensions._declarations import (
        ExtensionRegistrationIdentity,
        ResourceDeclaration,
        ServiceDeclaration,
    )

_LOCAL_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,79}")


def local_name(value: object) -> str:
    if not isinstance(value, str) or not _LOCAL_NAME.fullmatch(value):
        raise ValueError("Service and resource names must be lowercase local identifiers")
    return value


def qualified_name(owner: str, name: str) -> str:
    """Encode owner delimiters so separate owners cannot collide."""
    return f"extension:{quote(owner, safe='')}:{name}"


def detached_json(value: Any, *, limit: int = 256 * 1024) -> Any:
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > limit:
            raise ValueError("JSON exceeds its size limit")
        # Reject Python-only values, including tuple values and non-string keys,
        # by returning the actual JSON representation used across the bridge.
        return json.loads(encoded)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Expected bounded finite UTF-8 JSON") from exc


def parameter_schema(value: object) -> dict[str, Any]:
    schema = detached_json(value)
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("Service and resource schemas must describe an object")

    def check_refs(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if key in {"$ref", "$dynamicRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise ValueError("Service schemas may only reference local definitions")
                check_refs(child)
        elif isinstance(node, list):
            for child in node:
                check_refs(child)

    check_refs(schema)
    Draft202012Validator.check_schema(schema)
    return schema


def description_text(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("Service and resource descriptions must be text up to 2000 characters")
    return value


def guarded_handler(
    handler: Callable[[dict[str, Any]], Any], require_current: Callable[[], None]
) -> Callable[[dict[str, Any]], Any]:
    """Recheck admission when a callback actually starts, after worker waits."""
    if inspect.iscoroutinefunction(handler):

        async def asynchronous(arguments: dict[str, Any]) -> Any:
            require_current()
            return await handler(arguments)

        return asynchronous

    context = copy_context()

    def invoke(arguments: dict[str, Any]) -> Any:
        require_current()
        return handler(arguments)

    def synchronous(arguments: dict[str, Any]) -> Any:
        return context.run(invoke, arguments)

    return synchronous


@dataclass(frozen=True)
class ExportedService:
    owner: ExtensionRegistrationIdentity
    declaration: ServiceDeclaration
    resources: tuple[str, ...]
    validator: Draft202012Validator
    require_current: Callable[[], None]

    def describe(self, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "kind": self.declaration.kind,
            "parameters": copy.deepcopy(self.declaration.parameters),
            "resources": list(self.resources),
            "description": self.declaration.description,
            "owner": self.owner.name,
        }


@dataclass(frozen=True)
class ExportedResource:
    owner: ExtensionRegistrationIdentity
    declaration: ResourceDeclaration
    validator: Draft202012Validator | None
    require_current: Callable[[], None]

    def describe(self, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "owner": self.owner.name,
            "description": self.declaration.description,
            "scope_schema": copy.deepcopy(self.declaration.scope_schema),
        }
