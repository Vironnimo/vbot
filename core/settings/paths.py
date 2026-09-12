"""Resolve public Settings paths and prepare validated atomic patches over persisted Settings."""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from difflib import get_close_matches
from typing import Any

from core.model_tasks import SUPPORTED_TASK_TYPES
from core.settings._path_definitions import (
    _DEFINITIONS,
    DEFAULT_ATTACHMENT_MAX_SIZE_BYTES,
    DEFAULT_SERVER_PORT,
    DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES,
    SUBAGENT_SETTING_DEFAULTS,
)
from core.settings._path_types import (
    _MISSING,
    APPLICATION_LIVE,
    APPLICATION_RESTART,
    DefinitionSegment,
    DynamicRemainder,
    DynamicSegment,
    JsonObject,
    PathSegment,
    ResolvedSetting,
    SettingDefinition,
    SettingsPatchOperation,
    SettingsPath,
    SettingsPathError,
)
from core.settings.normalizers import (
    normalize_appearance_settings,
    normalize_compaction_settings,
    normalize_debug_settings,
    normalize_defaults_settings,
    normalize_extensions_settings,
    normalize_local_models_settings,
    normalize_model_task_settings,
    normalize_providers_settings,
    normalize_recall_settings,
    normalize_reflection_settings,
    normalize_session_title_settings,
    normalize_skill_directories,
    normalize_speech_settings,
    normalize_subagent_integer,
    normalize_web_search_settings,
)
from core.settings.settings import (
    effective_timezone_name,
)
from core.settings.validation import PORT_SETTING_KEYS, validate_settings_data
from core.utils.errors import StorageError

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


def parse_settings_path(raw_path: str) -> SettingsPath:
    """Parse dotted identifiers plus JSON-quoted bracket segments."""

    if not isinstance(raw_path, str) or not raw_path:
        raise SettingsPathError("settings path must be a non-empty string")
    if raw_path != raw_path.strip():
        raise SettingsPathError("settings path must not have surrounding whitespace")

    position = 0
    match = _IDENTIFIER_PATTERN.match(raw_path, position)
    if match is None:
        raise SettingsPathError(
            "settings path must start with an identifier such as 'web_search.provider'"
        )
    segments = [PathSegment(match.group(), quoted=False)]
    position = match.end()

    while position < len(raw_path):
        marker = raw_path[position]
        if marker == ".":
            match = _IDENTIFIER_PATTERN.match(raw_path, position + 1)
            if match is None:
                raise SettingsPathError(f"invalid identifier after position {position + 1}")
            segments.append(PathSegment(match.group(), quoted=False))
            position = match.end()
            continue
        if marker == "[":
            try:
                value, consumed = json.JSONDecoder().raw_decode(raw_path[position + 1 :])
            except json.JSONDecodeError as error:
                raise SettingsPathError(
                    f"invalid quoted key at position {position}: {error.msg}"
                ) from error
            closing = position + 1 + consumed
            if closing >= len(raw_path) or raw_path[closing] != "]":
                raise SettingsPathError(f"quoted key at position {position} is missing closing ]")
            if not isinstance(value, str) or not value:
                raise SettingsPathError("bracket path segments must be non-empty JSON strings")
            segments.append(PathSegment(value, quoted=True))
            position = closing + 1
            continue
        raise SettingsPathError(
            f"unexpected character {marker!r} at position {position}; expected '.' or '['"
        )

    return SettingsPath(tuple(segments))


def setting_definitions() -> tuple[SettingDefinition, ...]:
    """Return the complete public Settings definition catalog."""

    return _DEFINITIONS


def resolve_setting(raw_path: str | SettingsPath) -> ResolvedSetting:
    """Resolve one concrete public path against the definition catalog."""

    path = parse_settings_path(raw_path) if isinstance(raw_path, str) else raw_path
    for definition in _DEFINITIONS:
        parameters = _match_definition(definition.pattern, path.segments)
        if parameters is not None:
            _validate_dynamic_parameters(parameters)
            return ResolvedSetting(path, definition, parameters)

    rendered = _format_input_path(path)
    templates = [definition.template for definition in _DEFINITIONS]
    suggestions = get_close_matches(rendered, templates, n=3, cutoff=0.35)
    suffix = f"; did you mean: {', '.join(suggestions)}" if suggestions else ""
    raise SettingsPathError(f"unknown settings path {rendered!r}{suffix}")


