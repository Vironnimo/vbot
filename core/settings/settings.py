"""Public Settings schema parsing and validation."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

JsonObject = dict[str, Any]
DiagnosticSeverity = str

ALLOWED_THINKING_EFFORTS = frozenset(
    {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
AGENT_DEFAULT_FIELDS = frozenset({"model", "fallback_model", "temperature", "thinking_effort"})
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0
SETTINGS_UPDATE_SECTIONS = frozenset(
    {"appearance", "skills", "subagents", "compaction", "defaults"}
)
KNOWN_RAW_SETTINGS_KEYS = frozenset(
    {
        "PORT",
        "SERVER_PORT",
        "appearance",
        "attachment_max_size_bytes",
        "compaction",
        "defaults",
        "extension_directories",
        "max_subagent_depth",
        "max_subagents_per_turn",
        "port",
        "server_port",
        "skill_directories",
        "subagent_timeout_minutes",
    }
)
PORT_SETTING_KEYS = frozenset({"PORT", "SERVER_PORT", "port", "server_port"})
SUBAGENT_SETTING_FIELDS = (
    "max_subagent_depth",
    "max_subagents_per_turn",
    "subagent_timeout_minutes",
)
APPEARANCE_FIELDS = frozenset({"language"})
SUPPORTED_APPEARANCE_LANGUAGES = frozenset({"en"})
COMPACTION_FIELDS = frozenset({"auto", "threshold", "tail_tokens", "summary_model"})
DEFAULTS_SECTIONS = frozenset({"agent"})


class SettingsValidationError(ValueError):
    """Raised when a public Settings payload is malformed."""


@dataclass(frozen=True)
class SettingsDiagnostic:
    """One raw settings file validation diagnostic."""

    severity: DiagnosticSeverity
    path: str
    message: str


@dataclass(frozen=True)
class SettingsValidationReport:
    """Validation result for one raw settings file."""

    file_path: Path
    exists: bool
    diagnostics: tuple[SettingsDiagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(diagnostic.severity == "error" for diagnostic in self.diagnostics)

    @property
    def error_count(self) -> int:
        return sum(1 for diagnostic in self.diagnostics if diagnostic.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for diagnostic in self.diagnostics if diagnostic.severity == "warning")


def validate_settings_file(settings_path: str | Path) -> SettingsValidationReport:
    """Validate a raw ``settings.json`` file without mutating it."""

    path = Path(settings_path)
    if not path.exists():
        return SettingsValidationReport(file_path=path, exists=False)

    try:
        data = _load_json_path(path)
    except json.JSONDecodeError as exc:
        return SettingsValidationReport(
            file_path=path,
            exists=True,
            diagnostics=(
                SettingsDiagnostic(
                    severity="error",
                    path="$",
                    message=(f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"),
                ),
            ),
        )
    except OSError as exc:
        return SettingsValidationReport(
            file_path=path,
            exists=True,
            diagnostics=(
                SettingsDiagnostic(
                    severity="error",
                    path="$",
                    message=f"cannot read settings file: {exc}",
                ),
            ),
        )

    return SettingsValidationReport(
        file_path=path,
        exists=True,
        diagnostics=tuple(validate_settings_data(data)),
    )


def _load_json_path(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_settings_data(data: Any) -> list[SettingsDiagnostic]:
    """Validate a decoded raw Settings mapping and return diagnostics."""

    diagnostics: list[SettingsDiagnostic] = []
    if not isinstance(data, dict):
        return [
            SettingsDiagnostic(
                severity="error",
                path="$",
                message=f"expected JSON object, got {type(data).__name__}",
            )
        ]

    _warn_unknown_keys(diagnostics, "$", data, KNOWN_RAW_SETTINGS_KEYS, "settings key")
    _validate_port_settings(diagnostics, data)
    _validate_appearance(diagnostics, data.get("appearance"))
    _validate_directory_list(diagnostics, "$.skill_directories", data.get("skill_directories"))
    _validate_directory_list(
        diagnostics,
        "$.extension_directories",
        data.get("extension_directories"),
    )
    _validate_positive_integer(
        diagnostics,
        "$.attachment_max_size_bytes",
        data.get("attachment_max_size_bytes"),
        required=False,
    )
    for field in SUBAGENT_SETTING_FIELDS:
        _validate_positive_integer(diagnostics, f"$.{field}", data.get(field), required=False)
    _validate_compaction(diagnostics, data.get("compaction"))
    _validate_defaults(diagnostics, data.get("defaults"))
    return diagnostics


def _warn_unknown_keys(
    diagnostics: list[SettingsDiagnostic],
    parent_path: str,
    data: Mapping[str, Any],
    known_keys: frozenset[str],
    label: str,
) -> None:
    for key in sorted(set(data) - known_keys):
        diagnostics.append(
            SettingsDiagnostic(
                severity="warning",
                path=_child_path(parent_path, key),
                message=f"unknown {label}: {key}",
            )
        )


def _validate_port_settings(diagnostics: list[SettingsDiagnostic], data: Mapping[str, Any]) -> None:
    for key in sorted(PORT_SETTING_KEYS):
        if key in data:
            _validate_port(diagnostics, f"$.{key}", data[key])


def _validate_port(diagnostics: list[SettingsDiagnostic], path: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int | str):
        _error(diagnostics, path, "must be an integer port")
        return
    try:
        port = int(value)
    except ValueError:
        _error(diagnostics, path, "must be an integer port")
        return
    if port < 1 or port > 65535:
        _error(diagnostics, path, "must be between 1 and 65535")


def _validate_appearance(diagnostics: list[SettingsDiagnostic], value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, "$.appearance", "must be an object")
        return

    _warn_unknown_keys(diagnostics, "$.appearance", value, APPEARANCE_FIELDS, "appearance field")
    language = value.get("language")
    if language is None:
        return
    if not isinstance(language, str) or not language:
        _error(diagnostics, "$.appearance.language", "must be a non-empty string")
        return
    if language not in SUPPORTED_APPEARANCE_LANGUAGES:
        supported = ", ".join(sorted(SUPPORTED_APPEARANCE_LANGUAGES))
        _error(
            diagnostics, "$.appearance.language", f"unsupported language; supported: {supported}"
        )


def _validate_directory_list(
    diagnostics: list[SettingsDiagnostic],
    path: str,
    value: Any,
) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        _error(diagnostics, path, "must be a list")
        return
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, str) or not item.strip():
            _error(diagnostics, item_path, "must be a non-empty string")
            continue
        if not _is_absolute_or_home_relative_path(item.strip()):
            _error(diagnostics, item_path, "must be an absolute or home-relative path")


def _validate_positive_integer(
    diagnostics: list[SettingsDiagnostic],
    path: str,
    value: Any,
    *,
    required: bool,
) -> None:
    if value is None:
        if required:
            _error(diagnostics, path, "is required")
        return
    if isinstance(value, bool) or not isinstance(value, int):
        _error(diagnostics, path, "must be a positive integer")
        return
    if value <= 0:
        _error(diagnostics, path, "must be a positive integer")


def _validate_compaction(diagnostics: list[SettingsDiagnostic], value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, "$.compaction", "must be an object")
        return

    _warn_unknown_keys(diagnostics, "$.compaction", value, COMPACTION_FIELDS, "compaction field")
    if "auto" in value and not isinstance(value["auto"], bool):
        _error(diagnostics, "$.compaction.auto", "must be a boolean")
    if "threshold" in value:
        threshold = value["threshold"]
        if isinstance(threshold, bool) or not isinstance(threshold, int | float):
            _error(diagnostics, "$.compaction.threshold", "must be a number")
        else:
            normalized_threshold = float(threshold)
            if normalized_threshold <= 0 or normalized_threshold > 1:
                _error(diagnostics, "$.compaction.threshold", "must be in (0, 1]")
    _validate_positive_integer(
        diagnostics,
        "$.compaction.tail_tokens",
        value.get("tail_tokens"),
        required=False,
    )
    if (
        "summary_model" in value
        and value["summary_model"] is not None
        and not isinstance(value["summary_model"], str)
    ):
        _error(diagnostics, "$.compaction.summary_model", "must be a string or null")


def _validate_defaults(diagnostics: list[SettingsDiagnostic], value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, "$.defaults", "must be an object")
        return

    unsupported_sections = sorted(set(value) - DEFAULTS_SECTIONS)
    for section in unsupported_sections:
        _error(
            diagnostics,
            _child_path("$.defaults", section),
            f"unsupported defaults section: {section}",
        )

    agent_defaults = value.get("agent")
    if agent_defaults is None:
        return
    if not isinstance(agent_defaults, Mapping):
        _error(diagnostics, "$.defaults.agent", "must be an object")
        return

    unsupported_fields = sorted(set(agent_defaults) - AGENT_DEFAULT_FIELDS)
    for field in unsupported_fields:
        _error(
            diagnostics,
            _child_path("$.defaults.agent", field),
            f"unsupported defaults.agent setting: {field}",
        )

    for field, item in agent_defaults.items():
        if field not in AGENT_DEFAULT_FIELDS:
            continue
        item_path = _child_path("$.defaults.agent", field)
        if item is None:
            continue
        if field in {"model", "fallback_model"}:
            if not isinstance(item, str):
                _error(diagnostics, item_path, "must be a string or null")
            continue
        if field == "temperature":
            try:
                _validate_temperature(item, label=item_path, allow_none=True)
            except SettingsValidationError as exc:
                _error(diagnostics, item_path, str(exc).removeprefix(f"{item_path} "))
            continue
        if field == "thinking_effort":
            try:
                _validate_thinking_effort(item, label=item_path, allow_none=True)
            except SettingsValidationError as exc:
                _error(diagnostics, item_path, str(exc).removeprefix(f"{item_path} "))


def _child_path(parent_path: str, key: str) -> str:
    if key.replace("_", "").isalnum():
        return f"{parent_path}.{key}"
    return f"{parent_path}[{key!r}]"


def _is_absolute_or_home_relative_path(path: str) -> bool:
    if path == "~" or path.startswith(("~/", "~\\")):
        return True
    return Path(path).is_absolute()


def _error(diagnostics: list[SettingsDiagnostic], path: str, message: str) -> None:
    diagnostics.append(SettingsDiagnostic(severity="error", path=path, message=message))


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

    return parsed_update


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
