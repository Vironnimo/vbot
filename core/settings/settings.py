"""Public Settings schema parsing and validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.fetch_config import parse_web_fetch_settings
from core.search_config import (
    FIRST_PARTY_WEB_SEARCH_PROVIDERS,
    MAX_WEB_SEARCH_COUNT,
    MIN_WEB_SEARCH_COUNT,
)
from core.settings._json_settings import normalize_json_object
from core.settings._rules import (
    AGENT_ID_PATTERN,
    ALLOWED_THINKING_EFFORTS,
    APPEARANCE_FIELDS,
    ARCHIVE_FIELDS,
    ARCHIVE_RETENTION_DAYS_RULE,
    COMPACTION_FIELDS,
    DEBUG_FIELDS,
    DEFAULT_APPEARANCE_CHAT_WIDTH,
    DEFAULT_APPEARANCE_CHAT_WORKING_MODE,
    DEFAULTS_SECTIONS,
    EXTENSIONS_FIELDS,
    LIBRARIAN_BOOLEAN_FIELDS,
    LIBRARIAN_DAY_FIELDS,
    LIBRARIAN_DAYS_RULE,
    LIBRARIAN_FIELDS,
    MAX_ARCHIVE_RETENTION_DAYS,
    MAX_FALLBACK_MODELS,
    MAX_LIBRARIAN_DAYS,
    MAX_TRACE_LIMIT,
    MIN_ARCHIVE_RETENTION_DAYS,
    NOTIFICATION_FIELDS,
    OPENROUTER_MODEL_ID_MAX_LENGTH,
    OPENROUTER_PROVIDER_SLUG_MAX_LENGTH,
    OPENROUTER_PROVIDER_SLUG_PATTERN,
    OPENROUTER_ROUTING_FIELDS,
    OPENROUTER_ROUTING_MODES,
    OPENROUTER_ROUTING_POLICY_FIELDS,
    PROJECT_ID_PATTERN,
    PROJECT_TOOL_ALLOWLIST_WILDCARD,
    RECALL_BACKEND_PATTERN,
    RECALL_FIELDS,
    REFLECTION_FIELDS,
    REFLECTION_INTERVAL_FIELDS,
    SESSION_TITLE_FIELDS,
    SETTINGS_UPDATE_SECTIONS,
    SUBAGENT_SETTING_FIELDS,
    SUPPORTED_APPEARANCE_CHAT_WIDTHS,
    SUPPORTED_APPEARANCE_CHAT_WORKING_MODES,
    WEB_SEARCH_FIELDS,
    WEB_SEARCH_SEARXNG_FIELDS,
    JsonObject,
    SettingsValidationError,
    _positive_integer,
    available_timezone_names,
    default_timezone_name,
    effective_timezone_name,
    is_archive_retention_days,
    is_valid_agent_id,
    is_valid_project_id,
    validate_temperature,
    validate_thinking_effort,
    validate_timezone_name,
    validate_top_p,
)
from core.settings._update_models import (
    _parse_local_models_update,
    _parse_model_tasks_update,
    _parse_providers_update,
    _parse_speech_update,
    parse_openrouter_routing,
)
from core.settings.agent_defaults import (
    AGENT_DEFAULT_FIELDS,
    MAX_TEMPERATURE,
    MAX_TOP_P,
    MIN_TEMPERATURE,
    MIN_TOP_P,
    parse_agent_default_value,
)
from core.utils.errors import StorageError


def parse_settings_update(params: Mapping[str, Any]) -> JsonObject:
    """Parse and validate a public ``settings.update`` payload."""
    unsupported_sections = sorted(set(params) - SETTINGS_UPDATE_SECTIONS)
    if unsupported_sections:
        raise SettingsValidationError(
            f"unsupported settings sections: {', '.join(unsupported_sections)}"
        )

    if not params:
        raise SettingsValidationError("settings.update requires a section")

    parsed_update: JsonObject = {}

    if "appearance" in params:
        parsed_update["appearance"] = _parse_appearance_update(params["appearance"])

    if "archive" in params:
        parsed_update["archive"] = _parse_archive_update(params["archive"])

    if "skills" in params:
        parsed_update["skills"] = _parse_skills_update(params["skills"])

    if "subagents" in params:
        parsed_update["subagents"] = _parse_subagents_update(params["subagents"])

    if "compaction" in params:
        parsed_update["compaction"] = _parse_compaction_update(params["compaction"])

    if "debug" in params:
        parsed_update["debug"] = _parse_debug_update(params["debug"])

    if "server" in params:
        parsed_update["server"] = _parse_server_update(params["server"])

    if "defaults" in params:
        parsed_update["defaults"] = _parse_defaults_update(params["defaults"])

    if "recall" in params:
        parsed_update["recall"] = _parse_recall_update(params["recall"])

    if "model_tasks" in params:
        parsed_update["model_tasks"] = _parse_model_tasks_update(params["model_tasks"])

    if "providers" in params:
        parsed_update["providers"] = _parse_providers_update(params["providers"])

    if "web_fetch" in params:
        try:
            parsed_update["web_fetch"] = parse_web_fetch_settings(params["web_fetch"], partial=True)
        except ValueError as error:
            raise SettingsValidationError(str(error)) from error

    if "web_search" in params:
        parsed_update["web_search"] = _parse_web_search_update(params["web_search"])

    if "extensions" in params:
        parsed_update["extensions"] = _parse_extensions_update(params["extensions"])

    if "reflection" in params:
        parsed_update["reflection"] = _parse_reflection_update(params["reflection"])

    if "librarian" in params:
        parsed_update["librarian"] = _parse_librarian_update(params["librarian"])

    if "local_models" in params:
        parsed_update["local_models"] = _parse_local_models_update(params["local_models"])

    if "session_titles" in params:
        parsed_update["session_titles"] = _parse_session_titles_update(params["session_titles"])

    if "speech" in params:
        parsed_update["speech"] = _parse_speech_update(params["speech"])

    if "notifications" in params:
        parsed_update["notifications"] = _parse_notifications_update(params["notifications"])

    return parsed_update


def parse_settings_update_base(raw_base: Any, settings_update: Mapping[str, Any]) -> JsonObject:
    """Parse ``params.base``: the caller's view of the sections it updates.

    ``base`` has the update's shape and may name only sections the parsed
    ``settings_update`` changes; the storage transaction compares it with the
    current values before it writes.
    """
    if not isinstance(raw_base, dict):
        raise SettingsValidationError("params.base must be an object of settings sections")
    base = parse_settings_update(raw_base)
    extra_sections = sorted(set(base) - set(settings_update))
    if extra_sections:
        raise SettingsValidationError(
            f"params.base names sections the update does not change: {', '.join(extra_sections)}"
        )
    return base


def _parse_notifications_update(notifications: Any) -> JsonObject:
    """Parse the desktop-notification switches (sparse update, like reflection)."""
    if not isinstance(notifications, dict):
        raise SettingsValidationError("params.notifications must be an object")

    unsupported_fields = sorted(set(notifications) - set(NOTIFICATION_FIELDS))
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported notifications settings: {', '.join(unsupported_fields)}"
        )

    parsed: JsonObject = {}
    for field in NOTIFICATION_FIELDS:
        if field not in notifications:
            continue
        enabled = notifications[field]
        if not isinstance(enabled, bool):
            raise SettingsValidationError(f"params.notifications.{field} must be a boolean")
        parsed[field] = enabled
    return parsed


def _parse_session_titles_update(session_titles: Any) -> JsonObject:
    """Parse the complete automatic Session-title settings section."""
    if not isinstance(session_titles, dict):
        raise SettingsValidationError("params.session_titles must be an object")

    unsupported_fields = sorted(set(session_titles) - SESSION_TITLE_FIELDS)
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported session_titles settings: {', '.join(unsupported_fields)}"
        )

    enabled = session_titles.get("enabled")
    if not isinstance(enabled, bool):
        raise SettingsValidationError("params.session_titles.enabled must be a boolean")

    model = session_titles.get("model", "")
    if not isinstance(model, str):
        raise SettingsValidationError("params.session_titles.model must be a string")
    return {"enabled": enabled, "model": model.strip()}


def _parse_reflection_update(reflection: Any) -> JsonObject:
    """Parse the background-reflection section (partial update, like debug)."""
    if not isinstance(reflection, dict):
        raise SettingsValidationError("params.reflection must be an object")

    unsupported_fields = sorted(set(reflection) - REFLECTION_FIELDS)
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported reflection settings: {', '.join(unsupported_fields)}"
        )

    parsed: JsonObject = {}
    if "enabled" in reflection:
        enabled = reflection["enabled"]
        if not isinstance(enabled, bool):
            raise SettingsValidationError("params.reflection.enabled must be a boolean")
        parsed["enabled"] = enabled
    for field in REFLECTION_INTERVAL_FIELDS:
        if field in reflection:
            parsed[field] = _positive_integer(reflection[field], f"params.reflection.{field}")
    return parsed


def _parse_librarian_update(librarian: Any) -> JsonObject:
    """Parse the Librarian section (partial update, like reflection)."""
    if not isinstance(librarian, dict):
        raise SettingsValidationError("params.librarian must be an object")

    unsupported_fields = sorted(set(librarian) - LIBRARIAN_FIELDS)
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported librarian settings: {', '.join(unsupported_fields)}"
        )

    parsed: JsonObject = {}
    for field in LIBRARIAN_BOOLEAN_FIELDS:
        if field in librarian:
            if not isinstance(librarian[field], bool):
                raise SettingsValidationError(f"params.librarian.{field} must be a boolean")
            parsed[field] = librarian[field]
    for field in LIBRARIAN_DAY_FIELDS:
        if field in librarian:
            days = librarian[field]
            if (
                isinstance(days, bool)
                or not isinstance(days, int)
                or not 1 <= days <= MAX_LIBRARIAN_DAYS
            ):
                raise SettingsValidationError(f"params.librarian.{field} {LIBRARIAN_DAYS_RULE}")
            parsed[field] = days
    return parsed


def _parse_extensions_update(extensions: Any) -> JsonObject:
    """Parse a complete ``extensions`` section (disabled list + JSON config).

    Full-section write: ``disabled`` and ``config`` default to empty when
    omitted, so callers send the complete section. Loaded Extension schemas
    are validated separately by the Runtime-aware RPC boundary.
    """
    if not isinstance(extensions, dict):
        raise SettingsValidationError("params.extensions must be an object")

    unsupported_fields = sorted(set(extensions) - EXTENSIONS_FIELDS)
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported extensions settings: {', '.join(unsupported_fields)}"
        )

    disabled = extensions.get("disabled", [])
    if not isinstance(disabled, list) or not all(
        isinstance(name, str) and name.strip() for name in disabled
    ):
        raise SettingsValidationError(
            "params.extensions.disabled must be a list of non-empty strings"
        )

    config = extensions.get("config", {})
    if not isinstance(config, dict) or not all(
        isinstance(value, dict) for value in config.values()
    ):
        raise SettingsValidationError("params.extensions.config must be an object of objects")

    try:
        parsed_config = normalize_json_object(config, "params.extensions.config")
    except StorageError as error:
        raise SettingsValidationError(str(error)) from error
    return {
        "disabled": [name.strip() for name in disabled],
        "config": parsed_config,
    }


def _parse_web_search_update(web_search: Any) -> JsonObject:
    if not isinstance(web_search, dict):
        raise SettingsValidationError("params.web_search must be an object")

    unsupported_fields = sorted(set(web_search) - WEB_SEARCH_FIELDS)
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported web_search settings: {', '.join(unsupported_fields)}"
        )

    provider = web_search.get("provider")
    if not isinstance(provider, str) or provider not in FIRST_PARTY_WEB_SEARCH_PROVIDERS:
        allowed = ", ".join(sorted(FIRST_PARTY_WEB_SEARCH_PROVIDERS))
        raise SettingsValidationError(f"params.web_search.provider must be one of: {allowed}")

    parsed: JsonObject = {"provider": provider}
    if "default_count" in web_search:
        default_count = web_search["default_count"]
        if (
            isinstance(default_count, bool)
            or not isinstance(default_count, int)
            or not (MIN_WEB_SEARCH_COUNT <= default_count <= MAX_WEB_SEARCH_COUNT)
        ):
            raise SettingsValidationError(
                "params.web_search.default_count must be an integer between "
                f"{MIN_WEB_SEARCH_COUNT} and {MAX_WEB_SEARCH_COUNT}"
            )
        parsed["default_count"] = default_count
    if "searxng" in web_search:
        parsed["searxng"] = _parse_searxng_settings(web_search["searxng"])
    return parsed


def _parse_searxng_settings(searxng: Any) -> JsonObject:
    if not isinstance(searxng, dict):
        raise SettingsValidationError("params.web_search.searxng must be an object")

    unsupported_fields = sorted(set(searxng) - WEB_SEARCH_SEARXNG_FIELDS)
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported SearXNG settings: {', '.join(unsupported_fields)}"
        )

    base_url = searxng.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        raise SettingsValidationError("params.web_search.searxng.base_url must be a string")
    return {"base_url": base_url.strip()}


def _parse_recall_update(recall: Any) -> JsonObject:
    if not isinstance(recall, dict):
        raise SettingsValidationError("params.recall must be an object")

    unsupported_fields = sorted(set(recall) - RECALL_FIELDS)
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported recall settings: {', '.join(unsupported_fields)}"
        )

    backend = recall.get("backend")
    if not isinstance(backend, str) or not backend.strip():
        raise SettingsValidationError("params.recall.backend must be a non-empty string")
    backend = backend.strip()
    if RECALL_BACKEND_PATTERN.fullmatch(backend) is None:
        raise SettingsValidationError("params.recall.backend must use lowercase snake_case")

    return {"backend": backend}


def _parse_defaults_update(defaults: Any) -> JsonObject:
    if not isinstance(defaults, dict):
        raise SettingsValidationError("params.defaults must be an object")

    unsupported_sections = sorted(set(defaults) - DEFAULTS_SECTIONS)
    if unsupported_sections:
        raise SettingsValidationError(
            f"unsupported defaults settings: {', '.join(unsupported_sections)}"
        )

    if "agent" not in defaults:
        raise SettingsValidationError("params.defaults must include an agent object")

    raw_agent_defaults = defaults["agent"]
    if not isinstance(raw_agent_defaults, dict):
        raise SettingsValidationError("params.defaults.agent must be an object")

    unsupported_agent_fields = sorted(set(raw_agent_defaults) - AGENT_DEFAULT_FIELDS)
    if unsupported_agent_fields:
        raise SettingsValidationError(
            f"unsupported defaults.agent settings: {', '.join(unsupported_agent_fields)}"
        )

    agent_defaults: JsonObject = {}
    for field, value in raw_agent_defaults.items():
        if value is None:
            agent_defaults[field] = None
            continue
        agent_defaults[field] = parse_agent_default_value(
            field, value, label=f"params.defaults.agent.{field}"
        )

    return {"agent": agent_defaults}


def _parse_appearance_update(appearance: Any) -> JsonObject:
    if not isinstance(appearance, dict):
        raise SettingsValidationError("params.appearance must be an object")

    unsupported_fields = sorted(set(appearance) - APPEARANCE_FIELDS)
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported appearance settings: {', '.join(unsupported_fields)}"
        )

    # `language` stays required so a partial update never resets it: the
    # appearance section is normalized as a whole (full replace), so callers
    # send the complete section. Display preferences are optional and validated
    # when present; missing values normalize to their defaults.
    language = appearance.get("language")
    if not isinstance(language, str) or not language:
        raise SettingsValidationError("params.appearance.language must be a non-empty string")

    parsed: JsonObject = {"language": language}

    if "chat_width" in appearance:
        chat_width = appearance.get("chat_width")
        if not isinstance(chat_width, str) or chat_width not in SUPPORTED_APPEARANCE_CHAT_WIDTHS:
            supported = ", ".join(sorted(SUPPORTED_APPEARANCE_CHAT_WIDTHS))
            raise SettingsValidationError(
                f"params.appearance.chat_width must be one of: {supported}"
            )
        parsed["chat_width"] = chat_width

    if "chat_working_mode" in appearance:
        chat_working_mode = appearance.get("chat_working_mode")
        if (
            not isinstance(chat_working_mode, str)
            or chat_working_mode not in SUPPORTED_APPEARANCE_CHAT_WORKING_MODES
        ):
            supported = ", ".join(sorted(SUPPORTED_APPEARANCE_CHAT_WORKING_MODES))
            raise SettingsValidationError(
                f"params.appearance.chat_working_mode must be one of: {supported}"
            )
        parsed["chat_working_mode"] = chat_working_mode

    return parsed


def _parse_skills_update(skills: Any) -> JsonObject:
    if not isinstance(skills, dict):
        raise SettingsValidationError("params.skills must be an object")

    unsupported_fields = sorted(set(skills) - {"directories"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported skills settings: {', '.join(unsupported_fields)}"
        )

    directories = skills.get("directories")
    if not isinstance(directories, list) or not all(
        isinstance(directory, str) for directory in directories
    ):
        raise SettingsValidationError("params.skills.directories must be a list of strings")

    return {"directories": list(directories)}


def _parse_subagents_update(subagents: Any) -> JsonObject:
    if not isinstance(subagents, dict):
        raise SettingsValidationError("params.subagents must be an object")

    supported_fields = set(SUBAGENT_SETTING_FIELDS)
    unsupported_fields = sorted(set(subagents) - supported_fields)
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported sub-agent settings: {', '.join(unsupported_fields)}"
        )

    missing_fields = [field for field in SUBAGENT_SETTING_FIELDS if field not in subagents]
    if missing_fields:
        raise SettingsValidationError(f"missing sub-agent settings: {', '.join(missing_fields)}")

    return {
        field: _positive_integer(subagents[field], f"params.subagents.{field}")
        for field in SUBAGENT_SETTING_FIELDS
    }


def _parse_compaction_update(compaction: Any) -> JsonObject:
    if not isinstance(compaction, dict):
        raise SettingsValidationError("params.compaction must be an object")

    unsupported_fields = sorted(set(compaction) - COMPACTION_FIELDS)
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported compaction settings: {', '.join(unsupported_fields)}"
        )

    required_fields = ("enabled", "trigger", "strategy")
    missing_fields = [field for field in required_fields if field not in compaction]
    if missing_fields:
        raise SettingsValidationError(f"missing compaction settings: {', '.join(missing_fields)}")

    enabled = compaction["enabled"]
    if not isinstance(enabled, bool):
        raise SettingsValidationError("params.compaction.enabled must be a boolean")
    trigger = _parse_compaction_trigger(compaction["trigger"])
    strategy = _parse_compaction_strategy(compaction["strategy"])
    return {"enabled": enabled, "trigger": trigger, "strategy": strategy}


def _parse_compaction_trigger(value: Any) -> JsonObject:
    if not isinstance(value, dict):
        raise SettingsValidationError("params.compaction.trigger must be an object")
    trigger_type = value.get("type")
    if trigger_type == "context_ratio":
        if not {"type", "threshold"} <= set(value) or set(value) - {
            "type",
            "threshold",
            "tokens",
        }:
            raise SettingsValidationError(
                "context_ratio trigger requires type and threshold, with optional tokens"
            )
        threshold_value = value["threshold"]
        if isinstance(threshold_value, bool) or not isinstance(threshold_value, int | float):
            raise SettingsValidationError("params.compaction.trigger.threshold must be a number")
        if not 0 < threshold_value <= 1:
            raise SettingsValidationError("params.compaction.trigger.threshold must be in (0, 1]")
        threshold = float(threshold_value)
        parsed: JsonObject = {"type": trigger_type, "threshold": threshold}
        if "tokens" in value:
            parsed["tokens"] = _positive_integer(
                value["tokens"], "params.compaction.trigger.tokens"
            )
        return parsed
    if trigger_type == "input_tokens":
        if set(value) != {"type", "tokens"}:
            raise SettingsValidationError("input_tokens trigger requires exactly type and tokens")
        return {
            "type": trigger_type,
            "tokens": _positive_integer(value["tokens"], "params.compaction.trigger.tokens"),
        }
    raise SettingsValidationError(
        "params.compaction.trigger.type must be context_ratio or input_tokens"
    )


def _parse_compaction_strategy(value: Any) -> JsonObject:
    if not isinstance(value, dict):
        raise SettingsValidationError("params.compaction.strategy must be an object")
    strategy_type = value.get("type")
    if strategy_type == "summary_tail":
        if set(value) != {"type", "tail_tokens", "summary_model"}:
            raise SettingsValidationError(
                "summary_tail strategy requires exactly type, tail_tokens, and summary_model"
            )
        summary_model = value["summary_model"]
        if summary_model is not None and not isinstance(summary_model, str):
            raise SettingsValidationError(
                "params.compaction.strategy.summary_model must be a string or null"
            )
        return {
            "type": strategy_type,
            "tail_tokens": _positive_integer(
                value["tail_tokens"], "params.compaction.strategy.tail_tokens"
            ),
            "summary_model": summary_model,
        }
    if strategy_type == "continuation":
        if set(value) != {"type"}:
            raise SettingsValidationError("continuation strategy accepts only type")
        return {"type": strategy_type}
    raise SettingsValidationError(
        "params.compaction.strategy.type must be summary_tail or continuation"
    )


def _parse_debug_update(debug: Any) -> JsonObject:
    if not isinstance(debug, dict):
        raise SettingsValidationError("params.debug must be an object")

    unsupported_fields = sorted(set(debug) - DEBUG_FIELDS)
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported debug settings: {', '.join(unsupported_fields)}"
        )

    parsed: JsonObject = {}

    if "enabled" in debug:
        enabled = debug["enabled"]
        if not isinstance(enabled, bool):
            raise SettingsValidationError("params.debug.enabled must be a boolean")
        parsed["enabled"] = enabled

    if "trace_limit" in debug:
        trace_limit = _positive_integer(debug["trace_limit"], "params.debug.trace_limit")
        if trace_limit > MAX_TRACE_LIMIT:
            raise SettingsValidationError(
                f"params.debug.trace_limit must not exceed {MAX_TRACE_LIMIT}"
            )
        parsed["trace_limit"] = trace_limit

    return parsed


def _parse_archive_update(archive: Any) -> JsonObject:
    if not isinstance(archive, dict):
        raise SettingsValidationError("params.archive must be an object")

    unsupported_fields = sorted(set(archive) - ARCHIVE_FIELDS)
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported archive settings: {', '.join(unsupported_fields)}"
        )

    parsed: JsonObject = {}
    if "retention_days" in archive:
        retention_days = archive["retention_days"]
        if not is_archive_retention_days(retention_days):
            raise SettingsValidationError(
                f"params.archive.retention_days {ARCHIVE_RETENTION_DAYS_RULE}"
            )
        parsed["retention_days"] = retention_days
    return parsed


def _parse_server_update(server: Any) -> JsonObject:
    if not isinstance(server, dict):
        raise SettingsValidationError("params.server must be an object")

    unsupported_fields = sorted(set(server) - {"keep_awake", "timezone"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported server settings: {', '.join(unsupported_fields)}"
        )

    parsed: JsonObject = {}

    if "keep_awake" in server:
        keep_awake = server["keep_awake"]
        if not isinstance(keep_awake, bool):
            raise SettingsValidationError("params.server.keep_awake must be a boolean")
        parsed["keep_awake"] = keep_awake

    if "timezone" in server:
        parsed["timezone"] = validate_timezone_name(
            server["timezone"], label="params.server.timezone"
        )

    return parsed


__all__ = [
    "AGENT_ID_PATTERN",
    "ALLOWED_THINKING_EFFORTS",
    "ARCHIVE_RETENTION_DAYS_RULE",
    "DEFAULT_APPEARANCE_CHAT_WIDTH",
    "DEFAULT_APPEARANCE_CHAT_WORKING_MODE",
    "JsonObject",
    "LIBRARIAN_BOOLEAN_FIELDS",
    "LIBRARIAN_DAYS_RULE",
    "LIBRARIAN_DAY_FIELDS",
    "MAX_ARCHIVE_RETENTION_DAYS",
    "MAX_FALLBACK_MODELS",
    "MAX_LIBRARIAN_DAYS",
    "MAX_TEMPERATURE",
    "MAX_TOP_P",
    "MIN_ARCHIVE_RETENTION_DAYS",
    "MIN_TEMPERATURE",
    "MIN_TOP_P",
    "NOTIFICATION_FIELDS",
    "OPENROUTER_MODEL_ID_MAX_LENGTH",
    "OPENROUTER_PROVIDER_SLUG_MAX_LENGTH",
    "OPENROUTER_PROVIDER_SLUG_PATTERN",
    "OPENROUTER_ROUTING_FIELDS",
    "OPENROUTER_ROUTING_MODES",
    "OPENROUTER_ROUTING_POLICY_FIELDS",
    "PROJECT_ID_PATTERN",
    "PROJECT_TOOL_ALLOWLIST_WILDCARD",
    "RECALL_BACKEND_PATTERN",
    "REFLECTION_INTERVAL_FIELDS",
    "SETTINGS_UPDATE_SECTIONS",
    "SUBAGENT_SETTING_FIELDS",
    "SUPPORTED_APPEARANCE_CHAT_WIDTHS",
    "SUPPORTED_APPEARANCE_CHAT_WORKING_MODES",
    "SettingsValidationError",
    "available_timezone_names",
    "default_timezone_name",
    "effective_timezone_name",
    "is_archive_retention_days",
    "is_valid_agent_id",
    "is_valid_project_id",
    "parse_openrouter_routing",
    "validate_temperature",
    "validate_thinking_effort",
    "validate_timezone_name",
    "validate_top_p",
]
