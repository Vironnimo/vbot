"""Local task-model target registration hooks.

Local targets bypass Provider catalogs and credentials. Each execution owner
supplies descriptors with option schemas and a live availability check. Runtime
registers the speech executor's catalog; catalog entries alone cannot execute.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.model_tasks.constants import SUPPORTED_TASK_TYPES
from core.model_tasks.options import TaskModelOptionField
from core.utils.errors import VBotError


class LocalTaskTargetError(VBotError):
    """Raised when a local task target cannot be resolved or executed."""


@dataclass(frozen=True)
class LocalTaskTargetDescriptor:
    """Description of a local task target.

    ``option_fields`` carries the descriptor-owned option schema the
    Settings UI should render for this local engine, in the same
    :class:`TaskModelOptionField` shape used by provider option schemas.
    Availability checks must be inexpensive and must not load model weights.
    """

    id: str
    label: str
    task_types: tuple[str, ...]
    usable: bool = True
    metadata: dict[str, Any] | None = None
    option_fields: tuple[TaskModelOptionField, ...] = ()
    availability: Callable[[], bool] | None = None

    def can_execute(self) -> bool:
        """Require an execution owner's live preflight, not just a catalog entry."""
        return self.usable and self.availability is not None and self.availability()

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
        object.__setattr__(self, "option_fields", tuple(self.option_fields))

    @property
    def public_id(self) -> str:
        """Return the settings-facing target id."""

        return f"local/{self.id}"


class LocalTaskTargetRegistry:
    """Small registry for local task-model targets.

    Execution owners supply descriptors without Provider credentials or catalogs.
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