def build_effective_settings(raw_settings: JsonObject) -> JsonObject:
    """Build the normalized public Settings document from persisted data."""

    port = DEFAULT_SERVER_PORT
    for key in ("server_port", "SERVER_PORT", "port", "PORT"):
        if key in raw_settings:
            port = int(raw_settings[key])
            break

    appearance = normalize_appearance_settings(raw_settings.get("appearance"))
    extensions = normalize_extensions_settings(raw_settings.get("extensions"))
    providers = normalize_providers_settings(raw_settings.get("providers"))
    speech = normalize_speech_settings(raw_settings.get("speech"))
    return {
        "server": {
            "port": port,
            "keep_awake": raw_settings.get("keep_awake") is True,
            "timezone": effective_timezone_name(raw_settings),
        },
        "appearance": appearance,
        "live_voice": {"enabled": raw_settings.get("live_voice", {}).get("enabled") is True},
        "skills": {
            "directories": normalize_skill_directories(raw_settings.get("skill_directories"))
        },
        "extensions": {
            "directories": normalize_skill_directories(raw_settings.get("extension_directories")),
            **extensions,
        },
        "attachments": {
            "max_size_bytes": raw_settings.get(
                "attachment_max_size_bytes", DEFAULT_ATTACHMENT_MAX_SIZE_BYTES
            )
        },
        "speech": {
            "upload_max_size_bytes": raw_settings.get(
                "speech_upload_max_size_bytes", DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES
            ),
            **speech,
        },
        "subagents": {
            field: normalize_subagent_integer(field, raw_settings.get(field), default)
            for field, default in SUBAGENT_SETTING_DEFAULTS.items()
        },
        "compaction": normalize_compaction_settings(raw_settings.get("compaction")),
        "defaults": normalize_defaults_settings(raw_settings.get("defaults")),
        "providers": providers,
        "recall": normalize_recall_settings(raw_settings.get("recall")),
        "reflection": normalize_reflection_settings(raw_settings.get("reflection")),
        "web_search": normalize_web_search_settings(raw_settings.get("web_search")),
        "debug": normalize_debug_settings(raw_settings.get("debug")),
        "session_titles": normalize_session_title_settings(raw_settings.get("session_titles")),
        "local_models": normalize_local_models_settings(raw_settings.get("local_models")),
        "model_tasks": normalize_model_task_settings(raw_settings.get("model_tasks")),
    }


def parse_patch_operations(raw_operations: Any) -> list[SettingsPatchOperation]:
    """Validate a public patch payload before any Settings mutation."""

    if not isinstance(raw_operations, list) or not raw_operations:
        raise SettingsPathError("settings.patch requires a non-empty operations list")

    operations: list[SettingsPatchOperation] = []
    for index, raw_operation in enumerate(raw_operations):
        if not isinstance(raw_operation, dict):
            raise SettingsPathError(f"operations[{index}] must be an object")
        unknown = sorted(set(raw_operation) - {"op", "path", "value"})
        if unknown:
            raise SettingsPathError(
                f"operations[{index}] has unsupported fields: {', '.join(unknown)}"
            )
        operation = raw_operation.get("op")
        if operation not in {"set", "unset"}:
            raise SettingsPathError(f"operations[{index}].op must be set or unset")
        raw_path = raw_operation.get("path")
        if not isinstance(raw_path, str):
            raise SettingsPathError(f"operations[{index}].path must be a string")
        resolved = resolve_setting(raw_path)

        if operation == "set":
            if "value" not in raw_operation:
                raise SettingsPathError(f"operations[{index}] set requires value")
            value = raw_operation["value"]
            _validate_value(resolved.definition, value, resolved.canonical_path)
            operations.append(SettingsPatchOperation("set", resolved, deepcopy(value)))
            continue

        if "value" in raw_operation:
            raise SettingsPathError(f"operations[{index}] unset must not include value")
        if not resolved.definition.unsettable:
            raise SettingsPathError(
                f"{resolved.canonical_path} cannot be unset independently; "
                "unset its owning object or set an explicit value"
            )
        operations.append(SettingsPatchOperation("unset", resolved))

    _reject_overlapping_operations(operations)
    return operations


