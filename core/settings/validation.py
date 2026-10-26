"""Raw ``settings.json`` validation and data-dir validation orchestration."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonObject,
    JsonValidationReport,
    discover_json_documents,
    load_validated_json_file,
    validate_json_file,
)
from core.config_validation import add_error as _error
from core.config_validation import error_diagnostic as _error_diagnostic
from core.config_validation import validate_positive_integer as _validate_positive_integer
from core.config_validation import warn_unknown_keys as _warn_unknown_keys
from core.fetch_config import parse_web_fetch_settings
from core.json_documents import (
    DURABLE_DOCUMENTS,
    FORMAT_VERSION_FIELD,
    JsonDocumentFormat,
    strip_unknown_fields,
    validate_format_version,
    warn_unknown_fields,
)
from core.settings._raw_schema import (
    APPEARANCE_FIELDS,
    ARCHIVE_FIELDS,
    COMPACTION_FIELDS,
    COMPACTION_POLICY_SHAPE,
    COMPACTION_STRATEGY_FIELDS,
    COMPACTION_TRIGGER_FIELDS,
    CUSTOM_PROVIDER_SHAPE,
    DEBUG_FIELDS,
    DEFAULTS_SECTIONS,
    EXTENSIONS_FIELDS,
    KNOWN_RAW_SETTINGS_KEYS,
    LIBRARIAN_FIELDS,
    LOCAL_MODELS_FIELDS,
    MAX_TRACE_LIMIT,
    MODEL_TASK_BINDING_FIELDS,
    NOTIFICATION_FIELD_SET,
    OPENROUTER_PROVIDER_FIELDS,
    OPENROUTER_ROUTING_SHAPE,
    PROVIDERS_FIELDS,
    RECALL_FIELDS,
    REFLECTION_FIELDS,
    REFLECTION_INTERVAL_FIELDS,
    SESSION_TITLE_FIELDS,
    SETTINGS_FORMAT_VERSION,
    SETTINGS_SHAPE,
    SPEECH_FIELDS,
    SUBAGENT_SETTING_FIELDS,
    TRANSCRIPTION_AUDIO_FIELDS,
    WEB_FETCH_SHAPE,
    WEB_SEARCH_FIELDS,
    WEB_SEARCH_SEARXNG_FIELDS,
)
from core.settings._raw_sections import (
    _validate_appearance,
    _validate_archive,
    _validate_compaction,
    _validate_debug,
    _validate_defaults,
    _validate_directory_list,
    _validate_extensions,
    _validate_librarian,
    _validate_local_models,
    _validate_model_tasks,
    _validate_notifications,
    _validate_port_settings,
    _validate_providers,
    _validate_recall,
    _validate_reflection,
    _validate_session_titles,
    _validate_speech,
    _validate_timezone,
    _validate_web_search,
    validate_optional_compaction_policy,
    validate_temperature_diagnostic,
    validate_thinking_effort_diagnostic,
    validate_top_p_diagnostic,
)
from core.settings.agent_defaults import INVALID_PERMISSION_RULES
from core.settings.settings import SettingsValidationError

SettingsDiagnostic = JsonDiagnostic
SettingsValidationReport = JsonValidationReport


def validate_settings_document(data: Any) -> list[JsonDiagnostic]:
    """Validate a decoded ``settings.json`` document, including its ``format_version``."""

    if not isinstance(data, dict):
        return [_error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]
    diagnostics: list[JsonDiagnostic] = []
    if not validate_format_version(diagnostics, data, SETTINGS_FORMAT_VERSION):
        return diagnostics
    return diagnostics + validate_settings_data(data)


SETTINGS_FORMAT = JsonDocumentFormat(
    name="Settings file",
    version=SETTINGS_FORMAT_VERSION,
    shape=SETTINGS_SHAPE,
    validate=validate_settings_document,
    sort_keys=True,
)


def validate_settings_file(settings_path: str | Path) -> JsonValidationReport:
    """Validate a raw ``settings.json`` file without mutating it."""
    path = Path(settings_path)
    _paths, failures = discover_json_documents(path.parent, path.name)
    if failures:
        return failures[0]
    return validate_json_file(settings_path, validate_settings_document, missing_ok=True)


def load_validated_settings_json(settings_path: str | Path) -> JsonObject:
    """Load the Settings fields of a valid ``settings.json``, or `{}` when missing.

    ``format_version`` and unknown fields are left out; the Settings writer sets
    the version and merges unknown fields back from disk.
    """
    try:
        data = load_validated_json_file(
            settings_path,
            validate_settings_document,
            missing_ok=True,
            missing_default={},
        )
    except JsonConfigValidationError as error:
        raise SettingsValidationError(str(error)) from error
    return _settings_fields(data)


def load_runtime_settings_json(
    settings_path: str | Path,
) -> tuple[JsonObject, tuple[JsonDiagnostic, ...]]:
    """Load usable Settings without letting one invalid key reject valid siblings.

    Syntax, root-shape, and ``format_version`` failures cannot be isolated and
    therefore raise. Schema failures are isolated at their top-level Settings key:
    the invalid section or scalar is omitted, valid siblings remain live, and
    diagnostics are returned so runtime callers can report the degradation without
    mutating the source file. ``format_version`` and unknown fields are left out
    of the result.
    """

    path = Path(settings_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, ()
    except UnicodeDecodeError as error:
        raise SettingsValidationError(f"{path}: error $: File is not valid UTF-8") from error
    except OSError as error:
        raise SettingsValidationError(f"{path}: error $: Cannot read file: {error}") from error

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SettingsValidationError(
            f"{path}: error $: Invalid JSON: {error.msg} "
            f"at line {error.lineno} column {error.colno}"
        ) from error
    except (ValueError, RecursionError) as error:
        raise SettingsValidationError(f"{path}: error $: JSON exceeds decoder limits") from error
    if not isinstance(data, dict):
        raise SettingsValidationError(
            f"{path}: error $: Expected a JSON object, got {type(data).__name__}"
        )
    version_errors: list[JsonDiagnostic] = []
    if not validate_format_version(version_errors, data, SETTINGS_FORMAT_VERSION):
        details = "; ".join(f"error {item.path}: {item.message}" for item in version_errors)
        raise SettingsValidationError(f"{path}: {details}")

    errors = tuple(
        diagnostic for diagnostic in validate_settings_data(data) if diagnostic.severity == "error"
    )
    invalid_keys = {_settings_top_level_key(diagnostic.path) for diagnostic in errors}
    usable = {key: value for key, value in data.items() if key not in invalid_keys}
    if "defaults" in invalid_keys:
        defaults = _usable_defaults(data.get("defaults"), errors)
        if defaults is not None:
            usable["defaults"] = defaults
    return _settings_fields(usable), errors


_PERMISSION_RULES_KEY = "permission_rules"


def unusable_settings_fallback(settings_path: str | Path) -> JsonObject:
    """The Settings a runtime reader uses when ``settings.json`` cannot be used at all.

    Every section falls back to its default except ``defaults.agent.permission_rules``:
    a file that cannot be read, or whose text names that key, may hold default
    Permission Rules, and dropping them would give every inheriting Agent full
    access. They become ``INVALID_PERMISSION_RULES`` instead, so those Agents
    refuse to run until the file is repaired, while Agents whose own rules set
    every part keep running.
    """
    try:
        raw: bytes | None = Path(settings_path).read_bytes()
    except FileNotFoundError:
        return {}
    except OSError:
        raw = None
    if raw is not None:
        # Look for evidence even in broken JSON. ASCII keys may use JSON escapes
        # or UTF-16/32; neither spelling may turn unreadable rules into full access.
        key_evidence = re.sub(
            rb"\\u00([0-9a-fA-F]{2})",
            lambda match: bytes([int(match[1], 16)]),
            raw.replace(b"\x00", b""),
        )
        if _PERMISSION_RULES_KEY.encode("ascii") not in key_evidence:
            return {}
    return {"defaults": {"agent": {_PERMISSION_RULES_KEY: INVALID_PERMISSION_RULES}}}


def _usable_defaults(defaults: Any, errors: tuple[JsonDiagnostic, ...]) -> JsonObject | None:
    """Keep the default Permission Rules of an invalid ``defaults`` section.

    The section is left out as a whole, except ``defaults.agent.permission_rules``:
    dropping it would give inheriting Agents full access. A valid value stays; an
    invalid one becomes ``INVALID_PERMISSION_RULES``, so those Agents refuse to run.
    """
    agent = defaults.get("agent") if isinstance(defaults, dict) else None
    if not isinstance(agent, dict) or agent.get("permission_rules") is None:
        return None
    field_path = "$.defaults.agent.permission_rules"
    failed = any(
        item.path == field_path or item.path.startswith((f"{field_path}.", f"{field_path}["))
        for item in errors
    )
    rules = INVALID_PERMISSION_RULES if failed else agent["permission_rules"]
    return {"agent": {"permission_rules": rules}}


def _settings_fields(data: JsonObject) -> JsonObject:
    """Return the Settings a runtime consumer reads from a decoded document."""

    fields = cast("JsonObject", strip_unknown_fields(data, SETTINGS_SHAPE))
    fields.pop(FORMAT_VERSION_FIELD, None)
    return fields


def _settings_top_level_key(path: str) -> str:
    """Extract the owning top-level Settings key from a schema diagnostic path."""

    if not path.startswith("$."):
        raise SettingsValidationError(f"Cannot isolate root Settings diagnostic: {path}")
    remainder = path[2:]
    return remainder.split(".", 1)[0].split("[", 1)[0]


def validate_data_dir_config(data_dir: str | Path) -> tuple[JsonValidationReport, ...]:
    """Validate every durable JSON document in a data directory.

    ``settings.json`` is always reported; every other document only when present.
    Plaintext credential files, which vBot no longer reads, are reported with a
    warning each.
    """

    # Settings owns bundle orchestration, while each persisted format is validated
    # by its domain. Imports stay local so those domains may reuse Settings-owned
    # scalar/Policy rules without creating package initialization cycles.
    from core.access.devices import validate_device_store_file
    from core.access.server_record import validate_server_access_file
    from core.agents import (
        validate_agent_file,
        validate_agent_order_file,
        validate_agent_rename_file,
        validate_agent_retirements_file,
    )
    from core.attachments import validate_attachment_metadata_file
    from core.automation import (
        validate_bootstrap_jobs_file,
        validate_cron_jobs_file,
        validate_librarian_state_file,
    )
    from core.calendar import validate_calendar_events_file
    from core.channels import validate_channel_file
    from core.computers._store import validate_computers_file
    from core.extensions.config_validation import validate_extension_documents
    from core.model_tasks.artifacts import validate_task_artifact_metadata_file
    from core.projects import validate_project_file, validate_project_order_file
    from core.secrets import credential_file_reports, validate_secret_store_file
    from core.skills import validate_skill_policy_file
    from core.statistics._period import validate_statistics_period_file
    from core.storage.prompt_blocks import validate_prompt_layout_file
    from core.tools.terminal_store import (
        validate_terminal_groups_file,
        validate_terminal_launch_history_file,
    )

    # One owner validator per kind of ``DURABLE_DOCUMENTS``, which owns the locations.
    validators: dict[str, Callable[[Path], JsonValidationReport]] = {
        "settings": validate_settings_file,
        "statistics_period": validate_statistics_period_file,
        "agent": validate_agent_file,
        "agent_order": validate_agent_order_file,
        "agent_rename": validate_agent_rename_file,
        "agent_retirements": validate_agent_retirements_file,
        "agent_prompt_layout": validate_prompt_layout_file,
        "librarian_state": validate_librarian_state_file,
        "prompt_layout": validate_prompt_layout_file,
        "channel": validate_channel_file,
        "project": validate_project_file,
        "project_order": validate_project_order_file,
        "cron_jobs": validate_cron_jobs_file,
        "bootstrap_jobs": validate_bootstrap_jobs_file,
        "calendar_events": validate_calendar_events_file,
        "skill_policy": validate_skill_policy_file,
        "terminal_launch_history": validate_terminal_launch_history_file,
        "terminal_groups": validate_terminal_groups_file,
        "secrets": validate_secret_store_file,
        "attachment_metadata": validate_attachment_metadata_file,
        "speech_artifact_metadata": validate_task_artifact_metadata_file,
        "access_devices": validate_device_store_file,
        "computers": validate_computers_file,
        "access_server": validate_server_access_file,
    }
    core_documents = {
        kind: pattern
        for kind, pattern in DURABLE_DOCUMENTS.items()
        if not pattern.startswith("extension-data/")
    }
    if set(validators) != set(core_documents):
        raise RuntimeError("every durable JSON document kind needs exactly one validator")
    root = Path(data_dir).expanduser()
    reports = [validate_settings_file(root / DURABLE_DOCUMENTS["settings"])]
    for kind, pattern in core_documents.items():
        if kind == "settings":
            continue
        validate = validators[kind]
        # A literal pattern matches only an existing file.
        paths, failures = discover_json_documents(root, pattern)
        reports.extend(failures)
        for path in paths:
            try:
                reports.append(validate(path))
            except Exception as error:
                # One owner failing must not suppress every later document. Do
                # not echo exception values: a validator can include credentials.
                reports.append(
                    JsonValidationReport(
                        path,
                        True,
                        (
                            _error_diagnostic(
                                "$",
                                f"Document validator failed ({type(error).__name__})",
                                code="validator_failed",
                            ),
                        ),
                    )
                )
    try:
        settings, _diagnostics = load_runtime_settings_json(root / "settings.json")
    except SettingsValidationError:
        settings = {}
    directories = [Path(path).expanduser() for path in settings.get("extension_directories", [])]
    reports.extend(validate_extension_documents(root, directories))
    # Credentials outside the live Secret Store: plaintext files vBot ignores, and
    # quarantined copies.
    reports.extend(credential_file_reports(root))
    reported = {report.file_path for report in reports}
    for pattern in DURABLE_DOCUMENTS.values():
        if pattern.startswith("extension-data/"):
            paths, failures = discover_json_documents(root, pattern)
            reports.extend(failures)
            for path in paths:
                if path not in reported:
                    reports.append(
                        JsonValidationReport(
                            path,
                            True,
                            (
                                _error_diagnostic(
                                    "$",
                                    "The Extension declares no validator for this document",
                                    code="missing_validator",
                                ),
                            ),
                        )
                    )
    # Several patterns can encounter the same unavailable parent directory.
    merged: dict[Path, JsonValidationReport] = {}
    for report in reports:
        previous = merged.get(report.file_path)
        diagnostics = tuple(
            dict.fromkeys(
                (
                    *(previous.diagnostics if previous else ()),
                    *report.diagnostics,
                )
            )
        )
        merged[report.file_path] = JsonValidationReport(
            report.file_path, report.exists, diagnostics
        )
    return tuple(merged.values())


def validate_settings_data(data: Any) -> list[JsonDiagnostic]:
    """Validate the Settings fields of a decoded mapping and return diagnostics.

    ``format_version`` belongs to the file rather than to the fields; see
    :func:`validate_settings_document`.
    """

    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, dict):
        return [_error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]

    _warn_unknown_keys(diagnostics, "$", data, KNOWN_RAW_SETTINGS_KEYS, "settings key")
    _validate_port_settings(diagnostics, data)
    _validate_timezone(diagnostics, data.get("timezone"))
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
    if "keep_awake" in data and not isinstance(data["keep_awake"], bool):
        _error(diagnostics, "$.keep_awake", "must be a boolean")
    for field in SUBAGENT_SETTING_FIELDS:
        _validate_positive_integer(diagnostics, f"$.{field}", data.get(field), required=False)
    _validate_compaction(diagnostics, data.get("compaction"))
    _validate_defaults(diagnostics, data.get("defaults"))
    _validate_recall(diagnostics, data.get("recall"))
    _validate_extensions(diagnostics, data.get("extensions"))
    _validate_web_search(diagnostics, data.get("web_search"))
    if "web_fetch" in data:
        web_fetch = data["web_fetch"]
        warn_unknown_fields(
            diagnostics, "$.web_fetch", web_fetch, WEB_FETCH_SHAPE, label="web_fetch field"
        )
        try:
            parse_web_fetch_settings(strip_unknown_fields(web_fetch, WEB_FETCH_SHAPE))
        except ValueError as error:
            _error(diagnostics, "$.web_fetch", str(error))
    _validate_model_tasks(diagnostics, data.get("model_tasks"))
    _validate_debug(diagnostics, data.get("debug"))
    _validate_archive(diagnostics, data.get("archive"))
    _validate_reflection(diagnostics, data.get("reflection"))
    _validate_librarian(diagnostics, data.get("librarian"))
    _validate_local_models(diagnostics, data.get("local_models"))
    _validate_providers(diagnostics, data.get("providers"))
    _validate_session_titles(diagnostics, data.get("session_titles"))
    _validate_notifications(diagnostics, data.get("notifications"))
    return diagnostics


__all__ = [
    "APPEARANCE_FIELDS",
    "ARCHIVE_FIELDS",
    "COMPACTION_FIELDS",
    "COMPACTION_POLICY_SHAPE",
    "COMPACTION_STRATEGY_FIELDS",
    "COMPACTION_TRIGGER_FIELDS",
    "CUSTOM_PROVIDER_SHAPE",
    "DEBUG_FIELDS",
    "DEFAULTS_SECTIONS",
    "EXTENSIONS_FIELDS",
    "KNOWN_RAW_SETTINGS_KEYS",
    "LIBRARIAN_FIELDS",
    "LOCAL_MODELS_FIELDS",
    "MAX_TRACE_LIMIT",
    "MODEL_TASK_BINDING_FIELDS",
    "NOTIFICATION_FIELD_SET",
    "OPENROUTER_PROVIDER_FIELDS",
    "OPENROUTER_ROUTING_SHAPE",
    "PROVIDERS_FIELDS",
    "RECALL_FIELDS",
    "REFLECTION_FIELDS",
    "REFLECTION_INTERVAL_FIELDS",
    "SESSION_TITLE_FIELDS",
    "SETTINGS_FORMAT_VERSION",
    "SETTINGS_SHAPE",
    "SPEECH_FIELDS",
    "SUBAGENT_SETTING_FIELDS",
    "TRANSCRIPTION_AUDIO_FIELDS",
    "WEB_FETCH_SHAPE",
    "WEB_SEARCH_FIELDS",
    "WEB_SEARCH_SEARXNG_FIELDS",
    "validate_optional_compaction_policy",
    "validate_temperature_diagnostic",
    "validate_thinking_effort_diagnostic",
    "validate_top_p_diagnostic",
]
