# Public authoring declarations; runtime identities come from the installed host.
from __future__ import annotations

import typing
from collections.abc import (
    Callable,
)
from dataclasses import dataclass
from typing import (
    Any,
    Final,
)

DESKTOP_EXTENSION_API_VERSION: Final[int] = 2

class DesktopExtensionAPI:
    def __init__(
        self, owner: str, *, source: str = "", revision: str = "", legacy: bool = False
    ) -> None: ...
    def register_capability(
        self,
        name: str,
        label: str,
        factory: HandlerFactory,
        *,
        description: str = "",
        available: bool = True,
    ) -> None: ...

class ExecutionHandler(typing.Protocol):
    async def execute(self, operation: str, params: dict[str, Any]) -> dict[str, Any]: ...
    async def disconnected(self) -> None: ...
    async def close(self) -> None: ...

type EventSink = Callable[[str, dict[str, Any]], None]
type HandlerFactory = Callable[[EventSink], ExecutionHandler]

@dataclass(frozen=True)
class NativeCapability:
    id: str
    owner: str
    label: str
    description: str
    factory: HandlerFactory
    available: bool = True
    source: str = ""
    revision: str = ""

    @property
    def identity(self) -> tuple[str, str]: ...
    def describe(self) -> dict[str, Any]: ...