def apply_settings_patch(
    raw_settings: JsonObject,
    operations: list[SettingsPatchOperation],
) -> tuple[JsonObject, tuple[str, ...]]:
    """Return a validated candidate settings mapping for an atomic patch."""

    original = deepcopy(raw_settings)
    candidate = deepcopy(raw_settings)
    _prepare_structured_patch(candidate, operations)
    changed_paths: list[str] = []
    for operation in operations:
        raw_path = _raw_path(operation.resolved.path.values)
        if operation.operation == "set":
            if operation.resolved.path.values == ("server", "port"):
                before = next(
                    (
                        original[key]
                        for key in ("server_port", "SERVER_PORT", "port", "PORT")
                        if key in original
                    ),
                    _MISSING,
                )
                for key in PORT_SETTING_KEYS:
                    candidate.pop(key, None)
            else:
                before = _lookup(original, raw_path, _MISSING)
            _set_nested(candidate, raw_path, operation.value)
            if before is _MISSING or before != operation.value:
                changed_paths.append(operation.resolved.canonical_path)
            continue

        before = _lookup(original, raw_path, _MISSING)
        if operation.resolved.path.values == ("server", "port"):
            before = next(
                (
                    original[key]
                    for key in ("server_port", "SERVER_PORT", "port", "PORT")
                    if key in original
                ),
                _MISSING,
            )
            for key in PORT_SETTING_KEYS:
                candidate.pop(key, None)
        elif (
            operation.resolved.path.values[0] == "model_tasks"
            and operation.resolved.path.values[-1] == "target"
        ):
            _delete_nested(candidate, raw_path[:-1])
        else:
            _delete_nested(candidate, raw_path)
        if before is not _MISSING:
            changed_paths.append(operation.resolved.canonical_path)

    errors = [
        diagnostic
        for diagnostic in validate_settings_data(candidate)
        if diagnostic.severity == "error"
    ]
    if errors:
        details = "; ".join(f"{item.path}: {item.message}" for item in errors)
        raise SettingsPathError(f"invalid settings patch: {details}")

    try:
        build_effective_settings(candidate)
    except (StorageError, TypeError, ValueError) as error:
        raise SettingsPathError(f"invalid settings patch: {error}") from error

    return candidate, tuple(changed_paths)


def setting_details(
    raw_settings: JsonObject,
    raw_path: str | SettingsPath,
    *,
    allow_missing: bool = False,
) -> JsonObject:
    """Return catalog metadata plus effective/configured state for one path."""

    resolved = resolve_setting(raw_path)
    effective = build_effective_settings(raw_settings)
    public_values = resolved.path.values
    raw_values = _raw_path(public_values)
    value = _lookup(effective, public_values, _MISSING)
    configured_value = _lookup(raw_settings, raw_values, _MISSING)
    if public_values == ("server", "port"):
        configured_value = next(
            (
                raw_settings[key]
                for key in ("server_port", "SERVER_PORT", "port", "PORT")
                if key in raw_settings
            ),
            _MISSING,
        )

    if value is _MISSING and resolved.definition.has_default:
        value = deepcopy(resolved.definition.default)
    if value is _MISSING and not allow_missing:
        raise SettingsPathError(f"settings path {resolved.canonical_path!r} is not configured")

    details = _definition_payload(resolved.definition)
    details.update(
        {
            "path": resolved.canonical_path,
            "configured": configured_value is not _MISSING,
            "source": (
                "configured"
                if configured_value is not _MISSING
                else "default"
                if value is not _MISSING
                else "unconfigured"
            ),
            "restart_required": False,
        }
    )
    if value is not _MISSING:
        details["value"] = deepcopy(value)
    if configured_value is not _MISSING:
        details["configured_value"] = deepcopy(configured_value)
    return details


def catalog_payload(prefix: str | None = None) -> list[JsonObject]:
    """Return definition metadata, optionally filtered by path prefix."""

    normalized_prefix = (prefix or "").strip()
    entries = []
    for definition in _DEFINITIONS:
        if normalized_prefix and not definition.template.startswith(normalized_prefix):
            continue
        entries.append({"path": definition.template, **_definition_payload(definition)})
    return entries


