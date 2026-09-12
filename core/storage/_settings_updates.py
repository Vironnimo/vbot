"""Pure in-memory Settings merges used inside StorageManager transactions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.model_tasks import SUPPORTED_TASK_TYPES
from core.settings import (
    SettingsValidationError,
)
from core.settings.normalizers import (
    coerce_defaults_section,
    normalize_agent_default_value,
    normalize_agent_defaults,
    normalize_appearance_settings,
    normalize_compaction_settings,
    normalize_debug_settings,
    normalize_defaults_settings,
    normalize_extensions_settings,
    normalize_json_object,
    normalize_local_models_settings,
    normalize_model_task_settings,
    normalize_providers_settings,
    normalize_recall_settings,
    normalize_reflection_settings,
    normalize_session_title_settings,
    normalize_skill_directories,
    normalize_speech_settings,
    normalize_subagent_integer,
    normalize_web_search_settings,
    validate_supported_agent_default_fields,
)
from core.settings.paths import SUBAGENT_SETTING_DEFAULTS
from core.settings.settings import effective_timezone_name, validate_timezone_name
from core.storage.errors import StorageError


def apply_appearance_settings(
    settings: dict[str, Any],
    appearance: Mapping[str, Any],
) -> dict[str, str]:
    """Merge Appearance settings into an in-memory settings mapping."""

    if not isinstance(appearance, Mapping):
        raise StorageError("Appearance settings must be a mapping")

    unsupported_fields = sorted(set(appearance) - {"language", "chat_width", "chat_working_mode"})
    if unsupported_fields:
        raise StorageError(f"Unsupported appearance settings: {', '.join(unsupported_fields)}")

    if "language" not in appearance:
        raise StorageError("Appearance settings must include language")

    settings["appearance"] = normalize_appearance_settings(appearance)
    return dict(settings["appearance"])


def apply_speech_settings(
    settings: dict[str, Any],
    speech: Mapping[str, Any],
) -> dict[str, Any]:
    """Replace the complete server-owned speech settings section."""

    normalized = normalize_speech_settings(speech)
    settings["speech"] = normalized
    return normalized


def apply_skill_directory_settings(
    settings: dict[str, Any],
    directories: Any,
) -> list[str]:
    """Merge extra skill directories into an in-memory settings mapping."""

    normalized_directories = normalize_skill_directories(directories)
    settings["skill_directories"] = normalized_directories
    return normalized_directories


def apply_subagent_settings(
    settings: dict[str, Any],
    subagents: Mapping[str, Any],
) -> dict[str, int]:
    """Merge Sub-Agent settings into an in-memory settings mapping."""

    if not isinstance(subagents, Mapping):
        raise StorageError("Sub-agent settings must be a mapping")

    expected_fields = set(SUBAGENT_SETTING_DEFAULTS)
    unsupported_fields = sorted(set(subagents) - expected_fields)
    if unsupported_fields:
        raise StorageError(f"Unsupported sub-agent settings: {', '.join(unsupported_fields)}")

    missing_fields = sorted(expected_fields - set(subagents))
    if missing_fields:
        raise StorageError(f"Missing sub-agent settings: {', '.join(missing_fields)}")

    normalized_subagents = {
        key: normalize_subagent_integer(key, subagents[key], default)
        for key, default in SUBAGENT_SETTING_DEFAULTS.items()
    }
    settings.update(normalized_subagents)
    return normalized_subagents


def apply_session_title_settings(
    settings: dict[str, Any],
    session_titles: Mapping[str, Any],
) -> dict[str, Any]:
    """Replace the complete automatic Session-title settings section."""
    normalized = normalize_session_title_settings(session_titles)
    settings["session_titles"] = normalized
    return normalized


def apply_local_models_settings(
    settings: dict[str, Any],
    local_models: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge local-models settings into an in-memory settings mapping.

    Sparse per-key merge: a ``null`` window removes the key (the model
    falls back to the default cap), an integer sets it, and unmentioned
    keys are preserved.
    """

    if not isinstance(local_models, Mapping):
        raise StorageError("Local-models settings must be a mapping")

    unsupported_fields = sorted(set(local_models) - {"context_windows"})
    if unsupported_fields:
        raise StorageError(f"Unsupported local_models settings: {', '.join(unsupported_fields)}")

    update_windows = local_models.get("context_windows")
    if not isinstance(update_windows, Mapping):
        raise StorageError("local_models.context_windows must be a mapping")

    merged = dict(normalize_local_models_settings(settings.get("local_models"))["context_windows"])
    for key, value in update_windows.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value

    normalized = normalize_local_models_settings({"context_windows": merged})
    settings["local_models"] = normalized
    return dict(normalized)


