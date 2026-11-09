"""Persisted Task Model fallback entries, shared by all Settings surfaces."""

from collections.abc import Mapping
from typing import Any

from core.model_tasks.constants import TASK_TEXT_EMBEDDING
from core.utils.errors import StorageError

MAX_TASK_FALLBACKS = 8


def normalize_fallbacks(
    task_type: str, value: Any, *, stored: bool = False, primary_target: str | None = None
) -> list[dict[str, Any]]:
    # Imported at use: Settings imports this lightweight shape owner itself.
    from core.model_tasks.model_tasks import (
        TaskModelError,
        parse_task_model_target_id,
        task_model_targets_equal,
    )
    from core.settings._json_settings import normalize_json_object

    if not isinstance(value, list) or len(value) > MAX_TASK_FALLBACKS:
        raise StorageError(
            f"{task_type}.fallbacks must be a list of at most {MAX_TASK_FALLBACKS} entries"
        )
    if task_type == TASK_TEXT_EMBEDDING and value:
        raise StorageError("text_embedding uses keyword search on failure, not Model fallbacks")
    result: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, Mapping) or (not stored and set(entry) - {"target", "options"}):
            raise StorageError(f"{task_type}.fallbacks entries require target and optional options")
        if "fallbacks" in entry:
            raise StorageError(f"{task_type}.fallbacks entries must not contain nested fallbacks")
        target = entry.get("target")
        if not isinstance(target, str) or not target.strip():
            raise StorageError(f"{task_type}.fallbacks targets must be non-empty strings")
        target = target.strip()
        try:
            parse_task_model_target_id(target)
        except TaskModelError as exc:
            raise StorageError(f"Invalid {task_type}.fallbacks target") from exc
        if task_model_targets_equal(target, primary_target):
            raise StorageError("A fallback must differ from the primary target")
        if any(task_model_targets_equal(target, previous["target"]) for previous in result):
            raise StorageError(f"{task_type}.fallbacks must not repeat a target")
        result.append(
            {
                "target": target,
                "options": normalize_json_object(
                    entry.get("options", {}), f"{task_type}.fallbacks.options"
                ),
            }
        )
    return result