def _definition_payload(definition: SettingDefinition) -> JsonObject:
    payload: JsonObject = {
        "template": definition.template,
        "type": definition.value_type,
        "description": definition.description,
        "application": definition.application,
        "nullable": definition.nullable,
        "unsettable": definition.unsettable,
        "has_default": definition.has_default,
    }
    if definition.has_default:
        payload["default"] = deepcopy(definition.default)
    if definition.allowed_values:
        payload["allowed_values"] = list(definition.allowed_values)
    if definition.minimum is not None:
        payload["minimum"] = definition.minimum
        payload["exclusive_minimum"] = definition.exclusive_minimum
    if definition.maximum is not None:
        payload["maximum"] = definition.maximum
    return payload


def _prepare_structured_patch(
    candidate: JsonObject,
    operations: list[SettingsPatchOperation],
) -> None:
    """Seed discriminator-owned objects before applying independent leaf edits."""

    compaction = candidate.get("compaction")
    if not isinstance(compaction, dict):
        return

    trigger_operations = [
        operation
        for operation in operations
        if operation.operation == "set"
        and operation.resolved.path.values[:2] == ("compaction", "trigger")
    ]
    strategy_operations = [
        operation
        for operation in operations
        if operation.operation == "set"
        and operation.resolved.path.values[:2] == ("compaction", "strategy")
    ]
    for operation in trigger_operations:
        if operation.operation == "set" and operation.resolved.path.values == (
            "compaction",
            "trigger",
            "type",
        ):
            current = compaction.get("trigger")
            current_type = (
                current.get("type", "context_ratio")
                if isinstance(current, dict)
                else "context_ratio"
            )
            if current_type != operation.value:
                compaction["trigger"] = {}
    for operation in strategy_operations:
        if operation.operation == "set" and operation.resolved.path.values == (
            "compaction",
            "strategy",
            "type",
        ):
            current = compaction.get("strategy")
            current_type = (
                current.get("type", "summary_tail") if isinstance(current, dict) else "summary_tail"
            )
            if current_type != operation.value:
                compaction["strategy"] = {}


def _match_definition(
    pattern: tuple[DefinitionSegment, ...],
    segments: tuple[PathSegment, ...],
) -> dict[str, Any] | None:
    has_remainder = bool(pattern) and isinstance(pattern[-1], DynamicRemainder)
    minimum_length = len(pattern) if not has_remainder else len(pattern)
    if (not has_remainder and len(segments) != len(pattern)) or (
        has_remainder and len(segments) < minimum_length
    ):
        return None

    parameters: dict[str, Any] = {}
    for index, expected in enumerate(pattern):
        if isinstance(expected, DynamicRemainder):
            remaining = segments[index:]
            if not remaining or any(not segment.quoted for segment in remaining):
                return None
            parameters[expected.name] = tuple(segment.value for segment in remaining)
            return parameters
        actual = segments[index]
        if isinstance(expected, DynamicSegment):
            if not actual.quoted:
                return None
            parameters[expected.name] = actual.value
            continue
        if actual.quoted or actual.value != expected:
            return None
    return parameters


def _validate_dynamic_parameters(parameters: dict[str, Any]) -> None:
    task = parameters.get("task")
    if task is not None and task not in SUPPORTED_TASK_TYPES:
        allowed = ", ".join(sorted(SUPPORTED_TASK_TYPES))
        raise SettingsPathError(f"unsupported model task {task!r}; available tasks: {allowed}")
    model = parameters.get("model")
    if model is not None and not isinstance(model, str):
        raise SettingsPathError("dynamic Model key must be a string")
    connection = parameters.get("connection")
    if connection is not None and ":" not in connection:
        raise SettingsPathError("Provider Connection key must be '<provider>:<connection>'")


