"""Live Settings coordination within the Runtime owner."""

from __future__ import annotations

import os
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from core.model_tasks.constants import TASK_SPEECH_TO_TEXT, TASK_TEXT_TO_SPEECH
from core.prompts import SkillPromptRegistry
from core.runtime._configuration import _resolve_resources_path
from core.runtime._recall import text_embedding_binding
from core.runtime._workers import _RUNTIME_WORKERS
from core.settings.normalizers import normalize_debug_settings
from core.settings.settings import effective_timezone_name
from core.skills.skills import SkillRegistry
from core.tools import register_skill_manage_tool, register_skill_tool
from core.utils.logging import get_logger
from core.utils.workers import SharedRefresh

if TYPE_CHECKING:
    from core.runtime.runtime import Runtime

_LOGGER = get_logger("runtime.settings")
_SPEECH_TASKS = (TASK_SPEECH_TO_TEXT, TASK_TEXT_TO_SPEECH)


@dataclass(frozen=True)
class SettingsChangeEffects:
    """Which operator-visible catalogs a live Settings change may have changed.

    ``commands_changed``: the Extension layer was rebuilt or narrowed, so its
    Commands and page registrations may differ. ``skills_changed``: the Skill
    layer was rescanned (Skill directories, or Extension Skill folders), so the
    Skill inventory may differ. ``agents_changed``: the shared Agent defaults
    (``defaults.agent``, the default Permission Rules included) changed, so
    every Agent's effective configuration may differ.
    """

    commands_changed: bool = False
    skills_changed: bool = False
    agents_changed: bool = False


async def apply_settings_change(
    runtime: Runtime,
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    refresh_sections: Collection[str] = (),
) -> SettingsChangeEffects:
    """Refresh live consumers after persistence; return the catalogs to invalidate."""
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
        runtime.reload_recall_backend(current)
    if text_embedding_binding(previous) != text_embedding_binding(current):
        runtime.recall.embedding_binding_changed()

    if previous.get("archive") != current.get("archive"):
        runtime.archive.retention_changed()
    if previous.get("keep_awake") != current.get("keep_awake"):
        runtime.reload_keep_awake(current)
    if previous.get("timezone") != current.get("timezone"):
        runtime.reload_timezone(current)
    changed_speech = [
        task_type
        for task_type in _SPEECH_TASKS
        if _binding(previous, task_type) != _binding(current, task_type)
    ]
    if TASK_SPEECH_TO_TEXT in changed_speech:
        runtime.speech.transcription_binding_changed()
    if changed_speech:
        runtime.speech.preload_configured(changed_speech)
    debug_enabled = _debug_enabled(current)
    if _debug_enabled(previous) != debug_enabled:
        # Provider traffic capture is a privacy-relevant mode an operator must see.
        _LOGGER.info("Debug Mode %s", "enabled" if debug_enabled else "disabled")

    extension_layer_changed = rebuild_extensions or bool(newly_disabled)
    return SettingsChangeEffects(
        commands_changed=extension_layer_changed,
        skills_changed=extension_layer_changed or skills_changed,
        agents_changed=_agent_defaults(previous) != _agent_defaults(current),
    )


def _agent_defaults(settings: Mapping[str, Any]) -> Any:
    defaults = settings.get("defaults")
    return defaults.get("agent") if isinstance(defaults, dict) else None


def _debug_enabled(settings: Mapping[str, Any]) -> bool:
    return bool(normalize_debug_settings(settings.get("debug"))["enabled"])


def _binding(settings: Mapping[str, Any], task_type: str) -> Any:
    model_tasks = settings.get("model_tasks")
    return model_tasks.get(task_type) if isinstance(model_tasks, dict) else None


def _disabled_names(settings: Mapping[str, Any]) -> set[str]:
    extensions = settings.get("extensions")
    disabled = extensions.get("disabled") if isinstance(extensions, dict) else None
    # Raw Settings accept null as the default empty disabled set.
    return set(disabled) if isinstance(disabled, list) else set()


def reload_skills(runtime: Runtime) -> None:
    """Reload the runtime skill registry from current persisted settings."""
    owner = runtime._skill_operations()
    runtime._skill_reload_required = True
    generation = runtime._skill_reload_generation = object()
    publish_skills(runtime, owner.load_global_registry(), generation=generation)


async def reload_skills_async(runtime: Runtime, *, only_if_changed: bool = False) -> None:
    """Read Skills off the loop; an editor refresh can preserve unchanged scopes.

    Concurrent callers share rescans: each waits for one that started after its
    call, so it sees every change made before it asked (``SharedRefresh``).
    """
    runtime._skill_operations()  # Fails fast while Skills are unavailable.
    runtime._skill_reload_required |= not only_if_changed
    reloads = runtime._skill_reloads
    if reloads is None:
        reloads = runtime._skill_reloads = SharedRefresh(
            partial(_rescan_skills, runtime), name="skill-reload"
        )
    await reloads.run()


async def _rescan_skills(runtime: Runtime) -> None:
    owner = runtime._skill_operations()
    generation = runtime._skill_reload_generation = object()
    credential_snapshot = runtime._fallback_environment
    skills = await _RUNTIME_WORKERS.run(owner.load_global_registry)
    if credential_snapshot is not runtime._fallback_environment:
        # A credential reload updates held registries while the scan runs.
        # Rebase its environment without discarding newly scanned packages.
        environment = dict(runtime._fallback_environment)
        environment.update(os.environ)
        skills.reload_environment(environment)
    # A later editor refresh inherits any explicit reload it superseded:
    # unchanged global metadata must not swallow that cache invalidation.
    if (
        not runtime._skill_reload_required
        and runtime._skills is not None
        and runtime._skills.same_catalog(skills)
    ):
        return
    publish_skills(runtime, skills, generation=generation)


