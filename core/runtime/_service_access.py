"""Typed read-only access to services owned by a started Runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, overload

if TYPE_CHECKING:
    from core.runtime.runtime import Runtime


class _StartedService[Service]:
    """Apply the same readiness and availability policy to an explicit service getter."""

    def __init__(self, getter: Callable[[Runtime], Service | None], unavailable: str) -> None:
        self._getter = getter
        self._unavailable = unavailable
        self._name = ""

    def __set_name__(self, owner: type[Runtime], name: str) -> None:
        self._name = name

    @overload
    def __get__(self, instance: None, owner: type[Runtime]) -> _StartedService[Service]: ...

    @overload
    def __get__(self, instance: Runtime, owner: type[Runtime] | None = None) -> Service: ...

    def __get__(
        self, instance: Runtime | None, owner: type[Runtime] | None = None
    ) -> Service | _StartedService[Service]:
        if instance is None:
            return self
        instance._ensure_started()
        service = self._getter(instance)
        if service is None:
            raise RuntimeError(self._unavailable)
        return service

    def __set__(self, instance: Runtime, value: object) -> None:
        raise AttributeError(
            f"property '{self._name}' of '{type(instance).__name__}' object has no setter"
        )
