"""Live Settings coordination within the Runtime owner."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.runtime.runtime import Runtime


async def apply_settings_change(
    runtime: Runtime,
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    refresh_sections: Collection[str] = (),
) -> bool:
    """Refresh live consumers after persistence; return Command catalog invalidation."""
    newly_enabled = _disabled_names(previous) - _disabled_names(current)
    newly_disabled = _disabled_names(current) - _disabled_names(previous)
    rebuild_extensions = bool(newly_enabled) or previous.get(
        "extension_directories"
    ) != current.get("extension_directories")

    if rebuild_extensions:
        # Full rebuild already refreshes Recall, Prompts and Skills against the
        # replacement registry and applies the complete persisted disabled set.
        await runtime.reload_extensions()
    elif newly_disabled:
        # Deactivation refreshes Prompts and Skills, but only recovers Recall
        # when a removed Extension supplied the selected backend. It does not
        # apply an independently changed Recall selection.
        await runtime.apply_extension_disabled_change(newly_disabled)

    skills_changed = "skills" in refresh_sections or previous.get(
        "skill_directories"
    ) != current.get("skill_directories")
    if skills_changed and not (rebuild_extensions or newly_disabled):
        await runtime.reload_skills_async()

    recall_changed = "recall" in refresh_sections or previous.get("recall") != current.get("recall")
    if recall_changed and not rebuild_extensions:
        runtime.reload_recall_backend()

    if previous.get("keep_awake") != current.get("keep_awake"):
        runtime.reload_keep_awake()
    if previous.get("timezone") != current.get("timezone"):
        runtime.reload_timezone()

    return rebuild_extensions or bool(newly_disabled)


def _disabled_names(settings: Mapping[str, Any]) -> set[str]:
    extensions = settings.get("extensions")
    disabled = extensions.get("disabled") if isinstance(extensions, dict) else None
    # Raw Settings accept null as the default empty disabled set.
    return set(disabled) if isinstance(disabled, list) else set()
