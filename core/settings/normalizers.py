"""Stateless Settings section normalization with internal Provider and Compaction rules."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, cast

from core.model_tasks import SUPPORTED_TASK_TYPES
from core.model_tasks.constants import (
    DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS,
    SUPPORTED_TRANSCRIPTION_AUDIO_FORMATS,
    SUPPORTED_TRANSCRIPTION_AUDIO_PROFILES,
    SUPPORTED_TRANSCRIPTION_AUDIO_SAMPLE_RATES,
    TRANSCRIPTION_AUDIO_PRESETS,
)
from core.search_config import (
    DEFAULT_SEARXNG_BASE_URL,
    DEFAULT_WEB_SEARCH_COUNT,
    DEFAULT_WEB_SEARCH_PROVIDER,
    FIRST_PARTY_WEB_SEARCH_PROVIDERS,
    MAX_WEB_SEARCH_COUNT,
    MIN_WEB_SEARCH_COUNT,
)
from core.settings._compaction_settings import (
    COMPACTION_SETTING_DEFAULTS,
    normalize_compaction_policy,
    normalize_compaction_settings,
)
from core.settings._json_settings import (
    normalize_json_object,
)
from core.settings._provider_settings import (
    CUSTOM_MODEL_CAPABILITY_FIELDS,
    CUSTOM_PROVIDER_ADAPTERS,
    CUSTOM_PROVIDER_AUTH_TYPES,
    CUSTOM_PROVIDER_CONNECTION_ID,
    CUSTOM_PROVIDER_ID_PATTERN,
    CUSTOM_PROVIDER_MODEL_FIELDS,
    normalize_custom_provider_id,
    normalize_custom_provider_settings,
    normalize_custom_providers_settings,
    normalize_local_models_settings,
    normalize_providers_settings,
)
from core.settings.agent_defaults import AGENT_DEFAULT_FIELDS, normalize_agent_default_value
from core.settings.settings import (
    DEFAULT_APPEARANCE_CHAT_WIDTH,
    DEFAULT_APPEARANCE_CHAT_WORKING_MODE,
    SUPPORTED_APPEARANCE_CHAT_WIDTHS,
    SUPPORTED_APPEARANCE_CHAT_WORKING_MODES,
)
from core.utils.errors import StorageError

DEFAULT_APPEARANCE_LANGUAGE = "en"


SUPPORTED_APPEARANCE_LANGUAGES = frozenset({DEFAULT_APPEARANCE_LANGUAGE})


DEFAULT_RECALL_SETTINGS = {"backend": "sqlite_fts"}


DEFAULT_WEB_SEARCH_SETTINGS = {
    "provider": DEFAULT_WEB_SEARCH_PROVIDER,
    "default_count": DEFAULT_WEB_SEARCH_COUNT,
    "searxng": {"base_url": DEFAULT_SEARXNG_BASE_URL},
}


DEFAULT_SESSION_TITLE_SETTINGS = {"enabled": False, "model": ""}


DEBUG_SETTING_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "trace_limit": 50,
}


REFLECTION_SETTING_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "memory_turn_interval": 10,
    "skill_model_step_interval": 10,
}


def normalize_speech_settings(speech: Any) -> dict[str, Any]:
    """Return the normalized server-owned speech settings section."""

    if speech is None:
        section: Mapping[str, Any] = {}
    elif isinstance(speech, Mapping):
        section = speech
    else:
        raise StorageError("Speech settings must be a mapping")

    unsupported_fields = sorted(set(section) - {"transcription_audio"})
    if unsupported_fields:
        raise StorageError(f"Unsupported speech settings: {', '.join(unsupported_fields)}")

    return {
        "transcription_audio": normalize_transcription_audio_settings(
            section.get("transcription_audio")
        )
    }


def normalize_transcription_audio_settings(value: Any) -> dict[str, Any]:
    """Return one complete Provider-facing transcription audio profile."""

    if value is None:
        return dict(DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS)
    if not isinstance(value, Mapping):
        raise StorageError("speech.transcription_audio must be a mapping")

    unsupported_fields = sorted(set(value) - {"profile", "format", "sample_rate_hz"})
    if unsupported_fields:
        raise StorageError(
            f"Unsupported speech.transcription_audio settings: {', '.join(unsupported_fields)}"
        )

    profile = value.get("profile", DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS["profile"])
    if not isinstance(profile, str) or profile not in SUPPORTED_TRANSCRIPTION_AUDIO_PROFILES:
        allowed = ", ".join(sorted(SUPPORTED_TRANSCRIPTION_AUDIO_PROFILES))
        raise StorageError(f"speech.transcription_audio.profile must be one of: {allowed}")

    preset = TRANSCRIPTION_AUDIO_PRESETS.get(profile)
    if preset is not None:
        for field, preset_value in preset.items():
            configured = value.get(field, preset_value)
            if configured != preset_value:
                raise StorageError(
                    f"speech.transcription_audio.{field} must be {preset_value!r} "
                    f"for profile {profile!r}"
                )
        return {"profile": profile, **preset}

    audio_format = value.get("format", DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS["format"])
    if (
        not isinstance(audio_format, str)
        or audio_format not in SUPPORTED_TRANSCRIPTION_AUDIO_FORMATS
    ):
        allowed = ", ".join(sorted(SUPPORTED_TRANSCRIPTION_AUDIO_FORMATS))
        raise StorageError(f"speech.transcription_audio.format must be one of: {allowed}")

    sample_rate_hz = value.get(
        "sample_rate_hz",
        DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS["sample_rate_hz"],
    )
    if (
        not isinstance(sample_rate_hz, int)
        or isinstance(sample_rate_hz, bool)
        or sample_rate_hz not in SUPPORTED_TRANSCRIPTION_AUDIO_SAMPLE_RATES
    ):
        allowed = ", ".join(
            str(sample_rate) for sample_rate in sorted(SUPPORTED_TRANSCRIPTION_AUDIO_SAMPLE_RATES)
        )
        raise StorageError(f"speech.transcription_audio.sample_rate_hz must be one of: {allowed}")

    return {
        "profile": profile,
        "format": audio_format,
        "sample_rate_hz": sample_rate_hz,
    }


def normalize_appearance_settings(appearance: Any) -> dict[str, str]:
    """Return the normalized Appearance settings subset."""

    section = _coerce_appearance_section(appearance)
    return {
        "language": _normalize_appearance_language(section),
        "chat_width": _normalize_appearance_chat_width(section),
        "chat_working_mode": _normalize_appearance_chat_working_mode(section),
    }


def _normalize_appearance_language(section: Mapping[str, Any]) -> str:
    value = section.get("language")
    if value is None:
        return DEFAULT_APPEARANCE_LANGUAGE
    return _validate_appearance_language(value)


def _normalize_appearance_chat_width(section: Mapping[str, Any]) -> str:
    # Unlike language, an unknown chat_width is a display-only preference, so a
    # missing or invalid value normalizes to the comfortable default rather than
    # raising. Public updates are still rejected by the settings parser.
    value = section.get("chat_width")
    if value not in SUPPORTED_APPEARANCE_CHAT_WIDTHS:
        return DEFAULT_APPEARANCE_CHAT_WIDTH
    return cast(str, value)


def _normalize_appearance_chat_working_mode(section: Mapping[str, Any]) -> str:
    value = section.get("chat_working_mode")
    if value not in SUPPORTED_APPEARANCE_CHAT_WORKING_MODES:
        return DEFAULT_APPEARANCE_CHAT_WORKING_MODE
    return cast(str, value)


def _coerce_appearance_section(appearance: Any) -> dict[str, Any]:
    if appearance is None:
        return {}
    if not isinstance(appearance, Mapping):
        raise StorageError("Expected settings.appearance to be an object")
    return dict(appearance)


def _validate_appearance_language(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise StorageError("Appearance language must be a non-empty string")
    if value not in SUPPORTED_APPEARANCE_LANGUAGES:
        supported = ", ".join(sorted(SUPPORTED_APPEARANCE_LANGUAGES))
        raise StorageError(f"Unsupported appearance language: {value}. Supported: {supported}")
    return value


def coerce_skills_update(skills: Any) -> dict[str, Any]:
    """Validate the shape of a Skills settings update and return it as a dict."""

    if not isinstance(skills, Mapping):
        raise StorageError("Skills settings must be a mapping")
    unsupported_fields = sorted(set(skills) - {"directories"})
    if unsupported_fields:
        raise StorageError(f"Unsupported skills settings: {', '.join(unsupported_fields)}")
    if "directories" not in skills:
        raise StorageError("Skills settings must include directories")
    return dict(skills)


def normalize_skill_directories(directories: Any) -> list[str]:
    """Return the normalized extra skill directory list."""

    if directories is None:
        return []
    if not isinstance(directories, list):
        raise StorageError("settings.skill_directories must be a list")

    normalized_directories: list[str] = []
    for directory in directories:
        if not isinstance(directory, str) or not directory.strip():
            raise StorageError("Skill directories must be non-empty strings")
        normalized_directory = directory.strip()
        if not is_absolute_or_home_relative_path(normalized_directory):
            raise StorageError(
                "Skill directories must be absolute paths or home-relative paths starting with ~"
            )
        normalized_directories.append(normalized_directory)
    return normalized_directories


def normalize_subagent_integer(key: str, value: Any, default: int) -> int:
    """Return a positive integer sub-agent setting, falling back to ``default``."""

    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError(f"Sub-agent setting {key} must be an integer")
    if value <= 0:
        raise StorageError(f"Sub-agent setting {key} must be positive")
    return cast("int", value)


def coerce_defaults_update(defaults: Any) -> dict[str, Any]:
    """Validate the shape of a Defaults settings update and return it as a dict."""

    if not isinstance(defaults, Mapping):
        raise StorageError("Defaults settings must be a mapping")
    unsupported_sections = sorted(set(defaults) - {"agent"})
    if unsupported_sections:
        raise StorageError(f"Unsupported defaults settings: {', '.join(unsupported_sections)}")
    if "agent" not in defaults:
        raise StorageError("Defaults settings must include agent")
    if not isinstance(defaults["agent"], Mapping):
        raise StorageError("Defaults agent settings must be a mapping")
    return dict(defaults)


def normalize_defaults_settings(defaults: Any) -> dict[str, Any]:
    """Return the normalized defaults settings section."""

    section = coerce_defaults_section(defaults)
    normalized_agent_defaults = normalize_agent_defaults(section.get("agent"))
    if not normalized_agent_defaults:
        return {}
    return {"agent": normalized_agent_defaults}


def normalize_session_title_settings(session_titles: Any) -> dict[str, Any]:
    """Return the complete automatic Session-title settings section."""
    if session_titles is None:
        return dict(DEFAULT_SESSION_TITLE_SETTINGS)
    if not isinstance(session_titles, Mapping):
        raise StorageError("Expected settings.session_titles to be an object")

    unsupported_fields = sorted(set(session_titles) - {"enabled", "model"})
    if unsupported_fields:
        raise StorageError(f"Unsupported session_titles settings: {', '.join(unsupported_fields)}")

    enabled = session_titles.get("enabled", DEFAULT_SESSION_TITLE_SETTINGS["enabled"])
    if not isinstance(enabled, bool):
        raise StorageError("Session-title enabled setting must be a boolean")

    model = session_titles.get("model", DEFAULT_SESSION_TITLE_SETTINGS["model"])
    if not isinstance(model, str):
        raise StorageError("Session-title model setting must be a string")
    return {"enabled": enabled, "model": model.strip()}


def coerce_defaults_section(defaults: Any) -> dict[str, Any]:
    """Coerce the top-level ``defaults`` section into a plain dict."""

    if defaults is None:
        return {}
    if not isinstance(defaults, Mapping):
        raise StorageError("Expected settings.defaults to be an object")
    return dict(defaults)


def normalize_agent_defaults(defaults: Any) -> dict[str, Any]:
    """Return the normalized ``defaults.agent`` mapping (omitting null fields)."""

    section = _coerce_agent_defaults_section(defaults)
    validate_supported_agent_default_fields(section)

    normalized_agent_defaults: dict[str, Any] = {}
    for field, value in section.items():
        normalized_value = normalize_agent_default_value(field, value)
        if normalized_value is None:
            continue
        normalized_agent_defaults[field] = normalized_value
    return normalized_agent_defaults


def _coerce_agent_defaults_section(defaults: Any) -> dict[str, Any]:
    if defaults is None:
        return {}
    if not isinstance(defaults, Mapping):
        raise StorageError("Expected settings.defaults.agent to be an object")
    return dict(defaults)


def validate_supported_agent_default_fields(values: Mapping[str, Any]) -> None:
    """Raise when ``defaults.agent`` carries an unsupported field."""

    unsupported_fields = sorted(set(values) - AGENT_DEFAULT_FIELDS)
    if unsupported_fields:
        raise StorageError(f"Unsupported defaults.agent settings: {', '.join(unsupported_fields)}")


def normalize_recall_settings(recall: Any) -> dict[str, str]:
    """Return the normalized recall backend settings section."""

    section = _coerce_recall_section(recall)
    backend = section.get("backend", DEFAULT_RECALL_SETTINGS["backend"])
    if not isinstance(backend, str) or not backend.strip():
        raise StorageError("Recall backend must be a non-empty string")
    return {"backend": backend.strip()}


def _coerce_recall_section(recall: Any) -> dict[str, Any]:
    if recall is None:
        return {}
    if not isinstance(recall, Mapping):
        raise StorageError("Expected settings.recall to be an object")
    return dict(recall)


def normalize_debug_settings(debug: Any) -> dict[str, Any]:
    """Return the normalized debug settings section."""

    section = _coerce_debug_section(debug)
    return {
        "enabled": _normalize_debug_enabled(section.get("enabled")),
        "trace_limit": _normalize_debug_trace_limit(section.get("trace_limit")),
    }


def _coerce_debug_section(debug: Any) -> dict[str, Any]:
    if debug is None:
        return {}
    if not isinstance(debug, Mapping):
        raise StorageError("Expected settings.debug to be an object")
    return dict(debug)


def _normalize_debug_enabled(value: Any) -> bool:
    if value is None:
        return cast("bool", DEBUG_SETTING_DEFAULTS["enabled"])
    if not isinstance(value, bool):
        raise StorageError("Debug setting enabled must be a boolean")
    return value


def _normalize_debug_trace_limit(value: Any) -> int:
    if value is None:
        return cast("int", DEBUG_SETTING_DEFAULTS["trace_limit"])
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError("Debug setting trace_limit must be an integer")
    if value <= 0:
        raise StorageError("Debug setting trace_limit must be positive")
    if value > 500:
        raise StorageError("Debug setting trace_limit must be at most 500")
    return value


def normalize_reflection_settings(reflection: Any) -> dict[str, Any]:
    """Return the normalized background-reflection settings section."""

    section = _coerce_reflection_section(reflection)
    return {
        "enabled": _normalize_reflection_enabled(section.get("enabled")),
        "memory_turn_interval": _normalize_reflection_interval(
            "memory_turn_interval", section.get("memory_turn_interval")
        ),
        "skill_model_step_interval": _normalize_reflection_interval(
            "skill_model_step_interval", section.get("skill_model_step_interval")
        ),
    }


def _coerce_reflection_section(reflection: Any) -> dict[str, Any]:
    if reflection is None:
        return {}
    if not isinstance(reflection, Mapping):
        raise StorageError("Expected settings.reflection to be an object")
    return dict(reflection)


def _normalize_reflection_enabled(value: Any) -> bool:
    if value is None:
        return cast("bool", REFLECTION_SETTING_DEFAULTS["enabled"])
    if not isinstance(value, bool):
        raise StorageError("Reflection setting enabled must be a boolean")
    return value


def _normalize_reflection_interval(key: str, value: Any) -> int:
    if value is None:
        return cast("int", REFLECTION_SETTING_DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError(f"Reflection setting {key} must be an integer")
    if value <= 0:
        raise StorageError(f"Reflection setting {key} must be positive")
    return value


def normalize_web_search_settings(web_search: Any) -> dict[str, Any]:
    """Return the normalized web search provider settings section."""

    section = _coerce_web_search_section(web_search)
    provider = section.get("provider", DEFAULT_WEB_SEARCH_SETTINGS["provider"])
    if not isinstance(provider, str) or provider not in FIRST_PARTY_WEB_SEARCH_PROVIDERS:
        allowed = ", ".join(sorted(FIRST_PARTY_WEB_SEARCH_PROVIDERS))
        raise StorageError(f"Web search provider must be one of: {allowed}")

    searxng = section.get("searxng", {})
    if searxng is None:
        searxng = {}
    if not isinstance(searxng, Mapping):
        raise StorageError("Expected settings.web_search.searxng to be an object")

    unsupported_searxng_fields = sorted(set(searxng) - {"base_url"})
    if unsupported_searxng_fields:
        raise StorageError("Unsupported SearXNG settings: " + ", ".join(unsupported_searxng_fields))

    base_url = searxng.get("base_url", DEFAULT_SEARXNG_BASE_URL)
    if not isinstance(base_url, str) or not base_url.strip():
        raise StorageError("SearXNG base_url must be a non-empty string")

    default_count = section.get("default_count", DEFAULT_WEB_SEARCH_COUNT)
    if (
        isinstance(default_count, bool)
        or not isinstance(default_count, int)
        or not (MIN_WEB_SEARCH_COUNT <= default_count <= MAX_WEB_SEARCH_COUNT)
    ):
        raise StorageError(
            "Web search default_count must be an integer between "
            f"{MIN_WEB_SEARCH_COUNT} and {MAX_WEB_SEARCH_COUNT}"
        )

    return {
        "provider": provider,
        "default_count": default_count,
        "searxng": {"base_url": base_url.strip()},
    }


def _coerce_web_search_section(web_search: Any) -> dict[str, Any]:
    if web_search is None:
        return {}
    if not isinstance(web_search, Mapping):
        raise StorageError("Expected settings.web_search to be an object")
    unsupported_fields = sorted(set(web_search) - {"provider", "default_count", "searxng"})
    if unsupported_fields:
        raise StorageError(f"Unsupported web_search settings: {', '.join(unsupported_fields)}")
    return dict(web_search)


def normalize_model_task_settings(model_tasks: Any) -> dict[str, dict[str, Any]]:
    """Return the normalized task-model bindings section."""

    section = _coerce_model_tasks_section(model_tasks)
    normalized: dict[str, dict[str, Any]] = {}

    for task_type, raw_binding in section.items():
        if task_type not in SUPPORTED_TASK_TYPES:
            raise StorageError(f"Unsupported model task type: {task_type}")
        if not isinstance(raw_binding, Mapping):
            raise StorageError(f"Expected settings.model_tasks.{task_type} to be an object")

        unsupported_fields = sorted(set(raw_binding) - {"target", "options"})
        if unsupported_fields:
            raise StorageError(
                f"Unsupported model task settings for {task_type}: {', '.join(unsupported_fields)}"
            )

        target = raw_binding.get("target")
        if not isinstance(target, str) or not target.strip():
            raise StorageError(f"Model task target for {task_type} must be a non-empty string")

        normalized[task_type] = {
            "target": target.strip(),
            "options": normalize_json_object(
                raw_binding.get("options", {}),
                f"settings.model_tasks.{task_type}.options",
            ),
        }
    return normalized


def _coerce_model_tasks_section(model_tasks: Any) -> dict[str, Any]:
    if model_tasks is None:
        return {}
    if not isinstance(model_tasks, Mapping):
        raise StorageError("Expected settings.model_tasks to be an object")
    return dict(model_tasks)


def normalize_extensions_settings(extensions: Any) -> dict[str, Any]:
    """Return the normalized ``extensions`` section (disabled list + config map).

    Restart-applied: the runtime reads this at ``Runtime.start()``. ``disabled``
    is a deduplicated list of non-empty names (order preserved); ``config`` maps
    each extension name to a deep-validated JSON object passed to its
    ``register()``.
    """
    section = _coerce_extensions_section(extensions)

    disabled_value = section.get("disabled", [])
    if disabled_value is None:
        disabled_value = []
    if not isinstance(disabled_value, list):
        raise StorageError("Expected settings.extensions.disabled to be a list")
    disabled: list[str] = []
    for name in disabled_value:
        if not isinstance(name, str) or not name.strip():
            raise StorageError("settings.extensions.disabled entries must be non-empty strings")
        normalized_name = name.strip()
        if normalized_name not in disabled:
            disabled.append(normalized_name)

    config_value = section.get("config", {})
    if config_value is None:
        config_value = {}
    if not isinstance(config_value, Mapping):
        raise StorageError("Expected settings.extensions.config to be an object")
    config: dict[str, Any] = {}
    for name, value in config_value.items():
        if not isinstance(name, str) or not name.strip():
            raise StorageError("settings.extensions.config keys must be non-empty strings")
        config[name.strip()] = normalize_json_object(value, f"settings.extensions.config.{name}")

    return {"disabled": disabled, "config": config}


def _coerce_extensions_section(extensions: Any) -> dict[str, Any]:
    if extensions is None:
        return {}
    if not isinstance(extensions, Mapping):
        raise StorageError("Expected settings.extensions to be an object")
    unsupported_fields = sorted(set(extensions) - {"disabled", "config"})
    if unsupported_fields:
        raise StorageError(f"Unsupported extensions settings: {', '.join(unsupported_fields)}")
    return dict(extensions)


def is_absolute_or_home_relative_path(path: str) -> bool:
    """Return whether *path* is absolute (POSIX or Windows form) or home-relative."""

    if path == "~" or path.startswith(("~/", "~\\")):
        return True
    # Accept both POSIX and Windows absolute forms on any host so the same
    # settings.json validates identically across platforms.
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


__all__ = [
    "COMPACTION_SETTING_DEFAULTS",
    "CUSTOM_MODEL_CAPABILITY_FIELDS",
    "CUSTOM_PROVIDER_ADAPTERS",
    "CUSTOM_PROVIDER_AUTH_TYPES",
    "CUSTOM_PROVIDER_CONNECTION_ID",
    "CUSTOM_PROVIDER_ID_PATTERN",
    "CUSTOM_PROVIDER_MODEL_FIELDS",
    "DEBUG_SETTING_DEFAULTS",
    "DEFAULT_APPEARANCE_LANGUAGE",
    "DEFAULT_RECALL_SETTINGS",
    "DEFAULT_SESSION_TITLE_SETTINGS",
    "DEFAULT_WEB_SEARCH_SETTINGS",
    "REFLECTION_SETTING_DEFAULTS",
    "SUPPORTED_APPEARANCE_LANGUAGES",
    "coerce_defaults_section",
    "coerce_defaults_update",
    "coerce_skills_update",
    "is_absolute_or_home_relative_path",
    "normalize_agent_defaults",
    "normalize_appearance_settings",
    "normalize_compaction_policy",
    "normalize_compaction_settings",
    "normalize_custom_provider_id",
    "normalize_custom_provider_settings",
    "normalize_custom_providers_settings",
    "normalize_debug_settings",
    "normalize_defaults_settings",
    "normalize_extensions_settings",
    "normalize_json_object",
    "normalize_local_models_settings",
    "normalize_model_task_settings",
    "normalize_providers_settings",
    "normalize_recall_settings",
    "normalize_reflection_settings",
    "normalize_session_title_settings",
    "normalize_skill_directories",
    "normalize_speech_settings",
    "normalize_subagent_integer",
    "normalize_transcription_audio_settings",
    "normalize_web_search_settings",
    "validate_supported_agent_default_fields",
]
