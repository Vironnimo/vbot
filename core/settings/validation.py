"""Raw ``settings.json`` validation and data-dir validation orchestration."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonObject,
    JsonValidationReport,
    load_validated_json_file,
    validate_json_file,
)
from core.config_validation import (
    add_error as _error,
)
from core.config_validation import (
    child_path as _child_path,
)
from core.config_validation import (
    error_diagnostic as _error_diagnostic,
)
from core.config_validation import (
    validate_positive_integer as _validate_positive_integer,
)
from core.config_validation import (
    warn_unknown_keys as _warn_unknown_keys,
)
from core.model_tasks import SUPPORTED_TASK_TYPES
from core.model_tasks.constants import (
    SUPPORTED_TRANSCRIPTION_AUDIO_FORMATS,
    SUPPORTED_TRANSCRIPTION_AUDIO_PROFILES,
    SUPPORTED_TRANSCRIPTION_AUDIO_SAMPLE_RATES,
    TRANSCRIPTION_AUDIO_PRESETS,
)
from core.search_config import (
    FIRST_PARTY_WEB_SEARCH_PROVIDERS,
    MAX_WEB_SEARCH_COUNT,
    MIN_WEB_SEARCH_COUNT,
)
from core.settings.normalizers import (
    SUPPORTED_APPEARANCE_LANGUAGES,
    is_absolute_or_home_relative_path,
    normalize_custom_provider_settings,
)
from core.settings.settings import (
    AGENT_DEFAULT_FIELDS,
    RECALL_BACKEND_PATTERN,
    SUPPORTED_APPEARANCE_CHAT_WIDTHS,
    SettingsValidationError,
    parse_openrouter_routing,
    validate_temperature,
    validate_thinking_effort,
)
from core.utils.errors import StorageError

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
        "local_models",
        "max_subagent_depth",
        "max_subagents_per_turn",
        "model_tasks",
        "port",
        "providers",
        "recall",
        "reflection",
        "server_port",
        "session_titles",
        "skill_directories",
        "speech",
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
COMPACTION_FIELDS = frozenset({"enabled", "trigger", "strategy"})
DEFAULTS_SECTIONS = frozenset({"agent"})
RECALL_FIELDS = frozenset({"backend"})
EXTENSIONS_FIELDS = frozenset({"disabled", "config"})
WEB_SEARCH_FIELDS = frozenset({"provider", "default_count", "searxng"})
WEB_SEARCH_SEARXNG_FIELDS = frozenset({"base_url"})
MODEL_TASK_BINDING_FIELDS = frozenset({"target", "options"})
DEBUG_FIELDS = frozenset({"enabled", "trace_limit"})
SESSION_TITLE_FIELDS = frozenset({"enabled", "model"})
SPEECH_FIELDS = frozenset({"transcription_audio"})
TRANSCRIPTION_AUDIO_FIELDS = frozenset({"profile", "format", "sample_rate_hz"})
MAX_TRACE_LIMIT = 500
REFLECTION_FIELDS = frozenset({"enabled", "memory_turn_interval", "skill_model_step_interval"})
LOCAL_MODELS_FIELDS = frozenset({"context_windows"})
PROVIDERS_FIELDS = frozenset({"connections", "custom", "openrouter"})
OPENROUTER_PROVIDER_FIELDS = frozenset({"routing"})
REFLECTION_INTERVAL_FIELDS = ("memory_turn_interval", "skill_model_step_interval")

SettingsDiagnostic = JsonDiagnostic
SettingsValidationReport = JsonValidationReport


def validate_settings_file(settings_path: str | Path) -> JsonValidationReport:
    """Validate a raw ``settings.json`` file without mutating it."""
    return validate_json_file(settings_path, validate_settings_data, missing_ok=True)


def load_validated_settings_json(settings_path: str | Path) -> JsonObject:
    """Load a validated raw ``settings.json`` mapping, or `{}` when missing."""
    try:
        return cast(
            "JsonObject",
            load_validated_json_file(
                settings_path,
                validate_settings_data,
                missing_ok=True,
                missing_default={},
            ),
        )
    except JsonConfigValidationError as error:
        raise SettingsValidationError(str(error)) from error


def load_runtime_settings_json(
    settings_path: str | Path,
) -> tuple[JsonObject, tuple[JsonDiagnostic, ...]]:
    """Load usable Settings without letting one invalid key reject valid siblings.

    Syntax and root-shape failures cannot be isolated and therefore raise. Schema
    failures are isolated at their top-level Settings key: the invalid section or
    scalar is omitted, valid siblings remain live, and diagnostics are returned so
    runtime callers can report the degradation without mutating the source file.
    """

    path = Path(settings_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, ()
    except OSError as error:
        raise SettingsValidationError(f"{path}: error $: Cannot read file: {error}") from error

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SettingsValidationError(
            f"{path}: error $: Invalid JSON: {error.msg} "
            f"at line {error.lineno} column {error.colno}"
        ) from error
    if not isinstance(data, dict):
        raise SettingsValidationError(
            f"{path}: error $: Expected a JSON object, got {type(data).__name__}"
        )

    errors = tuple(
        diagnostic for diagnostic in validate_settings_data(data) if diagnostic.severity == "error"
    )
    if not errors:
        return dict(data), ()

    invalid_keys = {_settings_top_level_key(diagnostic.path) for diagnostic in errors}
    usable = {key: value for key, value in data.items() if key not in invalid_keys}
    return usable, errors


def _settings_top_level_key(path: str) -> str:
    """Extract the owning top-level Settings key from a schema diagnostic path."""

    if not path.startswith("$."):
        raise SettingsValidationError(f"Cannot isolate root Settings diagnostic: {path}")
    remainder = path[2:]
    return remainder.split(".", 1)[0].split("[", 1)[0]


def validate_data_dir_config(data_dir: str | Path) -> tuple[JsonValidationReport, ...]:
    """Validate all current user-editable JSON config files in a data directory."""

    # Settings owns bundle orchestration, while each persisted format is validated
    # by its domain. Imports stay local so those domains may reuse Settings-owned
    # scalar/Policy rules without creating package initialization cycles.
    from core.agents import validate_agent_file, validate_agent_order_file
    from core.automation import validate_cron_jobs_file
    from core.channels import validate_channel_file
    from core.projects import validate_project_file

    root = Path(data_dir).expanduser()
    reports = [validate_settings_file(root / "settings.json")]
    reports.extend(
        validate_agent_file(agent_path)
        for agent_path in sorted((root / "agents").glob("*/agent.json"))
    )
    agent_order_path = root / "agents" / "order.json"
    if agent_order_path.exists():
        reports.append(validate_agent_order_file(agent_order_path))
    reports.extend(
        validate_channel_file(channel_path)
        for channel_path in sorted((root / "channels").glob("*/channel.json"))
    )
    reports.extend(
        validate_project_file(project_path)
        for project_path in sorted((root / "projects").glob("*/project.json"))
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
    _validate_speech(diagnostics, data.get("speech"))
    for field in SUBAGENT_SETTING_FIELDS:
        _validate_positive_integer(diagnostics, f"$.{field}", data.get(field), required=False)
    _validate_compaction(diagnostics, data.get("compaction"))
    _validate_defaults(diagnostics, data.get("defaults"))
    _validate_recall(diagnostics, data.get("recall"))
    _validate_extensions(diagnostics, data.get("extensions"))
    _validate_web_search(diagnostics, data.get("web_search"))
    _validate_model_tasks(diagnostics, data.get("model_tasks"))
    _validate_debug(diagnostics, data.get("debug"))
    _validate_reflection(diagnostics, data.get("reflection"))
    _validate_local_models(diagnostics, data.get("local_models"))
    _validate_providers(diagnostics, data.get("providers"))
    _validate_session_titles(diagnostics, data.get("session_titles"))
    return diagnostics


def _validate_speech(diagnostics: list[JsonDiagnostic], value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, "$.speech", "must be an object")
        return
    _warn_unknown_keys(diagnostics, "$.speech", value, SPEECH_FIELDS, "speech field")

    audio = value.get("transcription_audio")
    if audio is None:
        return
    if not isinstance(audio, Mapping):
        _error(diagnostics, "$.speech.transcription_audio", "must be an object")
        return
    _warn_unknown_keys(
        diagnostics,
        "$.speech.transcription_audio",
        audio,
        TRANSCRIPTION_AUDIO_FIELDS,
        "transcription_audio field",
    )

    profile = audio.get("profile")
    if profile is not None and (
        not isinstance(profile, str) or profile not in SUPPORTED_TRANSCRIPTION_AUDIO_PROFILES
    ):
        allowed = ", ".join(sorted(SUPPORTED_TRANSCRIPTION_AUDIO_PROFILES))
        _error(
            diagnostics,
            "$.speech.transcription_audio.profile",
            f"must be one of: {allowed}",
        )

    audio_format = audio.get("format")
    if audio_format is not None and (
        not isinstance(audio_format, str)
        or audio_format not in SUPPORTED_TRANSCRIPTION_AUDIO_FORMATS
    ):
        allowed = ", ".join(sorted(SUPPORTED_TRANSCRIPTION_AUDIO_FORMATS))
        _error(
            diagnostics,
            "$.speech.transcription_audio.format",
            f"must be one of: {allowed}",
        )

    sample_rate_hz = audio.get("sample_rate_hz")
    if sample_rate_hz is not None and (
        not isinstance(sample_rate_hz, int)
        or isinstance(sample_rate_hz, bool)
        or sample_rate_hz not in SUPPORTED_TRANSCRIPTION_AUDIO_SAMPLE_RATES
    ):
        allowed = ", ".join(
            str(sample_rate) for sample_rate in sorted(SUPPORTED_TRANSCRIPTION_AUDIO_SAMPLE_RATES)
        )
        _error(
            diagnostics,
            "$.speech.transcription_audio.sample_rate_hz",
            f"must be one of: {allowed}",
        )

    preset = TRANSCRIPTION_AUDIO_PRESETS.get(profile) if isinstance(profile, str) else None
    if preset is None:
        return
    for field, preset_value in preset.items():
        if field in audio and audio[field] != preset_value:
            _error(
                diagnostics,
                f"$.speech.transcription_audio.{field}",
                f"must be {preset_value!r} for profile {profile!r}",
            )


def _validate_session_titles(diagnostics: list[JsonDiagnostic], value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, "$.session_titles", "must be an object")
        return

    _warn_unknown_keys(
        diagnostics,
        "$.session_titles",
        value,
        SESSION_TITLE_FIELDS,
        "session_titles field",
    )
    enabled = value.get("enabled")
    if "enabled" in value and not isinstance(enabled, bool):
        _error(diagnostics, "$.session_titles.enabled", "must be a boolean")
    model = value.get("model")
    if "model" in value and not isinstance(model, str):
        _error(diagnostics, "$.session_titles.model", "must be a string")


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


def _validate_compaction(diagnostics: list[JsonDiagnostic], value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, "$.compaction", "must be an object")
        return

    _warn_unknown_keys(diagnostics, "$.compaction", value, COMPACTION_FIELDS, "compaction field")
    if "enabled" in value and not isinstance(value["enabled"], bool):
        _error(diagnostics, "$.compaction.enabled", "must be a boolean")
    _validate_compaction_trigger(diagnostics, value.get("trigger"), "$.compaction.trigger")
    _validate_compaction_strategy(diagnostics, value.get("strategy"), "$.compaction.strategy")


def validate_optional_compaction_policy(
    diagnostics: list[JsonDiagnostic], value: Any, path: str
) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, path, "must be an object or null")
        return
    _warn_unknown_keys(
        diagnostics,
        path,
        value,
        frozenset({"enabled", "trigger", "strategy"}),
        "compaction Policy field",
    )
    if "enabled" in value and not isinstance(value["enabled"], bool):
        _error(diagnostics, f"{path}.enabled", "must be a boolean")
    _validate_compaction_trigger(diagnostics, value.get("trigger"), f"{path}.trigger")
    _validate_compaction_strategy(diagnostics, value.get("strategy"), f"{path}.strategy")


def _validate_compaction_trigger(diagnostics: list[JsonDiagnostic], value: Any, path: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, path, "must be an object")
        return
    trigger_type = value.get("type", "context_ratio")
    if trigger_type == "context_ratio":
        _warn_unknown_keys(
            diagnostics, path, value, frozenset({"type", "threshold"}), "trigger field"
        )
        if "threshold" in value:
            threshold = value["threshold"]
            if isinstance(threshold, bool) or not isinstance(threshold, int | float):
                _error(diagnostics, f"{path}.threshold", "must be a number")
            elif not 0 < float(threshold) <= 1:
                _error(diagnostics, f"{path}.threshold", "must be in (0, 1]")
        return
    if trigger_type == "input_tokens":
        _warn_unknown_keys(diagnostics, path, value, frozenset({"type", "tokens"}), "trigger field")
        _validate_positive_integer(
            diagnostics, f"{path}.tokens", value.get("tokens"), required=False
        )
        return
    _error(diagnostics, f"{path}.type", "must be context_ratio or input_tokens")


def _validate_compaction_strategy(diagnostics: list[JsonDiagnostic], value: Any, path: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, path, "must be an object")
        return
    strategy_type = value.get("type", "summary_tail")
    if strategy_type == "summary_tail":
        _warn_unknown_keys(
            diagnostics,
            path,
            value,
            frozenset({"type", "tail_tokens", "summary_model"}),
            "strategy field",
        )
        _validate_positive_integer(
            diagnostics, f"{path}.tail_tokens", value.get("tail_tokens"), required=False
        )
        summary_model = value.get("summary_model")
        if summary_model is not None and not isinstance(summary_model, str):
            _error(diagnostics, f"{path}.summary_model", "must be a string or null")
        return
    if strategy_type == "continuation":
        _warn_unknown_keys(diagnostics, path, value, frozenset({"type"}), "strategy field")
        return
    _error(diagnostics, f"{path}.type", "must be summary_tail or continuation")


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
            validate_temperature_diagnostic(diagnostics, item_path, item, allow_none=True)
            continue
        if field == "thinking_effort":
            validate_thinking_effort_diagnostic(diagnostics, item_path, item, allow_none=True)


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

    default_count = value.get("default_count")
    if default_count is not None and (
        isinstance(default_count, bool)
        or not isinstance(default_count, int)
        or not (MIN_WEB_SEARCH_COUNT <= default_count <= MAX_WEB_SEARCH_COUNT)
    ):
        _error(
            diagnostics,
            "$.web_search.default_count",
            f"must be an integer between {MIN_WEB_SEARCH_COUNT} and {MAX_WEB_SEARCH_COUNT}",
        )

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


def _validate_local_models(diagnostics: list[JsonDiagnostic], value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, "$.local_models", "must be an object")
        return

    _warn_unknown_keys(
        diagnostics, "$.local_models", value, LOCAL_MODELS_FIELDS, "local_models field"
    )
    context_windows = value.get("context_windows")
    if context_windows is None:
        return
    if not isinstance(context_windows, Mapping):
        _error(diagnostics, "$.local_models.context_windows", "must be an object")
        return
    for key, window in context_windows.items():
        key_path = f"$.local_models.context_windows['{key}']"
        if not isinstance(key, str) or "/" not in key or not key.strip():
            _error(diagnostics, key_path, "key must be a '<provider>/<model_id>' string")
            continue
        if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
            _error(diagnostics, key_path, "must be a positive integer")


def _validate_providers(diagnostics: list[JsonDiagnostic], value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, "$.providers", "must be an object")
        return

    _warn_unknown_keys(diagnostics, "$.providers", value, PROVIDERS_FIELDS, "providers field")
    connections = value.get("connections")
    if connections is not None:
        if not isinstance(connections, Mapping):
            _error(diagnostics, "$.providers.connections", "must be an object")
        else:
            for key, enabled in connections.items():
                key_path = f"$.providers.connections['{key}']"
                if not isinstance(key, str) or ":" not in key or not key.strip():
                    _error(diagnostics, key_path, "key must be a '<provider>:<connection>' string")
                    continue
                if not isinstance(enabled, bool):
                    _error(diagnostics, key_path, "must be a boolean")

    custom = value.get("custom")
    if custom is not None:
        if not isinstance(custom, Mapping):
            _error(diagnostics, "$.providers.custom", "must be an object")
        else:
            for provider_id, provider in custom.items():
                provider_path = _child_path("$.providers.custom", str(provider_id))
                try:
                    normalize_custom_provider_settings(str(provider_id), provider)
                except StorageError as exc:
                    _error(
                        diagnostics,
                        provider_path,
                        str(exc).replace("settings.providers.custom.", "", 1),
                    )

    openrouter = value.get("openrouter")
    if openrouter is None:
        return
    if not isinstance(openrouter, Mapping):
        _error(diagnostics, "$.providers.openrouter", "must be an object")
        return
    _warn_unknown_keys(
        diagnostics,
        "$.providers.openrouter",
        openrouter,
        OPENROUTER_PROVIDER_FIELDS,
        "providers.openrouter field",
    )
    routing = openrouter.get("routing")
    if routing is None:
        return
    try:
        parse_openrouter_routing(routing)
    except SettingsValidationError as exc:
        _error(
            diagnostics,
            "$.providers.openrouter.routing",
            str(exc).replace("params.providers.openrouter.routing", "routing"),
        )


def _validate_reflection(diagnostics: list[JsonDiagnostic], value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        _error(diagnostics, "$.reflection", "must be an object")
        return

    _warn_unknown_keys(diagnostics, "$.reflection", value, REFLECTION_FIELDS, "reflection field")
    if "enabled" in value and not isinstance(value["enabled"], bool):
        _error(diagnostics, "$.reflection.enabled", "must be a boolean")
    for field in REFLECTION_INTERVAL_FIELDS:
        if field not in value:
            continue
        interval = value[field]
        if isinstance(interval, bool) or not isinstance(interval, int):
            _error(diagnostics, f"$.reflection.{field}", "must be a positive integer")
        elif interval <= 0:
            _error(diagnostics, f"$.reflection.{field}", "must be at least 1")


def validate_temperature_diagnostic(
    diagnostics: list[JsonDiagnostic], path: str, value: Any, *, allow_none: bool
) -> None:
    _delegate_field_rule(diagnostics, path, validate_temperature, value, allow_none=allow_none)


def validate_thinking_effort_diagnostic(
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