def _validate_value(definition: SettingDefinition, value: Any, path: str) -> None:
    if value is None:
        if definition.nullable:
            return
        raise SettingsPathError(f"{path} does not accept null")

    valid = {
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(float(value)),
        "string": isinstance(value, str),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
        "json": True,
    }.get(definition.value_type, False)
    if not valid:
        raise SettingsPathError(f"{path} must be {definition.value_type}")
    if definition.non_empty and isinstance(value, str) and not value.strip():
        raise SettingsPathError(f"{path} must be a non-empty string")
    if definition.allowed_values and value not in definition.allowed_values:
        allowed = ", ".join(repr(item) for item in definition.allowed_values)
        raise SettingsPathError(f"{path} must be one of: {allowed}")
    if definition.minimum is not None and isinstance(value, int | float):
        below = (
            value <= definition.minimum
            if definition.exclusive_minimum
            else value < definition.minimum
        )
        if below:
            operator = "greater than" if definition.exclusive_minimum else "at least"
            raise SettingsPathError(f"{path} must be {operator} {definition.minimum:g}")
    if (
        definition.maximum is not None
        and isinstance(value, int | float)
        and value > definition.maximum
    ):
        raise SettingsPathError(f"{path} must be at most {definition.maximum:g}")


def _reject_overlapping_operations(operations: list[SettingsPatchOperation]) -> None:
    paths = [operation.resolved.path.values for operation in operations]
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            shared = min(len(path), len(other))
            if path[:shared] == other[:shared]:
                raise SettingsPathError(
                    "settings.patch operations must not duplicate or overlap paths: "
                    f"{_format_values(path)} and {_format_values(other)}"
                )


def _raw_path(public_path: tuple[str, ...]) -> tuple[str, ...]:
    if public_path == ("server", "port"):
        return ("server_port",)
    if public_path == ("server", "keep_awake"):
        return ("keep_awake",)
    if public_path == ("server", "timezone"):
        return ("timezone",)
    prefix_mappings = {
        ("skills", "directories"): ("skill_directories",),
        ("extensions", "directories"): ("extension_directories",),
        ("attachments", "max_size_bytes"): ("attachment_max_size_bytes",),
        ("speech", "upload_max_size_bytes"): ("speech_upload_max_size_bytes",),
    }
    for public_prefix, raw_prefix in prefix_mappings.items():
        if public_path[: len(public_prefix)] == public_prefix:
            return raw_prefix + public_path[len(public_prefix) :]
    if public_path and public_path[0] == "subagents" and len(public_path) == 2:
        return (public_path[1],)
    return public_path


def _lookup(root: Any, path: tuple[str, ...], default: Any) -> Any:
    current = root
    for segment in path:
        if not isinstance(current, dict) or segment not in current:
            return default
        current = current[segment]
    return current


def _set_nested(root: JsonObject, path: tuple[str, ...], value: Any) -> None:
    current = root
    for segment in path[:-1]:
        child = current.get(segment)
        if child is None:
            child = {}
            current[segment] = child
        if not isinstance(child, dict):
            raise SettingsPathError(
                f"cannot set {_format_values(path)} because {_format_values(path[:-1])} "
                "is not an object"
            )
        current = child
    current[path[-1]] = deepcopy(value)


def _delete_nested(root: JsonObject, path: tuple[str, ...]) -> None:
    parents: list[tuple[JsonObject, str]] = []
    current = root
    for segment in path[:-1]:
        child = current.get(segment)
        if not isinstance(child, dict):
            return
        parents.append((current, segment))
        current = child
    current.pop(path[-1], None)
    for parent, segment in reversed(parents):
        child = parent.get(segment)
        if isinstance(child, dict) and not child:
            parent.pop(segment, None)


def _format_input_path(path: SettingsPath) -> str:
    output = ""
    for segment in path.segments:
        if segment.quoted:
            output += f"[{json.dumps(segment.value, ensure_ascii=False)}]"
        else:
            output += ("." if output else "") + segment.value
    return output


def _format_values(path: tuple[str, ...]) -> str:
    return ".".join(path)


__all__ = [
    "APPLICATION_LIVE",
    "APPLICATION_RESTART",
    "DEFAULT_ATTACHMENT_MAX_SIZE_BYTES",
    "DEFAULT_SERVER_PORT",
    "DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES",
    "DefinitionSegment",
    "DynamicRemainder",
    "DynamicSegment",
    "JsonObject",
    "PathSegment",
    "ResolvedSetting",
    "SUBAGENT_SETTING_DEFAULTS",
    "SettingDefinition",
    "SettingsPatchOperation",
    "SettingsPath",
    "SettingsPathError",
    "apply_settings_patch",
    "build_effective_settings",
    "catalog_payload",
    "parse_patch_operations",
    "parse_settings_path",
    "resolve_setting",
    "setting_definitions",
    "setting_details",
]