def apply_providers_settings(
    settings: dict[str, Any],
    providers: Mapping[str, Any],
) -> dict[str, Any]:
    """Replace OpenRouter routing while preserving Connections and Custom Providers."""

    if not isinstance(providers, Mapping):
        raise StorageError("Provider settings must be a mapping")
    unsupported_fields = sorted(set(providers) - {"openrouter"})
    if unsupported_fields:
        raise StorageError(
            f"Unsupported public providers settings: {', '.join(unsupported_fields)}"
        )
    if "openrouter" not in providers:
        raise StorageError("Provider settings must include openrouter")

    current = normalize_providers_settings(settings.get("providers"))
    candidate = {
        "connections": current["connections"],
        "custom": current["custom"],
        "openrouter": providers["openrouter"],
    }
    normalized = normalize_providers_settings(candidate)
    settings["providers"] = normalized
    return dict(normalized)


def apply_reflection_settings(
    settings: dict[str, Any],
    reflection: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge reflection settings into an in-memory settings mapping."""

    if not isinstance(reflection, Mapping):
        raise StorageError("Reflection settings must be a mapping")

    unsupported_fields = sorted(
        set(reflection) - {"enabled", "memory_turn_interval", "skill_model_step_interval"}
    )
    if unsupported_fields:
        raise StorageError(f"Unsupported reflection settings: {', '.join(unsupported_fields)}")

    normalized_reflection = normalize_reflection_settings(
        {
            **normalize_reflection_settings(settings.get("reflection")),
            **dict(reflection),
        }
    )
    settings["reflection"] = normalized_reflection
    return dict(normalized_reflection)


def apply_extensions_settings(
    settings: dict[str, Any],
    extensions: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge the ``extensions`` section into an in-memory settings mapping."""

    if not isinstance(extensions, Mapping):
        raise StorageError("Extensions settings must be a mapping")

    normalized_extensions = normalize_extensions_settings(extensions)
    settings["extensions"] = normalized_extensions
    return dict(normalized_extensions)


def apply_recall_settings(
    settings: dict[str, Any],
    recall: Mapping[str, Any],
) -> dict[str, str]:
    """Merge recall settings into an in-memory settings mapping."""

    if not isinstance(recall, Mapping):
        raise StorageError("Recall settings must be a mapping")

    unsupported_fields = sorted(set(recall) - {"backend"})
    if unsupported_fields:
        raise StorageError(f"Unsupported recall settings: {', '.join(unsupported_fields)}")

    normalized_recall = normalize_recall_settings(recall)
    settings["recall"] = normalized_recall
    return dict(normalized_recall)


def apply_debug_settings(
    settings: dict[str, Any],
    debug: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge debug settings into an in-memory settings mapping."""

    if not isinstance(debug, Mapping):
        raise StorageError("Debug settings must be a mapping")

    unsupported_fields = sorted(set(debug) - {"enabled", "trace_limit"})
    if unsupported_fields:
        raise StorageError(f"Unsupported debug settings: {', '.join(unsupported_fields)}")

    normalized_debug = normalize_debug_settings(
        {
            **normalize_debug_settings(settings.get("debug")),
            **dict(debug),
        }
    )
    settings["debug"] = normalized_debug
    return dict(normalized_debug)


def apply_server_settings(
    settings: dict[str, Any],
    server: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge server settings into an in-memory settings mapping.

    The public ``server`` section persists to flat raw keys; unspecified
    fields keep their stored values (sparse merge).
    """

    if not isinstance(server, Mapping):
        raise StorageError("Server settings must be a mapping")

    unsupported_fields = sorted(set(server) - {"keep_awake", "timezone"})
    if unsupported_fields:
        raise StorageError(f"Unsupported server settings: {', '.join(unsupported_fields)}")

    if "keep_awake" in server:
        keep_awake = server["keep_awake"]
        if not isinstance(keep_awake, bool):
            raise StorageError("Server keep_awake setting must be a boolean")
        settings["keep_awake"] = keep_awake

    if "timezone" in server:
        try:
            settings["timezone"] = validate_timezone_name(
                server["timezone"], label="Server timezone setting"
            )
        except SettingsValidationError as error:
            raise StorageError(str(error)) from error

    return {
        "keep_awake": settings.get("keep_awake") is True,
        "timezone": effective_timezone_name(settings),
    }


def apply_web_search_settings(
    settings: dict[str, Any],
    web_search: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge web search settings into an in-memory settings mapping."""

    if not isinstance(web_search, Mapping):
        raise StorageError("Web search settings must be a mapping")

    unsupported_fields = sorted(set(web_search) - {"provider", "default_count", "searxng"})
    if unsupported_fields:
        raise StorageError(f"Unsupported web_search settings: {', '.join(unsupported_fields)}")

    current_settings = normalize_web_search_settings(settings.get("web_search"))
    raw_searxng_update = web_search.get("searxng", {})
    if raw_searxng_update is None:
        raw_searxng_update = {}
    if not isinstance(raw_searxng_update, Mapping):
        raise StorageError("Expected settings.web_search.searxng to be an object")

    normalized_web_search = normalize_web_search_settings(
        {
            **current_settings,
            **dict(web_search),
            "searxng": {
                **current_settings["searxng"],
                **dict(raw_searxng_update),
            },
        }
    )
    settings["web_search"] = normalized_web_search
    return dict(normalized_web_search)


def apply_model_task_settings(
    settings: dict[str, Any],
    model_tasks: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Merge task-model bindings into an in-memory settings mapping."""

    merged_model_tasks = normalize_model_task_settings(settings.get("model_tasks"))

    for task_type, raw_binding in model_tasks.items():
        if task_type not in SUPPORTED_TASK_TYPES:
            raise StorageError(f"Unsupported model task type: {task_type}")
        if not isinstance(raw_binding, Mapping):
            raise StorageError(f"Model task binding {task_type} must be a mapping")

        unsupported_fields = sorted(set(raw_binding) - {"target", "options"})
        if unsupported_fields:
            raise StorageError(
                f"Unsupported model task settings for {task_type}: {', '.join(unsupported_fields)}"
            )

        current_binding = dict(merged_model_tasks.get(task_type, {}))
        target = current_binding.get("target", "")
        if "target" in raw_binding:
            raw_target = raw_binding["target"]
            if not isinstance(raw_target, str):
                raise StorageError(f"Model task target for {task_type} must be a string")
            target = raw_target.strip()

        if not target:
            merged_model_tasks.pop(task_type, None)
            continue

        options = current_binding.get("options", {})
        if "options" in raw_binding:
            options = normalize_json_object(
                raw_binding["options"],
                f"settings.model_tasks.{task_type}.options",
            )

        merged_model_tasks[task_type] = {
            "target": target,
            "options": options,
        }

    if merged_model_tasks:
        settings["model_tasks"] = merged_model_tasks
    else:
        settings.pop("model_tasks", None)

    return normalize_model_task_settings(settings.get("model_tasks"))


def apply_defaults(
    settings: dict[str, Any],
    section: str,
    values: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge defaults into an in-memory settings mapping."""

    merged_defaults = coerce_defaults_section(settings.get("defaults"))

    if section == "agent":
        current_agent_defaults = normalize_agent_defaults(merged_defaults.get("agent"))
        validate_supported_agent_default_fields(values)
        for field, value in values.items():
            normalized_value = normalize_agent_default_value(field, value)
            if normalized_value is None:
                current_agent_defaults.pop(field, None)
                continue
            current_agent_defaults[field] = normalized_value

        if current_agent_defaults:
            merged_defaults["agent"] = current_agent_defaults
        else:
            merged_defaults.pop("agent", None)

    if merged_defaults:
        settings["defaults"] = merged_defaults
    else:
        settings.pop("defaults", None)

    return normalize_defaults_settings(merged_defaults)


def apply_compaction_settings(
    settings: dict[str, Any],
    compaction: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge compaction settings into an in-memory settings mapping."""

    if not isinstance(compaction, Mapping):
        raise StorageError("Compaction settings must be a mapping")

    normalized_compaction = normalize_compaction_settings(
        {
            **normalize_compaction_settings(settings.get("compaction")),
            **dict(compaction),
        }
    )
    settings["compaction"] = normalized_compaction
    return dict(normalized_compaction)
