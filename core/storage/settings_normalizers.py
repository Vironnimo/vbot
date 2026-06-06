"""Pure validation and normalization for persisted ``settings.json`` sections.

Every function here is stateless: it takes raw settings data and returns the
normalized value, raising :class:`StorageError` on invalid input. ``StorageManager``
owns the read-modify-write transactions and delegates section normalization here.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from core.model_tasks import SUPPORTED_TASK_TYPES
from core.search_config import (
    DEFAULT_SEARXNG_BASE_URL,
    DEFAULT_WEB_SEARCH_PROVIDER,
    FIRST_PARTY_WEB_SEARCH_PROVIDERS,
)
from core.storage.errors import StorageError

DEFAULT_APPEARANCE_LANGUAGE = "en"
SUPPORTED_APPEARANCE_LANGUAGES = frozenset({DEFAULT_APPEARANCE_LANGUAGE})
DEFAULT_RECALL_SETTINGS = {"backend": "jsonl_scan"}
DEFAULT_WEB_SEARCH_SETTINGS = {
    "provider": DEFAULT_WEB_SEARCH_PROVIDER,
    "searxng": {"base_url": DEFAULT_SEARXNG_BASE_URL},
}
DEBUG_SETTING_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "trace_limit": 50,
}
COMPACTION_SETTING_DEFAULTS: dict[str, Any] = {
    "auto": True,
    "threshold": 0.8,
    "tail_tokens": 15_000,
    "summary_model": None,
}
AGENT_DEFAULT_FIELDS = frozenset({"model", "fallback_model", "temperature", "thinking_effort"})
ALLOWED_THINKING_EFFORTS = frozenset(
    {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0


# --- appearance ---------------------------------------------------------------


def normalize_appearance_settings(appearance: Any) -> dict[str, str]:
    """Return the normalized Appearance settings subset."""

    return {"language": _normalize_appearance_language(appearance)}


def _normalize_appearance_language(appearance: Any) -> str:
    section = _coerce_appearance_section(appearance)
    value = section.get("language")
    if value is None:
        return DEFAULT_APPEARANCE_LANGUAGE
    return _validate_appearance_language(value)


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
        if not _is_absolute_or_home_relative_path(normalized_directory):
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
    """Return the normalized compaction settings section."""

    section = _coerce_compaction_section(compaction)
    return {
        "auto": _normalize_compaction_auto(section.get("auto")),
        "threshold": _normalize_compaction_threshold(section.get("threshold")),
        "tail_tokens": _normalize_compaction_tail_tokens(section.get("tail_tokens")),
        "summary_model": _normalize_compaction_summary_model(section.get("summary_model")),
    }


def _coerce_compaction_section(compaction: Any) -> dict[str, Any]:
    if compaction is None:
        return {}
    if not isinstance(compaction, Mapping):
        raise StorageError("Expected settings.compaction to be an object")
    return dict(compaction)


def _normalize_compaction_auto(value: Any) -> bool:
    if value is None:
        return cast("bool", COMPACTION_SETTING_DEFAULTS["auto"])
    if not isinstance(value, bool):
        raise StorageError("Compaction setting auto must be a boolean")
    return value


def _normalize_compaction_threshold(value: Any) -> float:
    if value is None:
        return cast("float", COMPACTION_SETTING_DEFAULTS["threshold"])
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise StorageError("Compaction setting threshold must be a number")

    normalized_value = float(value)
    if normalized_value <= 0 or normalized_value > 1:
        raise StorageError("Compaction setting threshold must be in (0, 1]")
    return normalized_value


def _normalize_compaction_tail_tokens(value: Any) -> int:
    if value is None:
        return cast("int", COMPACTION_SETTING_DEFAULTS["tail_tokens"])
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError("Compaction setting tail_tokens must be an integer")
    if value <= 0:
        raise StorageError("Compaction setting tail_tokens must be positive")
    return value


def _normalize_compaction_summary_model(value: Any) -> str | None:
    if value is None:
        return cast("str | None", COMPACTION_SETTING_DEFAULTS["summary_model"])
    if not isinstance(value, str):
        raise StorageError("Compaction setting summary_model must be a string or null")
    return value


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


def normalize_agent_default_value(field: str, value: Any) -> str | float | None:
    """Validate and normalize a single ``defaults.agent`` field value."""

    if value is None:
        return None

    if field in {"model", "fallback_model"}:
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

    return {
        "provider": provider,
        "searxng": {"base_url": base_url.strip()},
    }


def _coerce_web_search_section(web_search: Any) -> dict[str, Any]:
    if web_search is None:
        return {}
    if not isinstance(web_search, Mapping):
        raise StorageError("Expected settings.web_search to be an object")
    unsupported_fields = sorted(set(web_search) - {"provider", "searxng"})
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


# --- shared path helper -------------------------------------------------------


def _is_absolute_or_home_relative_path(path: str) -> bool:
    if path == "~" or path.startswith(("~/", "~\\")):
        return True
    return Path(path).is_absolute()
