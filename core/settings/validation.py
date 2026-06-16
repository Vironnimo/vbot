"""Central validation for user-editable vBot JSON configuration files."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from core.memory import MEMORY_PROMPT_MODES
from core.model_tasks import SUPPORTED_TASK_TYPES
from core.search_config import FIRST_PARTY_WEB_SEARCH_PROVIDERS
from core.settings.normalizers import (
    SUPPORTED_APPEARANCE_LANGUAGES,
    is_absolute_or_home_relative_path,
)
from core.settings.settings import (
    AGENT_DEFAULT_FIELDS,
    AGENT_ID_PATTERN,
    RECALL_BACKEND_PATTERN,
    SUPPORTED_APPEARANCE_CHAT_WIDTHS,
    SettingsValidationError,
    validate_temperature,
    validate_thinking_effort,
)

DiagnosticSeverity = Literal["error", "warning"]
JsonObject = dict[str, Any]
JsonValidator = Callable[[Any], list["JsonDiagnostic"]]

KNOWN_RAW_SETTINGS_KEYS = frozenset(
    {
        "PORT",
        "SERVER_PORT",
        "appearance",
        "attachment_max_size_bytes",
        "compaction",
        "debug",
        "defaults",
        "extension_directories",
        "extensions",
        "max_subagent_depth",
        "max_subagents_per_turn",
        "model_tasks",
        "port",
        "recall",
        "server_port",
        "skill_directories",
        "speech_upload_max_size_bytes",
        "subagent_timeout_minutes",
        "web_search",
    }
)
PORT_SETTING_KEYS = frozenset({"PORT", "SERVER_PORT", "port", "server_port"})
SUBAGENT_SETTING_FIELDS = (
    "max_subagent_depth",
    "max_subagents_per_turn",
    "subagent_timeout_minutes",
)
APPEARANCE_FIELDS = frozenset({"language", "chat_width"})
COMPACTION_FIELDS = frozenset({"auto", "threshold", "tail_tokens", "summary_model"})
DEFAULTS_SECTIONS = frozenset({"agent"})
RECALL_FIELDS = frozenset({"backend"})
EXTENSIONS_FIELDS = frozenset({"disabled", "config"})
WEB_SEARCH_FIELDS = frozenset({"provider", "searxng"})
WEB_SEARCH_SEARXNG_FIELDS = frozenset({"base_url"})
MODEL_TASK_BINDING_FIELDS = frozenset({"target", "options"})
DEBUG_FIELDS = frozenset({"enabled", "trace_limit"})
MAX_TRACE_LIMIT = 500

AGENT_FIELDS = frozenset(
    {
        "allowed_skills",
        "allowed_tools",
        "created_at",
        "custom_system_prompt_enabled",
        "current_session_id",
        "fallback_model",
        "id",
        "memory_prompt_mode",
        "model",
        "name",
        "temperature",
        "thinking_effort",
        "updated_at",
        "workspace",
    }
)
REQUIRED_AGENT_FIELDS = frozenset(
    {
        "allowed_skills",
        "allowed_tools",
        "created_at",
        "fallback_model",
        "id",
        "model",
        "name",
        "temperature",
        "thinking_effort",
        "updated_at",
    }
)

CRON_JOB_FIELDS = frozenset(
    {
        "agent_id",
        "created_at",
        "cron_expression",
        "id",
        "last_fired_at",
        "prompt",
        "run_at",
        "schedule_type",
        "session_id",
        "status",
        "timezone",
    }
)
REQUIRED_CRON_JOB_FIELDS = frozenset(
    {"agent_id", "created_at", "id", "prompt", "schedule_type", "status"}
)
ALLOWED_CRON_SCHEDULE_TYPES = frozenset({"cron", "once"})
ALLOWED_CRON_STATUSES = frozenset({"active", "paused", "completed", "failed"})

CHANNEL_FIELDS = frozenset(
    {
        "agent_id",
        "allowed_chat_ids",
        "dm_scope",
        "enabled",
        "id",
        "mention_patterns",
        "observe_unaddressed",
        "owner_user_ids",
        "platform",
        "response_mode",
        "token_env_var",
    }
)
ALLOWED_CHANNEL_PLATFORMS = frozenset({"discord", "telegram"})
ALLOWED_CHANNEL_DM_SCOPES = frozenset(
    {"main", "per_account_channel_peer", "per_conversation", "per_peer"}
)
ALLOWED_CHANNEL_RESPONSE_MODES = frozenset({"all", "mention"})


@dataclass(frozen=True)
class JsonDiagnostic:
    """One JSON configuration validation diagnostic."""

    severity: DiagnosticSeverity
    path: str
    message: str


@dataclass(frozen=True)
class JsonValidationReport:
    """Validation result for one JSON configuration file."""

    file_path: Path
    exists: bool
    diagnostics: tuple[JsonDiagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(diagnostic.severity == "error" for diagnostic in self.diagnostics)

    @property
    def error_count(self) -> int:
        return sum(1 for diagnostic in self.diagnostics if diagnostic.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for diagnostic in self.diagnostics if diagnostic.severity == "warning")


SettingsDiagnostic = JsonDiagnostic
SettingsValidationReport = JsonValidationReport


@dataclass(frozen=True)
class _ValidatedDocument:
    report: JsonValidationReport
    data: Any = None


def validate_settings_file(settings_path: str | Path) -> JsonValidationReport:
    """Validate a raw ``settings.json`` file without mutating it."""

    return _load_and_validate_json_file(
        Path(settings_path),
        validate_settings_data,
        missing_ok=True,
    ).report


def load_validated_settings_json(settings_path: str | Path) -> JsonObject:
    """Load a validated raw ``settings.json`` mapping, or `{}` when missing."""

    document = _load_and_validate_json_file(
        Path(settings_path),
        validate_settings_data,
        missing_ok=True,
    )
    _raise_for_errors(document.report)
    if not document.report.exists:
        return {}
    return cast("JsonObject", document.data)


def validate_agent_file(agent_path: str | Path) -> JsonValidationReport:
    return _load_and_validate_json_file(
        Path(agent_path),
        validate_agent_data,
        missing_ok=False,
    ).report


def load_validated_agent_json(agent_path: str | Path) -> JsonObject:
    document = _load_and_validate_json_file(
        Path(agent_path),
        validate_agent_data,
        missing_ok=False,
    )
    _raise_for_errors(document.report)
    return cast("JsonObject", document.data)


def validate_cron_jobs_file(jobs_path: str | Path) -> JsonValidationReport:
    return _load_and_validate_json_file(
        Path(jobs_path),
        validate_cron_jobs_data,
        missing_ok=True,
    ).report


def load_validated_cron_jobs_json(jobs_path: str | Path) -> list[JsonObject]:
    document = _load_and_validate_json_file(
        Path(jobs_path),
        validate_cron_jobs_data,
        missing_ok=True,
    )
    _raise_for_errors(document.report)
    if not document.report.exists:
        return []
    return cast("list[JsonObject]", document.data)


def validate_channel_file(config_path: str | Path) -> JsonValidationReport:
    return _load_and_validate_json_file(
        Path(config_path),
        validate_channel_data,
        missing_ok=False,
    ).report


def load_validated_channel_json(config_path: str | Path) -> JsonObject:
    document = _load_and_validate_json_file(
        Path(config_path),
        validate_channel_data,
        missing_ok=False,
    )
    _raise_for_errors(document.report)
    return cast("JsonObject", document.data)


def validate_data_dir_config(data_dir: str | Path) -> tuple[JsonValidationReport, ...]:
    """Validate all current user-editable JSON config files in a data directory."""

    root = Path(data_dir).expanduser()
    reports = [validate_settings_file(root / "settings.json")]
    reports.extend(
        validate_agent_file(agent_path)
        for agent_path in sorted((root / "agents").glob("*/agent.json"))
    )
    reports.extend(
        validate_channel_file(channel_path)
        for channel_path in sorted((root / "channels").glob("*/channel.json"))
    )
    cron_jobs_path = root / "cron" / "jobs.json"
    if cron_jobs_path.exists():
        reports.append(validate_cron_jobs_file(cron_jobs_path))
    return tuple(reports)


def validate_settings_data(data: Any) -> list[JsonDiagnostic]:
    """Validate a decoded raw Settings mapping and return diagnostics."""

    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, dict):
        return [_error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]

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
    _validate_positive_integer(
        diagnostics,
        "$.speech_upload_max_size_bytes",
        data.get("speech_upload_max_size_bytes"),
        required=False,
    )
    for field in SUBAGENT_SETTING_FIELDS:
        _validate_positive_integer(diagnostics, f"$.{field}", data.get(field), required=False)
    _validate_compaction(diagnostics, data.get("compaction"))
    _validate_defaults(diagnostics, data.get("defaults"))
    _validate_recall(diagnostics, data.get("recall"))
    _validate_extensions(diagnostics, data.get("extensions"))
    _validate_web_search(diagnostics, data.get("web_search"))
    _validate_model_tasks(diagnostics, data.get("model_tasks"))
    _validate_debug(diagnostics, data.get("debug"))
    return diagnostics


def validate_agent_data(data: Any) -> list[JsonDiagnostic]:
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, dict):
        return [_error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]

    _warn_unknown_keys(diagnostics, "$", data, AGENT_FIELDS, "agent field")
    _validate_required_fields(diagnostics, "$", data, REQUIRED_AGENT_FIELDS)
    _validate_agent_id(diagnostics, data.get("id"))
    _validate_non_empty_string(diagnostics, "$.name", data.get("name"), required=True)
    _validate_string(diagnostics, "$.model", data.get("model"), required=True)
    _validate_string(diagnostics, "$.fallback_model", data.get("fallback_model"), required=True)
    _validate_optional_path_string(diagnostics, "$.workspace", data.get("workspace"))
    _validate_temperature_value(
        diagnostics, "$.temperature", data.get("temperature"), allow_none=True
    )
    _validate_thinking_effort_value(
        diagnostics,
        "$.thinking_effort",
        data.get("thinking_effort"),
        allow_none=True,
    )
    _validate_optional_allowed_string(
        diagnostics,
        "$.memory_prompt_mode",
        data.get("memory_prompt_mode"),
        frozenset(MEMORY_PROMPT_MODES),
    )
    _validate_string_list(diagnostics, "$.allowed_tools", data.get("allowed_tools"))
    _validate_string_list(diagnostics, "$.allowed_skills", data.get("allowed_skills"))
    if "custom_system_prompt_enabled" in data and not isinstance(
        data["custom_system_prompt_enabled"], bool
    ):
        _error(diagnostics, "$.custom_system_prompt_enabled", "must be a boolean")
    _validate_string(diagnostics, "$.created_at", data.get("created_at"), required=True)
    _validate_string(diagnostics, "$.updated_at", data.get("updated_at"), required=True)
    if "current_session_id" in data:
        _validate_string(
            diagnostics, "$.current_session_id", data.get("current_session_id"), required=False
        )
    return diagnostics


def validate_cron_jobs_data(data: Any) -> list[JsonDiagnostic]:
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, list):
        return [_error_diagnostic("$", f"Expected a JSON array, got {type(data).__name__}")]

    for index, item in enumerate(data):
        item_path = f"$[{index}]"
        if not isinstance(item, dict):
            _error(diagnostics, item_path, "Expected a JSON object")
            continue
        _warn_unknown_keys(diagnostics, item_path, item, CRON_JOB_FIELDS, "cron job field")
        _validate_non_empty_string(diagnostics, f"{item_path}.id", item.get("id"), required=True)
        _validate_non_empty_string(
            diagnostics, f"{item_path}.agent_id", item.get("agent_id"), required=True
        )
        _validate_non_empty_string(
            diagnostics, f"{item_path}.prompt", item.get("prompt"), required=True
        )
        _validate_allowed_string(
            diagnostics,
            f"{item_path}.schedule_type",
            item.get("schedule_type"),
            ALLOWED_CRON_SCHEDULE_TYPES,
        )
        _validate_allowed_string(
            diagnostics,
            f"{item_path}.status",
            item.get("status"),
            ALLOWED_CRON_STATUSES,
        )
        _validate_optional_string(
            diagnostics, f"{item_path}.cron_expression", item.get("cron_expression")
        )
        _validate_optional_string(diagnostics, f"{item_path}.run_at", item.get("run_at"))
        _validate_optional_string(diagnostics, f"{item_path}.timezone", item.get("timezone"))
        _validate_optional_string(diagnostics, f"{item_path}.session_id", item.get("session_id"))
        _validate_optional_string(
            diagnostics, f"{item_path}.last_fired_at", item.get("last_fired_at")
        )
        _validate_non_empty_string(
            diagnostics, f"{item_path}.created_at", item.get("created_at"), required=True
        )
    return diagnostics


def validate_channel_data(data: Any) -> list[JsonDiagnostic]:
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, dict):
        return [_error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]

    _warn_unknown_keys(diagnostics, "$", data, CHANNEL_FIELDS, "channel field")
    _validate_non_empty_string(diagnostics, "$.id", data.get("id"), required=True)
    _validate_allowed_string(
        diagnostics,
        "$.platform",
        data.get("platform"),
        ALLOWED_CHANNEL_PLATFORMS,
    )
    _validate_non_empty_string(diagnostics, "$.agent_id", data.get("agent_id"), required=True)
    _validate_allowed_string(
        diagnostics,
        "$.dm_scope",
        data.get("dm_scope", "per_conversation"),
        ALLOWED_CHANNEL_DM_SCOPES,
    )
    _validate_platform_id_list(
        diagnostics,
        "$.allowed_chat_ids",
        data.get("allowed_chat_ids", []),
    )
    _validate_non_empty_string(
        diagnostics, "$.token_env_var", data.get("token_env_var"), required=True
    )
    if "enabled" in data and not isinstance(data["enabled"], bool):
        _error(diagnostics, "$.enabled", "must be a boolean")
    if "observe_unaddressed" in data and not isinstance(data["observe_unaddressed"], bool):
        _error(diagnostics, "$.observe_unaddressed", "must be a boolean")
    _validate_allowed_string(
        diagnostics,
        "$.response_mode",
        data.get("response_mode", "mention"),
        ALLOWED_CHANNEL_RESPONSE_MODES,
    )
    _validate_regex_list(diagnostics, "$.mention_patterns", data.get("mention_patterns", []))
    _validate_user_id_list(diagnostics, "$.owner_user_ids", data.get("owner_user_ids", []))
    return diagnostics


def format_report_diagnostics(report: JsonValidationReport) -> list[str]:
    return [
        f"{diagnostic.severity} {diagnostic.path}: {diagnostic.message}"
        for diagnostic in report.diagnostics
    ]


def _load_and_validate_json_file(
    file_path: Path,
    validator: JsonValidator,
    *,
    missing_ok: bool,
) -> _ValidatedDocument:
    if not file_path.exists():
        diagnostics: tuple[JsonDiagnostic, ...] = ()
        if not missing_ok:
            diagnostics = (_error_diagnostic("$", "File does not exist"),)
        return _ValidatedDocument(
            report=JsonValidationReport(file_path=file_path, exists=False, diagnostics=diagnostics)
        )

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _ValidatedDocument(
            report=JsonValidationReport(
                file_path=file_path,
                exists=True,
                diagnostics=(
                    _error_diagnostic(
                        "$",
                        f"Invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}",
                    ),
                ),
            )
        )
    except OSError as exc:
        return _ValidatedDocument(
            report=JsonValidationReport(
                file_path=file_path,
                exists=True,
                diagnostics=(_error_diagnostic("$", f"Cannot read file: {exc}"),),
            )
        )

    return _ValidatedDocument(
        report=JsonValidationReport(
            file_path=file_path,
            exists=True,
            diagnostics=tuple(validator(data)),
        ),
        data=data,
    )


def _raise_for_errors(report: JsonValidationReport) -> None:
    if report.ok:
        return
    details = "; ".join(format_report_diagnostics(report))
    raise SettingsValidationError(f"{report.file_path}: {details}")


def _warn_unknown_keys(
    diagnostics: list[JsonDiagnostic],
    parent_path: str,
    data: Mapping[str, Any],
    known_keys: frozenset[str],
    label: str,
) -> None:
    for key in sorted(set(data) - known_keys):
        diagnostics.append(
            JsonDiagnostic(
                severity="warning",
                path=_child_path(parent_path, key),
                message=f"unknown {label}: {key}",
            )
        )


def _validate_required_fields(
    diagnostics: list[JsonDiagnostic],
    parent_path: str,
    data: Mapping[str, Any],
    required_fields: frozenset[str],
) -> None:
    for field in sorted(required_fields - set(data)):
        _error(diagnostics, _child_path(parent_path, field), "is required")


def _validate_port_settings(diagnostics: list[JsonDiagnostic], data: Mapping[str, Any]) -> None:
    for key in sorted(PORT_SETTING_KEYS):
        if key in data:
            _validate_port(diagnostics, f"$.{key}", data[key])


def _validate_port(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
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


def _validate_appearance(diagnostics: list[JsonDiagnostic], value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, "$.appearance", "must be an object")
        return

    _warn_unknown_keys(diagnostics, "$.appearance", value, APPEARANCE_FIELDS, "appearance field")
    _validate_appearance_chat_width(diagnostics, value.get("chat_width"))

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


def _validate_appearance_chat_width(diagnostics: list[JsonDiagnostic], value: Any) -> None:
    if value is None:
        return
    if value not in SUPPORTED_APPEARANCE_CHAT_WIDTHS:
        supported = ", ".join(sorted(SUPPORTED_APPEARANCE_CHAT_WIDTHS))
        _error(
            diagnostics,
            "$.appearance.chat_width",
            f"unsupported chat width; supported: {supported}",
        )


def _validate_directory_list(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
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
        if not is_absolute_or_home_relative_path(item.strip()):
            _error(diagnostics, item_path, "must be an absolute or home-relative path")


def _validate_positive_integer(
    diagnostics: list[JsonDiagnostic], path: str, value: Any, *, required: bool
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


def _validate_compaction(diagnostics: list[JsonDiagnostic], value: Any) -> None:
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
        diagnostics, "$.compaction.tail_tokens", value.get("tail_tokens"), required=False
    )
    if (
        "summary_model" in value
        and value["summary_model"] is not None
        and not isinstance(value["summary_model"], str)
    ):
        _error(diagnostics, "$.compaction.summary_model", "must be a string or null")


def _validate_defaults(diagnostics: list[JsonDiagnostic], value: Any) -> None:
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
            _validate_temperature_value(diagnostics, item_path, item, allow_none=True)
            continue
        if field == "thinking_effort":
            _validate_thinking_effort_value(diagnostics, item_path, item, allow_none=True)


def _validate_recall(diagnostics: list[JsonDiagnostic], value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, "$.recall", "must be an object")
        return

    _warn_unknown_keys(diagnostics, "$.recall", value, RECALL_FIELDS, "recall field")
    backend = value.get("backend")
    if backend is None:
        return
    if not isinstance(backend, str) or not backend.strip():
        _error(diagnostics, "$.recall.backend", "must be a non-empty string")
        return
    if RECALL_BACKEND_PATTERN.fullmatch(backend.strip()) is None:
        _error(diagnostics, "$.recall.backend", "must use lowercase snake_case")


def _validate_extensions(diagnostics: list[JsonDiagnostic], value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, "$.extensions", "must be an object")
        return

    _warn_unknown_keys(diagnostics, "$.extensions", value, EXTENSIONS_FIELDS, "extensions field")

    disabled = value.get("disabled")
    if disabled is not None:
        if not isinstance(disabled, list):
            _error(diagnostics, "$.extensions.disabled", "must be a list")
        else:
            for index, item in enumerate(disabled):
                if not isinstance(item, str) or not item.strip():
                    _error(
                        diagnostics,
                        f"$.extensions.disabled[{index}]",
                        "must be a non-empty string",
                    )

    config = value.get("config")
    if config is None:
        return
    if not isinstance(config, Mapping):
        _error(diagnostics, "$.extensions.config", "must be an object")
        return
    for key, item in config.items():
        if not isinstance(item, Mapping):
            _error(
                diagnostics,
                _child_path("$.extensions.config", str(key)),
                "must be an object",
            )


def _validate_web_search(diagnostics: list[JsonDiagnostic], value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, "$.web_search", "must be an object")
        return

    _warn_unknown_keys(diagnostics, "$.web_search", value, WEB_SEARCH_FIELDS, "web_search field")
    provider = value.get("provider")
    if provider is not None and (
        not isinstance(provider, str) or provider not in FIRST_PARTY_WEB_SEARCH_PROVIDERS
    ):
        allowed = ", ".join(sorted(FIRST_PARTY_WEB_SEARCH_PROVIDERS))
        _error(diagnostics, "$.web_search.provider", f"must be one of: {allowed}")

    searxng = value.get("searxng")
    if searxng is None:
        return
    if not isinstance(searxng, Mapping):
        _error(diagnostics, "$.web_search.searxng", "must be an object")
        return

    _warn_unknown_keys(
        diagnostics,
        "$.web_search.searxng",
        searxng,
        WEB_SEARCH_SEARXNG_FIELDS,
        "SearXNG field",
    )
    base_url = searxng.get("base_url")
    if "base_url" in searxng and (not isinstance(base_url, str) or not base_url.strip()):
        _error(diagnostics, "$.web_search.searxng.base_url", "must be a non-empty string")


def _validate_model_tasks(diagnostics: list[JsonDiagnostic], value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, "$.model_tasks", "must be an object")
        return

    for task_type, binding in value.items():
        task_path = _child_path("$.model_tasks", str(task_type))
        if not isinstance(task_type, str) or task_type not in SUPPORTED_TASK_TYPES:
            allowed = ", ".join(sorted(SUPPORTED_TASK_TYPES))
            _error(diagnostics, task_path, f"unsupported task type; supported: {allowed}")
            continue
        if not isinstance(binding, Mapping):
            _error(diagnostics, task_path, "must be an object")
            continue

        _warn_unknown_keys(
            diagnostics,
            task_path,
            binding,
            MODEL_TASK_BINDING_FIELDS,
            "model task field",
        )
        target = binding.get("target")
        if "target" in binding and (not isinstance(target, str) or not target.strip()):
            _error(diagnostics, _child_path(task_path, "target"), "must be a non-empty string")
        options = binding.get("options")
        if "options" in binding and not isinstance(options, Mapping):
            _error(diagnostics, _child_path(task_path, "options"), "must be an object")


def _validate_debug(diagnostics: list[JsonDiagnostic], value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, "$.debug", "must be an object")
        return

    _warn_unknown_keys(diagnostics, "$.debug", value, DEBUG_FIELDS, "debug field")
    if "enabled" in value and not isinstance(value["enabled"], bool):
        _error(diagnostics, "$.debug.enabled", "must be a boolean")
    if "trace_limit" in value:
        trace_limit = value["trace_limit"]
        if isinstance(trace_limit, bool) or not isinstance(trace_limit, int):
            _error(diagnostics, "$.debug.trace_limit", "must be a positive integer (1-500)")
        elif trace_limit <= 0:
            _error(diagnostics, "$.debug.trace_limit", "must be at least 1")
        elif trace_limit > MAX_TRACE_LIMIT:
            _error(
                diagnostics,
                "$.debug.trace_limit",
                f"must be at most {MAX_TRACE_LIMIT}",
            )


def _validate_agent_id(diagnostics: list[JsonDiagnostic], value: Any) -> None:
    if not isinstance(value, str) or not value:
        _error(diagnostics, "$.id", "must be a non-empty string")
        return
    if AGENT_ID_PATTERN.fullmatch(value) is None:
        _error(
            diagnostics,
            "$.id",
            "must be 1-64 characters using only letters, numbers, hyphen, or underscore",
        )


def _validate_string(
    diagnostics: list[JsonDiagnostic], path: str, value: Any, *, required: bool
) -> None:
    if value is None:
        if required:
            _error(diagnostics, path, "is required")
        return
    if not isinstance(value, str):
        _error(diagnostics, path, "must be a string")


def _validate_optional_string(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if value is not None and not isinstance(value, str):
        _error(diagnostics, path, "must be a string or null")


def _validate_non_empty_string(
    diagnostics: list[JsonDiagnostic], path: str, value: Any, *, required: bool
) -> None:
    if value is None:
        if required:
            _error(diagnostics, path, "is required")
        return
    if not isinstance(value, str) or not value.strip():
        _error(diagnostics, path, "must be a non-empty string")


def _validate_allowed_string(
    diagnostics: list[JsonDiagnostic], path: str, value: Any, allowed: frozenset[str]
) -> None:
    if value is None:
        _error(diagnostics, path, "is required")
        return
    if not isinstance(value, str) or value not in allowed:
        choices = ", ".join(sorted(allowed))
        _error(diagnostics, path, f"must be one of: {choices}")


def _validate_optional_allowed_string(
    diagnostics: list[JsonDiagnostic], path: str, value: Any, allowed: frozenset[str]
) -> None:
    if value is None:
        return
    if not isinstance(value, str) or value not in allowed:
        choices = ", ".join(sorted(allowed))
        _error(diagnostics, path, f"must be one of: {choices}")


def _validate_optional_path_string(
    diagnostics: list[JsonDiagnostic], path: str, value: Any
) -> None:
    if value is None or value == "":
        return
    if not isinstance(value, str):
        _error(diagnostics, path, "must be a path string")


def _validate_temperature_value(
    diagnostics: list[JsonDiagnostic], path: str, value: Any, *, allow_none: bool
) -> None:
    _delegate_field_rule(diagnostics, path, validate_temperature, value, allow_none=allow_none)


def _validate_thinking_effort_value(
    diagnostics: list[JsonDiagnostic], path: str, value: Any, *, allow_none: bool
) -> None:
    _delegate_field_rule(diagnostics, path, validate_thinking_effort, value, allow_none=allow_none)


def _delegate_field_rule(
    diagnostics: list[JsonDiagnostic],
    path: str,
    validator: Callable[..., Any],
    value: Any,
    *,
    allow_none: bool,
) -> None:
    """Run a canonical raise-based field validator, turning its error into a diagnostic.

    Keeps the canonical validators in ``core.settings.settings`` the single
    implementation of the value rules; the only adaptation is dropping the
    ``label`` prefix they embed (the diagnostic carries ``path`` separately).
    """
    try:
        validator(value, label=path, allow_none=allow_none)
    except SettingsValidationError as exc:
        _error(diagnostics, path, str(exc).removeprefix(f"{path} "))


def _validate_string_list(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if value is None:
        _error(diagnostics, path, "is required")
        return
    if not isinstance(value, list):
        _error(diagnostics, path, "must be a list of strings")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str):
            _error(diagnostics, f"{path}[{index}]", "must be a string")


def _validate_integer_list(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if not isinstance(value, list):
        _error(diagnostics, path, "must be a list of integers")
        return
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, int):
            _error(diagnostics, f"{path}[{index}]", "must be an integer")


def _validate_regex_list(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if not isinstance(value, list):
        _error(diagnostics, path, "must be a list of regex strings")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            _error(diagnostics, f"{path}[{index}]", "must be a non-empty string")
            continue
        try:
            re.compile(item)
        except re.error as error:
            _error(diagnostics, f"{path}[{index}]", f"must be a valid regex: {error}")


def _validate_user_id_list(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if not isinstance(value, list):
        _error(diagnostics, path, "must be a list of platform user ids")
        return
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            _error(diagnostics, f"{path}[{index}]", "must be a string or integer user id")
            continue
        if isinstance(item, str) and not item.strip():
            _error(diagnostics, f"{path}[{index}]", "must not be empty")


def _validate_platform_id_list(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if not isinstance(value, list):
        _error(diagnostics, path, "must be a list of platform ids")
        return
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            _error(diagnostics, f"{path}[{index}]", "must be a string or integer id")
            continue
        if isinstance(item, str) and not item.strip():
            _error(diagnostics, f"{path}[{index}]", "must not be empty")


def _child_path(parent_path: str, key: str) -> str:
    if key.replace("_", "").isalnum():
        return f"{parent_path}.{key}"
    return f"{parent_path}[{key!r}]"


def _error(diagnostics: list[JsonDiagnostic], path: str, message: str) -> None:
    diagnostics.append(_error_diagnostic(path, message))


def _error_diagnostic(path: str, message: str) -> JsonDiagnostic:
    return JsonDiagnostic(severity="error", path=path, message=message)
