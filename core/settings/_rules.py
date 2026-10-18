"""Shared Settings scalar rules, field contracts and limits."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from tzlocal import get_localzone_name

from core.settings.agent_defaults import MAX_TEMPERATURE, MAX_TOP_P, MIN_TEMPERATURE, MIN_TOP_P

JsonObject = dict[str, Any]


# Structural shape of a recall backend name (lowercase snake_case). Whether a
# name actually resolves to a registered backend is a runtime concern checked
# against the recall registry (built-ins + extension backends) at the RPC layer,
# not here — the parser only enforces the shape.
RECALL_BACKEND_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


ALLOWED_THINKING_EFFORTS = frozenset(
    {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}
)


# Upper bound for one Agent's ordered fallback-model chain. Shared by the agent
# store's save-time validation and the chat loop's defensive runtime slice; a
# bounded chain keeps a misconfigured cascade from stacking unbounded retries.
MAX_FALLBACK_MODELS = 5


# Single authority for the agent-id format (filesystem-safe slug): a leading
# letter or digit, then up to 63 more of letter/digit/hyphen/underscore. Shared
# by file-schema validation, the agent store, and prompt-fragment storage. Every
# reader accepts these; creating or renaming an Agent also refuses names Windows
# reserves (``core.utils.ids.is_reserved_name``), such as ``con`` or ``lpt1``.
AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


# Single authority for the project-id format (filesystem-safe slug). Same shape
# as the agent-id rule: a leading letter or digit, then up to 63 more of
# letter/digit/hyphen/underscore. Shared by the project store and the central
# file-schema validator. Lives here (not in core.projects) so validation.py can
# import it without an import cycle through the projects package. Creating a
# Project also refuses names Windows reserves (``core.utils.ids.is_reserved_name``).
PROJECT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


# Tool allowlists generally use ``*`` as the all-tools wildcard, but a Project
# Tool Whitelist is a security ceiling assembled from explicit tool names. Keep
# the spelling in the low-level settings contract so both raw ``project.json``
# validation and the Project entity reject the same forbidden value without an
# import cycle through ``core.tools``.
PROJECT_TOOL_ALLOWLIST_WILDCARD = "*"


# Appearance chat-width preference (the WebUI chat reading-column width). The
# constant lives here, not in normalizers.py, so the public update parser can
# validate membership without a circular import (normalizers imports settings).
DEFAULT_APPEARANCE_CHAT_WIDTH = "comfortable"


SUPPORTED_APPEARANCE_CHAT_WIDTHS = frozenset({"comfortable", "wide", "full"})


# Appearance preference for inline Thinking/Tool detail versus compact Working
# disclosures in Chat. It is display-only and applies live in accessors.
DEFAULT_APPEARANCE_CHAT_WORKING_MODE = "normal"


SUPPORTED_APPEARANCE_CHAT_WORKING_MODES = frozenset({"normal", "compact"})


SETTINGS_UPDATE_SECTIONS = frozenset(
    {
        "appearance",
        "archive",
        "debug",
        "server",
        "skills",
        "subagents",
        "compaction",
        "defaults",
        "recall",
        "model_tasks",
        "providers",
        "web_search",
        "web_fetch",
        "extensions",
        "reflection",
        "librarian",
        "local_models",
        "session_titles",
        "speech",
        "notifications",
    }
)


# Desktop notification kinds the Windows tray application shows. Each field is
# an independent boolean switch; defaults live in ``normalizers.py``.
NOTIFICATION_FIELDS = (
    "run_completed",
    "run_failed",
    "automation_failed",
    "update_result",
    "server_stopped",
)


OPENROUTER_ROUTING_MODES = frozenset({"automatic", "allowed", "ordered"})


OPENROUTER_ROUTING_FIELDS = frozenset({"default", "models"})


OPENROUTER_ROUTING_POLICY_FIELDS = frozenset({"mode", "providers", "blocked", "allow_fallbacks"})


OPENROUTER_PROVIDER_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")


OPENROUTER_PROVIDER_SLUG_MAX_LENGTH = 128


OPENROUTER_MODEL_ID_MAX_LENGTH = 256


REFLECTION_INTERVAL_FIELDS = ("memory_turn_interval", "skill_model_step_interval")


# The Librarian section: switches, and day counts from 1 to 3650 (ten years,
# like archive retention), which keeps every date computed from them valid.
LIBRARIAN_BOOLEAN_FIELDS = ("enabled", "consolidate")


LIBRARIAN_DAY_FIELDS = ("interval_days", "archive_after_days")


MAX_LIBRARIAN_DAYS = 3650


LIBRARIAN_DAYS_RULE = f"must be an integer from 1 to {MAX_LIBRARIAN_DAYS}"


SUBAGENT_SETTING_FIELDS = (
    "max_subagent_depth",
    "max_active_subagents",
    "max_active_subagents_total",
)


# How many days an archive entry rests before vBot deletes it permanently;
# ``None`` keeps archived items until they are deleted by hand. The default lives
# in ``normalizers.py``.
MIN_ARCHIVE_RETENTION_DAYS = 1


MAX_ARCHIVE_RETENTION_DAYS = 3650


ARCHIVE_RETENTION_DAYS_RULE = (
    f"must be an integer from {MIN_ARCHIVE_RETENTION_DAYS} to {MAX_ARCHIVE_RETENTION_DAYS}, "
    "or null to keep archived items until they are deleted"
)


class SettingsValidationError(ValueError):
    """Raised when a public Settings payload is malformed."""


def validate_timezone_name(value: Any, *, label: str) -> str:
    """Validate and normalize one IANA timezone name."""
    if not isinstance(value, str) or not value.strip():
        raise SettingsValidationError(f"{label} must be a non-empty IANA timezone name")
    normalized = value.strip()
    try:
        ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise SettingsValidationError(f"{label} is not a known IANA timezone") from error
    return normalized


def default_timezone_name() -> str:
    """Return the host IANA zone used when Settings has no explicit override."""
    try:
        return validate_timezone_name(get_localzone_name(), label="system timezone")
    except SettingsValidationError, OSError:
        return "UTC"


def is_archive_retention_days(value: Any) -> bool:
    """Return whether ``value`` is a valid ``archive.retention_days``, including ``None``."""
    if value is None:
        return True
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and MIN_ARCHIVE_RETENTION_DAYS <= value <= MAX_ARCHIVE_RETENTION_DAYS
    )


def available_timezone_names() -> tuple[str, ...]:
    """Return the runtime's deterministic IANA timezone catalog."""
    return tuple(sorted(available_timezones()))


