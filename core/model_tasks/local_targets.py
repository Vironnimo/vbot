"""Local task-model target registration hooks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.model_tasks.constants import SUPPORTED_TASK_TYPES
from core.utils.errors import VBotError


class LocalTaskTargetError(VBotError):
    """Raised when a local task target cannot be resolved or executed."""


@dataclass(frozen=True)
class LocalTaskTargetDescriptor:
    """Description of a local task target such as a future Whisper backend."""

    id: str
    label: str
    task_types: tuple[str, ...]
    usable: bool = True
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.id or "/" in self.id or "::" in self.id:
            raise ValueError("Local task target id must be a non-empty local identifier")
        if not self.label:
            raise ValueError("Local task target label is required")
        invalid_task_types = sorted(set(self.task_types) - SUPPORTED_TASK_TYPES)
        if invalid_task_types:
            raise ValueError(f"Unsupported local task types: {', '.join(invalid_task_types)}")
        object.__setattr__(self, "task_types", tuple(dict.fromkeys(self.task_types)))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @property
    def public_id(self) -> str:
        """Return the settings-facing target id."""

        return f"local/{self.id}"


class LocalTaskTargetRegistry:
    """Small registry for local task-model targets.

    The default runtime starts empty. Local engines can register descriptors
    later without being forced through provider credentials or model catalogs.
    """

    def __init__(self, descriptors: list[LocalTaskTargetDescriptor] | None = None) -> None:
        self._descriptors = {descriptor.id: descriptor for descriptor in descriptors or []}

    def register(self, descriptor: LocalTaskTargetDescriptor) -> None:
        """Register or replace one local target descriptor."""

        self._descriptors[descriptor.id] = descriptor

    def list_for_task(self, task_type: str) -> list[LocalTaskTargetDescriptor]:
        """Return local targets that advertise *task_type*."""

        return sorted(
            [
                descriptor
                for descriptor in self._descriptors.values()
                if task_type in descriptor.task_types
            ],
            key=lambda descriptor: descriptor.id,
        )

    def get(self, local_id: str) -> LocalTaskTargetDescriptor:
        """Return a local target descriptor by local id."""

        try:
            return self._descriptors[local_id]
        except KeyError:
            raise LocalTaskTargetError(f"Local task target not found: {local_id}") from None


DEFAULT_LOCAL_TASK_TARGET_REGISTRY = LocalTaskTargetRegistry()
