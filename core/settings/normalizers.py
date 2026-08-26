"""Pure validation and normalization for persisted ``settings.json`` sections.

Every function here is stateless: it takes raw settings data and returns the
normalized value, raising :class:`StorageError` on invalid input. The settings
domain owns the per-section schema knowledge; ``StorageManager`` owns the
read-modify-write transactions and delegates section normalization here.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, cast
from urllib.parse import urlsplit

from core.model_tasks import SUPPORTED_TASK_TYPES
from core.model_tasks.constants import (
    DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS,
    SUPPORTED_TRANSCRIPTION_AUDIO_FORMATS,
    SUPPORTED_TRANSCRIPTION_AUDIO_PROFILES,
    SUPPORTED_TRANSCRIPTION_AUDIO_SAMPLE_RATES,
    TRANSCRIPTION_AUDIO_PRESETS,
)
from core.models.models import MODEL_TASK_ORDER
from core.search_config import (
    DEFAULT_SEARXNG_BASE_URL,
    DEFAULT_WEB_SEARCH_COUNT,
    DEFAULT_WEB_SEARCH_PROVIDER,
    FIRST_PARTY_WEB_SEARCH_PROVIDERS,
    MAX_WEB_SEARCH_COUNT,
    MIN_WEB_SEARCH_COUNT,
)
from core.settings.settings import (
    AGENT_DEFAULT_FIELDS,
    ALLOWED_THINKING_EFFORTS,
    DEFAULT_APPEARANCE_CHAT_WIDTH,
    DEFAULT_APPEARANCE_CHAT_WORKING_MODE,
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    SUPPORTED_APPEARANCE_CHAT_WIDTHS,
    SUPPORTED_APPEARANCE_CHAT_WORKING_MODES,
    SettingsValidationError,
    parse_openrouter_routing,
)
from core.utils.errors import StorageError

DEFAULT_APPEARANCE_LANGUAGE = "en"
SUPPORTED_APPEARANCE_LANGUAGES = frozenset({DEFAULT_APPEARANCE_LANGUAGE})
DEFAULT_RECALL_SETTINGS = {"backend": "jsonl_scan"}
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
COMPACTION_SETTING_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "trigger": {"type": "context_ratio", "threshold": 0.8},
    "strategy": {"type": "summary_tail", "tail_tokens": 15_000, "summary_model": None},
}
REFLECTION_SETTING_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "memory_turn_interval": 10,
    "skill_model_step_interval": 10,
}
CUSTOM_PROVIDER_ADAPTERS = frozenset({"openai_compatible"})
CUSTOM_PROVIDER_AUTH_TYPES = frozenset({"api_key", "none"})
CUSTOM_PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CUSTOM_PROVIDER_CONNECTION_ID = "default"
CUSTOM_PROVIDER_MODEL_FIELDS = frozenset(
    {
        "name",
        "context_window",
        "max_output_tokens",
        "capabilities",
    }
)
CUSTOM_MODEL_CAPABILITY_FIELDS = frozenset(
    {
        "vision",
        "tools",
        "json_mode",
        "reasoning",
        "input_modalities",
        "output_modalities",
        "supported_parameters",
        "supported_voices",
        "task_types",
        "task_options",
    }
)


# --- speech -------------------------------------------------------------------


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


# --- appearance ---------------------------------------------------------------


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


# --- skills -------------------------------------------------------------------


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


# --- sub-agents ---------------------------------------------------------------


def normalize_subagent_integer(key: str, value: Any, default: int) -> int:
    """Return a positive integer sub-agent setting, falling back to ``default``."""

    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError(f"Sub-agent setting {key} must be an integer")
    if value <= 0:
        raise StorageError(f"Sub-agent setting {key} must be positive")
    return cast("int", value)


# --- compaction ---------------------------------------------------------------


def normalize_compaction_settings(compaction: Any) -> dict[str, Any]:
    """Return the normalized global compaction Policy."""
    return normalize_compaction_policy(compaction, use_defaults=True)


def normalize_compaction_policy(policy: Any, *, use_defaults: bool = False) -> dict[str, Any]:
    """Validate and normalize one complete Compaction Policy."""
    if policy is None:
        if use_defaults:
            return {
                "enabled": True,
                "trigger": {"type": "context_ratio", "threshold": 0.8},
                "strategy": {
                    "type": "summary_tail",
                    "tail_tokens": 15_000,
                    "summary_model": None,
                },
            }
        raise StorageError("Compaction Policy must be an object")
    section = _coerce_compaction_section(policy)
    unsupported = sorted(set(section) - {"enabled", "trigger", "strategy"})
    if unsupported:
        raise StorageError(f"Unsupported Compaction Policy fields: {', '.join(unsupported)}")
    if not use_defaults:
        missing = sorted({"enabled", "trigger", "strategy"} - set(section))
        if missing:
            raise StorageError(f"Missing Compaction Policy fields: {', '.join(missing)}")
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise StorageError("Compaction Policy enabled must be a boolean")
    trigger = _normalize_compaction_trigger(section.get("trigger"))
    strategy = _normalize_compaction_strategy(section.get("strategy"))
    return {"enabled": enabled, "trigger": trigger, "strategy": strategy}


def _coerce_compaction_section(compaction: Any) -> dict[str, Any]:
    if compaction is None:
        return {}
    if not isinstance(compaction, Mapping):
        raise StorageError("Expected settings.compaction to be an object")
    return dict(compaction)


def _normalize_compaction_trigger(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "context_ratio", "threshold": 0.8}
    if not isinstance(value, Mapping):
        raise StorageError("Compaction Policy trigger must be an object")
    trigger = dict(value)
    trigger_type = trigger.get("type", "context_ratio")
    if trigger_type == "context_ratio":
        unsupported = sorted(set(trigger) - {"type", "threshold"})
        if unsupported:
            raise StorageError(
                f"Unsupported context-ratio Trigger fields: {', '.join(unsupported)}"
            )
        return {
            "type": trigger_type,
            "threshold": _normalize_compaction_threshold(trigger.get("threshold")),
        }
    if trigger_type == "input_tokens":
        unsupported = sorted(set(trigger) - {"type", "tokens"})
        if unsupported:
            raise StorageError(f"Unsupported input-token Trigger fields: {', '.join(unsupported)}")
        return {
            "type": trigger_type,
            "tokens": _normalize_compaction_positive_integer(
                trigger.get("tokens"), "tokens", 100_000
            ),
        }
    raise StorageError("Compaction Policy trigger.type must be context_ratio or input_tokens")


def _normalize_compaction_threshold(value: Any) -> float:
    if value is None:
        return cast("float", COMPACTION_SETTING_DEFAULTS["trigger"]["threshold"])
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise StorageError("Compaction setting threshold must be a number")

    normalized_value = float(value)
    if normalized_value <= 0 or normalized_value > 1:
        raise StorageError("Compaction setting threshold must be in (0, 1]")
    return normalized_value


def _normalize_compaction_positive_integer(value: Any, field: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError(f"Compaction Policy {field} must be an integer")
    if value <= 0:
        raise StorageError(f"Compaction Policy {field} must be positive")
    return value


def _normalize_compaction_strategy(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "summary_tail", "tail_tokens": 15_000, "summary_model": None}
    if not isinstance(value, Mapping):
        raise StorageError("Compaction Policy strategy must be an object")
    strategy = dict(value)
    strategy_type = strategy.get("type", "summary_tail")
    if strategy_type == "summary_tail":
        unsupported = sorted(set(strategy) - {"type", "tail_tokens", "summary_model"})
        if unsupported:
            raise StorageError(
                f"Unsupported summary-tail Strategy fields: {', '.join(unsupported)}"
            )
        summary_model = strategy.get("summary_model")
        if summary_model is not None and not isinstance(summary_model, str):
            raise StorageError("Compaction Policy summary_model must be a string or null")
        return {
            "type": strategy_type,
            "tail_tokens": _normalize_compaction_positive_integer(
                strategy.get("tail_tokens"), "tail_tokens", 15_000
            ),
            "summary_model": summary_model,
        }
    if strategy_type == "continuation":
        unsupported = sorted(set(strategy) - {"type"})
        if unsupported:
            raise StorageError(
                f"Unsupported continuation Strategy fields: {', '.join(unsupported)}"
            )
        return {"type": strategy_type}
    raise StorageError("Compaction Policy strategy.type must be summary_tail or continuation")


# --- defaults / agent ---------------------------------------------------------


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


# --- automatic Session titles ------------------------------------------------


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


def normalize_agent_default_value(field: str, value: Any) -> str | list[str] | float | None:
    """Validate and normalize a single ``defaults.agent`` field value."""

    if value is None:
        return None

    if field in {"model", "fallback_models"}:
        if field == "fallback_models":
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise StorageError("Agent default fallback_models must be a string list")
            return list(value)
        if not isinstance(value, str):
            raise StorageError(f"Agent default {field} must be a string")
        return value

    if field == "temperature":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise StorageError("Agent default temperature must be a number or null")
        temperature = float(value)
        if not math.isfinite(temperature):
            raise StorageError("Agent default temperature must be finite")
        if temperature < MIN_TEMPERATURE or temperature > MAX_TEMPERATURE:
            raise StorageError(
                "Agent default temperature must be between "
                f"{MIN_TEMPERATURE:g} and {MAX_TEMPERATURE:g}"
            )
        return temperature

    if field == "thinking_effort":
        if not isinstance(value, str):
            raise StorageError("Agent default thinking_effort must be a string or null")
        if value not in ALLOWED_THINKING_EFFORTS:
            allowed = ", ".join(repr(item) for item in sorted(ALLOWED_THINKING_EFFORTS))
            raise StorageError(f"Agent default thinking_effort must be one of: {allowed}")
        return value

    raise StorageError(f"Unsupported defaults.agent setting: {field}")


# --- recall -------------------------------------------------------------------


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


# --- debug --------------------------------------------------------------------


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


# --- local models ---------------------------------------------------------------


def normalize_local_models_settings(local_models: Any) -> dict[str, Any]:
    """Return the normalized local-models settings section.

    Shape: ``{"context_windows": {"<provider>/<model_id>": positive int}}`` —
    the user-configured effective context window per flagged-local model
    (see ``resolve_effective_context_window`` in ``core/providers``).
    """

    section = _coerce_local_models_section(local_models)
    raw_windows = section.get("context_windows")
    if raw_windows is None:
        return {"context_windows": {}}
    if not isinstance(raw_windows, Mapping):
        raise StorageError("Expected settings.local_models.context_windows to be an object")

    context_windows: dict[str, int] = {}
    for key, value in raw_windows.items():
        if not isinstance(key, str) or "/" not in key or not key.strip():
            raise StorageError(
                "local_models.context_windows keys must be '<provider>/<model_id>' strings"
            )
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise StorageError(f"local_models.context_windows['{key}'] must be a positive integer")
        context_windows[key] = value
    return {"context_windows": context_windows}


def _coerce_local_models_section(local_models: Any) -> dict[str, Any]:
    if local_models is None:
        return {}
    if not isinstance(local_models, Mapping):
        raise StorageError("Expected settings.local_models to be an object")
    return dict(local_models)


# --- providers -------------------------------------------------------------------


def normalize_providers_settings(providers: Any) -> dict[str, Any]:
    """Return the normalized providers settings section.

    ``connections`` carries per-Connection enabled overrides. ``openrouter``
    carries vBot-owned routing policy rendered by ``OpenRouterAdapter`` onto
    each request; it is independent from Connection and Account identity.
    ``custom`` carries user-owned Provider definitions and manual Model facts;
    credential values never belong in this section.
    """

    if providers is None:
        section: dict[str, Any] = {}
    elif isinstance(providers, Mapping):
        section = dict(providers)
    else:
        raise StorageError("Expected settings.providers to be an object")

    unsupported_fields = sorted(set(section) - {"connections", "custom", "openrouter"})
    if unsupported_fields:
        raise StorageError(f"Unsupported providers settings: {', '.join(unsupported_fields)}")

    raw_connections = section.get("connections", {})
    if not isinstance(raw_connections, Mapping):
        raise StorageError("Expected settings.providers.connections to be an object")

    connections: dict[str, bool] = {}
    for key, enabled in raw_connections.items():
        if not isinstance(key, str) or ":" not in key or not key.strip():
            raise StorageError(
                "providers.connections keys must be '<provider>:<connection>' strings"
            )
        if not isinstance(enabled, bool):
            raise StorageError(f"providers.connections['{key}'] must be a boolean")
        connections[key] = enabled
    raw_openrouter = section.get("openrouter", {})
    if not isinstance(raw_openrouter, Mapping):
        raise StorageError("Expected settings.providers.openrouter to be an object")
    unsupported_openrouter_fields = sorted(set(raw_openrouter) - {"routing"})
    if unsupported_openrouter_fields:
        raise StorageError(
            f"Unsupported providers.openrouter settings: {', '.join(unsupported_openrouter_fields)}"
        )
    try:
        routing = parse_openrouter_routing(raw_openrouter.get("routing", {}))
    except SettingsValidationError as exc:
        raise StorageError(str(exc).replace("params.", "settings.", 1)) from exc

    return {
        "connections": connections,
        "custom": normalize_custom_providers_settings(section.get("custom")),
        "openrouter": {"routing": routing},
    }


def normalize_custom_providers_settings(custom: Any) -> dict[str, dict[str, Any]]:
    """Normalize the secret-free ``providers.custom`` map."""

    if custom is None:
        return {}
    if not isinstance(custom, Mapping):
        raise StorageError("Expected settings.providers.custom to be an object")

    normalized: dict[str, dict[str, Any]] = {}
    for raw_provider_id, raw_provider in custom.items():
        provider_id = normalize_custom_provider_id(raw_provider_id)
        normalized[provider_id] = normalize_custom_provider_settings(
            provider_id,
            raw_provider,
        )
    return normalized


def normalize_custom_provider_id(value: Any) -> str:
    """Return a canonical Custom Provider id or raise."""

    if not isinstance(value, str) or CUSTOM_PROVIDER_ID_PATTERN.fullmatch(value) is None:
        raise StorageError(
            "Custom Provider ids must use lowercase letters and digits in hyphen-separated segments"
        )
    return value


def normalize_custom_provider_settings(
    provider_id: str,
    provider: Any,
) -> dict[str, Any]:
    """Normalize one secret-free Custom Provider record."""

    normalize_custom_provider_id(provider_id)
    path = f"settings.providers.custom.{provider_id}"
    if not isinstance(provider, Mapping):
        raise StorageError(f"Expected {path} to be an object")
    unsupported = sorted(
        set(provider)
        - {
            "name",
            "adapter",
            "base_url",
            "auth",
            "models_endpoint",
            "defaults",
            "models",
        }
    )
    if unsupported:
        raise StorageError(f"Unsupported {path} fields: {', '.join(unsupported)}")

    name = _non_empty_string(provider.get("name"), f"{path}.name")
    adapter = _non_empty_string(provider.get("adapter"), f"{path}.adapter")
    if adapter not in CUSTOM_PROVIDER_ADAPTERS:
        supported = ", ".join(sorted(CUSTOM_PROVIDER_ADAPTERS))
        raise StorageError(f"{path}.adapter must be one of: {supported}")

    base_url = _normalize_custom_provider_base_url(provider.get("base_url"), path=path)
    auth = _non_empty_string(provider.get("auth", "api_key"), f"{path}.auth")
    if auth not in CUSTOM_PROVIDER_AUTH_TYPES:
        supported = ", ".join(sorted(CUSTOM_PROVIDER_AUTH_TYPES))
        raise StorageError(f"{path}.auth must be one of: {supported}")

    raw_models_endpoint = provider.get("models_endpoint")
    models_endpoint: str | None = None
    if raw_models_endpoint is not None:
        models_endpoint = _non_empty_string(
            raw_models_endpoint,
            f"{path}.models_endpoint",
        )
        if not models_endpoint.startswith("/") or models_endpoint.startswith("//"):
            raise StorageError(f"{path}.models_endpoint must be an absolute URL path")
        endpoint_parts = urlsplit(models_endpoint)
        if endpoint_parts.scheme or endpoint_parts.netloc or endpoint_parts.fragment:
            raise StorageError(f"{path}.models_endpoint must be an absolute URL path")

    defaults = normalize_json_object(provider.get("defaults", {}), f"{path}.defaults")
    models = _normalize_custom_provider_models(provider.get("models", {}), path=path)
    return {
        "name": name,
        "adapter": adapter,
        "base_url": base_url,
        "auth": auth,
        "models_endpoint": models_endpoint,
        "defaults": defaults,
        "models": models,
    }


def _normalize_custom_provider_base_url(value: Any, *, path: str) -> str:
    base_url = _non_empty_string(value, f"{path}.base_url").rstrip("/")
    parts = urlsplit(base_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise StorageError(
            f"{path}.base_url must be an absolute HTTP(S) URL without "
            "credentials, query, or fragment"
        )
    return base_url


def _normalize_custom_provider_models(
    models: Any,
    *,
    path: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(models, Mapping):
        raise StorageError(f"Expected {path}.models to be an object")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_model_id, raw_model in models.items():
        if not isinstance(raw_model_id, str) or not raw_model_id.strip() or "::" in raw_model_id:
            raise StorageError(f"{path}.models keys must be non-empty wire Model ids without '::'")
        model_id = raw_model_id.strip()
        normalized[model_id] = _normalize_custom_model(
            model_id,
            raw_model,
            path=f"{path}.models",
        )
    return normalized


def _normalize_custom_model(
    model_id: str,
    model: Any,
    *,
    path: str,
) -> dict[str, Any]:
    model_path = f"{path}['{model_id}']"
    if not isinstance(model, Mapping):
        raise StorageError(f"Expected {model_path} to be an object")
    unsupported = sorted(set(model) - CUSTOM_PROVIDER_MODEL_FIELDS)
    if unsupported:
        raise StorageError(f"Unsupported {model_path} fields: {', '.join(unsupported)}")

    context_window = _optional_positive_integer(
        model.get("context_window"),
        f"{model_path}.context_window",
    )
    max_output_tokens = _optional_positive_integer(
        model.get("max_output_tokens"),
        f"{model_path}.max_output_tokens",
    )
    return {
        "name": _non_empty_string(model.get("name", model_id), f"{model_path}.name"),
        "context_window": context_window,
        "max_output_tokens": max_output_tokens,
        "capabilities": _normalize_custom_model_capabilities(
            model.get("capabilities", {}),
            path=f"{model_path}.capabilities",
        ),
    }


def _normalize_custom_model_capabilities(
    capabilities: Any,
    *,
    path: str,
) -> dict[str, Any]:
    if not isinstance(capabilities, Mapping):
        raise StorageError(f"Expected {path} to be an object")
    unsupported = sorted(set(capabilities) - CUSTOM_MODEL_CAPABILITY_FIELDS)
    if unsupported:
        raise StorageError(f"Unsupported {path} fields: {', '.join(unsupported)}")

    input_modalities = _custom_string_list(
        capabilities.get("input_modalities", ["text"]),
        f"{path}.input_modalities",
    )
    output_modalities = _custom_string_list(
        capabilities.get("output_modalities", ["text"]),
        f"{path}.output_modalities",
    )
    task_types = _custom_string_list(
        capabilities.get("task_types", []),
        f"{path}.task_types",
    )
    unknown_tasks = sorted(set(task_types) - set(MODEL_TASK_ORDER))
    if unknown_tasks:
        raise StorageError(
            f"{path}.task_types contains unsupported values: {', '.join(unknown_tasks)}"
        )

    return {
        "vision": _custom_bool(capabilities.get("vision", False), f"{path}.vision"),
        "tools": _custom_bool(capabilities.get("tools", True), f"{path}.tools"),
        "json_mode": _custom_bool(
            capabilities.get("json_mode", False),
            f"{path}.json_mode",
        ),
        "reasoning": _custom_bool(
            capabilities.get("reasoning", False),
            f"{path}.reasoning",
        ),
        "input_modalities": input_modalities,
        "output_modalities": output_modalities,
        "supported_parameters": _custom_string_list(
            capabilities.get("supported_parameters", []),
            f"{path}.supported_parameters",
        ),
        "supported_voices": _custom_string_list(
            capabilities.get("supported_voices", []),
            f"{path}.supported_voices",
        ),
        "task_types": task_types,
        "task_options": normalize_json_object(
            capabilities.get("task_options", {}),
            f"{path}.task_options",
        ),
    }


def _custom_string_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise StorageError(f"Expected {path} to be a list")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise StorageError(f"{path} must contain only non-empty strings")
        stripped = item.strip()
        if stripped not in normalized:
            normalized.append(stripped)
    return normalized


def _custom_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise StorageError(f"{path} must be a boolean")
    return value


def _optional_positive_integer(value: Any, path: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StorageError(f"{path} must be a positive integer or null")
    return value


def _non_empty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StorageError(f"{path} must be a non-empty string")
    return value.strip()


# --- reflection ----------------------------------------------------------------


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


# --- web search ---------------------------------------------------------------


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


# --- model tasks --------------------------------------------------------------


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


def normalize_json_object(value: Any, path: str) -> dict[str, Any]:
    """Return a deep-validated JSON object, rejecting non-JSON values."""

    if not isinstance(value, Mapping):
        raise StorageError(f"Expected {path} to be an object")
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise StorageError(f"Expected {path} keys to be non-empty strings")
        normalized[key] = _normalize_json_value(item, f"{path}.{key}")
    return normalized


def _normalize_json_value(value: Any, path: str) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_normalize_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        return normalize_json_object(value, path)
    raise StorageError(f"Unsupported JSON value at {path}")


# --- extensions ---------------------------------------------------------------


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


# --- shared path helper -------------------------------------------------------


def is_absolute_or_home_relative_path(path: str) -> bool:
    """Return whether *path* is absolute (POSIX or Windows form) or home-relative."""

    if path == "~" or path.startswith(("~/", "~\\")):
        return True
    # Accept both POSIX and Windows absolute forms on any host so the same
    # settings.json validates identically across platforms.
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()