def effective_timezone_name(settings: Mapping[str, Any]) -> str:
    """Resolve the configured application timezone with the host zone as default."""
    configured = settings.get("timezone")
    if configured is None:
        return default_timezone_name()
    return validate_timezone_name(configured, label="settings.timezone")


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsValidationError(f"{label} must be a positive integer")
    if value <= 0:
        raise SettingsValidationError(f"{label} must be a positive integer")
    return cast("int", value)


def is_valid_agent_id(value: Any) -> bool:
    """Return whether ``value`` is a filesystem-safe agent id per the canonical rule."""
    return isinstance(value, str) and AGENT_ID_PATTERN.fullmatch(value) is not None


def is_valid_project_id(value: Any) -> bool:
    """Return whether ``value`` is a filesystem-safe project id per the canonical rule."""
    return isinstance(value, str) and PROJECT_ID_PATTERN.fullmatch(value) is not None


def validate_temperature(
    value: Any,
    *,
    label: str,
    allow_none: bool = False,
) -> float | None:
    """Validate one agent ``temperature`` value against the canonical schema rules."""
    return _validate_sampling_value(
        value, label=label, allow_none=allow_none, minimum=MIN_TEMPERATURE, maximum=MAX_TEMPERATURE
    )


def validate_top_p(
    value: Any,
    *,
    label: str,
    allow_none: bool = False,
) -> float | None:
    """Validate one agent ``top_p`` value against the canonical schema rules."""
    return _validate_sampling_value(
        value, label=label, allow_none=allow_none, minimum=MIN_TOP_P, maximum=MAX_TOP_P
    )


def _validate_sampling_value(
    value: Any, *, label: str, allow_none: bool, minimum: float, maximum: float
) -> float | None:
    if value is None:
        if allow_none:
            return None
        raise SettingsValidationError(f"{label} must be a number")

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SettingsValidationError(f"{label} must be a number")
    if value < minimum or value > maximum:
        raise SettingsValidationError(f"{label} must be between {minimum:g} and {maximum:g}")
    number = float(value)
    if not math.isfinite(number):
        raise SettingsValidationError(f"{label} must be finite")
    return number


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
