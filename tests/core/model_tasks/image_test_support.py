"""Shared fixtures and fakes for image behavior tests."""

from __future__ import annotations

from core.model_tasks import (
    TaskModelError,
)


class _MissingModelTasks:
    def binding_for(self, _task_type: str) -> object:
        raise TaskModelError("No task model configured")
