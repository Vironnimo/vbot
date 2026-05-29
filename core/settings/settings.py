"""Public Settings schema parsing and validation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, cast

from core.recall.recall import FIRST_PARTY_RECALL_BACKENDS

JsonObject = dict[str, Any]

ALLOWED_THINKING_EFFORTS = frozenset(
    {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
AGENT_DEFAULT_FIELDS = frozenset({"model", "fallback_model", "temperature", "thinking_effort"})
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0
SETTINGS_UPDATE_SECTIONS = frozenset(
    {"appearance", "skills", "subagents", "compaction", "defaults", "recall"}
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

    if "defaults" in params:
        parsed_update["defaults"] = _parse_defaults_update(params["defaults"])

    if "recall" in params:
        parsed_update["recall"] = _parse_recall_update(params["recall"])

    return parsed_update


def _parse_recall_update(recall: Any) -> JsonObject:
    if not isinstance(recall, dict):
        raise SettingsValidationError("params.recall must be an object")

    unsupported_fields = sorted(set(recall) - {"backend"})
    if unsupported_fields:
        raise SettingsValidationError(
            f"unsupported recall settings: {', '.join(unsupported_fields)}"
        )

    backend = recall.get("backend")
    if not isinstance(backend, str) or backend not in FIRST_PARTY_RECALL_BACKENDS:
        allowed = ", ".join(sorted(FIRST_PARTY_RECALL_BACKENDS))
        raise SettingsValidationError(f"params.recall.backend must be one of: {allowed}")

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
            agent_defaults[field] = _validate_temperature(
                value,
                label="params.defaults.agent.temperature",
                allow_none=True,
            )
            continue

        if field == "thinking_effort":
            agent_defaults[field] = _validate_thinking_effort(
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


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsValidationError(f"{label} must be a positive integer")
    if value <= 0:
        raise SettingsValidationError(f"{label} must be a positive integer")
    return cast("int", value)


def _validate_temperature(
    value: Any,
    *,
    label: str,
    allow_none: bool = False,
) -> float | None:
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


def _validate_thinking_effort(
    value: Any,
    *,
    label: str,
    allow_none: bool = False,
) -> str | None:
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
