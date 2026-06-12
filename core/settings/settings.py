"""Public Settings schema parsing and validation."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, cast

from core.model_tasks import SUPPORTED_TASK_TYPES
from core.search_config import FIRST_PARTY_WEB_SEARCH_PROVIDERS

JsonObject = dict[str, Any]

# Structural shape of a recall backend name (lowercase snake_case). Whether a
# name actually resolves to a registered backend is a runtime concern checked
# against the recall registry (built-ins + extension backends) at the RPC layer,
# not here — the parser only enforces the shape.
RECALL_BACKEND_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

ALLOWED_THINKING_EFFORTS = frozenset(
    {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
AGENT_DEFAULT_FIELDS = frozenset({"model", "fallback_model", "temperature", "thinking_effort"})
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0
SETTINGS_UPDATE_SECTIONS = frozenset(
    {
        "appearance",
        "debug",
        "skills",
        "subagents",
        "compaction",
        "defaults",
        "recall",
        "model_tasks",
        "web_search",
        "extensions",
    }
)
SUBAGENT_SETTING_FIELDS = (
    "max_subagent_depth",
    "max_subagents_per_turn",
    "subagent_timeout_minutes",
)


class SettingsValidationError(ValueError):
    """Raised when a public Settings payload is malformed."""


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

    if "skills" in params:
        parsed_update["skills"] = _parse_skills_update(params["skills"])

    if "subagents" in params:
        parsed_update["subagents"] = _parse_subagents_update(params["subagents"])

    if "compaction" in params:
        parsed_update["compaction"] = _parse_compaction_update(params["compaction"])

    if "debug" in params:
        parsed_update["debug"] = _parse_debug_update(params["debug"])

    if "defaults" in params:
        parsed_update["defaults"] = _parse_defaults_update(params["defaults"])

    if "recall" in params:
        parsed_update["recall"] = _parse_recall_update(params["recall"])

    if "model_tasks" in params:
        parsed_update["model_tasks"] = _parse_model_tasks_update(params["model_tasks"])

    if "web_search" in params:
        parsed_update["web_search"] = _parse_web_search_update(params["web_search"])

    if "extensions" in params:
        parsed_update["extensions"] = _parse_extensions_update(params["extensions"])

    return parsed_update


def _parse_extensions_update(extensions: Any) -> JsonObject:
    """Parse the restart-applied ``extensions`` section (disabled list + config).

    Full-section write: ``disabled`` and ``config`` default to empty when
    omitted, so callers send the complete section. Shape only — the runtime
    reads this at the next ``Runtime.start()`` (decision #9, restart-applied);
    deep JSON normalization of ``config`` happens in storage.
    """
    if not isinstance(extensions, dict):
        raise SettingsValidationError("params.extensions must be an object")

    unsupported_fields = sorted(set(extensions) - {"disabled", "config"})
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

    return {
        "disabled": [name.strip() for name in disabled],
        "config": {name: dict(value) for name, value in config.items()},
    }


def _parse_web_search_update(web_search: Any) -> JsonObject:
    if not isinstance(web_search, dict):
        raise SettingsValidationError("params.web_search must be an object")

    unsupported_fields = sorted(set(web_search) - {"provider", "searxng"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported web_search settings: {', '.join(unsupported_fields)}"
        )

    provider = web_search.get("provider")
    if not isinstance(provider, str) or provider not in FIRST_PARTY_WEB_SEARCH_PROVIDERS:
        allowed = ", ".join(sorted(FIRST_PARTY_WEB_SEARCH_PROVIDERS))
        raise SettingsValidationError(f"params.web_search.provider must be one of: {allowed}")

    parsed: JsonObject = {"provider": provider}
    if "searxng" in web_search:
        parsed["searxng"] = _parse_searxng_settings(web_search["searxng"])
    return parsed


def _parse_searxng_settings(searxng: Any) -> JsonObject:
    if not isinstance(searxng, dict):
        raise SettingsValidationError("params.web_search.searxng must be an object")

    unsupported_fields = sorted(set(searxng) - {"base_url"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported SearXNG settings: {', '.join(unsupported_fields)}"
        )

    base_url = searxng.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        raise SettingsValidationError("params.web_search.searxng.base_url must be a string")
    return {"base_url": base_url.strip()}


def _parse_model_tasks_update(model_tasks: Any) -> JsonObject:
    if not isinstance(model_tasks, dict):
        raise SettingsValidationError("params.model_tasks must be an object")

    parsed: JsonObject = {}
    for task_type, raw_binding in model_tasks.items():
        if not isinstance(task_type, str) or task_type not in SUPPORTED_TASK_TYPES:
            allowed = ", ".join(sorted(SUPPORTED_TASK_TYPES))
            raise SettingsValidationError(
                f"params.model_tasks contains unsupported task type {task_type!r}; "
                f"supported: {allowed}"
            )
        if not isinstance(raw_binding, dict):
            raise SettingsValidationError(f"params.model_tasks.{task_type} must be an object")

        unsupported_fields = sorted(set(raw_binding) - {"target", "options"})
        if unsupported_fields:
            raise SettingsValidationError(
                f"unsupported model task settings for {task_type}: {', '.join(unsupported_fields)}"
            )

        parsed_binding: JsonObject = {}
        if "target" in raw_binding:
            target = raw_binding["target"]
            if not isinstance(target, str):
                raise SettingsValidationError(
                    f"params.model_tasks.{task_type}.target must be a string"
                )
            parsed_binding["target"] = target.strip()

        if "options" in raw_binding:
            options = raw_binding["options"]
            if not isinstance(options, dict):
                raise SettingsValidationError(
                    f"params.model_tasks.{task_type}.options must be an object"
                )
            parsed_binding["options"] = dict(options)

        if not parsed_binding:
            raise SettingsValidationError(
                f"params.model_tasks.{task_type} must include target or options"
            )
        parsed[task_type] = parsed_binding

    return parsed


def _parse_recall_update(recall: Any) -> JsonObject:
    if not isinstance(recall, dict):
        raise SettingsValidationError("params.recall must be an object")

    unsupported_fields = sorted(set(recall) - {"backend"})
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

    unsupported_sections = sorted(set(defaults) - {"agent"})
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

        if field in {"model", "fallback_model"}:
            if not isinstance(value, str):
                raise SettingsValidationError(
                    f"params.defaults.agent.{field} must be a string or null"
                )
            agent_defaults[field] = value
            continue

        if field == "temperature":
            agent_defaults[field] = validate_temperature(
                value,
                label="params.defaults.agent.temperature",
                allow_none=True,
            )
            continue

        if field == "thinking_effort":
            agent_defaults[field] = validate_thinking_effort(
                value,
                label="params.defaults.agent.thinking_effort",
                allow_none=True,
            )

    return {"agent": agent_defaults}


def _parse_appearance_update(appearance: Any) -> JsonObject:
    if not isinstance(appearance, dict):
        raise SettingsValidationError("params.appearance must be an object")

    unsupported_fields = sorted(set(appearance) - {"language"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported appearance settings: {', '.join(unsupported_fields)}"
        )

    language = appearance.get("language")
    if not isinstance(language, str) or not language:
        raise SettingsValidationError("params.appearance.language must be a non-empty string")

    return {"language": language}


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

    supported_fields = {"auto", "threshold", "tail_tokens", "summary_model"}
    unsupported_fields = sorted(set(compaction) - supported_fields)
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported compaction settings: {', '.join(unsupported_fields)}"
        )

    required_fields = ("auto", "threshold", "tail_tokens", "summary_model")
    missing_fields = [field for field in required_fields if field not in compaction]
    if missing_fields:
        raise SettingsValidationError(f"missing compaction settings: {', '.join(missing_fields)}")

    auto = compaction["auto"]
    if not isinstance(auto, bool):
        raise SettingsValidationError("params.compaction.auto must be a boolean")

    threshold_value = compaction["threshold"]
    if isinstance(threshold_value, bool) or not isinstance(threshold_value, int | float):
        raise SettingsValidationError("params.compaction.threshold must be a number")
    threshold = float(threshold_value)
    if threshold <= 0 or threshold > 1:
        raise SettingsValidationError("params.compaction.threshold must be in (0, 1]")

    tail_tokens = _positive_integer(compaction["tail_tokens"], "params.compaction.tail_tokens")

    summary_model = compaction["summary_model"]
    if summary_model is not None and not isinstance(summary_model, str):
        raise SettingsValidationError("params.compaction.summary_model must be a string or null")

    return {
        "auto": auto,
        "threshold": threshold,
        "tail_tokens": tail_tokens,
        "summary_model": summary_model,
    }


def _parse_debug_update(debug: Any) -> JsonObject:
    if not isinstance(debug, dict):
        raise SettingsValidationError("params.debug must be an object")

    unsupported_fields = sorted(set(debug) - {"enabled", "trace_limit"})
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
        if trace_limit > 500:
            raise SettingsValidationError("params.debug.trace_limit must not exceed 500")
        parsed["trace_limit"] = trace_limit

    return parsed


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsValidationError(f"{label} must be a positive integer")
    if value <= 0:
        raise SettingsValidationError(f"{label} must be a positive integer")
    return cast("int", value)


def validate_temperature(
    value: Any,
    *,
    label: str,
    allow_none: bool = False,
) -> float | None:
    """Validate one agent ``temperature`` value against the canonical schema rules."""
    if value is None:
        if allow_none:
            return None
        raise SettingsValidationError(f"{label} must be a number")

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SettingsValidationError(f"{label} must be a number")
    temperature = float(value)
    if not math.isfinite(temperature):
        raise SettingsValidationError(f"{label} must be finite")
    if temperature < MIN_TEMPERATURE or temperature > MAX_TEMPERATURE:
        raise SettingsValidationError(
            f"{label} must be between {MIN_TEMPERATURE:g} and {MAX_TEMPERATURE:g}"
        )
    return temperature


def validate_thinking_effort(
    value: Any,
    *,
    label: str,
    allow_none: bool = False,
) -> str | None:
    """Validate one agent ``thinking_effort`` value against the canonical schema rules."""
    if value is None:
        if allow_none:
            return None
        raise SettingsValidationError(f"{label} must be a string")

    if not isinstance(value, str):
        raise SettingsValidationError(f"{label} must be a string")
    if value not in ALLOWED_THINKING_EFFORTS:
        allowed = ", ".join(repr(item) for item in sorted(ALLOWED_THINKING_EFFORTS))
        raise SettingsValidationError(f"{label} must be one of: {allowed}")
    return value
