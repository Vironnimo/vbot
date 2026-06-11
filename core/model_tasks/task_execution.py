"""Shared binding-resolution plumbing for task execution services.

Every task execution service (speech, image, embeddings) starts a request the
same way: resolve the configured binding for its task type, merge stored
options over the backend schema defaults, and parse the target id — mapping
every ``TaskModelError`` to the task's own configuration-error class.
:class:`TaskBindingResolver` owns that sequence once.
"""

from __future__ import annotations

from typing import Any

from core.model_tasks.model_tasks import (
    TaskModelBinding,
    TaskModelError,
    TaskModelTargetRef,
    parse_task_model_target_id,
)
from core.utils.errors import TaskError

JsonObject = dict[str, Any]


class TaskBindingResolver:
    """Resolve one task type to its (binding, options, target) triple."""

    def __init__(self, model_tasks: Any, *, configuration_error: type[TaskError]) -> None:
        self._model_tasks = model_tasks
        self._configuration_error = configuration_error

    def resolve(self, task_type: str) -> tuple[TaskModelBinding, JsonObject, TaskModelTargetRef]:
        """Return the configured binding, merged options, and parsed target ref."""
        binding = self.binding_for(task_type)
        options = self._model_tasks.options_with_defaults(binding)
        return binding, options, self.parse_target(binding.target)

    def binding_for(self, task_type: str) -> TaskModelBinding:
        try:
            return self._model_tasks.binding_for(task_type)  # type: ignore[no-any-return]
        except TaskModelError as exc:
            raise self._configuration_error(str(exc)) from exc

    def parse_target(self, target: str) -> TaskModelTargetRef:
        try:
            return parse_task_model_target_id(target)
        except TaskModelError as exc:
            raise self._configuration_error(str(exc)) from exc
