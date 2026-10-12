"""The objects an ``InterpreterHost`` worker hosts, by key.

Loaded into each worker's subinterpreter; the host's calls address these
functions by their canonical names. Standard library only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_OBJECTS: dict[int, Any] = {}


def create(
    key: int, factory: Callable[..., Any], arguments: tuple[Any, ...], options: dict[str, Any]
) -> None:
    _OBJECTS[key] = factory(*arguments, **options)


def call(key: int, method: str, arguments: tuple[Any, ...], options: dict[str, Any]) -> Any:
    return getattr(_OBJECTS[key], method)(*arguments, **options)


def drop(key: int) -> None:
    _OBJECTS.pop(key, None)