def publish_skills(runtime: Runtime, skills: SkillRegistry, *, generation: object) -> None:
    """Install a fully built Skill registry and refresh its loop-owned consumers."""
    if not runtime._started or generation is not runtime._skill_reload_generation:
        return
    runtime._skill_reload_required = False
    runtime._skills = skills
    runtime._skill_operations().replace_registry(skills)
    invalid_skill_count = len(runtime._skills.invalid_diagnostics())
    if runtime.logger is not None:
        # Routine cache maintenance (fires on every project open), not an event.
        runtime.logger.debug("Reloaded skill registry")
        if invalid_skill_count > 0:
            runtime.logger.warning(
                "Reloaded skills with %s invalid skill directories; "
                "see vbot.skills warnings for details",
                invalid_skill_count,
            )
    if runtime._tools is not None:
        runtime._tools.unregister("skill")
        register_skill_tool(
            runtime._tools,
            runtime.skills_for,
            runtime.reload_skills_async,
            runtime.archived_skill,
            runtime.background_skill_protection,
        )
        if runtime._skill_authoring is not None:
            runtime._tools.unregister("skill_manage")
            register_skill_manage_tool(
                runtime._tools,
                runtime._skill_authoring,
                runtime.agent_skills_dir,
                runtime.invalidate_agent_skills,
                runtime._resolve_shared_skills_dir,
                runtime._resolve_external_skill_scope,
                lifecycle_guard=runtime.agents.lifecycle_guard,
                on_changed=runtime._notify_skills_changed,
                run_started_at=runtime.run_started_at,
                follow_merge=runtime.follow_skill_merge,
                resolve_global_skills_dir=lambda: runtime.global_skills_dir,
                publish_skill=runtime._publish_agent_skill,
                refresh_global_skills=runtime.reload_skills_async,
            )
    if runtime._system_prompts is not None:
        runtime._system_prompts.update_skill_registry(cast(SkillPromptRegistry, runtime._skills))
        runtime._refresh_prompt_block_definitions()


def reload_environment_credentials(runtime: Runtime) -> None:
    """Reload shared credential fallback values from the Secret Store."""
    runtime._ensure_started()
    generation = runtime._credential_reload_generation = object()
    publish_credentials(runtime, runtime.storage.load_environment(), generation=generation)


async def reload_environment_credentials_async(runtime: Runtime) -> None:
    """Read credentials off-loop before updating the live resolver and Skill snapshots."""
    runtime._ensure_started()
    generation = runtime._credential_reload_generation = object()
    load_environment = runtime.storage.load_environment
    credentials = await _RUNTIME_WORKERS.run(load_environment)
    publish_credentials(runtime, credentials, generation=generation)


def publish_credentials(
    runtime: Runtime, credentials: Mapping[str, str], *, generation: object
) -> None:
    if not runtime._started or generation is not runtime._credential_reload_generation:
        return
    runtime._fallback_environment = dict(credentials)
    runtime.provider_credentials.reload_fallback_credentials(credentials)
    runtime._provider_operations().retire_http_transports()
    runtime.agent_secrets.set_system_values(credentials)
    runtime._skill_operations().reload_environment(dict(credentials))


def reload_custom_providers(runtime: Runtime) -> None:
    """Reload Settings-owned Provider, Model and wire profile overlays in place."""

    runtime._ensure_started()
    runtime._provider_reload_generation = object()
    resources_path = _resolve_resources_path(runtime.config)
    custom_providers = runtime.storage.load_custom_providers_settings()
    runtime.providers.reload(
        resources_path,
        custom_providers=custom_providers,
        tolerate_invalid=True,
    )
    runtime.models.reload(
        resources_path,
        runtime_models_dir=runtime.storage.layout.models,
        custom_providers=custom_providers,
    )
    runtime._provider_operations().reload_custom_wire_profiles(custom_providers)


async def reload_custom_providers_async(runtime: Runtime) -> None:
    """Read persisted Provider overlays off-loop and publish registries on the caller."""
    runtime._ensure_started()
    generation = runtime._provider_reload_generation = object()
    storage, providers, models = runtime.storage, runtime.providers, runtime.models
    provider_runtime = runtime._provider_operations()
    resources_path = _resolve_resources_path(runtime.config)
    custom = await _RUNTIME_WORKERS.run(storage.load_custom_providers_settings)
    if not runtime._started or generation is not runtime._provider_reload_generation:
        return
    await providers.reload_async(resources_path, custom_providers=custom, tolerate_invalid=True)
    if not runtime._started or generation is not runtime._provider_reload_generation:
        return
    await models.reload_async(
        resources_path, runtime_models_dir=storage.layout.models, custom_providers=custom
    )
    if runtime._started and generation is runtime._provider_reload_generation:
        provider_runtime.reload_custom_wire_profiles(custom)


def reload_keep_awake(runtime: Runtime, settings: Mapping[str, Any] | None = None) -> None:
    """Apply the persisted ``keep_awake`` setting to the running process."""

    runtime._ensure_started()
    if runtime._storage is None or runtime._keep_awake is None:
        return
    if settings is None:
        settings = runtime._storage.load_settings()
    runtime._keep_awake.set_enabled(settings.get("keep_awake") is True)


def reload_timezone(runtime: Runtime, settings: Mapping[str, Any] | None = None) -> None:
    """Apply the persisted timezone to all live wall-clock services."""
    runtime._ensure_started()
    timezone_name = (
        runtime.timezone_name() if settings is None else effective_timezone_name(dict(settings))
    )
    if runtime._cron_service is not None:
        runtime._cron_service.set_timezone(timezone_name)
    if runtime._calendar_service is not None:
        runtime._calendar_service.set_timezone(timezone_name)
